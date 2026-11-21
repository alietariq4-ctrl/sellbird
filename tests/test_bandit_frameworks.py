"""Phase 4 bandit: the Writer learns which framework earns replies.

writer.frameworks is empty by default, so nothing changes for anyone who
doesn't ask for it. With two or more named, each campaign is written in one
of them and tagged (campaigns.framework), and the next one is chosen by
Thompson sampling over real reply outcomes: a framework that keeps getting
replies should be picked far more often than one that never does, while
both still get explored.
"""

import random
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.agents.writer import Writer
from sellbird.bandit import (BUILT_IN, choose_framework, instruction_for,
                             known_frameworks)
from sellbird.models.campaign import Campaign
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager
from tests.test_writer_retry import FakeBrain, GOOD


def config(frameworks=None):
    writer = SimpleNamespace(frameworks=frameworks or [], email_rules="")
    return SimpleNamespace(
        writer=writer,
        channels=SimpleNamespace(email=SimpleNamespace(provider="instantly", require_approval=False)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "b.db"))
    await state.init_db()
    return state


async def add(state, n, status="new"):
    return await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        industry="Roofing", email=f"p{n}@co{n}.test", email_status="verified",
        status=status,
    ))


# ── framework selection ──

def test_no_frameworks_configured_means_builtin_only():
    assert known_frameworks(config()) == (BUILT_IN,)


def test_configured_frameworks_are_the_arms():
    got = known_frameworks(config(["pas", "bab"]))
    assert got == ("pas", "bab")  # built_in is not an arm unless listed


def test_unknown_framework_names_are_dropped():
    got = known_frameworks(config(["pas", "nonsense", "  BAB  "]))
    assert got == ("pas", "bab")


def test_builtin_has_no_instruction_line():
    assert instruction_for(BUILT_IN) == ""
    assert "Problem" in instruction_for("pas")


def test_sampler_explores_untried_frameworks():
    # Nothing tried yet: every framework must be reachable, not just the first.
    rng = random.Random(0)
    picks = {choose_framework({}, ("pas", "bab", BUILT_IN), rng=rng) for _ in range(50)}
    assert picks == {"pas", "bab", BUILT_IN}


def test_sampler_favours_the_framework_that_earns_replies():
    # pas: 15 replies / 100. bab: 10 / 100. Close rates, so pas should win
    # most draws but bab must still come up (exploration), not be frozen out.
    stats = {"pas": (15, 100), "bab": (10, 100)}
    rng = random.Random(1)
    picks = [choose_framework(stats, ("pas", "bab"), rng=rng) for _ in range(400)]
    pas_share = picks.count("pas") / len(picks)
    assert pas_share > 0.6
    assert picks.count("bab") > 0


def test_sampler_exploits_a_clear_winner():
    # pas: 40 / 100 vs bab: 1 / 100. The gap is wide enough that pas should
    # take nearly every draw once there is this much data.
    rng = random.Random(1)
    picks = [choose_framework({"pas": (40, 100), "bab": (1, 100)}, ("pas", "bab"), rng=rng)
             for _ in range(400)]
    assert picks.count("pas") / len(picks) > 0.95


def test_a_framework_never_used_is_not_starved_by_one_with_history():
    # bab has 3/3 replies; pas has never run. Exploration must still try pas.
    rng = random.Random(2)
    picks = {choose_framework({"bab": (3, 3)}, ("pas", "bab"), rng=rng) for _ in range(30)}
    assert "pas" in picks


# ── writer integration ──

@pytest.mark.asyncio
async def test_writer_tags_the_campaign_with_its_framework(state):
    await add(state, 1)
    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, config(["pas", "bab"]), None).run()
    campaigns = await state.get_campaigns_by_status("draft")
    assert len(campaigns) == 1
    assert campaigns[0].framework in ("pas", "bab")


@pytest.mark.asyncio
async def test_writer_without_frameworks_leaves_the_tag_empty(state):
    await add(state, 1)
    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, config(), None).run()
    campaigns = await state.get_campaigns_by_status("draft")
    assert campaigns[0].framework == ""


@pytest.mark.asyncio
async def test_the_chosen_framework_reaches_the_prompt(state):
    await add(state, 1)
    brain = FakeBrain(reply=GOOD)
    seen = []
    orig = brain.think_json

    async def capture(prompt, **kw):
        seen.append(prompt)
        return await orig(prompt, **kw)

    brain.think_json = capture
    await Writer(brain, state, config(["pas"]), None).run()
    assert any("FRAMEWORK: Problem, Agitate, Solve" in p for p in seen)


# ── outcome accounting ──

@pytest.mark.asyncio
async def test_stats_count_replies_against_finished_sequences(state):
    replied = await add(state, 1, status="replied")
    quiet = await add(state, 2, status="contacted")
    pending = await add(state, 3, status="queued")
    await state.add_campaign(Campaign(id="", name="c", prospect_ids=[replied, quiet, pending], framework="pas"))
    stats = await state.framework_stats()
    # 1 reply out of 2 finished; the still-queued prospect is not counted.
    assert stats["pas"] == (1, 2)


@pytest.mark.asyncio
async def test_a_campaign_with_no_outcomes_yet_is_absent(state):
    p = await add(state, 1)  # still 'new'
    await state.add_campaign(Campaign(id="", name="c", prospect_ids=[p], framework="bab"))
    assert await state.framework_stats() == {}
