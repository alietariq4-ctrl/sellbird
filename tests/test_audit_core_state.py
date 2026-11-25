"""Focused audit regression tests for core state, models, mailboxes, sender, main, cli, config, and logsafety."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import yaml
from pydantic import ValidationError

from sellbird import cli
from sellbird import config as config_mod
from sellbird import main as main_mod
from sellbird.agents import sender as sender_mod
from sellbird.config import (
    BrainConfig,
    ConfigError,
    ConfigFileNotFoundError,
    EnvConfig,
    ICPConfig,
    PersonaConfig,
    ProductConfig,
    QuietHoursConfig,
    SellbirdConfig,
    load_brain_config,
    load_config,
    load_env,
    set_require_approval,
    use_config,
)
from sellbird.logsafety import RedactSecrets
from sellbird.mailboxes import MailboxStore
from sellbird.models.company import Company
from sellbird.models.conversation import Conversation, Message
from sellbird.models.prospect import Prospect
from sellbird.state import MIGRATIONS, StateManager
from sellbird.workspace_home import Home, use_home


def _minimal_config_dict(**overrides) -> dict:
    data = {
        "persona": {
            "name": "Ada",
            "company": "Acme Corp",
            "role": "Founder",
            "email": "ada@acme.example",
            "linkedin": "",
            "tone": "direct",
        },
        "product": {
            "name": "Widget",
            "description": "Helps teams",
            "pricing": "$99/mo",
            "key_benefits": ["Fast"],
            "objection_responses": {},
        },
        "icp": {
            "industries": ["SaaS"],
            "company_size": "10-50",
            "titles": ["VP Sales"],
            "geography": ["US"],
        },
    }
    data.update(overrides)
    return data


# ── StateManager & MailboxStore ──


@pytest.mark.asyncio
async def test_sent_today_and_sent_since_normalize_datetime(tmp_path):
    db_path = str(tmp_path / "test.db")
    state = StateManager(db_path)
    await state.init_db()
    store = MailboxStore(db_path)

    # Space-separated and 'T'-separated timestamps at and before cutoff
    i1 = await state.add_outbox_item(
        prospect_id="p1", to_email="a@example.com", subject="s", body="b",
        send_at="2026-11-24T08:00:00", status="sent", campaign_id="c1", step=1, mailbox_id="",
    )
    i2 = await state.add_outbox_item(
        prospect_id="p2", to_email="b@example.com", subject="s", body="b",
        send_at="2026-11-24T08:00:00", status="sent", campaign_id="c1", step=1, mailbox_id="mb1",
    )
    i3 = await state.add_outbox_item(
        prospect_id="p3", to_email="c@example.com", subject="s", body="b",
        send_at="2026-11-24T07:00:00", status="sent", campaign_id="c1", step=1, mailbox_id="",
    )
    await state.update_outbox_item(i1, sent_at="2026-11-24 08:00:00")
    await state.update_outbox_item(i2, sent_at="2026-11-24T08:05:00")
    await state.update_outbox_item(i3, sent_at="2026-11-24 07:59:59")

    assert await state.count_outbox_sent_today(since="2026-11-24T08:00:00") == 2
    assert await store.sent_since("", "2026-11-24T08:00:00") == 1
    assert await store.sent_since("mb1", "2026-11-24 08:00:00") == 1


@pytest.mark.asyncio
async def test_suppressions_plus_tag_chunking_and_writable_prospects(tmp_path):
    state = StateManager(str(tmp_path / "sup.db"))
    await state.init_db()

    await state.add_suppression("address", "jane@acme.example", "opted_out")
    await state.add_suppression("domain", "blocked.example", "manual")

    assert await state.is_suppressed("jane+news@acme.example") == "opted_out"
    assert await state.is_suppressed("JANE+sales@ACME.EXAMPLE") == "opted_out"
    assert await state.is_suppressed("bob@sub.blocked.example") == "manual"

    # >500 unique domains -> >1000 bind pairs if unchunked; must not hit SQLite 999 limit
    many_emails = [f"user{i}@domain{i}.example" for i in range(600)]
    many_emails.append("jane+tag@acme.example")
    found = await state.suppressed_among(many_emails)
    assert found == {"jane+tag@acme.example": "opted_out"}

    # count_writable_prospects must exclude address, +tag, and domain suppressions
    await state.add_prospect(Prospect(
        first_name="Jane", last_name="Tag", email="jane+vip@acme.example",
        email_status="verified", status="new",
    ))
    await state.add_prospect(Prospect(
        first_name="Bob", last_name="Sub", email="bob@mail.blocked.example",
        email_status="verified", status="new",
    ))
    await state.add_prospect(Prospect(
        first_name="Ok", last_name="Lead", email="ok@clean.example",
        email_status="verified", status="new",
    ))
    assert await state.count_writable_prospects() == 1


@pytest.mark.asyncio
async def test_observations_simultaneous_writes_no_duplicates(tmp_path):
    state = StateManager(str(tmp_path / "obs.db"))
    await state.init_db()
    # init_db must NOT seed signal_codes automatically
    assert await state.get_signal_codes() == []

    await state.upsert_signal_code("RUNS_ADS", label="Runs Ads", status="confirmed")
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    ts = "2026-11-24 12:00:00"
    await state.add_observation("RUNS_ADS", company_id=cid, value_num=1.0, observed_at=ts)
    await state.add_observation("RUNS_ADS", company_id=cid, value_num=0.0, observed_at=ts)

    latest = await state.get_observations(company_id=cid, latest_only=True)
    assert len(latest) == 1
    assert latest[0]["value_num"] == 0.0

    # Because latest observation is 0.0, positive cohort & signal_counts must exclude it
    assert await state.cohort(["RUNS_ADS"]) == []
    assert await state.signal_counts() == []

    # Flipping back to 1.0 at the same timestamp makes the newest row win cleanly (once)
    await state.add_observation("RUNS_ADS", company_id=cid, value_num=1.0, observed_at=ts)
    counts = await state.signal_counts()
    assert len(counts) == 1
    assert counts[0]["observations"] == 1
    assert counts[0]["companies"] == 1
    total, rows = await state.cohort_companies(["RUNS_ADS"], [])
    assert total == 1 and len(rows) == 1


@pytest.mark.asyncio
async def test_concurrent_init_db_and_v21_upgrade(tmp_path):
    db_path = str(tmp_path / "upgrade.db")
    # Build a v20 database where enrichment_columns already has dashboard columns
    async with aiosqlite.connect(db_path) as db:
        for script in MIGRATIONS[:20]:
            await db.executescript(script)
        await db.execute("ALTER TABLE enrichment_columns ADD COLUMN output_format TEXT DEFAULT 'text'")
        await db.execute("ALTER TABLE enrichment_columns ADD COLUMN use_web_search INTEGER DEFAULT 0")
        await db.execute("PRAGMA user_version = 20")
        await db.commit()

    s1 = StateManager(db_path)
    s2 = StateManager(db_path)
    await asyncio.gather(s1.init_db(), s2.init_db(), s1.init_db())

    async with aiosqlite.connect(db_path) as db:
        version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
        assert version == 21
        indexes = {
            r[0]
            for r in await (await db.execute("SELECT name FROM sqlite_master WHERE type='index'")).fetchall()
        }
        assert {
            "idx_outbox_message_id",
            "idx_outbox_thread",
            "idx_conversations_prospect_updated",
        } <= indexes
        cols = {
            r[1]
            for r in await (await db.execute("PRAGMA table_info(enrichment_columns)")).fetchall()
        }
        assert {"col_key", "output_format", "use_web_search"} <= cols

    # Enrichment column CRUD with key / output_format / use_web_search
    col_id = await s1.add_enrichment_column(
        "Hiring Signals",
        "Check careers page",
        key="hiring_signals",
        output_format="json",
        use_web_search=True,
    )
    col_rows = await s1.get_enrichment_columns()
    assert len(col_rows) == 1
    assert col_rows[0]["key"] == "hiring_signals"
    assert col_rows[0]["output_format"] == "json"
    assert col_rows[0]["value_type"] == "json"
    assert col_rows[0]["use_web_search"] is True

    await s1.set_enrichment_value(col_id, "comp_1", '{"hiring": true}')
    val_rows = await s1.get_enrichment_values("comp_1")
    assert len(val_rows) == 1
    assert val_rows[0]["entity_id"] == "comp_1"
    assert val_rows[0]["company_id"] == "comp_1"
    assert val_rows[0]["key"] == "hiring_signals"
    assert val_rows[0]["output_format"] == "json"
    assert val_rows[0]["use_web_search"] is True


@pytest.mark.asyncio
async def test_get_latest_sent_outbox_for_recipient(tmp_path):
    state = StateManager(str(tmp_path / "latest_sent.db"))
    await state.init_db()

    o_old = await state.add_outbox_item(
        prospect_id="p1", to_email="pat@acme.example", subject="Old", body="b",
        send_at="2026-11-20T09:00:00", status="sent", campaign_id="camp_old", step=1,
    )
    o_new = await state.add_outbox_item(
        prospect_id="p1", to_email="pat@acme.example", subject="New", body="b",
        send_at="2026-11-21T09:00:00", status="sent", campaign_id="camp_new", step=1,
    )
    o_reply_no_camp = await state.add_outbox_item(
        prospect_id="p1", to_email="pat@acme.example", subject="Reply", body="b",
        send_at="2026-11-22T09:00:00", status="sent", campaign_id="", kind="reply",
    )
    await state.update_outbox_item(o_old, sent_at="2026-11-20 09:00:00")
    await state.update_outbox_item(o_new, sent_at="2026-11-21T09:00:00")
    await state.update_outbox_item(o_reply_no_camp, sent_at="2026-11-22T09:00:00")

    found = await state.get_latest_sent_outbox_for_recipient(" PAT@ACME.EXAMPLE ")
    assert found is not None
    assert found["id"] == o_new
    assert found["campaign_id"] == "camp_new"
    assert await state.get_latest_sent_outbox_for_recipient("nobody@acme.example") is None


@pytest.mark.asyncio
async def test_usage_queries_normalize_iso_timestamps(tmp_path):
    state = StateManager(str(tmp_path / "usage.db"))
    await state.init_db()

    now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    await state.record_usage_event(
        agent="writer", task="draft", model="m1", input_tokens=10, output_tokens=5,
        cost_usd=0.25, created_at=now_iso,
    )
    totals = await state.usage_totals()
    assert totals["today"]["calls"] == 1
    assert totals["week"]["calls"] == 1
    assert totals["month"]["calls"] == 1
    assert len(await state.usage_by_day(days=7)) == 1
    assert len(await state.usage_by_agent(days=7)) == 1


@pytest.mark.asyncio
async def test_conversation_mailbox_id_roundtrip(tmp_path):
    state = StateManager(str(tmp_path / "convo.db"))
    await state.init_db()

    convo = Conversation(
        id="",
        prospect_id="p1",
        campaign_id="c1",
        mailbox_id="mb_extra",
        thread=[Message(sender="sellbird", content="Hi")],
    )
    cid = await state.add_conversation(convo)
    fetched = await state.get_conversation(cid)
    assert fetched is not None
    assert fetched.mailbox_id == "mb_extra"
    listed = await state.get_conversations_by_status("open")
    assert len(listed) == 1
    assert listed[0].mailbox_id == "mb_extra"


# ── Sender daily cap alignment ──


@pytest.mark.asyncio
async def test_sender_aligns_daily_and_cold_caps_to_local_midnight(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "sender.db"))
    await state.init_db()

    # Local time is UTC-8 at 2026-01-02 18:00 (2026-01-03 02:00 UTC).
    # Local midnight is 2026-01-02 08:00 UTC.
    utc_minus_8 = timezone(timedelta(hours=-8))
    pinned_local = datetime(2026, 1, 2, 18, 0, tzinfo=utc_minus_8)
    monkeypatch.setattr(sender_mod, "_local_now", lambda: pinned_local)

    # Sent BEFORE local midnight (2026-01-02 06:00 UTC = Jan 1 22:00 local)
    o_prev = await state.add_outbox_item(
        prospect_id="p_prev", to_email="prev@example.com", subject="Hi", body="Body?",
        send_at="2026-01-02T05:00:00", status="sent", campaign_id="c1", step=1,
    )
    await state.update_outbox_item(o_prev, sent_at="2026-01-02T06:00:00")

    cfg = SellbirdConfig(
        **_minimal_config_dict(
            channels={
                "email": {
                    "provider": "smtp",
                    "require_approval": False,
                    "max_daily_sends": 1,
                }
            },
            usage={"quiet_hours": {"start": "22:00", "end": "07:00", "timezone": "UTC"}},
        )
    )
    sender = sender_mod.Sender(brain=None, state=state, config=cfg, env=EnvConfig())
    assert sender._today_since_iso() == "2026-01-02T08:00:00"

    seen_since: list[str | None] = []
    orig_count = state.count_outbox_sent_today

    async def spy_count(*, kind=None, since=None, mailbox_id=None):
        seen_since.append(since)
        return await orig_count(kind=kind, since=since, mailbox_id=mailbox_id)

    monkeypatch.setattr(state, "count_outbox_sent_today", spy_count)
    monkeypatch.setattr(sender, "provider", SimpleNamespace(is_configured=lambda: True, name="smtp"))

    await sender._run_native()
    # Both max_daily_sends and cold_daily_cap must pass the exact local-midnight since=
    assert seen_since == ["2026-01-02T08:00:00", "2026-01-02T08:00:00"]


# ── Main & Logsafety ──


def test_main_attaches_redact_secrets_to_root_handlers_and_redacts():
    # Verify filter is present on root logger handlers when logging is configured,
    # and masks query-param secrets directly.
    root_handlers = logging.getLogger().handlers
    if root_handlers:
        assert any(isinstance(f, RedactSecrets) for h in root_handlers for f in h.filters)
    filt = RedactSecrets()
    rec = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1,
        "GET https://api.reoon.com/check?key=%s&email=a@b.com", ("supersecret123",), None,
    )
    assert filt.filter(rec) is True
    assert "supersecret123" not in rec.getMessage()
    assert "***" in rec.getMessage()


def test_needs_setup_uses_home_env_and_propagates_yaml_error(tmp_path, monkeypatch):
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    home = Home.for_root(home_dir)
    with use_home(home):
        assert main_mod._needs_setup() is True
        home.env_file.write_text("X=1\n", encoding="utf-8")
        assert main_mod._needs_setup() is True
        home.config_file.write_text("persona: [broken\n", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            main_mod._needs_setup()


@pytest.mark.asyncio
async def test_signal_handler_second_signal_avoids_logger(monkeypatch, capsys):
    registered = {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: registered.setdefault(sig, fn))
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **k: (_ for _ in ()).throw(NotImplementedError()))

    async def fake_heartbeat(stop_event):
        stop_event.set()
        handler = registered[signal.SIGINT]
        with pytest.raises(SystemExit) as exc_info:
            handler(signal.SIGINT, None)
        assert exc_info.value.code == 1

    monkeypatch.setattr(main_mod, "heartbeat", fake_heartbeat)
    logger_calls = []
    monkeypatch.setattr(main_mod.logger, "info", lambda *a, **k: logger_calls.append(a))

    await main_mod._run_with_signals()
    assert logger_calls == []
    assert "Second shutdown signal" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_heartbeat_seeds_signal_catalog(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hb.db")
    cfg = SellbirdConfig(**_minimal_config_dict())
    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(main_mod, "load_env", lambda: EnvConfig())
    monkeypatch.setattr(main_mod, "StateManager", lambda: StateManager(db_path))

    stop_event = asyncio.Event()

    async def stop_preflight():
        stop_event.set()
        return "paused"

    def fake_brain(state, brain_cfg=None):
        return SimpleNamespace(
            provider_label="Claude",
            model="",
            paused_reason="",
            preflight=stop_preflight,
            aclose=AsyncMock(),
        )

    monkeypatch.setattr(main_mod, "Brain", fake_brain)
    monkeypatch.setattr(main_mod, "in_quiet_hours", lambda c: False)
    await main_mod.heartbeat(stop_event)

    codes = await StateManager(db_path).get_signal_codes()
    assert len(codes) > 0


# ── CLI ──


def test_cli_companies_uses_state_manager_and_inits_db(tmp_path, monkeypatch, capsys):
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    home = Home.for_root(home_dir)
    home.data_dir.mkdir(parents=True)
    # Create an older v10 DB at home.db_path
    with sqlite3.connect(home.db_path) as conn:
        for script in MIGRATIONS[:10]:
            conn.executescript(script)
        conn.execute("PRAGMA user_version = 10")
        conn.execute(
            "INSERT INTO companies (id, name, domain, industry, location) "
            "VALUES ('c1', 'Home Co', 'home.example', 'SaaS', 'Austin')"
        )
        conn.commit()

    cfg = SellbirdConfig(**_minimal_config_dict())
    out_csv = tmp_path / "out.csv"
    with use_home(home), use_config(cfg):
        cli.cmd_companies(argparse.Namespace(out=str(out_csv)))

    text = out_csv.read_text(encoding="utf-8-sig")
    assert "Home Co" in text
    assert "1 companies" in capsys.readouterr().out
    with sqlite3.connect(home.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 21


def test_cli_suppressions_rejects_empty_value(tmp_path, monkeypatch):
    monkeypatch.setattr("sellbird.state.DB_PATH", str(tmp_path / "sup.db"))
    for action in ("add", "remove"):
        with pytest.raises(SystemExit) as exc:
            cli.cmd_suppressions(argparse.Namespace(suppression_action=action, value="   "))
        assert exc.value.code == 1


def test_cli_research_targets_domain_and_seeds_signals(tmp_path, monkeypatch):
    import sellbird.agents.researcher as res_mod
    import sellbird.brain as brain_mod

    db_path = str(tmp_path / "res.db")
    state = StateManager(db_path)
    cfg = SellbirdConfig(**_minimal_config_dict())
    monkeypatch.setattr("sellbird.state.StateManager", lambda: StateManager(db_path))
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(config_mod, "load_env", lambda: EnvConfig())

    class FakeBrain:
        def __init__(self, st, bc):
            pass

        async def preflight(self):
            return None

        async def is_within_budget(self, *a, **k):
            return True

        async def aclose(self):
            return None

    calls = []

    class FakeResearcher:
        def __init__(self, brain=None, state=None, config=None, env=None):
            self.state = state

        async def run(self, companies=None, limit=5):
            calls.append(("run", companies, limit))
            return [{"ok": True}]

        async def research_company(self, comp):
            calls.append(("research_company", comp))
            return {"ok": True}

    monkeypatch.setattr(brain_mod, "Brain", FakeBrain)
    monkeypatch.setattr(res_mod, "DeepResearcher", FakeResearcher)

    cli.cmd_research(argparse.Namespace(company="target.example", limit=3))
    assert calls == [("run", [{"domain": "target.example", "name": "target.example"}], 1)]
    assert asyncio.run(state.get_signal_codes()) != []


# ── Config ──


def test_config_yaml11_sexagesimal_and_none_prevalidators():
    qh = QuietHoursConfig(start=1320, end=420, timezone="UTC")
    assert qh.start == "22:00"
    assert qh.end == "07:00"
    with pytest.raises(ValidationError):
        QuietHoursConfig(start=True)
    with pytest.raises(ValidationError):
        QuietHoursConfig(start=1440)

    cfg = SellbirdConfig(
        persona={"name": "A", "company": "C", "role": "R", "email": "e@x.com", "linkedin": None, "tone": None},
        product={"name": "P", "description": None, "pricing": None, "key_benefits": None, "objection_responses": None, "offer": None},
        icp={"industries": None, "company_size": None, "titles": None, "geography": None, "hiring_signals": None, "geo_coordinates": None, "keyword_variants": None},
    )
    assert cfg.persona.linkedin == ""
    assert cfg.product.key_benefits == []
    assert cfg.product.objection_responses == {}
    assert cfg.icp.industries == []
    assert cfg.icp.geo_coordinates == {}


def test_load_env_home_priority_hosted_isolation_and_port_errors(tmp_path, monkeypatch):
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    (home_dir / ".env").write_text("SERPER_API_KEY=from_home\n", encoding="utf-8")
    (cwd_dir / ".env").write_text("SERPER_API_KEY=from_cwd\nHUNTER_API_KEY=from_cwd_only\n", encoding="utf-8")
    monkeypatch.chdir(cwd_dir)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)

    with use_home(Home.for_root(home_dir, mode="self_hosted")):
        env = load_env()
        assert env.serper_api_key == "from_home"
        assert env.hunter_api_key == "from_cwd_only"

    # Hosted mode must not read .env files or mutate os.environ
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    with use_home(Home.for_root(home_dir, mode="hosted")):
        env_hosted = load_env()
        assert env_hosted.serper_api_key == ""
        assert "SERPER_API_KEY" not in os.environ

    # Invalid port raises ConfigError
    monkeypatch.setenv("SMTP_PORT", "not-a-port")
    with use_home(Home.for_root(home_dir, mode="hosted")):
        with pytest.raises(ConfigError, match="SMTP_PORT"):
            load_env()
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.setenv("IMAP_PORT", "99999")
    with use_home(Home.for_root(home_dir, mode="hosted")):
        with pytest.raises(ConfigError, match="IMAP_PORT"):
            load_env()


def test_load_brain_config_honors_use_config_and_hosted(tmp_path):
    home_dir = tmp_path / "hosted"
    home_dir.mkdir()
    cfg = SellbirdConfig(**_minimal_config_dict(brain={"provider": "codex", "model": "gpt-5.5"}))

    with use_home(Home.for_root(home_dir, mode="hosted")):
        with pytest.raises(ConfigFileNotFoundError, match="weren't loaded"):
            load_brain_config()
        with use_config(cfg):
            assert load_brain_config() == BrainConfig(provider="codex", model="gpt-5.5")


def test_config_error_messages_and_set_require_approval_mkstemp(tmp_path):
    with pytest.raises(ConfigFileNotFoundError) as exc:
        load_config(str(tmp_path / "missing.yaml"))
    assert "sellbird.yaml.example" not in str(exc.value)
    assert "sellbird.yaml" in str(exc.value)

    cfg_path = tmp_path / "sellbird.yaml"
    cfg_path.write_text(
        yaml.safe_dump(_minimal_config_dict(channels={"email": {"provider": "smtp", "require_approval": True}})),
        encoding="utf-8",
    )
    set_require_approval(False, cfg_path)
    assert load_config(str(cfg_path)).channels.email.require_approval is False
    assert [p for p in tmp_path.iterdir() if ".tmp" in p.name] == []
