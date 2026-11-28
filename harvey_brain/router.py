"""Deterministic provider chain with a dollar budget gate.

Order is exactly the configured order — never shuffled, never chosen by a
model. A provider that is paused (bad key, out of credits) is skipped without
being called, so a dead primary costs nothing per attempt.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import inspect
import logging
from time import monotonic
from typing import Any, Callable, Sequence

from . import profile as profiles_mod
from .base import DEFAULT_TIMEOUT, Usage

logger = logging.getLogger(__name__)

# A chain that failed on every member pauses for this long, so the host does
# not hammer a dead provider set on every heartbeat. It clears by itself; an
# auth/credit pause on a member does not (that needs a human).
FAILURE_HOLD_SECONDS = 60.0


def _filter_knobs(transport: Any, knobs: dict[str, Any]) -> dict[str, Any]:
    """Pass only keyword overrides that ``transport.__call__`` accepts."""
    if not knobs:
        return {}
    try:
        sig = inspect.signature(transport)
    except (TypeError, ValueError):
        return knobs
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return knobs
    accepted = set(sig.parameters)
    return {k: v for k, v in knobs.items() if k in accepted}


class RouterTransport:
    """Try members in order. Success stops the walk."""

    def __init__(
        self,
        chain: Sequence[Any],
        *,
        budget_usd_per_day: float | None = None,
        ledger: Callable[..., Any] | None = None,
        on_usage: Callable[[Usage], Any] | None = None,
        failure_hold_seconds: float = FAILURE_HOLD_SECONDS,
        profiles: dict[str, profiles_mod.TaskProfile] | None = None,
        transient_cooldown_seconds: float = 0.0,
    ):
        if not chain:
            raise ValueError("RouterTransport needs at least one transport")
        self.chain = list(chain)
        self.name = "router"
        # Empty means "always walk the chain": a caller that has not thought
        # about roles must not silently get different models than configured.
        self.profiles: dict[str, profiles_mod.TaskProfile] = dict(profiles or {})
        self.last_profile = ""
        self.budget_usd_per_day = (
            float(budget_usd_per_day) if budget_usd_per_day is not None else None
        )
        self.ledger = ledger
        self.on_usage = on_usage
        self.failure_hold_seconds = max(0.0, float(failure_hold_seconds))
        self.transient_cooldown_seconds = max(0.0, float(transient_cooldown_seconds))
        self._transport_cooldowns: dict[int, float] = {}
        # In-memory day spend. Resets automatically when local date rolls over.
        self.spend_usd = 0.0
        self._spend_date: str = date.today().isoformat()
        self.last_provider = ""
        self.last_usage: Usage | None = None
        self.last_error = ""
        self._last_failure = ""
        self._failure_until = 0.0

    def _sync_spend_day(self) -> None:
        today = date.today().isoformat()
        if today != self._spend_date:
            self.spend_usd = 0.0
            self._spend_date = today

    async def _fire_usage(self, usage: Usage, transport: Any = None) -> None:
        if self.on_usage is None:
            return
        if transport is not None and getattr(transport, "on_usage", None) is self.on_usage:
            return
        try:
            try:
                result = self.on_usage(usage)
            except TypeError:
                result = self.on_usage(**asdict(usage))
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("on_usage callback failed: %s", type(exc).__name__)

    # ── pause state ───────────────────────────────────────────────────────

    @property
    def paused_reason(self) -> str:
        """Non-empty only when nothing can be attempted.

        Deliberately a property: the host reads it before every call, so a
        stale value would strand the whole pipeline.
        """
        if self._budget_paused():
            return self._budget_message()
        paused = [t for t in self.chain if getattr(t, "paused_reason", "")]
        if len(paused) == len(self.chain):
            names = ", ".join(getattr(t, "name", "?") for t in paused)
            first = str(getattr(paused[0], "paused_reason", ""))[:160]
            return f"every provider is paused ({names}): {first}"
        if self._last_failure and monotonic() < self._failure_until:
            return self._last_failure
        return ""

    def _ledger_spend(self) -> float:
        self._sync_spend_day()
        if self.ledger is None:
            return 0.0
        try:
            arity = len(inspect.signature(self.ledger).parameters)
        except (TypeError, ValueError):
            arity = 1
        try:
            if arity >= 2:
                value = self.ledger(self.spend_usd, self.last_provider or "")
            elif arity == 1:
                value = self.ledger(self.last_provider or "")
            else:
                value = self.ledger()
            if inspect.isawaitable(value):  # a sync read here cannot await it
                try:
                    value.close()  # type: ignore[union-attr]
                except Exception:
                    pass
                return self.spend_usd
            return float(value or 0.0)
        except Exception as exc:
            logger.warning("spend ledger read failed: %s", type(exc).__name__)
            return self.spend_usd

    def spend_today_usd(self) -> float:
        self._sync_spend_day()
        return max(self.spend_usd, self._ledger_spend())

    def _budget_paused(self) -> bool:
        if self.budget_usd_per_day is None:
            return False
        return self.spend_today_usd() >= self.budget_usd_per_day

    def _budget_message(self) -> str:
        return (
            f"daily AI budget reached: ${self.spend_today_usd():.4f} of "
            f"${self.budget_usd_per_day:.2f} spent today. It resets at midnight; "
            f"raise budget.max_usd_per_day to continue."
        )

    # ── per-role routing ──────────────────────────────────────────────────

    def _route(self, agent: str, task: str):
        """(profile_key, transport_view, knobs) for this call, or None.

        Returns None whenever no profile matches, the profile's provider is not
        in the chain, or the router has no profiles. Every one of those is a
        normal condition, so it is a log line at most, never an exception.
        """
        if not self.profiles:
            return None
        picked = profiles_mod.select(agent, task, self.profiles)
        if picked is None:
            return None
        key, profile = picked
        transport = profiles_mod.resolve(profile, self.chain)
        if transport is None or getattr(transport, "paused_reason", ""):
            return None
        return key, transport, profiles_mod.overrides(profile)

    def for_role(self, role: str) -> Any | None:
        """``self.for_role("scout")`` -> the transport that serves that role.

        Exposed for `probe`, which reports exactly what a real call would use.
        """
        picked = profiles_mod.select(role, role, self.profiles)
        if picked is None:
            return None
        return profiles_mod.resolve(picked[1], self.chain)

    # ── the seam ──────────────────────────────────────────────────────────

    async def __call__(
        self,
        prompt: str,
        *,
        agent: str = "",
        task: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        expect_json: bool = False,
    ) -> str:
        self._sync_spend_day()
        if self._budget_paused():
            logger.warning("%s", self._budget_message())
            return ""

        self._last_failure = ""
        self._failure_until = 0.0
        self.last_profile = ""
        failures: list[tuple[str, str]] = []
        deadline = monotonic() + float(timeout if timeout else DEFAULT_TIMEOUT)
        already_tried: tuple[int, str, str] | None = None

        routed = self._route(agent, task)
        if routed is not None:
            key, transport, knobs = routed
            if not getattr(transport, "paused_reason", ""):
                safe_knobs = _filter_knobs(transport, knobs)
                tried_model = str(safe_knobs.get("model") or getattr(transport, "model", ""))
                tried_url = str(safe_knobs.get("base_url") or "")
                text = await transport(
                    prompt, agent=agent, task=task, timeout=deadline - monotonic(),
                    expect_json=expect_json, **safe_knobs,
                )
                if text:
                    self.last_profile = key
                    self.last_provider = str(getattr(transport, "name", ""))
                    self.last_usage = getattr(transport, "last_usage", None)
                    if isinstance(self.last_usage, Usage):
                        self._sync_spend_day()
                        self.spend_usd = round(self.spend_usd + self.last_usage.cost_usd, 6)
                        await self._fire_usage(self.last_usage, transport)
                    self.last_error = ""
                    return text
                already_tried = (id(transport), tried_model, tried_url)
                # The role's model failed: fall through to the chain rather than
                # failing the call, because the chain is the configured safety net.
                failures.append((
                    f"profile:{key}",
                    str(getattr(transport, "last_error", "") or "empty reply")[:120],
                ))
                logger.warning("task profile %s failed; walking the chain", key)

        now_ts = monotonic()
        has_cool_fallback = any(
            not getattr(t, "paused_reason", "")
            and self._transport_cooldowns.get(id(t), 0.0) <= now_ts
            for t in self.chain
        )

        for transport in self.chain:
            default_model = str(getattr(transport, "model", ""))
            if already_tried == (id(transport), default_model, ""):
                # Exact same transport + default model already failed in routed pass.
                continue

            name = str(getattr(transport, "name", type(transport).__name__))
            reason = str(getattr(transport, "paused_reason", "") or "")
            if reason:
                # Skip without calling: a paused provider cannot help and a
                # call would only repeat the failure that paused it.
                logger.info("%s skipped: %s", name, reason)
                failures.append((name, f"paused: {reason[:80]}"))
                continue

            if (
                self.transient_cooldown_seconds > 0
                and has_cool_fallback
                and self._transport_cooldowns.get(id(transport), 0.0) > now_ts
            ):
                logger.debug("%s skipped (transient cooldown active)", name)
                failures.append((name, "transient cooldown"))
                continue

            remaining = deadline - monotonic()
            if remaining <= 0:
                failures.append((name, "no time left in the budget"))
                break

            text = await transport(
                prompt, agent=agent, task=task, timeout=remaining,
                expect_json=expect_json,
            )
            if text:
                self._transport_cooldowns.pop(id(transport), None)
                self.last_provider = name
                self.last_usage = getattr(transport, "last_usage", None)
                usage = self.last_usage
                if isinstance(usage, Usage):
                    self._sync_spend_day()
                    self.spend_usd = round(self.spend_usd + usage.cost_usd, 6)
                    await self._fire_usage(usage, transport)
                self.last_error = ""
                return text

            if self.transient_cooldown_seconds > 0 and not getattr(transport, "paused_reason", ""):
                self._transport_cooldowns[id(transport)] = monotonic() + self.transient_cooldown_seconds
            detail = str(getattr(transport, "last_error", "") or "empty reply")
            failures.append((name, detail[:120]))
            if getattr(transport, "paused_reason", ""):
                logger.warning("%s paused after this call: %s",
                               name, transport.paused_reason)

        last = failures[-1] if failures else ("none", "no provider attempted")
        self._last_failure = (
            f"all {len(failures) or len(self.chain)} providers failed "
            f"(last: {last[0]} {last[1]})"
        )
        self._failure_until = monotonic() + self.failure_hold_seconds
        self.last_error = self._last_failure
        logger.warning("%s", self._last_failure)
        return ""

    async def aclose(self) -> None:
        for transport in self.chain:
            close = getattr(transport, "aclose", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - teardown only
                logger.debug("aclose failed for %s: %s",
                             getattr(transport, "name", "?"), type(exc).__name__)


async def gather_usage(*callbacks: Callable[[Usage], Any]) -> Callable[[Usage], Any]:
    """Fan one Usage out to several ledgers (host DB, stderr, metrics)."""
    async def _run(usage: Usage) -> None:
        for callback in callbacks:
            try:
                result = callback(usage)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning("usage callback failed: %s", type(exc).__name__)
    return _run
