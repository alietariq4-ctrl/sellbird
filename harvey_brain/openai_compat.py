"""One transport for every OpenAI-compatible ``/chat/completions`` endpoint.

Covers OpenRouter, xAI, OpenAI, Gemini's OpenAI-compat endpoint, DeepSeek,
Groq, Mistral, Together, Fireworks, Cerebras, Ollama, LM Studio, vLLM, and any
custom ``OPENAI_BASE_URL`` endpoint. The host contract is the one proven
against ``sellbird.brain.Brain``:

    async transport(prompt, *, agent, task, timeout, expect_json) -> str

Never raises; ``""`` on failure; ``paused_reason`` for auth/credit pauses.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import inspect
import logging
import random
from time import monotonic
from typing import Any, Callable, Mapping

import httpx

from .base import (
    DEFAULT_SCHEMA,
    DEFAULT_TIMEOUT,
    RETRY_STATUSES,
    Usage,
    as_int,
    excerpt,
    extract_text,
    is_quota_body,
    nested,
)
from .pricing import estimate
from .schemas import resolve_response_format
from .spec import ProviderSpec

logger = logging.getLogger(__name__)

JsonMode = str  # "auto" | "json_schema" | "json_object" | "none"
JSON_MODES = frozenset({"auto", "json_schema", "json_object", "none"})


class OpenAICompatTransport:
    """A chat-completions transport that never raises."""

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str = "",
        model: str | None = None,
        *,
        base_url: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        on_usage: Callable[[Usage], Any] | None = None,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 2,
        retry_base_delay: float = 2.0,
        timeout: float = DEFAULT_TIMEOUT,
        json_mode: JsonMode = "auto",
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        system_prefix: str | None = None,
        json_schema: Mapping[str, Any] | None = None,
    ):
        if json_mode not in JSON_MODES:
            raise ValueError(
                f"json_mode must be one of {sorted(JSON_MODES)}, got {json_mode!r}"
            )
        self.spec = spec
        self.name = spec.name
        self.api_key = api_key
        self.model = model or spec.default_model
        self.base_url = (base_url or spec.base_url).strip()
        self.extra_headers = dict(extra_headers or {})
        self.on_usage = on_usage
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = max(0.0, float(retry_base_delay))
        self.default_timeout = float(timeout)
        self.json_mode = json_mode
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.system_prefix = system_prefix
        self.json_schema = dict(json_schema) if json_schema is not None else None
        self.paused_reason = ""
        self.last_error = ""
        self.last_usage: Usage | None = None
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=None)
        self._url = f"{self.base_url.rstrip('/')}/chat/completions"

    # ── request ───────────────────────────────────────────────────────────

    def headers(self) -> dict[str, str]:
        return self._headers()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            **self.spec.headers(),
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _messages(self, prompt: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if self.system_prefix:
            # Off by default: the host sends one flat prompt, so adding a
            # system turn would silently change every prompt it builds.
            messages.append({"role": "system", "content": self.system_prefix})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _structured(
        self, json_mode: str | None = None, *, task: str = ""
    ) -> Mapping[str, Any] | None:
        """The response_format for this request, or None for prompt-only JSON."""
        mode = json_mode if json_mode is not None else self.json_mode
        return resolve_response_format(
            mode,
            task=task,
            explicit_schema=self.json_schema,
            supports_json_schema=self.spec.supports_json_schema,
            supports_json_object=self.spec.supports_json_object,
            default_schema=DEFAULT_SCHEMA,
        )

    def _body(
        self,
        prompt: str,
        expect_json: bool,
        *,
        task: str = "",
        model: str | None = None,
        json_mode: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """The request body. Per-call overrides win over the configured values."""
        effective_model = (model or "").strip() or self.model
        body: dict[str, Any] = {
            "model": effective_model,
            "messages": self._messages(prompt),
        }
        if expect_json:
            structured = self._structured(
                json_mode if json_mode is not None else self.json_mode,
                task=task,
            )
            if structured is not None:
                body["response_format"] = dict(structured)
        limit = (max_output_tokens if max_output_tokens is not None
                 else self.max_output_tokens)
        if limit is not None:
            body["max_tokens"] = int(limit)
        temp = temperature if temperature is not None else self.temperature
        if temp is not None:
            body["temperature"] = float(temp)
        effort = reasoning_effort or self.reasoning_effort
        if effort:
            body["reasoning_effort"] = effort
        return body

    def _next_response_format(
        self, current: Mapping[str, Any] | None
    ) -> tuple[bool, dict[str, Any] | None]:
        """Downgrade ladder on HTTP 400/422: json_schema -> json_object -> None."""
        if not isinstance(current, Mapping):
            return False, None
        fmt_type = current.get("type")
        if fmt_type == "json_schema":
            if self.spec.supports_json_object:
                return True, {"type": "json_object"}
            return True, None
        if fmt_type == "json_object":
            return True, None
        return False, None

    # ── response ──────────────────────────────────────────────────────────

    @staticmethod
    def _text(payload: Mapping[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, Mapping):
            return ""
        message = first.get("message")
        if isinstance(message, Mapping):
            text = extract_text(message.get("content"))
            if text:
                return text
            # ``reasoning``/``reasoning_content`` is thinking, not the answer:
            # it is counted in usage and deliberately never returned as text.
        return extract_text(first.get("text"))

    def _usage(
        self,
        payload: Mapping[str, Any],
        agent: str,
        task: str,
        duration_ms: int,
        *,
        model: str | None = None,
    ) -> Usage | None:
        raw = payload.get("usage")
        if not isinstance(raw, Mapping):
            return None
        effective_model = (model or "").strip() or self.model
        input_tokens = as_int(raw.get("prompt_tokens", raw.get("input_tokens")))
        output_tokens = as_int(raw.get("completion_tokens", raw.get("output_tokens")))
        cached = as_int(nested(raw, "prompt_tokens_details", "cached_tokens"))
        if not cached:
            cached = as_int(nested(raw, "input_tokens_details", "cached_tokens"))
        reasoning = as_int(nested(raw, "completion_tokens_details", "reasoning_tokens"))
        if not reasoning:
            reasoning = as_int(nested(raw, "output_tokens_details", "reasoning_tokens"))
        total = as_int(raw.get("total_tokens")) or input_tokens + output_tokens

        reported = raw.get("cost")
        if isinstance(reported, (int, float)) and not isinstance(reported, bool):
            cost = round(float(reported), 6)   # OpenRouter: USD, doc fact
        else:
            cost = estimate(
                self.spec.name, effective_model,
                input_tokens=input_tokens,
                cached_input_tokens=cached,
                output_tokens=output_tokens,
            )

        resp_model = payload.get("model")
        return Usage(
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning,
            total_tokens=total,
            cost_usd=cost,
            model=resp_model if isinstance(resp_model, str) and resp_model else effective_model,
            provider=self.spec.name,
            duration_ms=duration_ms,
            agent=agent,
            task=task,
        )

    # ── failures ──────────────────────────────────────────────────────────

    def _pause_for(self, status: int, body: str) -> None:
        name = self.spec.name
        if status == 402 or is_quota_body(body):
            self.paused_reason = (
                f"{name} is paused: out of credits or over quota. "
                f"Top up the account or move it down the chain."
            )
        elif status in (401, 403):
            env = self.spec.api_key_env or "the API key"
            self.paused_reason = (
                f"{name} is paused: the API key was rejected. Check {env}."
            )

    async def _fire_usage(self, usage: Usage) -> None:
        if self.on_usage is None:
            return
        try:
            try:
                result = self.on_usage(usage)
            except TypeError:
                result = self.on_usage(**asdict(usage))
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # a broken ledger must not lose the answer
            logger.warning("on_usage callback failed: %s", type(exc).__name__)

    async def _sleep(self, attempt: int, deadline: float) -> None:
        delay = self.retry_base_delay * (2 ** attempt)
        delay *= random.uniform(0.5, 1.5)
        remaining = deadline - monotonic()
        await asyncio.sleep(max(0.0, min(delay, remaining)))

    # ── the seam ──────────────────────────────────────────────────────────

    async def __call__(
        self,
        prompt: str,
        *,
        agent: str = "",
        task: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        expect_json: bool = False,
        model: str | None = None,
        base_url: str | None = None,
        json_mode: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """One call. The host passes only the first five names; the rest are
        per-role overrides from the router (see ``harvey_brain/profile.py``)."""
        started = monotonic()
        budget = float(timeout if timeout is not None else self.default_timeout)
        deadline = started + budget
        self.last_usage = None

        if self.paused_reason:
            return ""

        effective_model = (model or "").strip() or self.model
        effective_url = (
            f"{base_url.strip().rstrip('/')}/chat/completions"
            if base_url and base_url.strip()
            else self._url
        )
        body = self._body(
            prompt, expect_json,
            task=task, model=effective_model,
            json_mode=json_mode, max_output_tokens=max_output_tokens,
            temperature=temperature, reasoning_effort=reasoning_effort,
        )
        attempt = 0
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                logger.warning("%s call timed out after %.0fs", self.name, budget)
                self.last_error = "timeout"
                return ""
            try:
                response = await asyncio.wait_for(
                    self._http.post(effective_url, json=body, headers=self._headers()),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                logger.warning("%s call timed out after %.0fs", self.name, budget)
                self.last_error = "timeout"
                return ""
            except httpx.TransportError as exc:
                self.last_error = f"transport: {type(exc).__name__}"
                if attempt >= self.max_retries:
                    logger.warning(
                        "%s transport error after %d attempts: %s",
                        self.name, attempt + 1, type(exc).__name__,
                    )
                    return ""
                await self._sleep(attempt, deadline)
                attempt += 1
                continue

            status = response.status_code
            if 200 <= status < 300:
                try:
                    payload = response.json()
                except Exception:
                    logger.warning("%s returned unparseable JSON (status %d)",
                                   self.name, status)
                    self.last_error = "bad json"
                    return ""
                if not isinstance(payload, Mapping):
                    logger.warning("%s returned a non-object body (status %d)",
                                   self.name, status)
                    self.last_error = "bad body"
                    return ""
                text = self._text(payload)
                duration_ms = int((monotonic() - started) * 1000)
                usage = self._usage(
                    payload, agent, task, duration_ms, model=effective_model
                )
                if usage is not None:
                    self.last_usage = usage
                    await self._fire_usage(usage)
                self.paused_reason = ""
                self.last_error = ""
                return text

            body_excerpt = excerpt(response, secret=self.api_key)
            self._pause_for(status, body_excerpt)
            if self.paused_reason:
                logger.warning("%s refused the request (status %d): %s",
                               self.name, status, body_excerpt)
                self.last_error = f"{status}: {body_excerpt}"
                return ""

            # Endpoint structured-output rejection (schema unsupported, strict
            # mode rejected, or json_object rejected): walk down the ladder
            # json_schema -> json_object -> prompt-only regardless of json_mode.
            if status in (400, 422) and "response_format" in body:
                can_downgrade, next_fmt = self._next_response_format(
                    body.get("response_format")
                )
                if can_downgrade:
                    cur_fmt = body["response_format"]
                    cur_type = (
                        cur_fmt.get("type", "response_format")
                        if isinstance(cur_fmt, Mapping)
                        else "response_format"
                    )
                    if next_fmt is None:
                        logger.warning(
                            "%s rejected %s (status %d); retrying with prompt-only JSON",
                            self.name, cur_type, status,
                        )
                        body.pop("response_format", None)
                    else:
                        logger.warning(
                            "%s rejected %s (status %d); retrying with %s",
                            self.name, cur_type, status, next_fmt.get("type"),
                        )
                        body["response_format"] = next_fmt
                    continue

            if status in RETRY_STATUSES and attempt < self.max_retries:
                logger.warning("%s %d on attempt %d, retrying: %s",
                               self.name, status, attempt + 1, body_excerpt)
                await self._sleep(attempt, deadline)
                attempt += 1
                continue

            logger.warning("%s failed (status %d): %s",
                           self.name, status, body_excerpt)
            self.last_error = f"{status}: {body_excerpt}"
            return ""

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
