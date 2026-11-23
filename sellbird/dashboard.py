"""Sellbird Dashboard — local web UI to set up, control, and monitor Sellbird."""

import asyncio
import dataclasses
import inspect
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path

import aiosqlite
import yaml
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, Response,
)
from pydantic import BaseModel, Field
from starlette.middleware import Middleware
from starlette.routing import Mount

logger = logging.getLogger("sellbird.dashboard")

from sellbird import agent_policy  # noqa: E402
from sellbird import dashboard_auth as auth  # noqa: E402
from sellbird.paths import PACKAGE_ROOT, PROJECT_ROOT, is_checkout  # noqa: E402
from sellbird import storage  # noqa: E402
from sellbird.pidfile import LOCK_FILE, loop_lock_held, pid_alive  # noqa: E402
from sellbird.textio import read_user_text  # noqa: E402
from sellbird.workspace_home import NotOnHosted, current_home, default_home, use_home  # noqa: E402
DB_PATH = PROJECT_ROOT / "data" / "sellbird.db"
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "sellbird.yaml"
BRAIN_CONFIG_FILE = PROJECT_ROOT / "brain.yaml"
PID_FILE = PROJECT_ROOT / "data" / "sellbird.pid"
LOG_FILE = PROJECT_ROOT / "data" / "sellbird.log"
TOKEN_FILE = PROJECT_ROOT / "data" / "dashboard-token"
AGENT_TOKEN_FILE = PROJECT_ROOT / "data" / "agent-token"


def _in_custom_home() -> bool:
    """True when use_home() is active for a non-default workspace."""
    return current_home() != default_home()


def _home_root() -> Path:
    if _in_custom_home():
        return Path(current_home().root)
    return Path(PROJECT_ROOT)


def _db_path() -> Path:
    if _in_custom_home():
        return Path(current_home().db_path)
    return Path(DB_PATH)


def _data_dir() -> Path:
    if _in_custom_home():
        return Path(current_home().data_dir)
    return Path(DB_PATH).parent


def _env_file() -> Path:
    if _in_custom_home():
        return Path(current_home().env_file)
    return Path(ENV_FILE)


def _pid_file() -> Path:
    if _in_custom_home():
        return _data_dir() / "sellbird.pid"
    return Path(PID_FILE)


def _log_file() -> Path:
    if _in_custom_home():
        return _data_dir() / "sellbird.log"
    return Path(LOG_FILE)


def _lock_file() -> Path:
    if _in_custom_home():
        return _data_dir() / "sellbird.lock"
    return Path(LOCK_FILE)


def _brain_config_file() -> Path:
    default_brain = PROJECT_ROOT / "brain.yaml"
    if Path(BRAIN_CONFIG_FILE) != default_brain:
        return Path(BRAIN_CONFIG_FILE)
    if _in_custom_home():
        return _home_root() / "brain.yaml"
    if Path(ENV_FILE) != PROJECT_ROOT / ".env":
        return Path(ENV_FILE).parent / "brain.yaml"
    if Path(DB_PATH) != PROJECT_ROOT / "data" / "sellbird.db":
        parent = Path(DB_PATH).parent
        root = parent.parent if parent.name == "data" else parent
        return root / "brain.yaml"
    return default_brain


def _resolve_config_file() -> Path:
    """sellbird.local.yaml wins when present — same resolution as config.py's
    loader, so the dashboard doesn't judge setup complete against the
    tracked sellbird.yaml template while the real trained config sits in the
    gitignored local override."""
    if _in_custom_home():
        cfg = current_home().config_file or (_home_root() / "sellbird.yaml")
        local = Path(cfg).with_name("sellbird.local.yaml")
        if local.exists():
            return local
        return Path(cfg)
    from sellbird.config import _find_config_file, ConfigFileNotFoundError
    try:
        return Path(_find_config_file())
    except ConfigFileNotFoundError:
        return CONFIG_FILE

# No /docs, /redoc or /openapi.json: the API is for this UI only, and every
# route except the static shell needs the token (see dashboard_auth).
app = FastAPI(title="Sellbird Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
token_store = auth.TokenStore(TOKEN_FILE)
agent_token_store = auth.TokenStore(AGENT_TOKEN_FILE)
# Innermost first: the guard (added next) sets the principal before this runs.
app.add_middleware(agent_policy.AgentPolicy)
app.add_middleware(auth.DashboardGuard, store=token_store, agent_store=agent_token_store)


class home_middleware:
    """A request runs in its app's home (``app.state.home``, see build_app).

    Pure ASGI, so the ContextVar set here is seen by everything inside it:
    the guard, async handlers and the sync handlers Starlette runs in its
    threadpool. An app with no ``state.home`` sets nothing, and the default
    home (the module globals) applies, which is every self-hosted install."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        home = None
        if scope["type"] in ("http", "websocket"):
            home = getattr(getattr(scope.get("app"), "state", None), "home", None)
        if home is None:
            await self.app(scope, receive, send)
            return
        with use_home(home):
            await self.app(scope, receive, send)


# Added last, so it is the outermost middleware.
app.add_middleware(home_middleware)


@app.exception_handler(NotOnHosted)
async def _not_on_hosted(request: Request, exc: NotOnHosted):
    return JSONResponse(exc.payload, status_code=409)

# Sellbird process tracking
_sellbird_process: subprocess.Popen | None = None
_sellbird_started_at: datetime | None = None
_env_lock = asyncio.Lock()


# ── Helpers ──


async def query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return results as list of dicts.

    Never raises: a missing DB file, missing table, or malformed schema
    returns [] so no dashboard route can 500 on an empty install.
    """
    db_path = _db_path()
    if not db_path.exists():
        return []
    try:
        async with storage.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("query_db failed (%s): %s", sql.split(None, 4)[:4], e)
        return []


def _mask_key(key: str) -> str:
    """Mask an API key for display: show first 4 and last 4 chars."""
    if not key or len(key) < 10:
        return "****" if key else ""
    return key[:4] + "****" + key[-4:]


def _read_env_file() -> dict[str, str]:
    """Read .env file and return as dict."""
    env_file = _env_file()
    env_vars = {}
    if env_file.exists():
        for line in read_user_text(env_file).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


def _env_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    key = stripped.partition("=")[0].strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    return key


def _write_env_file(updates: dict[str, str]):
    """Set keys in .env, keeping every other line (comments, order, other
    keys) as it was.

    Written to a private temporary file in the same directory and swapped
    in atomically, so a crash never leaves a half-written .env and the
    credentials are never readable by other users, even for a moment: the
    result is owner-only (0600 on POSIX) whether .env existed or not. A
    symlinked .env is refused rather than replaced or followed.
    """
    env_file = _env_file()
    if env_file.is_symlink():
        raise OSError(f"{env_file} is a symbolic link; edit it by hand.")
    if env_file.exists() and not env_file.is_file():
        raise OSError(f"{env_file} is not a regular file.")
    lines = (read_user_text(env_file).splitlines()
             if env_file.exists() else [])

    out, written = [], set()
    for line in lines:
        key = _env_key(line)
        if key in updates:
            if key in written:
                continue  # a later duplicate would override the new value
            out.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in written]

    env_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(env_file.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, env_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    if not (_in_custom_home() and current_home().hosted):
        load_dotenv(str(env_file), override=True)


def _check_sellbird_pid() -> int | None:
    """Check if there's a running Sellbird process from a PID file."""
    global _sellbird_process, _sellbird_started_at
    if _sellbird_process and _sellbird_process.poll() is None:
        return _sellbird_process.pid
    pid_file = _pid_file()
    lock_file = _lock_file()
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        # pid_alive, never os.kill(pid, 0): on Windows that terminates the loop.
        # And only while a loop holds the lock: after a crash or a reboot the
        # PID can belong to an unrelated program, which Stop must not signal.
        if pid is not None and pid_alive(pid) and loop_lock_held(lock_file):
            return pid
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
    return None


# ── Setup Status ──


@app.get("/api/setup-status")
async def get_setup_status():
    """Check what's configured and what still needs setup."""
    checks = []
    env_file = _env_file()
    gmail_tok = current_home().gmail_token_file if _in_custom_home() else (PROJECT_ROOT / "data" / "gmail_token.json")
    m365_tok = current_home().m365_token_file if _in_custom_home() else (PROJECT_ROOT / "data" / "m365_token.json")
    skills_dir = current_home().skills_dir if _in_custom_home() else (PROJECT_ROOT / "skills")

    # 1. Venv
    checks.append({
        "id": "venv", "label": "Python virtual environment",
        # An installed app (pipx) has no checkout venv to make.
        "done": (not is_checkout(PACKAGE_ROOT)) or (PACKAGE_ROOT / ".venv").is_dir(),
        "required": True,
        "help": "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e .",
    })

    # 2. Env file
    env_vars = _read_env_file()
    env_exists = env_file.exists() and bool(env_vars)
    checks.append({
        "id": "env_file", "label": "Environment file (.env)",
        "done": env_exists,
        "required": True,
        "help": "Go to the Settings tab to enter your API keys.",
    })

    # 3. Email provider configured (matches channels.email.provider)
    provider = _current_provider()

    def _has(*keys):
        return all((env_vars.get(k, "") or os.getenv(k, "")).strip() for k in keys)

    if provider == "gmail":
        provider_done = _has("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET") and Path(gmail_tok).is_file()
        provider_help = ("Add your Google OAuth client ID and secret in Settings, then sign "
                         "in on the Inboxes tab (or run 'sellbird gmail auth').")
    elif provider == "microsoft":
        provider_done = _has("M365_CLIENT_ID") and Path(m365_tok).is_file()
        provider_help = ("Add your Microsoft 365 client and tenant IDs in Settings, then sign "
                         "in on the Inboxes tab (or run 'sellbird microsoft auth').")
    elif provider == "smtp":
        provider_done = _has("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD")
        provider_help = "Enter your SMTP host, username, and password in Settings."
    else:
        instantly_key = env_vars.get("INSTANTLY_API_KEY", "") or os.getenv("INSTANTLY_API_KEY", "")
        provider_done = bool(instantly_key) and instantly_key != "your_instantly_api_key_here"
        provider_help = "Enter your Instantly API key in Settings."
    checks.append({
        "id": "email_provider", "label": f"Email provider configured ({provider})",
        "done": provider_done,
        "required": True,
        "help": provider_help,
    })

    # 4. Email verification (needed for addresses to be sendable, not 'guess')
    verifier_set = _has("REOON_API_KEY") or _has("ZEROBOUNCE_API_KEY") or _has("HUNTER_API_KEY")
    checks.append({
        "id": "verifier", "label": "Email verification key",
        "done": verifier_set,
        "required": True,
        "help": "Add a Reoon (free 600/mo), ZeroBounce, or Hunter key in Settings — "
                "without one, found emails stay 'guess' and are never sent.",
    })

    # 5. Config valid
    config_valid = False
    config_path = _resolve_config_file()
    if config_path.exists():
        try:
            cfg = yaml.safe_load(read_user_text(config_path))
            company = cfg.get("persona", {}).get("company", "")
            product = cfg.get("product", {}).get("name", "")
            config_valid = company not in ("Your Company", "") and product not in ("Your Product", "")
        except Exception:
            pass
    checks.append({
        "id": "config", "label": f"Sellbird configured ({config_path.name})",
        "done": config_valid,
        "required": True,
        "help": "Train Sellbird on your product. Use the trainer or set up manually through Claude.",
    })

    # 6. Product trained
    product_trained = (Path(skills_dir) / "product_knowledge.md").exists()
    checks.append({
        "id": "product_trained", "label": "Product knowledge trained",
        "done": product_trained,
        "required": True,
        "help": "Run: sellbird train https://yourwebsite.com (or set up through Claude).",
    })

    # 7. LinkedIn (optional)
    linkedin_email = env_vars.get("LINKEDIN_EMAIL", "") or os.getenv("LINKEDIN_EMAIL", "")
    linkedin_pass = env_vars.get("LINKEDIN_PASSWORD", "") or os.getenv("LINKEDIN_PASSWORD", "")
    checks.append({
        "id": "linkedin", "label": "LinkedIn credentials",
        "done": bool(linkedin_email) and bool(linkedin_pass),
        "required": False,
        "help": "Optional. Enter your LinkedIn credentials in Settings to enable LinkedIn prospecting.",
    })

    # 8. Cloudflare (optional)
    cf_id = env_vars.get("CLOUDFLARE_ACCOUNT_ID", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    cf_token = env_vars.get("CLOUDFLARE_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")
    checks.append({
        "id": "cloudflare", "label": "Cloudflare deep crawling",
        "done": bool(cf_id) and bool(cf_token),
        "required": False,
        "help": "Optional. For JavaScript-rendered website crawling during training.",
    })

    # 9. Warmup service: TrulyInbox connected, or another service the user named
    from sellbird.integrations import trulyinbox as ti
    try:
        state = _state()
        await state.init_db()
        choice = await ti.get_choice(state)
    except Exception:
        choice = ""
    if _has("TRULYINBOX_API_KEY"):
        choice = "connected"
    checks.append({
        "id": "warmup_service",
        "label": {"connected": "Warmup: TrulyInbox connected",
                  "other": "Warmup: your own warmup service"}.get(choice, "Warm up your mailbox"),
        "done": bool(choice),
        # Instantly warms its own accounts, so there it's a suggestion, not a step.
        "required": provider != "instantly",
        "help": ("A new mailbox needs 2-4 weeks of warmup before cold email, and warmup "
                 "keeps it out of spam after that. We recommend TrulyInbox: connect it and "
                 "Sellbird shows whether your mailbox is warmed."),
        "link": ti.AFFILIATE_URL,
    })

    required_checks = [c for c in checks if c["required"]]
    completed_required = sum(1 for c in required_checks if c["done"])

    return {
        "checks": checks,
        "completed": completed_required,
        "total_required": len(required_checks),
        "percent": int(completed_required / len(required_checks) * 100) if required_checks else 0,
    }


# ── Settings ──


ALLOWED_ENV_KEYS = (
    # Core email, verification & LinkedIn/Cloudflare
    "INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
    "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
    "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
    "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
    "M365_CLIENT_ID", "M365_TENANT_ID",
    "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY",
    # AI Router & OpenAI-compatible model providers
    "OPENAI_BASE_URL", "OPENAI_MODEL",
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
    "TOGETHER_API_KEY", "FIREWORKS_API_KEY", "CEREBRAS_API_KEY",
    "XAI_API_KEY",
    # Search & Scraping
    "TAVILY_API_KEY", "EXA_API_KEY", "SEARXNG_URL",
    "FIRECRAWL_API_KEY", "SERPER_API_KEY", "SCAVIO_API_KEY",
    "TINYFISH_API_KEY",
    # Calendar, CRM & Operator Copilot
    "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID",
    "TWENTY_API_KEY", "TWENTY_BASE_URL",
    "WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "SLACK_WEBHOOK_URL",
)


def _current_provider() -> str:
    """Read channels.email.provider from the active config (best-effort)."""
    try:
        cfg = yaml.safe_load(read_user_text(_resolve_config_file())) or {}
        return ((cfg.get("channels") or {}).get("email") or {}).get("provider", "instantly")
    except Exception:
        return "instantly"


@app.get("/api/settings")
async def get_settings():
    """Get current settings — presence flags only for secrets, never raw values."""
    env_vars = _read_env_file()
    for key in ALLOWED_ENV_KEYS:
        if key not in env_vars:
            env_vars[key] = os.getenv(key, "")

    def is_set(k):
        return bool((env_vars.get(k) or "").strip())

    gmail_tok = current_home().gmail_token_file if _in_custom_home() else (PROJECT_ROOT / "data" / "gmail_token.json")
    m365_tok = current_home().m365_token_file if _in_custom_home() else (PROJECT_ROOT / "data" / "m365_token.json")

    return {
        "provider": _current_provider(),
        # Non-secret values echo back so fields repopulate; secrets are
        # presence-only so keys never leave the box.
        "instantly_api_key_set": is_set("INSTANTLY_API_KEY"),
        "linkedin_email": env_vars.get("LINKEDIN_EMAIL", ""),
        "linkedin_password_set": is_set("LINKEDIN_PASSWORD"),
        "cloudflare_account_id": env_vars.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "cloudflare_api_token_set": is_set("CLOUDFLARE_API_TOKEN"),
        "gmail_client_id": env_vars.get("GMAIL_CLIENT_ID", ""),
        "gmail_client_secret_set": is_set("GMAIL_CLIENT_SECRET"),
        "gmail_authorized": Path(gmail_tok).is_file(),
        "smtp_host": env_vars.get("SMTP_HOST", ""),
        "smtp_port": env_vars.get("SMTP_PORT", ""),
        "smtp_username": env_vars.get("SMTP_USERNAME", ""),
        "smtp_password_set": is_set("SMTP_PASSWORD"),
        "imap_host": env_vars.get("IMAP_HOST", ""),
        "imap_port": env_vars.get("IMAP_PORT", ""),
        "imap_username": env_vars.get("IMAP_USERNAME", ""),
        "imap_password_set": is_set("IMAP_PASSWORD"),
        "m365_client_id": env_vars.get("M365_CLIENT_ID", ""),
        "m365_tenant_id": env_vars.get("M365_TENANT_ID", ""),
        "m365_authorized": Path(m365_tok).is_file(),
        "reoon_api_key_set": is_set("REOON_API_KEY"),
        "zerobounce_api_key_set": is_set("ZEROBOUNCE_API_KEY"),
        "hunter_api_key_set": is_set("HUNTER_API_KEY"),
        # AI Providers
        "openai_base_url": env_vars.get("OPENAI_BASE_URL", ""),
        "openai_model": env_vars.get("OPENAI_MODEL", ""),
        "openrouter_api_key_set": is_set("OPENROUTER_API_KEY"),
        "openai_api_key_set": is_set("OPENAI_API_KEY"),
        "gemini_api_key_set": is_set("GEMINI_API_KEY"),
        "deepseek_api_key_set": is_set("DEEPSEEK_API_KEY"),
        "groq_api_key_set": is_set("GROQ_API_KEY"),
        "mistral_api_key_set": is_set("MISTRAL_API_KEY"),
        "together_api_key_set": is_set("TOGETHER_API_KEY"),
        "fireworks_api_key_set": is_set("FIREWORKS_API_KEY"),
        "cerebras_api_key_set": is_set("CEREBRAS_API_KEY"),
        "xai_api_key_set": is_set("XAI_API_KEY"),
        # Search & Scraping
        "tavily_api_key_set": is_set("TAVILY_API_KEY"),
        "exa_api_key_set": is_set("EXA_API_KEY"),
        "searxng_url": env_vars.get("SEARXNG_URL", ""),
        "firecrawl_api_key_set": is_set("FIRECRAWL_API_KEY"),
        "serper_api_key_set": is_set("SERPER_API_KEY"),
        "scavio_api_key_set": is_set("SCAVIO_API_KEY"),
        "tinyfish_api_key_set": is_set("TINYFISH_API_KEY"),
        # Calendar, CRM & Copilot
        "calcom_api_key_set": is_set("CALCOM_API_KEY"),
        "calcom_event_type_id": env_vars.get("CALCOM_EVENT_TYPE_ID", ""),
        "twenty_api_key_set": is_set("TWENTY_API_KEY"),
        "twenty_base_url": env_vars.get("TWENTY_BASE_URL", ""),
        "webhook_url_set": is_set("WEBHOOK_URL"),
        "telegram_bot_token_set": is_set("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": env_vars.get("TELEGRAM_CHAT_ID", ""),
        "slack_webhook_url_set": is_set("SLACK_WEBHOOK_URL"),
    }


@app.post("/api/settings/env")
async def save_env_settings(request: Request):
    """Save environment variables to .env file."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    async with _env_lock:
        updates = {}
        for key in ALLOWED_ENV_KEYS:
            if key in data and data[key] is not None:
                value = data[key]
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    return JSONResponse({"success": False, "message": f"{key} must be text."},
                                        status_code=400)
                value = str(value)
                # A port Sellbird can't parse would stop it starting at all.
                if key.endswith("_PORT") and value.strip() and not value.strip().isdigit():
                    return JSONResponse({"success": False, "message": f"{key} must be a number."},
                                        status_code=400)
                if len(value) > 4096 or "\x00" in value:
                    return JSONResponse({"success": False, "message": f"{key} is invalid."},
                                        status_code=400)
                # Strip newlines so a crafted value can't inject extra .env entries
                updates[key] = value.replace("\n", " ").replace("\r", " ").strip()
        if updates:
            try:
                _write_env_file(updates)
            except Exception as e:
                logger.warning("Failed to write .env: %s", e)
                return {"success": False, "message": "Could not write .env file."}
            # Apply immediately to os.environ so AI Router, Search, CRM, Copilot
            # and OAuth sign-ins work right away without restarting the server.
            if not (_in_custom_home() and current_home().hosted):
                for key, val in updates.items():
                    if val:
                        os.environ[key] = val
                    else:
                        os.environ.pop(key, None)
    return {"success": True}


@app.post("/api/trulyinbox/connect")
async def trulyinbox_connect(request: Request):
    """Check a TrulyInbox key against the sending mailbox, then save it to .env."""
    from sellbird import deliverability as d
    from sellbird.integrations import trulyinbox as ti
    try:
        data = await request.json()
    except Exception:
        data = None
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip() or len(key) > 512:
        return JSONResponse({"success": False, "message": "Paste your TrulyInbox API key."},
                            status_code=400)
    key = key.strip()
    try:
        from sellbird.config import load_config
        config = load_config()
        address, warmup = ti.sending_address(config), config.channels.email.warmup
    except Exception:
        address, warmup = "", None
    if not address:
        return JSONResponse({"success": False, "message": ti.NO_ADDRESS}, status_code=400)
    workspace = (_read_env_file().get("TRULYINBOX_WORKSPACE_ID", "")
                 or os.getenv("TRULYINBOX_WORKSPACE_ID", "")).strip()
    ti.forget_rejected(key)
    try:
        client = ti.TrulyInbox(key, workspace)
        health = await client.mailbox_health(address)
    except ti.TrulyInboxError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)
    updates = {"TRULYINBOX_API_KEY": key}
    if not workspace:
        updates["TRULYINBOX_WORKSPACE_ID"] = client.workspace_id
    async with _env_lock:
        try:
            _write_env_file(updates)
        except Exception as e:
            logger.warning("Failed to write .env: %s", e)
            return {"success": False, "message": "Could not write .env file."}
    state = _state()
    await state.init_db()
    await ti.set_choice(state, "connected")
    return {"success": True, "message": d.check_warmup_service(health, warmup).detail}


@app.get("/api/rename-notice")
async def get_rename_notice():
    """Whether to say "Harvey is now Sellbird": once, after an upgrade moved files."""
    from sellbird import legacy
    return {"show": legacy.welcome_pending(legacy.PROJECT_ROOT)}


@app.post("/api/rename-notice/dismiss")
async def dismiss_rename_notice():
    from sellbird import legacy
    legacy.dismiss_welcome(legacy.PROJECT_ROOT)
    return {"success": True}


@app.post("/api/trulyinbox/other")
async def trulyinbox_other():
    """The user warms their mailbox with another service: stop recommending one."""
    from sellbird.integrations import trulyinbox as ti
    state = _state()
    await state.init_db()
    await ti.set_choice(state, "other")
    return {"success": True}


# ── Your offer: what every first email ends by offering (sellbird.offer) ──

def _offer_config_file() -> Path:
    """Where the offer is saved. The tracked sellbird.yaml template stays
    untrained: your offer goes in the gitignored sellbird.local.yaml, which
    then holds your whole config (as `sellbird train` does)."""
    target = _resolve_config_file()
    if target.name == "sellbird.yaml":
        local = target.with_name("sellbird.local.yaml")
        if not local.exists() and target.exists():
            shutil.copyfile(target, local)
        target = local
    return target


async def _offer_brain():
    from sellbird.brain import Brain
    from sellbird.config import load_config
    config = load_config()
    state = _state()
    await state.init_db()
    return Brain(state, config.brain), state, config


async def _close_brain(brain) -> None:
    if brain is not None and hasattr(brain, "aclose"):
        try:
            res = brain.aclose()
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            logger.debug("brain.aclose failed: %s", e)


async def _brain_within_budget(brain, config) -> bool:
    if brain is None or not hasattr(brain, "is_within_budget"):
        return True
    usage = getattr(config, "usage", None)
    pct = getattr(usage, "max_daily_claude_percent", 80)
    max_calls = max(int(200 * (pct / 100)), 1)
    max_usd = float(getattr(usage, "max_daily_usd", 5.0))
    return bool(await brain.is_within_budget(max_calls, max_percent=pct, max_usd=max_usd))


@app.get("/api/offer")
async def get_offer():
    from sellbird.config import load_config
    from sellbird.offer import offer_view
    try:
        return offer_view(load_config())
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read your config: {e}"}, status_code=500)


@app.post("/api/offer/suggest")
async def suggest_offer():
    """A few offers that fit your product. One model call; nothing is saved."""
    from sellbird.offer import suggest_offers
    brain = None
    try:
        brain, _, config = await _offer_brain()
        if not await _brain_within_budget(brain, config):
            return JSONResponse(
                {"error": "Daily AI budget reached. Raise usage.max_daily_usd or try tomorrow."},
                status_code=429,
            )
        ideas = await suggest_offers(brain, config)
    except Exception as e:
        logger.warning(f"Offer suggestions failed: {e}")
        return JSONResponse({"error": "Sellbird couldn't come up with offers just now. Try again."},
                            status_code=502)
    finally:
        await _close_brain(brain)
    if not ideas:
        return JSONResponse({"error": "Sellbird couldn't come up with offers just now. Try again."},
                            status_code=502)
    return {"ideas": ideas}


@app.post("/api/offer/preview")
async def preview_offer(request: Request):
    """One real first email written with this offer. Nothing is saved or sent."""
    from sellbird.offer import MAX_OFFER_CHARS, preview_email
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    entry = " ".join(str((body or {}).get("entry") or "").split())[:MAX_OFFER_CHARS]
    if not entry:
        return JSONResponse({"ok": False, "message": "Write an offer first."}, status_code=400)
    brain = None
    try:
        brain, state, config = await _offer_brain()
        if not await _brain_within_budget(brain, config):
            return JSONResponse(
                {"ok": False, "message": "Daily AI budget reached. Raise usage.max_daily_usd or try tomorrow."},
                status_code=429,
            )
        return await preview_email(brain, state, config, entry)
    except Exception as e:
        logger.warning(f"Offer preview failed: {e}")
        return JSONResponse({"ok": False, "message": "The preview failed. Try again."}, status_code=502)
    finally:
        await _close_brain(brain)


@app.post("/api/offer")
async def save_offer_route(request: Request):
    """Save the offer to product.offer.entry. The Writer reads it on its next run."""
    from sellbird.offer import MAX_OFFER_CHARS, save_offer
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    entry = " ".join(str((body or {}).get("entry") or "").split())[:MAX_OFFER_CHARS]
    ideas = (body or {}).get("ideas")
    ideas = [str(i) for i in ideas] if isinstance(ideas, list) else None
    target = _offer_config_file()
    try:
        save_offer(target, entry, ideas)
        from sellbird.offer import PROPOSED_KEY
        state = _state()
        await state.init_db()
        await state.delete_setting(PROPOSED_KEY)
    except Exception as e:
        return JSONResponse({"success": False, "message": f"Couldn't save to {target.name}: {e}"},
                            status_code=500)
    return {"success": True, "entry": entry,
            "message": "Saved. Every new first email ends with this offer." if entry
                       else "Saved. With no offer, first emails end with a question."}


@app.get("/api/company")
async def get_company():
    """Your company: everything Sellbird writes from, and any offer your agent proposed."""
    from sellbird.config import load_config
    from sellbird.offer import PROPOSED_KEY, company_view
    try:
        state = _state()
        await state.init_db()
        raw = await state.get_setting(PROPOSED_KEY)
        proposed = json.loads(raw) if raw else None
        return company_view(load_config(), _home_root(), proposed)
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read your config: {e}"}, status_code=500)


@app.post("/api/company")
async def save_company_route(request: Request):
    """Save one edited section of Your company to the config file."""
    from sellbird.offer import save_company
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    section = str((body or {}).get("section") or "")
    fields = (body or {}).get("fields")
    if not isinstance(fields, dict) or not fields:
        return JSONResponse({"success": False, "message": "Nothing to save."}, status_code=400)
    try:
        save_company(_offer_config_file(), section, fields)
    except ValueError as e:
        return JSONResponse({"success": False, "message": f"Not saved: {e}."}, status_code=400)
    except Exception as e:
        return JSONResponse({"success": False, "message": f"Couldn't save: {e}"}, status_code=500)
    return {"success": True, "message": "Saved. New emails use it from the next draft."}


@app.post("/api/offer/propose")
async def propose_offer(request: Request):
    """Your agent's proposed offer, waiting for you to use or dismiss it on
    Your company. Proposing changes nothing that's sent."""
    from sellbird.offer import MAX_OFFER_CHARS, PROPOSED_KEY
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    entry = " ".join(str((body or {}).get("entry") or "").split())[:MAX_OFFER_CHARS]
    why = " ".join(str((body or {}).get("why") or "").split())[:500]
    if not entry:
        return JSONResponse({"ok": False, "message": "An offer is required."}, status_code=400)
    state = _state()
    await state.init_db()
    await state.set_setting(PROPOSED_KEY, json.dumps({"entry": entry, "why": why,
                                                      "at": datetime.now(timezone.utc).isoformat()}))
    return {"ok": True, "message": "Proposed. The user confirms it on the Your company tab "
                                    "(dashboard → Your company → Your offer → Use this offer)."}


@app.delete("/api/offer/propose")
async def dismiss_proposed_offer():
    from sellbird.offer import PROPOSED_KEY
    state = _state()
    await state.init_db()
    await state.delete_setting(PROPOSED_KEY)
    return {"success": True}


@app.post("/api/settings/test-instantly")
async def test_instantly(request: Request):
    """Test an Instantly API key."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    api_key = str(data.get("api_key", "") or "")
    if not api_key:
        return {"success": False, "message": "No API key provided."}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.instantly.ai/api/v2/accounts",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                return {"success": True, "message": "Connected to Instantly."}
            else:
                return {"success": False, "message": f"API returned {resp.status_code}. Check your key."}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


# ── Companies ──


# The Companies tab shows the best-fit companies, up to this many; the CSV has all.
COMPANIES_TAB_LIMIT = 2000


@app.get("/api/companies")
async def get_companies():
    """Companies with contact counts, fit score and register status, best fit first."""
    rows = await query_db("""
        SELECT c.*,
            (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id) as contact_count
        FROM companies c ORDER BY c.created_at DESC
    """)
    try:
        from sellbird.config import load_config
        from sellbird.register import build_register
        register = {r["id"]: r for r in await build_register(query_db, load_config())}
        for c in rows:
            r = register.get(c["id"])
            if r:
                c["fit"], c["fit_reasons"] = r["fit"], r["fit_reasons"]
                c["register_status"], c["register_why"] = r["status"], r["why_not_contacted"]
        rows.sort(key=lambda c: c.get("fit", 0), reverse=True)
    except Exception as e:
        # A config that won't load must not take the Companies tab down with it.
        logger.warning(f"Company fit unavailable: {e}")
    return rows[:COMPANIES_TAB_LIMIT]


@app.get("/api/export/companies.csv")
async def export_companies():
    """The company register: every company, why it fits, why it wasn't contacted."""
    from sellbird.config import load_config
    from sellbird.register import build_register, to_csv
    try:
        text = to_csv(await build_register(query_db, load_config()))
    except Exception as e:
        logger.warning("Company register export failed: %s", e)
        return PlainTextResponse(f"Company register export failed: {e}", status_code=500)
    return PlainTextResponse(
        "﻿" + text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="company-register.csv"'},
    )


@app.get("/api/companies/{company_id}/profile")
async def get_company_profile(company_id: str):
    """One company's facts for its detail card: the register row plus what
    Sellbird observed on its site (latest value per signal, with the page)."""
    rows = await query_db("SELECT * FROM companies WHERE id = ?", (company_id,))
    if not rows:
        return JSONResponse({"error": "not found"}, status_code=404)
    observations = await query_db(
        "SELECT o.signal_code, o.value_text, o.value_num, o.evidence_url, o.observed_at, "
        "COALESCE(s.label, o.signal_code) AS label FROM observations o "
        "LEFT JOIN signal_codes s ON s.code = o.signal_code "
        "WHERE o.company_id = ? AND o.prospect_id = '' ORDER BY o.observed_at DESC LIMIT 200",
        (company_id,),
    )
    register = {}
    try:
        from sellbird.config import load_config
        from sellbird.register import build_register
        register = next(iter(await build_register(query_db, load_config(), company_id)), {})
    except Exception as e:
        logger.warning(f"Company profile register unavailable: {e}")
    return {"company": rows[0], "register": register, "observations": observations}


@app.get("/api/companies/{company_id}/contacts")
async def get_company_contacts(company_id: str):
    """Get all contacts for a specific company."""
    rows = await query_db(
        "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
        (company_id,),
    )
    return rows


# ── Feedback ──


@app.post("/api/feedback")
async def add_feedback(request: Request):
    """Add a comment/feedback on any entity."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    entity_type = str(data.get("entity_type", "") or "")[:50]
    entity_id = str(data.get("entity_id", "") or "")[:100]
    comment = str(data.get("comment", "") or "").strip()[:4000]
    if not comment:
        return {"success": False, "message": "Comment is required."}
    feedback_id = uuid.uuid4().hex[:12]
    db_path = _db_path()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with storage.connect(str(db_path)) as db:
            # Ensure the table exists so feedback works even on a fresh install
            await db.execute(
                """CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT,
                    entity_id TEXT,
                    comment TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )"""
            )
            await db.execute(
                "INSERT INTO feedback (id, entity_type, entity_id, comment) VALUES (?, ?, ?, ?)",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to save feedback: %s", e)
        return {"success": False, "message": "Could not save feedback."}
    return {"success": True, "id": feedback_id}


@app.get("/api/feedback/{entity_type}/{entity_id}")
async def get_feedback(entity_type: str, entity_id: str):
    """Get feedback for an entity."""
    rows = await query_db(
        "SELECT * FROM feedback WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC",
        (entity_type, entity_id),
    )
    return rows


# ── Sellbird Controls ──


@app.get("/api/sellbird/status")
async def get_sellbird_status():
    """Check if Sellbird is currently running."""
    pid = _check_sellbird_pid()
    started = _sellbird_started_at.isoformat() if _sellbird_started_at else None
    try:
        state = _state()
        await state.init_db()
        paused = await state.get_setting("sending_paused") or ""
    except Exception:
        paused = ""
    return {"running": pid is not None, "pid": pid, "started_at": started,
            "sending_paused": paused}


@app.post("/api/sellbird/start")
async def start_sellbird():
    """Start Sellbird's heartbeat loop as a subprocess."""
    global _sellbird_process, _sellbird_started_at

    if _check_sellbird_pid():
        return {"success": False, "message": "Sellbird is already running."}
    from sellbird.main import _needs_setup
    if _needs_setup():
        return {"success": False,
                "message": "Sellbird isn't set up yet. Run `sellbird setup` in a terminal first."}

    data_dir = _data_dir()
    log_file = _log_file()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Clear any stale stop sentinel before launching a new loop.
    (data_dir / "sellbird.stop").unlink(missing_ok=True)

    # The native Claude CLI installer puts claude in ~/.local/bin without
    # always adding it to PATH; the heartbeat can't think without it.
    env = os.environ.copy()
    local_bin = Path.home() / ".local" / "bin"
    if not shutil.which("claude") and local_bin.is_dir():
        env["PATH"] = str(local_bin) + os.pathsep + env.get("PATH", "")

    popen_kwargs: dict = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        log_handle = open(log_file, "a", encoding="utf-8")
        try:
            _sellbird_process = subprocess.Popen(
                [sys.executable, "-m", "sellbird", "run"],
                cwd=str(_home_root()),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                env=env,
                **popen_kwargs,
            )
        finally:
            # Child holds its own copies of the fds; don't leak ours.
            log_handle.close()
    except Exception as e:
        logger.warning("Failed to start Sellbird: %s", e)
        return {"success": False, "message": f"Failed to start Sellbird: {e}"}
    _sellbird_started_at = datetime.now()
    # The loop writes its own PID file once it holds the loop lock. Writing
    # the child's PID here would overwrite a running loop's record if this
    # child then finds the lock taken and exits.
    return {"success": True, "pid": _sellbird_process.pid}


@app.post("/api/sellbird/stop")
async def stop_sellbird():
    """Stop the Sellbird subprocess."""
    global _sellbird_process, _sellbird_started_at

    pid = _check_sellbird_pid()
    if not pid:
        return {"success": False, "message": "Sellbird is not running."}

    own = _sellbird_process if _sellbird_process and _sellbird_process.pid == pid else None

    def alive() -> bool:
        # Our own child is reaped by poll(); left unreaped it is a zombie,
        # which still answers signal 0.
        if own is not None:
            return own.poll() is None
        return pid_alive(pid)

    stop_file = _data_dir() / "sellbird.stop"
    if sys.platform == "win32":
        # On Windows os.kill(pid, signal.SIGTERM) calls TerminateProcess immediately,
        # which can cut off active SMTP sends mid-flight. Write sellbird.stop first
        # so the loop exits cleanly, and only force-terminate after 10s!
        try:
            stop_file.parent.mkdir(parents=True, exist_ok=True)
            stop_file.write_text(str(pid), encoding="utf-8")
        except OSError as e:
            logger.warning("Couldn't write stop sentinel %s: %s", stop_file, e)

        for _ in range(20):
            if not alive():
                break
            await asyncio.sleep(0.5)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as e:
                logger.warning("Couldn't signal Sellbird (pid %s): %s", pid, e)
            await asyncio.sleep(0.5)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            # Wait briefly for graceful shutdown
            for _ in range(10):
                if not alive():
                    break
                await asyncio.sleep(0.5)
            else:
                # Force kill if still running
                try:
                    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                except OSError:
                    pass
                await asyncio.sleep(0.5)
        except OSError as e:
            logger.warning("Couldn't signal Sellbird (pid %s): %s", pid, e)

    try:
        stop_file.unlink(missing_ok=True)
    except OSError:
        pass

    if alive():
        # Started by another user, or from an elevated terminal on Windows.
        return {"success": False,
                "message": f"Couldn't stop Sellbird (pid {pid}). Stop it where it was started."}

    _sellbird_process = None
    _sellbird_started_at = None
    _pid_file().unlink(missing_ok=True)

    return {"success": True}


@app.get("/api/sellbird/logs")
async def get_sellbird_logs():
    """Get recent log lines."""
    log_file = _log_file()
    if not log_file.exists():
        return {"lines": []}
    try:
        # Tail only the last 64KB so a huge log file never blocks the UI
        with open(log_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", errors="replace")
        lines = text.strip().splitlines()[-100:]
        return {"lines": lines}
    except Exception:
        return {"lines": []}


# ── Pipeline Data (existing endpoints) ──


def _pipeline_bucket(status: str | None, stage: str | None, intent: str | None,
                     email_status: str | None = None) -> str:
    """One funnel stage per contact, from its status and latest conversation."""
    status = status or "new"
    if status in ("new", "queued"):
        return "to_write"
    if status == "contacted":
        # A bounce leaves the status alone and marks the address invalid.
        return "closed_out" if email_status == "invalid" else "contacted"
    if status == "meeting":
        return "ready_to_meet"
    if status == "replied" and stage != "closed_lost":
        if stage == "closing":
            return "ready_to_meet"
        if intent == "interested" or stage in ("presenting", "negotiating"):
            return "interested"
        return "replied"
    return "closed_out"   # lost, opted out, bounced, unwritable


@app.get("/api/stats")
async def get_stats():
    """Pipeline overview stats."""
    try:
        prospects = await query_db(
            "SELECT status, COUNT(*) as count FROM prospects GROUP BY status"
        )
        prospect_total = sum(r["count"] for r in prospects)
        prospect_map = {r["status"]: r["count"] for r in prospects}

        campaigns = await query_db(
            "SELECT status, COUNT(*) as count FROM campaigns GROUP BY status"
        )
        campaign_map = {r["status"]: r["count"] for r in campaigns}

        conversations = await query_db(
            "SELECT status, COUNT(*) as count FROM conversations GROUP BY status"
        )
        convo_map = {r["status"]: r["count"] for r in conversations}

        # Where each contact stands. Prospect status alone stops at
        # "replied"; how a reply went lives on its latest conversation.
        rows = await query_db("""
            SELECT p.status, p.email_status, l.stage, l.intent, COUNT(*) AS count FROM prospects p
            LEFT JOIN (SELECT prospect_id, stage, intent, ROW_NUMBER() OVER (
                         PARTITION BY prospect_id ORDER BY updated_at DESC, created_at DESC) AS rn
                       FROM conversations) l ON l.prospect_id = p.id AND l.rn = 1
            GROUP BY p.status, p.email_status, l.stage, l.intent
        """)
        pipeline = dict.fromkeys(
            ("to_write", "contacted", "replied", "interested", "ready_to_meet", "closed_out"), 0)
        for r in rows:
            pipeline[_pipeline_bucket(r["status"], r["stage"], r["intent"],
                                      r["email_status"])] += r["count"]

        actions = await query_db("SELECT COUNT(*) as count FROM actions")
        action_count = actions[0]["count"] if actions else 0

        # usage_log rows are keyed by the LOCAL date (increment_usage /
        # get_usage_today use date.today()); SQLite's date('now') is UTC and
        # can select a different day's counts on either side of UTC midnight.
        local_today = date.today().isoformat()
        usage = await query_db(
            "SELECT * FROM usage_log WHERE date = ?",
            (local_today,),
        )
        usage_today = (usage[0].get("claude_calls") or 0) if usage else 0
        codex_today = (usage[0].get("codex_calls") or 0) if usage else 0
        openrouter_today = (usage[0].get("openrouter_calls") or 0) if usage else 0
        if not openrouter_today:
            ev = await query_db(
                "SELECT COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS c "
                "FROM usage_events WHERE date(created_at) = date('now') "
                "AND provider NOT IN ('claude', 'codex')"
            )
            openrouter_today = int((ev[0].get("c") or 0) if ev else 0)

        brain = _active_brain()
        transport_active = _is_transport_mode(brain)
        spend_today = await _spend_today_usd()
        max_usd = _active_max_daily_usd()

        return {
            "prospects": {"total": prospect_total, "by_status": prospect_map},
            "campaigns": {"total": sum(campaign_map.values()), "by_status": campaign_map},
            "conversations": {"total": sum(convo_map.values()), "by_status": convo_map},
            "pipeline": pipeline,
            "actions_total": action_count,
            "claude_calls_today": usage_today,
            "codex_calls_today": codex_today,
            "openrouter_calls_today": openrouter_today,
            "transport_active": transport_active,
            "spend_today_usd": spend_today,
            "max_daily_usd": max_usd,
            "brain_provider": _active_provider(),
            "brain_status": await _brain_status(),
        }
    except Exception as e:
        return {"error": str(e)}


_USAGE_SUM = (
    "COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS calls, "
    "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
    "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
    "ROUND(COALESCE(SUM(cost_usd), 0), 4) AS cost_usd"
)

_EMPTY_USAGE_ROW = {
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "cost_usd": 0.0,
}

_quota_clients: dict = {}


def _active_brain():
    """The brain config sellbird.yaml selects (None if unreadable)."""
    try:
        from sellbird.config import load_brain_config
        return load_brain_config()
    except Exception:
        return None


def _is_transport_mode(brain) -> bool:
    """True when an OpenAI-compatible router / custom transport handles LLM calls."""
    if brain is None:
        return False
    if bool((getattr(brain, "transport_factory", "") or "").strip()):
        return True
    return (getattr(brain, "provider", "") or "").strip().lower() in ("openrouter", "openai_compat")


def _read_brain_yaml_raw() -> dict:
    path = _brain_config_file()
    if not path.exists() or not path.is_file():
        return {}
    try:
        parsed = yaml.safe_load(read_user_text(path))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _brain_chain_summary() -> str:
    raw = _read_brain_yaml_raw()
    chain = raw.get("chain")
    if not isinstance(chain, list) or not chain:
        return ""
    parts = []
    for item in chain[:4]:
        if isinstance(item, dict):
            prov = str(item.get("provider") or "").strip()
            mod = str(item.get("model") or "").strip()
            if prov and mod:
                parts.append(f"{prov}:{mod}")
            elif prov or mod:
                parts.append(prov or mod)
    return " -> ".join(parts)


def _active_max_daily_usd() -> float:
    try:
        from sellbird.config import load_config
        cfg = load_config(str(_resolve_config_file()))
        return float(getattr(cfg.usage, "max_daily_usd", 5.0))
    except Exception:
        raw = _read_brain_yaml_raw()
        budget = raw.get("budget") if isinstance(raw.get("budget"), dict) else {}
        try:
            return float(budget.get("max_usd_per_day", 5.0))
        except (TypeError, ValueError):
            return 5.0


async def _spend_today_usd() -> float:
    if not _db_path().exists():
        return 0.0
    try:
        state = _state()
        if hasattr(state, "spend_today_usd"):
            val = await state.spend_today_usd()
            return round(float(val or 0.0), 4)
    except Exception:
        pass
    rows = await query_db(
        "SELECT ROUND(COALESCE(SUM(cost_usd), 0), 4) AS cost_usd "
        "FROM usage_events WHERE date(created_at) = date('now')"
    )
    return float((rows[0].get("cost_usd") or 0.0) if rows else 0.0)


async def _brain_status() -> dict:
    """Last recorded brain health transition ({} if none): lets the
    dashboard say "Codex paused: <why>" instead of looking healthy."""
    from sellbird.brain import BRAIN_STATUS_KEY, active_brain_status
    rows = await query_db("SELECT value FROM settings WHERE key = ?", (BRAIN_STATUS_KEY,))
    # Only the configured provider's health (a stale Codex pause isn't
    # Claude's).
    return active_brain_status(rows[0]["value"], _active_provider()) if rows else {}


def _active_provider() -> str:
    brain = _active_brain()
    if not brain:
        return ""
    if _is_transport_mode(brain):
        prov = (getattr(brain, "provider", "") or "").strip().lower()
        return prov if prov in ("openrouter", "openai_compat") else "openrouter"
    return brain.provider


def _quota_client_for(provider: str, model: str = ""):
    """One cached quota reader per provider (and Codex model). Each reads
    only its own subscription, so a Claude gauge is never shown as Codex or
    vice versa, and Codex shows the bucket metering the configured model."""
    key = (provider, model if provider == "codex" else "")
    if key not in _quota_clients:
        if provider == "codex":
            from sellbird.codex import CodexClient, CodexQuotaClient
            _quota_clients[key] = CodexQuotaClient(CodexClient(model=model))
        elif provider == "claude":
            from sellbird.integrations.quota import QuotaClient
            _quota_clients[key] = QuotaClient()
        else:
            return None
    return _quota_clients[key]


@app.get("/api/usage")
async def get_usage():
    """Token/cost accounting + live subscription quota or AI Router USD spend."""

    totals = {}
    for label, where in (
        ("today", "date(created_at) = date('now')"),
        ("week", "created_at >= datetime('now', '-7 days')"),
        ("month", "created_at >= datetime('now', '-30 days')"),
    ):
        rows = await query_db(f"SELECT {_USAGE_SUM} FROM usage_events WHERE {where}")
        totals[label] = {**_EMPTY_USAGE_ROW, **(rows[0] if rows else {})}

    def grouped(expr, alias):
        return (
            f"SELECT {expr} AS {alias}, {_USAGE_SUM} FROM usage_events "
            f"WHERE created_at >= datetime('now', '-30 days') "
            f"GROUP BY {alias} ORDER BY output_tokens DESC LIMIT 25"
        )

    by_agent = await query_db(grouped("CASE WHEN agent = '' THEN 'other' ELSE agent END", "agent"))
    by_task = await query_db(grouped("CASE WHEN task = '' THEN 'other' ELSE task END", "task"))
    by_model = await query_db(grouped("CASE WHEN model = '' THEN 'unknown' ELSE model END", "model"))
    by_provider = await query_db(grouped("provider", "provider"))
    by_day = await query_db(
        f"SELECT date(created_at) AS day, {_USAGE_SUM} FROM usage_events "
        f"WHERE created_at >= datetime('now', '-30 days') "
        f"GROUP BY day ORDER BY day ASC"
    )

    brain = _active_brain()
    transport_active = _is_transport_mode(brain)
    spend_today = await _spend_today_usd()
    max_usd = _active_max_daily_usd()

    local_today = date.today().isoformat()
    usage_rows = await query_db("SELECT * FROM usage_log WHERE date = ?", (local_today,))
    openrouter_today = int((usage_rows[0].get("openrouter_calls") or 0) if usage_rows else 0)
    if not openrouter_today:
        openrouter_today = int((totals.get("today") or {}).get("calls") or 0) if transport_active else 0

    if transport_active:
        prov = (getattr(brain, "provider", "") or "").strip().lower()
        provider = prov if prov in ("openrouter", "openai_compat") else "openrouter"
        return {
            "provider": provider,
            "transport_active": True,
            "chain_summary": _brain_chain_summary(),
            "spend_today_usd": spend_today,
            "max_daily_usd": max_usd,
            "openrouter_calls_today": openrouter_today,
            "quota": None,
            "quota_provider": "",
            "quota_status": "",
            "by_provider": by_provider,
            "totals": totals,
            "by_day": by_day,
            "by_agent": by_agent,
            "by_task": by_task,
            "by_model": by_model,
        }

    provider = brain.provider if brain else ""
    quota = None
    try:
        client = _quota_client_for(provider, brain.model if brain else "")
        if client is not None:
            quota = await client.get_utilization()
    except Exception as e:
        logger.debug("Quota lookup failed: %s", e)

    return {
        "provider": provider,
        "transport_active": False,
        "spend_today_usd": spend_today,
        "max_daily_usd": max_usd,
        "openrouter_calls_today": openrouter_today,
        "quota": quota,
        # Whose quota this is, even when it couldn't be read, so the UI shows
        # "unavailable" for the active provider instead of hiding the gauge.
        "quota_provider": provider,
        "quota_status": ("available" if quota else "unavailable") if provider else "",
        "by_provider": by_provider,
        "totals": totals,
        "by_day": by_day,
        "by_agent": by_agent,
        "by_task": by_task,
        "by_model": by_model,
    }


@app.get("/api/prospects")
async def get_prospects():
    rows = await query_db("SELECT * FROM prospects ORDER BY created_at DESC LIMIT 200")
    # Someone whose opt-out was undone keeps a way to see what happened.
    undone = {r["pid"] for r in await query_db(
        "SELECT DISTINCT json_extract(details_json, '$.prospect_id') AS pid FROM actions "
        "WHERE action_type = 'opt_out_undone' AND json_valid(details_json)")}
    for r in rows:
        r["opt_out_undone"] = r.get("id") in undone
    return rows


def _state():
    from sellbird.state import StateManager
    return StateManager(db_path=str(_db_path()))


# What a reviewer needs next to a draft to check its claims against the facts.
OUTBOX_CONTEXT_SIGNALS = (
    "TECH_STACK", "INCUMBENT_AGENCY", "HIRING_ROLE", "RUNNING_GOOGLE_ADS", "NO_ONLINE_BOOKING",
)


def _in(ids) -> str:
    return ",".join("?" for _ in ids)


async def _with_context(items: list[dict]) -> list[dict]:
    """Attach the campaign, company, contact and signals behind each draft."""
    if not items:
        return items
    pids = sorted({i["prospect_id"] for i in items if i.get("prospect_id")})
    cids = sorted({i["campaign_id"] for i in items if i.get("campaign_id")})

    people = {r["id"]: r for r in (await query_db(
        "SELECT p.id, p.first_name, p.last_name, p.title, p.source, p.email_status, "
        "p.score, p.personalization_notes, p.company_id, c.name AS company_name, "
        "c.domain, c.website, c.description, c.industry AS found_by "
        "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id "
        f"WHERE p.id IN ({_in(pids)})", tuple(pids)) if pids else [])}
    campaigns = {r["id"]: r["name"] for r in (await query_db(
        f"SELECT id, name FROM campaigns WHERE id IN ({_in(cids)})",
        tuple(cids)) if cids else [])}

    company_ids = sorted({p["company_id"] for p in people.values() if p.get("company_id")})
    signals: dict[str, dict[str, str]] = {}
    if company_ids:
        rows = await query_db(
            "SELECT o.company_id, o.signal_code, o.value_num, o.value_text, "
            "o.observed_at, o.run_id, "
            "COALESCE(NULLIF(sc.label, ''), o.signal_code) AS label FROM observations o "
            "LEFT JOIN signal_codes sc ON sc.code = o.signal_code "
            f"WHERE o.company_id IN ({_in(company_ids)}) "
            f"AND o.signal_code IN ({_in(OUTBOX_CONTEXT_SIGNALS)}) "
            "ORDER BY o.observed_at, o.id",
            (*company_ids, *OUTBOX_CONTEXT_SIGNALS),
        )
        # Oldest first, so the latest observation wins. A signal can be seen
        # several times at once (one HIRING_ROLE row per open role): every
        # value from the latest sighting shows, not just one of them.
        latest: dict[tuple, tuple] = {}
        for r in rows:
            if r["value_text"]:
                value = r["value_text"]
            elif r["value_num"] is None:
                value = ""
            else:
                value = "yes" if r["value_num"] else "no"
            key = (r["company_id"], r["label"])
            seen = (r["run_id"] or r["observed_at"])
            prev_seen, values = latest.get(key, (None, []))
            if seen != prev_seen:
                values = []
            if value not in values:
                values = values + [value]
            latest[key] = (seen, values)
        for (company_id, label), (_, values) in latest.items():
            signals.setdefault(company_id, {})[label] = ", ".join(v for v in values if v)

    for item in items:
        p = people.get(item.get("prospect_id")) or {}
        item["context"] = {
            "campaign": campaigns.get(item.get("campaign_id"), ""),
            "company": p.get("company_name", ""),
            "website": p.get("website") or (f"https://{p['domain']}" if p.get("domain") else ""),
            "description": p.get("description", ""),
            "found_by": p.get("found_by", ""),
            "contact": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
            "contact_title": p.get("title", ""),
            "contact_source": p.get("source", ""),
            "email_status": p.get("email_status", ""),
            "score": p.get("score"),
            "notes": p.get("personalization_notes", ""),
            "signals": signals.get(p.get("company_id"), {}),
        }
    return items


@app.get("/api/outbox")
async def get_outbox_api():
    """Outbox queue + kill-switch state for the Outbox tab."""
    try:
        state = _state()
        await state.init_db()
        # A send cut off mid-way shows up under Didn't send even when Sellbird
        # isn't running.
        await state.fail_stale_sending_outbox()
        return {
            **_compliance_view(),
            "paused": await state.get_setting("sending_paused"),
            "pending": await _with_context(
                await state.get_outbox(status="pending_review", limit=100)),
            # Mail going out right now leads the list.
            "approved": (await state.get_outbox(status="sending", limit=10)
                         + await state.get_outbox(status="approved", limit=50)),
            "sent": (await query_db(
                "SELECT * FROM outbox WHERE status = 'sent' "
                "ORDER BY sent_at DESC LIMIT 25")),
            "failed": (await query_db(
                "SELECT * FROM outbox WHERE status IN ('failed','rejected','cancelled') "
                "ORDER BY updated_at DESC LIMIT 25")),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sending-mode")
async def get_sending_mode():
    """Approval (every email waits for you) or autopilot, and what that means here."""
    from sellbird.config import load_config
    from sellbird.gate import _booking_prefix, allowed_link_domains
    path = _resolve_config_file()
    try:
        config = load_config(str(path))
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read {path.name}: {e}"}, status_code=500)
    state = _state()
    await state.init_db()
    return {
        "mode": "approval" if config.channels.email.require_approval else "autopilot",
        "provider": config.channels.email.provider,
        "file": path.name,
        "your_domains": sorted(allowed_link_domains(config)),
        "booking_page": _booking_prefix(config),
        "held": await state.count_held_for_review(),
    }


@app.post("/api/sending-mode")
async def set_sending_mode(request: Request):
    """Switch it. Autopilot needs {"mode": "autopilot", "confirm": "autopilot"};
    {"mode": "approval"} works at once and sends back to the Outbox whatever
    autopilot approved that hasn't gone out."""
    from sellbird.config import ConfigError, set_require_approval
    try:
        body = await request.json()
    except ValueError:
        body = None
    mode = body.get("mode") if isinstance(body, dict) else None
    if mode not in ("approval", "autopilot"):
        return _refuse('expected {"mode": "approval"} or {"mode": "autopilot", "confirm": "autopilot"}', 400)
    if mode == "autopilot" and body.get("confirm") != "autopilot":
        return _refuse('Type "autopilot" to confirm: Sellbird will send email without asking you.', 400)
    path = _resolve_config_file()
    try:
        set_require_approval(mode == "approval", path)
    except ConfigError as e:
        return _refuse(str(e), 409)
    state = _state()
    await state.init_db()
    requeued = await state.requeue_autopilot_approved() if mode == "approval" else 0
    await state.log_action("sending_mode", "dashboard", {"mode": mode, "file": path.name,
                                                         "requeued": requeued})
    out = {"success": True, "mode": mode}
    if mode == "approval":
        out["requeued"] = requeued
    return out


@app.post("/api/outbox/approve-all")
async def outbox_approve_all():
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox()
        # Held ones (possible planted link or leak) are approved one by one.
        return {"success": True, "approved": n, "held": await state.count_held_for_review()}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/approve")
async def outbox_approve(item_id: str):
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox(item_id)
        return {"success": bool(n)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/edit")
async def outbox_edit(item_id: str, request: Request):
    """Rewrite a draft awaiting review. Never approves it."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    subject = str(body.get("subject") or "").strip()
    text = str(body.get("body") or "").strip()
    try:
        state = _state()
        await state.init_db()
        item = await state.get_outbox_item(item_id)
        if not item or item.get("status") != "pending_review":
            return JSONResponse({"success": False,
                                 "message": "Only drafts awaiting review can be edited."},
                                status_code=409)
        if item.get("kind") == "reply":
            # Gmail threads a reply by its subject: a new one would land as a
            # fresh, unthreaded email to someone who wrote in. Only the body
            # of a reply is editable; its stored subject is kept exactly.
            subject = item.get("subject") or ""
        if not subject.strip() or not text:
            return JSONResponse({"success": False,
                                 "message": "Subject and body are both required."},
                                status_code=400)
        # The sender re-checks every email before it goes out; an edit it
        # would block is refused now, while the reviewer can still fix it,
        # instead of failing quietly at send time. Content rules only: the
        # recipient isn't something an edit can change.
        from sellbird.gate import pre_send_check
        checked = text
        footer = _compliance_view()["footer"]
        if item.get("kind") == "sequence" and footer and not text.rstrip().endswith(footer):
            checked = text.rstrip() + "\n\n" + footer  # as the Sender will send it
        gate = pre_send_check("reviewer@example.com", subject, checked)
        from sellbird.email_style import style_problems
        prospect = await state.get_prospect(item.get("prospect_id") or "")
        names = [n for n in (getattr(prospect, "company", ""), getattr(prospect, "first_name", ""),
                             getattr(prospect, "last_name", "")) if n]
        style = style_problems(subject, text, shape=False, names=names,
                               kind="reply" if item.get("kind") == "reply" else "cold")
        if style:
            return JSONResponse({"success": False,
                                 "message": "The sender would hold this: the email "
                                            + "; ".join(style)},
                                status_code=400)
        if not gate:
            return JSONResponse({"success": False,
                                 "message": "The sender would have blocked this: "
                                            + "; ".join(gate.reasons)},
                                status_code=400)
        if not await state.edit_outbox_draft(item_id, subject, text):
            return JSONResponse({"success": False,
                                 "message": "Only drafts awaiting review can be edited."},
                                status_code=409)
        await state.log_action("outbox_edited", "dashboard", {
            "outbox_id": item_id, "to_email": item.get("to_email", ""),
            "old_subject": item.get("subject", ""), "old_body": item.get("body", ""),
        })
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# A hand send is only offered to someone Sellbird could still email: never to
# someone who replied, opted out, bounced or was already contacted.
HAND_SENDABLE_STATUSES = ("new", "queued")
HAND_SENDABLE_EMAIL = ("verified", "risky")


def _sellbird_can_send() -> bool:
    """Can Sellbird send email itself (a connected mailbox, or Instantly)?

    Sending by hand from the dashboard is only offered when it can't: with
    two senders, any timing gap between them emails someone twice. If the
    config can't be read, the answer is yes (the safe side: no hand sends).
    """
    try:
        from sellbird.config import load_config, load_env
        from sellbird.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
        config, env = load_config(), load_env()
        from sellbird.config import legacy_sending_held
        email = config.channels.email
        if not email.enabled or legacy_sending_held(config):
            return False
        if (email.provider or "").strip().lower() in NATIVE_PROVIDERS:
            provider = get_mail_provider(config, env)
            return bool(provider and provider.is_configured()) or _extra_inboxes_connected()
        return bool(env.instantly_api_key)
    except Exception:
        return True


def _extra_inboxes_connected() -> bool:
    """Whether an inbox besides the main one is connected (sellbird inboxes):
    Sellbird sends from it even while the main mailbox isn't signed in."""
    import sqlite3
    db_path = _db_path()
    if not db_path.exists():
        return False
    try:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            row = db.execute("SELECT 1 FROM mailboxes WHERE state != 'retired' LIMIT 1").fetchone()
    except (sqlite3.Error, ValueError, OSError):
        return False   # no table yet: no extra inbox
    return row is not None


def _send_as() -> str:
    """The address Sellbird sends as, so Gmail opens the right account."""
    try:
        from sellbird.config import load_config
        return load_config().persona.email or ""
    except Exception:
        return ""


def _compliance_view() -> dict:
    """What the Sender adds under each cold email, so a reviewer sees the
    email as it will leave, and the warning when the address is missing."""
    try:
        from sellbird.config import compliance_footer_lines, compliance_notice, load_config
        config = load_config()
        return {"footer": "\n".join(compliance_footer_lines(config)),
                "compliance_notice": compliance_notice(config)}
    except Exception:
        return {"footer": "", "compliance_notice": ""}


async def _sellbird_committed_ids() -> set[str]:
    """Everyone Sellbird has approved or sent mail for (Outbox or Instantly).

    Approved mail can be going out at this moment, so these people are past
    the point where a hand send can be recorded safely. So is anyone whose
    send was cut off mid-way: it may have reached them. A draft that only
    awaits review isn't: marking the person sent cancels it.
    """
    from sellbird.state import MAYBE_SENT_PREFIX
    ids = {r["prospect_id"] for r in await query_db(
        "SELECT DISTINCT prospect_id FROM outbox WHERE status IN ('approved', 'sending', 'sent') "
        "OR (status = 'failed' AND substr(error, 1, ?) = ?)",
        (len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX))}
    for row in await query_db(
            "SELECT prospect_ids_json FROM campaigns WHERE COALESCE(instantly_campaign_id, '') != ''"):
        try:
            found = json.loads(row.get("prospect_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(found, list):
            ids.update(i for i in found if isinstance(i, str))
    return ids


async def _hand_sent_ids() -> set[str]:
    from sellbird.state import MANUAL_SEND_KEY
    rows = await query_db("SELECT key FROM settings WHERE key LIKE ?", (MANUAL_SEND_KEY + "%",))
    return {r["key"][len(MANUAL_SEND_KEY):] for r in rows}


async def _sellbird_sent_to(prospect_id: str) -> bool:
    """Did Sellbird itself email this person (native send, or an Instantly campaign)?

    A send in flight, or cut off mid-way, counts: it may have reached them.
    """
    from sellbird.state import MAYBE_SENT_PREFIX
    if await query_db(
            "SELECT 1 FROM outbox WHERE prospect_id = ? AND (status IN ('sending', 'sent') "
            "OR (status = 'failed' AND substr(error, 1, ?) = ?)) LIMIT 1",
            (prospect_id, len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX)):
        return True
    for row in await query_db(
            "SELECT prospect_ids_json FROM campaigns WHERE COALESCE(instantly_campaign_id, '') != ''"):
        try:
            if prospect_id in json.loads(row.get("prospect_ids_json") or "[]"):
                return True
        except (json.JSONDecodeError, TypeError):
            continue
    return False


def _refuse(message: str, code: int = 409):
    return JSONResponse({"success": False, "message": message}, status_code=code)


@app.post("/api/prospects/{prospect_id}/sent")
async def mark_prospect_sent(prospect_id: str, request: Request):
    """The user sent this email by hand (e.g. from Gmail): record it, or undo it."""
    from sellbird.state import HAND_SENT_REASON, MANUAL_SEND_KEY
    try:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict) or not isinstance(body.get("sent", True), bool):
            return _refuse('expected {"sent": true|false}', 400)
        sent = body.get("sent", True)
        rows = await query_db("SELECT status FROM prospects WHERE id = ?", (prospect_id,))
        if not rows:
            return _refuse("unknown prospect", 404)
        current = rows[0].get("status") or "new"
        state = _state()
        await state.init_db()
        key = MANUAL_SEND_KEY + prospect_id
        marked = bool(await state.get_setting(key))

        if sent:
            if marked:
                return {"success": True, "sent": True, "already": True}
            if current not in HAND_SENDABLE_STATUSES:
                # A reply, opt-out or close is never touched; neither is
                # someone Sellbird already emailed.
                return {"success": True, "sent": True, "status": current}
            # Allowed even when Sellbird can send: recording "I already emailed
            # them" can only ever stop Sellbird sending. Not once Sellbird has
            # approved or sent mail to them, though: that may be going out now.
            # The marker goes down FIRST: a drain that checks after this sees
            # it and cancels; one that checked before only holds an approved
            # row, which the check below then sees.
            await state.set_setting(key, current)
            if prospect_id in await _sellbird_committed_ids():
                await state.delete_setting(key)
                return _refuse("Sellbird has already approved or sent an email to this person. "
                               "Check the Outbox.")
            await state.update_prospect_status(prospect_id, "contacted")
            # Anything Sellbird had queued for them must not go out as well.
            changed = await state.cancel_pending_outbox_for_prospect(
                prospect_id, reason=HAND_SENT_REASON)
        else:
            if not marked:
                if current in HAND_SENDABLE_STATUSES:
                    return {"success": True, "sent": False}
                if current == "contacted":
                    # Setting them back would make Sellbird write and send again.
                    return _refuse("This wasn't marked as sent by hand here, so it stays sent.")
                return {"success": True, "sent": True, "status": current}
            if current != "contacted":
                # They replied, opted out or bounced since: that stands.
                return {"success": True, "sent": True, "status": current}
            previous = await state.get_setting(key) or "queued"
            await state.update_prospect_status(
                prospect_id, previous if previous in HAND_SENDABLE_STATUSES else "queued")
            await state.delete_setting(key)
            # What marking sent cancelled comes back for review, never straight out.
            changed = await state.restore_hand_cancelled_outbox(
                prospect_id, reason=HAND_SENT_REASON)
        if sent:
            # Open in Gmail adds today's footer; remember it as Sellbird's text.
            footer = _compliance_view()["footer"]
            await state.remember_footer_lines(footer.split("\n"))
        await state.log_action(
            action_type="manual_send" if sent else "manual_send_undone",
            agent="user",
            details={"prospect_id": prospect_id, "outbox_items": changed},
        )
        return {"success": True, "sent": sent}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/reject")
async def outbox_reject(item_id: str):
    try:
        state = _state()
        await state.init_db()
        if not await state.reject_outbox_item(item_id):
            return _refuse("That email is already sending or sent, so it can't be rejected.")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/sending/{action}")
async def sending_toggle(action: str):
    if action not in ("pause", "resume"):
        return JSONResponse({"success": False, "message": "unknown action"}, status_code=400)
    try:
        state = _state()
        await state.init_db()
        if action == "pause":
            await state.set_setting("sending_paused", "paused from dashboard")
        else:
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/export/prospects.csv")
async def export_prospects(all: bool = False, min_score: int = 0, email_status: str = ""):
    """Sequencer-ready CSV download of the prospect list."""
    from sellbird.export import export_prospects_csv

    state = _state()
    try:
        await state.init_db()
        statuses = [s.strip() for s in email_status.split(",") if s.strip()] or None
        _, text = await export_prospects_csv(
            state, email_statuses=statuses, min_score=min_score, include_all=all,
        )
    except Exception as e:
        logger.warning("Prospect export failed: %s", e)
        text = ""
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="prospects.csv"'},
    )


@app.get("/api/campaigns")
async def get_campaigns():
    rows = await query_db("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 100")
    sellbird_sends = _sellbird_can_send()
    send_as = "" if sellbird_sends else _send_as()
    footer = _compliance_view()["footer"]
    committed = await _sellbird_committed_ids()
    hand_sent = await _hand_sent_ids()
    for row in rows:
        try:
            row["sequence"] = json.loads(row.get("sequence_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["sequence"] = []
        try:
            row["prospect_ids"] = json.loads(row.get("prospect_ids_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["prospect_ids"] = []
        # Who each draft goes to, so the UI can open it in the user's own mail client.
        ids = row["prospect_ids"]
        row["recipients"] = await query_db(
            f"SELECT id, email, first_name, last_name, company, title, status, email_status, "
            f"source, personalization_notes FROM prospects "
            f"WHERE id IN ({','.join('?' * len(ids))}) AND email != ''",
            tuple(ids),
        ) if ids else []
        row["send_as"] = send_as
        # A cold email sent by hand needs the same footer Sellbird adds.
        row["footer"] = footer
        for r in row["recipients"]:
            r["sent_by_hand"] = r["id"] in hand_sent
            # "I already emailed them": always recordable before Sellbird commits.
            r["can_mark_sent"] = (
                r["id"] not in hand_sent and r["id"] not in committed
                and (r.get("status") or "new") in HAND_SENDABLE_STATUSES
            )
            # The Gmail button: only when Sellbird can't send itself.
            r["can_send_by_hand"] = (
                r["can_mark_sent"] and not sellbird_sends
                and (r.get("email_status") or "guess") in HAND_SENDABLE_EMAIL
            )
    return rows


@app.get("/api/frameworks")
async def get_frameworks():
    """How each copywriting framework is doing (the bandit experiment).

    The Campaigns tab shows this only when frameworks are configured or a
    campaign already carries a tag; the built-in shape alone has nothing
    to compare, so the card stays hidden.
    """
    configured = _configured_frameworks()
    state = _state()
    await state.init_db()
    stats = await state.framework_stats()
    rows = [{"framework": fw, "replies": replied, "finished": finished}
            for fw, (replied, finished) in sorted(stats.items()) if fw]
    return {"configured": configured, "rows": rows}


def _read_config_doc() -> dict:
    """The config file Sellbird reads, as a plain dict — without the full
    config model's required fields, so the dashboard works before training
    is done. {} when there is no file yet."""
    try:
        return yaml.safe_load(read_user_text(_resolve_config_file())) or {}
    except FileNotFoundError:
        return {}


def _configured_frameworks() -> list[str]:
    """writer.frameworks from the config file, filtered to the frameworks
    the bandit actually knows — a typo shows no arm that never runs."""
    from sellbird.bandit import FRAMEWORK_INSTRUCTIONS
    writer = _read_config_doc().get("writer")
    if not isinstance(writer, dict):
        return []
    fw = writer.get("frameworks")
    if not isinstance(fw, list):
        return []
    return [str(x) for x in fw
            if isinstance(x, str) and str(x).strip().lower() in FRAMEWORK_INSTRUCTIONS]


@app.get("/api/competitor-watch")
async def get_competitor_watch():
    """The configured watches plus the changes the watcher has logged."""
    enabled, watches = False, []
    cw = _read_config_doc().get("competitor_watch")
    if isinstance(cw, dict):
        enabled = bool(cw.get("enabled"))
        raw = cw.get("watches")
        if isinstance(raw, list):
            watches = [{"name": str(w.get("name") or ""), "url": str(w.get("url") or ""),
                        "kind": str(w.get("kind") or "homepage")}
                       for w in raw if isinstance(w, dict)]
    rows = await query_db(
        "SELECT details_json, created_at FROM actions "
        "WHERE action_type = 'competitor_change' ORDER BY created_at DESC LIMIT 20")
    changes = []
    for r in rows:
        try:
            details = json.loads(r.get("details_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            details = {}
        changes.append({"watch": details.get("watch", ""), "url": details.get("url", ""),
                        "kind": details.get("kind", ""), "at": str(r.get("created_at") or "")})
    return {"enabled": enabled, "watches": watches, "changes": changes}


@app.post("/api/competitor-watch")
async def save_competitor_watch(request: Request):
    """Save the watch list to the config file Sellbird reads."""
    from sellbird.collectors.competitor_watch import KINDS
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _refuse("expected a JSON object", 400)
    enabled = bool(body.get("enabled"))
    raw = body.get("watches") or []
    if not isinstance(raw, list) or len(raw) > 50:
        return _refuse("watches must be a list of at most 50 entries", 400)
    watches, seen = [], set()
    for w in raw:
        if not isinstance(w, dict):
            return _refuse("each watch must be an object", 400)
        name = str(w.get("name") or "").strip()[:120]
        url = str(w.get("url") or "").strip()[:2000]
        kind = str(w.get("kind") or "homepage").strip().lower()
        if not name and not url:
            continue  # a blank row the form left behind
        if not name or not url:
            return _refuse("Each watch needs both a name and a URL.", 400)
        if not url.startswith(("https://", "http://")):
            return _refuse(f"Watch '{name}': the URL must start with https:// or http://.", 400)
        if kind not in KINDS:
            kind = "homepage"
        if name.lower() in seen:
            return _refuse(f"Two watches are named '{name}' — names key the baseline, so make them unique.", 400)
        seen.add(name.lower())
        watches.append({"name": name, "url": url, "kind": kind})
    if enabled and not watches:
        return _refuse("Add at least one watch, or turn the watcher off.", 400)
    path = _resolve_config_file()
    try:
        doc = _read_config_doc()
        doc["competitor_watch"] = {"enabled": enabled, "watches": watches}
        _write_yaml_atomic(path, doc)
    except Exception as e:
        return JSONResponse({"success": False, "message": f"Couldn't save: {e}"}, status_code=500)
    state = _state()
    await state.init_db()
    await state.log_action("competitor_watch_saved", "dashboard",
                           {"enabled": enabled, "watches": len(watches)})
    return {"success": True, "message": "Saved. The watcher uses it from the next idle cycle."}


@app.post("/api/dns-check")
async def dns_check_route(request: Request):
    """MX/SPF/DKIM/DMARC for a domain — `sellbird dns-check` in the browser.

    DNS lookups block, so they run in a thread; nothing is written anywhere.
    """
    from sellbird import deliverability as d
    from sellbird.config import load_config
    try:
        body = await request.json()
    except ValueError:
        body = None
    domain = str((body or {}).get("domain") or "").strip().lower().rstrip(".")
    config = None
    try:
        config = load_config(str(_resolve_config_file()))
    except Exception:
        config = None
    if not domain:
        if config is None:
            return _refuse("No sending domain configured — pass a domain to check.", 400)
        from sellbird.integrations.trulyinbox import sending_address
        domain = sending_address(config).rpartition("@")[2].strip().lower().rstrip(".")
    if not d.DOMAIN_RE.match(domain):
        return _refuse("That doesn't look like a domain. Try yourdomain.com", 400)
    selector = ""
    if config is not None:
        selector = getattr(config.channels.email.warmup, "dkim_selector", "") or ""

    def _run():
        return d.dns_checks(domain, selector)

    checks = await asyncio.to_thread(_run)
    return {"domain": domain,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
            "failed": sum(c.status == d.FAIL for c in checks)}


@app.get("/api/prospects/{prospect_id}/opt-out")
async def get_opt_out(prospect_id: str):
    """Why this person is opted out: the rule, the words and their message."""
    rows = await query_db("SELECT status FROM prospects WHERE id = ?", (prospect_id,))
    if not rows:
        return _refuse("unknown prospect", 404)
    status = rows[0].get("status") or "new"
    state = _state()
    await state.init_db()
    history = await state.opt_out_history(prospect_id)
    last = next((h for h in reversed(history) if h["action"] == "opt_out"), None)
    out = {
        "opted_out": status == "opted_out", "status": status, "recorded": bool(last),
        "at": None, "rule": None, "matched": "", "why": "", "message": "",
        "conversation_id": "", "history": history,
    }
    if last:
        out.update({k: last.get(k) or out[k] for k in
                    ("rule", "matched", "why", "message", "conversation_id")})
        out["at"] = last["at"]
    elif status == "opted_out" or history:
        # Opted out before Sellbird kept reasons (and maybe undone since):
        # show the last thing they wrote.
        for c in await query_db(
                "SELECT id, thread_json, updated_at FROM conversations "
                "WHERE prospect_id = ? ORDER BY updated_at DESC", (prospect_id,)):
            try:
                thread = json.loads(c.get("thread_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            theirs = [m for m in thread if isinstance(m, dict) and m.get("sender") == "prospect"]
            if theirs:
                out.update(message=str(theirs[-1].get("content") or ""),
                           conversation_id=c["id"], at=theirs[-1].get("timestamp") or c.get("updated_at"))
                break
    return out


@app.post("/api/prospects/{prospect_id}/opt-out/undo")
async def undo_opt_out(prospect_id: str, request: Request):
    """The owner says this opt-out was a mistake. Needs {"confirm": true}."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return _refuse('expected {"confirm": true}', 400)
    rows = await query_db("SELECT email FROM prospects WHERE id = ?", (prospect_id,))
    if not rows:
        return _refuse("unknown prospect", 404)
    state = _state()
    await state.init_db()
    history = await state.opt_out_history(prospect_id)
    last = next((h for h in reversed(history) if h["action"] == "opt_out"), {})
    if not await state.undo_opt_out(prospect_id, details={
            "prospect_email": rows[0].get("email") or "",
            "previous_why": last.get("why", ""), "previous_at": last.get("at", "")}):
        return _refuse("This person isn't opted out.")
    return {"success": True, "status": "replied"}


@app.get("/api/offers/accepted")
async def get_offers_accepted():
    """People who said yes to your offer, newest first, with what the handoff
    (sellbird.handoff) answered, so you or your coding agent can deliver it."""
    rows = await query_db("""
        SELECT c.id AS conversation_id, c.status, c.updated_at, c.prospect_id,
               p.first_name, p.last_name, p.email, p.title, p.company, p.company_id
        FROM conversations c LEFT JOIN prospects p ON c.prospect_id = p.id
        WHERE c.intent = 'accepted_offer'
        ORDER BY c.updated_at DESC LIMIT 100
    """)
    for row in rows:
        done = await query_db("SELECT value FROM settings WHERE key = ?",
                              ("handoff:" + str(row.get("prospect_id") or ""),))
        try:
            row["handoff"] = json.loads(done[0]["value"]) if done else None
        except (TypeError, ValueError, KeyError):
            row["handoff"] = None
    return {"accepted": rows}


@app.get("/api/conversations")
async def get_conversations(awaiting: bool = False):
    rows = await query_db("""
        SELECT c.*, p.first_name, p.last_name, p.email as prospect_email, p.company,
               p.status as prospect_status
        FROM conversations c
        LEFT JOIN prospects p ON c.prospect_id = p.id
        ORDER BY c.updated_at DESC LIMIT 100
    """)
    # The stored thread holds replies and Sellbird's answers; the cold emails
    # a reply answers live in the Outbox. Attach those (sent ones only) and
    # any answer still waiting for approval, so the page shows the exchange.
    pids = sorted({r["prospect_id"] for r in rows if r.get("prospect_id")})
    emails: dict[str, list] = {}
    if pids:
        for e in await query_db(
                "SELECT prospect_id, campaign_id, step, subject, body, sent_at FROM outbox "
                f"WHERE kind = 'sequence' AND status = 'sent' AND prospect_id IN ({_in(pids)}) "
                "ORDER BY sent_at", tuple(pids)):
            emails.setdefault(e.pop("prospect_id"), []).append(e)
    cids = [r["id"] for r in rows]
    drafts = {d.pop("conversation_id"): d for d in (await query_db(
        "SELECT id, conversation_id, subject, body, send_at FROM outbox "
        f"WHERE kind = 'reply' AND status = 'pending_review' AND conversation_id IN ({_in(cids)}) "
        "ORDER BY created_at", tuple(cids)) if cids else [])}
    for row in rows:
        try:
            row["thread"] = json.loads(row.get("thread_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["thread"] = []
        # A prospect in two campaigns: show the sequence this reply answers.
        camp = row.get("campaign_id") or ""
        row["emails"] = [{k: v for k, v in e.items() if k != "campaign_id"}
                         for e in emails.get(row.get("prospect_id"), [])
                         if not camp or e.get("campaign_id") == camp]
        row["draft"] = drafts.get(row["id"])
    if awaiting:
        rows += await _awaiting_reply()
    return rows


async def _awaiting_reply() -> list[dict]:
    """Prospects Sellbird emailed who haven't replied yet, shaped like a
    conversation so the tab can show every sent email, not only the ones
    that got an answer. Read-only: no conversation row is created, and the
    card moves to the real conversation the moment a reply arrives."""
    waiting: dict[str, dict] = {}
    # Only people still waiting: anyone whose company has a conversation (even
    # one past the tab's latest 100), a bounced address, or who's otherwise done
    # isn't. Bounded because this runs on every load of the tab.
    sent = await query_db(
        "SELECT o.prospect_id, o.step, o.subject, o.body, o.sent_at, "
        "p.first_name, p.last_name, p.email AS prospect_email, p.company, "
        "p.status AS prospect_status FROM outbox o "
        "JOIN prospects p ON p.id = o.prospect_id "
        "WHERE o.kind = 'sequence' AND o.status = 'sent' "
        "AND p.status NOT IN ('replied', 'opted_out', 'lost', 'meeting', 'closed') "
        "AND COALESCE(p.email_status, '') != 'invalid' "
        "AND NOT EXISTS (SELECT 1 FROM conversations c JOIN prospects q ON q.id = c.prospect_id "
        "WHERE q.id = p.id OR (q.company_id = p.company_id AND COALESCE(p.company_id, '') != '')) "
        "ORDER BY o.sent_at DESC LIMIT 2000")
    for e in reversed(sent):
        pid = e["prospect_id"]
        row = waiting.setdefault(pid, {
            "id": "awaiting-" + pid, "prospect_id": pid, "campaign_id": "",
            "channel": "email", "intent": "", "stage": "", "status": "awaiting_reply",
            "thread": [], "draft": None, "emails": [],
            "first_name": e.get("first_name") or "", "last_name": e.get("last_name") or "",
            "prospect_email": e.get("prospect_email") or "", "company": e.get("company") or "",
            "prospect_status": e.get("prospect_status") or "",
        })
        row["emails"].append({"step": e["step"], "subject": e["subject"],
                              "body": e["body"], "sent_at": e["sent_at"]})
        row["created_at"] = row.get("created_at") or e["sent_at"]
        row["updated_at"] = e["sent_at"]
    return sorted(waiting.values(), key=lambda r: r.get("updated_at") or "", reverse=True)


@app.post("/api/conversations/{convo_id}/status")
async def set_conversation_status(convo_id: str, request: Request):
    """Close a conversation, or hand one Sellbird escalated back to it.

    Sellbird answers new replies only on open conversations, so "open" means
    "Sellbird, carry on" and "closed" means it's done."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    status = data.get("status") if isinstance(data, dict) else None
    if status not in ("open", "closed"):
        return JSONResponse({"success": False, "message": "status must be open or closed"},
                            status_code=400)
    rows = await query_db("SELECT status FROM conversations WHERE id = ?", (convo_id,))
    if not rows:
        return JSONResponse({"success": False, "message": "No such conversation."},
                            status_code=404)
    state = _state()
    await state.update_conversation(convo_id, status=status)
    await state.log_action("conversation_status", "dashboard",
                           {"conversation_id": convo_id, "from": rows[0]["status"], "to": status})
    return {"success": True, "status": status}


@app.get("/api/trend")
async def get_trend(days: int = 14, tz: int = 0):
    """Daily counts for the last `days` days in the viewer's time zone
    (`tz`: minutes east of UTC; stored times are UTC): contacts found,
    emails sent, first replies. Days with nothing are 0."""
    from datetime import datetime, timedelta, timezone
    days = max(1, min(90, days))
    tz = max(-840, min(840, tz))
    shift = f"{tz:+d} minutes"
    today = (datetime.now(timezone.utc) + timedelta(minutes=tz)).date()
    labels = [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]

    async def per_day(table: str, column: str, where: str = "") -> list[int]:
        rows = await query_db(
            f"SELECT date({column}, ?) AS d, COUNT(*) AS n FROM {table} "
            f"WHERE date({column}, ?) >= ? {where} GROUP BY d", (shift, shift, labels[0]))
        counts = {r["d"]: r["n"] for r in rows}
        return [counts.get(day, 0) for day in labels]

    return {
        "days": labels,
        "found": await per_day("prospects", "created_at"),
        "sent": await per_day("outbox", "sent_at", "AND status = 'sent'"),
        "replies": await per_day("conversations", "created_at"),
    }


@app.get("/api/activity")
async def get_activity():
    rows = await query_db("SELECT * FROM actions ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        try:
            row["details"] = json.loads(row.get("details_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            row["details"] = {}
    return rows


# ── Dashboard UI ──


# ── Discovery: the provider menu, an estimate, and a run ──
#
# This is the only stage that spends money, so the UI never starts one without
# showing what it will cost first.

_discovery_task: asyncio.Task | None = None
_discovery_report: dict | None = None
_discovery_kind = "discover"   # or "profile": reading sites has no stop switch


def _discovery_queries(body: dict, config):
    from sellbird.collectors.discover import build_queries, preset_problem

    preset = str(body.get("preset") or "")
    problem = preset_problem(preset, str(body.get("provider") or ""), config=config)
    if problem:
        raise ValueError(problem)
    cities = [c.strip() for c in (body.get("cities") or []) if c.strip()] or None
    return build_queries(
        config, cities=cities,
        depth=int(body.get("depth") or 30),
        limit=int(body.get("limit") or 100),
        preset=preset,
    )


@app.get("/api/discover/providers")
async def get_discovery_providers():
    """What each source does, what it costs, and whether it's ready to use."""
    try:
        from sellbird.collectors.discover import DEFAULT_PROVIDER, provider_menu
        from sellbird.config import load_env

        state = _state()
        await state.init_db()
        return {
            "providers": provider_menu(load_env().model_dump()),
            "default": DEFAULT_PROVIDER,
            "selected": await state.get_setting("discovery_provider") or DEFAULT_PROVIDER,
            "paused": await state.get_setting("discovery_paused"),
            "running": bool(_discovery_task and not _discovery_task.done()),
            "running_kind": _discovery_kind,
            "unprofiled": await state.count_companies_needing_profile(),
            "last_report": _discovery_report,
        }
    except Exception as e:
        logger.exception("discovery providers failed")
        return {"providers": [], "error": str(e)}


@app.post("/api/discover/estimate")
async def estimate_discovery(request: Request):
    """Projected spend and the exact query list, before anything is called."""
    try:
        from sellbird.collectors.discover import PROVIDERS, estimate_cost
        from sellbird.config import load_config

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"error": f"unknown provider {provider!r}"},
                                status_code=400)

        try:
            queries = _discovery_queries(body, load_config())
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {
            "provider": provider,
            "queries": [q.keyword() for q in queries],
            "query_count": len(queries),
            "estimated_cost": round(estimate_cost(provider, queries), 4),
            "free": PROVIDERS[provider].estimate(queries) == 0,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/discover/run")
async def start_discovery(request: Request):
    """Kick off a run in the background and hand back immediately.

    Discovery takes minutes, not milliseconds — holding the request open
    would just time out. Progress shows up in the run log.
    """
    global _discovery_task, _discovery_report

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from sellbird.collectors.discover import PROVIDERS
        from sellbird.config import load_config
        from sellbird.pipeline import run_prospecting

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"success": False,
                                 "message": f"unknown provider {provider!r}"},
                                status_code=400)

        config = load_config()
        try:
            queries = _discovery_queries(body, config)
        except ValueError as e:
            return JSONResponse({"success": False, "message": str(e)}, status_code=400)
        max_spend = float(body.get("max_spend") or 1.0)

        state = _state()
        await state.init_db()
        await state.set_setting("discovery_provider", provider)
        await state.set_setting("discovery_paused", "")

        global _discovery_kind
        _discovery_kind = "discover"

        async def _go():
            global _discovery_report
            try:
                # Discovery chains straight into profiling: reading the sites
                # is free, and it is what makes the results worth anything.
                result = await run_prospecting(state, config, provider, queries,
                                               max_spend=max_spend)
                _discovery_report = {
                    **(result.discover or {}),
                    "profiled_companies": result.profiled_companies,
                    "profile_observations": result.profile_observations,
                    "errors": result.errors,
                }
            except Exception as exc:
                logger.exception("discovery run failed")
                _discovery_report = {"errors": [str(exc)], "stopped": "failed"}

        _discovery_report = None
        _discovery_task = asyncio.create_task(_go())
        return {"success": True, "queries": len(queries)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/profile/run")
async def start_profile():
    """Read the websites of everything discovered but not yet looked at.

    Free and model-free, so there is nothing to estimate and no cap to set.
    """
    global _discovery_task, _discovery_report

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from sellbird.pipeline import run_profile_stage

        state = _state()
        await state.init_db()
        pending = await state.count_companies_needing_profile()
        if not pending:
            return {"success": True, "pending": 0}

        async def _go():
            global _discovery_report
            try:
                try:
                    from sellbird.config import load_config
                    config = load_config()
                except Exception:
                    config = None   # still profiles; no role is singled out
                companies, observations, _ = await run_profile_stage(
                    state, limit=200, config=config)
                _discovery_report = {
                    "profiled_companies": companies,
                    "profile_observations": observations,
                }
            except Exception as exc:
                logger.exception("profile run failed")
                _discovery_report = {"errors": [str(exc)], "stopped": "failed"}

        global _discovery_kind
        _discovery_kind = "profile"
        _discovery_report = None
        _discovery_task = asyncio.create_task(_go())
        return {"success": True, "pending": pending}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/discover/stop")
async def stop_discovery():
    """Kill switch. Read between batches, so an in-flight run stops cleanly."""
    try:
        state = _state()
        await state.init_db()
        await state.set_setting("discovery_paused", "stopped from dashboard")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


def _parse_ts(value) -> datetime | None:
    """SQLite CURRENT_TIMESTAMP / isoformat strings -> naive UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "").split("+")[0])
    except ValueError:
        return None


async def _scalar(sql: str, params: tuple = ()) -> int:
    rows = await query_db(sql, params)
    return int(next(iter(rows[0].values())) or 0) if rows else 0


async def _first(sql: str, params: tuple = (), key: str = "t"):
    """First row's `key`, or None — query_db returns [] on any DB problem."""
    rows = await query_db(sql, params)
    return rows[0].get(key) if rows else None


# A run row still says 'running' after its process was killed (closed window,
# reboot). No new facts for this long means it is not coming back.
STALE_RUN_SECONDS = 900
# Contact-finding has no run row; a recent prospects_checked_at means it is active.
CONTACT_ACTIVE_SECONDS = 180
# Moved on from "waiting to be emailed" (opted_out too: never offered again).
SENT_STATUSES = ("contacted", "replied", "meeting", "closed", "lost", "opted_out")
REPLY_STATUSES = ("replied", "meeting", "closed")


def _eta_text(minutes) -> str:
    if minutes is None:
        return ""
    return "less than a minute left" if minutes < 1 else f"about {round(minutes)} min left"


@app.get("/api/live-status")
async def get_live_status():
    """Plain-language progress: each pipeline step, what is running, how long is left.

    Built for someone who would otherwise keep asking "what's the status?".
    Everything is derived from the database, so it is true whatever started
    the work — `sellbird run`, a CLI command, or a one-off script. Every total
    counts only what the step can act on (a business with no website is never
    read), so a finished step always reaches 100% instead of sticking.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # DB stores naive UTC
    has_site = "COALESCE(domain, '') != ''"
    companies = await _scalar("SELECT COUNT(*) FROM companies")
    with_site = await _scalar(f"SELECT COUNT(*) FROM companies WHERE {has_site}")
    no_site = companies - with_site
    profiled = await _scalar(
        f"SELECT COUNT(*) FROM companies WHERE {has_site} AND profile_checked_at IS NOT NULL")
    checked = await _scalar(
        f"SELECT COUNT(*) FROM companies WHERE {has_site} "
        "AND profile_checked_at IS NOT NULL AND prospects_checked_at IS NOT NULL")

    prospects = await query_db("SELECT id, email, email_status, status FROM prospects")
    with_email = [p for p in prospects if p.get("email")]
    by_status: dict[str, int] = {}
    for p in with_email:
        k = (p.get("email_status") or "guess").lower()
        by_status[k] = by_status.get(k, 0) + 1
    deliverable = {p["id"] for p in with_email
                   if (p.get("email_status") or "") in ("verified", "risky")}
    unconfirmed = sum(n for k, n in by_status.items()
                      if k not in ("verified", "risky", "invalid"))

    # Drafts are counted per person, not per campaign: one campaign can cover many.
    drafted: set[str] = set()
    for row in await query_db("SELECT prospect_ids_json FROM campaigns"):
        try:
            ids = json.loads(row.get("prospect_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            ids = []
        if isinstance(ids, list):
            drafted.update(i for i in ids if isinstance(i, str))
    sent = {p["id"] for p in prospects if p.get("status") in SENT_STATUSES}
    # Who is left for the user to send by hand: only when Sellbird can't send
    # itself, and never someone Sellbird has queued, sent to, or can't reach.
    sellbird_sends = _sellbird_can_send()
    awaiting_review = await _scalar("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review'")
    by_hand: set[str] = set()
    if not sellbird_sends:
        skip = await _sellbird_committed_ids() | await _hand_sent_ids()
        by_hand = {p["id"] for p in prospects
                   if p["id"] in drafted and p["id"] not in skip
                   and (p.get("status") or "new") in HAND_SENDABLE_STATUSES
                   and (p.get("email_status") or "guess") in HAND_SENDABLE_EMAIL}
    replies = sum(1 for p in prospects if p.get("status") in REPLY_STATUSES)
    cost = float(await _first(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs", key="c") or 0)

    running, stopped = [], []
    for run in await query_db(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY started_at"):
        started = _parse_ts(run.get("started_at"))
        elapsed = max(0.0, (now - started).total_seconds()) if started else 0.0
        last = _parse_ts(await _first(
            "SELECT MAX(observed_at) AS t FROM observations WHERE run_id = ?",
            (run["id"],))) or started
        item = {"stage": run.get("stage"), "provider": run.get("provider"),
                "elapsed_min": round(elapsed / 60, 1), "eta_min": None}
        if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
            stopped.append(item)
            continue
        if run.get("stage") == "profile" and started:
            done_in_run = await _scalar(
                f"SELECT COUNT(*) FROM companies WHERE {has_site} AND profile_checked_at >= ?",
                (run["started_at"],))
            left = max(0, with_site - profiled)
            if left and done_in_run and elapsed > 0:
                item["eta_min"] = left / (done_in_run / elapsed) / 60
        running.append(item)

    last_contact = _parse_ts(await _first(
        "SELECT MAX(prospects_checked_at) AS t FROM companies"))
    finding_emails = bool(
        last_contact and (now - last_contact).total_seconds() < CONTACT_ACTIVE_SECONDS)
    reading = any(r["stage"] == "profile" for r in running)
    finding = any(r["stage"] == "discover" for r in running)

    def step(label, done, total, active, detail, info=False):
        if active:
            state_ = "running"
        elif info:
            state_ = "done" if done else "waiting"
        elif total and done >= total:
            state_ = "done"
        elif done:
            state_ = "partial"
        else:
            state_ = "waiting"
        return {"label": label, "done": done, "total": total,
                "state": state_, "detail": detail}

    profile_eta = next((r["eta_min"] for r in running if r["stage"] == "profile"), None)
    read_detail = f"{profiled} of {with_site} read"
    if reading and profile_eta is not None:
        read_detail += " · " + _eta_text(profile_eta)
    if no_site:
        read_detail += f" · {no_site} have no website (skipped)"

    steps = [
        step("Find businesses", companies, 0, finding,
             f"{companies} businesses found", info=True),
        step("Read their websites", profiled, with_site, reading, read_detail),
        step("Find emails", checked, profiled, finding_emails,
             f"{checked} of {profiled} checked · {len(with_email)} emails found"),
        step("Check emails are real", len(with_email) - unconfirmed, len(with_email), False,
             f"{by_status.get('verified', 0)} verified · {by_status.get('risky', 0)} risky · "
             f"{unconfirmed} not confirmed"
             + (f" · {by_status['invalid']} bad" if by_status.get("invalid") else "")),
        step("Write first emails", len(drafted), len(deliverable | drafted), False,
             f"{len(drafted)} written · see Campaigns"),
        step("Sent", len(sent & drafted), len(drafted), False,
             f"{len(sent & drafted)} of {len(drafted)} sent"),
        step("Replies", replies, 0, False,
             f"{replies} replies" if replies else "No replies yet", info=True),
    ]

    next_tab = ""   # where the next step is done, when it's done in the dashboard
    if reading or finding:
        next_up = "Wait — Sellbird is working. This page updates by itself."
    elif finding_emails:
        next_up = "Wait — emails are being found."
    elif stopped:
        next_up, next_tab = "A job stopped before finishing. Run it again.", "discover"
    elif sellbird_sends and awaiting_review:
        next_up, next_tab = f"Review the {awaiting_review} draft(s) waiting in the Outbox.", "outbox"
    elif by_hand:
        next_up, next_tab = f"Open Campaigns and send the {len(by_hand)} unsent email(s).", "campaigns"
    elif profiled > checked:
        next_up, next_tab = f"Find emails for the {profiled - checked} businesses not checked yet.", "controls"
    elif deliverable - drafted:
        next_up, next_tab = f"Write first emails for {len(deliverable - drafted)} more contact(s).", "controls"
    elif not companies:
        next_up, next_tab = "Search for businesses to get started.", "discover"
    else:
        next_up, next_tab = "Search more cities or industries to get more leads.", "discover"

    return {
        "steps": steps,
        "running": [dict(r, eta_text=_eta_text(r["eta_min"])) for r in running],
        "stopped": stopped,
        "next": next_up,
        "next_tab": next_tab,
        "cost_usd": round(cost, 4),
        "updated_at": now.isoformat() + "Z",
    }


def _require_approval_now() -> bool:
    try:
        from sellbird.config import load_config
        return bool(load_config(str(_resolve_config_file())).channels.email.require_approval)
    except Exception:
        return True


@app.get("/api/today")
async def get_today():
    """What needs a human, right now.

    The dashboard opens here rather than on a setup checklist: the question a
    returning user actually has is "is anything waiting on me?", and the
    answer is usually a short list or nothing at all.
    """
    items: list[dict] = []
    stats: dict = {}
    try:
        from sellbird.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = await state.get_signal_codes()
        proposed = [c for c in codes if c.get("status") == "proposed"]
        confirmed = [c for c in codes if c.get("status") == "confirmed"]
        pending = await state.get_outbox(status="pending_review", limit=200)
        approved = await state.get_outbox(status="approved", limit=200)
        paused = await state.get_setting("sending_paused")
        counts = await state.count_prospects_by_status()

        convos = await query_db(
            "SELECT COUNT(*) AS n FROM conversations WHERE status = 'open'"
        )
        open_convos = convos[0]["n"] if convos else 0
        handed = await query_db(
            "SELECT COUNT(*) AS n FROM conversations WHERE status = 'needs_human'"
        )
        needs_human = handed[0]["n"] if handed else 0
        companies = await query_db("SELECT COUNT(*) AS n FROM companies")
        n_companies = companies[0]["n"] if companies else 0

        unprofiled_count = await state.count_companies_needing_profile()
        stats = {
            "companies": n_companies,
            "prospects": sum(counts.values()),
            "signals_confirmed": len(confirmed),
            "outbox_pending": len(pending),
            "outbox_approved": len(approved),
            "open_conversations": open_convos,
            "needs_human": needs_human,
            "signals_proposed": len(proposed),
            "email_provider": _current_provider(),
            "unprofiled": unprofiled_count,
        }

        # Ordered by how much it blocks Sellbird from doing anything at all.
        brain = await _brain_status()
        if brain.get("status") == "paused":
            prov = brain.get("provider")
            who = ("Codex" if prov == "codex"
                   else "AI Router" if prov in ("openrouter", "openai_compat")
                   else "Sellbird's model")
            items.append({
                "key": "brain-paused", "tone": "bad",
                "title": f"{who} is paused, so Sellbird is waiting",
                "detail": (str(brain.get("reason") or "It can't be used right now.")[:300] +
                           " Replies and queued emails are untouched; Sellbird resumes "
                           "once it's fixed. sellbird status --check says what's wrong."),
                "action": "Open the log", "tab": "controls",
            })

        from sellbird.agents.writer import EMAIL_RULES_PROBLEM_KEY
        rules_problem = await state.get_setting(EMAIL_RULES_PROBLEM_KEY)
        if rules_problem:
            items.append({
                "key": "email-rules", "tone": "bad",
                "title": "Sellbird isn't writing emails: your email rules can't be used",
                "detail": (f"{rules_problem}. Fix the file, or clear writer.email_rules "
                           "to use Sellbird's built-in rules."),
                "action": "Open the log", "tab": "controls",
            })

        hold = await state.current_sends_held()
        if hold:
            from sellbird.sending_mode import held_message
            title, detail = held_message(hold, waiting=len(approved))
            items.append({
                "key": "sends-held", "tone": "bad", "title": title, "detail": detail,
                "action": "Open the log", "tab": "controls",
            })

        if paused:
            items.append({
                "key": "paused", "tone": "bad",
                "title": "Sending is paused",
                "detail": str(paused) + ". Nothing will go out until you resume it.",
                "action": "Review and resume", "tab": "outbox",
            })

        # Cold email waiting on the optional Precleared check: its free
        # checks ran out, the key was rejected, or it couldn't be reached.
        from sellbird.integrations.precleared import current_hold, hold_message
        try:
            from sellbird.config import load_config
            loaded = load_config(str(_resolve_config_file()))
        except Exception:
            loaded = None  # can't judge: show the hold rather than hide it
        precleared_hold = await current_hold(state, loaded)
        if precleared_hold:
            message = hold_message(precleared_hold)
            items.append({
                "key": "precleared-hold", "tone": message["tone"],
                "title": message["title"], "detail": message["detail"],
                "action": "Open the log", "tab": "controls",
                "url": message["url"], "url_label": message["url_label"],
            })

        setup = await get_setup_status()
        if isinstance(setup, dict) and setup.get("percent", 100) < 100:
            missing = [c["label"] for c in setup.get("checks", [])
                       if c.get("required") and not c.get("done")]
            items.append({
                "key": "setup", "tone": "warn",
                "title": "Finish setting Sellbird up",
                "detail": ", ".join(missing[:3]) or "Some required steps are incomplete.",
                "action": "Open setup", "tab": "settings",
            })

        if proposed:
            items.append({
                "key": "signals", "tone": "warn",
                "title": (f"{len(proposed)} signal waiting for your confirmation"
                          if len(proposed) == 1
                          else f"{len(proposed)} signals waiting for your confirmation"),
                "detail": ("Sellbird won't collect anything you haven't approved. "
                           "Confirm which signals define a good prospect for you."),
                "action": "Review signals", "tab": "signals",
            })
        elif not confirmed:
            items.append({
                "key": "signals-none", "tone": "warn",
                "title": "No signals confirmed",
                "detail": "Every signal is rejected, so prospecting has nothing to collect.",
                "action": "Review signals", "tab": "signals",
            })

        if not n_companies and confirmed:
            items.append({
                "key": "discover", "tone": "good",
                "title": "No companies yet",
                "detail": ("Signals are confirmed but nothing has been collected. "
                           "Discovery is free to try — no account needed."),
                "action": "Find businesses", "tab": "discover",
            })

        unprofiled = unprofiled_count
        if unprofiled:
            items.append({
                "key": "profile", "tone": "good",
                "title": f"{unprofiled} companies not looked at yet",
                "detail": ("Reading their websites is free and it is what makes "
                           "an email specific — who their agency is, what they "
                           "are missing, whether they are spending on ads."),
                "action": "Read their sites", "tab": "discover",
            })

        if needs_human:
            items.append({
                "key": "needs-human", "tone": "bad",
                "title": (f"{needs_human} conversation needs you" if needs_human == 1
                          else f"{needs_human} conversations need you"),
                "detail": ("Sellbird stopped and handed these over: an escalation, or a "
                           "reply it had no safe answer for. Reply yourself, then close "
                           "it or hand it back."),
                "action": "Read them", "tab": "conversations",
            })

        if pending:
            items.append({
                "key": "outbox", "tone": "warn",
                "title": (f"{len(pending)} email waiting for approval" if len(pending) == 1
                          else f"{len(pending)} emails waiting for approval"),
                "detail": ("Nothing sends until you approve it. Read them one at a time."
                           if _require_approval_now() else
                           "Autopilot sends the rest on its own; these wait for you. "
                           "Read them one at a time."),
                "action": "Open the decisions desk", "tab": "outbox",
            })

        if open_convos:
            items.append({
                "key": "replies", "tone": "good",
                "title": (f"{open_convos} live conversation" if open_convos == 1
                          else f"{open_convos} live conversations"),
                "detail": "People replied. Check how Sellbird is handling them.",
                "action": "Read conversations", "tab": "conversations",
            })

        return {"items": items, "stats": stats}
    except Exception as e:
        logger.exception("today load failed")
        return {"items": [], "stats": stats, "error": str(e)}


# ── Signals: Sellbird proposes, the user confirms ──
#
# Nothing is collected until a human has said yes to it. This is the gate the
# whole prospecting pipeline hangs off: collectors ask `state.confirmed_signal_codes()`
# and skip anything that isn't in the set.

CATEGORY_META = {
    "discovery": {
        "label": "Discovery — who exists",
        "blurb": "How Sellbird finds businesses at all, and how visible they are. "
                 "This is the only stage that costs money.",
    },
    "profile": {
        "label": "Profile — what they are",
        "blurb": "Read from the pages a business already publishes. Free, no AI "
                 "tokens, three HTTP requests per company. These are the signals "
                 "that make an email specific.",
    },
    "people": {
        "label": "People — who decides",
        "blurb": "Named humans and whether they're the one who can say yes.",
    },
    "verification": {
        "label": "Contactability — can you reach them",
        "blurb": "Whether the address will actually deliver, and what to do when "
                 "it won't.",
    },
}
CATEGORY_ORDER = ["discovery", "profile", "people", "verification"]


@app.get("/api/signals")
async def get_signals():
    """The signal vocabulary, grouped for review, with live cohort sizes."""
    try:
        from sellbird.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        # Seeding is idempotent and never overrides a decision the user made,
        # so it is safe to run on every load — new signals shipped in an
        # upgrade show up as `proposed` without any migration step.
        await seed_signal_catalog(state)

        codes = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}

        groups, summary = [], {"proposed": 0, "confirmed": 0, "rejected": 0}
        for cat in CATEGORY_ORDER:
            rows = []
            for sig in codes:
                if sig.get("category") != cat:
                    continue
                seen = counts.get(sig["code"], {})
                rows.append({
                    **sig,
                    "companies": seen.get("companies", 0),
                    "observations": seen.get("observations", 0),
                })
            if rows:
                meta = CATEGORY_META.get(cat, {})
                groups.append({
                    "key": cat,
                    "label": meta.get("label", cat.title()),
                    "blurb": meta.get("blurb", ""),
                    "signals": rows,
                })
        for sig in codes:
            summary[sig.get("status", "proposed")] = (
                summary.get(sig.get("status", "proposed"), 0) + 1
            )

        return {"summary": summary, "groups": groups, "total": len(codes)}
    except Exception as e:
        logger.exception("signals load failed")
        return {"error": str(e), "groups": [], "summary": {}}


@app.post("/api/signals/status")
async def set_signals_status(request: Request):
    """Confirm or reject one signal, or a whole category at once."""
    try:
        body = await request.json()
        status = (body.get("status") or "").strip()
        codes = body.get("codes") or ([body["code"]] if body.get("code") else [])
        if status not in ("proposed", "confirmed", "rejected"):
            return JSONResponse(
                {"success": False, "message": f"invalid status: {status!r}"},
                status_code=400,
            )
        if not codes:
            return JSONResponse(
                {"success": False, "message": "no signal codes given"}, status_code=400
            )

        from sellbird.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        # Seed first: a confirm that arrives before anything has loaded the
        # catalog would otherwise report success while changing nothing.
        await seed_signal_catalog(state)

        changed, unknown = 0, []
        for code in codes:
            if await state.set_signal_status(code, status):
                changed += 1
            else:
                unknown.append(code)
        return {
            "success": True, "changed": changed,
            "status": status, "unknown": unknown,
        }
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


COHORT_PAGE_SIZE = 50
COHORT_MAX_PAGE_SIZE = 200


async def _cohort_companies(require: list[str], exclude: list[str],
                            offset: int = 0, limit: int | None = None,
                            ranges: list[dict] | None = None):
    state = _state()
    await state.init_db()
    return await state.cohort_companies(require, exclude, offset, limit,
                                        ranges=ranges)


def _codes(raw) -> list[str]:
    """Accept lists or CSV strings, with bounded SQL parameter counts."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or any(not isinstance(c, str) for c in raw):
        raise ValueError("signals must be a list of strings or comma-separated string")
    codes = list(dict.fromkeys(c.strip() for c in raw if c.strip()))
    if len(codes) > 200 or any(len(c) > 128 for c in codes):
        raise ValueError("too many or oversized signal codes")
    return codes


COHORT_MAX_RANGES = 20


def _range_bound(raw):
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("range bounds must be numbers")
    value = float(raw)
    if value != value or abs(value) > 1e12:
        raise ValueError("range bound is out of bounds")
    return value


def _ranges(raw) -> list[dict]:
    """Numeric cohort filters: a list of {code, min, max}, or the CSV query
    form "SERP_RANK:11:30,MAPS_RANK::3". At least one bound per range."""
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, str):
        items = []
        for part in raw.split(","):
            if not part.strip():
                continue
            pieces = part.split(":")
            if len(pieces) != 3:
                raise ValueError("ranges look like CODE:min:max")
            items.append({"code": pieces[0], "min": pieces[1], "max": pieces[2]})
        raw = items
    if not isinstance(raw, list) or any(not isinstance(r, dict) for r in raw):
        raise ValueError("ranges must be a list of {code, min, max}")
    if len(raw) > COHORT_MAX_RANGES:
        raise ValueError("too many ranges")
    out = []
    for r in raw:
        code = r.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > 128:
            raise ValueError("each range needs a signal code")
        low, high = _range_bound(r.get("min")), _range_bound(r.get("max"))
        if low is None and high is None:
            raise ValueError(f"range on {code} needs a min or a max")
        if low is not None and high is not None and low > high:
            raise ValueError(f"range on {code}: min is above max")
        out.append({"code": code.strip(), "min": low, "max": high})
    return out


def _cohort_integer(raw, default):
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError("offset and limit must be integers")
    value = int(raw)
    if abs(value) > 9223372036854775807:
        raise ValueError("offset or limit is too large")
    return value


@app.post("/api/cohort")
async def preview_cohort(request: Request):
    """How many companies carry ALL these signals and none of those.

    The payoff for confirming signals: a cohort is a query, not a list. Set
    intersection happens in SQL — intersecting in JS over a capped fetch
    silently returns the wrong answer. ``offset``/``limit`` page through the
    matches; ``size`` is always the whole cohort. ``ranges`` adds numeric
    filters — ``[{"code": "SERP_RANK", "min": 11, "max": 30}]``.
    """
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        require = _codes(body.get("require"))
        exclude = _codes(body.get("exclude"))
        ranges = _ranges(body.get("ranges"))
        offset = max(0, _cohort_integer(body.get("offset"), 0))
        limit = min(max(1, _cohort_integer(body.get("limit"), COHORT_PAGE_SIZE)),
                    COHORT_MAX_PAGE_SIZE)
        if not require and not ranges:
            return {"size": 0, "offset": 0, "limit": limit, "companies": []}

        size, rows = await _cohort_companies(require, exclude, offset, limit,
                                             ranges=ranges)
        return {"size": size, "offset": offset, "limit": limit,
                "companies": rows}
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"size": 0, "companies": [], "error": str(e)}, status_code=500)


def _csv_cell(value):
    text = str(value or "")
    # Spreadsheet software may ignore leading whitespace before formulas.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


@app.get("/api/cohort.csv")
async def export_cohort(require: str = "", exclude: str = "", ranges: str = ""):
    """The whole cohort as a CSV download — every match, not just one page."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "domain", "website", "industry", "location"])
    try:
        codes = _codes(require)
        excluded = _codes(exclude)
        bounds = _ranges(ranges)
        if codes or bounds:
            _, rows = await _cohort_companies(codes, excluded, ranges=bounds)
            for r in rows:
                writer.writerow([_csv_cell(r.get(k)) for k in
                                 ("name", "domain", "website", "industry", "location")])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("Cohort export failed: %s", e)
        return JSONResponse({"error": "Cohort export failed"}, status_code=500)
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="cohort.csv"'},
    )


@app.get("/api/runs")
async def get_runs_api():
    """The collector run log — what ran, when, what it produced and cost."""
    try:
        state = _state()
        await state.init_db()
        await state.sweep_stale_runs()
        runs = await state.get_runs(limit=25)
        # Same rule as the live panel: a "running" run that has recorded
        # nothing for STALE_RUN_SECONDS has stopped, whatever its row says.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for run in runs:
            if run.get("status") != "running":
                continue
            last = _parse_ts(await _first(
                "SELECT MAX(observed_at) AS t FROM observations WHERE run_id = ?",
                (run["id"],))) or _parse_ts(run.get("started_at"))
            if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
                run["status"] = "stalled"
        return runs
    except Exception as e:
        return {"error": str(e)}


WEB_DIR = (Path(__file__).resolve().parent / "web")


# ── Inbound booking webhooks ──
# Cal.com (or any calendar that can POST) tells Sellbird a meeting was booked,
# so the sequence stops without waiting for the next reply poll. The route is
# public in the guard (Cal.com cannot carry the dashboard token) and
# authenticates with its own shared secret instead: an HMAC-SHA256 signature
# of the raw body, in the Cal.com "x-cal-signature-256" header (the same
# scheme WEBHOOK_SECRET uses for the CRM's outbound webhooks). Fail closed:
# no secret configured, no bookings accepted.

CALCOM_SIGNATURE_HEADERS = ("x-cal-signature-256", "x-cal-signature")


def _webhook_secret() -> str:
    return (os.getenv("CALCOM_WEBHOOK_SECRET", "") or "").strip()


def _hmac_sha256_hex(secret: str, raw: bytes) -> str:
    import hashlib
    import hmac as hmac_mod
    return hmac_mod.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


@app.post("/api/webhooks/calcom")
async def calcom_webhook(request: Request):
    raw = await request.body()
    secret = _webhook_secret()
    if not secret:
        return JSONResponse(
            {"error": "not_configured",
             "message": "Set CALCOM_WEBHOOK_SECRET in .env to accept booking webhooks."},
            status_code=503,
        )
    signature = ""
    for header in CALCOM_SIGNATURE_HEADERS:
        value = request.headers.get(header, "")
        if value:
            signature = value.strip().lower().removeprefix("sha256=")
            break
    import hmac as hmac_mod
    expected = _hmac_sha256_hex(secret, raw)
    if not signature or not hmac_mod.compare_digest(signature, expected):
        return JSONResponse({"error": "invalid_signature"}, status_code=401)

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    trigger = str(payload.get("trigger") or payload.get("type") or "").strip().lower()
    if trigger and trigger not in ("booking_created", "booking.confirmed", "booking_created_event"):
        # Bookings rescheduled or cancelled still count as a meeting; anything
        # else (invitee cancellations included) leaves the sequence alone.
        return {"received": True, "handled": False, "trigger": trigger}

    # Cal.com nests the attendee differently across payload versions; accept
    # the shapes seen in the wild rather than one exact schema.
    booking = payload.get("booking") if isinstance(payload.get("booking"), dict) else payload
    attendees = booking.get("attendees")
    if not isinstance(attendees, list):
        attendee = booking.get("attendee")
        attendees = [attendee] if isinstance(attendee, dict) else []
    email = ""
    for person in attendees:
        if isinstance(person, dict):
            email = str(person.get("email") or "").strip().lower()
            if email:
                break
    if not email:
        return JSONResponse(
            {"error": "no_attendee_email",
             "message": "The webhook payload carried no attendee email to match."},
            status_code=400,
        )

    state = _state()
    await state.init_db()
    prospect = await state.get_prospect_by_email(email)
    if prospect is None:
        # Not one of ours (a booking made by hand, or someone else's
        # calendar): acknowledge so Cal.com stops retrying, change nothing.
        return {"received": True, "matched": False}

    await state.update_prospect_status(prospect.id, "meeting")
    cancelled = await state.cancel_pending_outbox_for_prospect(
        prospect.id, reason="meeting_booked")
    convo_id = ""
    try:
        rows = await query_db(
            "SELECT id FROM conversations WHERE prospect_id = ? ORDER BY updated_at DESC LIMIT 1",
            (prospect.id,))
        if rows:
            convo_id = rows[0]["id"]
            await state.update_conversation(convo_id, status="meeting_booked")
    except Exception as exc:
        logger.warning("Cal.com webhook: conversation not updated: %s", exc)
    await state.log_action(
        action_type="meeting_booked", agent="calcom_webhook",
        details={"prospect_id": prospect.id, "prospect_email": email,
                 "conversation_id": convo_id, "cancelled_emails": cancelled,
                 "booking_title": str(booking.get("title") or "")[:200],
                 "booking_start": str(booking.get("start") or "")[:60]})
    return {"received": True, "matched": True, "cancelled_emails": cancelled}


TEXT_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
BINARY_TYPES = {".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png"}


@app.get("/static/{path:path}")
async def static_file(path: str):
    """Serve the dashboard's own assets from disk.

    Read per-request rather than cached at import: editing app.css and hitting
    reload is the whole point of having them as real files. Fonts are vendored
    rather than fetched from a CDN — this is a local tool and it should work
    with the network off.
    """
    target = (WEB_DIR / path).resolve()
    root = WEB_DIR.resolve()
    if not target.is_file() or not target.is_relative_to(root):
        return PlainTextResponse("not found", status_code=404)

    if target.suffix in BINARY_TYPES:
        return Response(
            target.read_bytes(),
            media_type=BINARY_TYPES[target.suffix],
            headers={"Cache-Control": "public, max-age=604800"},
        )
    return PlainTextResponse(
        target.read_text(encoding="utf-8"),
        media_type=TEXT_TYPES.get(target.suffix, "text/plain"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


class DashboardSetupError(Exception):
    """The dashboard can't start safely; the message says what to do."""


def _check_loopback_bind(host: str) -> str:
    """The host to bind, which must be a loopback address. The dashboard has
    no remote-access mode: from another machine, use an SSH tunnel."""
    name = (host or "").strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not auth.is_loopback(name):
        raise DashboardSetupError(
            f"Refusing to listen on {host!r}: the dashboard only listens on this machine "
            "(127.0.0.1, ::1 or localhost). To reach it from elsewhere, tunnel instead: "
            "ssh -L 5555:127.0.0.1:5555 <this-host>")
    return name


def dashboard_url(host: str = "127.0.0.1", port: int = 5555) -> str:
    """The unlock link. The token rides in the #fragment, which browsers
    never send to the server (so it stays out of logs and Referer); the
    page moves it to sessionStorage and strips it from the address bar."""
    name = _check_loopback_bind(host)
    try:
        if ipaddress.ip_address(name).version == 6:
            name = f"[{name}]"
    except ValueError:
        pass
    return f"http://{name}:{port}/#token={token_store.get()}"


# Which dashboard answers on this port: a random id per process, never the
# token. The MCP server checks it against data/dashboard.json before it sends
# the agent token anywhere (dashboard_auth lets it through unauthenticated).
INSTANCE_ID = uuid.uuid4().hex


@app.get("/api/instance")
async def get_instance():
    return {"instance": INSTANCE_ID}


# ── AI Brain Router, Clay AI Enrichment & Deep Research API ──

_agentic_router = APIRouter(tags=["AI Brain & Enrichment"])

BRAIN_ROLES = ("scout", "researcher", "writer", "critic", "handler", "trainer")

_PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "openai_compat": ("", "OPENAI_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "fireworks": ("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
}

_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,39}$")


class _EnrichmentColumnCreate(BaseModel):
    model_config = {"extra": "ignore"}

    name: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1, max_length=4000)
    use_web_search: bool = False
    output_format: str = Field(default="text", max_length=40)


def _normalize_chain_entry(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Each chain entry must be an object.")
    provider = str(item.get("provider") or "").strip()
    model = str(item.get("model") or "").strip()
    if not provider or not model:
        raise ValueError("Each chain entry requires both 'provider' and 'model'.")
    if len(provider) > 80 or len(model) > 200:
        raise ValueError("Chain provider or model name is too long.")

    base_url = str(item.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValueError(f"base_url for {provider!r} must start with http:// or https://.")

    api_key_env = str(item.get("api_key_env") or "").strip()
    if api_key_env and not _ENV_VAR_RE.match(api_key_env):
        raise ValueError(f"api_key_env {api_key_env!r} must be a valid environment variable name.")

    out: dict = {"provider": provider, "model": model}
    if base_url:
        out["base_url"] = base_url
    if api_key_env:
        out["api_key_env"] = api_key_env
    for extra_key in ("timeout_s", "max_tokens", "temperature", "headers",
                      "input_cost_per_1m", "output_cost_per_1m", "name"):
        if extra_key in item and item[extra_key] is not None:
            out[extra_key] = item[extra_key]
    return out


def _normalize_profile_entry(role: str, val) -> dict | list | str | None:
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
        return s if s else None
    if isinstance(val, list):
        cleaned = [_normalize_chain_entry(x) for x in val if isinstance(x, dict)
                   and (str(x.get("provider") or "").strip() or str(x.get("model") or "").strip())]
        return cleaned or None
    if isinstance(val, dict):
        if isinstance(val.get("chain"), list):
            chain_items = [
                _normalize_chain_entry(x) for x in val["chain"]
                if isinstance(x, dict) and (str(x.get("provider") or "").strip()
                                            or str(x.get("model") or "").strip())
            ]
            if not chain_items:
                return None
            out_chain: dict = {"chain": chain_items}
            for k in ("temperature", "max_tokens", "timeout_s"):
                if k in val and val[k] is not None:
                    out_chain[k] = val[k]
            return out_chain

        provider = str(val.get("provider") or "").strip()
        model = str(val.get("model") or "").strip()
        if not provider and not model:
            return None
        if not model:
            raise ValueError(f"Profile {role!r} specifies a provider without a model.")
        out: dict = {"model": model}
        if provider:
            out["provider"] = provider
        base_url = str(val.get("base_url") or "").strip()
        if base_url:
            if not base_url.startswith(("http://", "https://")):
                raise ValueError(f"Profile {role!r} base_url must start with http:// or https://.")
            out["base_url"] = base_url
        api_key_env = str(val.get("api_key_env") or "").strip()
        if api_key_env:
            if not _ENV_VAR_RE.match(api_key_env):
                raise ValueError(f"Profile {role!r} api_key_env is invalid.")
            out["api_key_env"] = api_key_env
        for k in ("temperature", "max_tokens", "timeout_s"):
            if k in val and val[k] is not None:
                out[k] = val[k]
        return out
    raise ValueError(f"Invalid profile configuration for role {role!r}.")


def _write_yaml_atomic(target: Path, doc: dict) -> None:
    if target.is_symlink():
        raise ValueError(f"Refusing to write symlinked config file: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.stem}-", suffix=".yaml", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _sync_router_in_sellbird_yaml(activate: bool | None, max_usd: float | None = None) -> None:
    """Optionally toggle brain.transport_factory and usage.max_daily_usd in sellbird config."""
    cfg_path = _offer_config_file()
    if not cfg_path.exists() or cfg_path.is_symlink():
        return
    try:
        parsed = yaml.safe_load(read_user_text(cfg_path))
    except Exception:
        return
    if not isinstance(parsed, dict):
        return
    changed = False
    if activate is True:
        brain_sec = parsed.setdefault("brain", {})
        if isinstance(brain_sec, dict):
            if not (brain_sec.get("transport_factory") or "").strip():
                brain_sec["transport_factory"] = "harvey_brain.router_factory:build"
                changed = True
    elif activate is False:
        brain_sec = parsed.get("brain")
        if isinstance(brain_sec, dict) and brain_sec.get("transport_factory"):
            brain_sec["transport_factory"] = ""
            changed = True
    if max_usd is not None and max_usd > 0:
        usage_sec = parsed.setdefault("usage", {})
        if isinstance(usage_sec, dict) and usage_sec.get("max_daily_usd") != round(max_usd, 2):
            usage_sec["max_daily_usd"] = round(max_usd, 2)
            changed = True
    if changed:
        _write_yaml_atomic(cfg_path, parsed)


def _validate_with_brain_settings(doc: dict) -> dict:
    """Validate mapping via harvey_brain.config.BrainSettings.from_mapping when installed."""
    try:
        from harvey_brain.config import BrainSettings
    except ImportError:
        return doc
    if not hasattr(BrainSettings, "from_mapping"):
        return doc

    try:
        BrainSettings.from_mapping(doc)
        return doc
    except Exception as first_exc:
        # If profiles were passed as {role: {provider, model}} and BrainSettings expects
        # {role: {"chain": [...]}} or {role: [{provider, model}]}, try adapting cleanly.
        profiles = doc.get("profiles")
        if isinstance(profiles, dict) and profiles:
            default_prov = (
                doc["chain"][0].get("provider", "openrouter")
                if isinstance(doc.get("chain"), list) and doc["chain"]
                else "openrouter"
            )
            for wrap_kind in ("chain_dict", "chain_list"):
                adapted_profiles = {}
                can_adapt = True
                for r_name, r_val in profiles.items():
                    if isinstance(r_val, dict) and "chain" not in r_val and r_val.get("model"):
                        entry = dict(r_val)
                        entry.setdefault("provider", default_prov)
                        adapted_profiles[r_name] = (
                            {"chain": [entry]} if wrap_kind == "chain_dict" else [entry]
                        )
                    else:
                        can_adapt = False
                        break
                if can_adapt and adapted_profiles:
                    candidate = {**doc, "profiles": adapted_profiles}
                    try:
                        BrainSettings.from_mapping(candidate)
                        return candidate
                    except Exception:
                        continue
        raise ValueError(str(first_exc)) from first_exc


def _merged_env_map() -> dict[str, str]:
    file_env = _read_env_file()
    merged = {k: str(v) for k, v in os.environ.items() if v is not None}
    for k, v in file_env.items():
        if v and not merged.get(k):
            merged[k] = str(v)
    return merged


@_agentic_router.get("/api/brain/config")
async def get_brain_config():
    """Read brain.yaml configuration (chain, per-role profiles, daily USD budget)."""
    path = _brain_config_file()
    raw = _read_brain_yaml_raw()
    env_map = _merged_env_map()

    budget_raw = raw.get("budget") if isinstance(raw.get("budget"), dict) else {}
    try:
        max_usd = float(budget_raw.get("max_usd_per_day", _active_max_daily_usd()))
    except (TypeError, ValueError):
        max_usd = _active_max_daily_usd()

    chain_raw = raw.get("chain") if isinstance(raw.get("chain"), list) else []
    chain: list[dict] = []
    for item in chain_raw:
        if isinstance(item, dict):
            prov = str(item.get("provider") or "").strip()
            mod = str(item.get("model") or "").strip()
            def_url, def_env = _PROVIDER_DEFAULTS.get(prov.lower(), ("", ""))
            chain.append({
                **item,
                "provider": prov,
                "model": mod,
                "base_url": str(item.get("base_url") or def_url),
                "api_key_env": str(item.get("api_key_env") or def_env),
            })

    if not chain:
        def_base = env_map.get("OPENAI_BASE_URL", "").strip()
        def_model = env_map.get("OPENAI_MODEL", "").strip()
        if def_base and def_model:
            chain = [{
                "provider": "openai_compat",
                "model": def_model,
                "base_url": def_base,
                "api_key_env": "OPENAI_API_KEY",
            }]
        else:
            chain = [{
                "provider": "openrouter",
                "model": "openai/gpt-4o-mini",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            }]

    profiles_raw = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    profiles: dict[str, dict] = {}
    for role, val in profiles_raw.items():
        if isinstance(val, dict):
            if isinstance(val.get("chain"), list) and val["chain"] and isinstance(val["chain"][0], dict):
                first = val["chain"][0]
                profiles[str(role)] = {
                    "provider": str(first.get("provider") or ""),
                    "model": str(first.get("model") or ""),
                    "base_url": str(first.get("base_url") or ""),
                    "api_key_env": str(first.get("api_key_env") or ""),
                }
            else:
                profiles[str(role)] = {
                    "provider": str(val.get("provider") or ""),
                    "model": str(val.get("model") or ""),
                    "base_url": str(val.get("base_url") or ""),
                    "api_key_env": str(val.get("api_key_env") or ""),
                }
        elif isinstance(val, list) and val and isinstance(val[0], dict):
            first = val[0]
            profiles[str(role)] = {
                "provider": str(first.get("provider") or ""),
                "model": str(first.get("model") or ""),
                "base_url": str(first.get("base_url") or ""),
                "api_key_env": str(first.get("api_key_env") or ""),
            }
        elif isinstance(val, str) and val.strip():
            profiles[str(role)] = {"provider": "", "model": val.strip(), "base_url": "", "api_key_env": ""}

    ai_env_keys = (
        "OPENROUTER_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
        "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY",
        "CEREBRAS_API_KEY", "XAI_API_KEY",
    )
    brain = _active_brain()
    return {
        "exists": path.exists(),
        "file": path.name,
        "budget": {"max_usd_per_day": round(max_usd, 2)},
        "chain": chain,
        "profiles": profiles,
        "roles": list(BRAIN_ROLES),
        "transport_active": _is_transport_mode(brain),
        "brain_provider": _active_provider(),
        "transport_factory": (getattr(brain, "transport_factory", "") or "") if brain else "",
        "openai_base_url": env_map.get("OPENAI_BASE_URL", ""),
        "openai_model": env_map.get("OPENAI_MODEL", ""),
        "env_keys_set": {k: bool(env_map.get(k, "").strip()) for k in ai_env_keys},
    }


@_agentic_router.post("/api/brain/config")
async def save_brain_config(request: Request):
    """Validate via BrainSettings.from_mapping and atomically write brain.yaml."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be a JSON object."},
                            status_code=400)

    budget_in = body.get("budget") if isinstance(body.get("budget"), dict) else {}
    raw_usd = budget_in.get("max_usd_per_day", body.get("max_usd_per_day", 5.0))
    try:
        if isinstance(raw_usd, bool):
            raise ValueError("max_usd_per_day must be a number")
        max_usd = float(raw_usd)
    except (TypeError, ValueError):
        return JSONResponse({"success": False, "message": "budget.max_usd_per_day must be a number."},
                            status_code=400)
    if max_usd != max_usd or max_usd < 0 or max_usd > 100000:
        return JSONResponse({"success": False, "message": "budget.max_usd_per_day must be between 0 and 100000."},
                            status_code=400)

    chain_in = body.get("chain")
    if not isinstance(chain_in, list) or not chain_in:
        return JSONResponse({"success": False, "message": "chain must be a non-empty list of providers."},
                            status_code=400)
    if len(chain_in) > 20:
        return JSONResponse({"success": False, "message": "chain cannot exceed 20 entries."},
                            status_code=400)

    try:
        chain = [_normalize_chain_entry(item) for item in chain_in]
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

    profiles_in = body.get("profiles") if isinstance(body.get("profiles"), dict) else {}
    profiles: dict = {}
    try:
        for role_key, role_val in profiles_in.items():
            role = str(role_key or "").strip().lower()
            if not _ROLE_NAME_RE.match(role):
                raise ValueError(f"Invalid role name: {role_key!r}")
            norm = _normalize_profile_entry(role, role_val)
            if norm is not None:
                profiles[role] = norm
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

    existing = _read_brain_yaml_raw()
    doc: dict = {}
    for k, v in existing.items():
        if k not in ("budget", "chain", "profiles"):
            doc[k] = v
    existing_budget = existing.get("budget") if isinstance(existing.get("budget"), dict) else {}
    doc["budget"] = {**existing_budget, "max_usd_per_day": round(max_usd, 4)}
    doc["chain"] = chain
    if profiles:
        doc["profiles"] = profiles

    try:
        validated_doc = _validate_with_brain_settings(doc)
    except ValueError as exc:
        return JSONResponse(
            {"success": False, "message": f"Invalid brain.yaml configuration: {exc}"},
            status_code=400,
        )

    target = _brain_config_file()
    try:
        _write_yaml_atomic(target, validated_doc)
        activate_flag = body.get("activate_router")
        if isinstance(activate_flag, bool):
            _sync_router_in_sellbird_yaml(activate_flag, max_usd=max_usd)
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)
    except OSError as exc:
        logger.warning("Failed writing brain.yaml: %s", exc)
        return JSONResponse({"success": False, "message": f"Could not write {target.name}: {exc}"},
                            status_code=500)

    return {
        "success": True,
        "file": target.name,
        "budget": validated_doc["budget"],
        "chain": validated_doc["chain"],
        "profiles": validated_doc.get("profiles", {}),
        "transport_active": _is_transport_mode(_active_brain()),
    }


def _coerce_doctor_payload(raw_res, use_network: bool) -> dict:
    if hasattr(raw_res, "model_dump"):
        raw_res = raw_res.model_dump()
    elif dataclasses.is_dataclass(raw_res) and not isinstance(raw_res, type):
        raw_res = dataclasses.asdict(raw_res)

    if isinstance(raw_res, dict):
        providers = raw_res.get("providers") or raw_res.get("checks") or raw_res.get("chain") or []
        if isinstance(providers, list):
            providers = [
                p.model_dump() if hasattr(p, "model_dump")
                else (dataclasses.asdict(p) if dataclasses.is_dataclass(p) and not isinstance(p, type) else p)
                for p in providers
            ]
        ok = raw_res.get("ok")
        if ok is None:
            ok = bool(raw_res.get("ready", bool(providers) and any(
                isinstance(p, dict) and (p.get("ok") or p.get("ready") or p.get("key_set"))
                for p in providers
            )))
        return {
            **raw_res,
            "ok": bool(ok),
            "network": use_network,
            "providers": providers if isinstance(providers, list) else [],
        }
    if isinstance(raw_res, list):
        items = [
            p.model_dump() if hasattr(p, "model_dump")
            else (dataclasses.asdict(p) if dataclasses.is_dataclass(p) and not isinstance(p, type) else p)
            for p in raw_res
        ]
        any_ok = any(isinstance(p, dict) and (p.get("ok") or p.get("ready")) for p in items)
        return {"ok": any_ok, "network": use_network, "providers": items, "checks": items}
    return {"ok": bool(raw_res), "network": use_network, "providers": [], "summary": str(raw_res)}


async def _fallback_brain_doctor(use_network: bool) -> dict:
    cfg = await get_brain_config()
    env_map = _merged_env_map()
    providers_out: list[dict] = []
    import httpx

    for entry in cfg.get("chain", []):
        prov = str(entry.get("provider") or "openrouter")
        model = str(entry.get("model") or "")
        def_url, def_env = _PROVIDER_DEFAULTS.get(prov.lower(), ("", ""))
        base_url = str(entry.get("base_url") or def_url).rstrip("/")
        key_env = str(entry.get("api_key_env") or def_env)
        api_key = env_map.get(key_env, "").strip() if key_env else ""
        key_present = bool(api_key) or (prov.lower() in ("ollama", "local", "openai_compat")
                                        and base_url.startswith("http://127.0.0.1")
                                        or base_url.startswith("http://localhost"))
        row: dict = {
            "provider": prov,
            "model": model,
            "base_url": base_url,
            "api_key_env": key_env,
            "key_set": bool(api_key),
            "ok": key_present and bool(model),
            "detail": "API key present" if api_key else (
                f"Missing {key_env} in .env" if key_env else "No API key env configured"
            ),
        }
        if use_network and row["ok"] and base_url:
            t0 = time.perf_counter()
            try:
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                async with httpx.AsyncClient(timeout=6.0, trust_env=False) as client:
                    resp = await client.get(f"{base_url}/models", headers=headers)
                row["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                row["http_status"] = resp.status_code
                if resp.status_code in (200, 201):
                    row["ok"] = True
                    row["detail"] = f"Connected ({row['latency_ms']} ms)"
                elif resp.status_code in (401, 403):
                    row["ok"] = False
                    row["detail"] = f"Authentication rejected (HTTP {resp.status_code})"
                else:
                    row["detail"] = f"Endpoint answered HTTP {resp.status_code} ({row['latency_ms']} ms)"
            except Exception as exc:
                row["ok"] = False
                row["detail"] = f"Connection failed: {exc}"
        providers_out.append(row)

    any_ok = any(p.get("ok") for p in providers_out)
    return {
        "ok": any_ok,
        "network": use_network,
        "providers": providers_out,
        "checks": providers_out,
        "summary": (
            f"{sum(1 for p in providers_out if p.get('ok'))}/{len(providers_out)} provider(s) ready"
            if providers_out else "No providers configured in brain.yaml"
        ),
    }


@_agentic_router.get("/api/brain/doctor")
async def brain_doctor(request: Request, network: str = "0"):
    """Inspect brain.yaml provider chain and optionally run network checks (?network=1)."""
    use_network = str(network or "").strip().lower() in ("1", "true", "yes", "on")
    cfg_path = _brain_config_file()
    env_map = _merged_env_map()

    try:
        from harvey_brain import router_factory as rf
    except ImportError:
        rf = None

    if rf is not None:
        fn_async = getattr(rf, "doctor_async", None)
        fn_sync = getattr(rf, "doctor", None)
        target_fn = fn_async or fn_sync
        if callable(target_fn):
            try:
                sig = inspect.signature(target_fn)
                kwargs: dict = {}
                for p_name in sig.parameters:
                    if p_name in ("network", "check_network", "live"):
                        kwargs[p_name] = use_network
                    elif p_name in ("config_path", "path", "brain_yaml", "settings_path"):
                        kwargs[p_name] = cfg_path
                    elif p_name in ("home", "root", "project_root"):
                        kwargs[p_name] = _home_root()
                    elif p_name in ("env", "environ"):
                        kwargs[p_name] = env_map
                if fn_async is not None:
                    res = await fn_async(**kwargs)
                else:
                    res = await asyncio.to_thread(fn_sync, **kwargs)
                return _coerce_doctor_payload(res, use_network)
            except Exception as exc:
                logger.warning("router_factory doctor failed, falling back: %s", exc)

    return await _fallback_brain_doctor(use_network)


@_agentic_router.post("/api/brain/probe")
async def brain_probe(request: Request):
    """Dry-run validation or live 1-token LLM probe against the configured router/provider."""
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    if "live" in body:
        live = bool(body.get("live"))
    elif "dry_run" in body:
        live = not bool(body.get("dry_run"))
    else:
        live = False

    role = str(body.get("role") or "writer").strip().lower()
    prompt_text = str(body.get("prompt") or "Reply with the single word OK.").strip()[:500] or "OK"

    if not live:
        doc = await brain_doctor(request, network="0")
        providers = doc.get("providers") or []
        first_ready = next((p for p in providers if isinstance(p, dict) and p.get("ok")), None)
        return {
            "ok": bool(doc.get("ok")),
            "mode": "dry_run",
            "role": role,
            "provider": (first_ready or {}).get("provider", ""),
            "model": (first_ready or {}).get("model", ""),
            "summary": doc.get("summary") or ("Ready for live calls" if doc.get("ok") else "No usable provider key set"),
            "providers": providers,
        }

    # Live 1-token probe: enforce daily USD budget before hitting any external endpoint
    spend = await _spend_today_usd()
    max_usd = _active_max_daily_usd()
    if max_usd > 0 and spend >= max_usd:
        return JSONResponse(
            {"ok": False, "mode": "live", "error": f"Daily AI budget reached (${spend:.4f} / ${max_usd:.2f})."},
            status_code=429,
        )

    env_map = _merged_env_map()
    override_model = str(body.get("model") or "").strip()
    override_base = str(body.get("base_url") or "").strip()
    override_prov = str(body.get("provider") or "").strip()
    override_env = str(body.get("api_key_env") or "").strip()

    # Try harvey_brain.router_factory transport when no ad-hoc endpoint override was requested
    if not (override_model and override_base):
        try:
            from harvey_brain import router_factory as rf
        except ImportError:
            rf = None
        if rf is not None:
            probe_fn = getattr(rf, "probe_async", None)
            if callable(probe_fn):
                t0 = time.perf_counter()
                try:
                    sig = inspect.signature(probe_fn)
                    p_kwargs: dict = {}
                    for p_name in sig.parameters:
                        if p_name in ("role", "agent_role"):
                            p_kwargs[p_name] = role
                        elif p_name == "prompt":
                            p_kwargs[p_name] = prompt_text
                        elif p_name in ("live", "network"):
                            p_kwargs[p_name] = True
                        elif p_name in ("config_path", "path"):
                            p_kwargs[p_name] = _brain_config_file()
                        elif p_name in ("env", "environ"):
                            p_kwargs[p_name] = env_map
                    res = await probe_fn(**p_kwargs)
                    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
                    if isinstance(res, dict):
                        return {"mode": "live", "latency_ms": latency_ms, **res, "ok": bool(res.get("ok", True))}
                    return {"ok": True, "mode": "live", "latency_ms": latency_ms, "reply": str(res)[:200]}
                except Exception as exc:
                    return JSONResponse(
                        {"ok": False, "mode": "live", "error": f"Probe failed: {exc}"},
                        status_code=502,
                    )

            build_fn = getattr(rf, "build", None)
            if callable(build_fn):
                transport = None
                t0 = time.perf_counter()
                try:
                    state = _state()
                    await state.init_db()
                    b_sig = inspect.signature(build_fn)
                    b_kwargs: dict = {}
                    for p_name in b_sig.parameters:
                        if p_name == "state":
                            b_kwargs[p_name] = state
                        elif p_name in ("config_path", "path"):
                            b_kwargs[p_name] = _brain_config_file()
                        elif p_name in ("home", "root"):
                            b_kwargs[p_name] = _home_root()
                        elif p_name in ("env", "environ"):
                            b_kwargs[p_name] = env_map
                    transport = build_fn(**b_kwargs)
                    if inspect.isawaitable(transport):
                        transport = await transport
                    if transport is not None:
                        call_fn = (getattr(transport, "complete", None)
                                   or getattr(transport, "ask", None)
                                   or (transport if callable(transport) else None))
                        if callable(call_fn):
                            c_sig = inspect.signature(call_fn)
                            c_kwargs: dict = {}
                            for p_name in c_sig.parameters:
                                if p_name == "prompt":
                                    c_kwargs[p_name] = prompt_text
                                elif p_name in ("system", "system_prompt"):
                                    c_kwargs[p_name] = "Reply with OK."
                                elif p_name in ("role", "agent", "task"):
                                    c_kwargs[p_name] = role
                                elif p_name == "max_tokens":
                                    c_kwargs[p_name] = 4
                            reply = call_fn(**c_kwargs) if c_kwargs else call_fn(prompt_text)
                            if inspect.isawaitable(reply):
                                reply = await reply
                            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
                            text_out = (
                                reply.get("text") or reply.get("content") or str(reply)
                                if isinstance(reply, dict) else getattr(reply, "text", str(reply))
                            )
                            return {
                                "ok": True,
                                "mode": "live",
                                "role": role,
                                "latency_ms": latency_ms,
                                "reply": str(text_out).strip()[:200],
                            }
                except Exception as exc:
                    logger.warning("Transport live probe failed, trying direct OpenAI-compat probe: %s", exc)
                finally:
                    if transport is not None and hasattr(transport, "aclose"):
                        try:
                            res_close = transport.aclose()
                            if inspect.isawaitable(res_close):
                                await res_close
                        except Exception:
                            pass

    # Direct OpenAI-compatible 1-token probe against first ready chain entry or explicit override
    cfg = await get_brain_config()
    chain = cfg.get("chain") or []
    target_entry = {
        "provider": override_prov or (chain[0].get("provider") if chain else "openrouter"),
        "model": override_model or (chain[0].get("model") if chain else "openai/gpt-4o-mini"),
        "base_url": override_base or (chain[0].get("base_url") if chain else ""),
        "api_key_env": override_env or (chain[0].get("api_key_env") if chain else ""),
    }
    prov = str(target_entry["provider"] or "openrouter")
    def_url, def_env = _PROVIDER_DEFAULTS.get(prov.lower(), ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"))
    base_url = str(target_entry["base_url"] or def_url).rstrip("/")
    key_env = str(target_entry["api_key_env"] or def_env)
    api_key = env_map.get(key_env, "").strip() if key_env else ""
    model = str(target_entry["model"] or "").strip()

    if not base_url or not model:
        return JSONResponse(
            {"ok": False, "mode": "live", "error": "Configure a provider base_url and model first."},
            status_code=400,
        )
    if not api_key and not base_url.startswith(("http://127.0.0.1", "http://localhost")):
        return JSONResponse(
            {"ok": False, "mode": "live", "error": f"Missing API key ({key_env}) in .env."},
            status_code=400,
        )

    import httpx
    t0 = time.perf_counter()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt_text}],
                    "max_tokens": 4,
                    "temperature": 0,
                },
            )
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        if resp.status_code != 200:
            detail = resp.text[:240]
            return JSONResponse(
                {
                    "ok": False,
                    "mode": "live",
                    "provider": prov,
                    "model": model,
                    "http_status": resp.status_code,
                    "latency_ms": latency_ms,
                    "error": f"HTTP {resp.status_code}: {detail}",
                },
                status_code=502,
            )
        payload = resp.json()
        choices = payload.get("choices") or []
        reply_txt = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            reply_txt = str(msg.get("content") or "").strip()
        return {
            "ok": True,
            "mode": "live",
            "provider": prov,
            "model": model,
            "latency_ms": latency_ms,
            "reply": reply_txt[:200] or "OK",
        }
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "mode": "live", "provider": prov, "model": model, "error": str(exc)},
            status_code=502,
        )


async def _ensure_enrichment_tables(db) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS enrichment_columns (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            use_web_search INTEGER NOT NULL DEFAULT 0,
            output_format TEXT NOT NULL DEFAULT 'text',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS enrichment_values (
            company_id TEXT NOT NULL,
            column_id TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'done',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (company_id, column_id)
        )"""
    )


async def _call_state_method(state, names: tuple[str, ...], *args, **kwargs):
    for name in names:
        fn = getattr(state, name, None)
        if callable(fn):
            res = fn(*args, **kwargs)
            return (await res) if inspect.isawaitable(res) else res
    return None


@_agentic_router.get("/api/enrichment/columns")
async def get_enrichment_columns():
    """List Clay-mode AI enrichment columns and enriched cell values per company."""
    state = _state()
    await state.init_db()

    cols = await _call_state_method(
        state, ("list_enrichment_columns", "get_enrichment_columns")
    )
    raw_vals = await _call_state_method(
        state, ("get_enrichment_values", "list_enrichment_values", "list_enrichment_cells")
    )

    db_path = _db_path()
    if cols is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with storage.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_enrichment_tables(db)
            await db.commit()
            cur = await db.execute(
                "SELECT id, name, prompt, use_web_search, output_format, created_at "
                "FROM enrichment_columns ORDER BY created_at ASC"
            )
            cols = [dict(r) for r in await cur.fetchall()]
            if raw_vals is None:
                vcur = await db.execute(
                    "SELECT company_id, column_id, value, status, updated_at FROM enrichment_values"
                )
                raw_vals = [dict(r) for r in await vcur.fetchall()]

    norm_cols = []
    for c in (cols or []):
        if isinstance(c, dict):
            norm_cols.append({
                **c,
                "id": str(c.get("id") or ""),
                "name": str(c.get("name") or c.get("title") or ""),
                "prompt": str(c.get("prompt") or c.get("instruction") or ""),
                "use_web_search": bool(c.get("use_web_search") or c.get("web_search")),
                "output_format": str(c.get("output_format") or c.get("dtype") or "text"),
            })

    values_map: dict[str, dict[str, dict]] = {}
    if isinstance(raw_vals, dict):
        for comp_id, inner in raw_vals.items():
            if isinstance(inner, dict):
                values_map[str(comp_id)] = {
                    str(cid): (v if isinstance(v, dict) else {"value": str(v or ""), "status": "done"})
                    for cid, v in inner.items()
                }
    elif isinstance(raw_vals, list):
        for r in raw_vals:
            if isinstance(r, dict) and r.get("company_id") is not None and r.get("column_id") is not None:
                values_map.setdefault(str(r["company_id"]), {})[str(r["column_id"])] = {
                    "value": str(r.get("value") or r.get("result") or ""),
                    "status": str(r.get("status") or "done"),
                    "updated_at": str(r.get("updated_at") or ""),
                }

    return {"columns": norm_cols, "values": values_map}


@_agentic_router.post("/api/enrichment/columns")
async def create_enrichment_column(request: Request):
    """Create a custom Clay-mode AI enrichment column."""
    from pydantic import ValidationError
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)

    mapped = {
        "name": str(body.get("name") or body.get("title") or "").strip(),
        "prompt": str(body.get("prompt") or body.get("instruction") or "").strip(),
        "use_web_search": bool(body.get("use_web_search", body.get("web_search", False))),
        "output_format": str(body.get("output_format") or body.get("dtype") or "text").strip() or "text",
    }
    try:
        validated = _EnrichmentColumnCreate.model_validate(mapped)
    except ValidationError as exc:
        return JSONResponse({"success": False, "message": str(exc.errors()[0].get("msg", "Invalid column."))},
                            status_code=400)

    state = _state()
    await state.init_db()

    for m_name in ("add_enrichment_column", "create_enrichment_column"):
        fn = getattr(state, m_name, None)
        if callable(fn):
            sig = inspect.signature(fn)
            kwargs: dict = {}
            for p_name in sig.parameters:
                if p_name in ("name", "title"):
                    kwargs[p_name] = validated.name
                elif p_name in ("prompt", "instruction"):
                    kwargs[p_name] = validated.prompt
                elif p_name in ("use_web_search", "web_search"):
                    kwargs[p_name] = validated.use_web_search
                elif p_name in ("output_format", "dtype", "kind"):
                    kwargs[p_name] = validated.output_format
            res = fn(**kwargs) if kwargs else fn(validated.name, validated.prompt)
            created = (await res) if inspect.isawaitable(res) else res
            if isinstance(created, dict):
                col_id = str(created.get("id") or "")
                col_obj = {
                    **created,
                    "id": col_id,
                    "name": validated.name,
                    "prompt": validated.prompt,
                    "use_web_search": validated.use_web_search,
                    "output_format": validated.output_format,
                }
            else:
                col_id = str(created)
                col_obj = {
                    "id": col_id,
                    "name": validated.name,
                    "prompt": validated.prompt,
                    "use_web_search": validated.use_web_search,
                    "output_format": validated.output_format,
                }
            return {"success": True, "id": col_id, "column": col_obj}

    col_id = "col_" + uuid.uuid4().hex[:10]
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with storage.connect(str(db_path)) as db:
        await _ensure_enrichment_tables(db)
        await db.execute(
            "INSERT INTO enrichment_columns (id, name, prompt, use_web_search, output_format) "
            "VALUES (?, ?, ?, ?, ?)",
            (col_id, validated.name, validated.prompt, int(validated.use_web_search), validated.output_format),
        )
        await db.commit()

    col_obj = {
        "id": col_id,
        "name": validated.name,
        "prompt": validated.prompt,
        "use_web_search": validated.use_web_search,
        "output_format": validated.output_format,
    }
    return {"success": True, "id": col_id, "column": col_obj}


@_agentic_router.delete("/api/enrichment/columns/{col_id}")
async def delete_enrichment_column(col_id: str):
    """Delete a Clay-mode AI enrichment column and its stored values."""
    clean_id = (col_id or "").strip()
    if not clean_id or len(clean_id) > 120:
        return JSONResponse({"success": False, "message": "Invalid column id."}, status_code=400)

    state = _state()
    await state.init_db()

    for m_name in ("delete_enrichment_column", "remove_enrichment_column"):
        fn = getattr(state, m_name, None)
        if callable(fn):
            arg_id: int | str = clean_id
            sig = inspect.signature(fn)
            first_param = next(iter(sig.parameters.values()), None)
            if first_param is not None and first_param.annotation is int and clean_id.isdigit():
                arg_id = int(clean_id)
            res = fn(arg_id)
            deleted = (await res) if inspect.isawaitable(res) else res
            if deleted is False:
                return JSONResponse({"success": False, "message": "Column not found."}, status_code=404)
            return {"success": True, "deleted": clean_id}

    db_path = _db_path()
    if not db_path.exists():
        return JSONResponse({"success": False, "message": "Column not found."}, status_code=404)
    async with storage.connect(str(db_path)) as db:
        await _ensure_enrichment_tables(db)
        cur = await db.execute("DELETE FROM enrichment_columns WHERE id = ?", (clean_id,))
        await db.execute("DELETE FROM enrichment_values WHERE column_id = ?", (clean_id,))
        await db.commit()
        if cur.rowcount == 0:
            return JSONResponse({"success": False, "message": "Column not found."}, status_code=404)
    return {"success": True, "deleted": clean_id}


@_agentic_router.post("/api/enrichment/run")
async def run_enrichment_route(request: Request):
    """Run ClayEnricher (or Brain fallback) across companies for custom AI columns."""
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    col_id = str(body.get("column_id") or "").strip() or None
    company_ids = [str(x) for x in body.get("company_ids", []) if x] if isinstance(body.get("company_ids"), list) else None
    try:
        limit = max(1, min(50, int(body.get("limit") or 10)))
    except (TypeError, ValueError):
        return JSONResponse({"success": False, "message": "limit must be an integer."}, status_code=400)

    brain = None
    try:
        brain, state, config = await _offer_brain()
        if not await _brain_within_budget(brain, config):
            return JSONResponse(
                {"success": False, "message": "Daily AI budget reached. Raise usage.max_daily_usd or try tomorrow."},
                status_code=429,
            )

        try:
            from sellbird.agents.enricher import ClayEnricher
        except ImportError:
            ClayEnricher = None

        if ClayEnricher is not None:
            from sellbird.config import load_env
            env = load_env()
            init_sig = inspect.signature(ClayEnricher)
            init_kwargs: dict = {}
            for p_name in init_sig.parameters:
                if p_name == "brain":
                    init_kwargs[p_name] = brain
                elif p_name == "state":
                    init_kwargs[p_name] = state
                elif p_name == "config":
                    init_kwargs[p_name] = config
                elif p_name == "env":
                    init_kwargs[p_name] = env
            enricher = ClayEnricher(**init_kwargs)
            run_sig = inspect.signature(enricher.run)
            run_kwargs: dict = {}
            for p_name in run_sig.parameters:
                if p_name in ("column_id", "col_id") and col_id is not None:
                    run_kwargs[p_name] = col_id
                elif p_name == "company_ids" and company_ids is not None:
                    run_kwargs[p_name] = company_ids
                elif p_name == "limit":
                    run_kwargs[p_name] = limit
            outcome = await enricher.run(**run_kwargs)
            return {
                "success": True,
                "result": outcome if isinstance(outcome, (dict, list, int, str, bool)) else str(outcome),
            }

        # Clean Brain fallback when ClayEnricher is not yet installed
        cols_payload = await get_enrichment_columns()
        columns = [c for c in cols_payload.get("columns", []) if not col_id or c["id"] == col_id]
        if not columns:
            return JSONResponse(
                {"success": False, "message": "Add at least one AI column before running enrichment."},
                status_code=400,
            )
        if company_ids:
            companies = await query_db(
                f"SELECT id, name, domain, website, industry, location, description FROM companies "
                f"WHERE id IN ({_in(company_ids)}) LIMIT ?",
                (*company_ids, limit),
            )
        else:
            companies = await query_db(
                "SELECT id, name, domain, website, industry, location, description FROM companies "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

        enriched_cells = 0
        ask_fn = getattr(brain, "complete", None) or getattr(brain, "ask", None) or getattr(brain, "think", None)
        if callable(ask_fn) and companies:
            db_path = _db_path()
            async with storage.connect(str(db_path)) as db:
                await _ensure_enrichment_tables(db)
                for comp in companies:
                    for col in columns:
                        prompt = (
                            f"Company: {comp.get('name') or ''} ({comp.get('domain') or ''})\n"
                            f"Industry: {comp.get('industry') or ''} | Location: {comp.get('location') or ''}\n"
                            f"Description: {comp.get('description') or ''}\n\n"
                            f"Task: {col['prompt']}\n"
                            "Return a concise factual answer (max 25 words)."
                        )
                        ans = ask_fn(prompt)
                        if inspect.isawaitable(ans):
                            ans = await ans
                        val_str = (ans.get("text") if isinstance(ans, dict) else str(ans or "")).strip()[:500]
                        await db.execute(
                            "INSERT INTO enrichment_values (company_id, column_id, value, status, updated_at) "
                            "VALUES (?, ?, ?, 'done', datetime('now')) "
                            "ON CONFLICT(company_id, column_id) DO UPDATE SET "
                            "value = excluded.value, status = 'done', updated_at = datetime('now')",
                            (str(comp["id"]), str(col["id"]), val_str),
                        )
                        enriched_cells += 1
                await db.commit()

        return {"success": True, "enriched_cells": enriched_cells, "companies": len(companies)}
    except Exception as exc:
        logger.warning("Enrichment run failed: %s", exc)
        return JSONResponse({"success": False, "message": f"Enrichment failed: {exc}"}, status_code=500)
    finally:
        await _close_brain(brain)


@_agentic_router.post("/api/research/run")
async def run_deep_research_route(request: Request):
    """Run DeepResearcher across target companies or a specific company/domain."""
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    company_id = str(body.get("company_id") or "").strip() or None
    domain = str(body.get("domain") or body.get("company") or "").strip() or None
    try:
        limit = max(1, min(50, int(body.get("limit") or 5)))
    except (TypeError, ValueError):
        return JSONResponse({"success": False, "message": "limit must be an integer."}, status_code=400)

    try:
        from sellbird.agents.researcher import DeepResearcher
    except ImportError:
        return JSONResponse(
            {"success": False, "message": "DeepResearcher agent is not installed yet."},
            status_code=501,
        )

    brain = None
    try:
        brain, state, config = await _offer_brain()
        if not await _brain_within_budget(brain, config):
            return JSONResponse(
                {"success": False, "message": "Daily AI budget reached. Raise usage.max_daily_usd or try tomorrow."},
                status_code=429,
            )
        from sellbird.config import load_env
        env = load_env()

        init_sig = inspect.signature(DeepResearcher)
        init_kwargs: dict = {}
        for p_name in init_sig.parameters:
            if p_name == "brain":
                init_kwargs[p_name] = brain
            elif p_name == "state":
                init_kwargs[p_name] = state
            elif p_name == "config":
                init_kwargs[p_name] = config
            elif p_name == "env":
                init_kwargs[p_name] = env
        researcher = DeepResearcher(**init_kwargs)

        run_sig = inspect.signature(researcher.run)
        run_kwargs: dict = {}
        for p_name in run_sig.parameters:
            if p_name == "limit":
                run_kwargs[p_name] = limit
            elif p_name == "company_id" and company_id is not None:
                run_kwargs[p_name] = company_id
            elif p_name in ("domain", "company") and domain is not None:
                run_kwargs[p_name] = domain

        if domain and not run_kwargs and hasattr(researcher, "research_domain"):
            outcome = await researcher.research_domain(domain)
        elif company_id and not run_kwargs and hasattr(researcher, "research_company"):
            outcome = await researcher.research_company(company_id)
        else:
            outcome = await researcher.run(**run_kwargs)

        return {
            "success": True,
            "result": outcome if isinstance(outcome, (dict, list, int, str, bool)) else str(outcome),
        }
    except Exception as exc:
        logger.warning("Deep research failed: %s", exc)
        return JSONResponse({"success": False, "message": f"Deep research failed: {exc}"}, status_code=500)
    finally:
        await _close_brain(brain)


app.include_router(_agentic_router)

from sellbird.inbox_routes import router as _inbox_router  # noqa: E402

app.include_router(_inbox_router)


# ── Extensions (sellbird.extensions): their routes, after every built-in one,
# so an extension can add paths but never replace one. ──
from sellbird import extensions as _extensions  # noqa: E402

for _router in _extensions.registry().routers:
    app.include_router(_router)


def build_app(*, guard: Middleware | None = None, routers: Sequence[APIRouter] = ()) -> FastAPI:
    """A fresh app serving the module-level ``app``'s routes, middleware and
    exception handlers, for a server that runs Sellbird for someone else.

    ``routers`` come first, so they win over a built-in route on the same
    path. The built-in routes are mounted live, not copied: a route added to
    ``app`` later still reaches every built app. ``guard`` takes
    ``auth.DashboardGuard``'s place in the middleware stack (a server's own
    sign-in instead of the dashboard token). Set ``state.home`` on the result
    to run its requests in that home."""
    new = FastAPI(title=app.title, docs_url=None, redoc_url=None, openapi_url=None)
    for router in routers:
        new.include_router(router)
    new.router.routes.append(Mount("", app=app.router))
    new.user_middleware = [guard if guard is not None and m.cls is auth.DashboardGuard else m
                           for m in app.user_middleware]
    new.exception_handlers.update(app.exception_handlers)
    return new


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server (loopback only, token required)."""
    import uvicorn

    try:
        bind = _check_loopback_bind(host)
        url = dashboard_url(host, port)
    except (DashboardSetupError, auth.TokenError) as e:
        raise DashboardSetupError(str(e)) from e

    print(f"\n  Sellbird Dashboard running. Open this link to unlock it:\n\n    {url}\n")
    print(f"  The token is stored in {token_store.path} (owner-only). Keep the link")
    print("  private; anyone with it can use the dashboard. Press Ctrl+C to stop.\n")
    if os.name == "nt":
        print("  Windows: the token file is protected by your folder's permissions,")
        print("  not POSIX file modes.\n")
    from sellbird import dashboard_instance
    data_dir = _data_dir()
    dashboard_instance.write(data_dir, host=bind, port=port, instance=INSTANCE_ID)
    try:
        uvicorn.run(app, host=bind, port=port, log_level="warning",
                    proxy_headers=False, server_header=False, date_header=False)
    finally:
        dashboard_instance.remove(data_dir, instance=INSTANCE_ID)
