"""Focused tests for verified Brain, Agents, Collectors, and Integrations fixes."""

from __future__ import annotations

import asyncio
import email.message
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from harvey_brain.delegate import DelegateTransport
from harvey_brain.openai_compat import OpenAICompatTransport
from harvey_brain.profile import TaskProfile, resolve
from harvey_brain.router import RouterTransport
from harvey_brain.router_factory import _prepare_doctor_rows
from harvey_brain.spec import get_provider
from harvey_brain import probe

from sellbird.agents.critic import CopyCritic
from sellbird.agents.enricher import ClayEnricher
from sellbird.agents.handler import Handler
from sellbird.agents.researcher import DeepResearcher
from sellbird.agents.scout import Scout
from sellbird.agents.writer import Writer
from sellbird.brain import Brain
from sellbird.collectors.discover import Business, DiscoveryProvider, DiscoveryQuery, run_discovery
from sellbird.config import EnvConfig, load_config
from sellbird.export import collect_prospects
from sellbird.gate import MAX_SUBJECT_LEN
from sellbird.integrations import crm_sync, gmail, quota, smtp_mail, stealth_fetch
from sellbird.integrations.copilot import TelegramCopilot
from sellbird.integrations.mail_provider import InboundMessage
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.public_http import public_http_client
from sellbird.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    st = StateManager(db_path=tmp_path / "audit.db")
    await st.init_db()
    return st


# ── 1. harvey_brain/delegate.py ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delegate_isolated_cwd_timeout_reap_and_is_error(tmp_path, monkeypatch):
    from contextlib import contextmanager

    used_cwds: list[str] = []
    iso_dir = tmp_path / "iso"
    iso_dir.mkdir()

    @contextmanager
    def fake_isolated_cwd():
        yield str(iso_dir)

    class FakeRuntime:
        isolated_cwd = staticmethod(fake_isolated_cwd)

        @staticmethod
        def check_cli(env=None):
            return "claude"

        @staticmethod
        def build_command(exe, model="", session_id=None):
            return ["claude", "-p"]

        @staticmethod
        def runtime_env(cwd, base=None):
            used_cwds.append(cwd)
            return {"PWD": cwd}

    class FakeSellbird:
        claude_runtime = FakeRuntime
        __path__: list[str] = []

    monkeypatch.setitem(sys.modules, "sellbird", FakeSellbird)

    # 1a. is_error payload returns "" and populates last_error
    class ErrorProc:
        returncode = 0

        async def communicate(self, data):
            payload = json.dumps({"type": "result", "is_error": True, "result": "Overloaded error"}).encode()
            return payload, b""

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=ErrorProc()))
    t = DelegateTransport("claude_cli")
    assert await t("hello") == ""
    assert "Overloaded error" in t.last_error
    assert used_cwds == [str(iso_dir)]

    # 1b. Timeout kills and reaps with wait_for(..., timeout=5.0)
    killed = []
    waited = []

    class HangingProc:
        returncode = None

        async def communicate(self, data):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed.append(True)
            self.returncode = -9

        async def wait(self):
            waited.append(True)
            return -9

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=HangingProc()))
    t2 = DelegateTransport("claude_cli", timeout=0.05)
    assert await t2("hello", timeout=0.05) == ""
    assert killed == [True]
    assert waited == [True]
    assert t2.last_error == "timeout"


# ── 2. harvey_brain/profile.py & router.py ───────────────────────────────────


@pytest.mark.asyncio
async def test_profile_resolve_and_router_skip_paused_transports():
    class StubTransport:
        def __init__(self, name, model, *, paused_reason="", text="ok"):
            self.name = name
            self.model = model
            self.spec = get_provider(name)
            self.paused_reason = paused_reason
            self.text = text
            self.calls = 0
            self.last_error = ""
            self.last_usage = None

        async def __call__(self, prompt, **kwargs):
            self.calls += 1
            return self.text

    paused_exact = StubTransport("openrouter", "m-fast", paused_reason="out of credits", text="bad")
    live_exact = StubTransport("openrouter", "m-fast", paused_reason="", text="good")
    prof = TaskProfile(provider="openrouter", model="m-fast")

    # Pass 1 skips paused exact match and picks unpaused exact match
    picked = resolve(prof, [paused_exact, live_exact])
    assert picked is live_exact

    # Router._route returns None if only matching transport is paused, never calling it
    only_paused = StubTransport("openrouter", "m-fast", paused_reason="bad key", text="bad")
    backup = StubTransport("xai", "grok-4.7", paused_reason="", text="from-backup")
    router = RouterTransport([only_paused, backup], profiles={"scout": prof})
    assert router._route("scout", "score_contacts") is None
    assert await router("hi", agent="scout", task="score_contacts") == "from-backup"
    assert only_paused.calls == 0
    assert backup.calls == 1


# ── 3. harvey_brain/openai_compat.py ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_compat_200_error_and_retry_after_deadline():
    spec = get_provider("openrouter")

    # 3a. HTTP 200 with retryable error retries then succeeds
    attempts = []

    def retry_handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(200, json={"error": {"message": "Rate limit", "code": 429}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    c1 = httpx.AsyncClient(transport=httpx.MockTransport(retry_handler))
    t1 = OpenAICompatTransport(spec, "k", http=c1, max_retries=1, retry_base_delay=0.0)
    assert await t1("hi") == "ok"
    assert len(attempts) == 2

    # 3b. HTTP 200 with quota error pauses and sets last_error
    c2 = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"error": {"message": "Insufficient credits", "code": 402}})
        )
    )
    t2 = OpenAICompatTransport(spec, "k", http=c2, max_retries=2, retry_base_delay=0.0)
    assert await t2("hi") == ""
    assert "credits" in t2.paused_reason.lower()
    assert "402" in t2.last_error

    # 3c. Large Retry-After fails fast instead of sleeping past deadline
    calls3 = []

    def ra_handler(request: httpx.Request) -> httpx.Response:
        calls3.append(1)
        return httpx.Response(429, headers={"Retry-After": "30"}, text="slow down")

    c3 = httpx.AsyncClient(transport=httpx.MockTransport(ra_handler))
    t3 = OpenAICompatTransport(spec, "k", http=c3, max_retries=2, retry_base_delay=0.0)
    started = time.monotonic()
    assert await t3("hi", timeout=0.2) == ""
    assert time.monotonic() - started < 0.19
    assert len(calls3) == 1


# ── 4. harvey_brain/probe.py & router_factory.py ─────────────────────────────


@pytest.mark.asyncio
async def test_probe_closes_transports_and_doctor_dedups_by_provider_model(tmp_path, monkeypatch):
    closed = []

    class DummyTransport:
        def __init__(self, name):
            self.name = name
            self.spec = get_provider("ollama")
            self.model = "llama3.2"
            self.api_key = ""
            self.last_usage = None
            self.last_error = ""
            self.paused_reason = ""

        async def __call__(self, *a, **kw):
            return "ok"

        async def aclose(self):
            closed.append(self.name)

    await probe.live([DummyTransport("ollama")], confirm=True)
    assert closed == ["ollama"]

    # doctor deduplicates by (provider, model), keeping distinct models on same provider
    cfg = tmp_path / "brain.yaml"
    cfg.write_text(
        "chain:\n"
        "  - provider: openrouter\n    model: m-one\n"
        "  - provider: openrouter\n    model: m-two\n"
        "  - provider: openrouter\n    model: m-one\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    rows, _ = _prepare_doctor_rows(cfg, load_env=False)
    models = [(r["name"], r["model"]) for r, _, _ in rows]
    assert models == [("openrouter", "m-one"), ("openrouter", "m-two")]


def test_probe_main_closes_chain_in_dry_run(tmp_path, monkeypatch):
    cfg = tmp_path / "brain.yaml"
    cfg.write_text("chain:\n  - provider: ollama\n", encoding="utf-8")
    closed = []

    class DummyT:
        name = "ollama"
        spec = get_provider("ollama")
        model = "llama3.2"
        api_key = ""

        async def aclose(self):
            closed.append("closed")

    import harvey_brain.router_factory as rf
    monkeypatch.setattr(rf, "build_chain", lambda settings: [DummyT()])
    assert probe.main(["--config", str(cfg), "--json"]) == 0
    assert closed == ["closed"]


# ── 5. sellbird/brain.py ─────────────────────────────────────────────────────


def test_brain_extract_json_fences_in_prose_and_raw_decode():
    prose_fence = 'Sure! Here is the JSON:\n```json\n{"ok": true, "n": 2}\n```\nHope that helps {ignore}.'
    assert Brain._extract_json(prose_fence) == {"ok": True, "n": 2}

    trailing_braces = 'Prefix {not json} then {"a": [1, 2]} and trailing {noise}.'
    assert Brain._extract_json(trailing_braces) == {"a": [1, 2]}


@pytest.mark.asyncio
async def test_brain_think_timeout_reaps_killed_process(state, monkeypatch, tmp_path):
    from sellbird import claude_runtime

    monkeypatch.setattr(claude_runtime, "check_cli", lambda: "claude")
    monkeypatch.setattr(claude_runtime, "build_command", lambda exe, model, sid: ["claude", "-p"])
    monkeypatch.setattr(claude_runtime, "runtime_env", lambda cwd: {})

    waits = []
    kills = []

    class SlowProc:
        returncode = None

        async def communicate(self, data):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            kills.append(True)
            self.returncode = -9

        async def wait(self):
            waits.append(True)
            return -9

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=SlowProc()))
    brain = Brain(state)
    res = await brain.think("hi", timeout=0.05, max_retries=0)
    assert res == ""
    assert kills == [True]
    assert waits == [True]


# ── 6. Handler, Export, Discover, Scout, Writer suppressions & fixes ─────────


@pytest.mark.asyncio
async def test_handler_retry_on_exception_latest_outbox_bounce_own_and_suppression(state, monkeypatch):
    cfg = load_config()
    cfg.channels.email.provider = "smtp"
    cfg.persona.email = "main@myagency.com"

    # 6a. _read_inbox sets consumed=False on Exception so message is NOT marked processed
    provider = SimpleNamespace(
        name="smtp",
        identity=SimpleNamespace(address="extra@myagency.com"),
        confirm_read=MagicMock(),
        get_replies=AsyncMock(return_value=[
            InboundMessage(provider_id="msg-err-1", from_email="bob@co.io", subject="Re: hi", body="hello")
        ]),
    )
    brain = SimpleNamespace(
        load_skills_for_agent=lambda _: "",
        load_prompt=lambda *a, **k: "",
        think=AsyncMock(side_effect=RuntimeError("boom")),
    )
    handler = Handler(brain, state, cfg, EnvConfig())
    handler.provider = provider
    await state.add_prospect(Prospect(
        first_name="Bob", last_name="Lee", email="bob@co.io",
        email_status="verified", company="Co", title="CEO", status="contacted",
    ))
    complete = await handler._read_inbox(provider, "mb1", label="extra@myagency.com")
    assert complete is False
    assert await state.is_reply_processed("msg-err-1") is False

    # 6b. _match_outbox_for_inbound returns latest sent row even past 200 older rows
    c_old = await state.add_campaign(Campaign(
        id="", name="old-c", channel="email",
        sequence=[EmailStep(step=1, subject="s", body="b", delay_days=0)],
        prospect_ids=[], status="active",
    ))
    c_new = await state.add_campaign(Campaign(
        id="", name="new-c", channel="email",
        sequence=[EmailStep(step=1, subject="s", body="b", delay_days=0)],
        prospect_ids=[], status="active",
    ))
    for i in range(205):
        oid = await state.add_outbox_item(
            prospect_id=f"p-{i}", campaign_id=c_old, step=1, to_email=f"u{i}@x.io",
            subject="s", body="b", send_at=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
            status="sent", provider="smtp",
        )
        await state.update_outbox_item(oid, sent_at=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}")
    oid_target_old = await state.add_outbox_item(
        prospect_id="p-target", campaign_id=c_old, step=1, to_email="bob@co.io",
        subject="s", body="b", send_at="2026-02-01T10:00:00", status="sent", provider="smtp",
    )
    await state.update_outbox_item(oid_target_old, sent_at="2026-02-01T10:00:00")
    oid_target_new = await state.add_outbox_item(
        prospect_id="p-target", campaign_id=c_new, step=1, to_email="bob@co.io",
        subject="s", body="b", send_at="2026-03-01T10:00:00", status="sent", provider="smtp",
    )
    await state.update_outbox_item(oid_target_new, sent_at="2026-03-01T10:00:00")

    matched = await handler._match_outbox_for_inbound(
        InboundMessage(provider_id="m", from_email="bob@co.io", subject="Re: s", body="hi"),
        fallback_recipient=True,
    )
    assert matched is not None and matched["campaign_id"] == c_new

    # 6c. _handle_bounce ignores active inbox + configured mailboxes
    from sellbird.mailboxes import MailboxStore
    mb_store = MailboxStore(db_path=state.db_path)
    await mb_store.add_mailbox(
        mailbox_id="mb-alias", provider="smtp", connection="smtp",
        address="alias@myagency.com", display_name="Alias",
    )
    own_prospect_id = await state.add_prospect(Prospect(
        first_name="Own", last_name="Box", email="alias@myagency.com",
        email_status="verified", company="MyAgency", title="Owner", status="contacted",
    ))
    bounce_msg = InboundMessage(
        provider_id="b-own",
        from_email="mailer-daemon@mx.example",
        subject="Delivery failure",
        body="Original-Recipient: rfc822; alias@myagency.com\nFinal-Recipient: rfc822; unknown@nowhere.invalid",
        is_bounce=True,
    )
    await handler._handle_bounce(bounce_msg, own_emails={"extra@myagency.com"})
    assert (await state.get_prospect(own_prospect_id)).email_status == "verified"

    # 6d. Suppression enforcement in Handler._process_reply (Instantly path never calls LLM or send_reply)
    sup_pid = await state.add_prospect(Prospect(
        first_name="Sup", last_name="User", email="sup@blocked.io",
        email_status="verified", company="Blocked", title="CEO", status="contacted",
    ))
    await state.add_suppression("domain", "blocked.io", "manual")
    instantly_mock = SimpleNamespace(send_reply=AsyncMock())
    brain_clean = SimpleNamespace(
        load_skills_for_agent=lambda _: "",
        load_prompt=lambda *a, **k: "",
        think=AsyncMock(return_value="interested"),
    )
    h_inst = Handler(brain_clean, state, load_config(), EnvConfig())
    h_inst.instantly = instantly_mock
    await h_inst._process_reply("sup@blocked.io", "Sure, tell me more", "uuid-1", c_new)
    brain_clean.think.assert_not_called()
    instantly_mock.send_reply.assert_not_called()
    convos = await state.get_conversations_by_status("needs_human")
    assert any(c.prospect_id == sup_pid for c in convos)


@pytest.mark.asyncio
async def test_suppression_enforcement_export_discover_scout_writer(state):
    # Export filters DB-suppressed prospects when include_all=False
    await state.add_prospect(Prospect(
        first_name="Keep", last_name="One", email="keep@good.io",
        email_status="verified", company="Good", title="CEO", status="new",
    ))
    await state.add_prospect(Prospect(
        first_name="Drop", last_name="Two", email="drop@bad.io",
        email_status="verified", company="Bad", title="CEO", status="new",
    ))
    await state.add_suppression("domain", "bad.io", "manual")

    exported = await collect_prospects(state, include_all=False)
    assert [r["email"] for r in exported] == ["keep@good.io"]
    exported_all = await collect_prospects(state, include_all=True)
    assert {r["email"] for r in exported_all} == {"keep@good.io", "drop@bad.io"}

    # Discover skips DB-suppressed domains before upsert
    from sellbird.collectors import discover as disc_mod

    class FakeProv(DiscoveryProvider):
        key = "fake_disc"
        label = "Fake"
        kind = "serp"

        async def fetch(self, client, env, query):
            return [
                Business(name="Bad Co", domain="bad.io", website="https://bad.io", provider="fake_disc"),
                Business(name="Good Co", domain="good.io", website="https://good.io", provider="fake_disc"),
            ]

    disc_mod.PROVIDERS["fake_disc"] = FakeProv()
    try:
        from sellbird.signals import seed_signal_catalog
        await seed_signal_catalog(state)
        await state.set_signal_status("FOUND_IN_SERP", "confirmed")
        report = await run_discovery(
            state, load_config(), provider_key="fake_disc",
            queries=[DiscoveryQuery(term="saas", location="Austin, TX")],
        )
        assert report.excluded == 1
        assert await state.get_company_by_domain("bad.io") is None
        assert await state.get_company_by_domain("good.io") is not None
    finally:
        disc_mod.PROVIDERS.pop("fake_disc", None)

    # Writer skips DB-suppressed prospects pre-LLM
    fake_brain = SimpleNamespace(
        load_skills_for_agent=lambda _: "",
        load_skill=lambda _: "",
        load_prompt=lambda *a, **k: "",
        think_json=AsyncMock(return_value=[]),
    )
    # Mark keep@good.io as already queued so only suppressed drop@bad.io is 'new'
    p_keep = await state.get_prospect_by_email("keep@good.io")
    await state.update_prospect_status(p_keep.id, "queued")
    writer = Writer(fake_brain, state, load_config(), EnvConfig())
    await writer.run()
    fake_brain.think_json.assert_not_called()

    # Scout skips DB-suppressed domain pre-LLM
    scout = Scout(fake_brain, state, load_config(), EnvConfig())
    assert await scout._is_domain_suppressed("bad.io") == "manual"
    assert await scout._is_domain_suppressed("good.io") == ""


# ── 7. sellbird/agents/researcher.py ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_researcher_seeds_catalog_and_supports_domain_run(state, monkeypatch):
    import sellbird.agents.researcher as res_mod

    monkeypatch.setattr(
        DeepResearcher, "_search_web",
        AsyncMock(return_value=[{"url": "https://acme.io/about", "title": "About", "snippet": "Founded 2020"}]),
    )
    monkeypatch.setattr(DeepResearcher, "_check_public_osint", AsyncMock(return_value=[]))
    monkeypatch.setattr(res_mod, "validate_public_url", AsyncMock(return_value=None))
    monkeypatch.setattr(
        res_mod, "fetch_page",
        AsyncMock(return_value=SimpleNamespace(final_url="https://acme.io/about", markdown="Acme builds widgets.", published_date="")),
    )

    cid = await state.add_company(Company(name="Acme", domain="acme.io", website="https://acme.io"))
    brain = SimpleNamespace(
        think_json=AsyncMock(return_value={
            "summary": "Acme builds widgets.",
            "icp_fit": "Good fit.",
            "verified_facts": [{"fact": "Builds widgets", "evidence_url": "https://acme.io/about"}],
            "decision_makers": [],
            "recent_triggers": [],
        })
    )
    researcher = DeepResearcher(state, brain)
    # Note: signal catalog was NOT manually seeded beforehand!
    reports = await researcher.run(domain="acme.io")
    assert len(reports) == 1
    assert reports[0]["ok"] is True
    assert reports[0]["company_id"] == cid
    assert reports[0]["observations_written"] == 1

    reports_by_id = await researcher.run(company_id=cid)
    assert len(reports_by_id) == 1 and reports_by_id[0]["ok"] is True


# ── 8. sellbird/agents/enricher.py ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_enricher_filters_columns_companies_and_clears_unverified_source_url():
    saved: list[dict] = []

    class MockState:
        async def get_enrichment_columns(self):
            return [
                {"id": "c_active", "name": "Size", "prompt": "Size", "enabled": True, "entity_type": "company"},
                {"id": "c_disabled", "name": "Off", "prompt": "Off", "enabled": False, "entity_type": "company"},
                {"id": "c_prospect", "name": "Role", "prompt": "Role", "enabled": True, "entity_type": "prospect"},
            ]

        async def list_companies(self, limit=10):
            return [
                {"id": "co1", "name": "Co1", "domain": "", "website": ""},
                {"id": "co2", "name": "Co2", "domain": "co2.io", "website": "https://co2.io"},
            ]

        async def set_enrichment_value(self, **kw):
            saved.append(kw)

    brain = SimpleNamespace(
        think_json=AsyncMock(return_value={"value": "50", "source_url": "https://hallucinated.example/about"})
    )
    enricher = ClayEnricher(MockState(), brain)
    written = await enricher.run(column_id="c_active", company_ids=["co1"])
    assert written == 1
    assert len(saved) == 1
    assert saved[0]["company_id"] == "co1"
    assert saved[0]["column_id"] == "c_active"
    # known_urls was empty for co1 (no website/obs/web), so hallucinated source_url MUST be cleared
    assert saved[0]["source_url"] == ""


# ── 9. Scout Hunter mark_prospects_checked & Writer/Critic subject cap ──────


@pytest.mark.asyncio
async def test_hunter_only_marks_checked_when_saved_and_subject_capped(state, monkeypatch):
    import sellbird.integrations.email_finder as ef_mod

    cid = await state.add_company(Company(name="EmptyCo", domain="emptyco.io", website="https://emptyco.io"))
    monkeypatch.setattr(ef_mod, "hunter_searches_remaining", AsyncMock(return_value=5))
    monkeypatch.setattr(ef_mod, "record_hunter_search", AsyncMock())
    # When Hunter API errors (returns None), company must not be cooled down for 14 days
    monkeypatch.setattr(ef_mod, "hunter_domain_people", AsyncMock(return_value=None))

    cfg = load_config()
    env = EnvConfig(hunter_api_key="hk-test")
    brain = SimpleNamespace(load_skills_for_agent=lambda _: "")
    scout = Scout(brain, state, cfg, env)
    saved_count = await scout._prospect_via_hunter()
    assert saved_count == 0
    # Must still need prospects when Hunter call failed (people is None)
    needing = await state.companies_needing_prospects(limit=10)
    assert any(c["id"] == cid for c in needing)

    # Subject cap at MAX_SUBJECT_LEN (90) in Writer and CopyCritic
    long_subject = "a" * 115
    critic_brain = SimpleNamespace(
        think_json=AsyncMock(return_value={"subject": long_subject, "body": "Hi Sam,\n\nShort note. Free to chat?"})
    )
    critic = CopyCritic(critic_brain)
    res = await critic.review_and_refine({"subject": "", "body": "bad"})
    assert len(res["subject"]) <= MAX_SUBJECT_LEN

    writer_brain = SimpleNamespace(
        load_skills_for_agent=lambda _: "",
        load_skill=lambda _: "",
        load_prompt=lambda *a, **k: "",
        think_json=AsyncMock(return_value={"subject": long_subject, "body": "Short grounded note. Open to chat?"}),
    )
    writer = Writer(writer_brain, state, cfg, env)
    personal = await writer._write_personal_email(
        Prospect(first_name="Sam", last_name="Lee", email="sam@acme.io", company="Acme", title="CEO")
    )
    assert personal is not None
    assert len(personal["subject"]) <= MAX_SUBJECT_LEN


# ── 10. Integrations & public_http ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_public_http_max_redirects_copilot_atomic_edit_and_integrations(state, monkeypatch):
    # 10a. public_http_client accepts max_redirects
    async with public_http_client(max_redirects=3) as client:
        assert client.max_redirects == 3

    # 10b. stealth_fetch._check_ip_string rejects private and accepts public without dead check
    stealth_fetch._check_ip_string("8.8.8.8")
    with pytest.raises(ValueError):
        stealth_fetch._check_ip_string("127.0.0.1")

    # 10c. copilot._edit uses atomic edit_outbox_draft
    pid = await state.add_prospect(Prospect(
        first_name="A", last_name="B", email="a@b.io", company="B", title="CEO",
    ))
    oid = await state.add_outbox_item(
        prospect_id=pid, to_email="a@b.io", subject="Orig Subj", body="Orig Body",
        send_at="2026-01-01T00:00:00", status="pending_review",
    )
    copilot = TelegramCopilot(state, load_config(), EnvConfig())
    copilot._send = AsyncMock(return_value=True)
    note = await copilot._edit(f"/edit {oid} Updated body text")
    assert note.startswith("Edited.")
    updated = await state.get_outbox_item(oid)
    assert updated["body"] == "Updated body text"
    assert updated["subject"] == "Orig Subj"

    # Race check: if status changed concurrently before edit_outbox_draft executes, edit is refused
    await state.update_outbox_item(oid, status="approved")
    orig_get = state.get_outbox_item
    state.get_outbox_item = AsyncMock(side_effect=[
        {"id": oid, "status": "pending_review", "subject": "Orig Subj"},
        {"id": oid, "status": "approved", "subject": "Orig Subj"},
    ])
    note_race = await copilot._edit(f"/edit {oid} Sneaky overwrite")
    state.get_outbox_item = orig_get
    assert "Can't edit" in note_race
    assert (await state.get_outbox_item(oid))["body"] == "Updated body text"

    # 10d. smtp_mail passes timeout=30, adds Date/UID to fallback provider_id, raises on >limit UNSEEN
    import imaplib as imap_mod
    seen_timeouts = []

    def _raw_no_msgid(sender, subj, date_str):
        m = email.message.EmailMessage()
        m["From"] = sender
        m["To"] = "me@x.com"
        m["Subject"] = subj
        m["Date"] = date_str
        m.set_content("hello")
        return m.as_bytes()

    class MockIMAP:
        def __init__(self, host, port, timeout=None):
            seen_timeouts.append(timeout)

        def login(self, u, p):
            return "OK", []

        def select(self, box):
            return "OK", [b"2"]

        def search(self, charset, crit):
            return "OK", [b"10 11"]

        def fetch(self, mid, spec):
            return "OK", [(b"1", _raw_no_msgid("a@b.io", "Same", "Mon, 1 Jan 2026 10:00:00 +0000" if mid == b"10" else "Tue, 2 Jan 2026 10:00:00 +0000"))]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr(imap_mod, "IMAP4_SSL", MockIMAP)
    prov = smtp_mail.SmtpImapProvider(
        load_config(),
        EnvConfig(),
        smtp=smtp_mail.SmtpSettings(host="smtp.x.com", username="me@x.com", password="pw"),
    )
    replies = await prov.get_replies(limit=5)
    assert seen_timeouts == [30]
    assert len(replies) == 2
    assert replies[0].provider_id != replies[1].provider_id
    assert "10" in replies[0].provider_id and "11" in replies[1].provider_id

    with pytest.raises(RuntimeError, match="partial read"):
        await prov.get_replies(limit=1)

    # 10e. crm_sync.fetch_do_not_contact breaks immediately when pageInfo.hasNextPage is False
    calls = []

    def twenty_handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url)
        return httpx.Response(200, json={
            "data": {
                "people": [{"id": "p1", "emails": {"primaryEmail": "dnc@co.io"}}],
                "pageInfo": {"hasNextPage": False},
            }
        })

    sync = crm_sync.TwentyInboundSync(
        state, load_config(), EnvConfig(twenty_api_key="tw-key"),
        transport=httpx.MockTransport(twenty_handler),
    )
    found = await sync.fetch_do_not_contact()
    assert found == ["dnc@co.io"]
    assert len(calls) == 1

    # 10f. gmail.run_auth_flow binds IPv4+IPv6 with allow_reuse_address and calls server_close()
    assert gmail._LoopbackServer.allow_reuse_address is True
    created_servers = []

    class FakeServer:
        def __init__(self, addr, handler_cls):
            self.addr = addr
            self.closed = False
            self.shut = False
            created_servers.append(self)

        def serve_forever(self):
            pass

        def shutdown(self):
            self.shut = True

        def server_close(self):
            self.closed = True

    monkeypatch.setattr(gmail, "_LoopbackServer", FakeServer)
    monkeypatch.setattr(gmail, "_LoopbackServerV6", FakeServer)
    monkeypatch.setattr(gmail.webbrowser, "open", lambda url: True)
    import threading
    orig_event = threading.Event

    class ImmediateTimeoutEvent(orig_event):
        def wait(self, timeout=None):
            if timeout == 300:
                return False
            return super().wait(timeout=timeout)

    monkeypatch.setattr(threading, "Event", ImmediateTimeoutEvent)
    try:
        assert gmail.run_auth_flow("cid", "csec") is False
    finally:
        monkeypatch.setattr(threading, "Event", orig_event)
    assert len(created_servers) == 2
    assert all(s.shut and s.closed for s in created_servers)

    # 10g. quota.QuotaClient._fetch uses get_running_loop cleanly
    monkeypatch.setattr(quota, "get_oauth_token", lambda: "")
    qc = quota.QuotaClient()
    assert await qc._fetch() is None
