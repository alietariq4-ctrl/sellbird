"""Competitive intelligence tracker (sellbird/collectors/competitor_watch.py).

Watches pages the operator names and alerts when something there changes —
a competitor's price, a role, their stack. Findings go to the operator as a
copilot alert and an action log, never into cold emails. The first run only
records a baseline; changes alert from the second run on. All fixtures are
fictional; fetch_page is stubbed, nothing leaves the process.
"""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.collectors.competitor_watch import run_competitor_watch
from sellbird.config import SellbirdConfig
from sellbird.state import StateManager


def _config(watches, enabled=True):
    return SellbirdConfig(
        persona={"name": "Sellbird", "company": "Acme", "role": "BD",
                 "email": "s@acme.example", "linkedin": "", "tone": "direct"},
        product={"name": "Widget", "description": "Widgets", "pricing": "$10",
                 "key_benefits": ["fast"], "objection_responses": {}},
        icp={"industries": ["software"], "company_size": "10-50",
             "titles": ["VP"], "geography": ["US"]},
        competitor_watch={"enabled": enabled, "watches": watches},
    )


def _watch(name="Rival pricing", url="https://rival.example/pricing", kind="pricing"):
    return {"name": name, "url": url, "kind": kind}


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "cw.db"))
    await s.init_db()
    return s


def _pages(state, monkeypatch, pages_by_url):
    """Stub fetch_page: each URL returns its scripted markdown, in order."""
    import sellbird.collectors.competitor_watch as cwmod

    calls = []

    async def fake_fetch(url, *, config=None, env=None, **kw):
        calls.append(url)
        md = pages_by_url[url].pop(0) if len(pages_by_url[url]) > 1 else pages_by_url[url][0]
        return SimpleNamespace(ok=True, markdown=md, html="")

    monkeypatch.setattr(cwmod, "fetch_page", fake_fetch)
    return calls


# ── the off switch ──


@pytest.mark.asyncio
async def test_disabled_watches_nothing(state, monkeypatch):
    calls = _pages(state, monkeypatch, {})
    assert await run_competitor_watch(state, _config([_watch()], enabled=False)) == 0
    assert calls == []


@pytest.mark.asyncio
async def test_no_watches_configured_is_a_noop(state):
    assert await run_competitor_watch(state, _config([])) == 0


# ── baseline then change ──


@pytest.mark.asyncio
async def test_the_first_run_only_records_a_baseline(state, monkeypatch):
    alerts: list[tuple] = []

    async def fake_notify(event, title, message="", *, details=None, config=None, env=None):
        alerts.append((event, title))

    import sellbird.integrations.copilot as cop
    monkeypatch.setattr(cop, "notify_operator", fake_notify)

    _pages(state, monkeypatch, {"https://rival.example/pricing": ["# Pricing\n\n$29/mo"]})
    assert await run_competitor_watch(state, _config([_watch()])) == 0
    assert alerts == []
    stored = await state.get_setting("competitor_watch.baseline.rival pricing")
    assert stored and json.loads(stored)["hash"]


@pytest.mark.asyncio
async def test_a_change_alerts_and_moves_the_baseline(state, monkeypatch):
    alerts: list[tuple] = []

    async def fake_notify(event, title, message="", *, details=None, config=None, env=None):
        alerts.append((event, title))

    import sellbird.integrations.copilot as cop
    monkeypatch.setattr(cop, "notify_operator", fake_notify)

    pages = {"https://rival.example/pricing": ["# Pricing\n\n$29/mo",
                                               "# Pricing\n\n$49/mo"]}
    _pages(state, monkeypatch, pages)
    cfg = _config([_watch()])
    await run_competitor_watch(state, cfg)          # baseline
    assert await run_competitor_watch(state, cfg) == 1   # change → 1 alert
    assert alerts == [("competitor_change", "Rival pricing: pricing change detected")]
    async with state._connect() as db:
        async with db.execute(
            "SELECT details_json FROM actions WHERE action_type = 'competitor_change'"
        ) as cursor:
            rows = await cursor.fetchall()
    [logged] = [json.loads(r[0]) for r in rows]
    assert logged["watch"] == "Rival pricing"
    # Third run: same page as the new baseline → quiet.
    assert await run_competitor_watch(state, cfg) == 0


@pytest.mark.asyncio
async def test_an_unchanged_page_never_alerts(state, monkeypatch):
    alerts: list[tuple] = []

    async def fake_notify(event, title, message="", *, details=None, config=None, env=None):
        alerts.append((event, title))

    import sellbird.integrations.copilot as cop
    monkeypatch.setattr(cop, "notify_operator", fake_notify)

    _pages(state, monkeypatch, {"https://rival.example/pricing": ["# Pricing\n\n$29/mo"]})
    cfg = _config([_watch()])
    await run_competitor_watch(state, cfg)
    await run_competitor_watch(state, cfg)
    await run_competitor_watch(state, cfg)
    assert alerts == []


@pytest.mark.asyncio
async def test_whitespace_reflows_are_not_changes(state, monkeypatch):
    import sellbird.collectors.competitor_watch as cwmod

    pages = {"https://rival.example/pricing": [
        "# Pricing\n\n$29/mo",
        "#   Pricing\n\n\n$29/mo   ",
    ]}
    _pages(state, monkeypatch, pages)
    cfg = _config([_watch()])
    await run_competitor_watch(state, cfg)
    assert await run_competitor_watch(state, cfg) == 0   # same text, re-flowed


# ── failure handling ──


@pytest.mark.asyncio
async def test_a_page_that_wont_fetch_is_skipped(state, monkeypatch):
    import sellbird.collectors.competitor_watch as cwmod

    async def fake_fetch(url, *, config=None, env=None, **kw):
        raise ValueError("private address refused")

    monkeypatch.setattr(cwmod, "fetch_page", fake_fetch)
    assert await run_competitor_watch(state, _config([_watch()])) == 0


@pytest.mark.asyncio
async def test_a_watch_without_a_name_or_url_is_ignored(state):
    bad = [{"name": "", "url": "https://rival.example/pricing", "kind": "pricing"},
           {"name": "No url", "url": "  ", "kind": "homepage"}]
    assert await run_competitor_watch(state, _config(bad)) == 0
