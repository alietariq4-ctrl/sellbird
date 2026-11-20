"""Delegate transports: keep Claude and Codex by using the host's own runtimes.

The host already ships two isolated, tool-less runtimes:

* ``sellbird.claude_runtime.build_command`` — argv for one print-mode Claude
  call (``--tools ""``, ``--strict-mcp-config``, ``--safe-mode``), prompt on
  stdin, JSON result on stdout.
* ``sellbird.codex.CodexClient.run`` — one isolated Codex turn.

Reusing them means the "no tools, prompts carry untrusted text" guarantee in
``claude_runtime.build_command`` still holds. Nothing here re-implements either
runtime, and nothing is imported until a call is made, so this package works
without the host installed.

When the host is not importable the transport reports itself unavailable
(``paused_reason`` set, ``""`` returned) instead of raising.
"""

from __future__ import annotations

import asyncio
import json
import logging
from time import monotonic
from typing import Any

from .base import DEFAULT_TIMEOUT, Usage, as_int, extract_text

logger = logging.getLogger(__name__)

HOST_IMPORT_HINT = (
    "host not available: install the Sellbird package (pip install -e .) or put "
    "its checkout on sys.path to use claude_cli/codex_cli"
)


def _host():
    """Import the host package, or raise ImportError with a clear message."""
    try:
        import sellbird  # noqa: F401
    except Exception as exc:  # ImportError, or a host dependency missing
        raise ImportError(f"{HOST_IMPORT_HINT} ({type(exc).__name__})") from exc
    return sellbird


class DelegateTransport:
    """Calls the host's Claude CLI or Codex runtime. Never raises."""

    def __init__(self, kind: str, *, model: str = "", on_usage=None,
                 timeout: float = DEFAULT_TIMEOUT, cwd: str | None = None,
                 reasoning_effort: str | None = None):
        if kind not in ("claude_cli", "codex_cli"):
            raise ValueError(f"unknown delegate kind {kind!r}")
        self.name = kind
        self.kind = kind
        self.model = model
        self.on_usage = on_usage
        self.default_timeout = float(timeout)
        self.cwd = cwd
        self.reasoning_effort = reasoning_effort
        self.paused_reason = ""
        self.last_error = ""
        self.last_usage: Usage | None = None
        self._codex = None      # lazily built; CodexClient holds a lock
        self._checked = False

    # ── availability ──────────────────────────────────────────────────────

    def _check_host(self) -> str:
        """\"\" when the runtime is usable, else a short pause sentence."""
        try:
            _host()
        except ImportError as exc:
            return str(exc)
        if self.kind == "claude_cli":
            try:
                from sellbird import claude_runtime
                claude_runtime.check_cli()
            except Exception as exc:
                return f"claude_cli unavailable: {exc}"
            return ""
        try:
            from sellbird import codex
            if not (codex.find_codex() or getattr(codex, "codex_path", None)):
                return "codex_cli unavailable: the codex executable was not found"
            codex.require_supported_platform()
        except Exception as exc:
            return f"codex_cli unavailable: {exc}"
        return ""

    # ── usage ─────────────────────────────────────────────────────────────

    async def _fire_usage(self, usage: Usage) -> None:
        if self.on_usage is None:
            return
        try:
            try:
                result = self.on_usage(usage)
            except TypeError:
                from dataclasses import asdict
                import inspect
                result = self.on_usage(**asdict(usage))
                if inspect.isawaitable(result):
                    await result
                return
            import inspect
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("on_usage callback failed: %s", type(exc).__name__)

    def _claude_usage(self, payload: dict[str, Any], agent: str, task: str,
                      duration_ms: int) -> Usage:
        """The CLI's JSON result carries exact per-model cost."""
        model_usage = payload.get("modelUsage")
        input_tokens = output_tokens = cached = 0
        cost = 0.0
        model = self.model
        if isinstance(model_usage, dict) and model_usage:
            for name, mu in model_usage.items():
                if not isinstance(mu, dict):
                    continue
                input_tokens += as_int(mu.get("inputTokens"))
                output_tokens += as_int(mu.get("outputTokens"))
                cached += as_int(mu.get("cacheReadInputTokens"))
                cost += float(mu.get("costUSD") or 0.0)
                model = model or str(name)
        else:
            usage = payload.get("usage")
            if isinstance(usage, dict):
                input_tokens = as_int(usage.get("input_tokens"))
                output_tokens = as_int(usage.get("output_tokens"))
                cached = as_int(usage.get("cache_read_input_tokens"))
            cost = float(payload.get("total_cost_usd") or 0.0)
        return Usage(
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=round(cost, 6),
            model=model,
            provider="claude_cli",
            duration_ms=duration_ms,
            agent=agent,
            task=task,
        )

    # ── calls ─────────────────────────────────────────────────────────────

    async def __call__(self, prompt: str, *, agent: str = "", task: str = "",
                       timeout: float = DEFAULT_TIMEOUT,
                       expect_json: bool = False) -> str:
        self.last_usage = None
        if not self._checked:
            self.paused_reason = self._check_host()
            self._checked = True
        if self.paused_reason:
            return ""
        budget = float(timeout if timeout is not None else self.default_timeout)
        started = monotonic()
        try:
            if self.kind == "claude_cli":
                return await self._call_claude(prompt, agent, task, budget, started)
            return await self._call_codex(prompt, agent, task, budget, started)
        except asyncio.TimeoutError:
            logger.warning("%s call timed out after %.0fs", self.name, budget)
            self.last_error = "timeout"
            return ""
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("%s call failed: %s", self.name, self.last_error)
            return ""

    async def _call_claude(self, prompt: str, agent: str, task: str,
                           budget: float, started: float) -> str:
        from sellbird import claude_runtime

        exe = claude_runtime.check_cli()
        argv = claude_runtime.build_command(exe, model=self.model)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd or None,
            env=claude_runtime.runtime_env(self.cwd or "."),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=budget)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        raw = stdout.decode(errors="replace").strip()
        if proc.returncode != 0:
            note = " ".join(stderr.decode(errors="replace").split())[:200]
            logger.warning("claude_cli exited %s: %s", proc.returncode, note)
            self.last_error = f"exit {proc.returncode}: {note}"
            return ""
        text, payload = self._parse_claude(raw)
        if payload is not None:
            usage = self._claude_usage(
                payload, agent, task, int((monotonic() - started) * 1000))
            self.last_usage = usage
            await self._fire_usage(usage)
        return text

    @staticmethod
    def _parse_claude(raw: str) -> tuple[str, dict[str, Any] | None]:
        """The CLI's ``--output-format json`` envelope: {type:result, result:str}."""
        if not raw:
            return "", None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw, None
        if not isinstance(payload, dict):
            return raw, None
        if payload.get("type") not in (None, "result"):
            return raw, None
        return str(payload.get("result") or ""), payload

    async def _call_codex(self, prompt: str, agent: str, task: str,
                          budget: float, started: float) -> str:
        from sellbird import codex

        if self._codex is None:
            self._codex = codex.CodexClient(
                model=self.model or "",
                reasoning_effort=self.reasoning_effort or "",
            )
        result = await self._codex.run(prompt, timeout=budget)
        usage = getattr(result, "usage", None)
        if isinstance(usage, dict) and usage:
            row = Usage(
                input_tokens=as_int(usage.get("input_tokens")
                                    or usage.get("inputTokens")),
                cached_input_tokens=as_int(usage.get("cached_input_tokens")
                                           or usage.get("cachedInputTokens")),
                output_tokens=as_int(usage.get("output_tokens")
                                     or usage.get("outputTokens")),
                reasoning_tokens=as_int(usage.get("reasoning_tokens")
                                        or usage.get("reasoningOutputTokens")),
                total_tokens=as_int(usage.get("total_tokens")),
                cost_usd=0.0,   # ChatGPT plans are not billed per token
                model=getattr(result, "model", "") or self.model,
                provider="codex_cli",
                duration_ms=int((monotonic() - started) * 1000),
                agent=agent,
                task=task,
            )
            self.last_usage = row
            await self._fire_usage(row)
        return extract_text(getattr(result, "text", "")) or ""

    async def aclose(self) -> None:
        client = self._codex
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception as exc:  # pragma: no cover - teardown only
                logger.debug("codex close failed: %s", type(exc).__name__)
        self._codex = None


def build_delegate(kind: str, **kwargs: Any) -> DelegateTransport:
    return DelegateTransport(kind, **kwargs)
