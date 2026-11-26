"""Regression tests for Outbox guards/bulk, Contacts tags/bulk, Campaign Builder/Launch, and Conversation Replies."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.models.company import Company
from sellbird.models.conversation import Conversation, Message
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "sellbird" / "web"


@pytest.fixture
def suite_env(tmp_path, monkeypatch):
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
    - Autonomous lead research and personalized outreach
  objection_responses:
    too expensive: Pays for itself with one booked meeting
  offer:
    entry: 15-min pipeline audit
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
        yield client, headers, sm, db


def test_state_tags_and_campaign_crud_helpers(suite_env):
    _, _, sm, db = suite_env

    async def _run():
        await sm.add_company(Company(id="c1", name="Acme Cloud", domain="acmecloud.io"))
        p1 = Prospect(
            id="p1",
            company_id="c1",
            first_name="Jane",
            last_name="Doe",
            email="jane@acmecloud.io",
            company="Acme Cloud",
            email_status="verified",
            tags="saas, founder",
        )
        await sm.add_prospect(p1)
        got = await sm.get_prospect("p1")
        assert got is not None
        assert got.tags == "saas, founder"

        changed = await sm.update_prospect_tags(["p1"], add_tags=["VIP", "saas"], remove_tags=["founder"])
        assert changed == 1
        got2 = await sm.get_prospect("p1")
        assert "vip" in got2.tags and "saas" in got2.tags and "founder" not in got2.tags

        camp = Campaign(
            id="camp-state-1",
            name="VIP Sequence",
            target_tags="vip, saas",
            prospect_ids=["p1"],
            sequence=[EmailStep(step=1, delay_days=0, subject="Hi {{first_name}}", body="Body {{company}}")],
        )
        await sm.add_campaign(camp)
        got_camp = await sm.get_campaign("camp-state-1")
        assert got_camp is not None
        assert got_camp["target_tags"] == "vip, saas"

        assert await sm.delete_campaign("camp-state-1") is True
        assert await sm.get_campaign("camp-state-1") is None
        assert await sm.delete_prospects(["p1"]) == 1
        assert await sm.get_prospect("p1") is None

    asyncio.run(_run())


def test_contacts_tags_and_bulk_actions(suite_env):
    client, headers, sm, _ = suite_env

    r1 = client.post(
        "/api/prospects",
        headers=headers,
        json={
            "first_name": "Sara",
            "last_name": "Lin",
            "email": "sara@alpha.io",
            "company": "Alpha SaaS",
            "tags": "saas, founder",
            "email_status": "verified",
        },
    )
    r2 = client.post(
        "/api/prospects",
        headers=headers,
        json={
            "first_name": "Omar",
            "last_name": "Khan",
            "email": "omar@beta.io",
            "company": "Beta Trades",
            "tags": "local",
            "email_status": "verified",
        },
    )
    assert r1.status_code == 200 and r2.status_code == 200
    p1_id, p2_id = r1.json()["id"], r2.json()["id"]

    # Bulk tag + untag
    tag_res = client.post(
        "/api/prospects/bulk",
        headers=headers,
        json={"ids": [p1_id, p2_id], "action": "tag", "tags": "q4-launch"},
    )
    assert tag_res.status_code == 200
    assert tag_res.json()["count"] == 2

    untag_res = client.post(
        "/api/prospects/bulk",
        headers=headers,
        json={"ids": [p2_id], "action": "untag", "tags": "q4-launch"},
    )
    assert untag_res.status_code == 200
    listed = {p["id"]: p for p in client.get("/api/prospects", headers=headers).json()}
    assert "q4-launch" in listed[p1_id]["tags"]
    assert "q4-launch" not in listed[p2_id]["tags"]

    # Bulk suppress p2
    sup_res = client.post(
        "/api/prospects/bulk",
        headers=headers,
        json={"ids": [p2_id], "action": "suppress"},
    )
    assert sup_res.status_code == 200
    sup_list = client.get("/api/suppressions", headers=headers).json()["items"]
    assert any(item["value"] == "omar@beta.io" for item in sup_list)

    # Bulk delete both
    del_res = client.post(
        "/api/prospects/bulk",
        headers=headers,
        json={"ids": [p1_id, p2_id], "action": "delete"},
    )
    assert del_res.status_code == 200
    assert del_res.json()["count"] == 2
    assert client.get("/api/prospects", headers=headers).json() == []


def test_outbox_unrendered_tag_guard_and_bulk_actions(suite_env):
    client, headers, sm, _ = suite_env

    async def _seed():
        await sm.add_prospect(
            Prospect(id="op1", first_name="Mia", email="mia@one.io", company="One Co", email_status="verified")
        )
        await sm.add_prospect(
            Prospect(id="op2", first_name="Leo", email="leo@two.io", company="Two Co", email_status="verified")
        )
        bad_id = await sm.add_outbox_item(
            prospect_id="op1",
            to_email="mia@one.io",
            subject="Hello {{company}}",
            body="Hi {{first_name}}, quick note.",
            send_at="2026-11-26T12:00:00",
            step=1,
        )
        clean_id = await sm.add_outbox_item(
            prospect_id="op2",
            to_email="leo@two.io",
            subject="Quick question for Two Co",
            body="Hi Leo,\n\nSaw Two Co launched your API docs.",
            send_at="2026-11-26T12:05:00",
            step=1,
        )
        return bad_id, clean_id

    bad_id, clean_id = asyncio.run(_seed())

    outbox_data = client.get("/api/outbox", headers=headers).json()
    assert outbox_data["sender_name"] == "Ali Tariq"
    by_id = {it["id"]: it for it in outbox_data["pending"]}
    assert set(by_id[bad_id]["unrendered_tags"]) == {"{{company}}", "{{first_name}}"}
    assert by_id[clean_id]["unrendered_tags"] == []

    # Editing with unrendered tag still present must fail validation
    bad_edit = client.post(
        f"/api/outbox/{bad_id}/edit",
        headers=headers,
        json={"subject": "Hello {{company}}", "body": "Hi Mia, concrete edit body."},
    )
    assert bad_edit.status_code == 400
    assert "unrendered" in bad_edit.json()["message"].lower()

    # Clean edit succeeds and clears unrendered_tags
    good_edit = client.post(
        f"/api/outbox/{bad_id}/edit",
        headers=headers,
        json={"subject": "Hello One Co", "body": "Hi Mia, loved your product changelog."},
    )
    assert good_edit.status_code == 200

    # Bulk approve clean_id and bulk reject bad_id
    bulk_app = client.post(
        "/api/outbox/bulk",
        headers=headers,
        json={"ids": [clean_id], "action": "approve"},
    )
    assert bulk_app.status_code == 200 and bulk_app.json()["count"] == 1

    bulk_rej = client.post(
        "/api/outbox/bulk",
        headers=headers,
        json={"ids": [bad_id], "action": "reject"},
    )
    assert bulk_rej.status_code == 200 and bulk_rej.json()["count"] == 1

    after = client.get("/api/outbox", headers=headers).json()
    assert after["pending"] == []
    assert [x["id"] for x in after["approved"]] == [clean_id]


def test_campaign_builder_lifecycle_ai_steps_and_targeted_launch(suite_env, monkeypatch):
    client, headers, sm, _ = suite_env

    # Seed matching verified prospect, non-matching tag prospect, and risky unverified prospect
    async def _seed():
        await sm.add_prospect(
            Prospect(
                id="cp-match",
                first_name="Nora",
                last_name="Vance",
                email="nora@saasrocket.io",
                company="SaaS Rocket",
                email_status="verified",
                tags="saas, founder",
            )
        )
        await sm.add_prospect(
            Prospect(
                id="cp-other",
                first_name="Bob",
                email="bob@bakery.local",
                company="Bob Bakery",
                email_status="verified",
                tags="local",
            )
        )
        await sm.add_prospect(
            Prospect(
                id="cp-guess",
                first_name="Eve",
                email="eve@saasrocket.io",
                company="SaaS Rocket",
                email_status="guess",
                tags="saas",
            )
        )
        await sm.add_prospect(
            Prospect(
                id="cp-sup",
                first_name="Sam",
                email="sam@suppressed.io",
                company="Suppressed Inc",
                email_status="verified",
                tags="saas",
            )
        )
        await sm.add_suppression("address", "sam@suppressed.io", reason="manual")

    asyncio.run(_seed())

    # AI Draft 3 steps endpoint (mock Brain so test stays offline & deterministic)
    class _FakeDraftBrain:
        def load_skills_for_agent(self, agent):
            return ""

        async def think_json(self, prompt, **kw):
            return {
                "sequence": [
                    {"step": 1, "delay_days": 0, "subject": "Quick idea for {{company}}", "body": "Hi {{first_name}}, noticed {{company}} growing fast."},
                    {"step": 2, "delay_days": 3, "subject": "Re: Quick idea for {{company}}", "body": "Hi {{first_name}}, floating this back up."},
                    {"step": 3, "delay_days": 5, "subject": "Re: Quick idea for {{company}}", "body": "Hi {{first_name}}, worth a 15-min look next week?"},
                ]
            }

    monkeypatch.setattr("sellbird.brain.Brain", lambda *a, **kw: _FakeDraftBrain())
    async def _fake_runtime():
        return (_FakeDraftBrain(), sm, dash._load_active_sellbird_config(), {}), None
    monkeypatch.setattr(dash, "_prepare_agentic_runtime", lambda: _fake_runtime())
    draft_res = client.post(
        "/api/campaigns/draft-steps",
        headers=headers,
        json={"name": "SaaS Push", "target_tags": "saas", "goal": "Book 15-min pipeline audit"},
    )
    assert draft_res.status_code == 200
    seq = draft_res.json()["sequence"]
    assert len(seq) == 3

    # Create campaign
    create_res = client.post(
        "/api/campaigns",
        headers=headers,
        json={
            "name": "SaaS Push",
            "target_tags": "saas",
            "framework": "PAS",
            "sequence": seq,
        },
    )
    assert create_res.status_code == 200
    cid = create_res.json()["id"]

    # Update campaign + toggle status + clone
    patch_res = client.patch(
        f"/api/campaigns/{cid}",
        headers=headers,
        json={
            "name": "SaaS Push v2",
            "target_tags": "saas",
            "sequence": [
                {"step": 1, "delay_days": 0, "subject": "Quick note for {{company}}", "body": "Hi {{first_name}}, at {{company}}."},
                {"step": 2, "delay_days": 4, "subject": "Re: Quick note for {{company}}", "body": "Hi {{first_name}}, following up."},
            ],
        },
    )
    assert patch_res.status_code == 200

    pause_res = client.post(f"/api/campaigns/{cid}/status", headers=headers, json={"status": "paused"})
    assert pause_res.status_code == 200 and pause_res.json()["status"] == "paused"

    clone_res = client.post(f"/api/campaigns/{cid}/clone", headers=headers)
    assert clone_res.status_code == 200
    clone_id = clone_res.json()["id"]

    # Launch original campaign -> only cp-match should stage 2 resolved drafts in Outbox
    launch_res = client.post(f"/api/campaigns/{cid}/launch", headers=headers, json={})
    assert launch_res.status_code == 200
    ldata = launch_res.json()
    assert ldata["queued_prospects"] == 1
    assert ldata["queued_emails"] == 2
    assert ldata["skipped"] >= 1

    outbox = client.get("/api/outbox", headers=headers).json()["pending"]
    assert len(outbox) == 2
    assert all(it["to_email"] == "nora@saasrocket.io" for it in outbox)
    assert all(it["unrendered_tags"] == [] for it in outbox)
    assert "SaaS Rocket" in outbox[0]["subject"]
    assert "Hi Nora, at SaaS Rocket." in outbox[0]["body"]

    # Campaign list carries outbox_counts
    camps = {c["id"]: c for c in client.get("/api/campaigns", headers=headers).json()}
    assert camps[cid]["name"] == "SaaS Push v2"
    assert camps[cid]["outbox_counts"]["pending_review"] == 2

    # Delete cloned campaign
    del_res = client.delete(f"/api/campaigns/{clone_id}", headers=headers)
    assert del_res.status_code == 200
    assert clone_id not in {c["id"] for c in client.get("/api/campaigns", headers=headers).json()}


def test_conversation_reply_queue_and_ai_draft(suite_env, monkeypatch):
    client, headers, sm, _ = suite_env

    async def _seed():
        await sm.add_prospect(
            Prospect(id="rp1", first_name="Tariq", email="tariq@client.io", company="Client Inc", email_status="verified")
        )
        await sm.add_conversation(
            Conversation(
                id="conv-1",
                prospect_id="rp1",
                status="open",
                stage="qualifying",
                thread=[Message(sender="prospect", content="Can you share pricing and a quick demo link?")],
            )
        )

    asyncio.run(_seed())

    q_res = client.post(
        "/api/conversations/conv-1/reply",
        headers=headers,
        json={"mode": "queue", "body": "Sure Tariq, happy to walk you through pricing tomorrow at 2pm."},
    )
    assert q_res.status_code == 200, q_res.text
    assert q_res.json()["success"] is True

    class _FakeReplyBrain:
        async def think_json(self, prompt, **kw):
            return {"body": "Happy to share pricing and our 15-min booking link here: https://cal.com/harvey/15min."}

    async def _fake_reply_runtime():
        return (_FakeReplyBrain(), sm, dash._load_active_sellbird_config(), {}), None

    monkeypatch.setattr(dash, "_prepare_agentic_runtime", lambda: _fake_reply_runtime())
    ai_res = client.post(
        "/api/conversations/conv-1/reply",
        headers=headers,
        json={"mode": "ai_draft", "instruction": "Share 15-min link cleanly"},
    )
    assert ai_res.status_code == 200, ai_res.text
    assert ai_res.json()["success"] is True
    assert "cal.com/harvey/15min" in ai_res.json()["body"]

    pending = client.get("/api/outbox", headers=headers).json()["pending"]
    reply_items = [it for it in pending if it["kind"] == "reply" and it["to_email"] == "tariq@client.io"]
    # Staging a new AI draft reply intentionally supersedes the earlier pending reply draft for that thread
    assert len(reply_items) == 1
    assert reply_items[0]["id"] == ai_res.json()["outbox_id"]


def test_frontend_suite_contracts_and_node_vm():
    html_text = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for marker in (
        'id="campaigns-funnel"',
        'id="writer-run-btn"',
        "triggerWriterRun(this)",
        "Write Emails Now",
        'id="new-campaign-btn"',
        'id="campaign-builder-dialog"',
        'id="ac-tags"',
    ):
        assert marker in html_text

    app_js = WEB_DIR / "app.js"
    syntax = subprocess.run(["node", "--check", str(app_js)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

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
  '/api/outbox': {
    sender_name: 'Ali Tariq',
    footer: '100 Market St · Reply stop to opt out.',
    pending: [
      { id: 'o1', to_email: 'mia@one.io', step: 1, kind: 'sequence', subject: 'Hi {{company}}', body: 'Hello {{first_name}}', unrendered_tags: ['company', 'first_name'], send_at: '2026-11-26T12:00:00Z' },
      { id: 'o2', to_email: 'leo@two.io', step: 1, kind: 'sequence', subject: 'Hello Two Co', body: 'Clean note for Leo.', unrendered_tags: [], send_at: '2026-11-26T12:05:00Z' },
    ],
    approved: [], sent: [], failed: [], mailbox_ready: false,
  },
  '/api/sending-mode': { mode: 'manual' },
  '/api/prospects': [
    { id: 'p1', first_name: 'Mia', last_name: 'One', email: 'mia@one.io', company: 'One Co', title: 'CEO', score: 88, status: 'new', source: 'manual', tags: 'saas, founder', created_at: '2026-11-26T10:00:00Z' },
  ],
  '/api/suppressions': { items: [] },
  '/api/frameworks': {},
  '/api/stats': { companies: 1, prospects: 1, outbox_pending: 2 },
  '/api/campaigns': [
    { id: 'c1', name: 'Launch SaaS', status: 'active', target_tags: 'saas', framework: 'PAS', prospect_ids: ['p1'], outbox_counts: { pending_review: 2, approved: 0, sent: 0 }, sequence: [{ step: 1, delay_days: 0, subject: 'Hi', body: 'Body' }], created_at: '2026-11-26T10:00:00Z' },
  ],
  '/api/conversations?awaiting=1': [
    { id: 'cv1', prospect_id: 'p1', prospect_email: 'mia@one.io', first_name: 'Mia', last_name: 'One', company: 'One Co', status: 'open', stage: 'qualifying', prospect_status: 'replied', messages: [{ role: 'prospect', body: 'Interested!' }] },
  ],
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
  await context.loadOutbox();
  const deskHtml = getEl('outbox-desk').innerHTML;
  if (!deskHtml.includes('desk-tag-warning') || !deskHtml.includes('{{company}}')) throw new Error('Missing unrendered tag guard in Outbox desk');
  if (!deskHtml.includes('desk-signoff') || !deskHtml.includes('Ali Tariq')) throw new Error('Missing sender sign-off preview');
  if (!deskHtml.includes('desk-check')) throw new Error('Missing Outbox bulk selection checkbox');
  context.outboxToggleSelectAll();
  if (!getEl('outbox-actions').innerHTML.includes('Approve selected 2')) throw new Error('Missing Outbox Approve selected button');

  await context.loadProspects();
  const contactsHtml = getEl('contact-rows').innerHTML;
  if (!contactsHtml.includes('contacts-table-fit') || !contactsHtml.includes('tag-pill')) throw new Error('Missing Contacts table tags');
  context.toggleContactSelect('p1', true);
  if (!getEl('contacts-bulk-bar').innerHTML.includes('1 selected')) throw new Error('Missing Contacts bulk bar');

  await context.loadCampaigns();
  const campHtml = getEl('campaigns-list').innerHTML;
  if (!campHtml.includes('Launch to Outbox') || !campHtml.includes('Outbox: 2 pending')) throw new Error('Missing Campaign launch & counts');
  context.openCampaignBuilderModal();
  context.addBuilderStep();
  if (!getEl('cb-steps').innerHTML.includes('Step 3')) throw new Error('Campaign builder failed to add Step 3');

  await context.loadConversations();
  const convHtml = getEl('conversations-list').innerHTML;
  if (!convHtml.includes('convo-reply-cv1') || !convHtml.includes('AI Draft Reply')) throw new Error('Missing Conversations reply composer');

  console.log('VM_EMAIL_SUITE_OK');
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
"""
    proc = subprocess.run(["node", "-e", vm_script, str(app_js)], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, f"VM test failed:\n{proc.stderr}\n{proc.stdout}"
    assert "VM_EMAIL_SUITE_OK" in proc.stdout
