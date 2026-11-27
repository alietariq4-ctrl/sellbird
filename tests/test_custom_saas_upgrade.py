"""Tests for Custom SaaS & Local Business promotion setup, offer/persona editing, local OSM tags, and UI fixes."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sellbird.collectors.discover as D
import sellbird.dashboard as dash
from sellbird.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "sellbird" / "web"


@pytest.fixture
def saas_env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "sellbird.db"
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=AIza-test-key\n", encoding="utf-8")
    cfg_file = tmp_path / "sellbird.local.yaml"
    cfg_file.write_text(
        """persona:
  name: Smoke Test
  company: Smoke Test Inc (placeholder)
  role: Business Development
  email: alex@yourcompany.com
  linkedin: ''
  tone: professional
product:
  name: Smoke Test Product (placeholder)
  description: Placeholder description
  pricing: Starting at $X/mo
  key_benefits:
    - Benefit 1
  objection_responses:
    too expensive: ROI within 30 days
  offer:
    entry: ''
    booking_url: ''
icp:
  industries:
    - dentist
  company_size: 1-10 employees
  titles:
    - Practice Owner
  geography:
    - Denver, CO
brain:
  transport_factory: harvey_brain.router_factory:build
usage:
  max_daily_usd: 10.0
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

    asyncio.run(StateManager(db_path=str(db)).init_db())
    token = dash.token_store.get()
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    with TestClient(dash.app, base_url=ORIGIN, headers=headers) as client:
        yield client, headers, cfg_file


def test_custom_saas_company_persona_and_offer_editing(saas_env):
    """Users can promote their own SaaS by editing persona.company, persona.email, linkedin, and offer.booking_url."""
    client, headers, _ = saas_env

    # Initial GET uses resolved sellbird.local.yaml and returns linkedin & booking_url
    res_get = client.get("/api/company", headers=headers)
    assert res_get.status_code == 200
    initial = res_get.json()
    assert initial["persona"]["company"] == "Smoke Test Inc (placeholder)"
    assert "booking_url" in initial["offer"]

    # Update persona with real SaaS sender info
    res_persona = client.post(
        "/api/company",
        json={
            "section": "persona",
            "fields": {
                "name": "Ali Tariq",
                "role": "Founder",
                "company": "FlowPulse SaaS",
                "email": "ali@flowpulse.io",
                "linkedin": "linkedin.com/in/ali-tariq",
                "tone": "direct and helpful",
            },
        },
        headers=headers,
    )
    assert res_persona.status_code == 200
    assert res_persona.json()["success"] is True

    # Update offer CTA + Cal.com booking link
    res_offer = client.post(
        "/api/offer",
        json={
            "entry": "a 2-minute teardown of missed leads on your site",
            "booking_url": "https://cal.com/alitariq/15min",
        },
        headers=headers,
    )
    assert res_offer.status_code == 200
    assert res_offer.json()["success"] is True

    # Update ICP for local businesses + custom SaaS markets
    res_icp = client.post(
        "/api/company",
        json={
            "section": "icp",
            "fields": {
                "industries": ["dentist", "hvac", "auto repair", "SaaS"],
                "titles": ["Owner", "Founder"],
                "company_size": "1-50 employees",
                "geography": ["Austin, TX", "Denver, CO"],
            },
        },
        headers=headers,
    )
    assert res_icp.status_code == 200

    # Verify persisted config via /api/company & /api/offer
    updated = client.get("/api/company", headers=headers).json()
    assert updated["persona"]["company"] == "FlowPulse SaaS"
    assert updated["persona"]["email"] == "ali@flowpulse.io"
    assert updated["persona"]["linkedin"] == "linkedin.com/in/ali-tariq"
    assert updated["offer"]["booking_url"] == "https://cal.com/alitariq/15min"
    assert updated["offer"]["entry"] == "a 2-minute teardown of missed leads on your site"
    assert "auto repair" in updated["icp"]["industries"]
    assert "Austin, TX" in updated["icp"]["geography"]


def test_expanded_osm_local_business_trades():
    """OpenStreetMap TAG_MAP covers modern local business niches for SaaS outreach."""
    osm = D.OpenStreetMap()
    for term in (
        "chiropractor",
        "veterinarian",
        "auto repair",
        "hair salon",
        "fitness studio",
        "real estate",
        "insurance agency",
        "cpa firm",
        "restaurant",
        "solar installer",
        "dental clinic",
    ):
        tags = osm._tags(term)
        assert tags, f"Expected OSM tags for {term!r}"


def test_custom_saas_ui_js_vm_and_css_checks():
    app_js = WEB_DIR / "app.js"
    app_css = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    index_html = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    proc = subprocess.run(["node", "--check", str(app_js)], capture_output=True, text=True)
    assert proc.returncode == 0, f"node --check failed:\n{proc.stderr}"

    # Ensure broken showSection calls are completely gone & 2026 Liquid Glass + ASCII telemetry anchors exist
    assert "showSection(" not in app_js.read_text(encoding="utf-8")
    assert "auto repair" in index_html
    assert 'id="ascii-telemetry"' in index_html
    assert "scroll-margin-top: 120px" in app_css
    assert ".playbook-steps" in app_css and ".preset-grid" in app_css
    assert "backdrop-filter: blur(" in app_css and "::view-transition-new(root)" in app_css

    vm_script = """
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');

function mkEl(id) {
  const attrs = {};
  let _text = '';
  let _html = '';
  return {
    id, value: '', checked: false,
    get textContent() { return _text; },
    set textContent(v) { _text = String(v ?? ''); _html = _text; },
    get innerHTML() { return _html; },
    set innerHTML(v) { _html = String(v ?? ''); },
    className: '', style: { display: '' }, disabled: false, dataset: {},
    classList: {
      set: new Set(),
      add(c) { this.set.add(c); },
      remove(c) { this.set.delete(c); },
      toggle(c, f) { if (f === undefined) { this.set.has(c) ? this.set.delete(c) : this.set.add(c); } else if (f) { this.set.add(c); } else { this.set.delete(c); } },
      contains(c) { return this.set.has(c); }
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k] || null; },
    removeAttribute(k) { delete attrs[k]; },
    appendChild() {}, remove() {},
    addEventListener() {}, querySelectorAll() { return []; },
    querySelector() { return null; }, showModal() {}, close() {},
  };
}
const els = new Map();
const getEl = (id) => { if (!els.has(id)) els.set(id, mkEl(id)); return els.get(id); };

const mockFetch = async () => ({
  ok: true, status: 200,
  json: async () => ({
    product: { name: 'MySaaS', pricing: '$49/mo', description: 'Desc', key_benefits: ['Fast'], objection_responses: {} },
    persona: { name: 'Ali', role: 'Founder', company: 'MySaaS Inc', email: 'ali@mysaas.io', linkedin: '', tone: 'warm' },
    icp: { industries: ['dentist'], titles: ['Owner'], company_size: '1-20', geography: ['Austin, TX'] },
    offer: { entry: 'Free audit', booking_url: 'https://cal.com/ali/15', entry_ideas: [] },
  }),
});

const context = {
  console, Math, Date, JSON, Promise, Array, Object, String, Number, Boolean, RegExp, Error, Map, Set,
  Headers,
  encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  fetch: mockFetch,
  authFetch: mockFetch,
  authToken: () => 'testtoken1234567890',
  window: {
    location: { hash: '#token=testtoken1234567890', search: '', pathname: '/' },
    addEventListener() {},
    history: { replaceState() {} },
    scrollTo() {},
  },
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

(async () => {
  await context.loadCompany();
  const html = getEl('company-body').innerHTML;
  if (!html.includes('Launch Your SaaS or Local Outreach')) throw new Error('Missing Launch Playbook');
  if (!html.includes('co-s-company') || !html.includes('co-s-email') || !html.includes('offer-booking-url')) {
    throw new Error('Missing editable SaaS sender/booking inputs');
  }
  if (!html.includes('goToTab(')) throw new Error('Expected goToTab links');

  // Seed fields before applying preset
  ['co-p-name', 'co-p-pricing', 'co-p-desc', 'co-p-benefits', 'offer-entry', 'co-i-industries', 'co-i-titles', 'co-i-size', 'co-i-geo'].forEach(getEl);
  context.applyCompanyPreset('saas_local');
  if (getEl('co-p-name').value !== 'LocalFlow SaaS') throw new Error('Preset did not populate product name');
  if (!getEl('co-i-industries').value.includes('dentist')) throw new Error('Preset did not populate local trades');

  // Toggle an existing chip off, then a new trade on
  context.toggleIndustryChip('auto repair');
  if (getEl('co-i-industries').value.includes('auto repair')) throw new Error('Chip toggle failed to remove industry');
  context.toggleIndustryChip('auto repair');
  if (!getEl('co-i-industries').value.includes('auto repair')) throw new Error('Chip toggle failed to add industry');

  // Verify live pipeline status updater formats active vs ready cleanly
  vm.runInContext("_todayStats = { companies: 12, prospects: 4, outbox_pending: 2 };", context);
  context.updateAsciiTelemetry(false);
  if (!getEl('ascii-state').textContent.toLowerCase().includes('ready') || !getEl('ascii-bars').textContent.includes('12 companies')) {
    throw new Error('Pipeline status ready render failed: ' + getEl('ascii-bars').textContent);
  }
  context.updateAsciiTelemetry(true);
  if (!getEl('ascii-state').textContent.toLowerCase().includes('active')) {
    throw new Error('Pipeline status active render failed');
  }
  context.updateAsciiTelemetry(false);

  console.log('VM_CUSTOM_SAAS_OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    vm_proc = subprocess.run(["node", "-e", vm_script, str(app_js)], capture_output=True, text=True)
    assert vm_proc.returncode == 0, f"VM test failed:\n{vm_proc.stderr}\n{vm_proc.stdout}"
    assert "VM_CUSTOM_SAAS_OK" in vm_proc.stdout
