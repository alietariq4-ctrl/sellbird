"""Router and factory tests. Deterministic, offline."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx
import pytest

from harvey_brain.base import Usage
from harvey_brain.openai_compat import OpenAICompatTransport
from harvey_brain.router import RouterTransport
from harvey_brain.config import BrainSettings, ChainEntry, load_settings
from harvey_brain.router_factory import build_chain, doctor
from harvey_brain.spec import get_provider


def run(coro):
    return asyncio.run(coro)


def body(text="hello", cost=None, usage=None):
    payload = {
        "model": "m",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": usage if usage is not None else {
            "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
        },
    }
    if cost is not None:
        payload["usage"]["cost"] = cost
    return payload


class Fake:
    """A transport with a scripted outcome. Counts calls."""

    def __init__(self, name, outcome="ok", text="from-" + "x", paused="",
                 cost=0.0, error=""):
        self.name = name
        self.paused_reason = paused
        self.last_error = ""
        self.outcome = outcome
        self.error = error
        self.text = text
        self.cost = cost
        self.calls = 0
        self.last_usage: Usage | None = None
        self.closed = False

    async def __call__(self, prompt, *, agent="", task="", timeout=300.0,
                       expect_json=False):
        self.calls += 1
        if self.outcome == "fail":
            self.last_error = self.error or "429"
            return ""
        self.last_error = ""
        self.last_usage = Usage(cost_usd=self.cost, provider=self.name)
        return self.text

    async def aclose(self):
        self.closed = True


# 9 ── failover
def test_primary_429_falls_through_to_the_secondary():
    primary = Fake("openrouter", outcome="fail", error="429 rate limited")
    secondary = Fake("xai", text="second answer")

    router = RouterTransport([primary, secondary])
    assert run(router("hi")) == "second answer"
    assert router.last_provider == "xai"
    assert (primary.calls, secondary.calls) == (1, 1)
    assert router.paused_reason == ""


# 10 ── exhausted
def test_all_failing_names_the_count_and_the_last_error():
    first = Fake("openrouter", outcome="fail", error="429 rate limited")
    second = Fake("xai", outcome="fail", error="500 upstream")
    router = RouterTransport([first, second])

    assert run(router("hi")) == ""
    reason = router.paused_reason
    assert "all 2 providers failed" in reason
    assert "xai" in reason and "500" in reason


# 11 ── paused providers are skipped
def test_a_paused_provider_is_skipped_without_a_call():
    paused = Fake("openrouter", paused="out of credits")
    live = Fake("gemini", text="ok")
    router = RouterTransport([paused, live])

    assert run(router("hi")) == "ok"
    assert paused.calls == 0
    assert live.calls == 1


def test_every_provider_paused_means_the_router_pauses():
    a = Fake("openrouter", paused="bad key")
    b = Fake("xai", paused="out of credits")
    router = RouterTransport([a, b])

    assert run(router("hi")) == ""
    assert router.paused_reason
    assert "every provider is paused" in router.paused_reason
    assert a.calls == 0 and b.calls == 0


def test_one_live_provider_keeps_the_router_unpaused():
    router = RouterTransport([Fake("a", paused="bad key"), Fake("b")])
    assert router.paused_reason == ""


# 12 ── budget
def test_budget_cap_blocks_and_names_the_dollars():
    expensive = Fake("openrouter", cost=0.60)
    router = RouterTransport([expensive], budget_usd_per_day=1.0)

    assert run(router("hi")) == "from-x"
    assert router.spend_usd == pytest.approx(0.60)
    assert run(router("hi")) == "from-x"
    assert router.spend_usd == pytest.approx(1.20)

    assert run(router("hi")) == ""
    assert expensive.calls == 2
    assert "$1.20" in router.paused_reason
    assert "$1.00" in router.paused_reason


def test_external_ledger_is_honoured():
    calls: list[tuple] = []

    def ledger(spend: float, provider: str) -> float:
        # A real ledger reads the host's SQLite sum; this one flips once the
        # router has told it which provider just spent.
        calls.append((spend, provider))
        return 4.00 if not provider else 5.00

    transport = Fake("openrouter", cost=0.1)
    router = RouterTransport([transport], budget_usd_per_day=5.0, ledger=ledger)

    assert run(router("hi")) == "from-x"
    assert calls[0] == (0.0, "")
    # The ledger now reports $5.00, so the cap is already reached.
    assert run(router("hi")) == ""
    assert transport.calls == 1
    assert "$5.00" in router.paused_reason


def test_no_budget_never_pauses_for_money():
    router = RouterTransport([Fake("openrouter", cost=99.0)],
                             budget_usd_per_day=None)
    for _ in range(3):
        assert run(router("hi")) == "from-x"
    assert router.spend_usd == pytest.approx(297.0)


# 13 ── determinism
def test_the_same_failure_pattern_always_walks_the_same_order():
    def walk() -> list[str]:
        order: list[str] = []

        class Recorder(Fake):
            async def __call__(self, *a, **kw):
                order.append(self.name)
                return await super().__call__(*a, **kw)

        router = RouterTransport([
            Recorder("openrouter", outcome="fail"),
            Recorder("xai", outcome="fail"),
            Recorder("gemini", text="ok"),
            Recorder("ollama"),
        ])
        assert run(router("hi")) == "ok"
        return order

    first = walk()
    assert first == ["openrouter", "xai", "gemini"]
    for _ in range(4):
        assert walk() == first


def test_real_transports_fail_over_in_configured_order():
    seen: list[str] = []

    def make_transport(provider, status):
        spec = get_provider(provider)

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(provider)
            if status != 200:
                return httpx.Response(status, text="nope")
            return httpx.Response(200, json=body(f"answer from {provider}"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return OpenAICompatTransport(spec, "k", spec.default_model, http=client,
                                     max_retries=0, retry_base_delay=0.0)

    router = RouterTransport([make_transport("openrouter", 503),
                              make_transport("gemini", 200)])
    assert run(router("hi")) == "answer from gemini"
    assert seen == ["openrouter", "gemini"]
    assert router.last_provider == "gemini"


def test_failure_hold_pauses_then_clears_itself():
    transport = Fake("only", outcome="fail")
    router = RouterTransport([transport], failure_hold_seconds=0.05)

    assert run(router("hi")) == ""
    assert router.paused_reason
    assert "all 1 providers failed" in router.paused_reason

    # While the hold is active the host would skip the call entirely.
    time.sleep(0.12)
    assert router.paused_reason == ""
    assert transport.calls == 1


# ── factory ──
def test_load_settings_reads_yaml(tmp_path, monkeypatch):
    config = tmp_path / "brain.yaml"
    config.write_text(
        "budget:\n  max_usd_per_day: 2.5\nchain:\n"
        "  - provider: openrouter\n    model: anthropic/claude-sonnet-5.5\n"
        "    json_mode: json_object\n"
        "  - provider: ollama\n    model: llama3.2\n",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.max_usd_per_day == pytest.approx(2.5)
    assert [e.provider for e in settings.chain] == ["openrouter", "ollama"]
    assert settings.chain[0].json_mode == "json_object"


def test_load_settings_rejects_an_empty_chain(tmp_path):
    config = tmp_path / "brain.yaml"
    config.write_text("chain: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        load_settings(config)


def test_load_settings_rejects_unknown_entry_keys(tmp_path):
    config = tmp_path / "brain.yaml"
    config.write_text(
        "chain:\n  - provider: openrouter\n    model: m\n    modle: typo\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="modle"):
        load_settings(config)


def test_build_chain_skips_providers_without_a_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    settings = BrainSettings(chain=[
        ChainEntry(provider="openrouter"),
        ChainEntry(provider="xai"),
        ChainEntry(provider="ollama"),
    ])
    names = [t.name for t in build_chain(settings)]
    assert names == ["xai", "ollama"]


def test_build_chain_uses_an_explicit_env_name(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("MY_OR_KEY", "k")
    settings = BrainSettings(chain=[
        ChainEntry(provider="openrouter", api_key_env="MY_OR_KEY"),
    ])
    chain = build_chain(settings)
    assert len(chain) == 1 and chain[0].api_key == "k"


def test_build_chain_reports_an_unknown_provider():
    with pytest.raises(ValueError, match="unknown provider"):
        build_chain(BrainSettings(chain=[ChainEntry(provider="nope")]))


def test_chain_order_is_preserved_exactly(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k1")
    monkeypatch.setenv("XAI_API_KEY", "k2")
    monkeypatch.setenv("GEMINI_API_KEY", "k3")
    settings = BrainSettings(chain=[
        ChainEntry(provider="gemini"),
        ChainEntry(provider="openrouter"),
        ChainEntry(provider="xai"),
    ])
    assert [t.name for t in build_chain(settings)] == [
        "gemini", "openrouter", "xai"]


# 14 ── doctor
def test_doctor_reports_configuration_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "present")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = tmp_path / "brain.yaml"
    config.write_text(
        "chain:\n"
        "  - provider: openrouter\n    model: m1\n"
        "  - provider: xai\n    model: m2\n"
        "  - provider: ollama\n",
        encoding="utf-8",
    )
    rows = doctor(config)
    by_name = {row["name"]: row for row in rows}

    assert by_name["openrouter"]["configured"] is True
    assert by_name["openrouter"]["model"] == "m1"
    assert by_name["xai"]["configured"] is False
    assert "XAI_API_KEY" in by_name["xai"]["error"]
    assert by_name["ollama"]["configured"] is True  # keyless
    assert all(row["reachable"] == "not checked" for row in rows)


def test_doctor_without_a_config_still_lists_providers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARVEY_BRAIN_CONFIG", "")
    import sellbird.paths
    from sellbird.workspace_home import Home, use_home
    monkeypatch.setattr(sellbird.paths, "PROJECT_ROOT", tmp_path)
    for name in ("OPENROUTER_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY",
                 "OPENAI_BASE_URL", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
                 "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY",
                 "FIREWORKS_API_KEY", "CEREBRAS_API_KEY", "SELLBIRD_HOME"):
        monkeypatch.delenv(name, raising=False)

    with use_home(Home.for_root(tmp_path)):
        rows = doctor(None)
    names = {row["name"] for row in rows}
    assert {"openrouter", "xai", "ollama", "claude_cli"} <= names
    assert all(row["reachable"] == "not checked" for row in rows)


def test_doctor_never_touches_the_network_by_default(monkeypatch, tmp_path):
    """Any accidental HTTP call would raise: there is no transport to make one."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("HARVEY_BRAIN_CONFIG", str(tmp_path / "missing.yaml"))

    def boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("doctor made a network call")

    import harvey_brain.router_factory as factory
    monkeypatch.setattr(factory, "_probe", boom)
    doctor(None)
