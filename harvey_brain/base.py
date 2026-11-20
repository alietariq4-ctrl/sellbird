"""Shared types and helpers for every transport.

The host contract, proven against the real ``sellbird.brain.Brain``::

    async transport(prompt, *, agent, task, timeout, expect_json) -> str
    transport.paused_reason -> str   # non-empty makes the host skip calls

Transports never raise: a failure returns ``""`` and is logged as one line.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300.0
DEFAULT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Bodies that mean "the account is out of money", not "slow down".
QUOTA_MARKERS = (
    "insufficient_credits",
    "insufficient credits",
    "out of credits",
    "credit balance",
    "quota exceeded",
    "exceeded your current quota",
)


@dataclass(slots=True)
class Usage:
    """One call's billed usage, in the shape a host ledger wants."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    provider: str = ""
    duration_ms: int = 0
    agent: str = ""
    task: str = ""


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def nested(payload: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(payload, Mapping):
            return None
        payload = payload.get(key)
    return payload


# Key shapes worth scrubbing from a provider's error text. Providers do echo
# the offending credential back ("Incorrect API key provided: sk-..."), so an
# error body is not automatically safe to log.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-or-v1-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"xai-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"gsk_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
)


def redact(text: str, secret: str = "") -> str:
    """Mask credentials in text we are about to log."""
    if not text:
        return ""
    out = text
    if secret and len(secret) >= 6:
        out = out.replace(secret, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[redacted]", out)
    return out


def excerpt(response: Any, limit: int = 200, secret: str = "") -> str:
    """A short, single-line body excerpt with credentials masked."""
    try:
        text = " ".join(str(response.text).split())
    except Exception:  # pragma: no cover - body already consumed or broken
        return ""
    return redact(text, secret)[:limit]


def is_quota_body(body: str) -> bool:
    lowered = (body or "").lower()
    return any(marker in lowered for marker in QUOTA_MARKERS)


def extract_text(content: Any) -> str:
    """Message content as text.

    Providers differ: most send a string, some send ``[{"type":"text",...}]``
    parts. Reasoning fields are deliberately not accepted here — they are
    billed output, not the answer.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


class Transport:
    """Interface every member of a router chain implements."""

    name: str = ""
    paused_reason: str = ""
    last_error: str = ""

    async def __call__(self, prompt: str, *, agent: str = "", task: str = "",
                       timeout: float = DEFAULT_TIMEOUT,
                       expect_json: bool = False) -> str:  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover - optional
        return None
