"""Unit tests for dynamic live LLM model fetching, provider catalog, and Brain Studio role/env sync."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.state import StateManager

ORIGIN = "http://127.0.0.1:5555"


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
        "budget:\n  max_usd_per_day: 10.0\nchain:\n  - provider: openrouter\n    model: anthropic/claude-sonnet-5.5\n",
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
    dash._MODEL_LIST_CACHE.clear()
    for k in (
        "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY", "GROQ_API_KEY", "XAI_API_KEY", "MISTRAL_API_KEY",
        "TOGETHER_API_KEY", "FIREWORKS_API_KEY", "CEREBRAS_API_KEY",
        "OPENAI_BASE_URL", "OPENAI_MODEL",
    ):
        monkeypatch.delenv(k, raising=False)

    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    asyncio.run(StateManager(db_path=str(db)).init_db())
    token = dash.token_store.get()
    client = TestClient(dash.app, base_url=ORIGIN, headers={"Authorization": f"Bearer {token}"})
    return client, tmp_path, env_file


class _FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _patch_httpx(monkeypatch, handler):
    calls: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            calls.append({"url": url, "headers": dict(headers or {}), "client_kwargs": self.kwargs})
            res = handler(url, headers or {})
            if isinstance(res, BaseException):
                raise res
            return res

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return calls


# ── 1. Providers Catalog & Config / Settings Sync ──


def test_brain_providers_lists_core_local_cli_and_custom_with_key_readiness(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    resp = client.get("/api/brain/providers")
    assert resp.status_code == 200
    providers = resp.json()["providers"]
    by_id = {p["id"]: p for p in providers}

    for expected in (
        "openrouter", "openai", "openai_compat", "custom", "anthropic", "gemini",
        "deepseek", "groq", "xai", "mistral", "together", "fireworks", "cerebras",
        "ollama", "lmstudio", "vllm", "claude_cli", "codex_cli",
    ):
        assert expected in by_id, f"Missing provider {expected}"
        row = by_id[expected]
        assert set(row.keys()) == {
            "id", "label", "base_url", "api_key_env",
            "default_model", "keyless", "is_cli", "key_set",
        }

    assert by_id["openrouter"]["key_set"] is True
    assert by_id["openrouter"]["keyless"] is False
    assert by_id["anthropic"]["key_set"] is True
    assert by_id["anthropic"]["base_url"] == "https://api.anthropic.com/v1"
    assert by_id["openai"]["key_set"] is False
    assert by_id["ollama"]["keyless"] is True
    assert by_id["ollama"]["is_cli"] is False
    assert by_id["lmstudio"]["keyless"] is True
    assert by_id["vllm"]["keyless"] is True
    assert by_id["claude_cli"]["is_cli"] is True
    assert by_id["codex_cli"]["is_cli"] is True


def test_brain_config_roles_providers_and_anthropic_env_setting(env):
    client, _, env_file = env
    assert "ANTHROPIC_API_KEY" in dash.ALLOWED_ENV_KEYS

    save_resp = client.post("/api/settings/env", json={"ANTHROPIC_API_KEY": "sk-ant-saved-123"})
    assert save_resp.status_code == 200
    assert save_resp.json()["success"] is True
    assert "ANTHROPIC_API_KEY=sk-ant-saved-123" in env_file.read_text(encoding="utf-8")

    settings_resp = client.get("/api/settings")
    assert settings_resp.status_code == 200
    assert settings_resp.json()["anthropic_api_key_set"] is True

    cfg_resp = client.get("/api/brain/config")
    assert cfg_resp.status_code == 200
    cfg = cfg_resp.json()
    assert cfg["roles"] == [
        "scout", "researcher", "enricher", "writer",
        "critic", "handler", "analyst", "trainer",
    ]
    assert "enricher" in cfg["roles"] and "analyst" in cfg["roles"]
    assert cfg["env_keys_set"]["ANTHROPIC_API_KEY"] is True
    assert isinstance(cfg["providers"], list)
    assert any(p["id"] == "anthropic" and p["key_set"] is True for p in cfg["providers"])


# ── 2. Live OpenAI-Compat / OpenRouter Normalization, Filtering & Cache ──


def test_brain_models_openrouter_live_pricing_filtering_and_cache(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-live")

    now = [1000.0]
    monkeypatch.setattr(dash.time, "monotonic", lambda: now[0])

    payload = {
        "data": [
            {"id": "openai/whisper-1", "name": "Whisper"},
            {"id": "openai/text-embedding-3-small", "name": "Embed"},
            {
                "id": "z-ai/custom-chat",
                "name": "Z-AI Custom Chat",
                "context_length": 64000,
                "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
            },
            {
                "id": "anthropic/claude-sonnet-5.5",
                "name": "Anthropic: Claude Sonnet 5.5",
                "context_length": 200000,
                "pricing": {"prompt": "0.000002", "completion": "0.00001"},
            },
            {
                "id": "openai/gpt-6-luna",
                "name": "OpenAI: GPT-6 Luna",
                # No pricing dict -> falls back to harvey_brain.pricing.lookup
            },
            {
                "id": "meta-llama/llama-3.3-70b:free",
                "name": "Llama Free",
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]
    }
    calls = _patch_httpx(monkeypatch, lambda url, headers: _FakeResp(200, payload))

    r1 = client.post("/api/brain/models", json={"provider": "openrouter"})
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["ok"] is True
    assert d1["source"] == "live"
    assert d1["cached"] is False
    assert d1["api_key_set"] is True
    assert len(calls) == 1
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/models"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-or-live"
    assert calls[0]["headers"]["HTTP-Referer"]
    assert calls[0]["headers"]["X-Title"] == "Sellbird"

    ids = [m["id"] for m in d1["models"]]
    assert "openai/whisper-1" not in ids
    assert "openai/text-embedding-3-small" not in ids
    # Default/curated models sorted before uncurated custom-chat
    assert ids[0] == "anthropic/claude-sonnet-5.5"
    assert ids.index("openai/gpt-6-luna") < ids.index("z-ai/custom-chat")

    by_id = {m["id"]: m for m in d1["models"]}
    assert by_id["anthropic/claude-sonnet-5.5"] == {
        "id": "anthropic/claude-sonnet-5.5",
        "label": "Anthropic: Claude Sonnet 5.5",
        "context_length": 200000,
        "input_per_m": 2.0,
        "output_per_m": 10.0,
        "curated": True,
    }
    assert by_id["z-ai/custom-chat"]["input_per_m"] == 0.5
    assert by_id["z-ai/custom-chat"]["output_per_m"] == 1.5
    assert by_id["z-ai/custom-chat"]["curated"] is False
    assert by_id["openai/gpt-6-luna"]["input_per_m"] == 0.1
    assert by_id["openai/gpt-6-luna"]["output_per_m"] == 0.5
    assert by_id["meta-llama/llama-3.3-70b:free"]["input_per_m"] == 0.0

    # Cache hit within 120s
    now[0] += 60.0
    r2 = client.post("/api/brain/models", json={"provider": "openrouter"})
    assert r2.status_code == 200
    assert r2.json()["cached"] is True
    assert len(calls) == 1

    # refresh=True bypasses cache
    r3 = client.post("/api/brain/models", json={"provider": "openrouter", "refresh": True})
    assert r3.status_code == 200
    assert r3.json()["cached"] is False
    assert len(calls) == 2

    # Cache expires after 120s
    now[0] += 121.0
    r4 = client.post("/api/brain/models", json={"provider": "openrouter"})
    assert r4.status_code == 200
    assert r4.json()["cached"] is False
    assert len(calls) == 3


def test_brain_models_gemini_endpoint_and_bare_list(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    calls = _patch_httpx(
        monkeypatch,
        lambda url, headers: _FakeResp(200, [{"id": "models/gemini-4-hydrogen"}, {"id": "gemini-flash"}]),
    )
    resp = client.post("/api/brain/models", json={"provider": "gemini"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert calls[0]["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    ids = [m["id"] for m in data["models"]]
    assert ids == ["gemini-4-hydrogen", "gemini-flash"]
    assert data["models"][0]["input_per_m"] == 1.5


# ── 3. Ollama /api/tags Fallback & Anthropic Headers ──


def test_brain_models_ollama_api_tags_fallback_on_404(env, monkeypatch):
    client, _, _ = env

    def handler(url, headers):
        if url.endswith("/v1/models"):
            return _FakeResp(404, {"error": "not found"})
        if url.endswith("/api/tags"):
            return _FakeResp(200, {
                "models": [
                    {"name": "llama3.2:latest", "model": "llama3.2:latest"},
                    {"name": "nomic-embed-text:latest", "model": "nomic-embed-text:latest"},
                    {"name": "qwen2.5:14b", "model": "qwen2.5:14b"},
                ]
            })
        raise AssertionError(f"Unexpected URL {url}")

    calls = _patch_httpx(monkeypatch, handler)
    resp = client.post("/api/brain/models", json={"provider": "ollama"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["source"] == "live"
    assert [c["url"] for c in calls] == [
        "http://localhost:11434/v1/models",
        "http://localhost:11434/api/tags",
    ]
    ids = [m["id"] for m in data["models"]]
    assert "llama3.2:latest" in ids
    assert "qwen2.5:14b" in ids
    assert "nomic-embed-text:latest" not in ids
    assert data["models"][0]["input_per_m"] == 0.0


def test_brain_models_anthropic_headers_and_display_name(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")

    calls = _patch_httpx(
        monkeypatch,
        lambda url, headers: _FakeResp(200, {
            "data": [
                {"id": "claude-sonnet-5.5", "display_name": "Claude Sonnet 5.5"},
                {"id": "claude-haiku-5.5", "display_name": "Claude Haiku 5.5"},
            ]
        }),
    )

    resp = client.post("/api/brain/models", json={"provider": "anthropic"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["source"] == "live"
    assert calls[0]["url"] == "https://api.anthropic.com/v1/models"
    assert calls[0]["headers"]["x-api-key"] == "sk-ant-secret"
    assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in calls[0]["headers"]
    assert data["models"][0]["label"] == "Claude Sonnet 5.5"


# ── 4. CLI Delegates (codex_cli, claude_cli) ──


def test_brain_models_cli_delegates_curated_and_codex_catalog(env, monkeypatch):
    client, _, _ = env
    import sellbird.codex as codex_mod

    r_claude = client.post("/api/brain/models", json={"provider": "claude_cli"})
    assert r_claude.status_code == 200
    d_claude = r_claude.json()
    assert d_claude["ok"] is True
    assert d_claude["source"] == "curated"
    assert [m["id"] for m in d_claude["models"]] == ["sonnet", "haiku", "opus"]

    # Codex CLI unavailable -> graceful curated list
    monkeypatch.setattr(codex_mod, "find_codex", lambda: None)
    r_codex_fb = client.post("/api/brain/models", json={"provider": "codex_cli"})
    assert r_codex_fb.status_code == 200
    d_codex_fb = r_codex_fb.json()
    assert d_codex_fb["ok"] is True
    assert d_codex_fb["source"] == "curated"
    assert [m["id"] for m in d_codex_fb["models"]] == ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna", "gpt-5.5"]

    # Mocked codex.read_catalog succeeds -> live catalog
    monkeypatch.setattr(
        codex_mod,
        "read_catalog",
        lambda: {"gpt-6.1-sol": {"slug": "gpt-6.1-sol", "display_name": "GPT 6.1 Sol"}},
        raising=False,
    )
    r_codex_live = client.post("/api/brain/models", json={"provider": "codex", "refresh": True})
    assert r_codex_live.status_code == 200
    d_codex_live = r_codex_live.json()
    assert d_codex_live["ok"] is True
    assert d_codex_live["source"] == "live"
    assert d_codex_live["models"][0]["id"] == "gpt-6.1-sol"


# ── 5. Graceful Curated Fallback (Missing Key / HTTP Error / Network Error) ──


def test_brain_models_graceful_fallback_on_missing_key_http_error_and_network_error(env, monkeypatch):
    client, _, _ = env

    # Missing key -> 200 curated fallback, no HTTP call
    calls = _patch_httpx(monkeypatch, lambda url, headers: _FakeResp(200, {"data": []}))
    r_missing = client.post("/api/brain/models", json={"provider": "openai"})
    assert r_missing.status_code == 200
    d_missing = r_missing.json()
    assert d_missing["ok"] is False
    assert d_missing["source"] == "curated"
    assert d_missing["api_key_set"] is False
    assert "OPENAI_API_KEY" in d_missing["warning"]
    assert len(d_missing["models"]) >= 1
    assert all(m["curated"] is True for m in d_missing["models"])
    assert len(calls) == 0

    # HTTP 401 -> 200 curated fallback
    monkeypatch.setenv("OPENAI_API_KEY", "bad-key")
    _patch_httpx(monkeypatch, lambda url, headers: _FakeResp(401, {"error": "invalid key"}))
    r_401 = client.post("/api/brain/models", json={"provider": "openai"})
    assert r_401.status_code == 200
    d_401 = r_401.json()
    assert d_401["ok"] is False
    assert d_401["source"] == "curated"
    assert d_401["api_key_set"] is True
    assert "401" in d_401["warning"]
    assert len(d_401["models"]) >= 1

    # Network exception -> 200 curated fallback
    _patch_httpx(monkeypatch, lambda url, headers: httpx.ConnectError("connection refused"))
    r_net = client.post("/api/brain/models", json={"provider": "deepseek", "api_key_env": "OPENAI_API_KEY"})
    assert r_net.status_code == 200
    d_net = r_net.json()
    assert d_net["ok"] is False
    assert d_net["source"] == "curated"
    assert "connection refused" in d_net["warning"]
    assert len(d_net["models"]) >= 1


# ── 6. Security: SSRF base_url & Invalid api_key_env ──


@pytest.mark.parametrize(
    "bad_url,provider",
    [
        ("http://169.254.169.254/latest", "openai_compat"),
        ("http://[::ffff:169.254.169.254]/v1", "ollama"),
        ("file:///etc/passwd", "openai_compat"),
        ("http://127.0.0.1:8080/v1", "openai"),
    ],
)
def test_brain_models_blocks_ssrf_base_url(env, bad_url, provider):
    client, _, _ = env
    resp = client.post("/api/brain/models", json={"provider": provider, "base_url": bad_url})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


@pytest.mark.parametrize("bad_env", ["SMTP_PASSWORD", "DASHBOARD_TOKEN", "AWS_SECRET_ACCESS_KEY"])
def test_brain_models_blocks_invalid_api_key_env(env, bad_env):
    client, _, _ = env
    resp = client.post(
        "/api/brain/models",
        json={"provider": "openai_compat", "base_url": "https://api.openai.com/v1", "api_key_env": bad_env},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
