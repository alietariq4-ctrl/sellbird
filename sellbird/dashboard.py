"""Sellbird Dashboard — local web UI to set up, control, and monitor Sellbird."""

import asyncio
import dataclasses
import inspect
import io
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
from urllib.parse import urlparse

import aiosqlite
import yaml
from dotenv import dotenv_values, load_dotenv
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse,
)
from pydantic import BaseModel, Field
from starlette.middleware import Middleware
from starlette.routing import Mount

from sellbird.logsafety import RedactSecrets, quiet_http_loggers

logger = logging.getLogger("sellbird.dashboard")


def _attach_secret_redaction() -> None:
    """Attach RedactSecrets filter to dashboard & root logger handlers."""
    quiet_http_loggers()
    for target in (logger, logging.getLogger(), logging.getLogger("uvicorn.error"), logging.getLogger("uvicorn.access")):
        if not any(isinstance(f, RedactSecrets) for f in target.filters):
            target.addFilter(RedactSecrets())
        for handler in getattr(target, "handlers", ()):
            if not any(isinstance(f, RedactSecrets) for f in handler.filters):
                handler.addFilter(RedactSecrets())


_attach_secret_redaction()

from sellbird import agent_policy  # noqa: E402
from sellbird import dashboard_auth as auth  # noqa: E402
from sellbird.paths import PACKAGE_ROOT, PROJECT_ROOT, is_checkout  # noqa: E402
from sellbird import storage  # noqa: E402
from sellbird.pidfile import LOCK_FILE, loop_lock_held, pid_alive  # noqa: E402
from sellbird.textio import read_user_text  # noqa: E402
from sellbird.workspace_home import NotOnHosted, current_home, default_home, require_self_hosted, use_home  # noqa: E402
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
_sellbird_process_new_group: bool = False
_sellbird_started_at: datetime | None = None
_sellbird_lock = asyncio.Lock()
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
    """Read .env file and return as dict (quote/comment/export-aware)."""
    env_file = _env_file()
    if not env_file.exists():
        return {}
    raw = dotenv_values(stream=io.StringIO(read_user_text(env_file)))
    return {k: (v if v is not None else "") for k, v in raw.items() if k}


def _format_env_value(val: str) -> str:
    """Format a .env value, quoting if it contains comments, whitespace, quotes, or backslashes."""
    if val == "":
        return ""
    if any(ch in val for ch in ("#", " ", "\t", '"', "'", "\\")):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return val


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
            out.append(f"{key}={_format_env_value(updates[key])}")
            written.add(key)
        else:
            out.append(line)
    out += [f"{k}={_format_env_value(v)}" for k, v in updates.items() if k not in written]

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
    env_exists = env_file.exists() and any(str(v or "").strip() for v in env_vars.values())
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
        instantly_key = (env_vars.get("INSTANTLY_API_KEY", "") or os.getenv("INSTANTLY_API_KEY", "")).strip()
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
    checks.append({
        "id": "linkedin", "label": "LinkedIn credentials",
        "done": _has("LINKEDIN_EMAIL", "LINKEDIN_PASSWORD"),
        "required": False,
        "help": "Optional. Enter your LinkedIn credentials in Settings to enable LinkedIn prospecting.",
    })

    # 8. Cloudflare (optional)
    checks.append({
        "id": "cloudflare", "label": "Cloudflare deep crawling",
        "done": _has("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"),
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
    "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM",
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
    "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "CALCOM_WEBHOOK_SECRET",
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
        "smtp_from": env_vars.get("SMTP_FROM", ""),
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
        "calcom_webhook_secret_set": is_set("CALCOM_WEBHOOK_SECRET"),
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
    require_self_hosted("env_settings")
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


@app.post("/api/settings/email-provider")
async def set_active_email_provider(request: Request):
    """Switch channels.email.provider in the active sellbird config YAML."""
    require_self_hosted("env_settings")
    try:
        data = await request.json()
    except Exception:
        data = {}
    provider = str((data.get("provider") if isinstance(data, dict) else "") or "").strip().lower()
    allowed = {"gmail", "microsoft", "smtp", "instantly"}
    if provider not in allowed:
        return JSONResponse(
            {"success": False, "message": f"Provider must be one of: {', '.join(sorted(allowed))}."},
            status_code=400,
        )
    target = _offer_config_file()
    try:
        import yaml
        from sellbird.offer import read_user_text

        raw = read_user_text(target) if target.exists() else ""
        cfg_data = yaml.safe_load(raw) or {}
        if not isinstance(cfg_data, dict):
            cfg_data = {}
        ch = cfg_data.setdefault("channels", {})
        if not isinstance(ch, dict):
            ch = {}
            cfg_data["channels"] = ch
        em = ch.setdefault("email", {})
        if not isinstance(em, dict):
            em = {}
            ch["email"] = em
        em["provider"] = provider
        if target.exists():
            target.with_name(target.name + ".bak").write_text(raw, encoding="utf-8")
        target.write_text(
            yaml.safe_dump(cfg_data, default_flow_style=False, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return {"success": True, "provider": provider}
    except Exception as exc:
        logger.warning("Failed to update email provider: %s", exc)
        return JSONResponse({"success": False, "message": f"Couldn't update config: {exc}"}, status_code=500)


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
    config = _load_active_sellbird_config()
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
    from sellbird.offer import offer_view
    try:
        return offer_view(_load_active_sellbird_config())
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
    """Save the offer to product.offer (entry + optional booking_url). The Writer reads it on its next run."""
    from sellbird.offer import MAX_OFFER_CHARS, save_company, save_offer
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    body = body if isinstance(body, dict) else {}
    entry = " ".join(str(body.get("entry") or "").split())[:MAX_OFFER_CHARS]
    ideas = body.get("ideas")
    ideas = [str(i) for i in ideas] if isinstance(ideas, list) else None
    target = _offer_config_file()
    try:
        save_offer(target, entry, ideas)
        if "booking_url" in body:
            save_company(target, "offer", {"booking_url": str(body.get("booking_url") or "").strip()})
        from sellbird.offer import PROPOSED_KEY
        state = _state()
        await state.init_db()
        await state.delete_setting(PROPOSED_KEY)
    except Exception as e:
        return JSONResponse({"success": False, "message": f"Couldn't save to {target.name}: {e}"},
                            status_code=500)
    return {"success": True, "entry": entry,
            "booking_url": str(body.get("booking_url") or "").strip() if "booking_url" in body else None,
            "message": "Saved. Every new first email ends with this offer." if entry
                       else "Saved. With no offer, first emails end with a question."}


@app.get("/api/company")
async def get_company():
    """Your company: everything Sellbird writes from, and any offer your agent proposed."""
    from sellbird.offer import PROPOSED_KEY, company_view
    try:
        state = _state()
        await state.init_db()
        raw = await state.get_setting(PROPOSED_KEY)
        proposed = json.loads(raw) if raw else None
        return company_view(_load_active_sellbird_config(), _home_root(), proposed)
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


def _fallback_parse_brief(brief: str) -> dict:
    """Deterministic parser turning a plain-text business goal into Sellbird product, offer, and ICP."""
    import re as _re
    low = brief.lower()
    price_m = _re.search(r"(\$\s?\d+(?:[.,]\d+)?(?:/(?:mo|month|yr|year))?)", brief, _re.IGNORECASE)
    pricing = price_m.group(1).replace(" ", "") if price_m else "Custom pricing"

    known_trades = [
        ("dentist", "dentist"), ("dental", "dentist"), ("hvac", "hvac"), ("plumb", "plumber"),
        ("roof", "roofer"), ("salon", "salon"), ("barber", "barber"), ("gym", "gym"),
        ("clinic", "clinic"), ("vet", "vet"), ("lawyer", "lawyer"), ("legal", "lawyer"),
        ("real estate", "real estate"), ("realtor", "real estate"), ("restaurant", "restaurant"),
        ("auto", "auto repair"), ("saas", "saas"), ("ecommerce", "ecommerce"), ("agency", "marketing agency"),
    ]
    industries = []
    for needle, label in known_trades:
        if needle in low and label not in industries:
            industries.append(label)
    if not industries:
        industries = ["saas"]

    cities = []
    for m in _re.finditer(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?,\s*[A-Z]{2})\b", brief):
        if m.group(1) not in cities:
            cities.append(m.group(1))
    if not cities:
        for city_kw in ("Austin, TX", "Denver, CO", "New York, NY", "London, UK", "Toronto, Canada", "Lahore", "Karachi", "Dubai"):
            if city_kw.split(",")[0].lower() in low:
                cities.append(city_kw)

    first_clause = brief.strip().split(".")[0].strip()
    prod_name = first_clause[:48] if len(first_clause) <= 48 else (industries[0].title() + " Growth Solution")
    titles = ["Founder", "Owner", "CEO"] if "saas" in industries else ["Owner", "Practice Manager", "Director"]
    return {
        "product": {
            "name": prod_name,
            "pricing": pricing,
            "description": brief.strip()[:400],
            "key_benefits": [
                "Automated prospect discovery and personalization",
                "Consistent follow-ups without manual effort",
            ],
        },
        "offer": {
            "entry": f"a quick 15-min tailored audit showing how {prod_name} fits your business"[:240],
        },
        "icp": {
            "industries": industries,
            "titles": titles,
            "geography": cities or ["Austin, TX"],
        },
    }


@app.post("/api/company/setup-from-brief")
async def setup_company_from_brief(request: Request):
    """Turn a plain-language goal/description into saved Product, Offer, and ICP settings."""
    from sellbird.offer import save_company
    from sellbird.untrusted import UNTRUSTED_RULE, fence

    try:
        body = await request.json()
    except Exception:
        body = {}
    brief = str((body or {}).get("brief") or "").strip()
    if len(brief) < 8:
        return JSONResponse(
            {"success": False, "message": "Describe what you sell and who you want to reach (at least a few words)."},
            status_code=400,
        )

    parsed = None
    used_ai = False
    runtime, _err = await _prepare_agentic_runtime()
    if runtime is not None:
        brain, _st, _cfg, _env = runtime
        try:
            prompt = (
                "Extract structured B2B/local outreach setup from the operator's plain-language goal (English or Roman Urdu).\n"
                f"{UNTRUSTED_RULE}\n"
                f"Operator brief:\n{fence(brief[:1200], 'brief')}\n\n"
                'Return strictly JSON: {"product": {"name": "...", "pricing": "...", "description": "...", '
                '"key_benefits": ["...", "..."]}, "offer": {"entry": "free low-friction offer under 120 chars"}, '
                '"icp": {"industries": ["..."], "titles": ["..."], "geography": ["City, ST"]}}'
            )
            raw = await brain.think_json(prompt, session_id="company-brief-setup", agent="scout", task="setup_from_brief")
            if isinstance(raw, dict) and isinstance(raw.get("product"), dict):
                parsed = raw
                used_ai = True
        except Exception:
            parsed = None
        finally:
            await _close_brain(brain)

    if not parsed:
        parsed = _fallback_parse_brief(brief)

    cfg_path = _offer_config_file()
    prod = parsed.get("product") or {}
    offer = parsed.get("offer") or {}
    icp = parsed.get("icp") or {}
    prod_fields = {k: v for k, v in {
        "name": str(prod.get("name") or "").strip()[:120],
        "pricing": str(prod.get("pricing") or "").strip()[:120],
        "description": str(prod.get("description") or "").strip()[:500],
        "key_benefits": [str(x).strip() for x in (prod.get("key_benefits") or []) if str(x).strip()][:6],
    }.items() if v}
    icp_fields = {k: v for k, v in {
        "industries": [str(x).strip().lower() for x in (icp.get("industries") or []) if str(x).strip()][:12],
        "titles": [str(x).strip() for x in (icp.get("titles") or []) if str(x).strip()][:10],
        "geography": [str(x).strip() for x in (icp.get("geography") or []) if str(x).strip()][:10],
    }.items() if v}
    try:
        if prod_fields:
            save_company(cfg_path, "product", prod_fields)
        if icp_fields:
            save_company(cfg_path, "icp", icp_fields)
        entry_val = str(offer.get("entry") or "").strip()[:300]
        if entry_val:
            save_company(cfg_path, "offer", {"entry": entry_val})
    except Exception as exc:
        return JSONResponse({"success": False, "message": f"Could not save extracted setup: {exc}"}, status_code=400)

    state = _state()
    await state.init_db()
    await state.log_action("company_setup_from_brief", "dashboard", {"used_ai": used_ai, "industries": icp_fields.get("industries", [])})
    return {"success": True, "used_ai": used_ai, "product": prod_fields, "offer": {"entry": str(offer.get("entry") or "").strip()}, "icp": icp_fields}


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


@app.post("/api/company/train")
async def train_company_from_url(request: Request):
    """Crawl a public website URL and train Sellbird's config + knowledge skills."""
    require_self_hosted("local_ai_loop")
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)

    raw_url = str(body.get("url") or "").strip()
    if not raw_url:
        return JSONResponse({"success": False, "message": "Website URL is required."}, status_code=400)

    raw_max = body.get("max_pages", 25)
    if isinstance(raw_max, bool):
        return JSONResponse({"success": False, "message": "max_pages must be an integer."}, status_code=400)
    try:
        max_pages = max(1, min(50, int(raw_max)))
    except (TypeError, ValueError):
        return JSONResponse({"success": False, "message": "max_pages must be an integer."}, status_code=400)

    from sellbird.brain import Brain
    from sellbird.config import load_brain_config, load_config
    from sellbird.integrations.stealth_fetch import validate_public_url
    from sellbird.public_http import UnsafeURLError
    from sellbird.trainer import Trainer, _normalize_start_url

    norm_url = _normalize_start_url(raw_url)
    parsed = urlparse(norm_url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return JSONResponse({"success": False, "message": "Enter a valid http(s) website URL."}, status_code=400)

    try:
        await validate_public_url(norm_url)
    except (UnsafeURLError, ValueError) as exc:
        return JSONResponse({"success": False, "message": f"Invalid or unsafe URL: {exc}"}, status_code=400)

    state = _state()
    await state.init_db()
    brain_cfg = load_brain_config()
    trainer = Trainer(brain_config=brain_cfg)
    trainer.state = state
    await _close_brain(trainer.brain)
    trainer.brain = Brain(state, brain_cfg)

    try:
        unavail = await trainer.brain.preflight()
        if unavail:
            return JSONResponse({"success": False, "message": unavail}, status_code=503)
        try:
            app_cfg = load_config(str(_active_config_path()))
        except Exception:
            app_cfg = None
        if not await _brain_within_budget(trainer.brain, app_cfg):
            return JSONResponse(
                {"success": False, "message": "Daily AI budget reached. Raise usage.max_daily_usd or try tomorrow."},
                status_code=429,
            )
        cfg = await trainer.train(norm_url, output_path=str(_offer_config_file()), max_pages=max_pages)
        if cfg is None:
            return JSONResponse(
                {"success": False, "message": "Could not train from that website. Check the URL and try again."},
                status_code=502,
            )
        pages_crawled = len(getattr(trainer, "scraped_pages", {}) or {})
        await state.log_action(
            "company_trained", "dashboard", {"url": norm_url, "pages_crawled": pages_crawled}
        )
        return {
            "success": True,
            "url": norm_url,
            "pages_crawled": pages_crawled,
            "company": ((cfg.get("persona") or {}).get("company", "") if isinstance(cfg, dict) else ""),
            "product": ((cfg.get("product") or {}).get("name", "") if isinstance(cfg, dict) else ""),
        }
    except UnsafeURLError as exc:
        return JSONResponse({"success": False, "message": f"Invalid or unsafe URL: {exc}"}, status_code=400)
    except Exception as exc:
        logger.warning("Company training failed: %s", exc)
        return JSONResponse({"success": False, "message": f"Training failed: {exc}"}, status_code=502)
    finally:
        await _close_brain(trainer.brain)


@app.post("/api/settings/test-instantly")
async def test_instantly(request: Request):
    """Test an Instantly API key (falls back to the saved key when empty)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    api_key = str((data.get("api_key") if isinstance(data, dict) else "") or "").strip()
    if not api_key:
        env_val = _read_env_file().get("INSTANTLY_API_KEY", "")
        if not env_val and not (_in_custom_home() and current_home().hosted):
            env_val = os.getenv("INSTANTLY_API_KEY", "")
        api_key = env_val.strip()
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


@app.post("/api/settings/test-smtp")
async def test_smtp_connection(request: Request):
    """Test SMTP + IMAP mailbox credentials (falls back to saved .env values when empty)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)

    env_vars = _read_env_file()
    hosted_custom = _in_custom_home() and current_home().hosted

    def _pick_setting(body_keys: tuple[str, ...], env_key: str, default: str = "") -> str:
        for k in body_keys:
            val = data.get(k)
            if val is not None and str(val).strip():
                return str(val).strip()
        val = (env_vars.get(env_key, "") or "").strip()
        if not val and not hosted_custom:
            val = (os.getenv(env_key, "") or "").strip()
        return val or default

    smtp_host = _pick_setting(("smtp_host", "SMTP_HOST"), "SMTP_HOST")
    smtp_port_raw = _pick_setting(("smtp_port", "SMTP_PORT"), "SMTP_PORT", "587")
    smtp_user = _pick_setting(("smtp_username", "SMTP_USERNAME"), "SMTP_USERNAME")
    smtp_pass = _pick_setting(("smtp_password", "SMTP_PASSWORD"), "SMTP_PASSWORD")
    imap_host = _pick_setting(("imap_host", "IMAP_HOST"), "IMAP_HOST") or smtp_host
    imap_port_raw = _pick_setting(("imap_port", "IMAP_PORT"), "IMAP_PORT", "993")
    imap_user = _pick_setting(("imap_username", "IMAP_USERNAME"), "IMAP_USERNAME") or smtp_user
    same_imap_pass = bool(data.get("imap_same_pass"))
    imap_pass = (
        smtp_pass
        if same_imap_pass
        else (_pick_setting(("imap_password", "IMAP_PASSWORD"), "IMAP_PASSWORD") or smtp_pass)
    )

    try:
        smtp_port = int(smtp_port_raw or 587)
        imap_port = int(imap_port_raw or 993)
    except ValueError:
        return JSONResponse(
            {"success": False, "message": "SMTP and IMAP ports must be valid numbers."},
            status_code=400,
        )

    if not (smtp_host and smtp_user and smtp_pass):
        return {
            "success": False,
            "message": "Enter SMTP host, username, and password first (or save them in Settings).",
        }

    try:
        from sellbird.config import load_env
        from sellbird.integrations.smtp_mail import SmtpImapProvider, SmtpSettings

        try:
            config = _load_active_sellbird_config()
        except Exception:
            config = None
        env = load_env()
        settings = SmtpSettings(
            host=smtp_host,
            username=smtp_user,
            password=smtp_pass,
            port=smtp_port,
            imap_host=imap_host,
            imap_port=imap_port,
        )
        provider = SmtpImapProvider(config, env, smtp=settings)
        provider.imap_user = imap_user
        provider.imap_pass = imap_pass
        ok, message = await provider.test_connection()
        return {"success": bool(ok), "message": message}
    except Exception as exc:
        return {"success": False, "message": f"SMTP/IMAP check failed: {exc}"}


@app.post("/api/integrations/test")
async def test_integration_connection(request: Request):
    """Live connection & SSRF-validated configuration test for Calendar, CRM, and Copilot integrations."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)

    service = str(data.get("service") or "").strip().lower()
    if service not in ("telegram", "slack", "calcom", "twenty"):
        return JSONResponse(
            {"success": False, "message": "Unsupported integration service."},
            status_code=400,
        )

    env_file_vars = _read_env_file()
    hosted_custom = _in_custom_home() and current_home().hosted

    def _resolve_val(body_keys: tuple[str, ...], env_key: str, default: str = "") -> str:
        for k in body_keys:
            raw = data.get(k)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        val = (env_file_vars.get(env_key, "") or "").strip()
        if not val and not hosted_custom:
            val = (os.getenv(env_key, "") or "").strip()
        return val or default

    import httpx
    from sellbird.integrations.stealth_fetch import validate_public_url

    if service == "telegram":
        token = _resolve_val(("telegram_bot_token", "bot_token", "token"), "TELEGRAM_BOT_TOKEN")
        if not token:
            return {"success": False, "service": "telegram", "message": "No Telegram bot token provided."}
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            if resp.status_code == 200:
                payload = resp.json()
                if payload.get("ok"):
                    username = str((payload.get("result") or {}).get("username") or "").strip()
                    msg = f"Connected to Telegram (@{username})." if username else "Connected to Telegram."
                    return {"success": True, "service": "telegram", "username": username, "message": msg}
            return {"success": False, "service": "telegram", "message": f"Telegram API returned {resp.status_code}."}
        except Exception as exc:
            safe_err = str(exc).replace(token, "[REDACTED]")
            return {"success": False, "service": "telegram", "message": f"Connection failed: {safe_err}"}

    if service == "calcom":
        api_key = _resolve_val(("calcom_api_key", "api_key"), "CALCOM_API_KEY")
        if not api_key:
            return {"success": False, "service": "calcom", "message": "No Cal.com API key provided."}
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                resp = await client.get(
                    "https://api.cal.com/v2/me",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if resp.status_code == 200:
                return {"success": True, "service": "calcom", "message": "Connected to Cal.com."}
            return {"success": False, "service": "calcom", "message": f"Cal.com API returned {resp.status_code}."}
        except Exception as exc:
            return {"success": False, "service": "calcom", "message": f"Connection failed: {exc}"}

    if service == "twenty":
        base_url = _resolve_val(
            ("twenty_base_url", "base_url", "url"), "TWENTY_BASE_URL", "https://api.twenty.com"
        ).rstrip("/")
        try:
            await validate_public_url(base_url)
        except ValueError as exc:
            return JSONResponse(
                {"success": False, "service": "twenty", "message": f"Invalid Twenty CRM URL: {exc}"},
                status_code=400,
            )
        api_key = _resolve_val(("twenty_api_key", "api_key"), "TWENTY_API_KEY")
        if not api_key:
            return {"success": False, "service": "twenty", "message": "No Twenty CRM API key provided."}
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                resp = await client.get(
                    f"{base_url}/rest/people?limit=1",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if resp.status_code == 200:
                return {"success": True, "service": "twenty", "message": "Connected to Twenty CRM."}
            return {"success": False, "service": "twenty", "message": f"Twenty CRM returned {resp.status_code}."}
        except Exception as exc:
            return {"success": False, "service": "twenty", "message": f"Connection failed: {exc}"}

    # service == "slack"
    webhook_url = _resolve_val(("slack_webhook_url", "webhook_url", "url"), "SLACK_WEBHOOK_URL")
    if not webhook_url:
        return {"success": False, "service": "slack", "message": "No Slack webhook URL provided."}
    if not webhook_url.startswith("https://hooks.slack.com/"):
        return JSONResponse(
            {"success": False, "service": "slack",
             "message": "Slack webhook URL must start with https://hooks.slack.com/"},
            status_code=400,
        )
    try:
        await validate_public_url(webhook_url)
    except ValueError as exc:
        return JSONResponse(
            {"success": False, "service": "slack", "message": f"Invalid Slack webhook URL: {exc}"},
            status_code=400,
        )
    if bool(data.get("send_test")):
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                resp = await client.post(webhook_url, json={"text": "Sellbird integration test"})
            if resp.status_code in (200, 204):
                return {"success": True, "service": "slack", "message": "Sent test ping to Slack."}
            return {"success": False, "service": "slack", "message": f"Slack webhook returned {resp.status_code}."}
        except Exception as exc:
            return {"success": False, "service": "slack", "message": f"Connection failed: {exc}"}
    return {"success": True, "service": "slack", "message": "Slack webhook URL is valid."}


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


def _sellbird_needs_setup() -> bool:
    """Check whether Sellbird needs setup using resolved workspace paths."""
    if (_in_custom_home()
            or Path(ENV_FILE) != PROJECT_ROOT / ".env"
            or Path(CONFIG_FILE) != PROJECT_ROOT / "sellbird.yaml"):
        if not _env_file().exists():
            return True
        config_file = _resolve_config_file()
        if not config_file.exists():
            return True
        try:
            cfg = yaml.safe_load(read_user_text(config_file))
            if not isinstance(cfg, dict):
                return True
            company = (cfg.get("persona") or {}).get("company", "")
            if company in ("Your Company", ""):
                return True
        except Exception:
            return True
        return False
    from sellbird.main import _needs_setup
    return bool(_needs_setup())


@app.get("/api/sellbird/status")
async def get_sellbird_status():
    """Check if Sellbird is currently running."""
    require_self_hosted("local_ai_loop")
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
    global _sellbird_process, _sellbird_process_new_group, _sellbird_started_at
    require_self_hosted("local_ai_loop")

    async with _sellbird_lock:
        if _check_sellbird_pid():
            return {"success": False, "message": "Sellbird is already running."}
        if _sellbird_needs_setup():
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
        env["SELLBIRD_HOME"] = str(_home_root())
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONSAFEPATH"] = "1"
        local_bin = Path.home() / ".local" / "bin"
        if not shutil.which("claude") and local_bin.is_dir():
            env["PATH"] = str(local_bin) + os.pathsep + env.get("PATH", "")

        popen_kwargs: dict = {}
        new_group = False
        if sys.platform == "win32":
            new_group_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            popen_kwargs["creationflags"] = (
                new_group_flag
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            new_group = bool(new_group_flag)
        else:
            popen_kwargs["start_new_session"] = True

        try:
            log_handle = open(log_file, "a", encoding="utf-8")
            try:
                proc = subprocess.Popen(
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

        # Wait briefly and verify the child didn't exit immediately on startup.
        await asyncio.sleep(0.35)
        rc = proc.poll()
        if rc is not None:
            _sellbird_process = None
            _sellbird_process_new_group = False
            _sellbird_started_at = None
            return {"success": False,
                    "message": f"Sellbird exited immediately (code {rc}). Check the logs."}

        try:
            setattr(proc, "_sellbird_new_group", new_group)
        except Exception:
            pass
        _sellbird_process = proc
        _sellbird_started_at = datetime.now()
        # The loop writes its own PID file once it holds the loop lock. Writing
        # the child's PID here would overwrite a running loop's record if this
        # child then finds the lock taken and exits.
        return {"success": True, "pid": _sellbird_process.pid}


@app.post("/api/sellbird/stop")
async def stop_sellbird():
    """Stop the Sellbird subprocess."""
    global _sellbird_process, _sellbird_process_new_group, _sellbird_started_at
    require_self_hosted("local_ai_loop")

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
        # which can cut off active SMTP sends mid-flight. Write sellbird.stop and
        # send CTRL_BREAK_EVENT first (when in a new process group), then fall back
        # to TerminateProcess / taskkill after waiting.
        try:
            stop_file.parent.mkdir(parents=True, exist_ok=True)
            stop_file.write_text(str(pid), encoding="utf-8")
        except OSError as e:
            logger.warning("Couldn't write stop sentinel %s: %s", stop_file, e)

        own_new_group = bool(getattr(own, "_sellbird_new_group", False) or _sellbird_process_new_group)
        if own is not None and own_new_group and hasattr(signal, "CTRL_BREAK_EVENT"):
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            except OSError as e:
                logger.warning("Couldn't send CTRL_BREAK_EVENT to Sellbird (pid %s): %s", pid, e)

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
            if alive():
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=5,
                    )
                except Exception as e:
                    logger.warning("taskkill failed for Sellbird (pid %s): %s", pid, e)
                await asyncio.sleep(0.3)
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
    _sellbird_process_new_group = False
    _sellbird_started_at = None
    _pid_file().unlink(missing_ok=True)

    return {"success": True}


@app.get("/api/sellbird/logs")
async def get_sellbird_logs():
    """Get recent log lines."""
    require_self_hosted("process_logs")
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

        # Conversion funnel across existing tables
        company_count = await _scalar("SELECT COUNT(*) FROM companies")
        researched_count = await _scalar(
            "SELECT COUNT(*) FROM companies "
            "WHERE profile_checked_at IS NOT NULL "
            "OR id IN (SELECT DISTINCT company_id FROM observations WHERE company_id != '')"
        )
        drafted_pids = {
            r["prospect_id"]
            for r in await query_db(
                "SELECT DISTINCT prospect_id FROM outbox WHERE kind = 'sequence' AND prospect_id != ''"
            )
            if r.get("prospect_id")
        }
        for crow in await query_db("SELECT prospect_ids_json FROM campaigns"):
            try:
                pids_list = json.loads(crow.get("prospect_ids_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(pids_list, list):
                drafted_pids.update(str(pid) for pid in pids_list if pid)

        sent_pids = {
            r["prospect_id"]
            for r in await query_db(
                "SELECT DISTINCT prospect_id FROM outbox WHERE status = 'sent' AND prospect_id != ''"
            )
            if r.get("prospect_id")
        } | {
            r["id"]
            for r in await query_db(
                "SELECT id FROM prospects WHERE status IN ('contacted', 'replied', 'meeting', 'closed')"
            )
            if r.get("id")
        }
        replied_pids = {
            r["prospect_id"]
            for r in await query_db(
                "SELECT DISTINCT prospect_id FROM conversations WHERE prospect_id != ''"
            )
            if r.get("prospect_id")
        } | {
            r["id"]
            for r in await query_db("SELECT id FROM prospects WHERE status IN ('replied', 'meeting')")
            if r.get("id")
        }
        booked_pids = {
            r["id"]
            for r in await query_db("SELECT id FROM prospects WHERE status = 'meeting'")
            if r.get("id")
        } | {
            r["prospect_id"]
            for r in await query_db(
                "SELECT prospect_id FROM conversations WHERE status = 'meeting_booked' AND prospect_id != ''"
            )
            if r.get("prospect_id")
        }
        funnel = {
            "companies": company_count,
            "contacts": prospect_total,
            "researched": researched_count,
            "drafted": len(drafted_pids),
            "sent": len(sent_pids),
            "replied": len(replied_pids),
            "booked": len(booked_pids),
        }

        return {
            "prospects": {"total": prospect_total, "by_status": prospect_map},
            "campaigns": {"total": sum(campaign_map.values()), "by_status": campaign_map},
            "conversations": {"total": sum(convo_map.values()), "by_status": convo_map},
            "pipeline": pipeline,
            "funnel": funnel,
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
async def get_prospects(prospect_id: str = ""):
    rows = await query_db("SELECT * FROM prospects ORDER BY created_at DESC LIMIT 200")
    pid = (prospect_id or "").strip()
    if pid and all(r.get("id") != pid for r in rows):
        extra = await query_db("SELECT * FROM prospects WHERE id = ?", (pid,))
        if extra:
            rows.insert(0, extra[0])
    # Someone whose opt-out was undone keeps a way to see what happened.
    undone = {r["pid"] for r in await query_db(
        "SELECT DISTINCT json_extract(details_json, '$.prospect_id') AS pid FROM actions "
        "WHERE action_type = 'opt_out_undone' AND json_valid(details_json)")}
    for r in rows:
        r["opt_out_undone"] = r.get("id") in undone
    return rows


_FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "aol.com", "proton.me", "protonmail.com", "gmx.com",
})


def _live_dnc_entries() -> list:
    try:
        from sellbird.config import do_not_contact_from_file, live_do_not_contact, load_config
        cfg_path = _resolve_config_file()
        try:
            cfg = load_config(str(cfg_path))
            return live_do_not_contact(cfg)
        except BaseException:
            return do_not_contact_from_file(cfg_path) if cfg_path.exists() else []
    except BaseException:
        return []


@app.post("/api/prospects")
async def create_prospect(request: Request):
    """Create a single contact manually with email validation, DNC/suppression checks, and dedup."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)

    from sellbird.gate import EMAIL_RE, do_not_contact_match
    from sellbird.models.prospect import Prospect

    email = str(body.get("email") or "").strip().lower()
    if not email or not EMAIL_RE.match(email):
        return JSONResponse({"success": False, "message": "A valid email address is required."}, status_code=400)

    first_name = str(body.get("first_name") or "").strip()
    last_name = str(body.get("last_name") or "").strip()
    title = str(body.get("title") or "").strip()
    company_name = str(body.get("company") or "").strip()
    phone = str(body.get("phone") or "").strip()
    linkedin_url = str(body.get("linkedin_url") or "").strip()
    industry = str(body.get("industry") or "").strip()
    location = str(body.get("location") or "").strip()
    email_status = str(body.get("email_status") or "verified").strip().lower()
    if email_status not in ("verified", "risky", "guess", "invalid"):
        email_status = "verified"
    raw_tags = body.get("tags")
    if isinstance(raw_tags, list):
        tags = ", ".join(sorted({str(t).strip().lower() for t in raw_tags if str(t).strip()}))
    else:
        tags = ", ".join(sorted({t.strip().lower() for t in str(raw_tags or "").split(",") if t.strip()}))

    raw_dom = str(body.get("domain") or body.get("website") or "").strip()
    domain = ""
    website = ""
    if raw_dom:
        website = raw_dom if re.match(r"^https?://", raw_dom, re.IGNORECASE) else f"https://{raw_dom}"
        parsed = urlparse(website)
        domain = (parsed.hostname or "").removeprefix("www.").lower()
    elif "@" in email:
        mail_dom = email.split("@", 1)[1].lower()
        if mail_dom not in _FREEMAIL_DOMAINS:
            domain = mail_dom
            website = f"https://{domain}"

    state = _state()
    await state.init_db()

    dnc_hit = do_not_contact_match(email, _live_dnc_entries())
    sup_reason = await state.is_suppressed(email)
    if dnc_hit or sup_reason:
        why = dnc_hit or sup_reason
        return JSONResponse(
            {"success": False, "message": f"Cannot add: '{email}' is on Do-Not-Contact ({why})."},
            status_code=409,
        )

    existing = await state.get_prospect_by_email(email)
    if existing:
        return JSONResponse(
            {"success": False, "message": f"A contact with '{email}' already exists."},
            status_code=409,
        )

    company_id = ""
    if company_name or domain:
        company_id = await _ensure_import_company(
            state,
            name=company_name or domain,
            domain=domain,
            website=website,
            industry=industry,
            location=location,
            source="manual",
        )

    prospect = Prospect(
        company_id=company_id,
        first_name=first_name,
        last_name=last_name,
        title=title,
        company=company_name or domain,
        email=email,
        email_status=email_status,
        email_verified=(email_status == "verified"),
        linkedin_url=linkedin_url,
        phone=phone,
        source="manual",
        score=70 if (company_name or title) else 60,
        status="new",
        tags=tags,
    )
    prospect_id = await state.add_prospect(prospect)
    await state.log_action(
        "prospect_created",
        "dashboard",
        {"prospect_id": prospect_id, "email": email, "company": prospect.company, "tags": tags},
    )
    return {
        "success": True,
        "id": prospect_id,
        "prospect": {
            "id": prospect_id,
            "first_name": first_name,
            "last_name": last_name,
            "title": title,
            "company": prospect.company,
            "email": email,
            "email_status": email_status,
            "status": "new",
            "source": "manual",
            "tags": tags,
        },
    }


@app.post("/api/prospects/bulk")
async def bulk_prospects_action(request: Request):
    """Apply tag, untag, suppress, or delete across selected prospect IDs."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)

    action = str(body.get("action") or "").strip().lower()
    ids = [str(x).strip() for x in (body.get("ids") or []) if str(x).strip()]
    if action not in ("tag", "untag", "suppress", "delete"):
        return JSONResponse({"success": False, "message": "Unsupported bulk action."}, status_code=400)
    if not ids:
        return JSONResponse({"success": False, "message": "Select at least one contact first."}, status_code=400)

    state = _state()
    await state.init_db()
    count = 0

    if action in ("tag", "untag"):
        raw_tags = body.get("tags")
        tag_list = (
            [str(t).strip().lower() for t in raw_tags if str(t).strip()]
            if isinstance(raw_tags, list)
            else [t.strip().lower() for t in str(raw_tags or "").split(",") if t.strip()]
        )
        if not tag_list:
            return JSONResponse({"success": False, "message": "Enter at least one segment tag."}, status_code=400)
        count = await state.update_prospect_tags(
            ids,
            add_tags=tag_list if action == "tag" else (),
            remove_tags=tag_list if action == "untag" else (),
        )
    elif action == "suppress":
        for pid in ids:
            p = await state.get_prospect(pid)
            if p and getattr(p, "email", ""):
                await state.add_suppression("address", p.email, reason="manual", source="bulk_contacts")
                await state.suppress_matching_prospects("address", p.email, reason="manual")
                count += 1
    elif action == "delete":
        count = await state.delete_prospects(ids)

    await state.log_action("prospects_bulk", "dashboard", {"action": action, "count": count})
    return {"success": True, "action": action, "count": count}


@app.get("/api/prospects/{prospect_id}")
async def get_prospect_detail_api(prospect_id: str):
    """Return full editable contact profile, linked company, outbox emails, and conversation threads."""
    state = _state()
    await state.init_db()
    prospect = await state.get_prospect(prospect_id)
    if not prospect:
        return JSONResponse({"success": False, "message": "Contact not found."}, status_code=404)
    company_rows = (
        await query_db("SELECT * FROM companies WHERE id = ? LIMIT 1", (prospect.company_id,))
        if prospect.company_id else []
    )
    outbox_rows = await query_db(
        "SELECT id, campaign_id, step, kind, variant, subject, body, status, error, send_at, sent_at, created_at "
        "FROM outbox WHERE prospect_id = ? ORDER BY created_at DESC LIMIT 40",
        (prospect_id,),
    )
    convo_rows = await query_db(
        "SELECT id, status, stage, intent, thread_json, created_at, updated_at "
        "FROM conversations WHERE prospect_id = ? ORDER BY updated_at DESC LIMIT 20",
        (prospect_id,),
    )
    for cv in convo_rows:
        try:
            cv["messages"] = json.loads(cv.get("thread_json") or "[]")
        except Exception:
            cv["messages"] = []
    return {
        "success": True,
        "prospect": prospect.model_dump(mode="json"),
        "company": company_rows[0] if company_rows else None,
        "outbox": outbox_rows,
        "conversations": convo_rows,
    }


@app.patch("/api/prospects/{prospect_id}")
async def update_prospect_api(prospect_id: str, request: Request):
    """Update editable contact fields cleanly."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)

    state = _state()
    await state.init_db()
    existing = await state.get_prospect(prospect_id)
    if not existing:
        return JSONResponse({"success": False, "message": "Contact not found."}, status_code=404)

    new_email = str(body.get("email") or existing.email or "").strip().lower()
    if not new_email or "@" not in new_email or "." not in new_email.split("@")[-1]:
        return JSONResponse({"success": False, "message": "Please enter a valid work email address."}, status_code=400)
    if new_email != (existing.email or "").strip().lower():
        dup = await query_db(
            "SELECT id FROM prospects WHERE LOWER(email) = ? AND id != ? LIMIT 1",
            (new_email, prospect_id),
        )
        if dup:
            return JSONResponse({"success": False, "message": f"Another contact already uses {new_email}."}, status_code=409)

    email_status = str(body.get("email_status") or existing.email_status or "verified").strip().lower()
    if email_status not in ("verified", "risky", "guess", "invalid"):
        email_status = existing.email_status or "verified"

    status = str(body.get("status") or existing.status or "new").strip().lower()
    if status not in ("new", "queued", "contacted", "replied", "interested", "meeting", "lost", "opted_out"):
        status = existing.status or "new"

    await state.update_prospect(
        prospect_id,
        first_name=body.get("first_name") if "first_name" in body else existing.first_name,
        last_name=body.get("last_name") if "last_name" in body else existing.last_name,
        email=new_email,
        title=body.get("title") if "title" in body else existing.title,
        company=body.get("company") if "company" in body else existing.company,
        phone=body.get("phone") if "phone" in body else existing.phone,
        linkedin_url=body.get("linkedin_url") if "linkedin_url" in body else existing.linkedin_url,
        tags=body.get("tags") if "tags" in body else existing.tags,
        email_status=email_status,
        status=status,
    )
    await state.log_action("prospect_updated", "dashboard", {"prospect_id": prospect_id, "email": new_email})
    updated = await state.get_prospect(prospect_id)
    return {"success": True, "prospect": updated.model_dump(mode="json") if updated else None}


@app.delete("/api/prospects/{prospect_id}")
async def delete_prospect_api(prospect_id: str):
    """Delete a contact and cancel any queued outbox emails for them."""
    state = _state()
    await state.init_db()
    prospect = await state.get_prospect(prospect_id)
    if not prospect:
        return JSONResponse({"success": False, "message": "Contact not found."}, status_code=404)
    if hasattr(state, "delete_prospect"):
        deleted = await state.delete_prospect(prospect_id)
    else:
        await state.cancel_pending_outbox_for_prospect(prospect_id, reason="contact_deleted")
        deleted = False
    if not deleted:
        return JSONResponse({"success": False, "message": "Contact not found."}, status_code=404)
    await state.log_action(
        "prospect_deleted",
        "dashboard",
        {"prospect_id": prospect_id, "email": getattr(prospect, "email", "")},
    )
    return {"success": True, "id": prospect_id}


@app.get("/api/suppressions")
async def get_suppressions_api():
    """List email addresses and domains on the Do-Not-Contact suppression list."""
    state = _state()
    await state.init_db()
    items = await state.list_suppressions(limit=500)
    return {"items": items, "total": len(items)}


@app.post("/api/suppressions")
async def add_suppression_api(request: Request):
    """Add an email address or domain to the suppression list and cancel queued mail for matches."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)

    value = str(body.get("value") or "").strip()
    if not value:
        return JSONResponse({"success": False, "message": "Email address or domain is required."}, status_code=400)
    raw_kind = str(body.get("kind") or "").strip().lower()
    kind = raw_kind if raw_kind in ("address", "domain") else ("address" if "@" in value else "domain")
    reason = str(body.get("reason") or "manual").strip().lower() or "manual"

    state = _state()
    await state.init_db()
    try:
        added = await state.add_suppression(kind, value, reason=reason, source="dashboard")
        key = state.suppression_key(kind, value)
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

    matched_prospects = 0
    if hasattr(state, "suppress_matching_prospects"):
        matched_prospects = await state.suppress_matching_prospects(kind, value, reason=reason)
    await state.log_action(
        "suppression_added",
        "dashboard",
        {"kind": kind, "value": key, "reason": reason, "matched_prospects": matched_prospects},
    )
    return {
        "success": True,
        "added": bool(added),
        "kind": kind,
        "value": key,
        "matched_prospects": matched_prospects,
    }


@app.delete("/api/suppressions/{suppression_id}")
async def remove_suppression_api(suppression_id: str):
    """Remove an entry from the suppression list."""
    state = _state()
    await state.init_db()
    removed = await state.remove_suppression(suppression_id)
    if not removed:
        return JSONResponse({"success": False, "message": "Suppression entry not found."}, status_code=404)
    await state.log_action("suppression_removed", "dashboard", {"suppression_id": suppression_id})
    return {"success": True, "id": suppression_id}


async def _ensure_import_company(
    state,
    *,
    name: str,
    domain: str,
    website: str = "",
    industry: str = "",
    location: str = "",
    source: str = "csv_import",
) -> str:
    """Resolve or create a Company for an imported prospect, returning company_id."""
    if not name and not domain:
        return ""
    fn = getattr(state, "ensure_company", None)
    if callable(fn):
        res = fn(
            name=name or domain,
            domain=domain,
            website=website,
            industry=industry,
            location=location,
            source=source,
        )
        return str((await res) if inspect.isawaitable(res) else res)
    if domain:
        existing = await state.get_company_by_domain(domain)
        if existing:
            return existing.id
    elif name:
        rows = await query_db(
            "SELECT id FROM companies WHERE LOWER(name) = LOWER(?) LIMIT 1",
            (name,),
        )
        if rows:
            return str(rows[0]["id"])
    from sellbird.models.company import Company

    company = Company(
        name=name or domain,
        domain=domain,
        website=website or (f"https://{domain}" if domain else ""),
        industry=industry,
        location=location,
        source=source,
    )
    return await state.add_company(company)


@app.post("/api/prospects/import")
async def import_prospects_csv(request: Request):
    """Import prospects from CSV text with validation, suppression filtering, and deduplication."""
    import csv

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)

    csv_text = body.get("csv_text")
    if not isinstance(csv_text, str) or not csv_text.strip():
        return JSONResponse({"success": False, "message": "csv_text is required."}, status_code=400)

    cleaned_text = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(cleaned_text))
    if not reader.fieldnames:
        return JSONResponse({"success": False, "message": "CSV header row is missing."}, status_code=400)

    rows = list(reader)
    if not rows:
        return JSONResponse({"success": False, "message": "CSV contains no data rows."}, status_code=400)
    if len(rows) > 2000:
        return JSONResponse(
            {"success": False, "message": "CSV exceeds the maximum of 2,000 rows."},
            status_code=400,
        )

    from sellbird.gate import EMAIL_RE, do_not_contact_match
    from sellbird.models.prospect import Prospect

    state = _state()
    await state.init_db()

    try:
        from sellbird.config import do_not_contact_from_file, live_do_not_contact, load_config
        cfg_path = _resolve_config_file()
        try:
            cfg = load_config(str(cfg_path))
            dnc_entries = live_do_not_contact(cfg)
        except BaseException:
            dnc_entries = do_not_contact_from_file(cfg_path) if cfg_path.exists() else []
    except BaseException:
        dnc_entries = []

    def _norm_row(raw_row: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in (raw_row or {}).items():
            if k is None:
                continue
            nk = re.sub(r"[^a-z0-9]+", "_", k.strip().lower()).strip("_")
            if nk:
                out[nk] = str(v or "").strip()
        return out

    def _pick(nr: dict[str, str], *keys: str) -> str:
        for k in keys:
            val = nr.get(k, "")
            if val:
                return val
        return ""

    def _extract_domain(raw_dom: str) -> tuple[str, str]:
        val = (raw_dom or "").strip()
        if not val:
            return "", ""
        website = val if re.match(r"^https?://", val, re.IGNORECASE) else f"https://{val}"
        host = re.sub(r"^https?://", "", val, flags=re.IGNORECASE)
        host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split(":", 1)[0]
        host = host.lower().removeprefix("www.").strip(".")
        return host, website

    parsed_rows = [_norm_row(r) for r in rows]
    candidate_emails = []
    for nr in parsed_rows:
        em = _pick(nr, "email", "email_address", "e_mail").lower()
        if em and EMAIL_RE.match(em):
            candidate_emails.append(em)

    suppressed_map: dict[str, str] = {}
    if candidate_emails:
        suppressed_map = await state.suppressed_among(candidate_emails)

    existing_rows = await query_db("SELECT LOWER(email) AS e FROM prospects WHERE email != ''")
    existing_emails = {str(r["e"]).strip().lower() for r in existing_rows if r.get("e")}
    seen_in_batch: set[str] = set()
    company_cache: dict[tuple[str, str], str] = {}

    imported = 0
    skipped_duplicate = 0
    skipped_suppressed = 0
    skipped_invalid = 0

    for nr in parsed_rows:
        email = _pick(nr, "email", "email_address", "e_mail").lower()
        first_name = _pick(nr, "first_name", "firstname", "first")
        last_name = _pick(nr, "last_name", "lastname", "last")
        full_name = _pick(nr, "full_name", "name", "contact_name")
        if (not first_name or not last_name) and full_name:
            parts = full_name.split(None, 1)
            if not first_name and parts:
                first_name = parts[0]
            if not last_name and len(parts) > 1:
                last_name = parts[1]

        company_name = _pick(nr, "company", "company_name", "organization", "account")
        raw_domain = _pick(nr, "domain", "company_domain", "website", "company_website", "url")
        domain, website = _extract_domain(raw_domain)
        title = _pick(nr, "title", "role", "job_title", "position")
        linkedin_url = _pick(nr, "linkedin_url", "linkedin", "linkedin_profile")
        industry = _pick(nr, "industry", "sector")
        location = _pick(nr, "location", "city", "area", "country")
        raw_email_status = _pick(nr, "email_status", "verification_status").lower()
        email_status = "verified" if raw_email_status == "verified" else "guess"

        prospect = Prospect(
            first_name=first_name,
            last_name=last_name,
            email=email,
            email_verified=(email_status == "verified"),
            email_status=email_status,
            linkedin_url=linkedin_url,
            title=title,
            source="csv_import",
            company=company_name or domain,
            industry=industry,
        )
        if not email or not EMAIL_RE.match(email) or not prospect.is_valid():
            skipped_invalid += 1
            continue

        if do_not_contact_match(email, dnc_entries) or email in suppressed_map:
            skipped_suppressed += 1
            continue

        if email in existing_emails or email in seen_in_batch:
            skipped_duplicate += 1
            continue
        seen_in_batch.add(email)

        company_id = ""
        if domain or company_name:
            cache_key = (domain, (company_name or "").lower())
            if cache_key in company_cache:
                company_id = company_cache[cache_key]
            else:
                company_id = await _ensure_import_company(
                    state,
                    name=company_name,
                    domain=domain,
                    website=website,
                    industry=industry,
                    location=location,
                    source="csv_import",
                )
                if company_id:
                    company_cache[cache_key] = company_id
        prospect.company_id = company_id

        await state.add_prospect(prospect)
        imported += 1

    return {
        "success": True,
        "imported": imported,
        "skipped_duplicate": skipped_duplicate,
        "skipped_suppressed": skipped_suppressed,
        "skipped_invalid": skipped_invalid,
    }


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
        from sellbird.gate import MERGE_TAG_RE

        pending = await _with_context(await state.get_outbox(status="pending_review", limit=100))
        for item in pending:
            blob = f"{item.get('subject') or ''}\n{item.get('body') or ''}"
            item["unrendered_tags"] = MERGE_TAG_RE.findall(blob)

        try:
            cfg = _load_active_sellbird_config()
            sender_name = str(getattr(getattr(cfg, "persona", None), "name", "") or "").strip()
        except Exception:
            sender_name = ""

        return {
            **_compliance_view(),
            "mailbox_ready": _sellbird_can_send(),
            "sender_name": sender_name,
            "paused": await state.get_setting("sending_paused"),
            "pending": pending,
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


def _active_config_path() -> Path:
    """Resolve the active config path without copying sellbird.yaml on read."""
    resolved = _resolve_config_file()
    local = resolved.with_name("sellbird.local.yaml")
    if local.exists():
        return local
    return resolved


def _load_active_sellbird_config():
    """Load the active SellbirdConfig, honoring use_config() and _active_config_path()."""
    from sellbird.config import current_config, load_config
    cfg = current_config()
    if cfg is not None:
        return cfg
    path = _active_config_path()
    return load_config(str(path)) if path.exists() else load_config()


def _compliance_settings_payload(config) -> dict:
    """Serialize compliance, daily limits, warmup, send window, and quiet hours for Settings UI."""
    comp = getattr(config, "compliance", None)
    persona = getattr(config, "persona", None)
    email = getattr(getattr(config, "channels", None), "email", None)
    warmup = getattr(email, "warmup", None) if email is not None else None
    sw = getattr(email, "send_window", None)
    qh = getattr(getattr(config, "usage", None), "quiet_hours", None)
    qh_start = getattr(qh, "start", "22:00") if qh is not None else "22:00"
    qh_end = getattr(qh, "end", "07:00") if qh is not None else "07:00"
    qh_tz = getattr(qh, "timezone", "America/New_York") if qh is not None else "America/New_York"
    sw_enabled = sw is not None
    return {
        "postal_address": getattr(comp, "postal_address", "") if comp is not None else "",
        "opt_out_line": getattr(comp, "opt_out_line", "") if comp is not None else "",
        "from_name": getattr(persona, "name", "") if persona is not None else "",
        "daily_limit": int(getattr(email, "max_daily_sends", 50)) if email is not None else 50,
        "per_inbox_daily_cap": int(getattr(email, "per_inbox_daily_cap", 30)) if email is not None else 30,
        "send_to_risky": bool(getattr(email, "send_to_risky", False)) if email is not None else False,
        "require_approval": bool(getattr(email, "require_approval", True)) if email is not None else True,
        "warmup": {
            "start_date": getattr(warmup, "start_date", "") if warmup is not None else "",
            "min_days": int(getattr(warmup, "min_days", 14)) if warmup is not None else 14,
            "start_sends": int(getattr(warmup, "start_sends", 5)) if warmup is not None else 5,
            "daily_increase": int(getattr(warmup, "daily_increase", 3)) if warmup is not None else 3,
            "dkim_selector": getattr(warmup, "dkim_selector", "") if warmup is not None else "",
        },
        "send_window": {
            "enabled": sw_enabled,
            "start": getattr(sw, "start", "08:00") if sw is not None else "08:00",
            "end": getattr(sw, "end", "17:00") if sw is not None else "17:00",
            "days": ("mon-fri" if getattr(sw, "weekdays_only", True) else "mon-sun") if sw is not None else "mon-fri",
            "timezone": qh_tz,
        },
        "quiet_hours": {
            "enabled": bool(qh_start and qh_end and qh_start != qh_end),
            "start": qh_start,
            "end": qh_end,
            "timezone": qh_tz,
        },
    }


def _parse_send_window_days(raw_days) -> bool:
    """Map UI days string/list to SendWindowConfig.weekdays_only, raising ValueError if invalid."""
    if isinstance(raw_days, list):
        tokens = [str(d).strip().lower() for d in raw_days if str(d).strip()]
        if not tokens:
            raise ValueError("send_window.days cannot be empty.")
        valid = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        if not all(t in valid for t in tokens):
            raise ValueError("send_window.days contains an unknown day name.")
        return not any(t in ("sat", "sun") for t in tokens)
    if not isinstance(raw_days, str):
        raise ValueError("send_window.days must be text (e.g. 'mon-fri' or 'mon-sun').")
    norm = raw_days.strip().lower()
    if norm in ("", "mon-fri", "weekdays", "weekday", "mon,tue,wed,thu,fri"):
        return True
    if norm in ("mon-sun", "all", "daily", "everyday", "mon,tue,wed,thu,fri,sat,sun"):
        return False
    parts = [p.strip() for p in norm.split(",") if p.strip()]
    valid = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    if parts and all(p in valid for p in parts):
        return not any(p in ("sat", "sun") for p in parts)
    raise ValueError("send_window.days must be 'mon-fri', 'mon-sun', or comma-separated days (mon..sun).")


def _save_compliance_settings(body: dict) -> dict:
    """Validate and persist compliance + schedule fields into _offer_config_file()."""
    import pytz
    from pydantic import ValidationError
    from sellbird.config import QuietHoursConfig, SellbirdConfig, SendWindowConfig

    target = _offer_config_file()
    if not target.exists():
        raise ValueError(f"Config file {target.name} does not exist yet.")
    raw = read_user_text(target)
    doc = yaml.safe_load(raw) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{target.name} must contain a YAML mapping.")

    comp_sec = dict(doc.get("compliance") or {})
    channels_sec = dict(doc.get("channels") or {})
    email_sec = dict(channels_sec.get("email") or {})
    usage_sec = dict(doc.get("usage") or {})
    qh_sec = dict(usage_sec.get("quiet_hours") or {})

    if "postal_address" in body and body["postal_address"] is not None:
        postal = body["postal_address"]
        if not isinstance(postal, str):
            raise ValueError("postal_address must be text.")
        if "\x00" in postal or len(postal) > 500:
            raise ValueError("postal_address is invalid or too long (max 500 chars).")
        comp_sec["postal_address"] = postal.replace("\r", " ").replace("\n", " ").strip()

    if "opt_out_line" in body and body["opt_out_line"] is not None:
        opt_out = body["opt_out_line"]
        if not isinstance(opt_out, str):
            raise ValueError("opt_out_line must be text.")
        comp_sec["opt_out_line"] = opt_out.strip()

    if "daily_limit" in body and body["daily_limit"] is not None:
        raw_limit = body["daily_limit"]
        if isinstance(raw_limit, bool):
            raise ValueError("daily_limit must be an integer.")
        try:
            daily_limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("daily_limit must be an integer.") from exc
        if not 0 <= daily_limit <= 10000:
            raise ValueError("daily_limit must be between 0 and 10000.")
        email_sec["max_daily_sends"] = daily_limit

    if "require_approval" in body and body["require_approval"] is not None:
        if not isinstance(body["require_approval"], bool):
            raise ValueError("require_approval must be a boolean.")
        email_sec["require_approval"] = body["require_approval"]

    if "per_inbox_daily_cap" in body and body["per_inbox_daily_cap"] is not None:
        try:
            cap_val = int(body["per_inbox_daily_cap"])
        except (TypeError, ValueError) as exc:
            raise ValueError("per_inbox_daily_cap must be an integer.") from exc
        if not 1 <= cap_val <= 1000:
            raise ValueError("per_inbox_daily_cap must be between 1 and 1000.")
        email_sec["per_inbox_daily_cap"] = cap_val

    if "send_to_risky" in body and body["send_to_risky"] is not None:
        if not isinstance(body["send_to_risky"], bool):
            raise ValueError("send_to_risky must be a boolean.")
        email_sec["send_to_risky"] = body["send_to_risky"]

    if "from_name" in body and body["from_name"] is not None:
        fn = str(body["from_name"]).strip()[:120]
        if fn:
            persona_sec = dict(doc.get("persona") or {})
            persona_sec["name"] = fn
            doc["persona"] = persona_sec

    if "warmup" in body and isinstance(body["warmup"], dict):
        wu_in = body["warmup"]
        wu_sec = dict(email_sec.get("warmup") or {})
        if "start_date" in wu_in and wu_in["start_date"] is not None:
            wu_sec["start_date"] = str(wu_in["start_date"]).strip()[:20]
        if "min_days" in wu_in and wu_in["min_days"] is not None:
            wu_sec["min_days"] = max(0, min(90, int(wu_in["min_days"])))
        if "start_sends" in wu_in and wu_in["start_sends"] is not None:
            wu_sec["start_sends"] = max(1, min(100, int(wu_in["start_sends"])))
        if "daily_increase" in wu_in and wu_in["daily_increase"] is not None:
            wu_sec["daily_increase"] = max(1, min(50, int(wu_in["daily_increase"])))
        if "dkim_selector" in wu_in and wu_in["dkim_selector"] is not None:
            wu_sec["dkim_selector"] = str(wu_in["dkim_selector"]).strip()[:60]
        email_sec["warmup"] = wu_sec

    sw_tz_override = ""
    if "send_window" in body and body["send_window"] is not None:
        sw_in = body["send_window"]
        if not isinstance(sw_in, dict):
            raise ValueError("send_window must be an object.")
        sw_enabled = bool(sw_in.get("enabled"))
        if sw_enabled:
            prev_sw = email_sec.get("send_window") if isinstance(email_sec.get("send_window"), dict) else {}
            sw_start = str(sw_in.get("start") if sw_in.get("start") is not None else prev_sw.get("start", "08:00")).strip()
            sw_end = str(sw_in.get("end") if sw_in.get("end") is not None else prev_sw.get("end", "17:00")).strip()
            if "days" in sw_in and sw_in["days"] is not None:
                weekdays_only = _parse_send_window_days(sw_in["days"])
            else:
                weekdays_only = bool(prev_sw.get("weekdays_only", True))
            sw_tz = str(sw_in.get("timezone") or "").strip()
            if sw_tz:
                if sw_tz not in pytz.all_timezones_set:
                    raise ValueError(f"'{sw_tz}' is not a valid timezone. Use an IANA name like 'America/New_York'.")
                sw_tz_override = sw_tz
            sw_validated = SendWindowConfig(
                start=sw_start,
                end=sw_end,
                weekdays_only=weekdays_only,
                recipient_timezone=bool(prev_sw.get("recipient_timezone", True)),
            )
            email_sec["send_window"] = sw_validated.model_dump()
        else:
            email_sec["send_window"] = None

    if "quiet_hours" in body and body["quiet_hours"] is not None:
        qh_in = body["quiet_hours"]
        if not isinstance(qh_in, dict):
            raise ValueError("quiet_hours must be an object.")
        qh_enabled = bool(qh_in.get("enabled", True))
        qh_tz = str(qh_in.get("timezone") or qh_sec.get("timezone") or sw_tz_override or "America/New_York").strip()
        if not qh_enabled:
            qh_validated = QuietHoursConfig(start="00:00", end="00:00", timezone=qh_tz)
        else:
            qh_start = str(qh_in.get("start") if qh_in.get("start") is not None else qh_sec.get("start", "22:00")).strip()
            qh_end = str(qh_in.get("end") if qh_in.get("end") is not None else qh_sec.get("end", "07:00")).strip()
            if qh_start == qh_end:
                raise ValueError("quiet_hours.start and quiet_hours.end cannot be the same when enabled.")
            qh_validated = QuietHoursConfig(start=qh_start, end=qh_end, timezone=qh_tz)
        qh_sec = qh_validated.model_dump()
    elif sw_tz_override:
        qh_validated = QuietHoursConfig(
            start=str(qh_sec.get("start", "22:00")),
            end=str(qh_sec.get("end", "07:00")),
            timezone=sw_tz_override,
        )
        qh_sec = qh_validated.model_dump()

    doc["compliance"] = comp_sec
    channels_sec["email"] = email_sec
    doc["channels"] = channels_sec
    usage_sec["quiet_hours"] = qh_sec
    doc["usage"] = usage_sec

    try:
        validated = SellbirdConfig.model_validate(doc)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "Invalid configuration.")
        raise ValueError(f"{loc}: {msg}" if loc else msg) from exc

    target.with_name(target.name + ".bak").write_text(raw, encoding="utf-8")
    _write_yaml_atomic(target, doc)
    return _compliance_settings_payload(validated)


@app.get("/api/settings/compliance")
async def get_compliance_settings():
    """Return compliance, daily send limit, approval flag, send window, and quiet hours."""
    try:
        cfg = _load_active_sellbird_config()
        return _compliance_settings_payload(cfg)
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read your config: {e}"}, status_code=500)


@app.post("/api/settings/compliance")
async def save_compliance_settings_route(request: Request):
    """Validate and save compliance & sending schedule settings into active sellbird config."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not body:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    try:
        saved = _save_compliance_settings(body)
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)
    except Exception as exc:
        logger.warning("Failed to save compliance settings: %s", exc)
        return JSONResponse({"success": False, "message": f"Couldn't save: {exc}"}, status_code=500)

    state = _state()
    await state.init_db()
    await state.log_action(
        "compliance_settings_saved",
        "dashboard",
        {
            "daily_limit": saved["daily_limit"],
            "require_approval": saved["require_approval"],
            "send_window_enabled": saved["send_window"]["enabled"],
            "quiet_hours_enabled": saved["quiet_hours"]["enabled"],
        },
    )
    return {"success": True, **saved}


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


@app.post("/api/outbox/bulk")
async def outbox_bulk_action(request: Request):
    """Bulk approve or reject selected pending outbox drafts."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)
    action = str(body.get("action") or "").strip().lower()
    ids = [str(x).strip() for x in (body.get("ids") or []) if str(x).strip()]
    if action not in ("approve", "reject"):
        return JSONResponse({"success": False, "message": "Action must be approve or reject."}, status_code=400)
    if not ids:
        return JSONResponse({"success": False, "message": "Select at least one email first."}, status_code=400)

    state = _state()
    await state.init_db()
    count = 0
    for item_id in ids:
        if action == "approve":
            count += int(await state.approve_outbox(item_id) or 0)
        else:
            if await state.reject_outbox_item(item_id):
                count += 1
    await state.log_action("outbox_bulk", "dashboard", {"action": action, "count": count})
    return {"success": True, "action": action, "count": count}


@app.post("/api/outbox/test-send")
async def outbox_test_send(request: Request):
    """Render and optionally dispatch a test email for preview or live verification."""
    import html as _html
    from sellbird.config import load_env
    from sellbird.integrations.mail_provider import get_mail_provider

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}

    state = _state()
    await state.init_db()
    cfg = _load_active_sellbird_config()
    env = load_env()

    subject = str(body.get("subject") or "").strip()
    msg_body = str(body.get("body") or "").strip()
    item_id = str(body.get("item_id") or "").strip()
    campaign_id = str(body.get("campaign_id") or "").strip()
    kind = "sequence"
    if item_id:
        item = await state.get_outbox_item(item_id)
        if not item:
            return JSONResponse({"success": False, "message": "Outbox item not found."}, status_code=404)
        subject = subject or str(item.get("subject") or "")
        msg_body = msg_body or str(item.get("body") or "")
        kind = str(item.get("kind") or "sequence")
    elif campaign_id and (not subject or not msg_body):
        camp = await state.get_campaign(campaign_id)
        if not camp:
            return JSONResponse({"success": False, "message": "Campaign not found."}, status_code=404)
        seq = camp.get("sequence") or []
        first_step = seq[0] if seq and isinstance(seq[0], dict) else {}
        raw_sub = str(first_step.get("subject") or f"Quick idea for {getattr(cfg.persona, 'company', 'your team')}")
        raw_body = str(first_step.get("body") or "Hi {{first_name}},\n\nTesting our campaign outreach template for {{company}}.")
        demo_company = getattr(getattr(cfg, "persona", None), "company", "") or "Acme Dental"
        subject = subject or raw_sub.replace("{{first_name}}", "Alex").replace("{{company}}", demo_company)
        msg_body = msg_body or raw_body.replace("{{first_name}}", "Alex").replace("{{company}}", demo_company)

    if not subject or not msg_body:
        return JSONResponse({"success": False, "message": "Subject and body are required for test preview."}, status_code=400)

    to_email = str(body.get("to_email") or getattr(getattr(cfg, "persona", None), "email", "") or "").strip()
    sender_name = str(getattr(getattr(cfg, "persona", None), "name", "") or "").strip() or _send_as() or "Your Name"
    comp = getattr(cfg, "compliance", None)
    comp_parts = [
        str(getattr(comp, "opt_out_line", "") or "").strip(),
        str(getattr(comp, "postal_address", "") or "").strip(),
    ]
    footer = "\n".join(p for p in comp_parts if p) or (_compliance_view().get("footer") or "")

    rendered_lines = [msg_body]
    if kind == "sequence" and sender_name and sender_name.lower() not in msg_body.lower():
        rendered_lines.append(f"\nBest regards,\n{sender_name}")
    if kind == "sequence" and footer and not msg_body.rstrip().endswith(footer):
        rendered_lines.append(f"\n---\n{footer}")
    full_text = "\n".join(rendered_lines)
    html_paragraphs = "".join(
        f"<p style='margin:0 0 12px 0;line-height:1.55'>{_html.escape(p).replace(chr(10), '<br>')}</p>"
        for p in full_text.split("\n\n") if p.strip()
    )
    html_preview = (
        "<div style='font-family:Inter,Arial,sans-serif;font-size:14px;color:#111827;"
        "background:#ffffff;padding:20px;border-radius:8px;max-width:600px'>"
        + html_paragraphs
        + "</div>"
    )

    provider = get_mail_provider(cfg, env)
    from_addr = (
        getattr(provider, "smtp_from", "")
        or getattr(env, "smtp_from", "")
        or getattr(getattr(cfg, "persona", None), "email", "")
        or _send_as()
        or "onboarding@resend.dev"
    )
    provider_label = getattr(provider, "name", "dry_run") if provider else "unconfigured"
    if getattr(provider, "smtp_host", ""):
        provider_label = f"{provider_label} ({provider.smtp_host})"
    can_dispatch = bool(to_email and "@" in to_email and provider and provider.is_configured())
    if can_dispatch:
        try:
            res = await provider.send_email(to_email, f"[Test] {subject}", full_text)
            if res.ok:
                await state.log_action("outbox_test_sent", "dashboard", {"to_email": to_email, "provider": provider.name})
                return {
                    "success": True,
                    "sent": True,
                    "dry_run": False,
                    "provider_name": provider_label,
                    "from_email": from_addr,
                    "to_email": to_email,
                    "subject": f"[Test] {subject}",
                    "message": f"Test email sent from {from_addr} to {to_email} via {provider_label}.",
                    "preview": {"from": from_addr, "to": to_email, "subject": f"[Test] {subject}", "text": full_text, "html": html_preview},
                }
            return {
                "success": True,
                "sent": False,
                "dry_run": True,
                "provider_name": provider_label,
                "from_email": from_addr,
                "to_email": to_email,
                "subject": subject,
                "message": f"Live send returned '{res.error}' — rendered preview ready below.",
                "preview": {"from": from_addr, "to": to_email, "subject": subject, "text": full_text, "html": html_preview},
            }
        except Exception as exc:
            return {
                "success": True,
                "sent": False,
                "dry_run": True,
                "provider_name": provider_label,
                "from_email": from_addr,
                "to_email": to_email,
                "subject": subject,
                "message": f"Live send failed ({exc}) — rendered preview ready below.",
                "preview": {"from": from_addr, "to": to_email, "subject": subject, "text": full_text, "html": html_preview},
            }

    return {
        "success": True,
        "sent": False,
        "dry_run": True,
        "provider_name": provider_label,
        "from_email": from_addr,
        "to_email": to_email or "(set persona.email in Your company)",
        "subject": subject,
        "message": "No live mailbox connected yet — rendered preview ready below.",
        "preview": {"from": from_addr, "to": to_email or "(preview)", "subject": subject, "text": full_text, "html": html_preview},
    }


@app.post("/api/outbox/{item_id}/approve")
async def outbox_approve(item_id: str):
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox(item_id)
        return {"success": bool(n)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


async def _validate_outbox_draft(state, item: dict, subject: str, text: str) -> str | None:
    """Validate an outbox draft against merge-tag leaks, style, & pre-send compliance rules."""
    if not subject.strip() or not text.strip():
        return "Subject and body are both required."
    from sellbird.gate import MERGE_TAG_RE, pre_send_check
    from sellbird.email_style import style_problems

    leaks = MERGE_TAG_RE.findall(f"{subject}\n{text}")
    if leaks:
        return f"Draft still contains unrendered template placeholder(s): {', '.join(dict.fromkeys(leaks))}"

    checked = text
    footer = _compliance_view()["footer"]
    if item.get("kind") == "sequence" and footer and not text.rstrip().endswith(footer):
        checked = text.rstrip() + "\n\n" + footer  # as the Sender will send it
    gate = pre_send_check("reviewer@example.com", subject, checked)
    prospect = await state.get_prospect(item.get("prospect_id") or "")
    names = [
        n for n in (
            getattr(prospect, "company", ""),
            getattr(prospect, "first_name", ""),
            getattr(prospect, "last_name", ""),
        ) if n
    ]
    style = style_problems(
        subject,
        text,
        shape=False,
        names=names,
        kind="reply" if item.get("kind") == "reply" else "cold",
    )
    if style:
        return "The sender would hold this: the email " + "; ".join(style)
    if not gate:
        return "The sender would have blocked this: " + "; ".join(gate.reasons)
    return None


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
        err_msg = await _validate_outbox_draft(state, item, subject, text)
        if err_msg:
            return JSONResponse({"success": False, "message": err_msg}, status_code=400)
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


@app.post("/api/outbox/{item_id}/regenerate")
async def outbox_regenerate(item_id: str, request: Request):
    """Rewrite a pending outbox draft with Sellbird's Brain, preserving compliance & sign-off."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    instruction = str(payload.get("instruction") or "").strip()[:300]

    state = _state()
    await state.init_db()
    item = await state.get_outbox_item(item_id)
    if not item or item.get("status") != "pending_review":
        return JSONResponse(
            {"success": False, "message": "Only drafts awaiting review can be rewritten with AI."},
            status_code=409,
        )

    runtime, err = await _prepare_agentic_runtime()
    if err is not None:
        return err
    brain, _, config, _env = runtime
    try:
        from sellbird.agents.critic import CopyCritic, _unwrap_email_dict
        from sellbird.agents.writer import _fill_personal_merge_leaks
        from sellbird.email_style import tidy
        from sellbird.gate import MAX_SUBJECT_LEN
        from sellbird.untrusted import UNTRUSTED_RULE, fence

        prospect = await state.get_prospect(item.get("prospect_id") or "")
        skills = brain.load_skills_for_agent("writer")
        is_reply = item.get("kind") == "reply"
        sender_name = getattr(getattr(config, "persona", None), "name", "") or "the sender"
        direction = (
            instruction
            or "Tighten this draft so it sounds genuinely human, clear, and concise (under 65 words for cold email), keeping any real facts intact and avoiding generic buzzwords or em-dashes."
        )
        prospect_ctx = (
            f"Name: {getattr(prospect, 'first_name', '')} {getattr(prospect, 'last_name', '')}\n"
            f"Title: {getattr(prospect, 'title', '')}\n"
            f"Company: {getattr(prospect, 'company', '')}\n"
            f"Email: {item.get('to_email', '')}"
        ) if prospect else f"Email: {item.get('to_email', '')}"
        names = [
            n for n in (
                getattr(prospect, "company", ""),
                getattr(prospect, "first_name", ""),
                getattr(prospect, "last_name", ""),
            ) if n
        ]

        system = (
            f"You are rewriting a B2B {'reply' if is_reply else 'cold'} email on behalf of {sender_name}.\n"
            f"{UNTRUSTED_RULE}\n"
            "Rules:\n"
            "- Keep all factual claims grounded in the original draft and prospect record; never invent details.\n"
            "- Write the actual text directly; replace any {{company}} / {{first_name}} placeholders using the Prospect details and never output {{...}} tags.\n"
            "- Do NOT include a sign-off name or opt-out/postal footer; Sellbird appends those when sending.\n"
            "- Avoid banned AI cliches (delve, streamline, game-changer, synergy, reach out, quick question) and avoid em-dashes.\n"
            + ("- Preserve the thread subject verbatim.\n" if is_reply else f"- Keep subject lowercase, 2-6 words, max {MAX_SUBJECT_LEN} chars, no 'Re:' or 'Fwd:'.\n")
            + (f"\nProduct & email context:\n{skills[:2500]}" if skills else "")
        )
        full_prompt = (
            f"{system}\n\n"
            f"Rewrite instruction: {direction}\n\n"
            f"Prospect:\n{fence(prospect_ctx, 'prospect')}\n\n"
            f"Current Subject:\n{fence(str(item.get('subject') or ''), 'subject')}\n\n"
            f"Current Body:\n{fence(str(item.get('body') or ''), 'body')}\n\n"
            'Return strictly JSON: {"subject": "...", "body": "..."}'
        )
        raw = await brain.think_json(
            full_prompt,
            session_id=f"outbox-rewrite-{item_id}",
            agent="writer",
            task="rewrite_outbox_draft",
        )
        unwrapped = _unwrap_email_dict(raw)
        if not unwrapped:
            return JSONResponse(
                {"success": False, "message": "AI returned an invalid draft format. Please try again."},
                status_code=502,
            )

        new_subject = (
            str(item.get("subject") or "")
            if is_reply
            else str(unwrapped.get("subject") or item.get("subject") or "").strip()[:MAX_SUBJECT_LEN]
        )
        new_body = tidy(str(unwrapped.get("body") or "").strip())
        if prospect is not None:
            new_body = tidy(_fill_personal_merge_leaks(new_body, prospect))
            if not is_reply:
                new_subject = _fill_personal_merge_leaks(new_subject, prospect).strip()[:MAX_SUBJECT_LEN]

        err_msg = await _validate_outbox_draft(state, item, new_subject, new_body)
        if err_msg:
            critic = CopyCritic(brain, config)
            repaired = await critic.review_and_refine(
                {"subject": new_subject, "body": new_body},
                prospect_context=prospect_ctx,
                step=1,
                names=names,
            )
            if not is_reply and repaired.get("subject"):
                new_subject = str(repaired["subject"]).strip()[:MAX_SUBJECT_LEN]
            if repaired.get("body"):
                new_body = tidy(str(repaired["body"]))
            err_msg = await _validate_outbox_draft(state, item, new_subject, new_body)

        if err_msg:
            return JSONResponse({"success": False, "message": err_msg}, status_code=400)
        if not await state.edit_outbox_draft(item_id, new_subject, new_body):
            return JSONResponse(
                {"success": False, "message": "Draft status changed before rewrite could be saved."},
                status_code=409,
            )
        await state.log_action(
            "outbox_regenerated",
            "dashboard",
            {
                "outbox_id": item_id,
                "to_email": item.get("to_email", ""),
                "instruction": instruction,
            },
        )
        return {"success": True, "id": item_id, "subject": new_subject, "body": new_body}
    except Exception as exc:
        logger.warning("Outbox AI rewrite failed for %s: %s", item_id, exc)
        return JSONResponse({"success": False, "message": f"AI rewrite failed: {exc}"}, status_code=500)
    finally:
        await _close_brain(brain)


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
        ob_rows = await query_db(
            "SELECT status, COUNT(*) AS n FROM outbox WHERE campaign_id = ? AND kind = 'sequence' GROUP BY status",
            (row["id"],),
        )
        row["outbox_counts"] = {r["status"]: int(r["n"] or 0) for r in ob_rows}
        row["analytics"] = await _compute_campaign_analytics(row["id"], row)
    return rows


async def _compute_campaign_analytics(campaign_id: str, camp_row: dict | None = None) -> dict:
    """Aggregate sent/pending/failed, per-step funnel, and A/B variant outcomes for one campaign."""
    ob_rows = await query_db(
        "SELECT step, COALESCE(variant, 'A') AS variant, status, prospect_id FROM outbox "
        "WHERE campaign_id = ? AND kind = 'sequence'",
        (campaign_id,),
    )
    pids = []
    if camp_row and isinstance(camp_row.get("prospect_ids"), list):
        pids = list(camp_row["prospect_ids"])
    elif camp_row and camp_row.get("prospect_ids_json"):
        try:
            pids = json.loads(camp_row["prospect_ids_json"] or "[]")
        except Exception:
            pids = []
    else:
        pids = sorted({r["prospect_id"] for r in ob_rows if r.get("prospect_id")})

    prospect_statuses: dict[str, str] = {}
    if pids:
        ph = ",".join("?" * len(pids))
        prows = await query_db(f"SELECT id, status FROM prospects WHERE id IN ({ph})", tuple(pids))
        prospect_statuses = {r["id"]: (r.get("status") or "") for r in prows}

    totals = {"total": len(ob_rows), "pending_review": 0, "approved": 0, "sent": 0, "failed": 0}
    steps_map: dict[int, dict] = {}
    variants_map: dict[str, dict] = {
        "A": {"sent": 0, "pending": 0, "failed": 0, "replied_prospects": set()},
        "B": {"sent": 0, "pending": 0, "failed": 0, "replied_prospects": set()},
    }
    sent_prospects: set[str] = set()
    reply_states = {"replied", "interested", "meeting"}

    for r in ob_rows:
        st = r.get("status") or ""
        step_n = int(r.get("step") or 1)
        var = "B" if str(r.get("variant") or "A").upper() == "B" else "A"
        pid = r.get("prospect_id") or ""
        s_bucket = steps_map.setdefault(step_n, {"step": step_n, "sent": 0, "pending": 0, "scheduled": 0, "failed": 0})
        v_bucket = variants_map[var]

        if st == "sent":
            totals["sent"] += 1
            s_bucket["sent"] += 1
            v_bucket["sent"] += 1
            if pid:
                sent_prospects.add(pid)
                if prospect_statuses.get(pid) in reply_states:
                    v_bucket["replied_prospects"].add(pid)
        elif st == "pending_review":
            totals["pending_review"] += 1
            s_bucket["pending"] += 1
            v_bucket["pending"] += 1
        elif st in ("approved", "sending"):
            totals["approved"] += 1
            s_bucket["scheduled"] += 1
        elif st in ("failed", "cancelled", "rejected"):
            totals["failed"] += 1
            s_bucket["failed"] += 1
            v_bucket["failed"] += 1

    replied = sum(1 for st in prospect_statuses.values() if st in reply_states)
    interested = sum(1 for st in prospect_statuses.values() if st in ("interested", "meeting"))
    meetings = sum(1 for st in prospect_statuses.values() if st == "meeting")
    denom = max(len(sent_prospects), totals["sent"] > 0 and 1 or 0)
    reply_rate_pct = round((replied / denom) * 100, 1) if denom else 0.0

    variants_out = {}
    for k, vb in variants_map.items():
        v_rep = len(vb["replied_prospects"])
        variants_out[k] = {
            "sent": vb["sent"],
            "pending": vb["pending"],
            "failed": vb["failed"],
            "replies": v_rep,
            "reply_rate_pct": round((v_rep / vb["sent"]) * 100, 1) if vb["sent"] else 0.0,
        }
    winner = ""
    if variants_out["A"]["sent"] and variants_out["B"]["sent"]:
        if variants_out["A"]["reply_rate_pct"] > variants_out["B"]["reply_rate_pct"]:
            winner = "A"
        elif variants_out["B"]["reply_rate_pct"] > variants_out["A"]["reply_rate_pct"]:
            winner = "B"

    return {
        **totals,
        "prospects": len(pids),
        "replied": replied,
        "interested": interested,
        "meetings": meetings,
        "reply_rate_pct": reply_rate_pct,
        "steps": [steps_map[k] for k in sorted(steps_map)],
        "variants": variants_out,
        "winning_variant": winner,
    }


@app.get("/api/campaigns/{campaign_id}/analytics")
async def get_campaign_analytics_api(campaign_id: str):
    """Return step-level funnel and A/B variant analytics for one campaign."""
    state = _state()
    await state.init_db()
    camp = await state.get_campaign(campaign_id)
    if not camp:
        return JSONResponse({"success": False, "message": "Campaign not found."}, status_code=404)
    analytics = await _compute_campaign_analytics(campaign_id, camp)
    return {"success": True, "id": campaign_id, "name": camp.get("name") or "", "analytics": analytics}


def _normalize_sequence_steps(raw_steps) -> list:
    from sellbird.models.campaign import EmailStep
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("At least one email step is required.")
    out = []
    for idx, s in enumerate(raw_steps[:10], start=1):
        if not isinstance(s, dict):
            continue
        subj = str(s.get("subject") or "").strip()
        body = str(s.get("body") or "").strip()
        subj_b = str(s.get("subject_b") or "").strip()
        body_b = str(s.get("body_b") or "").strip()
        if not subj or not body:
            raise ValueError(f"Step {idx} requires both a subject and body.")
        try:
            delay = max(0, min(60, int(s.get("delay_days") or (0 if idx == 1 else 3))))
        except (TypeError, ValueError):
            delay = 0 if idx == 1 else 3
        out.append(EmailStep(
            step=idx,
            subject=subj,
            body=body,
            delay_days=0 if idx == 1 else delay,
            subject_b=subj_b,
            body_b=body_b,
        ))
    if not out:
        raise ValueError("At least one valid email step is required.")
    return out


@app.post("/api/campaigns/draft-steps")
async def draft_campaign_steps(request: Request):
    """Use the active AI Brain to draft a 3-step outreach sequence template for the Campaign Builder."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    name = str(body.get("name") or "").strip()[:120]
    target_tags = str(body.get("target_tags") or "").strip()[:200]
    goal = str(body.get("goal") or "").strip()[:300]

    runtime, err = await _prepare_agentic_runtime()
    if err is not None:
        return err
    brain, _, config, _env = runtime
    try:
        from sellbird.untrusted import UNTRUSTED_RULE, fence
        p = getattr(config, "product", None)
        offer_entry = str(getattr(getattr(p, "offer", None), "entry", "") or "").strip()
        skills = brain.load_skills_for_agent("writer")
        prompt = (
            f"Write a crisp 3-step B2B cold email sequence template for Sellbird.\n"
            f"{UNTRUSTED_RULE}\n"
            f"Campaign Name: {fence(name or 'Outbound Sequence', 'campaign')}\n"
            f"Target Audience/Tags: {fence(target_tags or ', '.join(getattr(getattr(config, 'icp', None), 'industries', []) or []), 'target')}\n"
            f"Product: {getattr(p, 'name', '')} — {getattr(p, 'description', '')}\n"
            f"Offer CTA: {offer_entry or 'Ask an easy question to start a conversation'}\n"
            + (f"Operator goal/note: {fence(goal, 'goal')}\n" if goal else "")
            + "\nRules:\n"
            "- Step 1 (delay_days: 0): concise 35-55 words, lowercase 2-5 word subject, clear observation + offer, may use {{first_name}} and {{company}}.\n"
            "- Step 2 (delay_days: 3): proof point / value follow-up under 45 words.\n"
            "- Step 3 (delay_days: 4): polite breakup under 30 words.\n"
            "- No sign-off names (Sellbird appends sender name) and no opt-out footer.\n"
            + (f"\nWriter context:\n{skills[:1800]}\n" if skills else "")
            + '\nReturn strictly a JSON array: [{"step": 1, "delay_days": 0, "subject": "...", "body": "..."}, ...]'
        )
        raw = await brain.think_json(prompt, session_id="campaign-builder", agent="writer", task="draft_campaign_steps")
        if isinstance(raw, dict):
            raw = raw.get("steps") or raw.get("sequence") or next((v for v in raw.values() if isinstance(v, list)), [])
        steps = _normalize_sequence_steps(raw)
        return {"success": True, "sequence": [s.model_dump() for s in steps]}
    except Exception as exc:
        return JSONResponse({"success": False, "message": f"AI sequence draft failed: {exc}"}, status_code=500)
    finally:
        await _close_brain(brain)


@app.post("/api/campaigns")
async def create_campaign_api(request: Request):
    """Create a custom multi-step campaign from the dashboard."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)

    from sellbird.models.campaign import Campaign

    name = str(body.get("name") or "").strip()[:140]
    if not name:
        return JSONResponse({"success": False, "message": "Campaign name is required."}, status_code=400)
    try:
        sequence = _normalize_sequence_steps(body.get("sequence"))
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

    raw_tags = str(body.get("target_tags") or "").strip()
    target_tags = ", ".join(sorted({t.strip().lower() for t in raw_tags.split(",") if t.strip()}))
    framework = str(body.get("framework") or "").strip()[:80]

    state = _state()
    await state.init_db()
    camp = Campaign(
        id="",
        name=name,
        channel="email",
        sequence=sequence,
        status="draft",
        prospect_ids=[],
        framework=framework,
        target_tags=target_tags,
    )
    cid = await state.add_campaign(camp)
    await state.log_action("campaign_created", "dashboard", {"campaign_id": cid, "name": name, "steps": len(sequence)})
    return {"success": True, "id": cid}


@app.patch("/api/campaigns/{campaign_id}")
async def update_campaign_api(campaign_id: str, request: Request):
    """Update a campaign's name, sequence steps, target_tags, or status."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)

    state = _state()
    await state.init_db()
    camp = await state.get_campaign(campaign_id)
    if not camp:
        return JSONResponse({"success": False, "message": "Campaign not found."}, status_code=404)

    updates: dict = {}
    if "name" in body:
        n = str(body.get("name") or "").strip()[:140]
        if not n:
            return JSONResponse({"success": False, "message": "Campaign name cannot be empty."}, status_code=400)
        updates["name"] = n
    if "target_tags" in body:
        rt = str(body.get("target_tags") or "").strip()
        updates["target_tags"] = ", ".join(sorted({t.strip().lower() for t in rt.split(",") if t.strip()}))
    if "sequence" in body:
        try:
            seq = _normalize_sequence_steps(body.get("sequence"))
        except ValueError as exc:
            return JSONResponse({"success": False, "message": str(exc)}, status_code=400)
        updates["sequence_json"] = json.dumps([s.model_dump() for s in seq])
    if "status" in body:
        st = str(body.get("status") or "").strip().lower()
        if st not in ("draft", "active", "paused", "completed"):
            return JSONResponse({"success": False, "message": "Invalid campaign status."}, status_code=400)
        updates["status"] = st

    if updates:
        await state.update_campaign(campaign_id, **updates)
        await state.log_action("campaign_updated", "dashboard", {"campaign_id": campaign_id, "fields": sorted(updates)})
    return {"success": True, "id": campaign_id}


@app.post("/api/campaigns/{campaign_id}/status")
async def set_campaign_status_api(campaign_id: str, request: Request):
    """Toggle a campaign between active, paused, completed, or draft."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = str((body or {}).get("status") or "").strip().lower()
    if status not in ("draft", "active", "paused", "completed"):
        return JSONResponse({"success": False, "message": "status must be draft, active, paused, or completed."}, status_code=400)
    state = _state()
    await state.init_db()
    camp = await state.get_campaign(campaign_id)
    if not camp:
        return JSONResponse({"success": False, "message": "Campaign not found."}, status_code=404)
    await state.update_campaign(campaign_id, status=status)
    await state.log_action("campaign_status", "dashboard", {"campaign_id": campaign_id, "status": status})
    return {"success": True, "id": campaign_id, "status": status}


@app.post("/api/campaigns/{campaign_id}/clone")
async def clone_campaign_api(campaign_id: str):
    """Clone an existing campaign sequence as a fresh draft."""
    state = _state()
    await state.init_db()
    src = await state.get_campaign(campaign_id)
    if not src:
        return JSONResponse({"success": False, "message": "Campaign not found."}, status_code=404)
    from sellbird.models.campaign import Campaign
    seq = Campaign.sequence_from_json(src.get("sequence_json"))
    clone = Campaign(
        id="",
        name=f"{src.get('name') or 'Campaign'} (Copy)"[:140],
        channel=src.get("channel") or "email",
        sequence=seq,
        status="draft",
        prospect_ids=[],
        framework=src.get("framework") or "",
        target_tags=src.get("target_tags") or "",
    )
    new_id = await state.add_campaign(clone)
    await state.log_action("campaign_cloned", "dashboard", {"source_id": campaign_id, "new_id": new_id})
    return {"success": True, "id": new_id}


@app.delete("/api/campaigns/{campaign_id}")
async def delete_campaign_api(campaign_id: str):
    """Delete a campaign and cancel its queued pending/approved sequence emails."""
    state = _state()
    await state.init_db()
    deleted = await state.delete_campaign(campaign_id)
    if not deleted:
        return JSONResponse({"success": False, "message": "Campaign not found."}, status_code=404)
    await state.log_action("campaign_deleted", "dashboard", {"campaign_id": campaign_id})
    return {"success": True, "id": campaign_id}


@app.post("/api/campaigns/{campaign_id}/launch")
async def launch_campaign_api(campaign_id: str, request: Request):
    """Stage personalized sequence emails into Outbox for matching contacts."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}

    state = _state()
    await state.init_db()
    camp = await state.get_campaign(campaign_id)
    if not camp:
        return JSONResponse({"success": False, "message": "Campaign not found."}, status_code=404)

    from datetime import timedelta, timezone
    from sellbird.agents.writer import _fill_personal_merge_leaks
    from sellbird.email_style import tidy
    from sellbird.gate import MERGE_TAG_RE, do_not_contact_match
    from sellbird.models.campaign import Campaign

    sequence = Campaign.sequence_from_json(camp.get("sequence_json"))
    if not sequence:
        return JSONResponse({"success": False, "message": "Campaign has no email steps to launch."}, status_code=400)

    explicit_ids = [str(x).strip() for x in (body.get("prospect_ids") or []) if str(x).strip()]
    target_tags_raw = str(body.get("target_tags") if "target_tags" in body else (camp.get("target_tags") or "")).strip()
    wanted_tags = {t.strip().lower() for t in target_tags_raw.split(",") if t.strip()}
    include_guess = bool(body.get("include_unverified", False))
    allowed_email_statuses = ("verified", "risky", "guess") if include_guess else ("verified", "risky")

    if explicit_ids:
        prospect_objs = [await state.get_prospect(pid) for pid in explicit_ids]
        candidates = [p for p in prospect_objs if p is not None]
    else:
        ph = ",".join("?" for _ in allowed_email_statuses)
        rows = await query_db(
            f"SELECT id FROM prospects WHERE email != '' "
            f"AND status IN ('new', 'queued') AND COALESCE(email_status, 'guess') IN ({ph}) "
            f"ORDER BY score DESC, created_at DESC LIMIT 200",
            tuple(allowed_email_statuses),
        )
        candidates = [p for p in [await state.get_prospect(r["id"]) for r in rows] if p is not None]

    if wanted_tags:
        candidates = [
            p for p in candidates
            if wanted_tags & {t.strip().lower() for t in (getattr(p, "tags", "") or "").split(",") if t.strip()}
        ]

    dnc = _live_dnc_entries()
    suppressed = await state.suppressed_among([p.email for p in candidates if p.email])
    existing_pids = set(camp.get("prospect_ids") or [])
    queued_prospects = 0
    queued_emails = 0
    skipped = 0
    variant_counts = {"A": 0, "B": 0}
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for idx, prospect in enumerate(candidates):
        email = (prospect.email or "").strip().lower()
        if not email or prospect.status not in ("new", "queued") or email in suppressed or do_not_contact_match(email, dnc):
            skipped += 1
            continue
        if (prospect.email_status or "guess").lower() not in allowed_email_statuses:
            skipped += 1
            continue

        cum_days = 0
        staged_for_person = 0
        person_variant = "B" if (idx % 2 == 1) else "A"
        for step_obj in sequence:
            cum_days += max(0, int(step_obj.delay_days or 0))
            has_b = bool((getattr(step_obj, "subject_b", "") or "").strip() or (getattr(step_obj, "body_b", "") or "").strip())
            chosen_var = person_variant if has_b else "A"
            raw_subj = (getattr(step_obj, "subject_b", "") or step_obj.subject) if chosen_var == "B" else step_obj.subject
            raw_body = (getattr(step_obj, "body_b", "") or step_obj.body) if chosen_var == "B" else step_obj.body
            subj = _fill_personal_merge_leaks(raw_subj, prospect).strip()
            msg_body = tidy(_fill_personal_merge_leaks(raw_body, prospect))
            if MERGE_TAG_RE.search(f"{subj}\n{msg_body}"):
                continue
            oid = await state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                to_email=email,
                subject=subj,
                body=msg_body,
                send_at=(now + timedelta(days=cum_days)).isoformat(),
                status="pending_review",
                step=int(step_obj.step),
                kind="sequence",
                variant=chosen_var,
            )
            if oid:
                staged_for_person += 1
                queued_emails += 1
                variant_counts[chosen_var] += 1

        if staged_for_person:
            queued_prospects += 1
            existing_pids.add(prospect.id)
            await state.queue_prospect_if_unsent(prospect.id)
        else:
            skipped += 1

    await state.update_campaign(
        campaign_id,
        status="active",
        prospect_ids_json=json.dumps(sorted(existing_pids)),
        **({"target_tags": ", ".join(sorted(wanted_tags))} if target_tags_raw or "target_tags" in body else {}),
    )
    await state.log_action(
        "campaign_launched",
        "dashboard",
        {
            "campaign_id": campaign_id,
            "queued_prospects": queued_prospects,
            "queued_emails": queued_emails,
            "skipped": skipped,
            "ab_split": variant_counts,
        },
    )
    return {
        "success": True,
        "id": campaign_id,
        "queued_prospects": queued_prospects,
        "queued_emails": queued_emails,
        "skipped": skipped,
        "ab_split": variant_counts,
    }


# ── Email Templates Library ──

@app.get("/api/templates")
async def list_templates_api():
    state = _state()
    await state.init_db()
    items = await state.list_email_templates()
    return {"items": items, "total": len(items)}


@app.post("/api/templates/seed")
async def seed_templates_api():
    state = _state()
    await state.init_db()
    added = await state.seed_starter_templates()
    items = await state.list_email_templates()
    return {"success": True, "added": added, "items": items}


@app.post("/api/templates")
async def create_template_api(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)
    name = str(body.get("name") or "").strip()[:120]
    subject = str(body.get("subject") or "").strip()[:240]
    tpl_body = str(body.get("body") or "").strip()[:8000]
    if not name or not subject or not tpl_body:
        return JSONResponse({"success": False, "message": "Template name, subject, and body are required."}, status_code=400)
    state = _state()
    await state.init_db()
    tid = await state.add_email_template(
        name=name,
        category=str(body.get("category") or "general"),
        subject=subject,
        body=tpl_body,
        tags=str(body.get("tags") or ""),
    )
    tpl = await state.get_email_template(tid)
    return {"success": True, "id": tid, "template": tpl}


@app.patch("/api/templates/{template_id}")
async def update_template_api(template_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)
    state = _state()
    await state.init_db()
    ok = await state.update_email_template(
        template_id,
        name=body.get("name"),
        category=body.get("category"),
        subject=body.get("subject"),
        body=body.get("body"),
        tags=body.get("tags"),
    )
    if not ok:
        return JSONResponse({"success": False, "message": "Template not found."}, status_code=404)
    return {"success": True, "id": template_id, "template": await state.get_email_template(template_id)}


@app.delete("/api/templates/{template_id}")
async def delete_template_api(template_id: str):
    state = _state()
    await state.init_db()
    ok = await state.delete_email_template(template_id)
    if not ok:
        return JSONResponse({"success": False, "message": "Template not found."}, status_code=404)
    return {"success": True, "id": template_id}


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


_agent_cycle_lock = asyncio.Lock()


async def _prepare_agentic_runtime():
    """Initialize DB, signal catalog, config, env, and checked Brain for an on-demand run."""
    from sellbird.brain import Brain
    from sellbird.config import ConfigError, load_env
    from sellbird.signals import seed_signal_catalog

    state = _state()
    await state.init_db()
    await seed_signal_catalog(state)
    try:
        config = _load_active_sellbird_config()
    except (ConfigError, Exception) as exc:
        return None, JSONResponse({"success": False, "message": f"Couldn't load config: {exc}"}, status_code=400)

    env = load_env()
    brain = Brain(state, config.brain)
    unavail = await brain.preflight()
    if unavail:
        await _close_brain(brain)
        return None, JSONResponse({"success": False, "message": unavail}, status_code=503)
    if not await _brain_within_budget(brain, config):
        await _close_brain(brain)
        return None, JSONResponse(
            {"success": False, "message": "Daily AI budget reached. Raise usage.max_daily_usd or try tomorrow."},
            status_code=429,
        )
    return (brain, state, config, env), None


@app.post("/api/writer/run")
async def trigger_writer_run():
    """Run the Writer agent on-demand to draft sequences for new deliverable prospects."""
    from sellbird.agents.writer import Writer

    runtime, err = await _prepare_agentic_runtime()
    if err is not None:
        return err
    brain, state, config, env = runtime
    try:
        writer = Writer(brain, state, config, env)
        await writer.run()
        campaigns_after = await _scalar("SELECT COUNT(*) FROM campaigns")
        pending_outbox = await _scalar("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review'")
        await state.log_action(
            "writer_run",
            "dashboard",
            {"campaigns_after": campaigns_after, "pending_outbox": pending_outbox},
        )
        return {
            "success": True,
            "campaigns_after": campaigns_after,
            "pending_outbox": pending_outbox,
        }
    except Exception as exc:
        logger.warning("On-demand Writer run failed: %s", exc)
        return JSONResponse({"success": False, "message": f"Writer run failed: {exc}"}, status_code=500)
    finally:
        await _close_brain(brain)


@app.post("/api/scout/run")
async def trigger_scout_run():
    """Run Scout on-demand to backfill contacts and qualify leads."""
    from sellbird.agents.scout import Scout

    runtime, err = await _prepare_agentic_runtime()
    if err is not None:
        return err
    brain, state, config, env = runtime
    try:
        before = await _scalar("SELECT COUNT(*) FROM prospects")
        scout = Scout(brain, state, config, env)
        await scout.run()
        after = await _scalar("SELECT COUNT(*) FROM prospects")
        added = max(0, int(after) - int(before))
        await state.log_action("scout_run", "dashboard", {"added": added, "prospects_after": after})
        return {"success": True, "added": added, "prospects_after": after}
    except Exception as exc:
        logger.warning("On-demand Scout run failed: %s", exc)
        return JSONResponse({"success": False, "message": f"Scout run failed: {exc}"}, status_code=500)
    finally:
        await _close_brain(brain)


_agent_cycle_status: dict = {
    "running": False,
    "current_step": "",
    "steps_done": [],
    "started_at": "",
    "finished_at": "",
    "result": None,
    "error": "",
}
_agent_cycle_task: asyncio.Task | None = None


@app.get("/api/agent/cycle/status")
async def get_agent_cycle_status():
    """Return live progress for the on-demand non-sending agentic cycle."""
    return {
        **_agent_cycle_status,
        "steps_done": list(_agent_cycle_status.get("steps_done") or []),
    }


@app.post("/api/agent/cycle")
async def trigger_agent_cycle(request: Request):
    """Run one complete non-sending agentic cycle on demand (even during quiet hours or without a mailbox).

    Runs: Scout -> DeepResearcher -> ClayEnricher (if columns exist) -> Writer (+ CopyCritic) -> Analyst.
    Pass ``{"background": true}`` to start asynchronously and poll ``/api/agent/cycle/status``.
    Drafts stage safely into Outbox and never send emails.
    """
    global _agent_cycle_task

    if _agent_cycle_lock.locked() or _agent_cycle_status.get("running"):
        return JSONResponse(
            {"success": False, "message": "An agentic cycle is already running."},
            status_code=409,
        )

    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    try:
        research_limit = max(1, min(20, int(body.get("research_limit") or 3)))
        enrich_limit = max(1, min(30, int(body.get("enrich_limit") or 5)))
    except (TypeError, ValueError):
        return JSONResponse({"success": False, "message": "Invalid limit parameter."}, status_code=400)
    include_writer = bool(body.get("include_writer", True))
    background = bool(body.get("background", False))

    runtime, err = await _prepare_agentic_runtime()
    if err is not None:
        return err

    _agent_cycle_status.update({
        "running": True,
        "current_step": "initializing",
        "steps_done": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": "",
        "result": None,
        "error": "",
    })

    async def _execute_cycle():
        brain, state, config, env = runtime
        steps_run: list[str] = []
        steps_failed: dict[str, str] = {}
        async with _agent_cycle_lock:
            try:
                prospects_before = await _scalar("SELECT COUNT(*) FROM prospects")

                _agent_cycle_status["current_step"] = "scout"
                from sellbird.agents.scout import Scout
                try:
                    await asyncio.wait_for(Scout(brain, state, config, env).run(), timeout=300.0)
                    steps_run.append("scout")
                    _agent_cycle_status["steps_done"] = list(steps_run)
                except Exception as exc:
                    steps_failed["scout"] = str(exc)[:180]

                _agent_cycle_status["current_step"] = "researcher"
                try:
                    from sellbird.agents.researcher import DeepResearcher
                    researcher = DeepResearcher(brain, state, config, env)
                    if not hasattr(researcher, "is_configured") or researcher.is_configured():
                        await asyncio.wait_for(researcher.run(limit=research_limit), timeout=180.0)
                        steps_run.append("researcher")
                        _agent_cycle_status["steps_done"] = list(steps_run)
                except Exception as exc:
                    steps_failed["researcher"] = str(exc)[:180]

                _agent_cycle_status["current_step"] = "enricher"
                try:
                    from sellbird.agents.enricher import ClayEnricher
                    cols = await state.get_enrichment_columns()
                    if cols:
                        await asyncio.wait_for(ClayEnricher(brain, state, config, env).run(limit=enrich_limit), timeout=180.0)
                        steps_run.append("enricher")
                        _agent_cycle_status["steps_done"] = list(steps_run)
                except Exception as exc:
                    steps_failed["enricher"] = str(exc)[:180]

                if include_writer:
                    _agent_cycle_status["current_step"] = "writer"
                    try:
                        from sellbird.agents.writer import Writer
                        await asyncio.wait_for(Writer(brain, state, config, env).run(), timeout=180.0)
                        steps_run.append("writer")
                        _agent_cycle_status["steps_done"] = list(steps_run)
                    except Exception as exc:
                        steps_failed["writer"] = str(exc)[:180]

                _agent_cycle_status["current_step"] = "analyst"
                try:
                    from sellbird.agents.analyst import Analyst
                    await asyncio.wait_for(Analyst(state, brain=brain, config=config).run(), timeout=60.0)
                    steps_run.append("analyst")
                    _agent_cycle_status["steps_done"] = list(steps_run)
                except Exception as exc:
                    steps_failed["analyst"] = str(exc)[:180]

                prospects_after = await _scalar("SELECT COUNT(*) FROM prospects")
                companies_total = await _scalar("SELECT COUNT(*) FROM companies")
                outbox_pending = await _scalar("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review'")
                outbox_approved = await _scalar("SELECT COUNT(*) FROM outbox WHERE status = 'approved'")
                mailbox_ready = _sellbird_can_send()

                summary_payload = {
                    "steps_run": steps_run,
                    "steps_failed": steps_failed,
                    "prospects_before": prospects_before,
                    "prospects_after": prospects_after,
                    "prospects_added": max(0, int(prospects_after) - int(prospects_before)),
                    "companies_total": companies_total,
                    "outbox_pending": outbox_pending,
                    "outbox_approved": outbox_approved,
                    "mailbox_ready": mailbox_ready,
                }
                await state.log_action("agent_cycle", "dashboard", summary_payload)
                note = (
                    "Drafts staged in Outbox cleanly (connect a mailbox in Settings when ready to send)."
                    if not mailbox_ready
                    else "Agentic cycle complete."
                )
                result = {"success": True, **summary_payload, "note": note}
                _agent_cycle_status["result"] = result
                return result
            except Exception as exc:
                _agent_cycle_status["error"] = str(exc)[:240]
                return JSONResponse({"success": False, "message": str(exc)}, status_code=500)
            finally:
                _agent_cycle_status["running"] = False
                _agent_cycle_status["current_step"] = ""
                _agent_cycle_status["finished_at"] = datetime.now(timezone.utc).isoformat()
                await _close_brain(brain)

    if background:
        _agent_cycle_task = asyncio.create_task(_execute_cycle())
        return {
            "success": True,
            "started": True,
            "status": {**_agent_cycle_status, "steps_done": []},
        }

    return await _execute_cycle()


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


@app.post("/api/conversations/{convo_id}/reply")
async def reply_to_conversation_api(convo_id: str, request: Request):
    """Queue a custom reply or AI-generated reply draft directly from Conversations into Outbox."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Request body must be an object."}, status_code=400)

    mode = str(body.get("mode") or "queue").strip().lower()
    if mode not in ("queue", "ai_draft"):
        return JSONResponse({"success": False, "message": "mode must be queue or ai_draft."}, status_code=400)

    rows = await query_db("SELECT * FROM conversations WHERE id = ?", (convo_id,))
    if not rows:
        return JSONResponse({"success": False, "message": "Conversation not found."}, status_code=404)
    convo = rows[0]

    state = _state()
    await state.init_db()
    prospect = await state.get_prospect(convo.get("prospect_id") or "")
    if not prospect or not prospect.email:
        return JSONResponse({"success": False, "message": "Prospect email is missing."}, status_code=400)
    if prospect.status == "opted_out" or await state.is_suppressed(prospect.email):
        return JSONResponse({"success": False, "message": "Prospect is opted out / suppressed."}, status_code=409)

    sent_rows = await query_db(
        "SELECT subject, thread_ref, message_id FROM outbox "
        "WHERE prospect_id = ? AND COALESCE(subject, '') != '' "
        "ORDER BY created_at DESC LIMIT 1",
        (prospect.id,),
    )
    base_subj = str((sent_rows[0].get("subject") if sent_rows else "") or "following up").strip()
    reply_subj = base_subj if base_subj.lower().startswith("re:") else f"Re: {base_subj}"
    thread_ref = str((sent_rows[0].get("thread_ref") or sent_rows[0].get("message_id")) if sent_rows else "")

    reply_text = str(body.get("body") or "").strip()
    if mode == "ai_draft":
        runtime, err = await _prepare_agentic_runtime()
        if err is not None:
            return err
        brain, _, config, _env = runtime
        try:
            from sellbird.email_style import tidy
            from sellbird.untrusted import UNTRUSTED_RULE, fence
            thread_msgs = json.loads(convo.get("thread_json") or "[]")
            thread_txt = "\n".join(
                f"{m.get('sender', 'unknown')}: {m.get('content', '')}" for m in thread_msgs[-6:]
            )
            offer_cfg = getattr(getattr(config, "product", None), "offer", None)
            booking_url = str(getattr(offer_cfg, "booking_url", "") or "").strip()
            instruction = str(body.get("instruction") or "").strip()[:300]
            prompt = (
                f"Write a helpful, concise B2B email reply for {prospect.first_name or prospect.email} ({prospect.company}).\n"
                f"{UNTRUSTED_RULE}\n"
                f"Thread:\n{fence(thread_txt or 'Customer replied.', 'thread')}\n\n"
                f"Product: {getattr(config.product, 'name', '')} — {getattr(config.product, 'description', '')}\n"
                + (f"Booking link (only include if relevant to scheduling): {booking_url}\n" if booking_url else "")
                + (f"Operator instruction: {fence(instruction, 'instruction')}\n" if instruction else "")
                + "\nRules: 2-4 plain sentences, no em-dashes, no sign-off name (Sellbird adds it).\n"
                'Return strictly JSON: {"body": "..."}'
            )
            raw = await brain.think_json(prompt, session_id=f"convo-{convo_id}", agent="handler", task="draft_custom_reply")
            reply_text = tidy(str((raw or {}).get("body") or "").strip()) if isinstance(raw, dict) else ""
        finally:
            await _close_brain(brain)

    if not reply_text:
        return JSONResponse({"success": False, "message": "Reply body is required."}, status_code=400)

    stub_item = {"kind": "reply", "prospect_id": prospect.id, "subject": reply_subj}
    err_msg = await _validate_outbox_draft(state, stub_item, reply_subj, reply_text)
    if err_msg:
        return JSONResponse({"success": False, "message": err_msg}, status_code=400)

    # Supersede any stale pending reply draft for this conversation before staging the new one.
    stale_rows = await query_db(
        "SELECT id FROM outbox WHERE conversation_id = ? AND kind = 'reply' AND status = 'pending_review'",
        (convo_id,),
    )
    for sr in stale_rows:
        await state.reject_outbox_item(sr["id"])

    from datetime import timezone
    item_id = await state.add_outbox_item(
        prospect_id=prospect.id,
        campaign_id=convo.get("campaign_id") or "",
        conversation_id=convo_id,
        to_email=prospect.email,
        subject=reply_subj,
        body=reply_text,
        send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        status="pending_review",
        kind="reply",
        step=0,
        thread_ref=thread_ref,
        in_reply_to=thread_ref,
        mailbox_id=convo.get("mailbox_id") or "",
    )
    await state.log_action("conversation_reply_drafted", "dashboard", {"conversation_id": convo_id, "outbox_id": item_id, "mode": mode})
    return {"success": True, "outbox_id": item_id, "subject": reply_subj, "body": reply_text}


def _calendar_client():
    """Build CalendarClient with .env + os.environ merged cleanly."""
    from types import SimpleNamespace
    from sellbird.config import EnvConfig, load_env
    from sellbird.integrations.calendar import CalendarClient

    try:
        cfg = _load_active_sellbird_config()
    except Exception:
        cfg = None
    try:
        env = load_env()
    except Exception:
        env = EnvConfig()

    env_vars = _read_env_file()
    hosted_custom = _in_custom_home() and current_home().hosted

    def _pick(env_key: str, current: str = "") -> str:
        val = (env_vars.get(env_key) or "").strip()
        if not val and not hosted_custom:
            val = (os.getenv(env_key) or "").strip()
        return val or str(current or "").strip()

    merged = env.model_dump()
    merged["calcom_api_key"] = _pick("CALCOM_API_KEY", merged.get("calcom_api_key", ""))
    merged["calcom_event_type_id"] = _pick("CALCOM_EVENT_TYPE_ID", merged.get("calcom_event_type_id", ""))
    merged["provider"] = _pick("CALENDAR_PROVIDER")
    merged["calcom_api_url"] = _pick("CALCOM_API_URL")
    merged["google_calendar_access_token"] = _pick("GOOGLE_CALENDAR_ACCESS_TOKEN")
    merged["google_calendar_id"] = _pick("GOOGLE_CALENDAR_ID")
    merged["m365_calendar_access_token"] = _pick("M365_CALENDAR_ACCESS_TOKEN")
    return CalendarClient(cfg, SimpleNamespace(**merged))


@app.get("/api/calendar/slots")
async def get_calendar_slots(days: int = 5):
    """Fetch open booking slots for the next N days (clamped 1..14)."""
    days = max(1, min(14, int(days)))
    cal = _calendar_client()
    if not cal.is_configured():
        return {"configured": False, "provider": "", "slots": []}
    slots = await cal.get_available_slots(f"next_{days}_days")
    return {"configured": True, "provider": cal.backend, "slots": slots}


@app.post("/api/calendar/book")
async def book_calendar_slot(request: Request):
    """Book a meeting slot for a prospect and update conversation/outbox state."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "message": "Invalid JSON body."}, status_code=400)

    conversation_id = str(body.get("conversation_id") or "").strip()
    prospect_id = str(body.get("prospect_id") or "").strip()
    prospect_email = str(body.get("prospect_email") or "").strip()
    prospect_name = str(body.get("prospect_name") or "").strip()
    slot = body.get("slot")
    notes = str(body.get("notes") or "").strip()

    if not prospect_email or "@" not in prospect_email:
        return JSONResponse({"success": False, "message": "Valid prospect_email is required."}, status_code=400)
    if not isinstance(slot, dict) or not str(slot.get("start") or "").strip():
        return JSONResponse({"success": False, "message": "slot with a valid start time is required."}, status_code=400)

    cal = _calendar_client()
    if not cal.is_configured():
        return JSONResponse(
            {"success": False, "message": "Calendar is not configured. Add Cal.com or Google Calendar credentials in Settings."},
            status_code=400,
        )

    ok = await cal.book_meeting(
        prospect_email, slot, prospect_name=prospect_name, notes=notes
    )
    if not ok:
        return JSONResponse(
            {"success": False, "message": "Calendar provider rejected the booking. Pick another slot."},
            status_code=502,
        )

    state = _state()
    await state.init_db()
    cancelled = 0
    if prospect_id:
        await state.update_prospect_status(prospect_id, "meeting")
        cancelled = await state.cancel_pending_outbox_for_prospect(
            prospect_id, reason="meeting_booked"
        )
    if conversation_id and not conversation_id.startswith("awaiting-"):
        convo_rows = await query_db("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
        if convo_rows:
            await state.update_conversation(
                conversation_id, status="meeting_booked", stage="closed_won"
            )
    await state.log_action(
        action_type="meeting_booked",
        agent="dashboard",
        details={
            "conversation_id": conversation_id,
            "prospect_id": prospect_id,
            "prospect_email": prospect_email,
            "prospect_name": prospect_name,
            "slot": slot,
            "cancelled_emails": cancelled,
        },
    )
    return {"success": True, "slot": slot, "cancelled_emails": cancelled}


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
    raw_terms = body.get("terms") or []
    if not raw_terms and isinstance(body.get("query"), str):
        raw_terms = [t.strip() for t in body["query"].replace(";", ",").split(",") if t.strip()]
    terms = [str(t).strip() for t in raw_terms if str(t).strip()] or None
    return build_queries(
        config, cities=cities,
        depth=int(body.get("depth") or 30),
        limit=int(body.get("limit") or 100),
        preset=preset,
        terms=terms,
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

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"error": f"unknown provider {provider!r}"},
                                status_code=400)

        try:
            queries = _discovery_queries(body, _load_active_sellbird_config())
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
    require_self_hosted("local_discovery")

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from sellbird.collectors.discover import PROVIDERS
        from sellbird.pipeline import run_prospecting

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"success": False,
                                 "message": f"unknown provider {provider!r}"},
                                status_code=400)

        config = _load_active_sellbird_config()
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
    require_self_hosted("local_discovery")

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

        # Live agentic runtime readiness for the Today Agentic Workbench
        brain_ready = False
        brain_provider = ""
        brain_model = ""
        try:
            doc_cfg = await _fallback_brain_doctor(False)
            ready_row = next(
                (p for p in (doc_cfg.get("providers") or []) if isinstance(p, dict) and p.get("ok")),
                None,
            )
            if ready_row:
                brain_ready = True
                brain_provider = str(ready_row.get("provider") or ready_row.get("name") or "")
                brain_model = str(ready_row.get("model") or "")
        except Exception:
            pass
        if not brain_ready:
            active_b = _active_brain()
            prov = (getattr(active_b, "provider", "") or "").strip().lower() if active_b else ""
            if prov in ("claude", "codex") and not brain.get("status") == "paused":
                brain_ready = True
                brain_provider = prov
                brain_model = str(getattr(active_b, "model", "") or "")

        qh_active = False
        qh_label = ""
        if loaded is not None:
            try:
                from sellbird.main import in_quiet_hours
                qh_active = bool(in_quiet_hours(loaded))
                qh = getattr(getattr(loaded, "usage", None), "quiet_hours", None)
                if qh is not None:
                    qh_label = f"{qh.start}-{qh.end} {qh.timezone}".strip()
            except Exception:
                pass

        agentic_info = {
            "running": _check_sellbird_pid() is not None,
            "brain_ready": brain_ready and brain.get("status") != "paused",
            "brain_provider": brain_provider,
            "brain_model": brain_model,
            "brain_paused": brain.get("status") == "paused",
            "spend_today_usd": await _spend_today_usd(),
            "max_daily_usd": _active_max_daily_usd(),
            "mailbox_ready": _sellbird_can_send(),
            "quiet_hours_active": qh_active,
            "quiet_hours_label": qh_label,
        }

        return {"items": items, "stats": stats, "agentic": agentic_info}
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
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("auth.js", "app.css", "app.js"):
        asset_path = WEB_DIR / asset
        if asset_path.is_file():
            ver = int(asset_path.stat().st_mtime_ns)
            html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={ver}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


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

_ALLOWED_BRAIN_ENV_VARS = frozenset({
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
    "CEREBRAS_API_KEY",
    "ANTHROPIC_API_KEY",
})

_LOCAL_INFERENCE_PROVIDERS = frozenset({
    "ollama", "lmstudio", "vllm", "local", "openai_compat",
})

_VALID_CHAIN_KEYS = frozenset({
    "provider", "model", "base_url", "api_key_env", "extra_headers",
    "timeout", "max_retries", "json_mode", "reasoning_effort",
    "max_output_tokens", "temperature",
})

_VALID_PROFILE_KEYS = frozenset({
    "provider", "model", "base_url", "max_output_tokens",
    "temperature", "json_mode", "reasoning_effort",
})


def _is_loopback_url(url: str) -> bool:
    """True when URL points strictly to a loopback host (127.0.0.0/8, ::1, localhost)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip.is_loopback
    except ValueError:
        return False


def _validate_brain_base_url(base_url: str, provider: str = "") -> str:
    """Reject non-HTTP(S), cloud metadata, private/link-local targets, and non-local loopback."""
    if not base_url:
        return ""
    try:
        parsed = urlparse(base_url)
    except ValueError as exc:
        raise ValueError(f"Invalid base_url: {base_url!r}") from exc
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"base_url for {provider or 'provider'!r} must start with http:// or https://.")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain embedded credentials.")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or "%" in host:
        raise ValueError("base_url must include a valid hostname.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url has an invalid port.") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("base_url port must be between 1 and 65535.")

    if host in ("metadata.google.internal", "metadata", "169.254.169.254", "fd00:ec2::254"):
        raise ValueError("Cloud metadata endpoints are not permitted in base_url.")

    is_local_prov = (provider or "").strip().lower() in _LOCAL_INFERENCE_PROVIDERS
    if host == "localhost" or host.endswith(".localhost"):
        if not is_local_prov:
            raise ValueError(f"Loopback base_url is only allowed for local inference providers, not {provider!r}.")
        return base_url

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError(f"Non-routable or metadata IP address {host!r} is not permitted in base_url.")
        if ip.is_loopback:
            if not is_local_prov:
                raise ValueError(f"Loopback base_url is only allowed for local inference providers, not {provider!r}.")
            return base_url
        if ip.is_private or not ip.is_global:
            raise ValueError(f"Private IP address {host!r} is not permitted in base_url.")

    return base_url


def _validate_api_key_env(api_key_env: str) -> str:
    """Restrict api_key_env to the explicit allowlist of known AI provider env vars."""
    if not api_key_env:
        return ""
    if not _ENV_VAR_RE.match(api_key_env) or api_key_env not in _ALLOWED_BRAIN_ENV_VARS:
        raise ValueError(
            f"api_key_env {api_key_env!r} is not in the allowed AI provider env var list."
        )
    return api_key_env


class _EnrichmentColumnCreate(BaseModel):
    model_config = {"extra": "ignore"}

    name: str = Field(..., min_length=1, max_length=120)
    key: str = Field(default="", max_length=120)
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

    base_url = _validate_brain_base_url(str(item.get("base_url") or "").strip(), provider)
    api_key_env = _validate_api_key_env(str(item.get("api_key_env") or "").strip())

    out: dict = {"provider": provider, "model": model}
    if base_url:
        out["base_url"] = base_url
    if api_key_env:
        out["api_key_env"] = api_key_env

    # Map legacy/UI aliases into harvey_brain ChainEntry keys and skip unsupported keys.
    mapped = dict(item)
    if "max_output_tokens" not in mapped and mapped.get("max_tokens") is not None:
        mapped["max_output_tokens"] = mapped["max_tokens"]
    if "timeout" not in mapped and mapped.get("timeout_s") is not None:
        mapped["timeout"] = mapped["timeout_s"]
    if "extra_headers" not in mapped and mapped.get("headers") is not None:
        mapped["extra_headers"] = mapped["headers"]

    for key in ("extra_headers", "timeout", "max_retries", "json_mode",
                "reasoning_effort", "max_output_tokens", "temperature"):
        if key in mapped and mapped[key] is not None:
            out[key] = mapped[key]
    return out


def _normalize_profile_entry(role: str, val) -> dict | bool | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return False if val is False else None
    if isinstance(val, str):
        s = val.strip()
        return {"model": s} if s else None
    if isinstance(val, list):
        for x in val:
            if isinstance(x, dict) and (str(x.get("provider") or "").strip() or str(x.get("model") or "").strip()):
                return _normalize_profile_entry(role, x)
        return None
    if isinstance(val, dict):
        source = val
        if isinstance(val.get("chain"), list):
            first = next(
                (x for x in val["chain"]
                 if isinstance(x, dict) and (str(x.get("provider") or "").strip()
                                             or str(x.get("model") or "").strip())),
                None,
            )
            if first is None:
                return None
            source = {**val, **first}

        provider = str(source.get("provider") or "").strip()
        model = str(source.get("model") or "").strip()
        if not provider and not model:
            return None
        if not model:
            raise ValueError(f"Profile {role!r} specifies a provider without a model.")
        out: dict = {"model": model}
        if provider:
            out["provider"] = provider
        base_url = _validate_brain_base_url(str(source.get("base_url") or "").strip(), provider)
        if base_url:
            out["base_url"] = base_url

        mapped = dict(source)
        if "max_output_tokens" not in mapped and mapped.get("max_tokens") is not None:
            mapped["max_output_tokens"] = mapped["max_tokens"]

        for key in ("max_output_tokens", "temperature", "json_mode", "reasoning_effort"):
            if key in mapped and mapped[key] is not None:
                out[key] = mapped[key]
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
    spend_today = await _spend_today_usd()
    return {
        "exists": path.exists(),
        "file": path.name,
        "budget": {"max_usd_per_day": round(max_usd, 2)},
        "spend": {
            "spend_today_usd": spend_today,
            "max_daily_usd": round(max_usd, 2),
        },
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

    existing = _read_brain_yaml_raw()
    existing_budget = existing.get("budget") if isinstance(existing.get("budget"), dict) else {}

    if "budget" in body or "max_usd_per_day" in body:
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
    else:
        try:
            max_usd = float(existing_budget.get("max_usd_per_day", _active_max_daily_usd()))
        except (TypeError, ValueError):
            max_usd = _active_max_daily_usd()

    if "chain" in body:
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
    else:
        existing_chain = existing.get("chain")
        if not isinstance(existing_chain, list) or not existing_chain:
            return JSONResponse({"success": False, "message": "chain must be a non-empty list of providers."},
                                status_code=400)
        try:
            chain = [_normalize_chain_entry(item) for item in existing_chain]
        except ValueError as exc:
            return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

    profiles_present = "profiles" in body
    profiles_raw = body.get("profiles") if profiles_present else existing.get("profiles")
    profiles: dict | bool = {}
    if profiles_raw is False:
        profiles = False
    elif isinstance(profiles_raw, dict):
        try:
            for role_key, role_val in profiles_raw.items():
                role = str(role_key or "").strip().lower()
                if not _ROLE_NAME_RE.match(role):
                    raise ValueError(f"Invalid role name: {role_key!r}")
                norm = _normalize_profile_entry(role, role_val)
                if norm is not None:
                    profiles[role] = norm
        except ValueError as exc:
            return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

    doc: dict = {}
    for k, v in existing.items():
        if k not in ("budget", "chain", "profiles"):
            doc[k] = v
    doc["budget"] = {**existing_budget, "max_usd_per_day": round(max_usd, 4)}
    doc["chain"] = chain
    if profiles is False:
        doc["profiles"] = False
    elif profiles:
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
        if isinstance(activate_flag, bool) or "budget" in body or "max_usd_per_day" in body:
            if _resolve_config_file().exists():
                _sync_router_in_sellbird_yaml(
                    activate_flag if isinstance(activate_flag, bool) else None,
                    max_usd=max_usd,
                )
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


def _normalize_doctor_row(p, use_network: bool):
    if hasattr(p, "model_dump"):
        p = p.model_dump()
    elif dataclasses.is_dataclass(p) and not isinstance(p, type):
        p = dataclasses.asdict(p)
    if not isinstance(p, dict):
        return p
    prov = p.get("provider") or p.get("name") or "provider"
    configured = bool(p.get("configured", p.get("key_set", False)))
    reachable = p.get("reachable")
    err = p.get("error") or ""
    if "ok" in p or "ready" in p:
        ok = bool(p.get("ok") or p.get("ready"))
    elif use_network and reachable is not None:
        ok = str(reachable).lower() == "yes"
    else:
        ok = configured
    detail = p.get("detail") or p.get("reason") or err
    if not detail:
        if use_network and str(reachable).lower() == "yes":
            detail = "Connected"
        elif configured:
            detail = "API key configured"
    return {
        **p,
        "provider": prov,
        "key_set": bool(p.get("key_set", configured)),
        "ok": ok,
        "ready": ok,
        "detail": detail,
    }


def _coerce_doctor_payload(raw_res, use_network: bool) -> dict:
    if hasattr(raw_res, "model_dump"):
        raw_res = raw_res.model_dump()
    elif dataclasses.is_dataclass(raw_res) and not isinstance(raw_res, type):
        raw_res = dataclasses.asdict(raw_res)

    if isinstance(raw_res, dict):
        providers = raw_res.get("providers") or raw_res.get("checks") or raw_res.get("chain") or []
        if isinstance(providers, list):
            providers = [_normalize_doctor_row(p, use_network) for p in providers]
        ok = raw_res.get("ok")
        if ok is None:
            ok = bool(raw_res.get("ready", bool(providers) and any(
                isinstance(p, dict) and (p.get("ok") or p.get("ready") or p.get("key_set"))
                for p in providers
            )))
        summary = raw_res.get("summary") or (
            f"{sum(1 for p in providers if isinstance(p, dict) and p.get('ok'))}/{len(providers)} provider(s) ready"
            if isinstance(providers, list) and providers else ""
        )
        return {
            **raw_res,
            "ok": bool(ok),
            "network": use_network,
            "providers": providers if isinstance(providers, list) else [],
            "summary": summary,
        }
    if isinstance(raw_res, list):
        items = [_normalize_doctor_row(p, use_network) for p in raw_res]
        any_ok = any(isinstance(p, dict) and (p.get("ok") or p.get("ready")) for p in items)
        summary = (
            f"{sum(1 for p in items if isinstance(p, dict) and p.get('ok'))}/{len(items)} provider(s) ready"
            if items else "No providers configured in brain.yaml"
        )
        return {
            "ok": any_ok,
            "network": use_network,
            "providers": items,
            "checks": items,
            "summary": summary,
        }
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
        key_present = bool(api_key) or (
            prov.lower() in _LOCAL_INFERENCE_PROVIDERS and _is_loopback_url(base_url)
        )
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
                _validate_brain_base_url(base_url, prov)
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

    role = str(body.get("role") or body.get("agent") or "writer").strip().lower()
    domain = str(body.get("domain") or "").strip()
    raw_prompt = str(body.get("prompt") or "").strip()
    if not raw_prompt:
        raw_prompt = (
            f"Verify connectivity for target domain {domain}. Reply with the single word OK."
            if domain else "Reply with the single word OK."
        )
    prompt_text = raw_prompt[:500] or "OK"

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

    try:
        if override_base:
            override_base = _validate_brain_base_url(override_base, override_prov or "openai_compat")
        if override_env:
            override_env = _validate_api_key_env(override_env)
    except ValueError as exc:
        return JSONResponse({"ok": False, "mode": "live", "error": str(exc)}, status_code=400)

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
                            if not str(text_out or "").strip():
                                last_err = (
                                    getattr(transport, "last_error", "")
                                    or getattr(transport, "paused_reason", "")
                                    or "Empty reply from provider"
                                )
                                raise RuntimeError(last_err)
                            last_usage = getattr(transport, "last_usage", None)
                            prov_name = str(getattr(transport, "last_provider", "") or "")
                            model_name = str(getattr(last_usage, "model", "") or "") if last_usage is not None else ""
                            return {
                                "ok": True,
                                "mode": "live",
                                "role": role,
                                "provider": prov_name,
                                "model": model_name,
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
    ready_entry = next(
        (
            c for c in chain
            if isinstance(c, dict) and (
                env_map.get(
                    str(c.get("api_key_env") or _PROVIDER_DEFAULTS.get(str(c.get("provider", "")).lower(), ("", ""))[1]),
                    "",
                ).strip()
                or str(c.get("base_url") or "").startswith(("http://127.0.0.1", "http://localhost"))
            )
        ),
        chain[0] if chain else {},
    )
    target_entry = {
        "provider": override_prov or ready_entry.get("provider", "openrouter"),
        "model": override_model or ready_entry.get("model", "openai/gpt-4o-mini"),
        "base_url": override_base or ready_entry.get("base_url", ""),
        "api_key_env": override_env or ready_entry.get("api_key_env", ""),
    }
    prov = str(target_entry["provider"] or "openrouter")
    def_url, def_env = _PROVIDER_DEFAULTS.get(prov.lower(), ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"))
    base_url = str(target_entry["base_url"] or def_url).rstrip("/")
    key_env = str(target_entry["api_key_env"] or def_env)
    try:
        if base_url:
            _validate_brain_base_url(base_url, prov)
        if key_env:
            _validate_api_key_env(key_env)
    except ValueError as exc:
        return JSONResponse({"ok": False, "mode": "live", "error": str(exc)}, status_code=400)
    api_key = env_map.get(key_env, "").strip() if key_env else ""
    model = str(target_entry["model"] or "").strip()

    if not base_url or not model:
        return JSONResponse(
            {"ok": False, "mode": "live", "error": "Configure a provider base_url and model first."},
            status_code=400,
        )
    if not api_key and not (_is_loopback_url(base_url) and prov.lower() in _LOCAL_INFERENCE_PROVIDERS):
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
            col_id = str(c.get("id") or "")
            col_name = str(c.get("name") or c.get("title") or "")
            col_key = str(c.get("key") or "").strip() or col_id
            fmt = str(
                c.get("output_type")
                or c.get("output_format")
                or c.get("value_type")
                or c.get("dtype")
                or "text"
            )
            norm_cols.append({
                **c,
                "id": col_id,
                "key": col_key,
                "name": col_name,
                "prompt": str(c.get("prompt") or c.get("instruction") or ""),
                "use_web_search": bool(c.get("use_web_search") or c.get("web_search")),
                "output_format": fmt,
                "output_type": fmt,
                "value_type": fmt,
            })

    values_map: dict[str, dict[str, dict]] = {}
    if isinstance(raw_vals, dict):
        for comp_id, inner in raw_vals.items():
            if isinstance(inner, dict):
                values_map[str(comp_id)] = {
                    str(cid): (
                        {**v, "source_url": str(v.get("source_url") or v.get("evidence_url") or "")}
                        if isinstance(v, dict)
                        else {"value": str(v or ""), "status": "done", "source_url": ""}
                    )
                    for cid, v in inner.items()
                }
    elif isinstance(raw_vals, list):
        for r in raw_vals:
            if not isinstance(r, dict):
                continue
            comp_id = r.get("company_id") if r.get("company_id") is not None else r.get("entity_id")
            col_ref = r.get("column_id")
            if comp_id is not None and col_ref is not None:
                values_map.setdefault(str(comp_id), {})[str(col_ref)] = {
                    "value": str(r.get("value") or r.get("result") or ""),
                    "status": str(r.get("status") or "done"),
                    "source_url": str(r.get("source_url") or r.get("evidence_url") or ""),
                    "updated_at": str(r.get("updated_at") or ""),
                }

    companies_map: dict[str, dict[str, str]] = {}
    referenced_ids = [cid for cid in values_map.keys() if cid]
    for i in range(0, len(referenced_ids), 500):
        chunk = referenced_ids[i:i + 500]
        ref_rows = await query_db(
            f"SELECT id, name, domain, industry FROM companies WHERE id IN ({_in(chunk)})",
            tuple(chunk),
        )
        for r in ref_rows:
            cid = str(r.get("id") or "")
            if cid:
                companies_map[cid] = {
                    "id": cid,
                    "name": str(r.get("name") or ""),
                    "domain": str(r.get("domain") or ""),
                    "industry": str(r.get("industry") or ""),
                }

    recent_rows = await query_db(
        "SELECT id, name, domain, industry FROM companies ORDER BY created_at DESC LIMIT 50"
    )
    for r in recent_rows:
        cid = str(r.get("id") or "")
        if cid and cid not in companies_map:
            companies_map[cid] = {
                "id": cid,
                "name": str(r.get("name") or ""),
                "domain": str(r.get("domain") or ""),
                "industry": str(r.get("industry") or ""),
            }

    return {"columns": norm_cols, "values": values_map, "companies": companies_map}


@_agentic_router.get("/api/export/enrichment.csv")
async def export_enrichment_csv():
    """Stream a formula-safe CSV of the Clay AI enrichment matrix."""
    import csv

    payload = await get_enrichment_columns()
    cols = payload.get("columns") or []
    values_map = payload.get("values") or {}
    companies_map = payload.get("companies") or {}

    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["Company", "Domain", "Industry"] + [
        _csv_cell(c.get("name") or c.get("id") or "") for c in cols
    ]
    writer.writerow(header)

    seen_cids: list[str] = list(companies_map.keys())
    for cid in values_map.keys():
        if cid not in companies_map:
            seen_cids.append(cid)

    for cid in seen_cids:
        comp = companies_map.get(cid) or {}
        comp_vals = values_map.get(cid) or {}
        row = [
            _csv_cell(comp.get("name") or cid),
            _csv_cell(comp.get("domain") or ""),
            _csv_cell(comp.get("industry") or ""),
        ]
        for col in cols:
            col_id = str(col.get("id") or "")
            col_key = str(col.get("key") or "")
            cell = comp_vals.get(col_id) or (comp_vals.get(col_key) if col_key else None) or {}
            val = cell.get("value") if isinstance(cell, dict) else cell
            row.append(_csv_cell(val or ""))
        writer.writerow(row)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="enrichment-matrix.csv"'},
    )


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

    raw_fmt = (
        body.get("output_type")
        or body.get("output_format")
        or body.get("value_type")
        or body.get("dtype")
        or "text"
    )
    raw_key = str(body.get("key") or "").strip()
    if raw_key:
        raw_key = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_key).strip("_")
    mapped = {
        "name": str(body.get("name") or body.get("title") or "").strip(),
        "key": raw_key,
        "prompt": str(body.get("prompt") or body.get("instruction") or "").strip(),
        "use_web_search": bool(body.get("use_web_search", body.get("web_search", False))),
        "output_format": str(raw_fmt).strip() or "text",
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
                elif p_name in ("output_format", "output_type", "value_type", "column_type", "dtype", "kind"):
                    kwargs[p_name] = validated.output_format
                elif p_name in ("column_id", "col_id", "key") and validated.key:
                    kwargs[p_name] = validated.key
            try:
                res = fn(**kwargs) if kwargs else fn(validated.name, validated.prompt)
                created = (await res) if inspect.isawaitable(res) else res
            except Exception as exc:
                msg = str(exc)
                if "UNIQUE" in msg.upper() or "PRIMARY KEY" in msg.upper():
                    return JSONResponse({"success": False, "message": "Column key already in use."}, status_code=400)
                raise
            if isinstance(created, dict):
                col_id = str(created.get("id") or "")
                col_key = str(created.get("key") or validated.key or col_id)
                col_obj = {
                    **created,
                    "id": col_id,
                    "key": col_key,
                    "name": validated.name,
                    "prompt": validated.prompt,
                    "use_web_search": validated.use_web_search,
                    "output_format": validated.output_format,
                    "output_type": validated.output_format,
                    "value_type": validated.output_format,
                }
            else:
                col_id = str(created)
                col_key = validated.key or col_id
                col_obj = {
                    "id": col_id,
                    "key": col_key,
                    "name": validated.name,
                    "prompt": validated.prompt,
                    "use_web_search": validated.use_web_search,
                    "output_format": validated.output_format,
                    "output_type": validated.output_format,
                    "value_type": validated.output_format,
                }
            return {"success": True, "id": col_id, "key": col_key, "column": col_obj}

    col_id = validated.key or ("col_" + uuid.uuid4().hex[:10])
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
        "key": validated.key or col_id,
        "name": validated.name,
        "prompt": validated.prompt,
        "use_web_search": validated.use_web_search,
        "output_format": validated.output_format,
        "output_type": validated.output_format,
        "value_type": validated.output_format,
    }
    return {"success": True, "id": col_id, "key": col_obj["key"], "column": col_obj}


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
        from sellbird.signals import seed_signal_catalog

        await seed_signal_catalog(state)
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

        has_target_arg = any(k in run_kwargs for k in ("domain", "company", "company_id"))
        if domain and not has_target_arg:
            if hasattr(researcher, "research_domain"):
                outcome = await researcher.research_domain(domain)
            else:
                matched = await query_db(
                    "SELECT id FROM companies WHERE LOWER(domain) = LOWER(?) LIMIT 1",
                    (domain,),
                )
                target = matched[0]["id"] if matched else {"domain": domain}
                if "companies" in run_sig.parameters:
                    outcome = await researcher.run(companies=[target], limit=1)
                elif hasattr(researcher, "research_company"):
                    outcome = await researcher.research_company(target)
                else:
                    outcome = await researcher.run(**run_kwargs)
        elif company_id and not has_target_arg:
            if hasattr(researcher, "research_company"):
                outcome = await researcher.research_company(company_id)
            elif "companies" in run_sig.parameters:
                outcome = await researcher.run(companies=[company_id], limit=1)
            else:
                outcome = await researcher.run(**run_kwargs)
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

    _attach_secret_redaction()
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
