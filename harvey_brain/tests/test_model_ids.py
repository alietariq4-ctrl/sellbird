"""Do the hard-coded default model ids actually exist?

A wrong default fails on the FIRST real call with 404 model_not_found, which is
the worst possible time to learn about it. The OpenRouter half of this runs
against a committed fixture (so it is deterministic and offline); the opt-in
``network`` half re-fetches the live catalogue and fails loudly when a model is
retired.

Run the whole thing, including the live check, with::

    python -m pytest harvey_brain/tests/test_model_ids.py -q -m network -p no:cacheprovider
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harvey_brain.spec import PROVIDERS

FIXTURE = Path(__file__).parent / "fixtures" / "openrouter_models.json"

# Ids verified against each provider's own documentation. A provider absent
# from this map has no independently checkable id (local model tags, delegated
# runtimes).
DOC_VERIFIED = {
    "openrouter": "GET /api/v1/models (tests/fixtures/openrouter_models.json)",
    "xai": "https://docs.x.ai/developers/models/grok-4.7",
    "openai": "https://developers.openai.com/api/docs/models",
    "gemini": "https://ai.google.dev/gemini-api/docs/openai",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing",
    "groq": "https://console.groq.com/docs/models",
    "mistral": "https://docs.mistral.ai/models/mistral-large-4-0",
    "together": "https://docs.together.ai/docs/serverless-models",
    "fireworks": "https://docs.fireworks.ai/getting-started/quickstart",
    "cerebras": "https://inference-docs.cerebras.ai/models/llama-33-70b",
}

UNVERIFIED = {"ollama", "lmstudio", "vllm", "custom"}


def catalogue() -> dict[str, dict]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {model["id"]: model for model in payload["data"]}


def test_fixture_is_present_and_labelled():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["source"] == "https://openrouter.ai/api/v1/models"
    assert payload["fetched_at"]
    assert len(payload["data"]) > 100


def test_openrouter_default_model_exists_in_the_catalogue():
    models = catalogue()
    default = PROVIDERS["openrouter"].default_model
    assert default in models, (
        f"openrouter default_model {default!r} is not in the catalogue; "
        f"nearest: {sorted(i for i in models if i.startswith(default.split('/')[0]))[:5]}"
    )


def test_every_openrouter_model_we_reference_exists_in_the_catalogue():
    """Defaults AND the price-table prefixes used for routing must be real ids."""
    models = catalogue()
    referenced = [
        PROVIDERS["openrouter"].default_model,
        # The ids the README and the profile defaults use.
        "anthropic/claude-haiku-5.5",
        "openai/gpt-6-luna",
        "google/gemini-4-hydrogen",
        "x-ai/grok-4.7",
    ]
    missing = [model for model in referenced if model not in models]
    assert not missing, f"ids missing from the live catalogue: {missing}"


def test_openrouter_default_is_a_claude_sonnet():
    """The user asked for Claude to stay available as an option."""
    default = PROVIDERS["openrouter"].default_model
    assert default.startswith("anthropic/claude-sonnet"), default


@pytest.mark.parametrize("provider", sorted(DOC_VERIFIED))
def test_direct_provider_defaults_are_doc_verified(provider):
    spec = PROVIDERS[provider]
    assert spec.default_model, f"{provider} has no default_model"
    assert spec.default_model == spec.default_model.strip()
    assert " " not in spec.default_model
    assert provider in DOC_VERIFIED


def test_only_ollama_is_allowed_to_be_unverified():
    unverified = {
        name for name, spec in PROVIDERS.items()
        if spec.default_model and name not in DOC_VERIFIED
    }
    assert unverified == UNVERIFIED, (
        f"these providers ship a default_model with no doc citation: "
        f"{sorted(unverified - UNVERIFIED)}"
    )


def test_delegates_have_no_model_id_of_their_own():
    for name in ("claude_cli", "codex_cli"):
        assert PROVIDERS[name].default_model == ""


def _version(model_id: str) -> tuple[int, ...]:
    """'anthropic/claude-sonnet-5.5' -> (5, 5); unparseable -> (0,)."""
    tail = model_id.rsplit("-", 1)[-1]
    try:
        return tuple(int(part) for part in tail.split("."))
    except ValueError:
        return (0,)


def test_anthropic_candidates_are_current():
    """Guards against silently picking a retired Claude id."""
    models = catalogue()
    sonnets = [i for i in models if i.startswith("anthropic/claude-sonnet")]
    assert "anthropic/claude-sonnet-5.5" in sonnets
    newest = max(sonnets, key=_version)
    assert _version(newest) <= (5, 5), (
        f"a newer Sonnet exists ({newest}); consider promoting it"
    )
    assert _version(PROVIDERS["openrouter"].default_model) == (5, 5)


# ── live check (opt-in; the default suite must stay offline) ──────────────
@pytest.mark.network
def test_live_openrouter_catalogue_still_lists_our_ids():
    """Re-fetch the real catalogue and fail if any id we ship has been retired."""
    import httpx

    response = httpx.get("https://openrouter.ai/api/v1/models", timeout=60)
    assert response.status_code == 200
    live = {model["id"] for model in response.json()["data"]}

    ours = {
        PROVIDERS["openrouter"].default_model,
        "anthropic/claude-haiku-5.5",
        "openai/gpt-6-luna",
        "google/gemini-4-hydrogen",
        "x-ai/grok-4.7",
    }
    missing = sorted(ours - live)
    assert not missing, (
        f"retired or renamed since the fixture was taken: {missing}. "
        f"Update spec.py defaults and refresh tests/fixtures/openrouter_models.json."
    )
