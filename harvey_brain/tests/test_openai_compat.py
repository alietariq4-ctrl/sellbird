"""Offline tests for the OpenAI-compatible transport. No keys, no network."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest

from harvey_brain.base import Usage
from harvey_brain.openai_compat import OpenAICompatTransport
from harvey_brain.spec import PROVIDERS, get_provider


def run(coro):
    return asyncio.run(coro)


def make(handler, provider="openrouter", model=None, **kwargs) -> OpenAICompatTransport:
    spec = get_provider(provider)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("max_retries", 2)
    kwargs.setdefault("retry_base_delay", 0.0)
    return OpenAICompatTransport(
        spec, "test-key", model or spec.default_model, http=client, **kwargs
    )


def body(text="hello", model="anthropic/claude-sonnet-5.5", usage=None, **extra):
    payload = {
        "id": "gen-1",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": usage if usage is not None else {
            "prompt_tokens": 194,
            "completion_tokens": 2,
            "total_tokens": 196,
            "prompt_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 100},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }
    payload.update(extra)
    return payload


# 1 ── request body
def test_request_body_and_auth_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body())

    run(make(handler)("score this"))
    request = seen[0]
    assert request.url.path.endswith("/chat/completions")
    assert request.headers["authorization"] == "Bearer test-key"
    payload = json.loads(request.content)
    assert payload["model"] == "anthropic/claude-sonnet-5.5"
    assert payload["messages"] == [{"role": "user", "content": "score this"}]
    assert "response_format" not in payload


def test_system_prefix_is_opt_in_only():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body())

    run(make(handler)("hi"))
    assert seen[0]["messages"][0]["role"] == "user"

    run(make(handler, system_prefix="Be terse.")("hi"))
    assert seen[1]["messages"][0] == {"role": "system", "content": "Be terse."}
    assert seen[1]["messages"][1]["role"] == "user"


def test_openrouter_attribution_headers_are_sent():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body())

    run(make(handler)("hi"))
    assert seen[0].headers["http-referer"]
    assert seen[0].headers["x-openrouter-title"]


# 2 ── JSON policy (three separate behaviours)
def test_expect_json_uses_json_schema_when_supported():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body('{"ok": true}'))

    run(make(handler, provider="openai")("return json", expect_json=True))
    fmt = seen[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"] == "response"
    assert fmt["json_schema"]["schema"] == {"type": "object", "additionalProperties": True}


def test_expect_json_uses_json_object_when_only_that_is_supported():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body('{"ok": true}'))

    run(make(handler, provider="deepseek")("return json", expect_json=True))
    assert seen[0]["response_format"] == {"type": "json_object"}


def test_expect_json_sends_nothing_when_the_provider_supports_neither():
    seen: list[dict] = []
    spec = get_provider("openai")
    # An endpoint with no structured-output support at all.
    from dataclasses import replace
    bare = replace(spec, name="bare", supports_json_schema=False,
                   supports_json_object=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (seen.append(json.loads(request.content)),
                         httpx.Response(200, json=body('{"ok": true}')))[1]))

    transport = OpenAICompatTransport(bare, "k", "m", http=client,
                                      retry_base_delay=0.0)
    assert run(transport("return json", expect_json=True)) == '{"ok": true}'
    assert "response_format" not in seen[0]


def test_explicit_json_mode_overrides_capabilities():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body('{"ok": true}'))

    run(make(handler, provider="openai", json_mode="none")(
        "return json", expect_json=True))
    assert "response_format" not in seen[0]


def test_schema_rejection_downgrades_to_json_object_once():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if payload.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": "schema unsupported"})
        return httpx.Response(200, json=body('{"ok": true}'))

    transport = make(handler, provider="openrouter")
    assert run(transport("json", expect_json=True)) == '{"ok": true}'
    assert [p.get("response_format", {}).get("type") for p in seen] == [
        "json_schema", "json_object"]


# 3 ── OpenRouter cost
def test_openrouter_cost_field_wins_over_the_price_table():
    recorded: list[Usage] = []
    payload = body(usage={
        "prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000,
        "cost": 0.0123,
    })
    transport = make(lambda request: httpx.Response(200, json=payload),
                     on_usage=recorded.append)
    run(transport("hi"))

    assert recorded[0].cost_usd == pytest.approx(0.0123)
    assert recorded[0].provider == "openrouter"
    assert (recorded[0].input_tokens, recorded[0].output_tokens) == (1000, 1000)


# 4 ── price-table fallback and unknown models
def test_price_table_is_used_when_the_api_reports_no_cost():
    recorded: list[Usage] = []
    payload = body(usage={"prompt_tokens": 1_000_000, "completion_tokens": 0,
                          "total_tokens": 1_000_000})
    transport = make(lambda request: httpx.Response(200, json=payload),
                     on_usage=recorded.append, provider="openai",
                     model="gpt-6-luna")
    run(transport("hi"))
    # gpt-6-luna: $0.10 per 1M input (doc fact).
    assert recorded[0].cost_usd == pytest.approx(0.10)


def test_unknown_model_costs_zero_and_does_not_raise():
    recorded: list[Usage] = []
    payload = body(model="mystery-model-9")
    transport = make(lambda request: httpx.Response(200, json=payload),
                     on_usage=recorded.append, provider="openai",
                     model="mystery-model-9")
    assert run(transport("hi")) == "hello"
    assert recorded[0].cost_usd == 0.0


def test_reasoning_field_is_counted_but_not_returned_as_text():
    recorded: list[Usage] = []
    payload = body(text="the answer", usage={
        "prompt_tokens": 10, "completion_tokens": 90, "total_tokens": 100,
        "completion_tokens_details": {"reasoning_tokens": 80},
    })
    payload["choices"][0]["message"]["reasoning"] = "long private thinking"
    transport = make(lambda request: httpx.Response(200, json=payload),
                     on_usage=recorded.append)

    assert run(transport("hi")) == "the answer"
    assert recorded[0].reasoning_tokens == 80
    assert "private thinking" not in recorded[0].model


def test_content_parts_are_joined():
    payload = body()
    payload["choices"][0]["message"]["content"] = [
        {"type": "text", "text": "part one "},
        {"type": "text", "text": "part two"},
    ]
    transport = make(lambda request: httpx.Response(200, json=payload))
    assert run(transport("hi")) == "part one part two"


# 5 ── retries
def test_two_500s_then_200_succeeds_after_three_calls():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(500, text="upstream boom")
        return httpx.Response(200, json=body())

    assert run(make(handler)("hi")) == "hello"
    assert len(calls) == 3


def test_transport_error_is_retried():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(200, json=body())

    assert run(make(handler)("hi")) == "hello"
    assert len(calls) == 2


# 6 ── auth
def test_401_returns_empty_without_retry_and_pauses():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": "invalid api key"})

    transport = make(handler)
    assert run(transport("hi")) == ""
    assert len(calls) == 1
    assert transport.paused_reason
    assert "OPENROUTER_API_KEY" in transport.paused_reason


def test_403_pauses_without_retrying():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, text="forbidden")

    transport = make(handler)
    assert run(transport("hi")) == ""
    assert len(calls) == 1
    assert transport.paused_reason


def test_paused_transport_makes_no_request():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=body())

    transport = make(handler)
    transport.paused_reason = "already paused"
    assert run(transport("hi")) == ""
    assert calls == []


def test_success_clears_a_pause():
    responses = [httpx.Response(401), httpx.Response(200, json=body())]
    transport = make(lambda request: responses.pop(0))
    assert run(transport("hi")) == ""
    assert transport.paused_reason
    transport.paused_reason = ""
    assert run(transport("hi")) == "hello"
    assert transport.paused_reason == ""


# 7 ── credits
def test_402_returns_empty_and_pauses_about_credits():
    transport = make(lambda request: httpx.Response(
        402, json={"error": "insufficient credits"}))
    assert run(transport("hi")) == ""
    assert "credit" in transport.paused_reason.lower()


def test_429_with_a_quota_body_pauses_about_credits():
    transport = make(lambda request: httpx.Response(
        429, json={"error": {"message": "You exceeded your current quota"}}))
    assert run(transport("hi")) == ""
    assert "quota" in transport.paused_reason.lower() or \
        "credit" in transport.paused_reason.lower()


def test_429_exhausted_returns_empty_without_raising():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text="rate limited")

    assert run(make(handler)("hi", timeout=5.0)) == ""
    assert len(calls) == 3


# 8 ── timeout
def test_timeout_bounds_the_whole_call():
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=body())

    started = time.monotonic()
    assert run(make(handler)("hi", timeout=0.3)) == ""
    assert time.monotonic() - started < 1.3


# 15 ── no secret in logs
def test_the_api_key_never_appears_in_a_log_record(caplog):
    secret = "sk-or-v1-DEADBEEFdeadbeef0123456789"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom for " + secret)

    transport = make(handler)
    with caplog.at_level(logging.DEBUG):
        run(transport("hi"))
    transport.paused_reason = ""
    with caplog.at_level(logging.DEBUG):
        run(make(lambda request: httpx.Response(401, text="bad key"))("hi"))

    assert secret not in caplog.text
    assert "test-key" not in caplog.text


def test_prompt_is_not_logged(caplog):
    prompt = "CONFIDENTIAL prospect dossier: Acme Ltd, revenue 4.2M"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="no")

    with caplog.at_level(logging.DEBUG):
        run(make(handler)(prompt))
    assert "CONFIDENTIAL" not in caplog.text


# ── misc ──
def test_unparseable_body_returns_empty():
    transport = make(lambda request: httpx.Response(200, text="<html>nope</html>"))
    assert run(transport("hi")) == ""


def test_no_choices_returns_empty():
    transport = make(lambda request: httpx.Response(200, json={"choices": []}))
    assert run(transport("hi")) == ""


def test_async_usage_callback_is_awaited():
    seen: list[float] = []

    async def on_usage(usage: Usage) -> None:
        seen.append(usage.cost_usd)

    transport = make(lambda request: httpx.Response(200, json=body()),
                     on_usage=on_usage)
    run(transport("hi"))
    assert len(seen) == 1


def test_max_output_tokens_and_temperature_are_sent():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body())

    run(make(handler, max_output_tokens=512, temperature=0.2)("hi"))
    assert seen[0]["max_tokens"] == 512
    assert seen[0]["temperature"] == pytest.approx(0.2)


def test_specs_cover_the_required_providers():
    for name in ("openrouter", "xai", "openai", "gemini", "deepseek", "groq",
                 "mistral", "together", "ollama", "claude_cli", "codex_cli"):
        assert name in PROVIDERS, name
    assert PROVIDERS["openrouter"].api_style == "openai"
    assert PROVIDERS["claude_cli"].delegates
    assert PROVIDERS["ollama"].keyless
