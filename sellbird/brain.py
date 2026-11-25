"""Brain — Sellbird's thinking engine.

Runs on the operator's Claude subscription (Claude Code headless, the
default) or, when ``brain.provider: codex`` is configured, their ChatGPT
subscription through an isolated Codex runtime (see sellbird/codex.py). The
provider is fixed per Brain; a failing provider never falls back to the
other one.
"""

import asyncio
from collections.abc import Mapping
import inspect
import json
import logging
import os
import re
import time
from pathlib import Path

from sellbird.state import StateManager

logger = logging.getLogger("sellbird.brain")

from sellbird.paths import PROJECT_ROOT
from sellbird.textio import read_user_text
from sellbird.workspace_home import current_home

PROMPTS_DIR = PROJECT_ROOT / "prompts"
SKILLS_DIR = PROJECT_ROOT / "skills"

# Subprocess safety limits
DEFAULT_TIMEOUT_SECONDS = 300  # a single Claude call should never hang forever
DEFAULT_MAX_RETRIES = 2  # retries on transient failures (non-zero exit, timeout)
RETRY_BASE_DELAY = 5.0  # seconds; doubles per attempt
KILL_REAP_SECONDS = 10.0  # waiting for a killed child on shutdown

# stderr fragments that indicate retrying is pointless
_NON_RETRYABLE_PATTERNS = (
    "not logged in",
    "please run `claude login`",
    "invalid api key",
    "unauthorized",
)


PROVIDER_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "openrouter": "OpenRouter",
    "openai_compat": "OpenAI-Compatible",
}

# The provider label a transport's usage row and daily counter use. A
# transport may be backed by any per-token API (OpenRouter, xAI, Gemini,
# DeepSeek, Groq); the ledger records the fact of the spend, and the model
# column names which actual model charged it. It must exist in
# BRAIN_PROVIDERS (config + state), which `record_usage_event` validates.
TRANSPORT_USAGE_PROVIDER = "openrouter"

# Settings key holding the brain's last health transition, for `sellbird
# status` and the dashboard: {"provider", "status": "ok"|"paused", "reason",
# "since", and for pauses "kind" (plus "until", epoch seconds, for usage limits)}.
BRAIN_STATUS_KEY = "brain_status"

# Codex failures that repeat on every call until the operator acts (login,
# CLI version, model choice, an unsafe Codex config layer). After one, the
# Brain stops starting runtimes until something changes.
_PERSISTENT_CODEX_KINDS = {"setup", "auth", "isolation"}
# Re-check a paused provider at least this often even if nothing changed
# on disk (e.g. a managed config layer was fixed).
PROVIDER_RECHECK_SECONDS = 1800
# After Codex reports the plan's usage limit, stop calling it (and keep the
# budget gate closed) for this long.
USAGE_LIMIT_PAUSE_SECONDS = 3600


def parse_brain_status(raw: str) -> dict:
    """The stored BRAIN_STATUS_KEY value as a dict ({} if absent or bad)."""
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def active_brain_status(raw: str, provider: str) -> dict:
    """The stored status if it belongs to ``provider`` (the one configured
    now), else {}: a pause recorded for Codex says nothing about Claude."""
    status = parse_brain_status(raw)
    return status if status.get("provider") == provider else {}


class BrainUnavailable(Exception):
    """The provider can't be used until the operator acts. Raised only where
    a caller must leave its input untouched for a later retry."""


# Specs already reported. A typo in sellbird.yaml is logged once, not once
# per Brain construction (the dashboard builds one per request), unless the
# environment or .env file changed (e.g. an API key was just added).
_TRANSPORT_SPEC_REPORTED: set[str] = set()
_LAST_ENV_FINGERPRINT: tuple | None = None
_WATCHED_ENV_KEYS = (
    "HARVEY_BRAIN_CONFIG", "SELLBIRD_HOME",
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "XAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "GROQ_API_KEY",
    "MISTRAL_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY", "CEREBRAS_API_KEY",
)


def _transport_env_fingerprint() -> tuple:
    env_vals = tuple(os.environ.get(k, "") for k in _WATCHED_ENV_KEYS)
    dotenv_mtime = 0
    try:
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            dotenv_mtime = env_path.stat().st_mtime_ns
    except OSError:
        pass
    return env_vals + (dotenv_mtime,)


def _resolve_transport(spec: str):
    """Build the transport named by ``brain.transport_factory``, or None.

    ``spec`` is ``"pkg.module:callable"``, or ``"pkg.module"`` to call its
    ``build``. The callable returns the transport object itself (see
    ``Brain.__init__`` for the contract).

    Never raises. Every ``Brain(...)`` in the codebase (main, cli, dashboard,
    setup, trainer) constructs one unconditionally, so a typo in config must
    degrade to "no transport" — which is exactly today's behaviour — rather
    than take the whole CLI down. The reason is logged once per distinct spec
    so an operator sees it without the log filling up.
    """
    global _LAST_ENV_FINGERPRINT
    spec = (spec or "").strip()
    if not spec:
        return None
    fp = _transport_env_fingerprint()
    if _LAST_ENV_FINGERPRINT is not None and fp != _LAST_ENV_FINGERPRINT:
        _TRANSPORT_SPEC_REPORTED.clear()
    _LAST_ENV_FINGERPRINT = fp
    try:
        import importlib

        module_name, _, attr = spec.partition(":")
        target = importlib.import_module(module_name.strip())
        factory = getattr(target, (attr or "build").strip(), None)
        if factory is None:
            raise AttributeError(
                f"{module_name.strip()} has no {attr or 'build'!r}")
        transport = factory()
        if transport is None:
            raise TypeError(f"{spec} returned None")
        if not callable(transport):
            raise TypeError(f"{spec} returned {type(transport).__name__}, "
                            "which is not callable")
        _TRANSPORT_SPEC_REPORTED.discard(spec)
        return transport
    except Exception as e:
        if spec not in _TRANSPORT_SPEC_REPORTED:
            _TRANSPORT_SPEC_REPORTED.add(spec)
            logger.error(
                "brain.transport_factory %r did not load (%s: %s); using "
                "brain.provider instead. Check the dotted path.",
                spec, type(e).__name__, e)
        return None


def _read_knowledge(folder: str, name: str) -> str:
    """``<home>/<folder>/<name>.md``, or the shipped copy when the home has
    none (a home with no prompts folder, such as a hosted workspace, still
    gets Sellbird's own prompts and skills). Raises FileNotFoundError when
    neither exists."""
    own = getattr(current_home(), f"{folder}_dir") / f"{name}.md"
    try:
        return read_user_text(own)
    except FileNotFoundError:
        from sellbird.home import bundled_defaults
        shipped = bundled_defaults()
        if shipped is None or (shipped / folder / f"{name}.md") == own:
            raise
        return read_user_text(shipped / folder / f"{name}.md")


class Brain:
    def __init__(self, state: StateManager, brain_config=None, transport=None):
        """``transport``, when given, replaces the Claude and Codex CLIs: an
        ``async transport(prompt, *, agent, task, timeout, expect_json) -> str``
        that returns the model's text ("" on failure). It may carry a
        ``paused_reason`` attribute (str) to pause calls the way a usage limit
        does. This is the public seam a server running Sellbird for others
        (the hosted product's API brain) plugs into, instead of overriding
        ``think``.

        An explicit ``transport=`` always wins. Otherwise the transport named
        by ``brain.transport_factory`` is built (see ``_resolve_transport``);
        when that is unset too, Claude or Codex runs as before. The transport
        is orthogonal to ``brain.provider``: it is checked first in ``think``
        and ``preflight``, so ``provider: claude`` stays valid config on a
        box that has no Claude CLI.
        """
        from sellbird.config import BrainConfig

        self.state = state
        self.config = brain_config or BrainConfig()
        self.provider = self.config.provider
        self.model = self.config.model
        self.transport = transport or self._configured_transport()
        self.codex = None
        # Whether the last think_json got no reply at all (an outage, logout,
        # rate limit or timeout), as opposed to a reply that wasn't valid
        # JSON. Callers that count model failures must not count outages.
        self.last_reply_empty = False
        # Codex health (unused for Claude, whose behaviour is unchanged).
        self.unavailable = ""
        self._unavailable_at = 0.0
        self._unavailable_fingerprint = None
        self._usage_limited_until = 0.0
        self._usage_pause_recorded = False
        self._status_synced = False
        if self.transport is not None:
            # A transport is not a subscription: quota/usage endpoints that
            # describe a plan do not exist for it, so it never builds either
            # client. Its spend is accounted per call instead.
            self.quota = None
            self._attach_usage_recorder()
        elif self.provider == "codex":
            from sellbird.codex import CodexClient, CodexQuotaClient
            self.codex = CodexClient(
                model=self.model, reasoning_effort=self.config.reasoning_effort
            )
            self.quota = CodexQuotaClient(self.codex)
        elif self.provider in ("openrouter", "openai_compat"):
            self.quota = None
        else:
            # Lazy import avoids a cycle (quota -> usage -> state).
            from sellbird.integrations.quota import QuotaClient
            self.quota = QuotaClient()

    @property
    def is_transport_active(self) -> bool:
        return self.transport is not None

    async def aclose(self) -> None:
        """Close any underlying HTTP transport or Codex client cleanly."""
        if self.transport is not None:
            close_fn = getattr(self.transport, "aclose", None)
            if callable(close_fn):
                try:
                    res = close_fn()
                    if inspect.isawaitable(res):
                        await res
                except Exception as e:
                    logger.debug(f"Transport close failed: {e}")
        if self.codex is not None:
            close_fn = getattr(self.codex, "close", None)
            if callable(close_fn):
                try:
                    res = close_fn()
                    if inspect.isawaitable(res):
                        await res
                except Exception as e:
                    logger.debug(f"Codex close failed: {e}")

    def _configured_transport(self):
        """The transport ``brain.transport_factory`` names (or harvey_brain for openrouter), or None."""
        spec = (getattr(self.config, "transport_factory", "") or "").strip()
        if not spec and self.provider in ("openrouter", "openai_compat"):
            spec = "harvey_brain.router_factory:build"
        return _resolve_transport(spec)

    def _ensure_api_transport(self) -> bool:
        """Re-try loading the API transport if keys were added to .env after init."""
        if self.transport is not None:
            return True
        if self.provider not in ("openrouter", "openai_compat") and not (
            getattr(self.config, "transport_factory", "") or ""
        ).strip():
            return False
        loaded = self._configured_transport()
        if loaded is not None:
            self.transport = loaded
            self.quota = None
            self.unavailable = ""
            self._attach_usage_recorder()
            return True
        return False

    def _attach_usage_recorder(self):
        """Give the transport an ``on_usage=`` hook that writes a real
        ``usage_events`` row per call.

        Accepts either a positional ``Usage`` object or keyword arguments so
        both :class:`harvey_brain.router.RouterTransport` and custom keyword
        transports work seamlessly.
        """
        sink = getattr(self.transport, "on_usage", None)
        if sink is not None:
            return   # the transport brought its own recorder
        chain = getattr(self.transport, "chain", None)
        if isinstance(chain, (list, tuple)) and chain and all(
            getattr(member, "on_usage", None) is not None for member in chain
        ):
            return   # every chain member already brought its own recorder

        async def record(*args, **kwargs) -> None:
            if self.state is None:
                return
            if args and args[0] is not None and not isinstance(args[0], Mapping):
                u = args[0]
                model = str(getattr(u, "model", "") or kwargs.get("model", ""))
                input_tokens = int(getattr(u, "input_tokens", 0) or kwargs.get("input_tokens", 0) or 0)
                output_tokens = int(getattr(u, "output_tokens", 0) or kwargs.get("output_tokens", 0) or 0)
                cache_read_tokens = int(
                    getattr(u, "cache_read_tokens", 0)
                    or getattr(u, "cached_input_tokens", 0)
                    or kwargs.get("cache_read_tokens", 0)
                    or kwargs.get("cached_input_tokens", 0)
                    or 0
                )
                cost_usd = float(getattr(u, "cost_usd", 0.0) or kwargs.get("cost_usd", 0.0) or 0.0)
                agent = str(getattr(u, "agent", "") or kwargs.get("agent", ""))
                task = str(getattr(u, "task", "") or kwargs.get("task", ""))
                duration_ms = int(getattr(u, "duration_ms", 0) or kwargs.get("duration_ms", 0) or 0)
            else:
                data = dict(args[0]) if (args and isinstance(args[0], Mapping)) else kwargs
                model = str(data.get("model") or "")
                input_tokens = int(data.get("input_tokens") or 0)
                output_tokens = int(data.get("output_tokens") or 0)
                cache_read_tokens = int(
                    data.get("cache_read_tokens") or data.get("cached_input_tokens") or 0
                )
                cost_usd = float(data.get("cost_usd") or 0.0)
                agent = str(data.get("agent") or "")
                task = str(data.get("task") or "")
                duration_ms = int(data.get("duration_ms") or 0)

            provider = TRANSPORT_USAGE_PROVIDER
            try:
                await self.state.record_usage_event(
                    agent=agent, task=task, model=model,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cost_usd=cost_usd, duration_ms=duration_ms,
                    num_turns=1,
                    source="transport", provider=provider,
                )
                await self.state.increment_usage(provider)
            except Exception as e:      # accounting never breaks a call
                logger.warning(f"Could not record transport usage: {e}")

        try:
            setattr(self.transport, "on_usage", record)
        except Exception as e:          # a slotted/immutable transport
            logger.debug(f"Transport does not accept on_usage: {e}")

    @property
    def provider_label(self) -> str:
        # A transport is what actually answers, so it is what the operator
        # should see named in status lines and budget messages — even though
        # `provider` still reads claude/codex, which is valid config.
        key = TRANSPORT_USAGE_PROVIDER if self.transport is not None else self.provider
        return PROVIDER_LABELS[key]

    @property
    def paused_reason(self) -> str:
        """Why calls are currently being skipped ("" when they aren't)."""
        transport_paused = str(getattr(self.transport, "paused_reason", "") or "") \
            if self.transport is not None else ""
        if transport_paused:
            return transport_paused
        if self.unavailable:
            return self.unavailable
        if time.monotonic() < self._usage_limited_until:
            return f"{self.provider_label} usage limit reached for this plan"
        return ""

    async def preflight(self) -> str:
        """Can this Brain be used right now? Returns "" or the reason not.

        Codex only (Claude keeps its existing behaviour): runs the adapter's
        full pre-prompt check (CodexClient.preflight): CLI version, route and
        lockdown attested with no login in the runtime, the native login read
        only (expiry, renewal), then with the access-only copy the configured
        model against the catalog, the ChatGPT account, and a started
        tool-less thread. No turn, so no inference; nothing is written to the
        native login. While paused, the check is repeated only when the login
        file or the Codex executable changes, or every
        PROVIDER_RECHECK_SECONDS, so a persistent problem doesn't start
        runtimes every heartbeat.

        A transport needs no CLI probe at all: it is not Claude or Codex, so
        neither CLI's presence says anything about whether it works. It
        reports its own health through ``paused_reason``, which
        ``Brain.paused_reason`` already reads, and ``_think_transport``
        checks. Probing Claude here was what paused the whole loop for a
        transport-only install with no ``claude`` binary.
        """
        if self.transport is not None or self._ensure_api_transport():
            return ""
        if self.provider in ("openrouter", "openai_compat"):
            reason = (
                "OpenRouter / OpenAI-compatible API brain has no configured key. "
                "Set OPENROUTER_API_KEY (or OPENAI_BASE_URL) in .env."
            )
            self.unavailable = reason
            return reason
        if self.provider != "codex":
            return await self._claude_preflight()
        from sellbird.codex import CodexError

        await self._expire_usage_pause()
        if self.unavailable:
            unchanged = self.codex.fingerprint() == self._unavailable_fingerprint
            recent = time.monotonic() - self._unavailable_at < PROVIDER_RECHECK_SECONDS
            if unchanged and recent:
                return self.unavailable
        try:
            await self.codex.preflight()
        except CodexError as e:
            if e.kind in _PERSISTENT_CODEX_KINDS:
                await self._mark_unavailable(e)
                return self.unavailable
            return str(e)  # transient: skip this cycle, check again next time
        except Exception as e:
            return f"Codex preflight failed unexpectedly ({type(e).__name__})."
        if self.unavailable:
            await self._mark_available()
        elif not self._status_synced:
            logger.info(
                f"Codex ready: supported CLI, isolation verified, login and account "
                f"valid, model {self.model or 'auto'} usable with no tools (no prompt sent).")
            # A pause recorded before a restart must not outlive the problem.
            self._status_synced = True
            try:
                stored = parse_brain_status(await self.state.get_setting(BRAIN_STATUS_KEY))
            except Exception:
                stored = {}
            until = stored.get("until")
            if (stored.get("status") == "paused" and stored.get("kind") == "usage_limit"
                    and isinstance(until, (int, float)) and until > time.time()):
                # Still inside a usage-limit pause recorded before a restart.
                self._usage_limited_until = time.monotonic() + (until - time.time())
                self._usage_pause_recorded = True
            elif stored.get("status") == "paused":
                await self._mark_available()
        return ""

    async def _claude_preflight(self) -> str:
        """Claude: the installed CLI must support every isolation flag
        (claude_runtime.check_cli). Runs ``claude --help`` only: no login
        check, no model call. Returns "" or why calls are paused."""
        from sellbird.claude_runtime import ClaudeRuntimeError, check_cli

        try:
            await asyncio.to_thread(check_cli)
        except ClaudeRuntimeError as e:
            await self._mark_unavailable(e)
            return self.unavailable
        if self.unavailable:
            await self._mark_available()
        return ""

    def _fingerprint(self):
        if self.provider == "codex":
            return self.codex.fingerprint()
        from sellbird.claude_runtime import fingerprint
        return fingerprint()

    async def _start_usage_pause(self):
        self._usage_limited_until = time.monotonic() + USAGE_LIMIT_PAUSE_SECONDS
        until = time.time() + USAGE_LIMIT_PAUSE_SECONDS
        resume = time.strftime("%H:%M UTC", time.gmtime(until))
        await self._record_status(
            "paused",
            f"{self.provider_label} usage limit reached for this plan; calls resume "
            f"after {resume}.",
            kind="usage_limit", until=until)
        self._usage_pause_recorded = True

    async def _expire_usage_pause(self):
        """Record recovery once a usage-limit pause has run out."""
        if self._usage_pause_recorded and time.monotonic() >= self._usage_limited_until:
            self._usage_pause_recorded = False
            if not self.unavailable:
                logger.info(f"{self.provider_label} usage-limit pause is over; resuming.")
                await self._record_status("ok", "")

    async def _mark_unavailable(self, error):
        first = not self.unavailable or self.unavailable != str(error)
        self.unavailable = str(error)
        self._unavailable_at = time.monotonic()
        self._unavailable_fingerprint = self._fingerprint()
        if first:
            await self._record_status("paused", self.unavailable,
                                      kind=getattr(error, "kind", "setup"))

    async def _mark_available(self):
        logger.info(f"{self.provider_label} is available again; resuming.")
        self.unavailable = ""
        self._unavailable_fingerprint = None
        await self._record_status("ok", "")

    async def _record_status(self, status: str, reason: str, **extra):
        try:
            await self.state.set_setting(BRAIN_STATUS_KEY, json.dumps({
                "provider": self.provider, "status": status, "reason": reason[:500],
                "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **extra,
            }))
        except Exception as e:
            logger.warning(f"Failed to record brain status: {e}")

    async def think(
        self,
        prompt: str,
        session_id: str | None = None,
        expect_json: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        agent: str = "",
        task: str = "",
    ) -> str:
        """Send a prompt to the configured provider and return the response.

        Retries transient failures with exponential backoff and enforces a
        hard timeout so a hung CLI call can never stall the heartbeat.
        Returns "" on unrecoverable failure (callers already handle empty).

        ``agent``/``task`` label the call in the usage_events ledger so the
        dashboard can attribute tokens and cost to specific work.
        """
        if self.transport is not None or self._ensure_api_transport():
            return await self._think_transport(prompt, expect_json=expect_json,
                                               timeout=timeout, agent=agent, task=task)
        if self.provider in ("openrouter", "openai_compat"):
            logger.error(
                "OpenRouter / OpenAI-compatible call not sent: no API key configured in .env."
            )
            return ""
        if self.provider == "codex":
            return await self._think_codex(
                prompt, session_id=session_id, timeout=timeout,
                max_retries=max_retries, agent=agent, task=task,
            )

        from sellbird import claude_runtime

        # Never start an unisolated call: a CLI missing any isolation flag
        # pauses Claude (like a Codex setup problem) until it's updated.
        try:
            executable = await asyncio.to_thread(claude_runtime.check_cli)
        except claude_runtime.ClaudeRuntimeError as e:
            logger.error(f"Claude call not sent: {e}")
            await self._mark_unavailable(e)
            return ""
        if self.unavailable:
            await self._mark_available()

        # The prompt always travels on stdin: argv is visible to other local
        # users, Windows caps it at ~32k chars, and .cmd shims re-parse it.
        cmd = claude_runtime.build_command(executable, self.model, session_id)

        logger.debug(f"Brain call (session={session_id}): {prompt[:100]}...")

        last_error = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    f"Retrying brain call in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})..."
                )
                await asyncio.sleep(delay)

            process = None
            try:
                # The directory outlives the child: it's removed only after
                # the process has exited (Windows can't delete a live cwd).
                with claude_runtime.isolated_cwd() as cwd:
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        # A fresh empty directory: never the checkout (whose
                        # CLAUDE.md is the setup wizard) or its ancestors.
                        cwd=cwd,
                        env=claude_runtime.runtime_env(cwd),
                        # A pipe, never the inherited terminal.
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            process.communicate(prompt.encode("utf-8")),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            f"Claude call timed out after {timeout:.0f}s. Killing process.")
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        try:
                            await asyncio.wait_for(process.wait(), timeout=5.0)
                        except (ProcessLookupError, asyncio.TimeoutError):
                            pass
                        stdout = None
                    except BaseException:
                        # Shutting down: stop and reap the child before its
                        # directory is removed, then let the cancellation
                        # through. The reap is bounded so a stuck child can't
                        # hold shutdown; a second cancel ends it at once.
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        try:
                            await asyncio.wait_for(process.wait(),
                                                   timeout=KILL_REAP_SECONDS)
                        except Exception as e:
                            logger.warning(f"Claude child not reaped on shutdown: {e!r}")
                        raise
                if stdout is None:
                    last_error = "timeout"
                    continue  # retry

                if process.returncode != 0:
                    error = stderr.decode(errors="replace").strip()
                    logger.error(
                        f"Claude exited with code {process.returncode}: {error[:300]}"
                    )
                    last_error = error
                    if any(p in error.lower() for p in _NON_RETRYABLE_PATTERNS):
                        logger.error(
                            "Non-retryable Claude error (auth). "
                            "Run 'claude login' and restart Sellbird."
                        )
                        return ""
                    continue  # retry transient failures

                raw = stdout.decode(errors="replace").strip()
                response, payload = self._parse_result_payload(raw)

                # Usage accounting must never break the response path.
                try:
                    await self.state.increment_usage()
                except Exception as e:
                    logger.warning(f"Failed to record usage counter: {e}")
                try:
                    await self._record_usage(payload, agent=agent, task=task,
                                             label=session_id or "")
                except Exception as e:
                    logger.warning(f"Failed to record usage event: {e}")

                if payload and payload.get("is_error"):
                    # The CLI reported an API error (overloaded, rate limited)
                    # inside a successful exit. That text is not the model's
                    # answer, so no caller may use it as one: retry like any
                    # other transient failure.
                    logger.error(f"Claude reported an error: {response[:300]}")
                    last_error = response
                    continue

                logger.debug(f"Brain response: {response[:200]}...")
                return response

            except FileNotFoundError:
                logger.error(
                    "Claude CLI not found. Install it: https://code.claude.com/docs/en/setup"
                )
                return ""  # not retryable
            except asyncio.CancelledError:
                # Shutting down — kill the child so it doesn't orphan
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                raise
            except Exception as e:
                logger.error(f"Brain error: {e}")
                last_error = str(e)
                continue

        logger.error(
            f"Brain call failed after {max_retries + 1} attempts. "
            f"Last error: {str(last_error)[:200]}"
        )
        return ""

    async def _think_codex(
        self,
        prompt: str,
        session_id: str | None,
        timeout: float,
        max_retries: int,
        agent: str,
        task: str,
    ) -> str:
        """One isolated Codex turn per attempt (see sellbird/codex.py)."""
        from sellbird.codex import CodexError

        paused = self.paused_reason
        if paused:
            # Known not to work until the operator acts (or the limit
            # resets): don't start a runtime per call.
            logger.debug(f"Codex call skipped: {paused}")
            return ""
        logger.debug(f"Brain call via Codex (label={session_id})")
        last_error = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    f"Retrying Codex call in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})..."
                )
                await asyncio.sleep(delay)
            try:
                result = await self.codex.run(
                    prompt, timeout=timeout, skip_if=lambda: self.paused_reason)
            except CodexError as e:
                if e.kind == "paused":
                    logger.debug(f"Codex call skipped: {e}")
                    return ""
                last_error = str(e)
                if e.retryable:
                    logger.warning(f"Codex call failed: {e}")
                    continue
                logger.error(f"Codex call failed: {e}")
                if e.kind in _PERSISTENT_CODEX_KINDS:
                    await self._mark_unavailable(e)
                elif e.kind == "usage_limit":
                    await self._start_usage_pause()
                    invalidate = getattr(self.quota, "invalidate", None)
                    if invalidate:
                        invalidate()
                return ""
            except Exception as e:
                # Not expected (the adapter reports failures as CodexError);
                # log only the type, since messages could carry call details.
                # CancelledError is not an Exception, so shutdown propagates.
                logger.error(f"Codex call failed unexpectedly ({type(e).__name__}).")
                return ""

            try:
                await self.state.increment_usage(provider="codex")
            except Exception as e:
                logger.warning(f"Failed to record usage counter: {e}")
            try:
                await self._record_codex_usage(result, agent=agent, task=task,
                                               label=session_id or "")
            except Exception as e:
                logger.warning(f"Failed to record usage event: {e}")
            return result.text

        logger.error(
            f"Codex call failed after {max_retries + 1} attempts. "
            f"Last error: {last_error[:200]}"
        )
        return ""

    async def _record_codex_usage(self, result, agent: str, task: str, label: str):
        """One usage row per Codex call. ChatGPT plans aren't billed per
        token, so cost is recorded as 0 rather than estimated."""
        if not agent and label.startswith("sellbird-"):
            agent = label[len("sellbird-"):].split("-")[0]
        await self.state.record_usage_event(
            agent=agent,
            task=task or label,
            session_id=result.thread_id,
            model=result.model,
            duration_ms=result.duration_ms,
            num_turns=1,
            source="codex_app_server",
            provider="codex",
            **result.usage,
        )

    @staticmethod
    def _parse_result_payload(raw: str) -> tuple[str, dict | None]:
        """Split CLI JSON output into (response_text, result_payload).

        With ``--output-format json`` stdout is one JSON object whose
        ``result`` field is the model's text. If parsing fails (older CLI,
        truncated output), fall back to treating stdout as plain text so a
        formatting change can never break Sellbird's pipeline.
        """
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw, None
        if not isinstance(payload, dict) or payload.get("type") != "result":
            return raw, None
        return str(payload.get("result") or ""), payload

    async def _record_usage(
        self, payload: dict | None, agent: str, task: str, label: str
    ):
        """Write per-model usage rows from a CLI result payload."""
        if not payload:
            return

        if not agent and label.startswith("sellbird-"):
            # Callers historically pass labels like "sellbird-scout-score".
            agent = label[len("sellbird-"):].split("-")[0]

        session = str(payload.get("session_id") or "")
        duration_ms = int(payload.get("duration_ms") or 0)
        num_turns = int(payload.get("num_turns") or 0)
        is_error = bool(payload.get("is_error"))

        # modelUsage includes subagent spend; the flat `usage` field does not.
        model_usage = payload.get("modelUsage")
        rows = []
        if isinstance(model_usage, dict) and model_usage:
            for model, mu in model_usage.items():
                if not isinstance(mu, dict):
                    continue
                rows.append({
                    "model": str(model),
                    "input_tokens": int(mu.get("inputTokens") or 0),
                    "output_tokens": int(mu.get("outputTokens") or 0),
                    "cache_read_tokens": int(mu.get("cacheReadInputTokens") or 0),
                    "cache_creation_tokens": int(mu.get("cacheCreationInputTokens") or 0),
                    "cost_usd": float(mu.get("costUSD") or 0.0),
                })
        else:
            usage = payload.get("usage")
            if isinstance(usage, dict):
                rows.append({
                    "model": "",
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
                    "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                    "cost_usd": float(payload.get("total_cost_usd") or 0.0),
                })

        for row in rows:
            await self.state.record_usage_event(
                agent=agent,
                task=task or label,
                session_id=session,
                duration_ms=duration_ms,
                num_turns=num_turns,
                is_error=is_error,
                source="result_json",
                provider="claude",
                **row,
            )

    async def think_json(
        self,
        prompt: str,
        session_id: str | None = None,
        agent: str = "",
        task: str = "",
    ) -> dict | list | None:
        """Send a prompt and parse the response as JSON."""
        full_prompt = (
            prompt
            + "\n\nRespond ONLY with valid JSON. No markdown, no explanation."
        )
        # expect_json=True is what tells a transport this call needs JSON: it
        # is the only signal that reaches it, since the instruction above is
        # just prose inside the prompt. The Claude and Codex paths ignore the
        # flag (they return the CLI's text either way) and go on parsing it.
        response = await self.think(
            full_prompt, session_id=session_id, expect_json=True,
            agent=agent, task=task,
        )
        self.last_reply_empty = not response
        if not response:
            return None
        parsed = self._extract_json(response)
        if parsed is None:
            logger.error(f"Failed to parse JSON from brain: {response[:200]}")
        return parsed

    @staticmethod
    def _extract_json(text: str) -> dict | list | None:
        """Best-effort JSON extraction: strips code fences anywhere in prose,
        then falls back to scanning `{`/`[` candidates with raw_decode."""
        if not text:
            return None
        stripped = text.strip()
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, (dict, list)):
                return parsed
        except json.JSONDecodeError:
            pass

        for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", stripped, re.IGNORECASE):
            block = match.group(1).strip()
            if not block:
                continue
            try:
                parsed = json.loads(block)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except json.JSONDecodeError:
                pass

        decoder = json.JSONDecoder()
        for idx, ch in enumerate(stripped):
            if ch not in ("{", "["):
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped, idx)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except json.JSONDecodeError:
                continue
        return None

    async def check_usage(self) -> float:
        """Check today's usage on this Brain's provider (own call count).

        Returns the number of calls made today. Errors are treated as
        "over budget is unknown" and return a safe 0.0 so a broken DB read
        doesn't crash the heartbeat.
        """
        provider = (
            TRANSPORT_USAGE_PROVIDER
            if (self.transport is not None or self.provider in ("openrouter", "openai_compat"))
            else self.provider
        )
        try:
            return await self.state.get_usage_today(provider=provider)
        except Exception as e:
            logger.warning(f"Could not read usage from state: {e}")
            return 0.0

    async def is_within_budget(
        self, max_daily_calls: int = 200, max_percent: float | None = None,
        max_usd: float | None = None,
    ) -> bool:
        """Check whether Sellbird may spend more of the active subscription.

        Preferred signal: the account's REAL utilization windows (Claude's
        `/usage` numbers, or Codex's ChatGPT-plan rate limits), compared
        against max_percent so Sellbird always leaves the remainder for the
        user's own interactive work. Fallback when those are unavailable:
        Sellbird's own call counter for this provider against max_daily_calls.

        A transport has no subscription to measure, and its calls are billed
        per token, so when one is active and ``max_usd`` is given the limit
        is real money: today's recorded ``usage_events.cost_usd`` total
        against ``max_usd``. That branch comes first because a dollar cap and
        a plan-percentage cap are not comparable, and the plan one would
        otherwise read a quota client that does not exist.
        """
        await self._expire_usage_pause()
        if time.monotonic() < self._usage_limited_until:
            # Codex said the plan is out of usage: an unreadable quota must
            # not be mistaken for room to spend.
            logger.info(f"{self.paused_reason}; waiting for it to reset.")
            return False
        if self.transport is not None and max_usd is not None:
            try:
                spent = await self.state.spend_today_usd()
            except Exception as e:
                # An unreadable ledger must not be mistaken for room to
                # spend: a per-token provider would keep billing.
                logger.warning(f"Could not read today's spend ({e}); "
                               "holding the dollar budget closed.")
                return False
            within = spent < max_usd
            if not within:
                logger.info(
                    f"AI budget gate: ${spent:.2f} spent today >= "
                    f"${max_usd:.2f} limit (usage.max_daily_usd). "
                    "Spend resets at midnight."
                )
            return within
        if max_percent is not None:
            try:
                windows = await self.quota.get_utilization()
            except Exception as e:
                logger.debug(f"Quota check errored: {e}")
                windows = None
            if windows:
                worst = max(
                    (w["utilization"] for w in windows.values()), default=0.0
                )
                within = worst < max_percent
                if not within:
                    resets = ", ".join(
                        f"{name} resets {w['resets_at']}"
                        for name, w in windows.items()
                        if w.get("resets_at")
                    )
                    logger.info(
                        f"{self.provider_label} quota gate: utilization {worst:.0f}% >= "
                        f"{max_percent:.0f}% limit. {resets}"
                    )
                return within

        calls = await self.check_usage()
        return calls < max_daily_calls

    async def _think_transport(self, prompt: str, *, expect_json: bool, timeout: float,
                               agent: str, task: str) -> str:
        """One call through ``self.transport``. Never raises."""
        if self.paused_reason:
            return ""
        try:
            text = await asyncio.wait_for(
                self.transport(prompt, agent=agent, task=task, timeout=timeout,
                               expect_json=expect_json),
                timeout=timeout)
        except Exception as e:  # the transport's failure, or the timeout
            logger.warning(f"Brain transport call failed ({agent}/{task}): {type(e).__name__}: {e}")
            return ""
        return str(text or "")

    def load_prompt(self, prompt_name: str, **kwargs) -> str:
        """Load a prompt template from the prompts/ directory and fill in variables."""
        try:
            template = _read_knowledge("prompts", prompt_name)
        except FileNotFoundError:
            logger.warning(f"Prompt file not found: {prompt_name}.md")
            return ""
        except OSError as e:
            logger.error(f"Could not read prompt file {prompt_name}.md: {e}")
            return ""
        # Check unfilled variables against the template BEFORE substituting
        # untrusted values, and substitute in one single pass so {{other_var}}
        # inside scraped text is never cross-expanded.
        template_vars = set(re.findall(r"\{\{(\w+)\}\}", template))
        leftover = sorted(template_vars - set(kwargs.keys()))
        if leftover:
            logger.warning(
                f"Prompt '{prompt_name}' has unfilled variables: {leftover}"
            )
        if kwargs:
            template = re.sub(
                r"\{\{(\w+)\}\}",
                lambda m: str(kwargs[m.group(1)]) if m.group(1) in kwargs else m.group(0),
                template,
            )
        return template

    def load_skill(self, skill_name: str) -> str:
        """Load a skill knowledge file from the skills/ directory."""
        try:
            return _read_knowledge("skills", skill_name)
        except FileNotFoundError:
            logger.warning(f"Skill file not found: {skill_name}.md")
            return ""
        except OSError as e:
            logger.error(f"Could not read skill file {skill_name}.md: {e}")
            return ""

    def load_skills_for_agent(self, agent_name: str) -> str:
        """Load all relevant skills for a specific sub-agent.

        Returns concatenated skill content that should be injected
        into the agent's prompts as foundational knowledge.
        """
        # product_knowledge and competitive_intel are auto-generated by the trainer
        # and loaded by every agent that needs product context
        skill_map = {
            "scout": ["prospecting_tactics", "lead_qualification", "account_navigation", "signal_playbook", "product_knowledge"],
            "researcher": ["prospecting_tactics", "signal_playbook", "product_knowledge"],
            "enricher": ["lead_qualification", "product_knowledge"],
            # The writer gets a short prompt (prompts/writer.md) and only the part of
            # signal_playbook that fits each prospect (Writer._signal_guide): long
            # playbooks bury the few rules that matter for a 40-word email.
            "writer": ["product_knowledge", "competitive_intel"],
            "critic": ["email_frameworks", "product_knowledge"],
            "handler": ["objection_handling", "sales_methodology", "offer_strategy", "product_knowledge", "competitive_intel"],
            "sender": ["email_frameworks", "product_knowledge"],
            "linkedin": ["linkedin_outreach", "prospecting_tactics", "product_knowledge"],
        }

        skill_names = skill_map.get(agent_name, [])
        if not skill_names:
            return ""

        sections = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                sections.append(content)

        if not sections:
            return ""

        return (
            "\n\n---\n## FOUNDATIONAL KNOWLEDGE\n"
            "Use the following frameworks and best practices to guide your work:\n\n"
            + "\n\n---\n\n".join(sections)
        )
