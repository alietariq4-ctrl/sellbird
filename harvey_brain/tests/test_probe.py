"""probe: dry run by default, one minimal real call with --live --yes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from harvey_brain import probe
from harvey_brain.openai_compat import OpenAICompatTransport
from harvey_brain.spec import get_provider

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_KEY = "sk-or-v1-" + "Z" * 32


def run(coro):
    return asyncio.run(coro)


def transport(provider="openrouter", model="anthropic/claude-sonnet-5.5", seen=None):
    spec = get_provider(provider)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 1,
                      "total_tokens": 10, "cost": 0.00002},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatTransport(spec, FAKE_KEY, model, http=client,
                                 max_retries=0, retry_base_delay=0.0)


# ── dry run ──
def test_dry_run_makes_zero_network_calls(monkeypatch):
    """A dry run must not construct a client or send anything.

    The spy records every request the injected client sees, then the whole
    module's live path is replaced by a tripwire, so any attempt to reach the
    network fails the test.
    """
    attempts: list[str] = []

    def tripwire(*args, **kwargs):  # pragma: no cover - must not run
        attempts.append("network")
        raise AssertionError("dry run made a network call")

    ready = transport()
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setattr(probe, "live", tripwire)
    # Belt and braces: the transport itself must never be called either, which
    # is what would happen if dry_run drifted into a live path.
    monkeypatch.setattr(type(ready), "__call__", tripwire)

    rows = probe.dry_run(chain=[ready])
    assert attempts == []
    assert rows and rows[0].status == "dry-run"
    assert rows[0].latency_ms == 0
    assert rows[0].cost_usd == 0.0


def test_dry_run_reports_the_endpoint_model_and_auth_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    rows = probe.dry_run(chain=[transport()])
    row = rows[0]
    assert row.provider == "openrouter"
    assert row.model == "anthropic/claude-sonnet-5.5"
    assert row.endpoint == "https://openrouter.ai/api/v1/chat/completions"
    assert row.auth_env == "OPENROUTER_API_KEY"
    assert row.key_present is True
    # 8k in / 1.2k out on claude-sonnet-5.5 = $0.028 (doc facts, arithmetic)
    assert row.est_cost_usd == pytest.approx(0.028)


def test_dry_run_never_prints_the_key_value(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    rows = probe.dry_run(chain=[transport()])
    rendered = probe.render(rows, live_mode=False)

    assert FAKE_KEY not in rendered
    assert "set (41 chars)" in rendered
    assert "ZZZZ" not in rendered


def test_dry_run_flags_a_missing_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    rows = probe.dry_run(chain=[transport(provider="xai", model="grok-4.7")])
    assert rows[0].key_present is False
    assert rows[0].key_hint == "empty"


def test_keyless_provider_reads_as_present(monkeypatch):
    rows = probe.dry_run(chain=[transport(provider="ollama", model="llama3.2")])
    assert rows[0].key_present is True
    assert rows[0].key_hint == "not needed"
    assert rows[0].auth_env == "— (keyless)"


def test_delegates_are_reported_as_delegated_not_http(monkeypatch):
    from harvey_brain.delegate import DelegateTransport
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    rows = probe.dry_run(chain=[transport(), DelegateTransport("claude_cli")])
    by_name = {row.provider: row for row in rows}
    assert by_name["claude_cli"].status == "delegated"
    assert by_name["claude_cli"].endpoint == "host runtime"
    assert by_name["claude_cli"].est_cost_usd == 0.0


def test_render_is_a_table_with_a_header():
    from harvey_brain.probe import ProbeRow
    rendered = probe.render([ProbeRow(
        provider="openrouter", model="m", endpoint="https://x/v1/chat/completions",
        auth_env="OPENROUTER_API_KEY", key_present=True, key_hint="set (4 chars)",
        est_cost_usd=0.01,
    )], live_mode=False)
    assert "provider" in rendered and "est/call" in rendered
    assert "$0.0100" in rendered


# ── live ──
def test_live_refuses_without_confirmation():
    with pytest.raises(PermissionError, match="--yes"):
        run(probe.live([transport()], confirm=False))


def test_live_makes_one_minimal_call_per_provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    seen: list[dict] = []
    rows = run(probe.live([transport(seen=seen)], confirm=True))

    assert len(seen) == 1
    payload = seen[0]
    assert payload["max_tokens"] == probe.LIVE_MAX_TOKENS
    assert payload["messages"][0]["content"] == probe.LIVE_PROMPT

    assert rows[0].status == "ok"
    assert rows[0].input_tokens == 9
    assert rows[0].output_tokens == 1
    assert rows[0].cost_usd == pytest.approx(0.00002)
    assert rows[0].latency_ms >= 0


def test_live_reports_an_http_error_without_raising(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    spec = get_provider("openrouter")
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(404, json={"error": "model_not_found"})))
    transport_ = OpenAICompatTransport(
        spec, FAKE_KEY, "nope", http=client, max_retries=0, retry_base_delay=0.0)
    rows = run(probe.live([transport_], confirm=True, http=client))
    assert rows[0].status == "failed"
    # The transport's own last_error carries the status and the masked excerpt.
    assert "404" in rows[0].error
    assert "model_not_found" in rows[0].error


def test_live_falls_back_to_the_price_table_when_no_cost_is_reported(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    spec = get_provider("openrouter")
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            "model": "anthropic/claude-haiku-5.5",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                      "total_tokens": 1_000_000},
        })))
    transport_ = OpenAICompatTransport(
        spec, FAKE_KEY, "anthropic/claude-haiku-5.5", http=client)
    rows = run(probe.live([transport_], confirm=True, http=client))
    assert rows[0].status == "ok"
    assert rows[0].cost_usd == pytest.approx(0.10)


# ── CLI ──
def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("OPENROUTER_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY",
                "OPENAI_API_KEY", "HARVEY_BRAIN_CONFIG"):
        env.pop(key, None)
    env.update(extra)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def test_cli_dry_run_prints_the_table_and_the_key_length_only(tmp_path):
    config = tmp_path / "brain.yaml"
    config.write_text("chain:\n  - provider: openrouter\n  - provider: ollama\n",
                      encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "harvey_brain", "probe", "--config", str(config)],
        capture_output=True, text=True, timeout=60, env=_env(OPENROUTER_API_KEY=FAKE_KEY),
    )
    assert result.returncode == 0, result.stderr
    assert "dry run (no network)" in result.stdout
    assert "openrouter" in result.stdout and "ollama" in result.stdout
    assert "set (41 chars)" in result.stdout
    assert FAKE_KEY not in result.stdout
    assert "Traceback" not in result.stderr


def test_cli_live_without_yes_refuses_and_says_so(tmp_path):
    config = tmp_path / "brain.yaml"
    config.write_text("chain:\n  - provider: ollama\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "harvey_brain", "probe", "--config", str(config),
         "--live"],
        capture_output=True, text=True, timeout=60, env=_env(),
    )
    assert result.returncode == 2
    assert "--yes" in result.stdout
    assert "Traceback" not in result.stderr


def test_cli_reports_a_missing_config_without_a_traceback(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "harvey_brain", "probe",
         "--config", str(tmp_path / "nope.yaml")],
        capture_output=True, text=True, timeout=60, env=_env(),
    )
    assert result.returncode == 2
    assert "not found" in result.stdout
    assert "Traceback" not in result.stderr
