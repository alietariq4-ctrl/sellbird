"""Tests for Phase 2 Dashboard & Workflow Features:
1. In-Dashboard Website Trainer (/api/company/train + Your Company card)
2. One-Click Calendar Slot Lookup & Auto-Booking (/api/calendar/slots, /api/calendar/book)
3. Compliance & Sending Schedule Editor (/api/settings/compliance)
4. Campaigns Funnel Bar & On-Demand Writer Trigger (/api/stats funnel, /api/writer/run)
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.middleware import Middleware

import sellbird.dashboard as dash
from sellbird.state import StateManager
from sellbird.workspace_home import Home

ORIGIN = "http://127.0.0.1:5555"
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "sellbird" / "web"

FULL_YAML = """\
persona:
  name: "Alex"
  company: "Acme Corp"
  role: "Founder"
  email: "alex@acme.example"
  linkedin: "linkedin.com/in/alex"
  tone: "direct, helpful"
product:
  name: "Acme Widget"
  description: "Automated widget analytics for B2B teams"
  pricing: "$99/mo"
  key_benefits:
    - "Cuts reporting time in half"
  objection_responses:
    "too expensive": "Most teams save 10 hours a week."
icp:
  industries:
    - "SaaS"
  company_size: "10-200"
  titles:
    - "VP Ops"
  geography:
    - "United States"
channels:
  email:
    enabled: true
    provider: "gmail"
    max_daily_sends: 50
    require_approval: true
usage:
  max_daily_usd: 10.0
  quiet_hours:
    start: "22:00"
    end: "07:00"
    timezone: "America/New_York"
compliance:
  postal_address: "100 Main St, Austin, TX"
  opt_out_line: "Not interested? Just let me know and I won't email you again."
  do_not_contact:
    - "blocked.example"
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "sellbird.db"
    env_file = tmp_path / ".env"
    cfg_file = tmp_path / "sellbird.yaml"
    cfg_file.write_text(FULL_YAML, encoding="utf-8")

    monkeypatch.setattr(dash, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dash, "ENV_FILE", env_file)
    monkeypatch.setattr(dash, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "sellbird.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "sellbird.log")
    local_cfg = tmp_path / "sellbird.local.yaml"
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: local_cfg if local_cfg.exists() else cfg_file)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")

    asyncio.run(StateManager(db_path=str(db)).init_db())
    token = dash.token_store.get()
    client = TestClient(dash.app, base_url=ORIGIN, headers={"Authorization": f"Bearer {token}"})
    return client, tmp_path, db, cfg_file, env_file


# ── 1. In-Dashboard Website Trainer ──


def test_company_train_blocks_hosted_and_unsafe_urls(env, tmp_path):
    client, _, _, _, _ = env

    # Hosted workspace must return 409 via require_self_hosted("local_ai_loop")
    class Open:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)

    hosted_app = dash.build_app(guard=Middleware(Open))
    hosted_app.state.home = Home.for_root(tmp_path, mode="hosted")
    hosted_client = TestClient(hosted_app)
    r_hosted = hosted_client.post("/api/company/train", json={"url": "https://example.com"})
    assert r_hosted.status_code == 409
    assert r_hosted.json()["error"] == "not_on_hosted"

    # Empty / bad / SSRF URLs must return 400
    assert client.post("/api/company/train", json={}).status_code == 400
    assert client.post("/api/company/train", json={"url": "   "}).status_code == 400
    assert client.post("/api/company/train", json={"url": "https://example.com", "max_pages": True}).status_code == 400
    for bad_url in (
        "http://127.0.0.1:8000",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/admin",
        "file:///etc/passwd",
        "ftp://example.com",
    ):
        resp = client.post("/api/company/train", json={"url": bad_url})
        assert resp.status_code == 400, f"Expected 400 for {bad_url}, got {resp.status_code}"


def test_company_train_runs_trainer_clamps_pages_and_closes_brain(env, monkeypatch):
    client, tmp_path, db, _, _ = env
    calls = []
    closed = []

    class FakeBrain:
        def __init__(self, state, brain_cfg):
            self.state = state
            self.config = brain_cfg

        async def preflight(self):
            calls.append("preflight")
            return ""

        async def is_within_budget(self, max_calls, max_percent=None, max_usd=None):
            calls.append(("budget", max_calls, max_percent, max_usd))
            return True

        async def aclose(self):
            closed.append(True)

    class FakeTrainer:
        def __init__(self, brain_config=None):
            self.brain_config = brain_config
            self.state = None
            self.brain = SimpleNamespace(aclose=lambda: asyncio.sleep(0))
            self.scraped_pages = {}

        async def train(self, url, output_path="sellbird.local.yaml", max_pages=100):
            calls.append(("train", url, output_path, max_pages))
            self.scraped_pages = {"/": "Home", "/pricing": "Pricing", "/about": "About"}
            return {
                "persona": {"company": "Northstar AI"},
                "product": {"name": "Northstar Copilot"},
            }

    async def fake_validate(url):
        return None

    monkeypatch.setattr("sellbird.integrations.stealth_fetch.validate_public_url", fake_validate)
    monkeypatch.setattr("sellbird.brain.Brain", FakeBrain)
    monkeypatch.setattr("sellbird.trainer.Trainer", FakeTrainer)

    resp = client.post("/api/company/train", json={"url": "northstar.example", "max_pages": 999})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data == {
        "success": True,
        "url": "https://northstar.example",
        "pages_crawled": 3,
        "company": "Northstar AI",
        "product": "Northstar Copilot",
    }
    assert closed == [True]
    train_call = next(c for c in calls if isinstance(c, tuple) and c[0] == "train")
    assert train_call[1] == "https://northstar.example"
    assert train_call[2] == str(tmp_path / "sellbird.local.yaml")
    assert train_call[3] == 50  # clamped 1..50

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT action_type, agent FROM actions WHERE action_type = 'company_trained'").fetchone()
    assert row == ("company_trained", "dashboard")


def test_company_train_returns_502_when_trainer_returns_none_and_closes_brain(env, monkeypatch):
    client, _, _, _, _ = env
    closed = []

    class FakeBrain:
        def __init__(self, state, brain_cfg):
            pass

        async def preflight(self):
            return ""

        async def is_within_budget(self, *a, **kw):
            return True

        async def aclose(self):
            closed.append(True)

    class FailingTrainer:
        def __init__(self, brain_config=None):
            self.brain = None
            self.scraped_pages = {}

        async def train(self, url, output_path="sellbird.local.yaml", max_pages=25):
            return None

    monkeypatch.setattr("sellbird.integrations.stealth_fetch.validate_public_url", lambda u: asyncio.sleep(0))
    monkeypatch.setattr("sellbird.brain.Brain", FakeBrain)
    monkeypatch.setattr("sellbird.trainer.Trainer", FailingTrainer)

    resp = client.post("/api/company/train", json={"url": "https://example.com"})
    assert resp.status_code == 502
    assert closed == [True]


# ── 2. One-Click Calendar Slot Lookup & Auto-Booking ──


def test_calendar_slots_unconfigured_and_configured_from_env_file(env, monkeypatch):
    client, _, _, _, env_file = env
    for k in ("CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "GOOGLE_CALENDAR_ACCESS_TOKEN", "M365_CALENDAR_ACCESS_TOKEN", "CALENDAR_PROVIDER"):
        monkeypatch.delenv(k, raising=False)

    r_unconf = client.get("/api/calendar/slots?days=5")
    assert r_unconf.status_code == 200
    assert r_unconf.json() == {"configured": False, "provider": "", "slots": []}

    env_file.write_text("CALCOM_API_KEY=cal_test_key\nCALCOM_EVENT_TYPE_ID=4242\n", encoding="utf-8")

    seen = {}

    async def fake_slots(self, date_range="next_5_days", **kwargs):
        seen["date_range"] = date_range
        seen["backend"] = self.backend
        return [{"start": "2026-11-26T15:00:00Z", "end": "2026-11-26T15:30:00Z", "provider": "calcom"}]

    monkeypatch.setattr("sellbird.integrations.calendar.CalendarClient.get_available_slots", fake_slots)

    r_conf = client.get("/api/calendar/slots?days=99")
    assert r_conf.status_code == 200
    body = r_conf.json()
    assert body["configured"] is True
    assert body["provider"] == "calcom"
    assert len(body["slots"]) == 1
    assert seen["date_range"] == "next_14_days"  # clamped 1..14


def test_calendar_book_updates_prospect_cancels_outbox_updates_convo_and_logs(env, monkeypatch):
    client, _, db, _, env_file = env
    env_file.write_text("CALCOM_API_KEY=cal_test_key\nCALCOM_EVENT_TYPE_ID=4242\n", encoding="utf-8")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO prospects (id, first_name, last_name, email, status) "
            "VALUES ('p1', 'Jane', 'Doe', 'jane@acme.example', 'replied')"
        )
        conn.execute(
            "INSERT INTO conversations (id, prospect_id, status, stage) "
            "VALUES ('c1', 'p1', 'open', 'negotiating')"
        )
        conn.execute(
            "INSERT INTO outbox (id, prospect_id, campaign_id, kind, step, to_email, subject, body, status) "
            "VALUES ('o1', 'p1', 'camp1', 'sequence', 2, 'jane@acme.example', 'Follow up', 'Hi', 'pending_review')"
        )
        conn.execute(
            "INSERT INTO outbox (id, prospect_id, campaign_id, kind, step, to_email, subject, body, status) "
            "VALUES ('o2', 'p1', 'camp1', 'sequence', 3, 'jane@acme.example', 'Last ping', 'Hi', 'approved')"
        )
        conn.commit()

    # Invalid payloads -> 400
    assert client.post("/api/calendar/book", json={"prospect_email": "bad", "slot": {"start": "2026-11-26T15:00:00Z"}}).status_code == 400
    assert client.post("/api/calendar/book", json={"prospect_email": "jane@acme.example", "slot": {}}).status_code == 400

    # Provider failure -> 502
    async def fail_book(self, *a, **kw):
        return False

    monkeypatch.setattr("sellbird.integrations.calendar.CalendarClient.book_meeting", fail_book)
    r_fail = client.post(
        "/api/calendar/book",
        json={
            "conversation_id": "c1",
            "prospect_id": "p1",
            "prospect_email": "jane@acme.example",
            "prospect_name": "Jane Doe",
            "slot": {"start": "2026-11-26T15:00:00Z", "provider": "calcom"},
            "notes": "Demo call",
        },
    )
    assert r_fail.status_code == 502

    # Provider success -> 200 + DB updates
    booked_args = {}

    async def ok_book(self, prospect_email, slot, *, prospect_name="", notes="", timezone_name="UTC"):
        booked_args.update({"email": prospect_email, "slot": slot, "name": prospect_name, "notes": notes})
        return True

    monkeypatch.setattr("sellbird.integrations.calendar.CalendarClient.book_meeting", ok_book)
    slot = {"start": "2026-11-26T15:00:00Z", "end": "2026-11-26T15:30:00Z", "provider": "calcom"}
    r_ok = client.post(
        "/api/calendar/book",
        json={
            "conversation_id": "c1",
            "prospect_id": "p1",
            "prospect_email": "jane@acme.example",
            "prospect_name": "Jane Doe",
            "slot": slot,
            "notes": "Demo call",
        },
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json() == {"success": True, "slot": slot, "cancelled_emails": 2}
    assert booked_args["email"] == "jane@acme.example"

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        p_status = conn.execute("SELECT status FROM prospects WHERE id='p1'").fetchone()["status"]
        c_row = conn.execute("SELECT status, stage FROM conversations WHERE id='c1'").fetchone()
        o_rows = conn.execute("SELECT status, error FROM outbox WHERE prospect_id='p1' ORDER BY id").fetchall()
        act = conn.execute("SELECT agent, details_json FROM actions WHERE action_type='meeting_booked'").fetchone()

    assert p_status == "meeting"
    assert c_row["status"] == "meeting_booked"
    assert c_row["stage"] == "closed_won"
    assert [(r["status"], r["error"]) for r in o_rows] == [
        ("cancelled", "meeting_booked"),
        ("cancelled", "meeting_booked"),
    ]
    assert act["agent"] == "dashboard"
    assert json.loads(act["details_json"])["cancelled_emails"] == 2


# ── 3. Compliance & Sending Schedule Editor ──


def test_compliance_settings_get_post_validation_and_roundtrip(env):
    client, tmp_path, _, _, _ = env

    r_get = client.get("/api/settings/compliance")
    assert r_get.status_code == 200, r_get.text
    g0 = r_get.json()
    assert g0["postal_address"] == "100 Main St, Austin, TX"
    assert g0["daily_limit"] == 50
    assert g0["require_approval"] is True
    assert g0["send_window"]["enabled"] is False
    assert g0["quiet_hours"] == {
        "enabled": True,
        "start": "22:00",
        "end": "07:00",
        "timezone": "America/New_York",
    }

    # Validation errors -> 400
    bad_payloads = [
        {"send_window": {"enabled": True, "start": "18:00", "end": "08:00"}},  # start >= end
        {"send_window": {"enabled": True, "start": "99:00", "end": "17:00"}},  # invalid time
        {"send_window": {"enabled": True, "start": "08:00", "end": "17:00", "days": "funday"}},
        {"quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00", "timezone": "Mars/Olympus"}},
        {"opt_out_line": "Cheers!"},  # no opt-out language
        {"daily_limit": -1},
        {"require_approval": "yes"},
        {"postal_address": "x" * 600},
    ]
    for bad in bad_payloads:
        resp = client.post("/api/settings/compliance", json=bad)
        assert resp.status_code == 400, f"Expected 400 for {bad}, got {resp.status_code}: {resp.text}"

    # Valid update persists to sellbird.local.yaml + .bak and preserves unrelated sections
    upd = {
        "postal_address": "500 Congress Ave, Austin, TX 78701",
        "opt_out_line": "Reply stop anytime and I won't write again.",
        "daily_limit": 75,
        "require_approval": False,
        "send_window": {
            "enabled": True,
            "start": "09:00",
            "end": "16:30",
            "days": "mon-sun",
            "timezone": "America/Chicago",
        },
        "quiet_hours": {
            "enabled": True,
            "start": "21:30",
            "end": "06:30",
            "timezone": "America/Chicago",
        },
    }
    r_post = client.post("/api/settings/compliance", json=upd)
    assert r_post.status_code == 200, r_post.text
    assert r_post.json()["success"] is True

    local_yaml = tmp_path / "sellbird.local.yaml"
    bak_yaml = tmp_path / "sellbird.local.yaml.bak"
    assert local_yaml.exists()
    assert bak_yaml.exists()
    assert "blocked.example" in local_yaml.read_text(encoding="utf-8")
    assert "Acme Widget" in local_yaml.read_text(encoding="utf-8")

    g1 = client.get("/api/settings/compliance").json()
    assert g1["postal_address"] == "500 Congress Ave, Austin, TX 78701"
    assert g1["opt_out_line"] == "Reply stop anytime and I won't write again."
    assert g1["daily_limit"] == 75
    assert g1["require_approval"] is False
    assert g1["send_window"] == {
        "enabled": True,
        "start": "09:00",
        "end": "16:30",
        "days": "mon-sun",
        "timezone": "America/Chicago",
    }
    assert g1["quiet_hours"] == {
        "enabled": True,
        "start": "21:30",
        "end": "06:30",
        "timezone": "America/Chicago",
    }

    # Existing /api/sending-mode reflects the change
    assert client.get("/api/sending-mode").json()["mode"] == "autopilot"


# ── 4. Campaigns Funnel Bar & On-Demand Writer Trigger ──


def test_stats_includes_funnel_and_writer_run_executes_and_closes_brain(env, monkeypatch):
    client, _, db, _, _ = env

    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO companies (id, name, domain, profile_checked_at) VALUES ('co1', 'Acme', 'acme.example', datetime('now'))")
        conn.execute("INSERT INTO companies (id, name, domain) VALUES ('co2', 'Beta', 'beta.example')")
        conn.execute("INSERT INTO prospects (id, company_id, email, status) VALUES ('p1', 'co1', 'a@acme.example', 'meeting')")
        conn.execute("INSERT INTO prospects (id, company_id, email, status) VALUES ('p2', 'co2', 'b@beta.example', 'contacted')")
        conn.execute("INSERT INTO prospects (id, company_id, email, status) VALUES ('p3', 'co2', 'c@beta.example', 'new')")
        conn.execute("INSERT INTO campaigns (id, name, status, prospect_ids_json) VALUES ('k1', 'Camp 1', 'active', '[\"p1\", \"p2\"]')")
        conn.execute("INSERT INTO outbox (id, prospect_id, campaign_id, kind, step, to_email, subject, body, status) VALUES ('o1', 'p1', 'k1', 'sequence', 1, 'a@acme.example', 'S', 'B', 'sent')")
        conn.execute("INSERT INTO outbox (id, prospect_id, campaign_id, kind, step, to_email, subject, body, status) VALUES ('o2', 'p2', 'k1', 'sequence', 1, 'b@beta.example', 'S', 'B', 'sent')")
        conn.execute("INSERT INTO outbox (id, prospect_id, campaign_id, kind, step, to_email, subject, body, status) VALUES ('o3', 'p3', 'k1', 'sequence', 1, 'c@beta.example', 'S', 'B', 'pending_review')")
        conn.execute("INSERT INTO conversations (id, prospect_id, status) VALUES ('cv1', 'p1', 'meeting_booked')")
        conn.commit()

    stats = client.get("/api/stats").json()
    assert "pipeline" in stats
    assert stats["funnel"] == {
        "companies": 2,
        "contacts": 3,
        "researched": 1,
        "drafted": 3,
        "sent": 2,
        "replied": 1,
        "booked": 1,
    }

    closed = []
    ran = []

    class FakeBrain:
        def __init__(self, state, brain_cfg):
            self.state = state

        async def preflight(self):
            return ""

        async def is_within_budget(self, *a, **kw):
            return True

        async def aclose(self):
            closed.append(True)

    class FakeWriter:
        def __init__(self, brain, state, config, env=None):
            self.state = state

        async def run(self):
            ran.append(True)
            with sqlite3.connect(db) as conn:
                conn.execute("INSERT INTO campaigns (id, name, status, prospect_ids_json) VALUES ('k2', 'Camp 2', 'draft', '[\"p3\"]')")
                conn.commit()

    monkeypatch.setattr("sellbird.brain.Brain", FakeBrain)
    monkeypatch.setattr("sellbird.agents.writer.Writer", FakeWriter)

    r_run = client.post("/api/writer/run")
    assert r_run.status_code == 200, r_run.text
    assert r_run.json() == {
        "success": True,
        "campaigns_after": 2,
        "pending_outbox": 1,
    }
    assert ran == [True]
    assert closed == [True]


def test_writer_run_respects_budget_gate_and_closes_brain(env, monkeypatch):
    client, _, _, _, _ = env
    closed = []

    class OverBudgetBrain:
        def __init__(self, state, brain_cfg):
            pass

        async def preflight(self):
            return ""

        async def is_within_budget(self, *a, **kw):
            return False

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr("sellbird.brain.Brain", OverBudgetBrain)
    resp = client.post("/api/writer/run")
    assert resp.status_code == 429
    assert closed == [True]


# ── 5. Frontend HTML, CSS, & JS Runtime Verification ──


def test_phase2_html_and_css_elements():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    css = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    # Feature 3: Compliance & Sending Schedule card in Settings
    for id_attr in (
        "compliance-card",
        "comp-postal",
        "comp-optout",
        "comp-daily-limit",
        "comp-require-approval",
        "comp-sw-enabled",
        "comp-sw-start",
        "comp-sw-end",
        "comp-sw-days",
        "comp-sw-tz",
        "comp-qh-enabled",
        "comp-qh-start",
        "comp-qh-end",
        "comp-qh-tz",
    ):
        assert f'id="{id_attr}"' in html
    assert "saveComplianceSettings()" in html

    # Feature 4: Campaigns funnel & Write Emails Now button
    assert 'id="campaigns-funnel"' in html
    assert 'id="writer-run-btn"' in html
    assert "triggerWriterRun(this)" in html
    assert "Write Emails Now" in html

    # CSS classes
    assert ".pipeline-funnel-bar" in css
    assert ".convo-book-panel" in css
    assert ".compliance-grid" in css
    assert ".company-train-row" in css


def test_phase2_js_syntax_and_vm_behaviors():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    js_path = WEB_DIR / "app.js"
    chk = subprocess.run([node, "--check", str(js_path)], capture_output=True, text=True, timeout=15)
    assert chk.returncode == 0, chk.stderr

    script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');

const elements = {};
function makeEl(id, initial = {}) {
  return Object.assign({
    id, value: '', checked: false, disabled: false, textContent: '', innerHTML: '',
    className: '', style: {}, dataset: {},
    focus: () => {},
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
    closest: () => null,
    remove: () => {},
    classList: { add: () => {}, remove: () => {}, contains: () => false },
    showModal: () => {}, close: () => {},
  }, initial);
}

for (const id of [
  'company-body', 'co-train-url', 'co-train-btn', 'co-train-status',
  'compliance-card', 'comp-postal', 'comp-optout', 'comp-daily-limit', 'comp-require-approval',
  'comp-sw-enabled', 'comp-sw-start', 'comp-sw-end', 'comp-sw-days', 'comp-sw-tz',
  'comp-qh-enabled', 'comp-qh-start', 'comp-qh-end', 'comp-qh-tz', 'comp-save-btn',
  'campaigns-funnel', 'campaigns-list', 'frameworks-card', 'writer-run-btn',
  'conversations-list', 'active-provider', 'gmail-id', 'smtp-host', 'smtp-port', 'smtp-user',
  'imap-host', 'imap-port', 'imap-user', 'm365-client', 'm365-tenant', 'linkedin-email',
  'cf-account-id', 'imap-same-pass', 'gmail-secret', 'smtp-pass', 'imap-pass', 'instantly-key',
  'reoon-key', 'zerobounce-key', 'hunter-key', 'linkedin-password', 'cf-api-token',
  'openai-base-url', 'openai-model', 'searxng-url', 'twenty-url', 'telegram-chat', 'calcom-event-type-id',
  'openai-key', 'openrouter-key', 'deepseek-key', 'gemini-key', 'xai-key', 'groq-key',
  'tavily-key', 'firecrawl-key', 'serper-key', 'calcom-key', 'calcom-webhook-secret',
  'twenty-key', 'telegram-token', 'slack-webhook',
  'nav-company', 'nav-today', 'nav-outbox', 'nav-signals', 'nav-conversations',
  'today-queue', 'today-renamed', 'today-kpis', 'today-facts', 'today-figures',
  'today-runs', 'today-runs-block', 'today-activity', 'today-setup', 'live-status',
  'header-dot', 'header-status-text', 'control-dot', 'control-label', 'control-meta',
  'log-viewer', 'btn-start', 'btn-stop', 'cw-enabled', 'cw-watches', 'cw-status'
]) {
  elements[id] = makeEl(id);
}

const apiCalls = [];
let apiHandler = async () => ({});
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {},
  confirm: () => true,
  matchMedia: () => ({ matches: false }),
  document: {
    hidden: false,
    documentElement: { attrs: {}, getAttribute() { return null; }, setAttribute() {}, removeAttribute() {} },
    body: { appendChild: () => {} },
    getElementById: (id) => elements[id] || null,
    querySelectorAll: () => [],
    createElement: (tag) => makeEl(tag),
    addEventListener: () => {},
  },
  localStorage: { store: {}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = v; }, removeItem() {} },
  window: { location: { origin: 'http://127.0.0.1:5555', pathname: '/', hash: '' }, matchMedia: () => ({ matches: false }) },
  authFetch: async () => ({ ok: true, json: async () => ({}) }),
  lockIfRefused: async () => false,
};
sandbox.globalThis = sandbox;
vm.runInNewContext(code, sandbox);

sandbox.api = async (path, opts) => {
  apiCalls.push({ path, opts });
  return apiHandler(path, opts);
};
sandbox.loadToday = async () => {};
sandbox.loadOutbox = async () => {};
sandbox.loadSendingModeCard = async () => {};

(async () => {
  // 1. loadCompany renders Train from Website card & trainCompanyFromUrl posts /api/company/train
  apiHandler = async (path) => {
    if (path === '/api/company') {
      return {
        product: { name: 'Widget', description: 'Desc', pricing: '$10', key_benefits: [], objection_responses: {} },
        persona: { name: 'Alex', role: 'CEO', company: 'Acme', tone: 'Direct', email: 'a@acme.example' },
        icp: { industries: ['SaaS'], titles: ['VP'], company_size: '10-50', geography: ['US'] },
        offer: { entry: 'Free audit', entry_ideas: [] },
      };
    }
    if (path === '/api/company/train') {
      return { success: true, url: 'https://acme.example', pages_crawled: 4, company: 'Acme', product: 'Widget' };
    }
    return {};
  };
  await sandbox.loadCompany();
  const coHtml = elements['company-body'].innerHTML;
  if (!coHtml.includes('id="co-train-url"') || !coHtml.includes('id="co-train-btn"') || !coHtml.includes('id="co-train-status"')) {
    throw new Error('Company trainer card missing expected elements');
  }
  elements['co-train-url'].value = 'https://acme.example';
  await sandbox.trainCompanyFromUrl();
  const trainReq = apiCalls.find(c => c.path === '/api/company/train');
  if (!trainReq || JSON.parse(trainReq.opts.body).url !== 'https://acme.example') {
    throw new Error('trainCompanyFromUrl did not post URL');
  }

  // 2. convoActions & calendar booking flow
  const openHtml = sandbox.convoActions({ id: 'c1', status: 'open', prospect_status: 'replied', prospect_id: 'p1', prospect_email: 'a@x.test' });
  const waitHtml = sandbox.convoActions({ id: 'awaiting-p2', status: 'awaiting_reply', prospect_status: 'contacted', prospect_id: 'p2', prospect_email: 'b@x.test' });
  const optedHtml = sandbox.convoActions({ id: 'c3', status: 'needs_human', prospect_status: 'opted_out', prospect_id: 'p3', prospect_email: 'c@x.test' });
  if (!openHtml.includes('Book Meeting') || !waitHtml.includes('Book Meeting') || optedHtml.includes('Book Meeting')) {
    throw new Error('convoActions Book Meeting visibility wrong');
  }
  elements['convo-book-c1'] = makeEl('convo-book-c1');
  apiHandler = async (path) => {
    if (path.startsWith('/api/calendar/slots')) return { configured: false, provider: '', slots: [] };
    return {};
  };
  await sandbox.toggleConvoBooking('c1');
  if (!elements['convo-book-c1'].innerHTML.includes("goTab('settings')")) {
    throw new Error('Unconfigured calendar did not show Settings link');
  }

  // Re-open with slots configured and book slot
  elements['convo-book-c1'].dataset.open = '0';
  apiHandler = async (path) => {
    if (path.startsWith('/api/calendar/slots')) {
      return { configured: true, provider: 'calcom', slots: [{ start: '2026-11-26T15:00:00Z', end: '2026-11-26T15:30:00Z', provider: 'calcom' }] };
    }
    if (path === '/api/calendar/book') return { success: true, cancelled_emails: 1 };
    if (path.startsWith('/api/conversations')) return [];
    return {};
  };
  await sandbox.toggleConvoBooking('c1');
  if (!elements['convo-book-c1'].innerHTML.includes('Confirm Book')) {
    throw new Error('Configured calendar did not render slot button');
  }
  await sandbox.loadConversations();
  // Seed _convos in VM context so confirmBookSlot finds it
  vm.runInContext("_convos = [{id:'c1', prospect_id:'p1', prospect_email:'a@x.test', first_name:'Ada', last_name:'L'}];", sandbox);
  await sandbox.confirmBookSlot('c1', 0, makeEl('btn'));
  const bookReq = apiCalls.find(c => c.path === '/api/calendar/book');
  if (!bookReq || JSON.parse(bookReq.opts.body).prospect_email !== 'a@x.test') {
    throw new Error('confirmBookSlot did not call /api/calendar/book');
  }

  // 3. loadSettings -> loadComplianceSettings & saveComplianceSettings
  apiHandler = async (path) => {
    if (path === '/api/settings') return { provider: 'gmail' };
    if (path === '/api/settings/compliance') {
      return {
        postal_address: '100 Main St',
        opt_out_line: 'Reply stop to opt out.',
        daily_limit: 40,
        require_approval: true,
        send_window: { enabled: true, start: '08:30', end: '16:30', days: 'mon-fri', timezone: 'America/New_York' },
        quiet_hours: { enabled: true, start: '22:00', end: '07:00', timezone: 'America/New_York' },
      };
    }
    return { success: true };
  };
  await sandbox.loadSettings();
  if (elements['comp-postal'].value !== '100 Main St' || elements['comp-daily-limit'].value !== '40' || elements['comp-sw-enabled'].checked !== true) {
    throw new Error('loadComplianceSettings did not populate fields');
  }
  await sandbox.saveComplianceSettings();
  const compSave = apiCalls.find(c => c.path === '/api/settings/compliance' && c.opts && c.opts.method === 'POST');
  if (!compSave || JSON.parse(compSave.opts.body).daily_limit !== 40) {
    throw new Error('saveComplianceSettings did not POST payload');
  }

  // 4. Campaigns funnel + triggerWriterRun
  apiHandler = async (path) => {
    if (path === '/api/stats') return { funnel: { companies: 10, contacts: 8, researched: 6, drafted: 4, sent: 3, replied: 2, booked: 1 } };
    if (path === '/api/frameworks') return { configured: [], rows: [] };
    if (path === '/api/campaigns') return [];
    if (path === '/api/writer/run') return { success: true, campaigns_after: 1, pending_outbox: 2 };
    return {};
  };
  await sandbox.loadCampaigns();
  if (!elements['campaigns-funnel'].innerHTML.includes('Meetings') || !elements['campaigns-funnel'].innerHTML.includes('10')) {
    throw new Error('Campaigns funnel did not render stats');
  }
  await sandbox.triggerWriterRun(elements['writer-run-btn']);
  if (!apiCalls.some(c => c.path === '/api/writer/run')) {
    throw new Error('triggerWriterRun did not call /api/writer/run');
  }

  console.log('PHASE2_NODE_OK');
})();
"""
    proc = subprocess.run(
        [node, "-e", script, str(js_path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "PHASE2_NODE_OK" in proc.stdout
