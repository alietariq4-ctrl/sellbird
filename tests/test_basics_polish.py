"""Tests for core Sellbird basics: Scout search failover, Contacts/Suppressions CRUD, Outbox AI rewrite, and SMTP connection test."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.agents.scout import Scout
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "sellbird" / "web"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "sellbird.db"
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=AIza-test-key\n", encoding="utf-8")
    cfg_file = tmp_path / "sellbird.local.yaml"
    cfg_file.write_text(
        """persona:
  name: Ali
  company: Harvey Test
  role: Growth
  email: ali@harvey.example
  linkedin: linkedin.com/in/ali-harvey
  tone: professional
product:
  name: Harvey Agent
  description: Autonomous SDR
  pricing: $99/mo
  key_benefits:
    - Automates prospecting
  objection_responses:
    too expensive: Pays for itself
icp:
  industries:
    - SaaS
  company_size: 10-100 employees
  titles:
    - CEO
  geography:
    - United States
compliance:
  postal_address: "100 Main St, Austin, TX 78701"
  opt_out_line: "Reply stop to opt out."
  do_not_contact:
    - blocked-yaml.example
brain:
  transport_factory: harvey_brain.router_factory:build
usage:
  max_daily_usd: 10.0
""",
        encoding="utf-8",
    )
    brain_yaml = tmp_path / "brain.yaml"
    brain_yaml.write_text(
        """budget:
  max_usd_per_day: 10.0
chain:
  - provider: gemini
    model: gemini-flash-lite-latest
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(dash, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dash, "ENV_FILE", env_file)
    monkeypatch.setattr(dash, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "sellbird.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "sellbird.log")
    monkeypatch.setattr(dash, "_brain_config_file", lambda: brain_yaml)
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: cfg_file)
    monkeypatch.setattr(dash, "_offer_config_file", lambda: cfg_file)
    monkeypatch.setattr(dash, "_sellbird_process", None)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key")

    asyncio.run(StateManager(db_path=str(db)).init_db())
    token = dash.token_store.get()
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    with TestClient(dash.app, base_url=ORIGIN, headers=headers) as client:
        yield client, headers, tmp_path, db, env_file


def test_scout_search_fast_failover_skips_dead_engines():
    """Blocked/failing DDG & Bing fail fast (retries=1, timeout=10) and are skipped on later queries."""
    scout = Scout.__new__(Scout)
    scout.env = SimpleNamespace(serper_api_key="", tinyfish_api_key="")
    scout._queries_this_cycle = 0
    scout._failed_search_engines = set()

    calls: list[tuple[str, float | None, int | None]] = []

    async def fake_fetch(url: str, *, method="GET", json_body=None, headers=None, timeout=None, retries=None):
        calls.append((url, timeout, retries))
        if "duckduckgo.com" in url or "bing.com" in url:
            return None
        if "google.com" in url:
            return SimpleNamespace(
                text='<div class="g"><a href="https://acme.example/team">Acme</a><div class="VwiC3b">Team</div></div>'
            )
        return None

    scout._fetch = fake_fetch

    r1 = asyncio.run(scout._web_search("saas companies"))
    assert r1 == [("https://acme.example/team", "Team")]
    assert "duckduckgo" in scout._failed_search_engines
    assert "bing" in scout._failed_search_engines

    # Query 2 must skip DDG & Bing completely and hit Google directly.
    r2 = asyncio.run(scout._web_search("fintech companies"))
    assert r2 == [("https://acme.example/team", "Team")]

    ddg_calls = [c for c in calls if "duckduckgo.com" in c[0]]
    bing_calls = [c for c in calls if "bing.com" in c[0]]
    google_calls = [c for c in calls if "google.com" in c[0]]
    assert len(ddg_calls) == 1 and ddg_calls[0][1] == 10.0 and ddg_calls[0][2] == 1
    assert len(bing_calls) == 1 and bing_calls[0][1] == 10.0 and bing_calls[0][2] == 1
    assert len(google_calls) == 2 and google_calls[0][1] == 10.0 and google_calls[0][2] == 1


def test_contacts_and_suppressions_crud_lifecycle(env):
    client, headers, _, db, _ = env

    # Invalid email rejected
    bad = client.post("/api/prospects", json={"email": "not-an-email"}, headers=headers)
    assert bad.status_code == 400

    # Create valid contact + company auto-link
    created = client.post(
        "/api/prospects",
        json={
            "first_name": "Sara",
            "last_name": "Khan",
            "email": "sara@northstar.io",
            "title": "COO",
            "company": "Northstar",
        },
        headers=headers,
    )
    assert created.status_code == 200
    body = created.json()
    assert body["success"] is True
    pid = body["id"]

    # Duplicate email rejected
    dup = client.post(
        "/api/prospects",
        json={"email": "SARA@northstar.io", "first_name": "Sara"},
        headers=headers,
    )
    assert dup.status_code == 409

    # Add pending outbox item to confirm suppress/delete cancels queued items
    sm = StateManager(db_path=str(db))
    async def _seed_outbox():
        return await sm.add_outbox_item(
            prospect_id=pid,
            campaign_id="cmp1",
            kind="sequence",
            to_email="sara@northstar.io",
            subject="quick idea",
            body="Noticed Northstar expanded ops. Open to a brief look?",
            send_at="2030-01-01T12:00:00+00:00",
            step=1,
        )
    oid = asyncio.run(_seed_outbox())
    assert oid

    # YAML do_not_contact blocks creation
    blocked_yaml = client.post(
        "/api/prospects",
        json={"email": "ceo@blocked-yaml.example"},
        headers=headers,
    )
    assert blocked_yaml.status_code == 409

    # Add domain suppression -> marks existing prospect opted_out + cancels outbox
    sup = client.post(
        "/api/suppressions",
        json={"value": "https://www.northstar.io/about", "reason": "manual"},
        headers=headers,
    )
    assert sup.status_code == 200
    sup_data = sup.json()
    assert sup_data["success"] is True
    assert sup_data["kind"] == "domain"
    assert sup_data["value"] == "northstar.io"
    assert sup_data["matched_prospects"] == 1

    # Verify prospect updated & outbox cancelled
    prospect_after = asyncio.run(sm.get_prospect(pid))
    assert prospect_after is not None and prospect_after.status == "opted_out"
    item_after = asyncio.run(sm.get_outbox_item(oid))
    assert item_after is not None and item_after["status"] == "cancelled"

    # Creating new contact at suppressed domain fails 409
    suppressed_new = client.post(
        "/api/prospects",
        json={"email": "cto@sub.northstar.io"},
        headers=headers,
    )
    assert suppressed_new.status_code == 409

    # List & remove suppression
    listed = client.get("/api/suppressions", headers=headers)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    sid = items[0]["id"]

    removed = client.delete(f"/api/suppressions/{sid}", headers=headers)
    assert removed.status_code == 200
    assert client.delete(f"/api/suppressions/{sid}", headers=headers).status_code == 404

    # Delete contact cleanly
    deleted = client.delete(f"/api/prospects/{pid}", headers=headers)
    assert deleted.status_code == 200
    assert asyncio.run(sm.get_prospect(pid)) is None
    assert client.delete(f"/api/prospects/{pid}", headers=headers).status_code == 404


def test_outbox_mailbox_ready_and_ai_regenerate(env, monkeypatch):
    client, headers, _, db, _ = env
    sm = StateManager(db_path=str(db))

    async def _seed():
        pid = await sm.add_prospect(
            Prospect(
                first_name="Omar",
                last_name="Raza",
                title="VP Ops",
                company="Beacon Logistics",
                email="omar@beacon.example",
                email_status="verified",
                email_verified=True,
                source="manual",
            )
        )
        oid_cold = await sm.add_outbox_item(
            prospect_id=pid,
            campaign_id="cmp_cold",
            kind="sequence",
            to_email="omar@beacon.example",
            subject="freight handoff",
            body="Saw Beacon opened a second warehouse. Worth a short teardown?",
            send_at="2030-01-01T12:00:00+00:00",
            step=1,
        )
        oid_reply = await sm.add_outbox_item(
            prospect_id=pid,
            campaign_id="",
            kind="reply",
            to_email="omar@beacon.example",
            subject="Re: freight handoff",
            body="Happy to share the numbers tomorrow afternoon.",
            send_at="2030-01-01T12:00:00+00:00",
            step=0,
        )
        return oid_cold, oid_reply

    oid_cold, oid_reply = asyncio.run(_seed())

    # Outbox reports mailbox_ready False when no mailbox configured
    monkeypatch.setattr(dash, "_sellbird_can_send", lambda: False)
    ob = client.get("/api/outbox", headers=headers)
    assert ob.status_code == 200
    assert ob.json()["mailbox_ready"] is False

    class FakeBrain:
        def load_skills_for_agent(self, _name):
            return "Keep outreach concise."

        async def preflight(self):
            return ""

        async def think_json(self, prompt, system=""):
            if "Re: freight handoff" in prompt:
                return {
                    "subject": "SHOULD NOT OVERWRITE REPLY SUBJECT",
                    "body": "Does 2pm Tuesday work to walk through the numbers?",
                }
            return {
                "subject": "warehouse handoff",
                "body": "Saw Beacon added a second warehouse. Want our short workflow checklist?",
            }

        async def close(self):
            return None

    async def fake_prepare():
        cfg = dash._load_active_sellbird_config()
        return (FakeBrain(), sm, cfg, None), None

    monkeypatch.setattr(dash, "_prepare_agentic_runtime", fake_prepare)

    res_cold = client.post(
        f"/api/outbox/{oid_cold}/regenerate",
        json={"instruction": "Make it tighter"},
        headers=headers,
    )
    assert res_cold.status_code == 200
    assert res_cold.json()["subject"] == "warehouse handoff"
    assert "checklist" in res_cold.json()["body"]

    updated_cold = asyncio.run(sm.get_outbox_item(oid_cold))
    assert updated_cold["subject"] == "warehouse handoff"
    assert updated_cold["status"] == "pending_review"

    # Thread reply must keep its stored subject verbatim
    res_reply = client.post(f"/api/outbox/{oid_reply}/regenerate", json={}, headers=headers)
    assert res_reply.status_code == 200
    assert res_reply.json()["subject"] == "Re: freight handoff"
    updated_reply = asyncio.run(sm.get_outbox_item(oid_reply))
    assert updated_reply["subject"] == "Re: freight handoff"
    assert "2pm Tuesday" in updated_reply["body"]


def test_smtp_test_endpoint_uses_form_or_saved_env(env, monkeypatch):
    client, headers, _, _, env_file = env

    # Empty creds -> clear guidance
    res_empty = client.post("/api/settings/test-smtp", json={}, headers=headers)
    assert res_empty.status_code == 200
    assert res_empty.json()["success"] is False
    assert "SMTP" in res_empty.json()["message"]

    captured = {}

    async def fake_test_connection(self):
        captured["smtp_host"] = self.smtp_host
        captured["smtp_port"] = self.smtp_port
        captured["smtp_user"] = self.smtp_user
        captured["smtp_pass"] = self.smtp_pass
        captured["imap_user"] = self.imap_user
        captured["imap_pass"] = self.imap_pass
        return True, f"SMTP + IMAP connected as {self.smtp_user}"

    import sellbird.integrations.smtp_mail as smtp_mail
    monkeypatch.setattr(smtp_mail.SmtpImapProvider, "test_connection", fake_test_connection)

    env_file.write_text(
        "GEMINI_API_KEY=AIza-test-key\nSMTP_HOST=mail.saved.example\nSMTP_PORT=465\nSMTP_USERNAME=ali@saved.example\nSMTP_PASSWORD=secret-pw\n",
        encoding="utf-8",
    )
    res_saved = client.post("/api/settings/test-smtp", json={"imap_same_pass": True}, headers=headers)
    assert res_saved.status_code == 200
    assert res_saved.json()["success"] is True
    assert captured["smtp_host"] == "mail.saved.example"
    assert captured["smtp_port"] == 465
    assert captured["imap_pass"] == "secret-pw"

    # Explicit body overrides .env
    res_override = client.post(
        "/api/settings/test-smtp",
        json={
            "smtp_host": "smtp.fastmail.com",
            "smtp_port": "587",
            "smtp_username": "custom@fastmail.com",
            "smtp_password": "app-pw",
        },
        headers=headers,
    )
    assert res_override.status_code == 200
    assert res_override.json()["success"] is True
    assert captured["smtp_host"] == "smtp.fastmail.com"
    assert captured["smtp_user"] == "custom@fastmail.com"


def test_frontend_basics_syntax_and_dom_wiring():
    app_js = WEB_DIR / "app.js"
    index_html = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    # JS syntax validation
    proc = subprocess.run(["node", "--check", str(app_js)], capture_output=True, text=True)
    assert proc.returncode == 0, f"node --check failed:\n{proc.stderr}"

    # Required HTML anchors
    for anchor in (
        'id="add-contact-dialog"',
        'id="suppressions-panel"',
        'id="suppressions-toggle-btn"',
        'id="test-smtp-btn"',
        'id="smtp-test-result"',
        "openAddContactModal()",
        "toggleSuppressionsPanel()",
        "testSmtpConnection(this)",
    ):
        assert anchor in index_html, f"Missing HTML anchor: {anchor}"

    # Node VM smoke test of new JS rendering paths
    vm_script = """
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');

function mkEl(id) {
  return {
    id, value: '', checked: false, innerHTML: '', textContent: '',
    className: '', style: { display: '' }, disabled: false, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    addEventListener() {}, querySelectorAll() { return []; },
    querySelector() { return null; }, showModal() {}, close() {},
  };
}
const els = new Map();
const getEl = (id) => { if (!els.has(id)) els.set(id, mkEl(id)); return els.get(id); };

const context = {
  console, Math, Date, JSON, Promise, Array, Object, String, Number, Boolean, RegExp, Error, Map, Set,
  encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
  window: { location: { hash: '', search: '', pathname: '/' }, addEventListener() {}, history: { replaceState() {} } },
  document: {
    documentElement: mkEl('html'),
    getElementById: getEl,
    querySelector: () => mkEl('q'),
    querySelectorAll: () => [],
    addEventListener() {},
    createElement: () => mkEl('div'),
    body: mkEl('body'),
  },
  navigator: { clipboard: { writeText: async () => {} } },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(code, context);
vm.runInContext(`
  _desk.items = [{
    id: 'ob1', prospect_id: 'p1', kind: 'sequence', to_email: 'jane@acme.com',
    subject: 'quick idea', body: 'Saw Acme scaled.', step: 1,
    company: 'Acme', title: 'VP', first_name: 'Jane', last_name: 'Doe'
  }];
  _desk.i = 0;
  _desk.footer = '';
  const deskHtml = renderDesk(_desk.items, 0);
  if (!deskHtml.includes('Rewrite with AI')) throw new Error('Missing Rewrite with AI button: ' + deskHtml);

  _prospects = [{
    id: 'p1', first_name: 'Jane', last_name: 'Doe', title: 'VP', company: 'Acme',
    email: 'jane@acme.com', email_status: 'verified', status: 'new', source: 'manual', score: 80, created_at: new Date().toISOString()
  }];
  document.getElementById('prospects-table').innerHTML = '<div id="contact-filters"></div><div id="contact-rows"></div>';
  renderContacts();
  const contactsHtml = document.getElementById('contact-rows').innerHTML;
  if (!contactsHtml.includes('Suppress') || !contactsHtml.includes('Delete')) {
    throw new Error('Missing Suppress/Delete row actions: ' + contactsHtml);
  }

  _suppressions = [{ id: 's1', kind: 'domain', value: 'bad.com', reason: 'manual', source: 'dashboard', created_at: new Date().toISOString() }];
  renderSuppressionsPanel();
  const supHtml = document.getElementById('suppressions-panel').innerHTML;
  if (!supHtml.includes('bad.com') || !supHtml.includes('Remove')) {
    throw new Error('Missing suppression entry rendering: ' + supHtml);
  }
  console.log('VM_BASICS_OK');
`, context);
"""
    vm_proc = subprocess.run(["node", "-e", vm_script, str(app_js)], capture_output=True, text=True)
    assert vm_proc.returncode == 0, f"VM smoke test failed:\n{vm_proc.stderr}\n{vm_proc.stdout}"
    assert "VM_BASICS_OK" in vm_proc.stdout
