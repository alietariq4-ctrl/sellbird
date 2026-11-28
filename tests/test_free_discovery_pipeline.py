"""Tests for Smart Free Multi-Source Discovery, domain healing, and contact extraction."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.collectors import discover as D
from sellbird.config import EnvConfig, load_config
from sellbird.models import Company, Prospect
from sellbird.pipeline import run_contact_extraction_stage
from sellbird.signals import seed_signal_catalog
from sellbird.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        sm = StateManager(str(Path(tmp) / "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


@pytest.fixture
def config():
    cfg = load_config()
    cfg.icp.industries = ["hvac"]
    cfg.icp.geography = ["Austin, TX"]
    return cfg


@pytest.mark.asyncio
async def test_osm_places_merges_duplicate_external_id_and_preserves_website():
    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self.content = b"1"
            self._payload = payload

        def json(self):
            return self._payload

    class _FakeClient:
        async def get(self, url, **kwargs):
            if "photon.komoot.io" in url:
                return _Resp({
                    "features": [{
                        "properties": {
                            "name": "Superior HVAC Austin",
                            "osm_type": "N",
                            "osm_id": 999,
                            "city": "Austin",
                            "state": "TX",
                        }
                    }]
                })
            if "nominatim.openstreetmap.org" in url:
                return _Resp([{
                    "name": "Superior HVAC Austin",
                    "osm_type": "node",
                    "osm_id": 999,
                    "address": {"city": "Austin", "state": "TX"},
                    "extratags": {
                        "website": "https://superiorhvacaustin.com/",
                        "phone": "+1-512-555-0100",
                    },
                }])
            raise AssertionError(url)

    out = await D.OsmPlacesProvider().fetch(
        _FakeClient(), {}, D.DiscoveryQuery(term="hvac", location="Austin", limit=10)
    )
    assert len(out) == 1
    assert out[0].domain == "superiorhvacaustin.com"
    assert out[0].website == "https://superiorhvacaustin.com/"
    assert out[0].phone == "+1-512-555-0100"


@pytest.mark.asyncio
async def test_fill_company_fields_heals_domainless_company_on_upsert(state, config, monkeypatch):
    cid = await state.add_company(Company(
        name="Superior HVAC Austin", domain="", website="", external_id="osm:node/999",
    ))

    class _FakeHeal(D.DiscoveryProvider):
        key = "fake_heal"
        label = "Fake Heal"
        env_keys = ()

        async def fetch(self, client, env, query):
            return [D.Business(
                name="Superior HVAC Austin",
                domain="superiorhvacaustin.com",
                website="https://superiorhvacaustin.com/",
                phone="+1-512-555-0100",
                location="Austin, TX",
                external_id="osm:node/999",
                provider=self.key,
            )]

    monkeypatch.setitem(D.PROVIDERS, "fake_heal", _FakeHeal())
    monkeypatch.setattr("sellbird.config.load_env", lambda: EnvConfig())
    await state.set_signal_status("FOUND_IN_SERP", "confirmed")

    report = await D.run_discovery(
        state, config, "fake_heal", [D.DiscoveryQuery(term="hvac", location="Austin")]
    )
    assert report.known_companies == 1
    updated = await state.get_company(cid)
    assert updated.domain == "superiorhvacaustin.com"
    assert updated.website == "https://superiorhvacaustin.com/"
    assert updated.phone == "+1-512-555-0100"


@pytest.mark.asyncio
async def test_free_auto_combines_osm_places_and_ddgs_and_filters_junk(monkeypatch):
    async def _osm_fetch(self, client, env, query):
        return [D.Business(name="Smart Air", domain="smartairaustin.com", website="https://smartairaustin.com", provider="osm")]

    async def _places_fetch(self, client, env, query):
        return [D.Business(name="MORSCO HVAC", domain="", external_id="osm:node/1", provider="osm_places")]

    async def _ddg_fetch(self, client, env, query):
        return [
            D.Business(name="Wikipedia", domain="en.wikipedia.org", website="https://en.wikipedia.org/wiki/HVAC", provider="ddgs"),
            D.Business(name="ABC Austin AC", domain="abcaustinac.com", website="https://abcaustinac.com", provider="ddgs"),
        ]

    monkeypatch.setattr(D.OpenStreetMap, "fetch", _osm_fetch)
    monkeypatch.setattr(D.OsmPlacesProvider, "fetch", _places_fetch)
    monkeypatch.setattr(D.DuckDuckGoSearch, "fetch", _ddg_fetch)

    prov = D.PROVIDERS["free_auto"]
    assert prov.env_keys == ()
    assert D.preset_problem("yc", "free_auto") is None

    res = await prov.fetch(
        None, {},
        D.DiscoveryQuery(term="hvac", location="Austin", limit=20, coordinate="30.26,-97.74,40"),
    )
    domains = [b.domain for b in res]
    assert "smartairaustin.com" in domains
    assert "abcaustinac.com" in domains
    assert "en.wikipedia.org" not in domains
    # Domain-bearing businesses sorted ahead of domainless pins
    assert res[-1].domain == ""


@pytest.mark.asyncio
async def test_run_contact_extraction_stage_persists_prospects(state, config, monkeypatch):
    cid = await state.add_company(Company(name="Smart Air Austin", domain="smartairaustin.com", website="https://smartairaustin.com"))

    async def _fake_contacts(self, *, company_id, domain, company_name, industry, source="", **kw):
        return [Prospect(
            first_name="Alex", last_name="Rivera",
            email="alex@smartairaustin.com", email_status="verified", email_verified=True,
            company=company_name, company_id=company_id, title="Owner", source=source,
        )]

    from sellbird.agents.scout import Scout
    monkeypatch.setattr(Scout, "_contacts_from_company", _fake_contacts)
    monkeypatch.setattr("sellbird.config.load_env", lambda: EnvConfig())

    progress_calls = []
    added, cos = await run_contact_extraction_stage(
        state, config, limit=10,
        progress_cb=lambda idx, total, cnt: progress_calls.append((idx, total, cnt)),
    )
    assert added == 1
    assert cos == 1
    assert progress_calls == [(1, 1, 1)]
    contacts = await state.get_contacts_for_company(cid)
    assert len(contacts) == 1 and contacts[0].email == "alex@smartairaustin.com"
