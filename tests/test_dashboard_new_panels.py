"""Dashboard API for the Phase 4 surfaces: the framework experiment
(Campaigns tab), the competitor watch (Settings), and the DNS check.

The contract each panel depends on: the framework card stays hidden until
there's an experiment to show, the watch list round-trips through the
config file Sellbird reads, and the DNS check returns one verdict row per
record with statuses the UI colours.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.models.campaign import Campaign
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager


@pytest.fixture
def client(monkeypatch):
    """A dashboard bound to a throwaway database and config file, used the
    way its own UI uses it: same origin, with the token."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sellbird.db"
        cfg = Path(tmp) / "sellbird.yaml"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "_resolve_config_file", lambda: cfg)
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            c.cfg_path = cfg
            yield c


async def _seed_campaign(state, framework, statuses):
    prospects = []
    for i, st in enumerate(statuses):
        p = Prospect(id=f"p-{framework}-{i}", company_id="co1",
                     first_name="A", last_name=str(i),
                     email=f"{framework}-{i}@acme.example",
                     status=st)
        prospects.append(p)
        await state.add_prospect(p)
    camp = Campaign(id=f"camp-{framework}", name=f"camp-{framework}", channel="email",
                    prospect_ids=[p.id for p in prospects], framework=framework)
    await state.add_campaign(camp)


# ── Framework experiment ──


def test_frameworks_hidden_without_an_experiment(client):
    """Built-in shape only: nothing configured, nothing tagged — no card."""
    data = client.get("/api/frameworks").json()
    assert data == {"configured": [], "rows": []}


def test_frameworks_list_configured_arms_with_zero_history(client):
    import yaml
    client.cfg_path.write_text(yaml.safe_dump(
        {"writer": {"frameworks": ["pas", "bab"]}}), encoding="utf-8")
    data = client.get("/api/frameworks").json()
    assert data["configured"] == ["pas", "bab"]
    assert data["rows"] == []


@pytest.mark.asyncio
async def test_frameworks_report_replies_per_arm(client):
    import asyncio
    state = StateManager(str(client.db_path))
    await state.init_db()
    await _seed_campaign(state, "pas", ["replied", "contacted", "contacted"])
    await _seed_campaign(state, "bab", ["contacted", "contacted"])
    data = client.get("/api/frameworks").json()
    rows = {r["framework"]: r for r in data["rows"]}
    assert rows["pas"] == {"framework": "pas", "replies": 1, "finished": 3}
    assert rows["bab"] == {"framework": "bab", "replies": 0, "finished": 2}


# ── Competitor watch ──


def test_competitor_watch_round_trips_through_the_config(client):
    """Save, then read back: the list the UI shows is the list it saved."""
    saved = client.post("/api/competitor-watch", json={
        "enabled": True,
        "watches": [{"name": "Rival pricing", "url": "https://rival.example/pricing",
                     "kind": "pricing"}],
    }).json()
    assert saved.get("success") is True
    data = client.get("/api/competitor-watch").json()
    assert data["enabled"] is True
    assert data["watches"] == [{"name": "Rival pricing",
                                "url": "https://rival.example/pricing",
                                "kind": "pricing"}]
    assert data["changes"] == []


def test_competitor_watch_rejects_a_bad_url(client):
    resp = client.post("/api/competitor-watch", json={
        "enabled": True,
        "watches": [{"name": "Rival", "url": "ftp://rival.example", "kind": "pricing"}],
    })
    assert resp.status_code == 400
    assert "https://" in resp.json()["message"]


def test_competitor_watch_rejects_duplicate_names(client):
    """Names key the stored baseline, so two of the same would collide."""
    resp = client.post("/api/competitor-watch", json={
        "enabled": True,
        "watches": [
            {"name": "Rival", "url": "https://rival.example/a", "kind": "pricing"},
            {"name": "rival", "url": "https://rival.example/b", "kind": "pricing"},
        ],
    })
    assert resp.status_code == 400
    assert "unique" in resp.json()["message"] or "Rival" in resp.json()["message"]


def test_competitor_watch_enabled_needs_a_watch(client):
    resp = client.post("/api/competitor-watch", json={"enabled": True, "watches": []})
    assert resp.status_code == 400


def test_competitor_watch_blank_rows_are_dropped_not_rejected(client):
    """The form's empty row saves as nothing, not as an error."""
    saved = client.post("/api/competitor-watch", json={
        "enabled": False, "watches": [{"name": "", "url": "", "kind": "homepage"}],
    }).json()
    assert saved.get("success") is True
    assert client.get("/api/competitor-watch").json()["watches"] == []


@pytest.mark.asyncio
async def test_competitor_watch_lists_logged_changes(client):
    import asyncio
    state = StateManager(str(client.db_path))
    await state.init_db()
    await state.log_action("competitor_change", "competitor_watch",
                           {"watch": "Rival pricing", "url": "https://rival.example/pricing",
                            "kind": "pricing"})
    data = client.get("/api/competitor-watch").json()
    assert len(data["changes"]) == 1
    assert data["changes"][0]["watch"] == "Rival pricing"
    assert data["changes"][0]["kind"] == "pricing"


# ── DNS check ──


def test_dns_check_reports_every_record(client, monkeypatch):
    import sellbird.deliverability as d
    from sellbird import dashboard as dashmod

    fake = [d.Check(name="MX", status=d.OK, detail="mail.example.com"),
            d.Check(name="SPF", status=d.OK, detail="v=spf1 include:_spf.example.com ~all"),
            d.Check(name="DKIM", status=d.WARN, detail="no selector found"),
            d.Check(name="DMARC", status=d.FAIL, detail="no record")]
    monkeypatch.setattr(d, "check_mx", lambda dom: fake[0])
    monkeypatch.setattr(d, "check_spf", lambda dom: fake[1])
    monkeypatch.setattr(d, "check_dkim", lambda dom, sel="": fake[2])
    monkeypatch.setattr(d, "check_dmarc", lambda dom: fake[3])
    data = client.post("/api/dns-check", json={"domain": "acme.example"}).json()
    assert data["domain"] == "acme.example"
    assert [c["status"] for c in data["checks"]] == ["ok", "ok", "warn", "fail"]
    assert data["failed"] == 1


def test_dns_check_rejects_a_non_domain(client):
    resp = client.post("/api/dns-check", json={"domain": "not a domain"})
    assert resp.status_code == 400
