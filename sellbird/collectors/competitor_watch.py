"""Competitive intelligence tracker (Phase 4, ROADMAP.md).

Watches pages the operator names — a competitor's pricing page, their
careers page, their homepage — and tells the operator when something
there changed: a price moved, a role opened, a tool appeared in their
stack. It never emails anyone: the findings go to the operator as a
copilot alert and an action log, because competitive intel is for the
operator's eyes, not for cold emails.

Each watch stores a baseline (the page's text, hashed and trimmed) in the
settings table and compares on every run. A first run only records the
baseline; changes alert from the second run on. The fetch goes through the
same SSRF-guarded stealth waterfall as profiling, so a private address in
the config is refused before any network tier runs.

Config (sellbird.yaml):

    competitor_watch:
      enabled: true
      watches:
        - name: "Rival pricing"
          url: "https://rival.example/pricing"
          kind: pricing        # pricing | hiring | homepage (labels the alert)
"""

from __future__ import annotations

import hashlib
import json
import logging

from sellbird.integrations.stealth_fetch import fetch_page

logger = logging.getLogger("sellbird.competitor_watch")

# One setting row per watch, keyed by this prefix + the watch name.
BASELINE_KEY = "competitor_watch.baseline."

# How much of a page's text is compared: enough to catch a real edit,
# small enough that a rotating banner or timestamp doesn't fire an alert.
COMPARE_CHARS = 20_000

KIND_LABELS = {
    "pricing": "pricing change",
    "hiring": "hiring change",
    "homepage": "homepage change",
}


def _normalise(text: str) -> str:
    """Collapse whitespace so a re-flowed page isn't a false change."""
    return " ".join((text or "").split())


def _fingerprint(markdown: str) -> tuple[str, str]:
    """(hash, trimmed text) for a page's markdown."""
    trimmed = _normalise(markdown)[:COMPARE_CHARS]
    return hashlib.sha256(trimmed.encode("utf-8", "replace")).hexdigest(), trimmed


def _watch_key(name: str) -> str:
    return BASELINE_KEY + (name or "").strip().lower()


async def run_competitor_watch(state, config=None, env=None) -> int:
    """One pass over every configured watch. Returns alerts sent; never raises.

    Runs on idle cycles only, so it never competes with the pipeline for the
    model budget — and it spends no model calls at all: the diff is plain
    text comparison, and the alert is the diff itself.
    """
    cw = getattr(config, "competitor_watch", None)
    if cw is None or not getattr(cw, "enabled", False):
        return 0
    watches = getattr(cw, "watches", None) or []
    if not watches:
        return 0

    alerts = 0
    for watch in watches:
        name = str(getattr(watch, "name", "") or "").strip()
        url = str(getattr(watch, "url", "") or "").strip()
        kind = str(getattr(watch, "kind", "") or "").strip().lower()
        if not name or not url:
            continue
        try:
            page = await fetch_page(url, config=config, env=env)
        except Exception as exc:
            logger.debug("competitor watch %s: fetch failed: %s", name, exc)
            continue
        if not page.ok or not (page.markdown or "").strip():
            logger.debug("competitor watch %s: nothing readable at %s", name, url)
            continue

        digest, trimmed = _fingerprint(page.markdown)
        key = _watch_key(name)
        stored = await state.get_setting(key)
        if not stored:
            # First run: record the baseline, alert nothing.
            await state.set_setting(key, json.dumps(
                {"url": url, "hash": digest, "text": trimmed}))
            logger.info("Competitor watch: baseline recorded for '%s'.", name)
            continue
        try:
            baseline = json.loads(stored)
        except (json.JSONDecodeError, TypeError):
            baseline = {}
        if baseline.get("hash") == digest:
            continue  # unchanged

        # Something changed: alert, then move the baseline forward.
        label = KIND_LABELS.get(kind, "change")
        await state.log_action(
            action_type="competitor_change",
            agent="competitor_watch",
            details={"watch": name, "url": url, "kind": kind or "page"},
        )
        try:
            from sellbird.integrations.copilot import notify_operator
            await notify_operator(
                "competitor_change",
                f"{name}: {label} detected",
                f"{url}",
                details={"watch": name, "url": url, "kind": kind or "page"},
                config=config,
                env=env,
            )
            alerts += 1
        except Exception as exc:
            logger.debug("competitor watch %s: alert failed: %s", name, exc)
        await state.set_setting(key, json.dumps(
            {"url": url, "hash": digest, "text": trimmed}))
        logger.info("Competitor watch: '%s' changed (%s).", name, label)
    return alerts
