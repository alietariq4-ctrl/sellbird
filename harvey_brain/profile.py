"""Per-role model selection.

The host uses one brain for very different jobs, and it already labels every
call:

* ``scout`` — scoring/contacts: high volume, JSON out, cheap model wins.
* ``researcher`` — web research dossier synthesis: cheap + fast.
* ``enricher`` — custom enrichment columns: structured, deterministic.
* ``writer`` — first emails: short output, quality matters most.
* ``critic`` — self-critique / QA pass on drafts: fast, deterministic.
* ``handler`` — reply judgment: short output, needs taste not length.
* ``analyst`` / ``trainer`` — occasional, long output.

Matching is by **agent first, then task**, so a new task label under a known
agent still lands on the right profile. No match falls back to the configured
chain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskProfile:
    """The shape of one role's call: which model, how long, how loose."""

    provider: str = ""
    model: str = ""
    base_url: str = ""
    max_output_tokens: int | None = None
    temperature: float | None = None
    json_mode: str = "auto"
    reasoning_effort: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TaskProfile":
        if not isinstance(data, Mapping):
            raise ValueError("each profile must be a mapping")
        known = {
            "provider", "model", "base_url", "max_output_tokens",
            "temperature", "json_mode", "reasoning_effort",
        }
        extra = set(data) - known
        if extra:
            raise ValueError(
                f"unknown keys in a profile: {', '.join(sorted(extra))}"
            )
        json_mode = str(data.get("json_mode") or "auto").strip()
        if json_mode not in ("auto", "json_schema", "json_object", "none"):
            raise ValueError(f"invalid json_mode {json_mode!r} in a profile")
        max_out = data.get("max_output_tokens")
        temp = data.get("temperature")
        return cls(
            provider=str(data.get("provider") or "").strip(),
            model=str(data.get("model") or "").strip(),
            base_url=str(data.get("base_url") or "").strip(),
            max_output_tokens=int(max_out) if max_out is not None else None,
            temperature=float(temp) if temp is not None else None,
            json_mode=json_mode,
            reasoning_effort=(str(data["reasoning_effort"]).strip()
                              if data.get("reasoning_effort") else None),
        )


# Defaults chosen for cost, not prestige. Prices are doc facts in pricing.py:
#   anthropic/claude-haiku-5.5  $0.10 in / $0.50 out per 1M
#   openai/gpt-6-luna           $0.10 in / $0.50 out per 1M
#   anthropic/claude-sonnet-5.5 $2.00 in / $10.00 out per 1M
DEFAULT_PROFILES: dict[str, TaskProfile] = {
    # Cheap, high volume, JSON. 8k in / 1.2k out ≈ $0.0014 per call.
    "scout": TaskProfile(
        provider="openrouter", model="anthropic/claude-haiku-5.5",
        max_output_tokens=1024, temperature=0.0, json_mode="json_schema",
    ),
    # Deep research dossier synthesis: fast, factual, low temperature.
    "researcher": TaskProfile(
        provider="openrouter", model="anthropic/claude-haiku-5.5",
        max_output_tokens=2048, temperature=0.1, json_mode="auto",
    ),
    # Custom enrichment columns (Clay-style): deterministic JSON extraction.
    "enricher": TaskProfile(
        provider="openrouter", model="anthropic/claude-haiku-5.5",
        max_output_tokens=1024, temperature=0.0, json_mode="auto",
    ),
    # Quality matters and the output is short (~900 tokens): deliberate flagship.
    "writer": TaskProfile(
        provider="openrouter", model="anthropic/claude-sonnet-5.5",
        max_output_tokens=2048, temperature=0.7, json_mode="json_schema",
    ),
    # Draft QA / self-critique pass: fast, low temperature.
    "critic": TaskProfile(
        provider="openrouter", model="anthropic/claude-haiku-5.5",
        max_output_tokens=1024, temperature=0.1, json_mode="auto",
    ),
    # Judgment on a short reply: mid-tier is enough, JSON is a must.
    "handler": TaskProfile(
        provider="openrouter", model="anthropic/claude-haiku-5.5",
        max_output_tokens=1024, temperature=0.2, json_mode="json_schema",
    ),
    # Rare, long, read-only analysis over a company register.
    "analyst": TaskProfile(
        provider="openrouter", model="anthropic/claude-sonnet-5.5",
        max_output_tokens=4096, json_mode="json_object",
    ),
    # Onboarding: a handful of calls, so use the strong model and let it talk.
    "trainer": TaskProfile(
        provider="openrouter", model="anthropic/claude-sonnet-5.5",
        max_output_tokens=8192, temperature=0.3,
    ),
}

# Agent label -> profile key. The host's agent labels are stable; task labels
# drift as features are added.
AGENT_TO_PROFILE: dict[str, str] = {
    "scout": "scout",
    "researcher": "researcher",
    "enricher": "enricher",
    "writer": "writer",
    "critic": "critic",
    "handler": "handler",
    "analyst": "analyst",
    "trainer": "trainer",
    "humanizer": "writer",      # humanize.py:127 calls with the writer's agent
}

# Task label -> profile key, for calls whose agent is empty or generic.
TASK_TO_PROFILE: dict[str, str] = {
    "score_contacts": "scout",
    "research": "researcher",
    "research_company": "researcher",
    "summarize_research": "researcher",
    "synthesize_dossier": "researcher",
    "enrich": "enricher",
    "enrich_column": "enricher",
    "enrich_company": "enricher",
    "enrich_prospect": "enricher",
    "personal_email": "writer",
    "write_sequence": "writer",
    "humanize": "writer",
    "fix_draft": "writer",
    "suggest_offers": "writer",
    "critique": "critic",
    "critique_draft": "critic",
    "review_draft": "critic",
    "classify_intent": "handler",
    "generate_response": "handler",
    "fix_reply": "handler",
    "extract_product": "trainer",
    "extract_icp": "trainer",
    "extract_competitors": "trainer",
    "generate_objections": "trainer",
    "probe": "scout",           # the CLI's one-word health check
}


def select(agent: str, task: str,
           profiles: Mapping[str, TaskProfile]) -> tuple[str, TaskProfile] | None:
    """(profile_key, profile) for this call, or None to use the chain."""
    agent_key = (agent or "").strip().lower()
    task_key = (task or "").strip().lower()
    for key in (AGENT_TO_PROFILE.get(agent_key), task_key,
                TASK_TO_PROFILE.get(task_key)):
        if key and key in profiles:
            return key, profiles[key]
    return None


def resolve(profile: TaskProfile, chain: list[Any]) -> Any | None:
    """The chain member that serves this profile, or None when unavailable.

    1. First tries an exact ``(provider, model)`` match so when ``chain`` has
       multiple transports for the same provider with different models, the
       exact model's transport still wins.
    2. Second, matches any HTTP OpenAI-compatible transport (``spec is not None``
       and ``not spec.delegates``) for ``profile.provider`` (or any HTTP
       transport when ``profile.provider == ""``), because
       :class:`OpenAICompatTransport` overrides ``model=`` per call.
    """
    model = profile.model
    provider = profile.provider

    # Pass 1: exact (provider, model) match.
    for transport in chain:
        if getattr(transport, "paused_reason", ""):
            continue
        spec = getattr(transport, "spec", None)
        if spec is None:                     # delegate transports
            if provider and getattr(transport, "name", "") != provider:
                continue
            if model and getattr(transport, "model", "") not in ("", model):
                continue
            return transport
        if provider and spec.name != provider:
            continue
        if model and getattr(transport, "model", "") not in ("", model):
            continue
        return transport

    # Pass 2: provider-level match on HTTP OpenAI-compatible transports (which
    # accept a per-call ``model=`` override). Prefer unpaused members first.
    fallback = None
    for transport in chain:
        spec = getattr(transport, "spec", None)
        if spec is None or getattr(spec, "delegates", False):
            continue
        if provider and spec.name != provider:
            continue
        if not getattr(transport, "paused_reason", ""):
            return transport
        if fallback is None:
            fallback = transport
    if fallback is not None:
        return fallback

    logger.info(
        "profile -> %s/%s is not in the chain; using the configured order",
        provider or "*", model or "*",
    )
    return None


def overrides(profile: TaskProfile) -> dict[str, Any]:
    """The per-call knobs to hand a transport.

    These travel as **call arguments**, not as mutated attributes: the host can
    have a scout call and a writer call in flight on the same transport, and two
    roles must never see each other's model, temperature, or token budget.
    """
    knobs: dict[str, Any] = {"json_mode": profile.json_mode}
    if profile.model:
        knobs["model"] = profile.model
    if profile.base_url:
        knobs["base_url"] = profile.base_url
    if profile.max_output_tokens is not None:
        knobs["max_output_tokens"] = profile.max_output_tokens
    if profile.temperature is not None:
        knobs["temperature"] = profile.temperature
    if profile.reasoning_effort is not None:
        knobs["reasoning_effort"] = profile.reasoning_effort
    return knobs
