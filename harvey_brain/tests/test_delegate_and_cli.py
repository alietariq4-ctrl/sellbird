"""claude_cli / codex_cli delegation, pricing rows, and the doctor CLI."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from harvey_brain import pricing
from harvey_brain.base import Usage
from harvey_brain.delegate import DelegateTransport
from harvey_brain.router import RouterTransport
from harvey_brain.spec import PROVIDERS, get_provider


REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDER_KEYS = (
    "OPENROUTER_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY",
    "HARVEY_BRAIN_CONFIG",
)


def clean_env() -> dict[str, str]:
    """The real environment minus every provider key, so no key can leak in."""
    env = dict(os.environ)
    for key in PROVIDER_KEYS:
        env.pop(key, None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(coro):
    return asyncio.run(coro)


class Fake:
    def __init__(self, name, text="", paused=""):
        self.name = name
        self.text = text
        self.paused_reason = paused
        self.last_error = "500 upstream" if not text else ""
        self.last_usage = None
        self.calls = 0

    async def __call__(self, prompt, *, agent="", task="", timeout=300.0,
                       expect_json=False):
        self.calls += 1
        return self.text


# 16 ── claude_cli is present and delegates
def test_claude_cli_spec_exists_and_delegates():
    spec = PROVIDERS["claude_cli"]
    assert spec.delegates
    assert spec.api_style == "delegate"
    assert PROVIDERS["codex_cli"].delegates


def test_claude_cli_reports_the_host_as_unavailable_when_missing(monkeypatch):
    """No host on sys.path -> a pause sentence, not a crash."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sellbird" or name.startswith("sellbird."):
            raise ImportError("No module named 'sellbird'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    transport = DelegateTransport("claude_cli")
    assert run(transport("hi")) == ""
    assert "host not available" in transport.paused_reason
    assert transport.last_error == ""


def test_claude_cli_uses_the_host_build_command(monkeypatch):
    """Delegation is asserted at the seam: build_command + stdin, no re-implementation."""
    calls: dict[str, object] = {}

    class FakeRuntime:
        REQUIRED_FLAGS = ()

        @staticmethod
        def check_cli(env=None):
            return "claude"

        @staticmethod
        def build_command(executable, model="", session_id=None):
            calls["argv"] = [(executable, model)]
            return [sys.executable, "-c",
                    "import sys,json;"
                    "p=sys.stdin.read();"
                    "print(json.dumps({'type':'result','result':'delegated:'+p,"
                    "'total_cost_usd':0.0042,"
                    "'modelUsage':{'claude-sonnet-5.5':{'inputTokens':10,"
                    "'outputTokens':2,'cacheReadInputTokens':5,'costUSD':0.0042}}}))"]

        @staticmethod
        def runtime_env(cwd, base=None):
            return None  # inherit; the argv above is a python child

    class FakeSellbird:
        claude_runtime = FakeRuntime
        __path__: list[str] = []

    monkeypatch.setitem(sys.modules, "sellbird", FakeSellbird)
    recorded: list[Usage] = []
    transport = DelegateTransport("claude_cli", model="claude-sonnet-5.5",
                                  on_usage=recorded.append)

    assert run(transport("PROMPT")) == "delegated:PROMPT"
    assert calls["argv"] == [("claude", "claude-sonnet-5.5")]
    assert recorded[0].cost_usd == pytest.approx(0.0042)
    assert recorded[0].cached_input_tokens == 5
    assert recorded[0].provider == "claude_cli"


def test_claude_cli_is_used_after_the_paid_providers_fail(monkeypatch):
    """The chain keeps Claude as the last resort, exactly as configured."""
    class FakeRuntime:
        @staticmethod
        def check_cli(env=None):
            return "claude"

        @staticmethod
        def build_command(executable, model="", session_id=None):
            return [sys.executable, "-c",
                    "import sys,json;print(json.dumps({'type':'result','result':'claude answer'}))"]

        @staticmethod
        def runtime_env(cwd, base=None):
            return None

    class FakeSellbird:
        claude_runtime = FakeRuntime
        __path__: list[str] = []

    monkeypatch.setitem(sys.modules, "sellbird", FakeSellbird)

    dead = Fake("openrouter")
    router = RouterTransport([dead, DelegateTransport("claude_cli")])
    assert run(router("hi")) == "claude answer"
    assert router.last_provider == "claude_cli"
    assert dead.calls == 1


def test_codex_cli_reports_a_missing_executable(monkeypatch):
    class FakeCodex:
        @staticmethod
        def find_codex():
            return None

        @staticmethod
        def require_supported_platform():
            return None

    class FakeSellbird:
        codex = FakeCodex
        __path__: list[str] = []

    monkeypatch.setitem(sys.modules, "sellbird", FakeSellbird)
    transport = DelegateTransport("codex_cli")
    assert run(transport("hi")) == ""
    assert "codex" in transport.paused_reason.lower()


# ── pricing ──
def test_free_suffix_is_priced_at_zero():
    assert pricing.estimate("openrouter", "inclusionai/ling-3.2-flash:free",
                            input_tokens=1_000_000) == 0.0


def test_longest_prefix_wins():
    # anthropic/claude-haiku-5.5 ($0.10) must beat anthropic/claude-* ($2.00).
    cost = pricing.estimate("openrouter", "anthropic/claude-haiku-5.5",
                            input_tokens=1_000_000)
    assert cost == pytest.approx(0.10)
    broad = pricing.estimate("openrouter", "anthropic/claude-sonnet-5.5",
                             input_tokens=1_000_000)
    assert broad == pytest.approx(2.00)


def test_cached_tokens_use_the_cached_rate():
    cost = pricing.estimate(
        "openai", "gpt-6-luna",
        input_tokens=1_000_000, cached_input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cost == pytest.approx(0.01)   # $0.01/M cached input


def test_unknown_model_is_free_and_warns_once(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        assert pricing.estimate("openai", "nope-1", input_tokens=1000) == 0.0
        assert pricing.estimate("openai", "nope-1", input_tokens=1000) == 0.0
    assert caplog.text.count("no price row") == 1


def test_every_row_carries_a_source_url():
    for provider, prefix, price in pricing.PRICES:
        assert price.source, f"{provider}/{prefix} has no source"


def test_groq_rows_are_marked_unverified():
    price = pricing.lookup("groq", "llama-3.3-70b-versatile")
    assert price is not None
    assert price.verified is False
    assert "UNVERIFIED" in price.source


def test_ollama_is_free():
    assert pricing.estimate("ollama", "llama3.2", input_tokens=10**6,
                            output_tokens=10**6) == 0.0


# ── doctor CLI ──
def test_doctor_cli_runs_with_no_keys_and_prints_a_table(tmp_path):
    env_file = tmp_path / "brain.yaml"
    env_file.write_text(
        "chain:\n  - provider: openrouter\n  - provider: ollama\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "harvey_brain", "doctor", "--config", str(env_file)],
        capture_output=True, text=True, timeout=60, env=clean_env(),
    )
    assert result.returncode in (0, 1)
    assert "provider" in result.stdout
    assert "openrouter" in result.stdout
    assert "ollama" in result.stdout
    assert "not checked" in result.stdout
    assert "Traceback" not in result.stderr


def test_doctor_cli_without_config_still_prints(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "harvey_brain", "doctor"],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
        env=clean_env(),
    )
    assert "none found" in result.stdout or "brain config:" in result.stdout
    assert "xai" in result.stdout
    assert "Traceback" not in result.stderr
