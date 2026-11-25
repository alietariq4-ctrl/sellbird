"""Tests for the 5 Dashboard & Full-Stack Feature Upgrades."""

from __future__ import annotations

import asyncio
import csv
import io
import shutil
import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "sellbird" / "web"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "sellbird.db"
    env_file = tmp_path / ".env"
    cfg_file = tmp_path / "sellbird.yaml"
    cfg_file.write_text(
        "persona:\n  company: Acme\nproduct:\n  name: Widget\n"
        "compliance:\n  do_not_contact:\n    - dnc.example\n    - blocked@partner.example\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dash, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dash, "ENV_FILE", env_file)
    monkeypatch.setattr(dash, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "sellbird.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "sellbird.log")
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: cfg_file)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")

    asyncio.run(StateManager(db_path=str(db)).init_db())
    token = dash.token_store.get()
    client = TestClient(dash.app, base_url=ORIGIN, headers={"Authorization": f"Bearer {token}"})
    return client, tmp_path, db, cfg_file, env_file


# ── 1. Clay AI Matrix Metadata & CSV Export ──


def test_enrichment_columns_returns_companies_and_exports_safe_csv(env):
    client, _, db, _, _ = env

    async def _seed():
        sm = StateManager(db_path=str(db))
        col_id = await sm.add_enrichment_column(
            "Pricing Model", "Check pricing page", key="pricing_model", output_format="text"
        )
        # Seed 55 companies: the oldest one (c_old) is referenced in enrichment_values
        # and must still be returned alongside up to 50 recent companies.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO companies (id, name, domain, industry, created_at) "
                "VALUES (?, ?, ?, ?, datetime('2020-01-01'))",
                ("c_old", "=CMD|'/C calc'!A0", "old.example", "SaaS"),
            )
            for i in range(52):
                conn.execute(
                    "INSERT INTO companies (id, name, domain, industry, created_at) "
                    "VALUES (?, ?, ?, ?, datetime('2026-01-01', ?))",
                    (f"c_{i}", f"Company {i}", f"co{i}.example", "Tech", f"+{i} minutes"),
                )
            conn.commit()
        await sm.set_enrichment_value(
            col_id, "c_old", "+100/seat", source_url="https://old.example/pricing"
        )
        await sm.set_enrichment_value(
            col_id, "c_51", "Enterprise custom", source_url="https://co51.example/pricing"
        )
        return col_id

    col_id = asyncio.run(_seed())

    resp = client.get("/api/enrichment/columns")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["columns"]) == 1
    assert payload["values"]["c_old"][col_id]["value"] == "+100/seat"
    assert payload["values"]["c_old"][col_id]["source_url"] == "https://old.example/pricing"
    assert "companies" in payload
    assert "c_old" in payload["companies"]
    assert payload["companies"]["c_old"] == {
        "id": "c_old",
        "name": "=CMD|'/C calc'!A0",
        "domain": "old.example",
        "industry": "SaaS",
    }
    assert len(payload["companies"]) <= 51

    csv_resp = client.get("/api/export/enrichment.csv")
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    assert "enrichment-matrix.csv" in csv_resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(csv_resp.text)))
    assert rows[0] == ["Company", "Domain", "Industry", "Pricing Model"]
    old_row = next(r for r in rows[1:] if r[1] == "old.example")
    # Formula injection neutralization on both company name and enriched cell
    assert old_row[0] == "'=CMD|'/C calc'!A0"
    assert old_row[2] == "SaaS"
    assert old_row[3] == "'+100/seat"


# ── 2. Contacts Tab: CSV Lead Importer ──


def test_prospects_import_validation_suppression_dedup_and_company_linking(env):
    client, _, db, _, _ = env

    # Empty / missing / header-only CSV returns 400
    assert client.post("/api/prospects/import", json={}).status_code == 400
    assert client.post("/api/prospects/import", json={"csv_text": "   "}).status_code == 400
    assert client.post("/api/prospects/import", json={"csv_text": "email,first_name\n"}).status_code == 400

    async def _seed_existing():
        sm = StateManager(db_path=str(db))
        await sm.add_suppression("address", "suppressed@clean.example", reason="manual")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO prospects (id, first_name, last_name, email, title, status) "
                "VALUES ('existing_1', 'Existing', 'User', 'dup@acme.example', 'CEO', 'new')"
            )
            conn.commit()

    asyncio.run(_seed_existing())

    csv_text = "\n".join([
        "full_name,email,role,company_name,website,linkedin_url,industry,location,email_status",
        "Jane Doe,jane@acme.example,VP Sales,Acme Corp,https://www.acme.example/about,https://linkedin.com/in/janedoe,SaaS,Austin,verified",
        "Bob Smith,bob@beta.example,CTO,Beta Labs,beta.example,,,New York,",
        "Dup User,DUP@acme.example,CEO,Acme Corp,acme.example,,,,",
        "Dup Batch,jane@acme.example,VP Sales,Acme Corp,acme.example,,,,",
        "DNC Domain,someone@dnc.example,CEO,DNC Co,dnc.example,,,,",
        "DNC Email,blocked@partner.example,CEO,Partner,partner.example,,,,",
        "DB Suppressed,suppressed@clean.example,CEO,Clean Co,clean.example,,,,",
        "Bad Email,not-an-email,CEO,Bad Co,bad.example,,,,",
        "Missing Title,notitle@valid.example,,Valid Co,valid.example,,,,",
    ])
    resp = client.post("/api/prospects/import", json={"csv_text": csv_text})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data == {
        "success": True,
        "imported": 2,
        "skipped_duplicate": 2,
        "skipped_suppressed": 3,
        "skipped_invalid": 2,
    }

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        jane = conn.execute("SELECT * FROM prospects WHERE email = 'jane@acme.example'").fetchone()
        bob = conn.execute("SELECT * FROM prospects WHERE email = 'bob@beta.example'").fetchone()
        assert jane is not None
        assert jane["first_name"] == "Jane"
        assert jane["last_name"] == "Doe"
        assert jane["title"] == "VP Sales"
        assert jane["source"] == "csv_import"
        assert jane["email_status"] == "verified"
        assert jane["email_verified"] == 1
        assert jane["company_id"] != ""

        assert bob is not None
        assert bob["email_status"] == "guess"
        assert bob["email_verified"] == 0

        acme_co = conn.execute("SELECT * FROM companies WHERE id = ?", (jane["company_id"],)).fetchone()
        assert acme_co["domain"] == "acme.example"
        assert acme_co["name"] == "Acme Corp"


def test_prospects_import_rejects_over_2000_rows(env):
    client, _, _, _, _ = env
    lines = ["first_name,last_name,email,title"]
    lines.extend(f"F{i},L{i},u{i}@example.com,VP" for i in range(2001))
    resp = client.post("/api/prospects/import", json={"csv_text": "\n".join(lines)})
    assert resp.status_code == 400


# ── 3. Settings: Cal.com Webhook Secret & Live Integration Testing ──


def test_calcom_webhook_secret_settings_roundtrip(env):
    client, _, _, _, _ = env
    r0 = client.get("/api/settings").json()
    assert r0["calcom_webhook_secret_set"] is False

    save = client.post("/api/settings/env", json={"CALCOM_WEBHOOK_SECRET": "whsec_test_123"})
    assert save.status_code == 200
    assert save.json()["success"] is True

    r1 = client.get("/api/settings").json()
    assert r1["calcom_webhook_secret_set"] is True
    assert "whsec_test_123" not in str(r1)


@pytest.mark.parametrize(
    "service,extra",
    [
        ("twenty", {"twenty_base_url": "http://127.0.0.1:8080", "twenty_api_key": "k"}),
        ("twenty", {"twenty_base_url": "http://169.254.169.254/latest", "twenty_api_key": "k"}),
        ("slack", {"slack_webhook_url": "https://evil.example/webhook"}),
        ("slack", {"slack_webhook_url": "http://hooks.slack.com/services/T/B/X"}),
    ],
)
def test_integrations_test_blocks_ssrf_and_bad_slack_prefix(env, service, extra):
    client, _, _, _, _ = env
    resp = client.post("/api/integrations/test", json={"service": service, **extra})
    assert resp.status_code == 400


def test_integrations_test_live_services_and_env_fallback(env, monkeypatch):
    client, _, _, _, env_file = env
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=tg_env_tok\n"
        "CALCOM_API_KEY=cal_env_key\n"
        "TWENTY_BASE_URL=https://api.twenty.com\n"
        "TWENTY_API_KEY=tw_env_key\n"
        "SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T00/B00/XXX\n",
        encoding="utf-8",
    )

    calls = []

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls.append(("GET", url, headers or {}))
            if "api.telegram.org" in url:
                return httpx.Response(200, json={"ok": True, "result": {"username": "sellbird_bot"}})
            if "api.cal.com/v2/me" in url:
                return httpx.Response(200, json={"data": {"id": 1}})
            if "/rest/people" in url:
                return httpx.Response(200, json={"data": {"people": []}})
            return httpx.Response(404)

        async def post(self, url, json=None):
            calls.append(("POST", url, json))
            return httpx.Response(200, text="ok")

    async def fake_validate_public(url):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr("sellbird.integrations.stealth_fetch.validate_public_url", fake_validate_public)

    # Telegram
    r_tg = client.post("/api/integrations/test", json={"service": "telegram"})
    assert r_tg.status_code == 200
    assert r_tg.json()["success"] is True
    assert r_tg.json()["username"] == "sellbird_bot"

    # Cal.com
    r_cal = client.post("/api/integrations/test", json={"service": "calcom"})
    assert r_cal.status_code == 200
    assert r_cal.json()["success"] is True

    # Twenty CRM
    r_tw = client.post("/api/integrations/test", json={"service": "twenty"})
    assert r_tw.status_code == 200
    assert r_tw.json()["success"] is True

    # Slack without send_test: does NOT POST
    pre_count = len(calls)
    r_sl_dry = client.post("/api/integrations/test", json={"service": "slack", "send_test": False})
    assert r_sl_dry.status_code == 200
    assert r_sl_dry.json()["success"] is True
    assert len(calls) == pre_count

    # Slack with send_test=True: POSTs test ping
    r_sl_ping = client.post("/api/integrations/test", json={"service": "slack", "send_test": True})
    assert r_sl_ping.status_code == 200
    assert r_sl_ping.json()["success"] is True
    assert calls[-1][0] == "POST"
    assert calls[-1][2] == {"text": "Sellbird integration test"}


# ── 4. Frontend HTML, CSS Responsive Nav, & JS Runtime Verification ──


def test_html_and_css_feature_upgrades():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    css = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    # Feature 1: Clay matrix
    assert 'id="clay-data-matrix"' in html
    assert "/api/export/enrichment.csv" in html
    # Feature 2: Companies batch deep research
    assert "runBatchResearch" in html
    assert "Deep Research (Top 5)" in html
    # Feature 3: Contacts CSV import dialog
    assert 'id="import-csv-dialog"' in html
    assert "openImportCsvModal()" in html
    # Feature 4: Cal.com webhook secret & live integration test UI
    assert 'id="calcom-webhook-secret"' in html
    assert 'id="integration-test-service"' in html
    assert 'id="integration-test-result"' in html
    # Feature 5: Usage copy updated
    assert "no dollar costs here" not in html
    # Feature 5: Responsive nav fix covering 760-1000px without negative margin clipping
    assert "@media (max-width: 1000px)" in css
    assert "justify-content: flex-start" in css
    assert "margin: 0 calc(-1 * var(--sp-4))" not in css


def test_upgrade_js_runtime_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');

const elements = {};
function makeEl(id, initial = {}) {
  return Object.assign({
    id, value: '', checked: false, disabled: false, textContent: '', innerHTML: '',
    className: '', style: {}, dataset: {},
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
  'clay-column-list', 'clay-matrix-table', 'enrichment-run-btn', 'enrichment-run-status',
  'usage-quota', 'usage-stats', 'usage-daily', 'usage-tables',
  'calcom-key', 'calcom-event-type-id', 'calcom-webhook-secret',
  'twenty-url', 'twenty-key', 'telegram-token', 'telegram-chat', 'slack-webhook',
  'integration-test-service', 'integration-test-ping', 'integration-test-result',
  'import-csv-file', 'import-csv-text', 'import-csv-dialog',
  'today-queue', 'today-renamed', 'today-kpis', 'today-facts', 'today-figures',
  'today-runs', 'today-runs-block', 'today-activity', 'today-setup', 'live-status',
  'nav-today', 'nav-outbox', 'nav-signals', 'nav-conversations', 'nav-company',
  'header-dot', 'header-status-text', 'control-dot', 'control-label', 'control-meta',
  'log-viewer', 'btn-start', 'btn-stop', 'cw-enabled', 'cw-watches', 'cw-status'
]) {
  elements[id] = makeEl(id);
}

const apiCalls = [];
let nextApiResult = null;
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
  return typeof nextApiResult === 'function' ? nextApiResult(path, opts) : nextApiResult;
};
sandbox.loadSettings = async () => {};

(async () => {
  // 1. Clay AI matrix + Run Column + XSS-safe source_url
  nextApiResult = {
    columns: [{ id: 'col1', key: 'pricing', name: 'Pricing', prompt: 'P', output_type: 'text' }],
    companies: { c1: { id: 'c1', name: 'Acme', domain: 'acme.example', industry: 'SaaS' } },
    values: {
      c1: {
        col1: { value: '$99/mo', source_url: 'https://acme.example/pricing' }
      }
    }
  };
  await sandbox.loadEnrichment();
  if (!elements['clay-column-list'].innerHTML.includes('runEnrichmentColumn(')) {
    throw new Error('Missing Run Column button');
  }
  const mHtml = elements['clay-matrix-table'].innerHTML;
  if (!mHtml.includes('$99/mo') || !mHtml.includes('https://acme.example/pricing') || !mHtml.includes('runCompanyEnrichment(')) {
    throw new Error('Matrix missing value, source_url, or Run button: ' + mHtml);
  }

  // 2. companyInfoCard actions + REGISTRY_VERIFIED dossier
  const cardHtml = sandbox.companyInfoCard(
    { id: 'c1', name: 'Acme', domain: 'acme.example', fit: 90 },
    {
      company: { id: 'c1', name: 'Acme', domain: 'acme.example' },
      observations: [
        { signal_code: 'REGISTRY_VERIFIED', value_text: 'SOC2 Type II certified', evidence_url: 'https://acme.example/security' }
      ]
    }
  );
  if (!cardHtml.includes('runCompanyResearch(') || !cardHtml.includes('runCompanyEnrichment(') || !cardHtml.includes('SOC2 Type II certified')) {
    throw new Error('companyInfoCard missing research/enrichment buttons or dossier: ' + cardHtml);
  }

  // 3. Usage tab USD cost rendering when cost_usd > 0
  nextApiResult = {
    transport_active: true,
    totals: {
      today: { calls: 4, input_tokens: 1000, output_tokens: 500, cache_read_tokens: 0, cost_usd: 0.0125 },
      week: { calls: 10, input_tokens: 3000, output_tokens: 1500, cache_read_tokens: 0, cost_usd: 0.25 },
      month: { calls: 20, input_tokens: 6000, output_tokens: 3000, cache_read_tokens: 0, cost_usd: 0.50 },
    },
    by_day: [{ day: '2026-11-25', calls: 4, output_tokens: 500, cost_usd: 0.0125 }],
    by_provider: [{ provider: 'deepseek', calls: 4, input_tokens: 1000, output_tokens: 500, cache_read_tokens: 0, cost_usd: 0.0125 }],
    by_agent: [{ agent: 'scout', calls: 4, input_tokens: 1000, output_tokens: 500, cache_read_tokens: 0, cost_usd: 0.0125 }],
    by_task: [],
    by_model: [],
  };
  await sandbox.loadUsage();
  if (!elements['usage-stats'].innerHTML.includes('$0.0125') || !elements['usage-tables'].innerHTML.includes('Cost (USD)')) {
    throw new Error('Usage tab did not render USD costs');
  }

  // 4. Integration test + saveIntegrations includes CALCOM_WEBHOOK_SECRET
  elements['calcom-webhook-secret'].value = 'sec_123';
  nextApiResult = { success: true };
  sandbox.saveIntegrations();
  const envCall = apiCalls.find(c => c.path === '/api/settings/env');
  if (!envCall || JSON.parse(envCall.opts.body).CALCOM_WEBHOOK_SECRET !== 'sec_123') {
    throw new Error('saveIntegrations did not include CALCOM_WEBHOOK_SECRET');
  }

  elements['integration-test-service'].value = 'telegram';
  elements['telegram-token'].value = 'tg_tok';
  nextApiResult = { success: true, message: 'Connected to Telegram (@testbot).' };
  await sandbox.testIntegrationConnection();
  if (!elements['integration-test-result'].innerHTML.includes('Connected')) {
    throw new Error('testIntegrationConnection did not update result');
  }

  console.log('UPGRADE_NODE_OK');
})();
"""
    proc = subprocess.run(
        [node, "-e", script, str(WEB_DIR / "app.js")],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "UPGRADE_NODE_OK" in proc.stdout
