"""brain.yaml loading, .env handling, and the end-to-end factory."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from harvey_brain.config import (
    BrainSettings,
    ChainEntry,
    find_config,
    load_dotenv,
    load_settings,
)
from harvey_brain.router_factory import build, build_chain


def yaml_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "brain.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def clear_keys(monkeypatch) -> None:
    for key in ("OPENROUTER_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY",
                "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
                "DEEPSEEK_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
                "TOGETHER_API_KEY", "FIREWORKS_API_KEY", "CEREBRAS_API_KEY",
                "HARVEY_BRAIN_CONFIG", "SELLBIRD_HOME"):
        monkeypatch.delenv(key, raising=False)


TWO_STEP = """
budget:
  max_usd_per_day: 3.0
chain:
  - provider: openrouter
    model: anthropic/claude-haiku-5.5
  - provider: ollama
    model: llama3.2
"""


def test_find_config_prefers_the_explicit_path(tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    explicit = yaml_file(tmp_path, TWO_STEP)
    monkeypatch.chdir(tmp_path)
    assert find_config(explicit) == explicit


def test_find_config_uses_the_env_override(tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    path = yaml_file(tmp_path, TWO_STEP)
    monkeypatch.setenv("HARVEY_BRAIN_CONFIG", str(path))
    monkeypatch.chdir(tmp_path.parent)
    assert find_config(None) == path


def test_find_config_returns_none_when_absent(tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    monkeypatch.chdir(tmp_path)
    import sellbird.paths
    from sellbird.workspace_home import Home, use_home
    monkeypatch.setattr(sellbird.paths, "PROJECT_ROOT", tmp_path)
    with use_home(Home.for_root(tmp_path)):
        assert find_config(None) is None


def test_find_config_raises_for_a_missing_explicit_path(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        find_config(tmp_path / "nope.yaml")


def test_load_dotenv_sets_missing_variables_only(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nOPENROUTER_API_KEY=fresh-key\nexport XAI_API_KEY='quoted'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    load_dotenv(env_file)
    assert os.environ["OPENROUTER_API_KEY"] == "fresh-key"
    assert os.environ["XAI_API_KEY"] == "quoted"

    # An existing value must win: never silently override a real environment.
    monkeypatch.setenv("OPENROUTER_API_KEY", "keep-me")
    load_dotenv(env_file)
    assert os.environ["OPENROUTER_API_KEY"] == "keep-me"


def test_settings_defaults(tmp_path):
    settings = load_settings(yaml_file(tmp_path, "chain:\n  - provider: ollama\n"))
    assert settings.max_usd_per_day == pytest.approx(5.0)
    assert settings.chain[0].json_mode == "auto"


def test_budget_can_be_disabled(tmp_path):
    settings = load_settings(yaml_file(
        tmp_path, "budget:\n  max_usd_per_day: null\nchain:\n  - provider: ollama\n"))
    assert settings.max_usd_per_day is None


def test_entry_optional_fields_are_parsed(tmp_path):
    settings = load_settings(yaml_file(tmp_path, """
chain:
  - provider: xai
    model: grok-4.7
    json_mode: json_object
    reasoning_effort: high
    max_output_tokens: 2048
    temperature: 0.3
    max_retries: 0
    timeout: 45
"""))
    entry = settings.chain[0]
    assert entry.json_mode == "json_object"
    assert entry.reasoning_effort == "high"
    assert entry.max_output_tokens == 2048
    assert entry.temperature == pytest.approx(0.3)
    assert entry.max_retries == 0
    assert entry.timeout == pytest.approx(45.0)


def test_build_raises_when_every_provider_is_missing_its_key(tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    path = yaml_file(tmp_path, "chain:\n  - provider: openrouter\n")
    with pytest.raises(RuntimeError, match="no usable provider"):
        build(path)


def test_build_returns_a_router_that_calls_the_configured_provider(
        tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    path = yaml_file(tmp_path, TWO_STEP)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.host))
        return httpx.Response(200, json={
            "model": "anthropic/claude-haiku-5.5",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1,
                      "total_tokens": 11, "cost": 0.0005},
        })

    import asyncio
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router = build(path, http=client)
    try:
        assert asyncio.run(router("hi")) == "ok"
        assert router.last_provider == "openrouter"
        assert router.last_usage is not None
        assert router.spend_usd == pytest.approx(0.0005)
        assert seen == ["openrouter.ai"]
    finally:
        asyncio.run(client.aclose())


def test_build_skips_a_keyless_provider_only_when_it_lacks_a_key() -> None:
    # ollama needs no key, so it survives on a machine with no credentials.
    settings = BrainSettings(chain=[ChainEntry(provider="ollama")])
    chain = build_chain(settings)
    assert [t.name for t in chain] == ["ollama"]
    assert chain[0].api_key == ""
