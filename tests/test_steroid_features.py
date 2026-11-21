"""Comprehensive tests for Sellbird's high-power open-source subsystems & pipeline bug fixes.

Covers:
1. 3-tier SSRF-guarded Stealth Web & Markdown Waterfall (stealth_fetch.py)
2. Open-source Discovery Providers: ddgs, SearXNG, Tavily, Firecrawl Search (discover.py)
3. Autonomous OSINT DeepResearcher with strict citation firewall (researcher.py)
4. Clay-style Custom AI Enrichment Engine (enricher.py)
5. Copywriting & Fact Critic Agent (critic.py)
6. Real Auto-Booking CalendarClient (Cal.com v2 / Google / M365), Twenty CRM + Webhook Sync,
   and Telegram + Slack Operator Copilot
7. Collector, Scout, Writer, Sender, Handler, Multi-Inbox, Mail, Email Finder, Send Window,
   and Analyst bug fixes.
"""

from __future__ import annotations

import email.message
import json
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from sellbird.agents.analyst import Analyst
from sellbird.agents.critic import CopyCritic
from sellbird.agents.enricher import ClayEnricher
from sellbird.agents.handler import BOUNCE_COUNT_KEY, Handler
from sellbird.agents.researcher import DeepResearcher
from sellbird.agents.scout import Scout
from sellbird.agents.sender import Sender
from sellbird.agents.writer import Writer
from sellbird.collectors import discover as D
from sellbird.collectors.profile import ProfileCollector, TECH_FINGERPRINTS
from sellbird.config import EnvConfig, load_config
from sellbird.handoff import call_tool
from sellbird.integrations import email_finder as EF
from sellbird.integrations.calendar import CalendarClient, format_slots_for_reply
from sellbird.integrations.copilot import OperatorCopilot, notify_operator
from sellbird.integrations.crm_sync import CRMSyncClient, sync_lead_event
from sellbird.integrations.mail_provider import InboundMessage, SendResult
from sellbird.integrations.microsoft import MicrosoftProvider
from sellbird.integrations.smtp_mail import SmtpImapProvider, SmtpSettings
from sellbird.integrations.stealth_fetch import (
    FetchPageResult,
    extract_markdown,
    extract_published_date,
    fetch_page,
    is_spa_shell,
    validate_public_url,
)
from sellbird.integrations.tech_detect import FINGERPRINTS, detect_tech
from sellbird.mailboxes import MailboxStore, load_inboxes
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.send_window import zone_for
from sellbird.signals import SIGNAL_CATALOG, seed_signal_catalog
from sellbird.site_facts import site_facts
from sellbird.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "steroid.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


# ═══════════════════════════════════════════════════════════════════════════
# 1. Stealth Web & Markdown Waterfall + Mandatory SSRF Guard
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_url", [
    "http://127.0.0.1/admin",
    "http://localhost:8080/api",
    "http://sub.localhost/secret",
    "http://10.0.0.5/internal",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[64:ff9b::7f00:1]/",
    "file:///etc/passwd",
    "ftp://93.184.216.34/file",
    "https://user:pass@93.184.216.34/",
])
async def test_stealth_fetch_blocks_ssrf_before_any_tier(bad_url):
    with pytest.raises(ValueError):
        await validate_public_url(bad_url)
    with pytest.raises(ValueError):
        await fetch_page(bad_url, backend="firecrawl", env=SimpleNamespace(firecrawl_api_key="fc_test"))


@pytest.mark.asyncio
async def test_stealth_fetch_blocks_dns_resolving_to_loopback(monkeypatch):
    loop = __import__("asyncio").get_running_loop()

    async def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError):
        await validate_public_url("https://rebind.attacker.example/path")


def test_is_spa_shell_detects_empty_bundles_not_real_pages():
    assert is_spa_shell('<html><body><div id="root"></div><script type="module" src="/assets/index-a1b2.js"></script></body></html>')
    assert is_spa_shell('<html><body><noscript>You need to enable JavaScript to run this app.</noscript><div id="app"></div></body></html>')
    real_html = "<html><body><main><h1>Northvale Industrial Supply</h1><p>" + ("We supply CNC tooling across Ohio. " * 12) + "</p></main></body></html>"
    assert not is_spa_shell(real_html)


def test_markdown_and_published_date_extraction():
    html = """<!doctype html><html><head>
    <meta property="article:published_time" content="2026-03-15T10:00:00Z">
    <title>Acme Raises Seed</title></head>
    <body><article><h1>Acme Raises $4M</h1>
    <p>Acme announced a new product for <a href="https://acme.example/platform">warehouse teams</a> today.</p>
    </article></body></html>"""
    md = extract_markdown(html, "https://acme.example/blog/seed")
    assert "Acme" in md
    pub = extract_published_date(html, "https://acme.example/blog/seed")
    assert pub == "2026-03-15"


@pytest.mark.asyncio
async def test_stealth_fetch_waterfall_escalates_spa_to_tier2_and_tier3(monkeypatch):
    import sellbird.integrations.stealth_fetch as sf

    async def ok_public(url):
        return None

    async def fake_t1(url, **kw):
        return url, 200, '<html><body><div id="root"></div></body></html>', "curl_cffi"

    async def fake_t2(url, **kw):
        return None  # simulate Playwright binary absent

    async def fake_t3(url, *, api_key, base_url, timeout):
        assert api_key == "fc_live_123"
        return FetchPageResult(
            url=url,
            final_url=url,
            status_code=200,
            html="<html><body><h1>Rendered via Firecrawl</h1></body></html>",
            markdown="# Rendered via Firecrawl",
            published_date="2026-04-01",
            source="firecrawl",
            cost_usd=0.001,
        )

    monkeypatch.setattr(sf, "validate_public_url", ok_public)
    monkeypatch.setattr(sf, "_fetch_tier1_curl_cffi", fake_t1)
    monkeypatch.setattr(sf, "_fetch_tier2_playwright", fake_t2)
    monkeypatch.setattr(sf, "_fetch_tier3_firecrawl", fake_t3)

    res = await fetch_page(
        "https://spa.example.com",
        env=SimpleNamespace(firecrawl_api_key="fc_live_123"),
        render_js=True,
    )
    assert res.ok
    assert res.source == "firecrawl"
    assert "Rendered via Firecrawl" in res.markdown


# ═══════════════════════════════════════════════════════════════════════════
# 2. Open-Source Discovery Providers (ddgs, searxng, tavily, firecrawl_search)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_open_source_discovery_providers_parse_and_normalize(monkeypatch):
    # 1. DuckDuckGoSearch (keyless, $0)
    ddg = D.PROVIDERS["ddgs"]
    assert ddg.env_keys == ()
    assert ddg.estimate([D.DiscoveryQuery(term="b2b saas")]) == 0.0
    monkeypatch.setattr(
        D.DuckDuckGoSearch,
        "_run_ddgs",
        staticmethod(lambda kw, mr: [
            {"href": "https://www.brightops.io/about", "title": "BrightOps | DevOps Automation"},
            {"href": "https://www.linkedin.com/company/brightops", "title": "LinkedIn"},
        ]),
    )
    async with httpx.AsyncClient() as client:
        rows = await ddg.fetch(client, {}, D.DiscoveryQuery(term="devops automation", depth=10))
    assert rows[0].domain == "brightops.io"
    assert rows[0].name == "BrightOps"

    # 2. SearXNGSearch
    searx = D.PROVIDERS["searxng"]
    searx_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"results": [{"url": "https://kellercnc.com", "title": "Keller CNC Machining"}]})
    )
    async with httpx.AsyncClient(transport=searx_transport) as client:
        s_rows = await searx.fetch(client, {"searxng_url": "https://searx.example.org"}, D.DiscoveryQuery(term="cnc"))
    assert len(s_rows) == 1 and s_rows[0].domain == "kellercnc.com"

    # 3. TavilySearch
    tavily = D.PROVIDERS["tavily"]
    tavily_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"results": [{"url": "https://verveanalytics.ai", "title": "Verve Analytics"}]})
    )
    async with httpx.AsyncClient(transport=tavily_transport) as client:
        t_rows = await tavily.fetch(client, {"tavily_api_key": "tvly-123"}, D.DiscoveryQuery(term="analytics"))
    assert len(t_rows) == 1 and t_rows[0].domain == "verveanalytics.ai"
    assert tavily.last_cost == pytest.approx(0.008)

    # 4. FirecrawlSearch
    fcs = D.PROVIDERS["firecrawl_search"]
    fc_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"success": True, "data": {"web": [{"url": "https://orbitcloud.dev", "title": "OrbitCloud"}]}})
    )
    async with httpx.AsyncClient(transport=fc_transport) as client:
        f_rows = await fcs.fetch(client, {"firecrawl_api_key": "fc-123"}, D.DiscoveryQuery(term="cloud"))
    assert len(f_rows) == 1 and f_rows[0].domain == "orbitcloud.dev"


# ═══════════════════════════════════════════════════════════════════════════
# 3. DeepResearcher (OSINT + Citation Verification Firewall)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_deep_researcher_enforces_strict_citation_verification(state, monkeypatch):
    import sellbird.agents.researcher as res_mod

    cid = await state.add_company(Company(name="Vektor Labs", domain="vektorlabs.io", website="https://vektorlabs.io"))

    async def fake_search(self, query):
        return [
            {"url": "https://vektorlabs.io/blog/series-a", "title": "Vektor Raises $8M", "snippet": "Led by Sequoia"},
        ]

    async def fake_osint(self, name, domain):
        return [
            {"url": "https://github.com/vektorlabs", "title": "GitHub Org: Vektor Labs", "snippet": "14 public repos"},
        ]

    async def fake_fetch(url, **kw):
        return FetchPageResult(
            url=url, final_url=url, status_code=200, html="<p>Vektor</p>", markdown="Vektor raised $8M.",
        )

    monkeypatch.setattr(DeepResearcher, "_search_web", fake_search)
    monkeypatch.setattr(DeepResearcher, "_check_public_osint", fake_osint)
    monkeypatch.setattr(res_mod, "validate_public_url", AsyncMock(return_value=None))
    monkeypatch.setattr(res_mod, "fetch_page", fake_fetch)

    fake_brain = SimpleNamespace(
        think_json=AsyncMock(return_value={
            "summary": "Vektor Labs builds developer observability tools.",
            "icp_fit": "Strong engineering-led B2B SaaS fit.",
            "verified_facts": [
                {"fact": "Maintains 14 public repositories on GitHub", "evidence_url": "https://github.com/vektorlabs"},
                {"fact": "Hallucinated SOC2 claim", "evidence_url": "https://hallucinated-url.example/soc2"},
            ],
            "decision_makers": [
                {"name": "Elena Rostova", "title": "CTO", "evidence_url": "https://vektorlabs.io/blog/series-a"},
                {"name": "Fake Person", "title": "CEO", "evidence_url": "https://invented.example/team"},
            ],
            "recent_triggers": [
                {"type": "funding", "detail": "Raised $8M Series A", "evidence_url": "https://vektorlabs.io/blog/series-a"},
            ],
        })
    )

    researcher = DeepResearcher(state, fake_brain)
    report = await researcher.research_company(cid)

    assert report["ok"] is True
    assert report["dropped_unverified"] == 2
    assert len(report["verified_facts"]) == 1
    assert len(report["decision_makers"]) == 1
    assert report["observations_written"] == 3

    obs = await state.get_observations(company_id=cid, latest_only=False)
    ev_urls = {o["evidence_url"] for o in obs if o["signal_code"] == "REGISTRY_VERIFIED"}
    assert "https://hallucinated-url.example/soc2" not in ev_urls
    assert "https://invented.example/team" not in ev_urls
    assert ev_urls == {"https://github.com/vektorlabs", "https://vektorlabs.io/blog/series-a"}

    updated_company = await state.get_company(cid)
    assert any("Raised $8M Series A" in s.get("detail", "") for s in updated_company.signals)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Clay-Style Custom AI Enrichment Engine (enricher.py)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clay_enricher_defensive_when_no_columns_and_saves_values(state):
    brain = SimpleNamespace(
        think_json=AsyncMock(return_value={"value": "Usage-based SaaS", "source_url": "https://acme.io"})
    )
    # Without enrichment column methods, returns 0 cleanly
    enricher_plain = ClayEnricher(SimpleNamespace(), brain)
    assert await enricher_plain.run() == 0

    cid = await state.add_company(Company(name="Acme Cloud", domain="acme.io", website="https://acme.io", description="Usage-based API billing"))
    saved_cells: list[dict] = []

    state.get_enrichment_columns = AsyncMock(return_value=[
        {"id": "col_pricing_model", "name": "Pricing Model", "prompt": "Classify pricing model", "use_web_search": False}
    ])
    state.set_enrichment_value = AsyncMock(side_effect=lambda **kw: saved_cells.append(kw))

    enricher = ClayEnricher(state, brain)
    count = await enricher.enrich_company(cid)
    assert count == 1
    assert saved_cells[0]["column_id"] == "col_pricing_model"
    assert saved_cells[0]["value"] == "Usage-based SaaS"
    assert saved_cells[0]["source_url"] == "https://acme.io"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Copywriting & Fact Critic Agent (critic.py)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_copy_critic_catches_hallucinations_and_repairs():
    repair_brain = SimpleNamespace(
        think_json=AsyncMock(return_value={
            "email": {
                "subject": "missing schema on acme",
                "body": "Hi Sam,\n\nAcme's homepage has no JSON-LD schema markup, so Google gets less context for local searches.\n\nWant me to send the snippet?",
            }
        })
    )
    critic = CopyCritic(repair_brain)

    bad_draft = {
        "subject": "Congrats on the $12M Series A!",
        "body": "Hi {{first_name}} — I hope this finds you well! Saw your LinkedIn post about your $12M Series A. Click here!",
    }
    res = await critic.review_and_refine(
        bad_draft,
        prospect_context="Name: Sam Cole, Owner at Acme Plumbing",
        evidence_facts=["Their homepage has no JSON-LD structured data"],
        names=["Sam", "Cole", "Acme Plumbing"],
    )
    assert res["refined"] is True
    assert res["approved"] is True
    assert "{{" not in res["body"]
    assert "$12M" not in res["body"]

    # Clean draft triggers zero LLM calls
    repair_brain.think_json.reset_mock()
    clean = await critic.review_and_refine(
        {"subject": "schema on acme", "body": res["body"]},
        prospect_context="Name: Sam Cole, Owner at Acme Plumbing",
        evidence_facts=["Their homepage has no JSON-LD structured data"],
        names=["Sam", "Cole", "Acme Plumbing"],
    )
    assert clean["approved"] is True
    assert clean["refined"] is False
    assert repair_brain.think_json.call_count == 0


# ═══════════════════════════════════════════════════════════════════════════
# 6. CalendarClient (Cal.com v2), CRMSyncClient (Twenty/Webhook), OperatorCopilot
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_calendar_client_backward_compat_and_calcom_v2():
    # 0-arg unconfigured client stays backward-compatible
    unconfigured = CalendarClient()
    assert await unconfigured.get_available_slots("2026-11-18..2026-11-22") == []
    assert await unconfigured.book_meeting("lead@acme.com", {"start": "2026-11-19T15:00:00Z"}) is False
    assert await unconfigured.book_meeting("", {}) is False

    seen_requests: list[httpx.Request] = []

    def cal_handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        assert request.headers.get("cal-api-version") == "2024-09-04"
        assert request.headers.get("authorization") == "Bearer cal_live_secret"
        if request.url.path.endswith("/v2/slots"):
            return httpx.Response(200, json={
                "status": "success",
                "data": {"2026-11-19": [{"start": "2026-11-19T14:00:00Z"}, {"start": "2026-11-19T15:30:00Z"}]},
            })
        if request.url.path.endswith("/v2/bookings"):
            body = json.loads(request.content)
            assert body["attendee"]["email"] == "dana@acme.com"
            assert body["eventTypeId"] == 98765
            return httpx.Response(201, json={"status": "success", "data": {"uid": "bk_123"}})
        return httpx.Response(404)

    cal = CalendarClient(
        env=SimpleNamespace(calcom_api_key="cal_live_secret", calcom_event_type_id="98765"),
        transport=httpx.MockTransport(cal_handler),
    )
    slots = await cal.get_available_slots("2026-11-19..2026-11-20")
    assert len(slots) == 2
    assert "Thursday" in format_slots_for_reply(slots)
    booked = await cal.book_meeting("dana@acme.com", slots[0], prospect_name="Dana")
    assert booked is True
    assert len(seen_requests) == 2


@pytest.mark.asyncio
async def test_crm_sync_twenty_and_signed_webhook():
    calls: list[tuple[str, dict, dict]] = []

    def crm_handler(req: httpx.Request) -> httpx.Response:
        calls.append((str(req.url), dict(req.headers), json.loads(req.content)))
        if req.url.path == "/rest/people":
            return httpx.Response(201, json={"data": {"createPerson": {"id": "person_42"}}})
        if req.url.path == "/rest/opportunities":
            return httpx.Response(201, json={"data": {"createOpportunity": {"id": "opp_99"}}})
        if req.url.host == "hooks.example.com":
            return httpx.Response(200, json={"received": True})
        return httpx.Response(404)

    prospect = Prospect(first_name="Maya", last_name="Lin", email="maya@linops.io", company="LinOps", title="CEO")
    env = SimpleNamespace(
        twenty_api_key="tw_key",
        twenty_base_url="https://crm.twenty.example",
        webhook_url="https://hooks.example.com/sellbird",
        webhook_secret="whsec_test",
    )
    outcome = await sync_lead_event(
        "interested",
        prospect=prospect,
        details={"stage": "engaged"},
        env=env,
        transport=httpx.MockTransport(crm_handler),
    )
    assert outcome["ok"] is True
    assert set(outcome["synced"]) == {"twenty", "webhook"}
    assert len(calls) == 3  # person + opportunity + webhook
    webhook_headers = calls[2][1]
    assert webhook_headers.get("x-sellbird-signature", "").startswith("sha256=")


@pytest.mark.asyncio
async def test_copilot_alerts_telegram_and_slack():
    sent: list[str] = []

    def copilot_handler(req: httpx.Request) -> httpx.Response:
        sent.append(str(req.url))
        return httpx.Response(200, json={"ok": True})

    env = SimpleNamespace(
        telegram_bot_token="12345:ABCDEF",
        telegram_chat_id="998877",
        slack_webhook_url="https://hooks.slack.com/services/T00/B00/XXX",
    )
    res = await notify_operator(
        "hot_reply",
        "Interested reply from Maya",
        "Let's chat Thursday afternoon.",
        details={"company": "LinOps"},
        env=env,
        transport=httpx.MockTransport(copilot_handler),
    )
    assert res["ok"] is True
    assert set(res["channels"]) == {"telegram", "slack"}
    assert len(sent) == 2


# ═══════════════════════════════════════════════════════════════════════════
# 7. Bug Fixes Across Tech Detect, Profile, Site Facts, Scout, Writer, Mail,
#    Bounce, Multi-Inbox, Microsoft, Email Finder, Send Window & Analyst
# ═══════════════════════════════════════════════════════════════════════════


def test_unified_tech_fingerprints_and_salesforce_duda_precision():
    assert len(FINGERPRINTS) >= 75
    assert set(FINGERPRINTS) == set(TECH_FINGERPRINTS)
    # workforce.com and airforce.com must NEVER trigger Salesforce!
    assert "Salesforce" not in detect_tech('<a href="https://workforce.com">Workforce</a><script src="https://cdn.airforce.com/app.js"></script>')
    assert "Salesforce" in detect_tech('<script src="https://acme.my.salesforce.com/embedded.js"></script>')
    assert "Salesforce" in detect_tech('<iframe src="https://acme.force.com/support"></iframe>')
    # d1.awsstatic must NEVER trigger Duda!
    assert "Duda" not in detect_tech('<img src="https://d1.awsstatic.com/logos/aws.png">')
    assert "Duda" in detect_tech('<script src="https://irp.cdn-website.com/widget.js"></script>')


@pytest.mark.asyncio
async def test_profile_sitemapindex_and_signal_sync_and_expanded_site_facts(state):
    cid = await state.add_company(Company(name="Apex HVAC", domain="apexhvac.example"))
    pages = {
        "https://apexhvac.example": """<html>
            <script src="https://www.googletagmanager.com/gtag/js?id=AW-999"></script>
            <script src="https://book.housecallpro.com/hc.js"></script>
            <footer>Website by <a href="https://sparkagency.io">Spark Agency</a></footer>
        </html>""",
        "https://apexhvac.example/sitemap.xml": """<sitemapindex>
            <sitemap><loc>https://apexhvac.example/post-sitemap.xml</loc></sitemap>
            <sitemap><loc>https://apexhvac.example/page-sitemap.xml</loc></sitemap>
        </sitemapindex>""",
        "https://apexhvac.example/post-sitemap.xml": "".join(
            f"<url><loc>https://apexhvac.example/p{i}</loc><lastmod>2026-03-01</lastmod></url>"
            for i in range(4)
        ),
        "https://apexhvac.example/page-sitemap.xml": "".join(
            f"<url><loc>https://apexhvac.example/s{i}</loc></url>"
            for i in range(5)
        ),
        "https://apexhvac.example/robots.txt": "User-agent: *\nAllow: /",
    }

    c = ProfileCollector(state, confirmed={"TECH_STACK", "RUNNING_GOOGLE_ADS", "INCUMBENT_AGENCY", "SITE_PAGE_COUNT"})
    c._get = AsyncMock(side_effect=lambda u: pages.get(u, ""))
    obs = await c.profile_company(cid, "apexhvac.example")
    await state.add_observations(obs)

    by_code = {o["signal_code"]: o for o in obs}
    # 4 + 5 = 9 real pages from child sitemaps (not 2 from the index!)
    assert by_code["SITE_PAGE_COUNT"]["value_num"] == 9
    comp = await state.get_company(cid)
    assert "Housecall Pro" in comp.tech_stack
    assert any(s["type"] == "ads" for s in comp.signals)

    # Also add SERP_RANK and REGISTRY_VERIFIED and verify expanded site_facts + Writer._has_tip_basis
    await state.add_observations([
        {"company_id": cid, "signal_code": "SERP_RANK", "collector": "discover", "value_num": 14.0, "value_text": "hvac denver"},
        {"company_id": cid, "signal_code": "REGISTRY_VERIFIED", "collector": "researcher", "value_num": 1.0, "value_text": "Licensed HVAC Contractor #4412"},
    ])
    facts_str = "\n".join(await site_facts(state, cid))
    assert "#14 organically on Google for 'hvac denver'" in facts_str
    assert "Google Ads tag" in facts_str
    assert "Spark Agency" in facts_str
    assert "Licensed HVAC Contractor #4412" in facts_str


@pytest.mark.asyncio
async def test_scout_unwraps_dict_scores_coerces_strings_and_emits_email_signals(state):
    cfg = load_config()
    scout = Scout(
        brain=SimpleNamespace(
            load_prompt=lambda *a, **k: "Score prompt",
            think_json=AsyncMock(return_value={
                "scores": [{"index": "1", "score": "88", "personalization": "Hiring ops lead"}]
            }),
        ),
        state=state,
        config=cfg,
        env=EnvConfig(),
    )
    scout.skills = "Prospecting skill block"
    p = Prospect(first_name="Liam", last_name="Hart", email="liam@hart.io", email_status="verified", company="Hart Co", title="Founder")
    scored = await scout._score_contacts([p])
    assert len(scored) == 1
    assert scored[0].score == 88
    assert scored[0].personalization_notes == "Hiring ops lead"

    cid = await state.add_company(Company(name="Hart Co", domain="hart.io"))
    await scout._observe_email_signals(cid, "hart.io", email="liam@hart.io", status="verified", pattern="{f}")
    codes = {o["signal_code"] for o in await state.get_observations(company_id=cid)}
    assert {"EMAIL_PATTERN", "EMAIL_STATUS"} <= codes


@pytest.mark.asyncio
async def test_writer_prompt_has_no_merge_leak_and_unwraps_nested_outputs(state):
    from sellbird.paths import PROJECT_ROOT

    raw_writer_md = (PROJECT_ROOT / "prompts" / "writer.md").read_text(encoding="utf-8")
    assert "{{first_name}}" not in raw_writer_md

    cfg = load_config()
    brain = SimpleNamespace(
        load_skills_for_agent=lambda _: "",
        load_skill=lambda name: "FRAMEWORK_RULE" if name == "email_frameworks" else "",
        load_prompt=lambda *a, **k: "Writer prompt",
        think_json=AsyncMock(return_value={
            "email": {"subject": "google rank in denver", "body": "Hi {{first_name}}, Hart Co ranks 14th on Google. Want our page-one plan?"}
        }),
    )
    w = Writer(brain, state, cfg, EnvConfig())
    w.skills = w._load_writer_skills()
    assert "FRAMEWORK_RULE" in w.skills

    prospect = Prospect(first_name="Liam", last_name="Hart", email="liam@hart.io", email_status="verified", company="Hart Co", title="Owner")
    draft = await w._write_personal_email(prospect)
    assert draft is not None
    assert "{{first_name}}" not in draft["body"]
    assert "Liam" in draft["body"]

    steps = w._parse_sequence({
        "custom_wrapper": [
            {"subject": "one", "body": "Body 1", "delay_days": "0"},
            {"subject": "two", "body": "Body 2", "delay_days": "4"},
        ]
    })
    assert len(steps) == 2 and steps[1].delay_days == 4


@pytest.mark.asyncio
async def test_smtp_peek_self_filter_and_imap_error_raises(monkeypatch):
    import imaplib as imap_mod

    cfg = load_config()
    cfg.persona.email = "me@myagency.com"
    provider = SmtpImapProvider(
        cfg,
        EnvConfig(),
        smtp=SmtpSettings(host="smtp.example.com", username="me@myagency.com", password="pw", imap_host="imap.example.com"),
    )

    def _make_raw(sender: str, subject: str, body: str) -> bytes:
        m = email.message.EmailMessage()
        m["From"] = sender
        m["To"] = "me@myagency.com"
        m["Subject"] = subject
        m["Message-ID"] = f"<{sender}-{subject}@test>"
        m.set_content(body)
        return m.as_bytes()

    fetch_commands: list[str] = []

    class FakeIMAP:
        def __init__(self, host, port):
            pass

        def login(self, u, p):
            return "OK", [b"logged in"]

        def select(self, box):
            return "OK", [b"2"]

        def search(self, charset, criterion):
            return "OK", [b"1 2"]

        def fetch(self, msg_id, spec):
            fetch_commands.append(spec)
            if msg_id == b"1":
                return "OK", [(b"1 (BODY[] {100})", _make_raw("me@myagency.com", "Self", "My own copy"))]
            return "OK", [(b"2 (BODY[] {100})", _make_raw("client@prospect.io", "Re: hello", "Yes send it"))]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr(imap_mod, "IMAP4_SSL", FakeIMAP)
    replies = await provider.get_replies()
    assert all(cmd == "(BODY.PEEK[])" for cmd in fetch_commands)
    assert len(replies) == 1 and replies[0].from_email == "client@prospect.io"

    # Connection error MUST raise so Handler holds outbound sends
    class BrokenIMAP:
        def __init__(self, host, port):
            raise OSError("IMAP connection refused")

    monkeypatch.setattr(imap_mod, "IMAP4_SSL", BrokenIMAP)
    with pytest.raises(OSError):
        await provider.get_replies()


@pytest.mark.asyncio
async def test_handler_bounce_matching_unknown_ignore_campaign_attribution_and_handoff_retry(state, monkeypatch):
    cfg = load_config()
    cfg.channels.email.provider = "smtp"
    brain = SimpleNamespace(
        load_skills_for_agent=lambda _: "",
        load_prompt=lambda *a, **k: "",
        think=AsyncMock(return_value="not_interested"),
    )
    handler = Handler(brain, state, cfg, EnvConfig())

    pid = await state.add_prospect(Prospect(
        first_name="Nora", last_name="Vance", email="nora@vance.io",
        email_status="verified", company="Vance", title="CEO", status="contacted",
    ))
    camp_id = await state.add_campaign(Campaign(
        id="", name="q4-saas", channel="email",
        sequence=[EmailStep(step=1, subject="hi", body="body", delay_days=0)],
        prospect_ids=[pid], status="active",
    ))
    outbox_id = await state.add_outbox_item(
        prospect_id=pid, campaign_id=camp_id, step=1, to_email="nora@vance.io",
        subject="hi", body="body", send_at="2026-11-01T10:00:00", status="sent", provider="smtp",
    )
    await state.update_outbox_item(outbox_id, message_id="<sent-msg-1@myagency.com>", sent_at="2026-11-01T10:00:00")

    # 1. Unmatched spam bounce MUST NOT increment bounce_count
    spam_bounce = InboundMessage(
        provider_id="b_spam",
        from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body="Final-Recipient: rfc822; random-stranger@nowhere.invalid",
        message_id="<dsn-spam@google>",
        in_reply_to="",
        is_bounce=True,
    )
    await handler._handle_bounce(spam_bounce)
    assert int(await state.get_setting(BOUNCE_COUNT_KEY) or 0) == 0

    # 2. Real DSN bounce with empty in_reply_to matched via Final-Recipient
    real_bounce = InboundMessage(
        provider_id="b_real",
        from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body="Final-Recipient: rfc822; nora@vance.io\nAction: failed",
        message_id="<dsn-real@google>",
        in_reply_to="",
        is_bounce=True,
    )
    await handler._handle_bounce(real_bounce)
    assert int(await state.get_setting(BOUNCE_COUNT_KEY) or 0) == 1
    assert (await state.get_prospect(pid)).email_status == "invalid"

    # 3. Native reply attributes campaign_id via References header
    pid2 = await state.add_prospect(Prospect(
        first_name="Owen", last_name="Cole", email="owen@cole.io",
        email_status="verified", company="Cole", title="CEO", status="contacted",
    ))
    outbox2 = await state.add_outbox_item(
        prospect_id=pid2, campaign_id=camp_id, step=1, to_email="owen@cole.io",
        subject="hi", body="body", send_at="2026-11-01T10:00:00", status="sent", provider="smtp",
    )
    await state.update_outbox_item(outbox2, message_id="<sent-msg-2@myagency.com>", sent_at="2026-11-01T10:00:00")

    reply_msg = InboundMessage(
        provider_id="rep_2",
        from_email="owen@cole.io",
        subject="Re: hi",
        body="Not interested right now.",
        message_id="<reply-2@cole.io>",
        in_reply_to="",
        headers={"references": "<older@x> <sent-msg-2@myagency.com>"},
        is_bounce=False,
    )
    matched = await handler._match_outbox_for_inbound(reply_msg, fallback_recipient=True)
    assert matched is not None and matched["campaign_id"] == camp_id

    # 4. _hand_off_once does NOT cache ok=False failures permanently
    import sellbird.handoff as handoff_mod

    outcomes = [{"ok": False, "error": "timeout"}, {"ok": True, "result": "https://audit.example/123"}]
    monkeypatch.setattr(handoff_mod, "hand_off", AsyncMock(side_effect=lambda *a, **k: outcomes.pop(0)))
    prospect2 = await state.get_prospect(pid2)
    first_try = await handler._hand_off_once(prospect2, "Yes please")
    assert first_try["ok"] is False
    second_try = await handler._hand_off_once(prospect2, "Yes please")
    assert second_try["ok"] is True


@pytest.mark.asyncio
async def test_handoff_blocks_private_mcp_url():
    h = SimpleNamespace(mcp_url="http://127.0.0.1:9000/mcp", mcp_command=[], token_env="", tool="ping")
    res = await call_tool(h, {})
    assert res["ok"] is False
    assert "non-public" in res["error"] or "Address is not public" in res["error"]


@pytest.mark.asyncio
async def test_multi_inbox_personalized_step1_reassignment_and_single_inbox_pool(state):
    cfg = load_config()
    cfg.channels.email.provider = "smtp"
    cfg.channels.email.follow_ups = False  # new_rows == 0 when Step 1 was personalized!
    cfg.persona.email = "main@agency.example"

    main_prov = SimpleNamespace(name="smtp", is_configured=lambda: True)
    store = MailboxStore(db_path=state.db_path)

    # Single-inbox pool enforcement: restrict campaign to a non-MAIN mailbox id
    pid = await state.add_prospect(Prospect(
        first_name="Ira", last_name="Bell", email="ira@bell.io",
        email_status="verified", company="Bell", title="Owner", status="queued",
    ))
    camp_id = await state.add_campaign(Campaign(
        id="", name="restricted-camp", channel="email",
        sequence=[EmailStep(step=1, subject="hi", body="Hi Ira", delay_days=0)],
        prospect_ids=[pid], status="draft",
    ))
    await store.set_campaign_pool(camp_id, ["mbx_extra"])

    single_inboxes = await load_inboxes(store, cfg, EnvConfig(), main_prov, secrets=MagicMock(get=lambda k: "pw"))
    assert single_inboxes.several is True
    assert single_inboxes.eligible(camp_id) == []

    # Add extra inbox and verify personalized Step 1 (already on MAIN) moves to mbx_extra
    # even when follow_ups=False (new_rows == 0) AND decrements MAIN.queued_first!
    await store.add_mailbox(
        mailbox_id="mbx_extra", address="extra@agency.example", provider="smtp",
        connection="smtp", settings={"host": "smtp.example.com", "username": "extra@agency.example"},
    )
    # Writer already staged personalized Step 1 on MAIN ('')
    await state.add_outbox_item(
        prospect_id=pid, campaign_id=camp_id, step=1, to_email="ira@bell.io",
        subject="personal", body="Personal body", send_at="2026-11-18T12:00:00",
        status="approved", provider="smtp",
    )

    sender = Sender(SimpleNamespace(), state, cfg, EnvConfig())
    sender.provider = main_prov
    sender.secrets = MagicMock(get=lambda k: "pw", available=lambda: True)
    sender.inboxes = await sender._load_inboxes()
    assert sender.inboxes.get("").queued_first == 1

    campaign_obj = (await state.get_campaigns_by_status("draft"))[0]
    await sender._stage_campaign_native(campaign_obj)

    assert await store.thread_mailbox(camp_id, pid) == "mbx_extra"
    assert sender.inboxes.get("").queued_first == 0
    assert sender.inboxes.get("mbx_extra").queued_first == 1


@pytest.mark.asyncio
async def test_microsoft_stored_tenant_and_email_finder_ttl_and_send_window_and_analyst(state, monkeypatch):
    # 1. Microsoft _token honors stored["tenant"]
    seen_urls: list[str] = []
    ms_transport = httpx.MockTransport(
        lambda req: (seen_urls.append(str(req.url)), httpx.Response(200, json={"access_token": "tok_ok", "expires_in": 3600}))[1]
    )
    tokens = SimpleNamespace(
        load=lambda: {"refresh_token": "rt_1", "tenant": "contoso-tenant-id"},
        save=lambda d: None,
    )
    ms = MicrosoftProvider(load_config(), SimpleNamespace(m365_client_id="cid", m365_tenant_id="default-tenant"), transport=ms_transport, tokens=tokens)
    tok = await ms._token()
    assert tok == "tok_ok"
    assert "contoso-tenant-id" in seen_urls[0]

    # 2. email_finder: env.hunter_api_key + 5-min negative MX cache TTL
    assert EF._resolve_hunter_key(SimpleNamespace(hunter_api_key="hunter_from_env_obj")) == "hunter_from_env_obj"
    EF._mx_cache["transient-dns.example"] = None
    EF._mx_negative_ts["transient-dns.example"] = time.monotonic() - 301.0  # expired (> 5 min)
    monkeypatch.setattr(
        EF.dns.resolver,
        "resolve",
        lambda dom, rtype: [SimpleNamespace(preference=10, exchange="aspmx.l.google.com.")],
    )
    assert await EF.get_mx_host("transient-dns.example") == "aspmx.l.google.com"

    # 3. send_window disambiguation: Portland ME vs OR, London UK vs ON (with and without commas)
    assert zone_for("Portland, ME") == "America/New_York"
    assert zone_for("Portland, OR") == "America/Los_Angeles"
    assert zone_for("Portland Maine") == "America/New_York"
    assert zone_for("Portland Oregon") == "America/Los_Angeles"
    assert zone_for("London, UK") == "Europe/London"
    assert zone_for("London UK") == "Europe/London"
    assert zone_for("London, ON") == "America/Toronto"
    assert zone_for("London Ontario") == "America/Toronto"

    # 4. Analyst: post-send total_contacted, deduped opt-outs, provider-gated hint, AI rec
    ai_brain = SimpleNamespace(think=AsyncMock(return_value="Double down on HVAC leads in Colorado."))
    cfg = load_config()
    cfg.channels.email.provider = "gmail"
    # AI insights are opt-in (they spend a model call per idle cycle).
    cfg.analyst.ai_insights = True
    analyst = Analyst(state, brain=ai_brain, config=cfg)

    insights_blocked = analyst._generate_insights({
        "pipeline": {"new": 20, "queued": 5, "contacted": 0},
        "intents": {},
        "campaigns": [],
        "stages": {},
    })
    assert any("Outbox tab" in i and "Instantly" not in i for i in insights_blocked)

    # 20 total post-send (5 contacted + 14 replied + 1 opted_out): opt-out is 1/20 = 5% (NOT 2/5 = 40%!)
    insights_rates = analyst._generate_insights({
        "pipeline": {"new": 15, "queued": 0, "contacted": 5, "replied": 14, "opted_out": 1},
        "intents": {"interested": 10, "unsubscribe": 1},
        "campaigns": [],
        "stages": {},
    })
    assert any("KEEP: Reply rate is strong (70.0%)" in i for i in insights_rates)
    assert any("Opt-out rate is 5.0%" in i for i in insights_rates)

    await state.add_prospect(Prospect(first_name="A", last_name="B", email="a@b.io", company="B", title="CEO", status="replied"))
    report = await analyst.run()
    assert report.get("ai_recommendation") == "Double down on HVAC leads in Colorado."
