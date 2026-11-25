"""Focused audit tests for Dashboard Backend, Security, Windows Runtime, and Frontend UI/CSS."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.state import StateManager
from sellbird.workspace_home import Home

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
        "persona:\n  company: Acme\nproduct:\n  name: Widget\nbrain:\n  provider: claude\nusage:\n  max_daily_usd: 10.0\n",
        encoding="utf-8",
    )
    brain_yaml = tmp_path / "brain.yaml"
    brain_yaml.write_text(
        """budget:
  max_usd_per_day: 10.0
chain:
  - provider: deepseek
    model: deepseek-chat
profiles:
  scout:
    model: deepseek-chat
    provider: deepseek
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
    monkeypatch.setattr(dash, "_sellbird_process", None)
    monkeypatch.setattr(dash, "_sellbird_process_new_group", False)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-ds")

    asyncio.run(StateManager(db_path=str(db)).init_db())
    token = dash.token_store.get()
    client = TestClient(dash.app, base_url=ORIGIN, headers={"Authorization": f"Bearer {token}"})
    return client, tmp_path, db, brain_yaml, cfg_file, env_file


# ── 1. Brain Studio ──


def test_brain_config_includes_spend_and_preserves_profiles_on_partial_update(env):
    client, _, db, _, _, _ = env
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_events (id, provider, model, agent, task, cost_usd, created_at) "
            "VALUES ('u1', 'deepseek', 'deepseek-chat', 'scout', 'score', 2.3456, datetime('now'))"
        )
        conn.commit()

    resp = client.get("/api/brain/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["spend"]["spend_today_usd"] == pytest.approx(2.3456, rel=1e-3)
    assert data["spend"]["max_daily_usd"] == 10.0

    # Partial update: only budget, omitting chain and profiles
    upd = client.post("/api/brain/config", json={"budget": {"max_usd_per_day": 25.0}})
    assert upd.status_code == 200
    assert upd.json()["budget"]["max_usd_per_day"] == 25.0
    assert upd.json()["profiles"]["scout"]["model"] == "deepseek-chat"

    reloaded = client.get("/api/brain/config").json()
    assert reloaded["budget"]["max_usd_per_day"] == 25.0
    assert reloaded["chain"][0]["provider"] == "deepseek"
    assert reloaded["profiles"]["scout"]["model"] == "deepseek-chat"


def test_brain_config_normalizes_aliases_and_activates_router(env):
    client, tmp_path, _, _, cfg_file, _ = env
    payload = {
        "budget": {"max_usd_per_day": 12.0},
        "activate_router": True,
        "chain": [
            {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "max_tokens": 1024,
                "timeout_s": 30,
                "headers": {"X-Test": "1"},
                "input_cost_per_1m": 0.14,  # non-harvey_brain alias: must be skipped
                "name": "primary",          # non-harvey_brain key: must be skipped
            }
        ],
        "profiles": {
            "writer": {
                "provider": "deepseek",
                "model": "deepseek-reasoner",
                "max_tokens": 2048,
                "timeout_s": 45,
                "api_key_env": "DEEPSEEK_API_KEY",  # not in TaskProfile: must be skipped
            }
        },
    }
    resp = client.post("/api/brain/config", json=payload)
    assert resp.status_code == 200, resp.text
    chain_entry = resp.json()["chain"][0]
    assert chain_entry["max_output_tokens"] == 1024
    assert chain_entry["timeout"] == 30
    assert chain_entry["extra_headers"] == {"X-Test": "1"}
    assert "input_cost_per_1m" not in chain_entry
    assert "name" not in chain_entry
    assert "max_tokens" not in chain_entry

    local_cfg = (tmp_path / "sellbird.local.yaml").read_text(encoding="utf-8")
    assert "harvey_brain.router_factory:build" in local_cfg


def test_brain_probe_supports_role_agent_and_domain(env):
    client, _, _, _, _, _ = env
    r1 = client.post("/api/brain/probe", json={"live": False, "agent": "researcher", "domain": "acme.com"})
    assert r1.status_code == 200
    assert r1.json()["role"] == "researcher"

    r2 = client.post("/api/brain/probe", json={"live": False, "role": "critic", "domain": "stripe.com"})
    assert r2.status_code == 200
    assert r2.json()["role"] == "critic"


# ── 2. Security: SSRF, Env Allowlist, Auth, Logger Redaction, Hosted Guards ──


@pytest.mark.parametrize(
    "bad_url,provider",
    [
        ("http://169.254.169.254/latest/meta-data", "openai_compat"),
        ("http://[::ffff:169.254.169.254]/v1", "ollama"),
        ("http://metadata.google.internal/v1", "openai_compat"),
        ("file:///etc/passwd", "openai_compat"),
        ("http://127.0.0.1:8080/v1", "openai"),
        ("http://localhost:11434/v1", "deepseek"),
    ],
)
def test_ssrf_base_url_blocked_in_chain_and_probe(env, bad_url, provider):
    client, _, _, _, _, _ = env
    r_cfg = client.post(
        "/api/brain/config",
        json={"chain": [{"provider": provider, "model": "m", "base_url": bad_url}]},
    )
    assert r_cfg.status_code == 400

    r_probe = client.post(
        "/api/brain/probe",
        json={"live": True, "provider": provider, "model": "m", "base_url": bad_url},
    )
    assert r_probe.status_code == 400


def test_loopback_allowed_for_local_inference_provider(env):
    client, _, _, _, _, _ = env
    r_cfg = client.post(
        "/api/brain/config",
        json={"chain": [{"provider": "ollama", "model": "llama3.1", "base_url": "http://127.0.0.1:11434/v1"}]},
    )
    assert r_cfg.status_code == 200


@pytest.mark.parametrize("bad_env", ["SMTP_PASSWORD", "AWS_SECRET_ACCESS_KEY", "DASHBOARD_TOKEN"])
def test_api_key_env_allowlist_blocks_secret_exfiltration(env, bad_env):
    client, _, _, _, _, _ = env
    r_cfg = client.post(
        "/api/brain/config",
        json={"chain": [{"provider": "openai_compat", "model": "m", "api_key_env": bad_env}]},
    )
    assert r_cfg.status_code == 400

    r_probe = client.post(
        "/api/brain/probe",
        json={"live": True, "provider": "openai_compat", "model": "m",
              "base_url": "https://api.openai.com/v1", "api_key_env": bad_env},
    )
    assert r_probe.status_code == 400


def test_calcom_webhook_bypasses_browser_provenance_but_requires_loopback_host(env):
    _, tmp_path, _, _, _, _ = env
    anon = TestClient(dash.app, base_url=ORIGIN)
    # Cross-origin headers from webhook relay/browser must NOT trigger 403 cross_origin
    resp = anon.post(
        "/api/webhooks/calcom",
        content=b'{"triggerEvent":"BOOKING_CREATED"}',
        headers={
            "Origin": "https://app.cal.com",
            "Sec-Fetch-Site": "cross-site",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code != 403
    assert resp.status_code in (400, 401, 503)

    # Non-loopback Host header must still be denied
    bad_host = anon.post(
        "/api/webhooks/calcom",
        content=b"{}",
        headers={"Host": "evil.example", "Origin": "https://app.cal.com"},
    )
    assert bad_host.status_code == 400


def test_env_file_quoting_parsing_and_setup_status_whitespace(env, monkeypatch):
    client, _, _, _, _, env_file = env
    monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
    monkeypatch.delenv("LINKEDIN_EMAIL", raising=False)
    monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    env_file.write_text(
        'export SMTP_HOST="mail.example.com"\n'
        'INSTANTLY_API_KEY="   "\n'
        'LINKEDIN_EMAIL="  "\n'
        'LINKEDIN_PASSWORD=pw\n'
        'SMTP_PASSWORD=unquoted # inline comment\n',
        encoding="utf-8",
    )
    parsed = dash._read_env_file()
    assert parsed["SMTP_HOST"] == "mail.example.com"
    assert parsed["SMTP_PASSWORD"] == "unquoted"

    status = client.get("/api/setup-status").json()
    checks = {c["id"]: c for c in status["checks"]}
    assert checks["email_provider"]["done"] is False
    assert checks["linkedin"]["done"] is False

    # Writing value containing '#' and spaces quotes it cleanly for round-trip
    r = client.post("/api/settings/env", json={"SMTP_PASSWORD": "p@ss # word"})
    assert r.json()["success"] is True
    assert 'SMTP_PASSWORD="p@ss # word"' in env_file.read_text(encoding="utf-8")
    assert dash._read_env_file()["SMTP_PASSWORD"] == "p@ss # word"


def test_logger_redacts_secret_query_params(caplog):
    records = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = CaptureHandler()
    dash.logger.addHandler(handler)
    dash._attach_secret_redaction()
    try:
        dash.logger.warning("GET https://api.reoon.com/check?api_key=supersecret123&email=a@b.com")
    finally:
        dash.logger.removeHandler(handler)

    assert any("api_key=***" in r and "supersecret123" not in r for r in records)


def test_local_only_routes_return_409_on_hosted_home(tmp_path):
    class Open:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)

    from starlette.middleware import Middleware

    built = dash.build_app(guard=Middleware(Open))
    built.state.home = Home.for_root(tmp_path, mode="hosted")
    c = TestClient(built)

    for method, path in (
        ("GET", "/api/sellbird/status"),
        ("POST", "/api/sellbird/start"),
        ("POST", "/api/sellbird/stop"),
        ("GET", "/api/sellbird/logs"),
        ("POST", "/api/settings/env"),
        ("POST", "/api/discover/run"),
        ("POST", "/api/profile/run"),
    ):
        resp = c.request(method, path, json={})
        assert resp.status_code == 409, f"{method} {path} returned {resp.status_code}"
        assert resp.json()["error"] == "not_on_hosted"


# ── 3. Clay AI Columns, Research, Prospects, Settings ──


def test_enrichment_columns_field_mapping_and_entity_id_values(env):
    client, _, db, _, _, _ = env
    resp = client.post(
        "/api/enrichment/columns",
        json={
            "name": "Pricing Model",
            "key": "pricing_model",
            "prompt": "Find pricing model",
            "output_type": "boolean",
            "use_web_search": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "pricing_model"
    assert body["key"] == "pricing_model"
    assert body["column"]["output_type"] == "boolean"
    assert body["column"]["value_type"] == "boolean"

    # Insert value using StateManager (which stores entity_id)
    async def _set_val():
        st = StateManager(db_path=str(db))
        await st.set_enrichment_value("pricing_model", entity_id="comp_1", value="true")

    asyncio.run(_set_val())
    listed = client.get("/api/enrichment/columns").json()
    col = listed["columns"][0]
    assert col["key"] == "pricing_model"
    assert col["output_type"] == "boolean"
    assert col["value_type"] == "boolean"
    assert listed["values"]["comp_1"]["pricing_model"]["value"] == "true"


def test_research_run_targets_domain_even_when_limit_present_and_seeds_signals(env, monkeypatch):
    client, _, db, _, _, _ = env
    calls = []

    class FakeResearcher:
        def __init__(self, state, brain, config=None, env=None):
            self.state = state

        async def research_domain(self, domain):
            calls.append(("domain", domain))
            return {"ok": True, "domain": domain}

        async def run(self, companies=None, limit=5):
            calls.append(("run", companies, limit))
            return []

    import sellbird.agents.researcher as res_mod
    monkeypatch.setattr(res_mod, "DeepResearcher", FakeResearcher)
    monkeypatch.setattr(dash, "_offer_brain", lambda: asyncio.sleep(0, result=(SimpleNamespace(), StateManager(str(db)), None)))

    resp = client.post("/api/research/run", json={"domain": "stripe.com", "limit": 5})
    assert resp.status_code == 200
    assert calls == [("domain", "stripe.com")]

    # Signal catalog must have been seeded
    with sqlite3.connect(db) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM signal_codes WHERE code = 'REGISTRY_VERIFIED'").fetchone()[0]
    assert cnt == 1


def test_prospects_endpoint_returns_requested_prospect_outside_top_200(env):
    client, _, db, _, _, _ = env
    with sqlite3.connect(db) as conn:
        for i in range(205):
            ts = f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}"
            conn.execute(
                "INSERT INTO prospects (id, first_name, email, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (f"p_{i}", "First", f"u{i}@example.com", "opted_out" if i == 0 else "new", ts),
            )
        conn.commit()

    # p_0 is oldest, outside top 200
    plain = client.get("/api/prospects").json()
    assert all(p["id"] != "p_0" for p in plain)

    targeted = client.get("/api/prospects?prospect_id=p_0").json()
    assert any(p["id"] == "p_0" for p in targeted)


def test_test_instantly_uses_saved_env_key_when_input_empty(env, monkeypatch):
    client, _, _, _, _, env_file = env
    env_file.write_text("INSTANTLY_API_KEY=saved-instantly-key\n", encoding="utf-8")
    seen = {}

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            seen["auth"] = (headers or {}).get("Authorization")
            return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    resp = client.post("/api/settings/test-instantly", json={"api_key": ""})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert seen["auth"] == "Bearer saved-instantly-key"


# ── 4. Windows Runtime Process Control ──


def test_start_sellbird_passes_safe_flags_env_and_checks_immediate_exit(env, monkeypatch):
    client, tmp_path, _, _, _, env_file = env
    launched = {}

    class RunningChild:
        pid = 9191

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        launched["cmd"] = cmd
        launched["env"] = kwargs.get("env")
        return RunningChild()

    monkeypatch.setattr(dash.subprocess, "Popen", fake_popen)
    orig_sleep = asyncio.sleep
    monkeypatch.setattr(dash.asyncio, "sleep", lambda s: orig_sleep(0))

    # Missing .env -> refuses to start via _sellbird_needs_setup()
    r_missing = client.post("/api/sellbird/start")
    assert r_missing.json()["success"] is False

    env_file.write_text("DEEPSEEK_API_KEY=k\n", encoding="utf-8")
    r = client.post("/api/sellbird/start")
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert launched["cmd"][1:] == ["-m", "sellbird", "run"]
    assert launched["env"]["PYTHONSAFEPATH"] == "1"
    assert launched["env"]["SELLBIRD_HOME"] == str(tmp_path)
    assert launched["env"]["PYTHONUNBUFFERED"] == "1"

    # Immediate exit is detected via poll()
    monkeypatch.setattr(dash, "_sellbird_process", None)
    monkeypatch.setattr(dash, "_sellbird_process_new_group", False)

    class DeadChild:
        pid = 9192

        def poll(self):
            return 2

    monkeypatch.setattr(dash.subprocess, "Popen", lambda *a, **kw: DeadChild())
    r2 = client.post("/api/sellbird/start")
    assert r2.status_code == 200
    assert r2.json()["success"] is False
    assert "exited immediately" in r2.json()["message"]


def test_windows_stop_sellbird_sends_ctrl_break_first_then_fallback(env, monkeypatch):
    client, tmp_path, _, _, _, _ = env
    monkeypatch.setattr(dash.sys, "platform", "win32")
    orig_sleep = asyncio.sleep
    monkeypatch.setattr(dash.asyncio, "sleep", lambda s: orig_sleep(0))
    ctrl_break = getattr(dash.signal, "CTRL_BREAK_EVENT", 1)
    monkeypatch.setattr(dash.signal, "CTRL_BREAK_EVENT", ctrl_break, raising=False)

    sent = []

    class Child:
        pid = 5151

        def __init__(self):
            self.alive = True

        def poll(self):
            return None if self.alive else 0

    child = Child()
    monkeypatch.setattr(dash, "_sellbird_process", child)
    monkeypatch.setattr(dash, "_sellbird_process_new_group", True)

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        if sig == ctrl_break:
            child.alive = False

    monkeypatch.setattr(dash.os, "kill", fake_kill)
    resp = client.post("/api/sellbird/stop")
    assert resp.json()["success"] is True
    assert sent == [(5151, ctrl_break)]


# ── 5. Frontend JS & CSS Audit Checks ──


def test_app_js_runtime_behaviors_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # Ensure case 'brain' is NOT in the 15s auto-refresh loop (while still in loadCurrentTab)
    assert "case 'brain': loadBrainStudio(); break;" in app_js
    assert app_js.count("case 'brain':") == 1

    # Verify key functions via Node VM execution
    script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');

const elements = {};
function makeEl(id, initial={}) {
  return Object.assign({
    id, value: '', checked: false, disabled: false, textContent: '', innerHTML: '',
    className: '', style: {}, dataset: {},
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
    closest: () => null,
    classList: { add: () => {}, remove: () => {} },
    showModal: () => {}, close: () => {},
  }, initial);
}

for (const id of [
  'probe-role', 'probe-domain', 'probe-btn', 'probe-output',
  'brain-doctor-btn', 'brain-doctor-status', 'brain-doctor-results',
  'brain-spend-usd', 'brain-spend-cap', 'brain-max-usd', 'brain-budget-bar',
  'brain-budget-status', 'brain-activate-router', 'brain-chain-container', 'brain-roles-grid',
  'clay-column-list', 'enrichment-run-btn', 'enrichment-run-status',
  'col-name', 'col-key', 'col-prompt', 'col-type', 'col-web-search',
  'today-queue', 'nav-today', 'nav-outbox', 'nav-signals', 'nav-conversations', 'nav-company',
  'header-dot', 'header-status-text', 'control-dot', 'control-label', 'control-meta',
  'live-status', 'today-renamed', 'today-kpis', 'today-facts', 'today-figures',
  'today-runs', 'today-runs-block', 'today-activity', 'today-setup',
  'log-viewer', 'btn-stop', 'btn-start', 'cw-enabled', 'cw-watches', 'cw-status'
]) {
  elements[id] = makeEl(id);
}
elements['probe-role'].value = 'scout';
elements['probe-domain'].value = 'stripe.com';
elements['col-type'].value = 'text';

const apiCalls = [];
let nextApiResult = null;
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {},
  confirm: () => true,
  matchMedia: () => ({ matches: true }),
  document: {
    hidden: false,
    documentElement: {
      attrs: {},
      getAttribute(k) { return this.attrs[k] ?? null; },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      removeAttribute(k) { delete this.attrs[k]; },
    },
    body: { appendChild: () => {} },
    getElementById: (id) => elements[id] || null,
    querySelectorAll: () => [],
    createElement: (tag) => makeEl(tag),
    addEventListener: () => {},
  },
  localStorage: { store: {}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = v; }, removeItem(k) { delete this.store[k]; } },
  window: { location: { origin: 'http://127.0.0.1:5555', pathname: '/', hash: '' }, matchMedia: () => ({ matches: true }) },
  authFetch: async () => ({ ok: true, json: async () => ({}) }),
  lockIfRefused: async () => false,
};
sandbox.globalThis = sandbox;
vm.runInNewContext(code, sandbox);

// Override api() after loading
sandbox.api = async (path, opts) => {
  apiCalls.push({ path, opts });
  return typeof nextApiResult === 'function' ? nextApiResult(path, opts) : nextApiResult;
};

(async () => {
  // 1. toggleOledTheme respects prefers-color-scheme: dark when data-theme is unset
  sandbox.document.documentElement.removeAttribute('data-theme');
  sandbox.toggleOledTheme();
  if (sandbox.document.documentElement.getAttribute('data-theme') !== 'light') {
    throw new Error('Expected OLED toggle in dark OS mode to switch to light');
  }

  // 2. runBrainProbe sends { live: true, role, prompt } and handles !data
  nextApiResult = null;
  await sandbox.runBrainProbe();
  const probeCall = apiCalls.find(c => c.path === '/api/brain/probe');
  const probeBody = JSON.parse(probeCall.opts.body);
  if (probeBody.live !== true || probeBody.role !== 'scout' || !probeBody.prompt) {
    throw new Error('Bad probe payload: ' + JSON.stringify(probeBody));
  }
  if (!elements['probe-output'].textContent.includes('Error')) {
    throw new Error('Expected clear error on !data');
  }
  if (elements['probe-btn'].disabled !== false) {
    throw new Error('Expected probe-btn re-enabled');
  }

  // 3. runBrainDoctor uses ?network=1, escapes error, no trailing middot when base_url absent
  nextApiResult = {
    summary: '0/1 provider(s) ready',
    providers: [{ provider: 'deepseek', model: 'deepseek-chat', base_url: '', ok: false, error: '<img src=x onerror=alert(1)>', latency_ms: 12.3 }],
  };
  await sandbox.runBrainDoctor();
  const docCall = apiCalls.find(c => c.path.startsWith('/api/brain/doctor'));
  if (docCall.path !== '/api/brain/doctor?network=1') {
    throw new Error('Doctor must call ?network=1, got ' + docCall.path);
  }
  const docHtml = elements['brain-doctor-results'].innerHTML;
  if (docHtml.includes('<img') || docHtml.includes('deepseek-chat &middot; <')) {
    throw new Error('XSS or trailing middot in doctor output: ' + docHtml);
  }

  // 4. loadBrainStudio marks budget exhausted
  nextApiResult = {
    budget: { max_usd_per_day: 5.0 },
    spend: { spend_today_usd: 5.5, max_daily_usd: 5.0 },
    chain: [{ provider: 'unknown_prov', model: 'm1' }],
    profiles: { scout: { provider: 'deepseek', model: 'deepseek-chat' } },
    transport_active: true,
  };
  await sandbox.loadBrainStudio();
  if (elements['brain-budget-status'].textContent !== 'Budget Exhausted') {
    throw new Error('Expected Budget Exhausted, got ' + elements['brain-budget-status'].textContent);
  }
  if (!elements['brain-chain-container'].innerHTML.includes('value="unknown_prov"')) {
    throw new Error('Unknown provider was not preserved in renderBrainChain');
  }

  // 5. loadEnrichment uses jsq for deleteEnrichmentCol
  nextApiResult = {
    columns: [{ id: "bad'id", key: "bad'id", name: 'Col', prompt: 'P', output_type: 'text' }],
  };
  await sandbox.loadEnrichment();
  if (elements['clay-column-list'].innerHTML.includes("deleteEnrichmentCol('bad'id')")) {
    throw new Error('Unsafe inline quote in deleteEnrichmentCol');
  }

  // 6. loadToday renders precleared-hold url safely
  nextApiResult = {
    items: [{ key: 'precleared-hold', tone: 'bad', title: 'Hold', detail: 'Details', action: 'Open log', tab: 'controls', url: 'https://precleared.example/billing', url_label: 'Add credits' }],
    stats: {},
  };
  await sandbox.loadToday();
  if (!elements['today-queue'].innerHTML.includes('https://precleared.example/billing') || !elements['today-queue'].innerHTML.includes('Add credits')) {
    throw new Error('Expected precleared-hold link rendered in Today');
  }

  // 7. loadLogs preserves existing logs on offline (!data)
  elements['log-viewer'].textContent = 'existing line';
  elements['log-viewer'].dataset.hasLogs = '1';
  nextApiResult = null;
  await sandbox.loadLogs();
  if (elements['log-viewer'].textContent !== 'existing line') {
    throw new Error('Expected existing logs preserved on offline error');
  }

  console.log('NODE_OK');
})();
"""
    proc = subprocess.run(
        [node, "-e", script, str(WEB_DIR / "app.js")],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "NODE_OK" in proc.stdout


def test_css_and_html_audit_fixes():
    css = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    assert ".toast.info" in css
    assert "--surface-solid:" in css
    assert "background: var(--surface-solid" in css
    assert "oled-icon-sun" in html and "oled-icon-moon" in html
    assert 'id="brain-activate-router"' in html
    assert 'id="col-web-search"' in html
