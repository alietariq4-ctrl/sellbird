"""Tests covering hardened AI Brain routing, structured JSON repair, stealth scraping, and agents."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from harvey_brain.schemas import resolve_response_format
from sellbird.agents.critic import CopyCritic
from sellbird.agents.enricher import ClayEnricher
from sellbird.agents.handler import Handler
from sellbird.agents.researcher import DeepResearcher
from sellbird.agents.scout import Scout
from sellbird.agents.writer import Writer
from sellbird.brain import Brain
from sellbird.collectors.profile import ProfileCollector
from sellbird.config import EnvConfig, SellbirdConfig
from sellbird.integrations.email_finder import _clean_name_part
from sellbird.integrations.stealth_fetch import _RedirectLimitError, fetch_page


class _ExtractSchema(BaseModel):
    company: str
    score: int


@pytest.mark.asyncio
async def test_brain_think_json_retries_on_prose_and_think_model_validates_schema(tmp_path):
    """Brain.think_json recovers from invalid prose once and think_model validates Pydantic schemas."""
    from sellbird.state import StateManager

    state = StateManager(str(tmp_path / "brain.db"))
    await state.init_db()
    replies = [
        "Sure! Here is a note [1] before the answer: not-valid-json",
        'Here you go: {"company": "Apex HVAC", "score": 92}',
        '{"company": "Apex HVAC", "score": "not-an-int"}',
        '{"company": "Apex HVAC", "score": 88}',
    ]

    async def fake_transport(prompt, *, agent="", task="", timeout=30.0, expect_json=False):
        return replies.pop(0)

    brain = Brain(state, transport=fake_transport)
    out = await brain.think_json("score company", agent="analyst", task="score")
    assert out == {"company": "Apex HVAC", "score": 92}

    model_out = await brain.think_model("extract company", _ExtractSchema, agent="analyst", task="extract")
    assert isinstance(model_out, _ExtractSchema)
    assert model_out.company == "Apex HVAC"
    assert model_out.score == 88


@pytest.mark.asyncio
async def test_brain_is_within_budget_without_max_usd_when_transport_active(tmp_path):
    """is_within_budget must not raise AttributeError when transport is active and max_usd is None."""
    from sellbird.state import StateManager

    state = StateManager(str(tmp_path / "budget.db"))
    await state.init_db()

    async def fake_transport(*a, **kw):
        return "ok"

    brain = Brain(state, transport=fake_transport)
    assert brain.quota is None
    assert await brain.is_within_budget(max_daily_calls=10, max_percent=80.0, max_usd=None) is True


def test_harvey_brain_array_tasks_exempted_from_strict_json_schema():
    """Array tasks under auto mode must not force strict object json_schema."""
    assert resolve_response_format("auto", task="write_sequence", supports_json_schema=True) is None
    assert resolve_response_format("auto", task="score_contacts", supports_json_schema=True) is None
    assert resolve_response_format("auto", task="suggest_offers", supports_json_schema=True) is None
    fmt = resolve_response_format("auto", task="personal_email", supports_json_schema=True)
    assert fmt is not None and fmt["type"] == "json_schema"


@pytest.mark.asyncio
async def test_stealth_fetch_redirect_limit_falls_back_to_firecrawl(monkeypatch):
    """Excess redirects must escalate to fallback tiers instead of aborting like SSRF ValueError."""
    import sellbird.integrations.stealth_fetch as sf

    async def ok_public(url):
        return None

    async def too_many(*a, **kw):
        raise _RedirectLimitError("Too many redirects")

    async def no_http(*a, **kw):
        raise sf.httpx.TooManyRedirects("too many redirects")

    async def ok_fc(url, *, api_key, base_url, timeout):
        return sf.FetchPageResult(
            url=url,
            final_url=url,
            status_code=200,
            html="<html><body><p>Recovered page content</p></body></html>",
            markdown="Recovered page content",
            source="firecrawl",
        )

    monkeypatch.setattr(sf, "validate_public_url", ok_public)
    monkeypatch.setattr(sf, "_fetch_tier1_curl_cffi", too_many)
    monkeypatch.setattr(sf, "_fetch_tier1_public_http", no_http)
    monkeypatch.setattr(sf, "_fetch_tier2_playwright", AsyncMock(return_value=None))
    monkeypatch.setattr(sf, "_fetch_tier3_firecrawl", ok_fc)

    res = await fetch_page("https://example.com", env=SimpleNamespace(firecrawl_api_key="fc_test"))
    assert res.ok and res.source == "firecrawl"


def test_profile_parsers_robots_cdata_and_contact_form():
    """Sitemap CDATA wraps unwrap cleanly and search/login forms don't spoof contact forms."""
    locs = ProfileCollector._extract_sitemap_locs(
        "<urlset><url><loc><![CDATA[https://acme.com/pricing]]></loc></url></urlset>"
    )
    assert locs == ["https://acme.com/pricing"]

    search_only = '<html><body><form role="search" action="/search"><input type="search"></form></body></html>'
    login_only = '<html><body><form action="/login"><input type="password"></form></body></html>'
    real_contact = '<html><body><form action="/contact"><input type="email"><textarea></textarea></form></body></html>'
    assert ProfileCollector._has_contact_form(search_only) is False
    assert ProfileCollector._has_contact_form(login_only) is False
    assert ProfileCollector._has_contact_form(real_contact) is True


def _test_cfg() -> SellbirdConfig:
    return SellbirdConfig(
        persona={
            "name": "Ali",
            "email": "ali@acme.io",
            "company": "VeloReach",
            "role": "Founder",
            "linkedin": "",
            "tone": "direct",
        },
        product={
            "name": "VeloReach",
            "description": "AI B2B outreach",
            "pricing": "$99/mo",
            "key_benefits": ["Automated research"],
            "objection_responses": {},
        },
        icp={
            "titles": ["CEO"],
            "industries": ["SaaS"],
            "company_size": "1-50",
            "geography": ["United States"],
        },
    )


def test_email_finder_folds_accents_and_handler_critic_boundaries():
    """Accented names fold cleanly; Handler & Critic avoid substring false-positives."""
    assert _clean_name_part("José-María") == "josemaria"

    critic = CopyCritic(SimpleNamespace(), _test_cfg())
    # "around" and "Google Analytics" in evidence must NOT authorize fabricated funding/ranking claims
    issues = critic.inspect_draft(
        {"subject": "Quick thought", "body": "Congrats on your Series B round and ranks #1 on Google!"},
        prospect_context="Founded around 2018. Uses Google Analytics for tracking.",
    )
    assert any("funding" in i for i in issues)
    assert any("rank" in i for i in issues)


@pytest.mark.asyncio
async def test_handler_classify_intent_word_boundaries():
    """Short words like 'good' must not trigger 'ooo', and 'not interested' stays not_interested."""
    fake_brain = SimpleNamespace(
        think=AsyncMock(side_effect=[
            "Looks good to me — question",
            "Definitely not_interested right now.",
        ])
    )
    handler = Handler(fake_brain, SimpleNamespace(), _test_cfg(), EnvConfig())
    prospect = SimpleNamespace(full_name=lambda: "Jane Doe", title="CEO", company="Acme")

    assert await handler._classify_intent("Tell me more", prospect) == "question"
    assert await handler._classify_intent("No thanks", prospect) == "not_interested"


@pytest.mark.asyncio
async def test_researcher_and_enricher_and_fail_closed_suppression(tmp_path):
    """Researcher marks ok=False on LLM failure; Enricher skips empty completed values; Writer fails closed on DB errors."""
    from sellbird.state import StateManager

    state = StateManager(str(tmp_path / "agents.db"))
    await state.init_db()

    # Researcher returns ok=False when think_json returns None
    res_brain = SimpleNamespace(
        think_json=AsyncMock(return_value=None),
        load_prompt=lambda *a, **k: "",
        load_skills_for_agent=lambda *a, **k: "",
        is_within_budget=AsyncMock(return_value=True),
    )
    researcher = DeepResearcher(res_brain, state, _test_cfg(), EnvConfig())
    researcher._gather_page_snippets = AsyncMock(return_value=[])
    researcher._check_public_osint = AsyncMock(return_value=[])
    researcher._search_web = AsyncMock(return_value=[])
    r_out = await researcher.research_company({"id": "c1", "name": "Acme", "domain": "acme.com"})
    assert r_out["ok"] is False

    # Enricher must NOT save blank values as completed
    enr_brain = SimpleNamespace(think_json=AsyncMock(return_value={"value": "", "source_url": ""}))
    enricher = ClayEnricher(enr_brain, state, _test_cfg(), EnvConfig())
    e_out = await enricher.enrich_company_column(
        {"id": "c1", "name": "Acme", "domain": "acme.com"},
        {"id": "col1", "name": "Pricing", "prompt": "What is pricing?", "use_web_search": False},
    )
    assert e_out["saved"] is False
    assert await state.get_enrichment_values("c1") == []

    # Scout & Writer fail closed on suppression DB error
    broken_state = SimpleNamespace(
        is_suppressed=AsyncMock(side_effect=RuntimeError("db locked")),
        suppressed_among=AsyncMock(side_effect=RuntimeError("db locked")),
        get_prospects=AsyncMock(return_value=[SimpleNamespace(email="bob@acme.com", email_status="verified", status="new", score=90)]),
    )
    scout = Scout(SimpleNamespace(), broken_state, _test_cfg(), EnvConfig())
    assert await scout._is_email_suppressed("bob@acme.com") == "suppression_check_failed"
