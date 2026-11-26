"""Tests for Today Agentic Workbench, /api/scout/run, /api/agent/cycle, and /api/today readiness."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.models.company import Company
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
brain:
  transport_factory: harvey_brain.router_factory:build
usage:
  max_daily_usd: 10.0
  quiet_hours:
    start: "00:00"
    end: "23:59"
    timezone: UTC
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
    with TestClient(dash.app, base_url=ORIGIN, headers={"Authorization": f"Bearer {token}"}) as client:
        yield client, tmp_path, db


def test_today_includes_agentic_readiness_and_quiet_hours_flag(env):
    client, _, _ = env
    r = client.get("/api/today")
    assert r.status_code == 200
    data = r.json()
    ag = data.get("agentic")
    assert isinstance(ag, dict)
    assert ag["brain_ready"] is True
    assert ag["brain_provider"] == "gemini"
    assert ag["brain_model"] == "gemini-flash-lite-latest"
    assert ag["mailbox_ready"] is False
    assert ag["quiet_hours_active"] is True
    assert ag["max_daily_usd"] == 10.0


def test_scout_run_and_full_agentic_cycle_without_mailbox(env, monkeypatch):
    client, _, db = env

    async def _seed():
        st = StateManager(db_path=str(db))
        await st.add_company(Company(name="Acme", domain="acme.io", website="https://acme.io"))
        await st.add_enrichment_column(name="Summary", key="summary", prompt="Summarize", output_format="text")

    asyncio.run(_seed())

    import sellbird.agents.scout as scout_mod
    import sellbird.agents.researcher as res_mod
    import sellbird.agents.enricher as enr_mod
    import sellbird.agents.writer as wr_mod
    import sellbird.agents.analyst as an_mod
    from sellbird.brain import Brain

    closed_count = {"n": 0}

    async def fake_preflight(self):
        return ""

    async def fake_aclose(self):
        closed_count["n"] += 1

    monkeypatch.setattr(Brain, "preflight", fake_preflight)
    monkeypatch.setattr(Brain, "aclose", fake_aclose)

    calls = []

    async def fake_scout_run(self):
        calls.append("scout")
        await self.state.add_prospect(Prospect(
            first_name="Jane", last_name="Doe", email="jane@acme.io",
            email_status="verified", company="Acme", title="CEO",
        ))

    async def fake_res_run(self, **kw):
        calls.append(("researcher", kw.get("limit")))
        return []

    async def fake_enr_run(self, **kw):
        calls.append(("enricher", kw.get("limit")))
        return 1

    async def fake_writer_run(self):
        calls.append("writer")

    async def fake_analyst_run(self):
        calls.append("analyst")

    monkeypatch.setattr(scout_mod.Scout, "run", fake_scout_run)
    monkeypatch.setattr(res_mod.DeepResearcher, "run", fake_res_run)
    monkeypatch.setattr(enr_mod.ClayEnricher, "run", fake_enr_run)
    monkeypatch.setattr(wr_mod.Writer, "run", fake_writer_run)
    monkeypatch.setattr(an_mod.Analyst, "run", fake_analyst_run)

    r_scout = client.post("/api/scout/run")
    assert r_scout.status_code == 200
    assert r_scout.json()["success"] is True
    assert r_scout.json()["added"] == 1
    assert r_scout.json()["prospects_after"] == 1

    calls.clear()
    r_cycle = client.post("/api/agent/cycle", json={"research_limit": 2, "enrich_limit": 4})
    assert r_cycle.status_code == 200
    body = r_cycle.json()
    assert body["success"] is True
    assert body["steps_run"] == ["scout", "researcher", "enricher", "writer", "analyst"]
    assert body["mailbox_ready"] is False
    assert "Drafts staged in Outbox cleanly" in body["note"]
    assert closed_count["n"] == 2

    # Background mode returns immediately and populates /api/agent/cycle/status
    calls.clear()
    r_bg = client.post("/api/agent/cycle", json={"background": True, "research_limit": 1, "include_writer": False})
    assert r_bg.status_code == 200
    assert r_bg.json()["started"] is True
    st_resp = client.get("/api/agent/cycle/status")
    assert st_resp.status_code == 200
    assert "steps_done" in st_resp.json()
    # Wait briefly for background task to wrap up cleanly
    import time
    for _ in range(30):
        st_now = client.get("/api/agent/cycle/status").json()
        if not st_now["running"]:
            assert st_now["result"]["success"] is True
            break
        time.sleep(0.05)


def test_today_workbench_html_css_and_js_vm():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    css = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="today-cycle-btn"' in html
    assert 'id="today-loop-btn"' in html
    assert 'id="today-agentic"' in html
    assert ".agentic-card" in css
    assert ".agentic-chip" in css

    if not any(Path(p).exists() for p in ("C:\\Program Files\\nodejs\\node.exe",)) and not __import__("shutil").which("node"):
        return

    node_bin = __import__("shutil").which("node") or "node"
    chk = subprocess.run([node_bin, "--check", str(WEB_DIR / "app.js")], capture_output=True, text=True)
    assert chk.returncode == 0, chk.stderr

    script = f"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync({json.dumps(str(WEB_DIR / 'app.js'))}, 'utf8');
const els = {{}};
function makeEl(id) {{
  return {{
    id, innerHTML: '', textContent: '', value: '', disabled: false, style: {{}},
    className: '', querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {{}},
    classList: {{ toggle: () => {{}}, add: () => {{}}, remove: () => {{}} }}
  }};
}}
['today-agentic','today-agentic-result','today-cycle-btn','today-loop-btn','today-queue',
 'today-kpis','today-facts','today-setup','today-activity','today-figures',
 'live-status','nav-today','nav-outbox','nav-signals','nav-conversations',
 'header-dot','header-status-text','control-dot','control-label','control-meta','btn-start','btn-stop'
].forEach(id => {{ els[id] = makeEl(id); }});

const apiCalls = [];
const sandbox = {{
  document: {{
    getElementById: (id) => els[id] || (els[id] = makeEl(id)),
    querySelectorAll: () => [],
    addEventListener: () => {{}},
    documentElement: {{ setAttribute: () => {{}}, getAttribute: () => 'light', removeAttribute: () => {{}} }},
  }},
  window: {{ matchMedia: () => ({{ matches: false, addEventListener: () => {{}} }}), addEventListener: () => {{}}, location: {{ hash: '' }} }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }}),
  console, setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {{}},
  Number, String, Array, Object, JSON, Math, Date, Promise,
}};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
sandbox.api = async (path, opts) => {{
  apiCalls.push({{ path, opts }});
  if (path === '/api/agent/cycle') {{
    return {{ success: true, steps_run: ['scout', 'researcher', 'writer'], prospects_added: 2, outbox_pending: 8, note: 'Drafts staged.' }};
  }}
  if (path === '/api/today') {{
    return {{
      items: [],
      stats: {{ companies: 10, prospects: 7, outbox_pending: 8 }},
      agentic: {{ running: false, brain_ready: true, brain_provider: 'gemini', brain_model: 'gemini-flash-lite-latest', spend_today_usd: 0.001, max_daily_usd: 5, mailbox_ready: false, quiet_hours_active: true, quiet_hours_label: '22:00-07:00 America/New_York' }},
    }};
  }}
  return {{}};
}};
sandbox.showToast = () => {{}};
sandbox.loadLiveStatus = async () => {{}};
sandbox.loadFunnel = async () => {{}};
sandbox.loadTodayActivity = async () => {{}};
(async () => {{
  await sandbox.loadToday();
  if (!els['today-agentic'].innerHTML.includes('gemini-flash-lite-latest')) {{
    throw new Error('Brain readiness chip missing in #today-agentic');
  }}
  await sandbox.runAgenticCycleToday(els['today-cycle-btn']);
  if (!apiCalls.some(c => c.path === '/api/agent/cycle')) {{
    throw new Error('runAgenticCycleToday did not call /api/agent/cycle');
  }}
  if (!els['today-agentic'].innerHTML.includes('8 draft(s) waiting in Outbox')) {{
    throw new Error('Result summary lost after loadToday refresh');
  }}
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    res = subprocess.run([node_bin, "-e", script], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr or res.stdout
