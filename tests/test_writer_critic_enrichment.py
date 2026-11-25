"""Tests for Writer integration with Clay AI enrichment, REGISTRY_VERIFIED facts, and CopyCritic."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from sellbird.agents.writer import Writer
from sellbird.config import ComplianceConfig
from sellbird.models.company import Company
from sellbird.models.prospect import COMPANY_INBOX_SOURCE, Prospect
from sellbird.signals import seed_signal_catalog
from sellbird.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "writer_critic.db"))
    await s.init_db()
    await seed_signal_catalog(s)
    await s.set_signal_status("REGISTRY_VERIFIED", "confirmed")
    return s


def _config(provider="gmail", *, site_facts=True, require_approval=True):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider=provider, require_approval=require_approval)),
        product=SimpleNamespace(name="Sellbird", description="Outreach tool", key_benefits=["More replies"], pricing="$99/mo"),
        persona=SimpleNamespace(name="Alex Kim", company="Sellbird", role="Founder", tone="direct"),
        writer=SimpleNamespace(humanizer=False, email_rules="", site_facts=site_facts),
        compliance=ComplianceConfig(),
    )


class ScriptedBrain:
    """Returns queued replies and records every (task, prompt) call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def load_skills_for_agent(self, name):
        return ""

    def load_skill(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "Write a concise cold email."

    async def think_json(self, prompt, **kw):
        self.calls.append((kw.get("task", ""), prompt))
        if not self.replies:
            return None
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_personal_email_includes_enrichment_and_deduped_registry_facts(state):
    cid = await state.add_company(Company(name="Apex Plumbing", domain="apexplumbing.io"))
    col1 = await state.add_enrichment_column(name="Pricing Model", prompt="Pricing model?")
    col2 = await state.add_enrichment_column(name="Empty Field", prompt="Empty?")
    col3 = await state.add_enrichment_column(name="Failed Field", prompt="Failed?")

    await state.set_enrichment_value(col1, company_id=cid, value="Flat-rate emergency dispatch", status="completed")
    await state.set_enrichment_value(col2, company_id=cid, value="   ", status="completed")
    await state.set_enrichment_value(col3, company_id=cid, value="Should ignore", status="failed")

    await state.add_observations([
        {
            "company_id": cid,
            "signal_code": "REGISTRY_VERIFIED",
            "collector": "researcher",
            "value_num": 1.0,
            "value_text": "State Contractor License #99412 active",
        },
        {
            "company_id": cid,
            "signal_code": "REGISTRY_VERIFIED",
            "collector": "researcher",
            "value_num": 1.0,
            "value_text": "State Contractor License #99412 active",  # duplicate
        },
        {
            "company_id": cid,
            "signal_code": "REGISTRY_VERIFIED",
            "collector": "researcher",
            "value_num": 1.0,
            "value_text": "BBB A+ accredited since 2018",
        },
        {
            "company_id": cid,
            "signal_code": "REGISTRY_VERIFIED",
            "collector": "researcher",
            "value_num": 1.0,
            "value_text": "   ",
        },
    ])

    pid = await state.add_prospect(Prospect(
        first_name="Sam", last_name="Cole", title="Owner", company="Apex Plumbing",
        industry="Plumbing", email="sam@apexplumbing.io", email_status="verified", company_id=cid,
    ))
    prospect = await state.get_prospect(pid)

    brain = ScriptedBrain([{"subject": "dispatch pricing", "body": "Hi Sam, saw your flat-rate dispatch. Open to chat?"}])
    w = Writer(brain, state, _config())
    w.skills = ""
    await w._write_personal_email(prospect)

    assert len(brain.calls) == 1
    prompt = brain.calls[0][1]
    assert "- Pricing Model: Flat-rate emergency dispatch" in prompt
    assert "Empty Field" not in prompt
    assert "Should ignore" not in prompt
    assert "State Contractor License #99412 active" in prompt
    assert prompt.count("State Contractor License #99412 active") == 1
    assert "BBB A+ accredited since 2018" in prompt

    # Even with site_facts=False, REGISTRY_VERIFIED observations still reach the prompt
    brain2 = ScriptedBrain([{"subject": "license check", "body": "Hi Sam, quick note. Open to chat?"}])
    w2 = Writer(brain2, state, _config(site_facts=False))
    w2.skills = ""
    await w2._write_personal_email(prospect)
    prompt2 = brain2.calls[0][1]
    assert "- Verified public record: State Contractor License #99412 active" in prompt2
    assert "- Verified public record: BBB A+ accredited since 2018" in prompt2


@pytest.mark.asyncio
async def test_personal_email_handles_minimal_and_failing_state_gracefully():
    prospect = Prospect(
        id="p1", first_name="Maya", last_name="Lin", title="CEO",
        company="LinOps", company_id="c_min", email="maya@linops.io",
    )

    # 1. Minimal state with no enrichment/observation methods
    minimal_state = SimpleNamespace(
        get_company=AsyncMock(return_value=None),
        get_writing_feedback=AsyncMock(return_value=[]),
    )
    brain1 = ScriptedBrain([{"subject": "ops note", "body": "Hi Maya, quick question on onboarding. Open to chat?"}])
    w1 = Writer(brain1, minimal_state, _config())
    w1.skills = ""
    res1 = await w1._write_personal_email(prospect)
    assert res1 is not None and res1["subject"] == "ops note"

    # 2. State where enrichment/observation getters raise exceptions
    broken_state = SimpleNamespace(
        get_company=AsyncMock(return_value=None),
        get_writing_feedback=AsyncMock(return_value=[]),
        get_enrichment_columns=AsyncMock(side_effect=RuntimeError("cols error")),
        get_enrichment_values=AsyncMock(side_effect=RuntimeError("vals error")),
        get_observations=AsyncMock(side_effect=RuntimeError("obs error")),
    )
    brain2 = ScriptedBrain([{"subject": "ops note", "body": "Hi Maya, quick question on onboarding. Open to chat?"}])
    w2 = Writer(brain2, broken_state, _config())
    w2.skills = ""
    res2 = await w2._write_personal_email(prospect)
    assert res2 is not None and res2["subject"] == "ops note"

    # 3. Column name resolved via get_enrichment_columns when omitted on enrichment_values row
    lookup_state = SimpleNamespace(
        get_company=AsyncMock(return_value=None),
        get_writing_feedback=AsyncMock(return_value=[]),
        get_enrichment_columns=AsyncMock(return_value=[{"id": "col_bm", "name": "Business Model"}]),
        get_enrichment_values=AsyncMock(return_value=[{"column_id": "col_bm", "value": "B2B SaaS"}]),
        get_observations=AsyncMock(return_value=[]),
    )
    brain3 = ScriptedBrain([{"subject": "b2b saas", "body": "Hi Maya, quick question on B2B SaaS. Open to chat?"}])
    w3 = Writer(brain3, lookup_state, _config())
    w3.skills = ""
    await w3._write_personal_email(prospect)
    assert "- Business Model: B2B SaaS" in brain3.calls[0][1]


@pytest.mark.asyncio
async def test_critic_repairs_ungrounded_funding_and_rank_hallucinations_in_personalize(state):
    cid = await state.add_company(Company(name="Vektor Labs", domain="vektor.io"))
    pid = await state.add_prospect(Prospect(
        first_name="Elena", last_name="Rostova", title="CTO", company="Vektor Labs",
        industry="SaaS", email="elena@vektor.io", email_status="verified", company_id=cid,
        personalization_notes="Uses Kubernetes on AWS.",
    ))
    prospect = await state.get_prospect(pid)

    # First call (_write_personal_email) hallucinates $12M Series A and 4th on Google;
    # Second call (CopyCritic repair) fixes it using verified facts.
    brain = ScriptedBrain([
        {
            "subject": "congrats on $12M series a",
            "body": "Hi Elena, saw Vektor Labs ranks 4th on Google after your $12M Series A. Want to chat?",
        },
        {
            "subject": "kubernetes on aws",
            "body": "Hi Elena,\n\nNoticed Vektor Labs runs Kubernetes on AWS, which often inflates cluster costs.\n\nWant a quick cost checklist?",
        },
    ])
    w = Writer(brain, state, _config(require_approval=True))
    w.skills = ""
    await w._personalize_first_emails("camp_1", [prospect])

    assert [t for t, _ in brain.calls] == ["personal_email", "review_draft"]
    outbox = await state.get_outbox(status="pending_review")
    assert len(outbox) == 1
    assert "$12M" not in outbox[0]["body"]
    assert "4th on Google" not in outbox[0]["body"]
    assert "Kubernetes on AWS" in outbox[0]["body"]
    assert outbox[0]["body"].endswith("\n\nAlex")


@pytest.mark.asyncio
async def test_critic_blocks_unrepaired_hallucination_from_outbox(state):
    cid = await state.add_company(Company(name="Ghost Co", domain="ghost.io"))
    pid = await state.add_prospect(Prospect(
        first_name="Dan", last_name="Wu", title="CEO", company="Ghost Co",
        email="dan@ghost.io", email_status="verified", company_id=cid,
    ))
    prospect = await state.get_prospect(pid)

    # Repair fails (returns None), so hallucinated draft must NOT be staged
    brain = ScriptedBrain([
        {"subject": "series b", "body": "Hi Dan, congrats on the $50M Series B. Want a demo?"},
        None,
    ])
    w = Writer(brain, state, _config())
    w.skills = ""
    await w._personalize_first_emails("camp_bad", [prospect])

    assert [t for t, _ in brain.calls] == ["personal_email", "review_draft"]
    assert await state.get_outbox() == []


@pytest.mark.asyncio
async def test_grounded_enrichment_and_registry_facts_trigger_zero_extra_llm_calls(state):
    cid = await state.add_company(Company(name="BrightOps", domain="brightops.io"))
    col = await state.add_enrichment_column(name="Funding", prompt="Latest funding?")
    await state.set_enrichment_value(col, company_id=cid, value="Series A $12M in 2026")
    await state.add_observations([
        {
            "company_id": cid,
            "signal_code": "REGISTRY_VERIFIED",
            "collector": "researcher",
            "value_num": 1.0,
            "value_text": "Ranked #4 on Google for cloud devops austin",
        }
    ])

    pid = await state.add_prospect(Prospect(
        first_name="Liam", last_name="Hart", title="VP Eng", company="BrightOps",
        email="liam@brightops.io", email_status="verified", company_id=cid,
    ))
    prospect = await state.get_prospect(pid)

    # Email cites the exact grounded facts from Clay enrichment and REGISTRY_VERIFIED
    brain = ScriptedBrain([
        {
            "subject": "brightops growth",
            "body": "Hi Liam,\n\nBrightOps ranks 4th on Google after the $12M Series A, which can stretch support queues.\n\nWant a 3-point triage plan?",
        }
    ])
    w = Writer(brain, state, _config())
    w.skills = ""
    await w._personalize_first_emails("camp_clean", [prospect])

    # Zero extra LLM calls: CopyCritic sees funding/amount/rank in evidence_facts and approves immediately
    assert [t for t, _ in brain.calls] == ["personal_email"]
    outbox = await state.get_outbox(status="pending_review")
    assert len(outbox) == 1
    assert "$12M Series A" in outbox[0]["body"]


@pytest.mark.asyncio
async def test_write_sequence_runs_critic_without_extra_calls_on_clean_and_repairs_spam(state):
    p1 = Prospect(
        id="p1", first_name="Ana", last_name="Doe", title="Owner",
        company="Acme", industry="Roofing", email="ana@acme.io",
    )
    p2 = Prospect(
        id="p2", first_name="Ben", last_name="Lee", title="Owner",
        company="Beta", industry="Roofing", email="ben@beta.io",
    )

    # 1. Clean multi-prospect sequence with merge tags -> zero extra calls
    clean_seq = [
        {"step": 1, "subject": "quick roof question", "body": "Hi {{first_name}}, saw {{company}} handles commercial roofs. Open to a mockup?", "delay_days": 0},
        {"step": 2, "subject": "following up", "body": "Checking if {{company}} saw my note. Worth sharing a sample?", "delay_days": 3},
        {"step": 3, "subject": "closing the loop", "body": "Leaving the mockup open for {{company}}.", "delay_days": 4},
    ]
    brain_clean = ScriptedBrain([clean_seq])
    w_clean = Writer(brain_clean, state, _config(provider="instantly"))
    w_clean.skills = ""
    steps_clean = await w_clean._write_sequence([p1, p2])
    assert len(steps_clean) == 3
    assert [t for t, _ in brain_clean.calls] == ["write_sequence"]
    assert "{{first_name}}" in steps_clean[0].body

    # 2. Clean inbox batch sequence using {{company}} -> zero extra calls & {{company}} kept
    inbox_p = Prospect(
        id="p3", email="info@acme.io", company="Acme",
        title="General inbox", source=COMPANY_INBOX_SOURCE,
    )
    inbox_seq = [
        {"step": 1, "subject": "quick one", "body": "Hi there, who answers calls at {{company}}?", "delay_days": 0},
    ]
    brain_inbox = ScriptedBrain([inbox_seq])
    w_inbox = Writer(brain_inbox, state, _config(provider="instantly"))
    w_inbox.skills = ""
    steps_inbox = await w_inbox._write_sequence([inbox_p])
    assert len(steps_inbox) == 1
    assert [t for t, _ in brain_inbox.calls] == ["write_sequence"]
    assert "{{company}}" in steps_inbox[0].body

    # 3. Sequence step with spam trigger -> CopyCritic repairs while preserving merge tags
    spam_seq = [
        {"step": 1, "subject": "special offer", "body": "Hi {{first_name}}, act now for {{company}}. Want a look?", "delay_days": 0},
    ]
    repaired_step = {
        "subject": "roofing leads",
        "body": "Hi {{first_name}}, noticed {{company}} works across commercial roofs. Want a sample report?",
    }
    brain_spam = ScriptedBrain([spam_seq, repaired_step])
    w_spam = Writer(brain_spam, state, _config(provider="instantly"))
    w_spam.skills = ""
    steps_repaired = await w_spam._write_sequence([p1, p2])
    assert len(steps_repaired) == 1
    assert [t for t, _ in brain_spam.calls] == ["write_sequence", "review_draft"]
    assert "act now" not in steps_repaired[0].body.lower()
    assert "{{first_name}}" in steps_repaired[0].body and "{{company}}" in steps_repaired[0].body
