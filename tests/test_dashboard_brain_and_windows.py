"""Tests for Dashboard AI Brain & Enrichment endpoints, Windows process control, and host seam."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.main import _check_stop_sentinel, _stop_file
from sellbird.state import StateManager

ORIGIN = "http://127.0.0.1:5555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "sellbird.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "sellbird.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "sellbird.log")
    monkeypatch.setattr(dash, "PROJECT_ROOT", tmp_path)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")

    # Write a test brain.yaml in tmp_path
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
    monkeypatch.setattr(dash, "_brain_config_file", lambda: brain_yaml)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-ds")

    async def _init():
        state = StateManager(db_path=str(db))
        await state.init_db()

    asyncio.run(_init())
    token = dash.token_store.get()
    client = TestClient(dash.app, base_url=ORIGIN, headers={"Authorization": f"Bearer {token}"})
    return client, tmp_path


def test_get_brain_config(env):
    client, _ = env
    resp = client.get("/api/brain/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "chain" in data
    assert "budget" in data
    assert data["budget"]["max_usd_per_day"] == 10.0
    assert len(data["chain"]) >= 1
    assert data["chain"][0]["provider"] == "deepseek"


def test_save_brain_config(env):
    client, tmp_path = env
    payload = {
        "budget": {"max_usd_per_day": 15.0},
        "chain": [
            {"provider": "deepseek", "model": "deepseek-reasoner"},
            {"provider": "groq", "model": "llama-3.3-70b-versatile"},
        ],
        "profiles": {
            "writer": {"model": "deepseek-reasoner", "provider": "deepseek"},
            "scout": {"model": "llama-3.3-70b-versatile", "provider": "groq"},
        },
    }
    resp = client.post("/api/brain/config", json=payload)
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Verify updated values on reload
    resp2 = client.get("/api/brain/config")
    data2 = resp2.json()
    assert data2["budget"]["max_usd_per_day"] == 15.0
    assert len(data2["chain"]) == 2
    assert data2["profiles"]["writer"]["model"] == "deepseek-reasoner"


def test_enrichment_columns_crud(env):
    client, _ = env

    # 1. Initially empty
    resp = client.get("/api/enrichment/columns")
    assert resp.status_code == 200
    assert resp.json()["columns"] == []

    # 2. Add column
    col_payload = {
        "name": "Tech Stack Analysis",
        "key": "tech_stack_detail",
        "prompt": "Evaluate the underlying technologies used on this domain.",
        "output_type": "text",
    }
    resp_add = client.post("/api/enrichment/columns", json=col_payload)
    assert resp_add.status_code == 200
    assert resp_add.json()["success"] is True

    # 3. List contains added column
    resp_list = client.get("/api/enrichment/columns")
    cols = resp_list.json()["columns"]
    assert len(cols) == 1
    assert cols[0]["name"] == "Tech Stack Analysis"
    assert "prompt" in cols[0]
    col_id = cols[0]["id"]

    # 4. Delete column
    resp_del = client.delete(f"/api/enrichment/columns/{col_id}")
    assert resp_del.status_code == 200
    assert resp_del.json()["success"] is True

    # 5. List is empty again
    resp_list2 = client.get("/api/enrichment/columns")
    assert resp_list2.json()["columns"] == []


def test_windows_stop_sentinel(tmp_path, monkeypatch):
    """Test graceful stop file sentinel detection."""
    stop_file = tmp_path / "sellbird.stop"
    monkeypatch.setattr("sellbird.main._stop_file", lambda: stop_file)

    stop_event = asyncio.Event()
    assert not _check_stop_sentinel(stop_event)
    assert not stop_event.is_set()

    # Create stop file
    stop_file.write_text("stop", encoding="utf-8")
    assert _check_stop_sentinel(stop_event)
    assert stop_event.is_set()
