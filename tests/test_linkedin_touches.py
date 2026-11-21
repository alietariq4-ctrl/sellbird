"""LinkedIn touchpoints between email steps (v19).

When the first email of a sequence goes out and the prospect has a profile,
Sellbird queues one light LinkedIn touch (a profile view, or a connection
request when configured) so the follow-up lands from a familiar name. One
touch per prospect, ever; the automation's own daily caps decide how many
run per day. All fixtures are fictional.
"""

import pytest

from sellbird.config import SellbirdConfig
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager


async def _state(tmp_path):
    state = StateManager(str(tmp_path / "sellbird.db"))
    await state.init_db()
    return state


async def _prospect_with_url(state, url="https://www.linkedin.com/in/dana-reed-example"):
    company = Company(name="Harbor Studio", domain="harbor.example")
    company.id = await state.add_company(company)
    p = Prospect(first_name="Dana", last_name="Reed", email="dana@harbor.example",
                 company="Harbor Studio", company_id=company.id,
                 title="Owner", status="contacted", linkedin_url=url)
    p.id = await state.add_prospect(p)
    return p


# ── state layer ──


@pytest.mark.asyncio
async def test_a_touch_is_scheduled_once_per_prospect(tmp_path):
    state = await _state(tmp_path)
    p = await _prospect_with_url(state)
    assert await state.schedule_linkedin_touch(p.id, p.linkedin_url) is True
    # Second schedule for the same prospect: the unique index says no.
    assert await state.schedule_linkedin_touch(p.id, p.linkedin_url) is False
    due = await state.due_linkedin_touches()
    assert len(due) == 1 and due[0]["prospect_id"] == p.id


@pytest.mark.asyncio
async def test_a_touch_without_a_url_is_never_scheduled(tmp_path):
    state = await _state(tmp_path)
    p = await _prospect_with_url(state)
    assert await state.schedule_linkedin_touch(p.id, "  ") is False
    assert await state.due_linkedin_touches() == []


@pytest.mark.asyncio
async def test_a_future_touch_is_not_due_yet(tmp_path):
    state = await _state(tmp_path)
    p = await _prospect_with_url(state)
    assert await state.schedule_linkedin_touch(
        p.id, p.linkedin_url, due_at="2099-01-01T00:00:00") is True
    assert await state.due_linkedin_touches() == []


@pytest.mark.asyncio
async def test_finishing_a_touch_records_the_outcome(tmp_path):
    state = await _state(tmp_path)
    p = await _prospect_with_url(state)
    await state.schedule_linkedin_touch(p.id, p.linkedin_url)
    touch = (await state.due_linkedin_touches())[0]

    await state.finish_linkedin_touch(touch["id"])
    assert await state.due_linkedin_touches() == []

    # A second prospect's touch can fail and is not retried either.
    p2 = Prospect(first_name="Eli", last_name="Stone", email="eli@harbor.example",
                  company="Harbor Studio", company_id=p.company_id,
                  title="PM", status="contacted",
                  linkedin_url="https://www.linkedin.com/in/eli-stone-example")
    p2.id = await state.add_prospect(p2)
    await state.schedule_linkedin_touch(p2.id, p2.linkedin_url, action="connect")
    touch = (await state.due_linkedin_touches())[0]
    await state.finish_linkedin_touch(touch["id"], error="login failed")
    assert await state.due_linkedin_touches() == []


@pytest.mark.asyncio
async def test_the_schema_migrates_on_an_existing_database(tmp_path):
    # A database at v18 (before linkedin_touches) must gain the table on
    # upgrade. Built from the first 18 migrations, the way a real v18 file
    # is, so later ALTER migrations aren't replayed onto columns that
    # already exist.
    import aiosqlite

    from sellbird.state import MIGRATIONS

    db_path = str(tmp_path / "sellbird.db")
    async with aiosqlite.connect(db_path) as db:
        for script in MIGRATIONS[:18]:
            await db.executescript(script)
        await db.execute("PRAGMA user_version = 18")
        await db.commit()

    state2 = StateManager(db_path)
    await state2.init_db()
    p = await _prospect_with_url(state2)
    assert await state2.schedule_linkedin_touch(p.id, p.linkedin_url) is True


# ── the drain gate ──


def _cfg(linkedin_enabled=False, touchpoints=False):
    return SellbirdConfig(
        persona={"name": "Sellbird", "company": "Acme", "role": "BD",
                 "email": "s@acme.example", "linkedin": "", "tone": "direct"},
        product={"name": "Widget", "description": "Widgets", "pricing": "$10",
                 "key_benefits": ["fast"], "objection_responses": {}},
        icp={"industries": ["software"], "company_size": "10-50",
             "titles": ["VP"], "geography": ["US"]},
        channels={"email": {"provider": "smtp", "require_approval": True},
                  "linkedin": {"enabled": linkedin_enabled,
                               "touchpoints": touchpoints}},
    )


@pytest.mark.asyncio
async def test_the_drain_does_nothing_when_the_channel_is_off(tmp_path, monkeypatch):
    from sellbird.main import run_linkedin_touches
    from sellbird.config import EnvConfig

    state = await _state(tmp_path)
    p = await _prospect_with_url(state)
    await state.schedule_linkedin_touch(p.id, p.linkedin_url)

    called = []
    monkeypatch.setattr(
        "sellbird.integrations.linkedin.LinkedInAutomation",
        lambda *a: called.append(a) or pytest.fail("must not start the browser"))
    done = await run_linkedin_touches(state, _cfg(), EnvConfig())
    assert done == 0 and not called
    # The touch stays queued for when the channel is switched on.
    assert await state.due_linkedin_touches()


@pytest.mark.asyncio
async def test_the_drain_does_nothing_without_linkedin_credentials(tmp_path):
    from sellbird.main import run_linkedin_touches
    from sellbird.config import EnvConfig

    state = await _state(tmp_path)
    p = await _prospect_with_url(state)
    await state.schedule_linkedin_touch(p.id, p.linkedin_url)

    done = await run_linkedin_touches(
        state, _cfg(linkedin_enabled=True, touchpoints=True), EnvConfig())
    assert done == 0
    assert await state.due_linkedin_touches()
