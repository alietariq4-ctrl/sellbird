"""Strict OpenAI-compatible JSON schemas and per-task structured-output policy.

OpenAI's strict structured outputs (``strict: True``) reject schemas that set
``additionalProperties: True`` or omit ``properties`` / ``required``, and
``response_format`` (both ``json_schema`` and ``json_object``) requires a
top-level JSON object rather than a bare JSON array ``[...]``.

Policy under ``json_mode="auto"``:
* Known fixed-shape object tasks (``personal_email``, ``fix_draft``,
  ``humanize``) use their strict schema when ``supports_json_schema`` is true,
  falling back to ``json_object`` or prompt-only.
* Array-capable tasks (``score_contacts``, ``write_sequence``,
  ``suggest_offers``) use prompt-only (``None``) so top-level ``[...]`` arrays
  are never rejected or coerced into objects.
* Other named tasks default to ``{"type": "json_object"}`` when supported so
  open-ended dictionaries never trigger strict-schema ``400`` errors.
* Unlabelled calls (``task=""``) keep the generic ``DEFAULT_SCHEMA`` probe so
  endpoint schema support can still be tested directly.
"""

from __future__ import annotations

from typing import Any, Mapping

from .base import DEFAULT_SCHEMA

EMAIL_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}

TASK_SCHEMAS: dict[str, dict[str, Any]] = {
    "personal_email": EMAIL_DRAFT_SCHEMA,
    "fix_draft": EMAIL_DRAFT_SCHEMA,
    "humanize": EMAIL_DRAFT_SCHEMA,
}

# Tasks where the host prompt asks for (or accepts) a top-level JSON array.
ARRAY_TASKS: frozenset[str] = frozenset({
    "score_contacts",
    "write_sequence",
    "suggest_offers",
})


def get_task_schema(task: str) -> dict[str, Any] | None:
    """Return a strict JSON schema for ``task``, or None if open-ended."""
    key = (task or "").strip().lower()
    schema = TASK_SCHEMAS.get(key)
    return dict(schema) if schema is not None else None


def is_array_task(task: str) -> bool:
    """True when ``task`` expects or permits a top-level JSON array."""
    return (task or "").strip().lower() in ARRAY_TASKS


def _wrap_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "response",
            "schema": dict(schema),
            "strict": True,
        },
    }


def resolve_response_format(
    mode: str,
    *,
    task: str = "",
    explicit_schema: Mapping[str, Any] | None = None,
    supports_json_schema: bool = False,
    supports_json_object: bool = False,
    default_schema: Mapping[str, Any] = DEFAULT_SCHEMA,
) -> dict[str, Any] | None:
    """Select the initial ``response_format`` for a JSON call."""
    if mode == "none":
        return None
    if mode == "json_object":
        return {"type": "json_object"}

    task_schema = dict(explicit_schema) if explicit_schema is not None else get_task_schema(task)

    if mode == "json_schema":
        return _wrap_schema(task_schema or default_schema)

    # mode == "auto"
    if explicit_schema is not None:
        if supports_json_schema:
            return _wrap_schema(explicit_schema)
        if supports_json_object:
            return {"type": "json_object"}
        return None

    clean_task = (task or "").strip().lower()
    if clean_task:
        if task_schema is not None:
            if supports_json_schema:
                return _wrap_schema(task_schema)
            if supports_json_object:
                return {"type": "json_object"}
            return None
        if clean_task in ARRAY_TASKS:
            return None
        if supports_json_object:
            return {"type": "json_object"}
        return None

    # Generic unlabelled call: preserve direct capability behaviour.
    if supports_json_schema:
        return _wrap_schema(default_schema)
    if supports_json_object:
        return {"type": "json_object"}
    return None
