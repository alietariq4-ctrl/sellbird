"""Tests for Batch 2 Email Marketing Suite: Contact Detail/Edit, Templates, A/B Split & Analytics, Warmup & Test-Send."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.models.conversation import Conversation, Message
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "sellbird" / "web"


@pytest.fixture
def adv_env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "sellbird.db"
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=AIza-test-key\n", encoding="utf-8")
    cfg_file = tmp_path / "sellbird.local.yaml"
    cfg_file.write_text(
        """persona:
  name: Ali Tariq
  company: Harvey SaaS
  role: Founder
  email: ali@harveysaas.io
  linkedin: https://linkedin.com/in/alitariq
  tone: professional
product:
  name: Harvey AI
  description: Autonomous SDR & email marketing suite
  pricing: Starting at $99/mo
  key_benefits:
    - Autonomous prospecting
  objection_responses:
    too expensive: ROI in 30 days
  offer:
    entry: 15-min audit
    booking_url: https://cal.com/harvey/15min
icp:
  industries:
    - saas
  company_size: 1-50 employees
  titles:
    - Founder
  geography:
    - United States
sender:
  from_name: Ali Tariq
compliance:
  postal_address: 100 Market St, San Francisco, CA 94105
  unsubscribe_text: Reply stop to opt out.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(dash, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dash, "ENV_FILE", env_file)
    monkeypatch.setattr(dash, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "sellbird.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "sellbird.log")
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: cfg_file)
    monkeypatch.setattr(dash, "_offer_config_file", lambda: cfg_file)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key")

    sm = StateManager(db_path=str(db))
    asyncio.run(sm.init_db())
    token = dash.token_store.get()
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    with TestClient(dash.app, base_url=ORIGIN, headers=headers) as client:
        yield client, headers, sm


def test_contact_detail_and_edit_drawer_api(adv_env):
    client, headers, sm = adv_env

    async def _seed():
        await sm.add_prospect(
            Prospect(
                id="cd-1",
                first_name="Zara",
                last_name="Noor",
                email="zara@cloudops.io",
                company="CloudOps",
                title="Head of Growth",
                email_status="verified",
                tags="saas",
            )
        )
        await sm.add_prospect(
            Prospect(id="cd-2", first_name="Alex", email="alex@other.io", company="Other", email_status="verified")
        )
        await sm.add_outbox_item(
            prospect_id="cd-1",
            to_email="zara@cloudops.io",
            subject="Hello CloudOps",
            body="Saw your product release.",
            send_at="2026-11-27T10:00:00",
        )
        await sm.add_conversation(
            Conversation(
                id="cd-conv",
                prospect_id="cd-1",
                status="open",
                stage="engaged",
                thread=[Message(sender="prospect", content="Tell me more")],
            )
        )

    asyncio.run(_seed())

    detail = client.get("/api/prospects/cd-1", headers=headers)
    assert detail.status_code == 200
    djson = detail.json()
    assert djson["prospect"]["email"] == "zara@cloudops.io"
    assert len(djson["outbox"]) == 1
    assert len(djson["conversations"]) == 1

    # Duplicate email collision blocked
    dup = client.patch(
        "/api/prospects/cd-1",
        headers=headers,
        json={"email": "alex@other.io"},
    )
    assert dup.status_code == 409

    # Valid edit persists normalized tags + status
    upd = client.patch(
        "/api/prospects/cd-1",
        headers=headers,
        json={
            "first_name": "Zara",
            "last_name": "Khan",
            "email": "zara@cloudops.io",
            "title": "VP Growth",
            "company": "CloudOps AI",
            "tags": "SaaS, VIP, founder",
            "status": "interested",
            "email_status": "verified",
        },
    )
    assert upd.status_code == 200
    p = upd.json()["prospect"]
    assert p["last_name"] == "Khan"
    assert p["title"] == "VP Growth"
    assert p["status"] == "interested"
    assert p["tags"] == "founder, saas, vip"


def test_email_templates_crud_and_starter_seed(adv_env):
    client, headers, _ = adv_env

    # Seed starter pack cleanly & idempotently
    s1 = client.post("/api/templates/seed", headers=headers)
    assert s1.status_code == 200 and s1.json()["added"] == 4
    s2 = client.post("/api/templates/seed", headers=headers)
    assert s2.status_code == 200 and s2.json()["added"] == 0

    # Create, update, list, delete custom template
    c_res = client.post(
        "/api/templates",
        headers=headers,
        json={
            "name": "Agency Custom Intro",
            "category": "cold",
            "tags": "agency, b2b",
            "subject": "Idea for {{company}}",
            "body": "Hi {{first_name}}, quick note for {{company}}.",
        },
    )
    assert c_res.status_code == 200
    tid = c_res.json()["id"]

    u_res = client.patch(
        f"/api/templates/{tid}",
        headers=headers,
        json={"name": "Agency Custom Intro v2", "category": "followup"},
    )
    assert u_res.status_code == 200
    assert u_res.json()["template"]["name"] == "Agency Custom Intro v2"

    all_items = client.get("/api/templates", headers=headers).json()["items"]
    assert len(all_items) == 5

    d_res = client.delete(f"/api/templates/{tid}", headers=headers)
    assert d_res.status_code == 200


def test_campaign_ab_split_launch_and_analytics(adv_env):
    client, headers, sm = adv_env

    async def _seed():
        await sm.add_prospect(
            Prospect(id="ab-1", first_name="Ava", email="ava@one.io", company="OneSaaS", email_status="verified", tags="saas", score=95)
        )
        await sm.add_prospect(
            Prospect(id="ab-2", first_name="Ben", email="ben@two.io", company="TwoSaaS", email_status="verified", tags="saas", score=80)
        )

    asyncio.run(_seed())

    camp_res = client.post(
        "/api/campaigns",
        headers=headers,
        json={
            "name": "AB Subject Test",
            "target_tags": "saas",
            "framework": "AIDA",
            "sequence": [
                {
                    "step": 1,
                    "delay_days": 0,
                    "subject": "Variant A for {{company}}",
                    "body": "Hi {{first_name}}, pitch A for {{company}}.",
                    "subject_b": "Variant B for {{company}}",
                    "body_b": "Hi {{first_name}}, pitch B for {{company}}.",
                }
            ],
        },
    )
    assert camp_res.status_code == 200
    cid = camp_res.json()["id"]

    launch_res = client.post(f"/api/campaigns/{cid}/launch", headers=headers, json={})
    assert launch_res.status_code == 200
    ldata = launch_res.json()
    assert ldata["queued_prospects"] == 2
    assert ldata["ab_split"] == {"A": 1, "B": 1}

    # Mark outbox items sent and mark Variant B prospect replied to verify winner analytics
    pending = client.get("/api/outbox", headers=headers).json()["pending"]
    b_item = next(it for it in pending if it.get("variant") == "B")
    a_item = next(it for it in pending if it.get("variant") == "A")
    assert "Variant B" in b_item["subject"]
    assert "Variant A" in a_item["subject"]

    async def _simulate_send_and_reply():
        await sm.update_outbox_item(a_item["id"], status="sent", sent_at="2026-11-27T11:00:00")
        await sm.update_outbox_item(b_item["id"], status="sent", sent_at="2026-11-27T11:05:00")
        await sm.update_prospect_status(b_item["prospect_id"], "replied")

    asyncio.run(_simulate_send_and_reply())

    an_res = client.get(f"/api/campaigns/{cid}/analytics", headers=headers)
    assert an_res.status_code == 200
    an = an_res.json()["analytics"]
    assert an["sent"] == 2
    assert an["replied"] == 1
    assert an["variants"]["A"]["replies"] == 0
    assert an["variants"]["B"]["replies"] == 1
    assert an["variants"]["B"]["reply_rate_pct"] == 100.0
    assert an["winning_variant"] == "B"


def test_warmup_settings_and_test_send_preview(adv_env, monkeypatch):
    client, headers, sm = adv_env

    save_res = client.post(
        "/api/settings/compliance",
        headers=headers,
        json={
            "from_name": "Harvey Sender",
            "daily_limit": 80,
            "per_inbox_daily_cap": 40,
            "send_to_risky": True,
            "warmup": {
                "start_date": "2026-11-01",
                "min_days": 10,
                "start_sends": 8,
                "daily_increase": 4,
            },
        },
    )
    assert save_res.status_code == 200, save_res.text
    got = client.get("/api/settings/compliance", headers=headers).json()
    assert got["from_name"] == "Harvey Sender"
    assert got["per_inbox_daily_cap"] == 40
    assert got["send_to_risky"] is True
    assert got["warmup"]["start_date"] == "2026-11-01"
    assert got["warmup"]["start_sends"] == 8

    # Test-send dry-run preview when no live mailbox provider is configured
    async def _seed_ob():
        await sm.add_prospect(Prospect(id="tp-1", first_name="Tim", email="tim@t.io", company="T Co", email_status="verified"))
        return await sm.add_outbox_item(
            prospect_id="tp-1",
            to_email="tim@t.io",
            subject="Testing preview",
            body="Hello Tim, preview paragraph.",
            send_at="2026-11-27T12:00:00",
        )

    oid = asyncio.run(_seed_ob())
    prev_res = client.post(
        "/api/outbox/test-send",
        headers=headers,
        json={"item_id": oid, "to_email": "ali@harveysaas.io"},
    )
    assert prev_res.status_code == 200
    pdata = prev_res.json()
    assert pdata["success"] is True
    assert pdata["dry_run"] is True
    assert "Harvey Sender" in pdata["preview"]["text"]
    assert "100 Market St" in pdata["preview"]["text"]
    assert "<div" in pdata["preview"]["html"]

    # Live send branch using fake MailProvider
    class _FakeMailProvider:
        name = "smtp"

        def is_configured(self):
            return True

        async def send_email(self, to_email, subject, body, **kw):
            from sellbird.integrations.mail_provider import SendResult
            return SendResult(ok=True, message_id="msg-test-1")

    monkeypatch.setattr("sellbird.integrations.mail_provider.get_mail_provider", lambda cfg, env: _FakeMailProvider())
    live_res = client.post(
        "/api/outbox/test-send",
        headers=headers,
        json={"item_id": oid, "to_email": "ali@harveysaas.io"},
    )
    assert live_res.status_code == 200
    assert live_res.json()["sent"] is True
    assert live_res.json()["dry_run"] is False


def test_advanced_suite_frontend_contracts_and_node_vm():
    html_text = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for marker in (
        'id="templates-btn"',
        'id="contact-detail-dialog"',
        'id="templates-dialog"',
        'id="test-send-dialog"',
        'id="comp-wu-start"',
        'id="comp-inbox-cap"',
    ):
        assert marker in html_text

    app_js = WEB_DIR / "app.js"
    vm_script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');

function mkEl(id) {
  return {
    id, value: '', checked: false, innerHTML: '', textContent: '', disabled: false,
    style: {}, dataset: {}, classList: { add(){}, remove(){}, toggle(){} },
    setAttribute(){}, removeAttribute(){}, getAttribute(){ return null; }, appendChild(){}, focus(){},
    querySelector() { return mkEl('q'); }, querySelectorAll() { return []; },
    showModal(){ this.open = true; }, close(){ this.open = false; },
    addEventListener(){},
  };
}
const els = new Map();
const getEl = id => { if (!els.has(id)) els.set(id, mkEl(id)); return els.get(id); };

const fixtures = {
  '/api/templates': {
    items: [{ id: 't1', name: 'SaaS Launch', category: 'cold', tags: 'saas', subject: 'Hi {{company}}', body: 'Hello {{first_name}}' }],
  },
  '/api/prospects/p1': {
    success: true,
    prospect: { id: 'p1', first_name: 'Mia', last_name: 'One', email: 'mia@one.io', title: 'CEO', company: 'One Co', email_status: 'verified', status: 'replied', tags: 'saas' },
    outbox: [{ subject: 'Hi One Co', body: 'Note', status: 'sent', variant: 'B' }],
    conversations: [{ status: 'open', stage: 'engaged', messages: [{ content: 'Yes!' }] }],
  },
  '/api/campaigns': [
    {
      id: 'c1', name: 'AB Launch', status: 'active', target_tags: 'saas', framework: 'PAS', prospect_ids: ['p1'],
      outbox_counts: { pending_review: 0, approved: 0, sent: 2 },
      analytics: {
        sent: 2, replied: 1, interested: 1, meetings: 0, reply_rate_pct: 50,
        winning_variant: 'B',
        variants: { A: { sent: 1, pending: 0, replies: 0, reply_rate_pct: 0 }, B: { sent: 1, pending: 0, replies: 1, reply_rate_pct: 100 } },
        steps: [{ step: 1, sent: 2, pending: 0, scheduled: 0 }],
      },
      sequence: [{ step: 1, delay_days: 0, subject: 'Sub A', body: 'Body A', subject_b: 'Sub B', body_b: 'Body B' }],
      created_at: '2026-11-27T10:00:00Z',
    },
  ],
  '/api/frameworks': {},
  '/api/stats': { companies: 1, prospects: 1, outbox_pending: 0 },
  '/api/outbox/test-send': {
    success: true, sent: false, dry_run: true, to_email: 'ali@harveysaas.io', subject: 'Preview',
    message: 'Rendered preview ready.',
    preview: { to: 'ali@harveysaas.io', subject: 'Preview', html: '<div>Preview body</div>', text: 'Preview body' },
  },
};

async function mockFetch(url) {
  const path = String(url).split('#')[0];
  const data = fixtures[path] !== undefined ? fixtures[path] : { success: true };
  return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(data)) };
}

const context = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
  URLSearchParams, encodeURIComponent, decodeURIComponent, Math, Date, JSON, Set, Map, Array, Object, String, Number, Boolean,
  fetch: mockFetch, authFetch: mockFetch, authToken: () => 'testtoken',
  window: { location: { hash: '', search: '', pathname: '/' }, addEventListener(){}, history: { replaceState(){} }, scrollTo(){} },
  document: {
    documentElement: mkEl('html'), getElementById: getEl, querySelector: () => mkEl('q'),
    querySelectorAll: () => [], addEventListener(){}, createElement: () => mkEl('div'), body: mkEl('body'),
  },
  navigator: { clipboard: { writeText: async () => {} } },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(code, context);

(async () => {
  await context.openContactDetailModal('p1');
  if (getEl('cd-email').value !== 'mia@one.io' || !getEl('cd-history').innerHTML.includes('Var B')) {
    throw new Error('Contact detail modal did not populate fields/history');
  }

  await context.openTemplatesModal();
  if (!getEl('templates-list').innerHTML.includes('SaaS Launch')) throw new Error('Templates list missing item');

  await context.loadCampaigns();
  context.toggleCampaignAnalytics('c1');
  await context.loadCampaigns();
  const ch = getEl('campaigns-list').innerHTML;
  if (!ch.includes('A/B Split:') || !ch.includes('Winner: Variant B')) throw new Error('Campaign analytics panel missing A/B winner');
  if (!ch.includes('Filter campaigns by status')) throw new Error('Missing Campaigns status filter bar');

  context.openCampaignBuilderModal('c1');
  if (!getEl('cb-steps').innerHTML.includes('Subject line Variant B')) throw new Error('Campaign builder missing Variant B field');
  context.copyVariantAToB(0);
  if (!getEl('cb-steps').innerHTML.includes('Sub A')) throw new Error('Copy A -> B helper failed');

  context.renderTodayWorkflowGuide({ companies: 5, prospects: 3, campaigns: 1, outbox_pending: 2 }, []);
  if (!getEl('today-workflow-guide').innerHTML.includes('5 co') || !getEl('today-workflow-guide').innerHTML.includes('2 waiting')) {
    throw new Error('Workflow progress guide did not update counts');
  }

  await context.openTestSendModal('o1');
  if (!getEl('ts-preview').innerHTML.includes('Rendered inbox preview')) throw new Error('Test send modal missing rendered preview');

  console.log('VM_ADV_SUITE_OK');
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
"""
    proc = subprocess.run(["node", "-e", vm_script, str(app_js)], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, f"VM test failed:\n{proc.stderr}\n{proc.stdout}"
    assert "VM_ADV_SUITE_OK" in proc.stdout
