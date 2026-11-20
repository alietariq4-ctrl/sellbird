"""Per-role routing: scout gets the cheap model, writer the strong one."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from harvey_brain import profile as profiles
from harvey_brain.config import BrainSettings, ChainEntry, load_settings
from harvey_brain.openai_compat import OpenAICompatTransport
from harvey_brain.profile import DEFAULT_PROFILES, TaskProfile, select
from harvey_brain.router import RouterTransport
from harvey_brain.spec import get_provider


def run(coro):
    return asyncio.run(coro)


def body(text="{}", model="m"):
    return {
        "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                  "total_tokens": 12, "cost": 0.0001},
    }


def transport_for(provider, model, *, seen=None):
    spec = get_provider(provider)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if seen is not None:
            seen.append({
                "provider": spec.name,
                "model": payload["model"],
                "max_tokens": payload.get("max_tokens"),
                "temperature": payload.get("temperature"),
                "response_format": payload.get("response_format", {}).get("type")
                if isinstance(payload.get("response_format"), dict) else None,
            })
        return httpx.Response(200, json=body(model=payload["model"]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatTransport(spec, "k", model, http=client,
                                 max_retries=0, retry_base_delay=0.0)


CHEAP = ("openrouter", "anthropic/claude-haiku-5.5")
STRONG = ("openrouter", "anthropic/claude-sonnet-5.5")


def chain(seen):
    return [transport_for(*CHEAP, seen=seen),
            transport_for(*STRONG, seen=seen)]


# ── defaults ──
def test_default_profiles_cover_every_host_agent():
    for agent in ("scout", "writer", "handler", "analyst", "trainer"):
        assert agent in DEFAULT_PROFILES, agent


def test_scout_defaults_to_the_cheap_model_and_writer_to_the_strong_one():
    assert DEFAULT_PROFILES["scout"].model == "anthropic/claude-haiku-5.5"
    assert DEFAULT_PROFILES["writer"].model == "anthropic/claude-sonnet-5.5"
    assert DEFAULT_PROFILES["scout"].temperature == 0
    assert DEFAULT_PROFILES["scout"].max_output_tokens == 1024


def test_matching_prefers_the_agent_label_over_the_task():
    key, _ = select("writer", "score_contacts", DEFAULT_PROFILES)
    assert key == "writer"


def test_matching_falls_back_to_the_task_label():
    key, _ = select("", "score_contacts", DEFAULT_PROFILES)
    assert key == "scout"
    key, _ = select("", "classify_intent", DEFAULT_PROFILES)
    assert key == "handler"


def test_an_unknown_agent_and_task_select_nothing():
    assert select("mystery", "unknown_task", DEFAULT_PROFILES) is None


# ── routing through the real router ──
def test_scout_task_routes_to_the_cheap_model():
    seen: list[dict] = []
    router = RouterTransport(chain(seen), profiles=DEFAULT_PROFILES)

    assert run(router("score this", agent="scout", task="score_contacts",
                      expect_json=True)) == "{}"
    assert seen[0]["model"] == "anthropic/claude-haiku-5.5"
    assert seen[0]["max_tokens"] == 1024
    assert seen[0]["temperature"] == 0.0
    assert seen[0]["response_format"] == "json_schema"
    assert router.last_profile == "scout"
    assert router.last_provider == "openrouter"


def test_writer_task_routes_to_the_strong_model():
    seen: list[dict] = []
    router = RouterTransport(chain(seen), profiles=DEFAULT_PROFILES)

    run(router("write", agent="writer", task="personal_email", expect_json=True))
    assert seen[0]["model"] == "anthropic/claude-sonnet-5.5"
    assert seen[0]["max_tokens"] == 2048
    assert seen[0]["temperature"] == 0.7
    assert router.last_profile == "writer"


def test_unknown_task_falls_back_to_the_chain_order():
    seen: list[dict] = []
    router = RouterTransport(chain(seen), profiles=DEFAULT_PROFILES)

    run(router("anything", agent="mystery", task="unknown_task"))
    assert seen[0]["model"] == "anthropic/claude-haiku-5.5"
    assert router.last_profile == ""
    assert router.last_provider == "openrouter"


def test_no_profiles_means_pure_chain_order():
    seen: list[dict] = []
    router = RouterTransport(chain(seen), profiles={})
    run(router("score", agent="scout", task="score_contacts"))
    assert router.last_profile == ""


def test_a_profile_for_an_unconfigured_provider_is_skipped_not_fatal(caplog):
    """The chain has no xai member: the profile must fall through, loudly but safely."""
    seen: list[dict] = []
    profiles = dict(DEFAULT_PROFILES)
    profiles["scout"] = TaskProfile(
        provider="xai", model="grok-4.7", max_output_tokens=512)

    router = RouterTransport(chain(seen), profiles=profiles)
    with caplog.at_level("INFO"):
        assert run(router("score", agent="scout", task="score_contacts")) == "{}"

    assert "not in the chain" in caplog.text
    assert seen[0]["model"] == "anthropic/claude-haiku-5.5"   # chain fallback
    assert router.last_profile == ""                          # nothing was routed


def test_a_profile_can_pin_a_provider_already_in_the_chain():
    seen: list[dict] = []
    profiles = {"scout": TaskProfile(provider="openrouter",
                                     model="anthropic/claude-sonnet-5.5")}
    router = RouterTransport(chain(seen), profiles=profiles)
    run(router("score", agent="scout"))
    assert seen[0]["model"] == "anthropic/claude-sonnet-5.5"


def test_profile_overrides_do_not_leak_into_the_next_call():
    """Two roles share one transport; each must keep its own knobs."""
    seen: list[dict] = []
    shared = transport_for(*CHEAP, seen=seen)
    profiles = {
        "scout": TaskProfile(model="anthropic/claude-haiku-5.5",
                             max_output_tokens=256, temperature=0.0),
        "writer": TaskProfile(model="anthropic/claude-haiku-5.5",
                              max_output_tokens=4096, temperature=0.9),
    }
    router = RouterTransport([shared], profiles=profiles)

    run(router("a", agent="scout"))
    run(router("b", agent="writer"))

    assert seen[0]["max_tokens"] == 256 and seen[1]["max_tokens"] == 4096
    assert seen[0]["temperature"] == 0.0 and seen[1]["temperature"] == 0.9
    # The transport's own configured values are untouched afterwards.
    assert shared.max_output_tokens is None
    assert shared.temperature is None


def test_a_failing_profile_falls_through_to_the_chain():
    calls: list[str] = []

    def make(provider, model, status):
        spec = get_provider(provider)

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(model)
            if status != 200:
                return httpx.Response(status, text="nope")
            return httpx.Response(200, json=body(model=model))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return OpenAICompatTransport(spec, "k", model, http=client,
                                     max_retries=0, retry_base_delay=0.0)

    profiles = {"scout": TaskProfile(provider="openrouter",
                                     model="anthropic/claude-sonnet-5.5")}
    router = RouterTransport(
        [make("openrouter", "anthropic/claude-haiku-5.5", 200)],
        profiles=profiles,
    )
    # The profile names sonnet, which is not in the chain -> chain fallback.
    assert run(router("score", agent="scout")) == "{}"
    assert calls == ["anthropic/claude-haiku-5.5"]


def test_for_role_exposes_the_transport_probe_needs():
    # The local ``chain()`` helper returns [cheap, strong]; it is not a list arg.
    router = RouterTransport(chain([]), profiles=DEFAULT_PROFILES)

    assert router.for_role("scout").model == "anthropic/claude-haiku-5.5"
    assert router.for_role("writer").model == "anthropic/claude-sonnet-5.5"
    assert router.for_role("mystery") is None


# ── config ──
def test_profiles_are_merged_over_the_defaults(tmp_path):
    path = tmp_path / "brain.yaml"
    path.write_text("""
chain:
  - provider: ollama
profiles:
  scout:
    provider: openrouter
    model: anthropic/claude-haiku-5.5
    max_output_tokens: 512
""", encoding="utf-8")
    settings = load_settings(path)
    assert settings.profiles["scout"].max_output_tokens == 512
    # Untouched roles keep their defaults.
    assert settings.profiles["writer"].model == "anthropic/claude-sonnet-5.5"


def test_a_role_can_be_switched_off(tmp_path):
    path = tmp_path / "brain.yaml"
    path.write_text("""
chain:
  - provider: ollama
profiles:
  trainer: false
""", encoding="utf-8")
    settings = load_settings(path)
    assert "trainer" not in settings.profiles
    assert "scout" in settings.profiles


def test_profiles_can_be_disabled_entirely(tmp_path):
    path = tmp_path / "brain.yaml"
    path.write_text("chain:\n  - provider: ollama\nprofiles: false\n",
                    encoding="utf-8")
    settings = load_settings(path)
    assert settings.profiles == {}
    assert settings.use_default_profiles is False


def test_absent_profiles_block_means_no_routing(tmp_path):
    path = tmp_path / "brain.yaml"
    path.write_text("chain:\n  - provider: ollama\n", encoding="utf-8")
    assert load_settings(path).profiles == {}


def test_an_unknown_profile_key_is_rejected(tmp_path):
    path = tmp_path / "brain.yaml"
    path.write_text("""
chain:
  - provider: ollama
profiles:
  scout:
    modle: typo
""", encoding="utf-8")
    with pytest.raises(ValueError, match="modle"):
        load_settings(path)


def test_an_invalid_profile_json_mode_is_rejected(tmp_path):
    path = tmp_path / "brain.yaml"
    path.write_text("""
chain:
  - provider: ollama
profiles:
  scout:
    json_mode: yaml
""", encoding="utf-8")
    with pytest.raises(ValueError, match="json_mode"):
        load_settings(path)
