"""Regression tests for ReAct Brain tool execution, Agentic Copilot,
closed-loop Writer learning, grounded Handler replies, Researcher subpage probing,
Router transient cooldowns, and Dashboard insights/copilot endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from harvey_brain.router import RouterTransport
from sellbird.agents.copilot_agent import SellbirdCopilotAgent
from sellbird.agents.handler import Handler
from sellbird.agents.researcher import DeepResearcher
from sellbird.agents.writer import Writer
from sellbird.brain import AgentTool, Brain
from sellbird.config import EnvConfig, SellbirdConfig
from sellbird.integrations.copilot import TelegramCopilot
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager


def _cfg() -> SellbirdConfig:
    return SellbirdConfig(
        persona={
            "name": "Alex",
            "company": "VeloReach",
            "role": "Founder",
            "email": "alex@veloreach.test",
            "linkedin": "",
            "tone": "direct",
        },
        product={
            "name": "VeloReach",
            "description": "Outbound pipeline automation",
            "pricing": "$499/mo",
            "key_benefits": ["book more demos"],
            "objection_responses": {},
        },
        icp={
            "industries": ["HVAC"],
            "company_size": "10-50",
            "titles": ["Owner"],
            "geography": ["US"],
        },
        copilot={"telegram_bot_token": "tok", "telegram_chat_id": "100100100"},
    )


@pytest.mark.asyncio
async def test_brain_run_agent_executes_tools_and_handles_unknown_tools(tmp_path):
    state = StateManager(str(tmp_path / "brain.db"))
    await state.init_db()
    brain = Brain(state, _cfg().brain)
    brain.think_json = AsyncMock(
        side_effect=[
            {"thought": "try missing tool", "action": "missing_tool", "arguments": {}},
            {"thought": "fetch count", "action": "count_leads", "arguments": {"status": "new"}},
            {"thought": "done", "action": "final", "answer": "There are 14 new leads."},
        ]
    )

    async def _count_leads(status: str = "new") -> dict:
        return {"status": status, "count": 14}

    tools = [
        AgentTool(
            name="count_leads",
            description="Count leads by status",
            parameters={"type": "object", "properties": {"status": {"type": "string"}}},
            handler=_count_leads,
        )
    ]
    res = await brain.run_agent("How many new leads?", tools, max_steps=5)
    assert res["ok"] is True
    assert res["tool_calls"] == 1
    assert "14 new leads" in res["answer"]
    assert len(res["steps"]) == 3


@pytest.mark.asyncio
async def test_copilot_agent_reads_state_and_telegram_answers_natural_language(tmp_path):
    state = StateManager(str(tmp_path / "cop.db"))
    await state.init_db()
    cid = await state.add_company(
        Company(name="LoneStar HVAC", domain="lonestarhvac.example", tech_stack=["ServiceTitan"])
    )
    await state.add_prospect(
        Prospect(
            first_name="Sam",
            last_name="hill",
            email="sam@lonestarhvac.example",
            email_status="verified",
            company="LoneStar HVAC",
            company_id=cid,
            title="Owner",
            score=92,
            status="new",
        )
    )

    brain = Brain(state, _cfg().brain)
    brain.think_json = AsyncMock(
        side_effect=[
            {"thought": "check summary", "action": "pipeline_summary", "arguments": {}},
            {"thought": "inspect co", "action": "inspect_company", "arguments": {"domain": "lonestarhvac.example"}},
            {"thought": "done", "action": "final", "answer": "LoneStar HVAC uses ServiceTitan and Sam scored 92."},
        ]
    )

    copilot_agent = SellbirdCopilotAgent(brain, state, _cfg(), EnvConfig())
    ans = await copilot_agent.ask("Tell me about LoneStar HVAC")
    assert ans["ok"] is True
    assert ans["tool_calls"] == 2
    assert "ServiceTitan" in ans["answer"]

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        if method == "getUpdates":
            if getattr(handler, "done", False):
                return httpx.Response(200, json={"ok": True, "result": []})
            handler.done = True
            return httpx.Response(
                200,
                json={"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 100100100}, "text": "status?"}}]},
            )
        if method == "sendMessage":
            sent.append(payload.get("text", ""))
        return httpx.Response(200, json={"ok": True, "result": True})

    brain.think_json = AsyncMock(side_effect=[{"thought": "ok", "action": "final", "answer": "All systems green."}])
    tg = TelegramCopilot(state, _cfg(), transport=httpx.MockTransport(handler), brain=brain)
    handled = await tg.poll()
    assert handled == 1
    assert sent == ["All systems green."]


@pytest.mark.asyncio
async def test_writer_learning_context_and_priority_sorting(tmp_path):
    state = StateManager(str(tmp_path / "writer.db"))
    await state.init_db()
    (tmp_path / "analytics.json").write_text(
        json.dumps({
            "ai_recommendation": "Lead with ServiceTitan call routing pain in email 1.",
            "insights": ["ACTION: Reply rate improved with direct question CTAs."],
        }),
        encoding="utf-8",
    )

    cfg = _cfg()
    cfg.channels.email.provider = "smtp"

    await state.add_prospect(
        Prospect(
            first_name="Low",
            last_name="Risky",
            email="low@example.test",
            email_status="risky",
            company="Risky Co",
            title="Manager",
            industry="HVAC",
            score=40,
            status="new",
        )
    )
    await state.add_prospect(
        Prospect(
            first_name="Top",
            last_name="Verified",
            email="top@example.test",
            email_status="verified",
            company="Verified Co",
            title="Owner",
            industry="HVAC",
            score=95,
            status="new",
        )
    )

    seen_prompts: list[str] = []

    async def fake_think_json(prompt, **kwargs):
        seen_prompts.append(prompt)
        if kwargs.get("task") == "write_sequence":
            return [
                {"step": 1, "subject": "dispatch schedule", "body": "Hi {{first_name}}, noticed {{company}} runs commercial installs. Can I send a breakdown?", "delay_days": 0},
                {"step": 2, "subject": "field routing", "body": "{{company}} peers cut missed dispatch calls fast. Open to seeing how?", "delay_days": 3},
                {"step": 3, "subject": "last note", "body": "Should I stop sending notes here {{first_name}}?", "delay_days": 4},
            ]
        return {"subject": "hvac scheduling", "body": "Noticed you run HVAC in Austin. Can I send a short breakdown?"}

    fake_brain = SimpleNamespace(
        think_json=AsyncMock(side_effect=fake_think_json),
        think=AsyncMock(return_value=""),
        load_prompt=lambda *a, **k: "",
        load_skills_for_agent=lambda *a, **k: "",
        load_skill=lambda *a, **k: "",
        paused_reason="",
        last_reply_empty=False,
    )
    writer = Writer(fake_brain, state, cfg, EnvConfig())
    learn = await writer._learning_context()
    assert "ServiceTitan call routing" in learn

    await writer.run()
    assert any("CLOSED-LOOP LEARNING FROM PAST CAMPAIGNS" in p for p in seen_prompts)
    outbox = await state.get_outbox()
    assert any(item["to_email"] == "top@example.test" for item in outbox)
    assert outbox[0]["to_email"] == "top@example.test"


@pytest.mark.asyncio
async def test_handler_grounding_ooo_date_and_critic_reply_guard(tmp_path):
    state = StateManager(str(tmp_path / "handler.db"))
    await state.init_db()
    cid = await state.add_company(
        Company(
            name="CoolAir",
            domain="coolair.test",
            description="Commercial HVAC contractor in Austin",
            tech_stack=["Housecall Pro"],
        )
    )
    prospect = Prospect(
        first_name="Jamie",
        last_name="Fox",
        email="jamie@coolair.test",
        company="CoolAir",
        company_id=cid,
        title="Owner",
    )

    assert Handler._extract_ooo_return_date("Thanks, I am out of office returning on 2026-12-15.") == "2026-12-15"

    captured_prompts: list[str] = []

    async def fake_think(prompt, **kwargs):
        captured_prompts.append(prompt)
        if kwargs.get("task") == "classify_intent":
            return "interested"
        return "We work with Housecall Pro setups."

    handler = Handler(
        SimpleNamespace(
            think=AsyncMock(side_effect=fake_think),
            paused_reason="",
            load_prompt=lambda *a, **k: "",
            load_skill=lambda *a, **k: "",
        ),
        state,
        _cfg(),
        EnvConfig(),
    )
    intent = await handler._classify_intent("Thursday works", prospect, recent_thread="[Message 1] Can we chat Thu?")
    assert intent == "interested"
    assert "Can we chat Thu?" in captured_prompts[0]

    facts = await handler._company_grounding_facts(prospect)
    assert any("Housecall Pro" in f for f in facts)

    # Reply with fabricated funding/price triggers CopyCritic rewrite
    handler.brain.think = AsyncMock(return_value="We can connect with Housecall Pro whenever you are ready.")
    cleaned = await handler._keep_to_rules(
        "Congrats on your Series B round! Our custom plan saves $99,999.",
        prospect,
        evidence_facts=facts,
    )
    assert "Series B" not in cleaned
    assert "$99,999" not in cleaned


@pytest.mark.asyncio
async def test_researcher_probes_subpages_and_returns_opening_hook(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "res.db"))
    await state.init_db()

    fetched_urls: list[str] = []

    async def fake_validate(url):
        return url

    async def fake_fetch(url, **kwargs):
        fetched_urls.append(url)
        return SimpleNamespace(
            markdown=f"Content for {url}",
            final_url=url,
            published_date="",
        )

    import sellbird.agents.researcher as res_mod
    monkeypatch.setattr(res_mod, "validate_public_url", fake_validate)
    monkeypatch.setattr(res_mod, "fetch_page", fake_fetch)

    fake_brain = SimpleNamespace(
        think_json=AsyncMock(
            return_value={
                "summary": "Austin HVAC team",
                "icp_fit": "Strong fit",
                "opening_hook": "Noticed you are hiring technicians on your careers page.",
                "verified_facts": [{"fact": "Hiring HVAC techs", "evidence_url": "https://acme.test/careers"}],
                "decision_makers": [],
                "recent_triggers": [],
            }
        ),
        paused_reason="",
    )
    researcher = DeepResearcher(fake_brain, state, _cfg(), EnvConfig())
    researcher._search_web = AsyncMock(return_value=[])
    researcher._check_public_osint = AsyncMock(return_value=[])

    out = await researcher.research_company({"id": "", "name": "Acme HVAC", "domain": "acme.test", "website": "https://acme.test"})
    assert out["ok"] is True
    assert out["opening_hook"] == "Noticed you are hiring technicians on your careers page."
    assert any(u.endswith("/about") for u in fetched_urls)
    assert any(u.endswith("/careers") for u in fetched_urls)


@pytest.mark.asyncio
async def test_router_transient_cooldown_skips_flaky_primary():
    class Flaky:
        name = "primary"
        paused_reason = ""
        last_error = "429"
        calls = 0

        async def __call__(self, prompt, **kwargs):
            self.calls += 1
            return ""

    class Healthy:
        name = "backup"
        paused_reason = ""
        last_error = ""
        last_usage = None
        calls = 0

        async def __call__(self, prompt, **kwargs):
            self.calls += 1
            return "ok"

    primary, backup = Flaky(), Healthy()
    router = RouterTransport([primary, backup], transient_cooldown_seconds=60.0)

    assert await router("one") == "ok"
    assert await router("two") == "ok"
    assert primary.calls == 1
    assert backup.calls == 2


def test_dashboard_insights_and_copilot_routes(tmp_path, monkeypatch):
    import sellbird.dashboard as dash

    db_path = str(tmp_path / "dash.db")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "dash.db")
    dash.token_store.configure(tmp_path / "dashboard-token")
    token = dash.token_store.get()

    def _test_state():
        return StateManager(db_path)

    monkeypatch.setattr(dash, "_state", _test_state)
    monkeypatch.setattr(dash, "_load_active_sellbird_config", _cfg)

    fake_brain = Brain(_test_state(), _cfg().brain)
    fake_brain.think_json = AsyncMock(side_effect=[{"thought": "done", "action": "final", "answer": "Pipeline has 0 prospects."}])

    async def _fake_prepare():
        st = _test_state()
        await st.init_db()
        return (fake_brain, st, _cfg(), EnvConfig()), None

    monkeypatch.setattr(dash, "_prepare_agentic_runtime", _fake_prepare)
    monkeypatch.setattr(dash, "_close_brain", AsyncMock())

    with TestClient(
        dash.app,
        base_url="http://127.0.0.1:5555",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        ins = client.get("/api/agents/insights")
        assert ins.status_code == 200
        ins_body = ins.json()
        assert ins_body["ok"] is True
        assert "insights" in ins_body
        assert "framework_stats" in ins_body

        chat = client.post("/api/copilot/chat", json={"message": "How many prospects?"})
        assert chat.status_code == 200
        chat_body = chat.json()
        assert chat_body["ok"] is True
        assert "0 prospects" in chat_body["answer"]
