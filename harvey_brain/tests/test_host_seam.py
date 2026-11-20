"""Integration smoke test against the REAL host seam at ``_ref/sellbird``.

Proves the router satisfies the same contract ``harvey_grok`` proved: a real
``sellbird.brain.Brain`` constructed with ``transport=router`` completes
``think_json``, forwards the host's JSON suffix intact, surfaces
``paused_reason``, and never double-calls once paused.

Skipped when the checkout is absent. No network, no keys.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

HOST_ROOT = Path(__file__).resolve().parents[2]
if not (HOST_ROOT / "sellbird").is_dir():  # pragma: no cover - only without the clone
    pytest.skip(f"Sellbird checkout not found at {HOST_ROOT}", allow_module_level=True)

os.environ.pop("SELLBIRD_HOME", None)
if str(HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(HOST_ROOT))

from harvey_brain.config import BrainSettings, ChainEntry  # noqa: E402
from harvey_brain.openai_compat import OpenAICompatTransport  # noqa: E402
from harvey_brain.router import RouterTransport  # noqa: E402
from harvey_brain.spec import get_provider  # noqa: E402

JSON_SUFFIX = "\n\nRespond ONLY with valid JSON. No markdown, no explanation."
ANSWER = "Acme builds warehouse robots in Leeds."


def run(coro):
    return asyncio.run(coro)


def chat_body(text: str, *, cost: float = 0.0042) -> dict:
    return {
        "id": "gen-host-seam",
        "object": "chat.completion",
        "model": "anthropic/claude-haiku-5.5",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 8000,
            "completion_tokens": 1200,
            "total_tokens": 9200,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 300},
            "cost": cost,
        },
    }


class Recorder:
    def __init__(self, responder):
        self.bodies: list[dict] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return self._responder(len(self.bodies))

    @property
    def count(self) -> int:
        return len(self.bodies)


async def make_host(tmp_path: Path, responder, *, budget: float | None = 5.0):
    from sellbird.brain import Brain
    from sellbird.state import StateManager

    state = StateManager(str(tmp_path / "sellbird.db"))
    await state.init_db()
    recorder = Recorder(responder)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    spec = get_provider("openrouter")
    transport = OpenAICompatTransport(
        spec, "test-key", "anthropic/claude-haiku-5.5", http=client,
        max_retries=0, retry_base_delay=0.0,
    )
    router = RouterTransport([transport], budget_usd_per_day=budget)
    brain = Brain(state=state, transport=router)
    return brain, router, transport, recorder, client


def test_think_json_round_trips_through_the_real_brain(tmp_path):
    async def scenario():
        brain, router, transport, recorder, client = await make_host(
            tmp_path, lambda n: httpx.Response(
                200, json=chat_body('{"company": "Acme", "score": 87}')))
        try:
            parsed = await brain.think_json("score this", agent="scout",
                                            task="profile")
            text = await brain.think("say something", agent="scout", task="note")
            return parsed, text, recorder, router
        finally:
            await client.aclose()

    parsed, text, recorder, router = run(scenario())

    assert parsed == {"company": "Acme", "score": 87}
    # The host appends its suffix and we must not strip it.
    assert recorder.bodies[0]["messages"][0]["content"] == "score this" + JSON_SUFFIX
    assert text == ANSWER or text == '{"company": "Acme", "score": 87}'
    assert router.last_provider == "openrouter"
    assert router.last_usage is not None
    assert router.spend_usd > 0


def test_usage_is_recorded_into_the_real_ledger(tmp_path):
    from sellbird.state import StateManager

    async def scenario():
        state = StateManager(str(tmp_path / "ledger.db"))
        await state.init_db()
        recorder = Recorder(lambda n: httpx.Response(200, json=chat_body(ANSWER)))
        client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))

        async def on_usage(usage):
            # The real host API, with a provider label the ledger accepts.
            await state.record_usage_event(
                agent=usage.agent, task=usage.task, session_id="",
                model=usage.model, duration_ms=usage.duration_ms, num_turns=1,
                source="result_json", provider="claude",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cached_input_tokens,
                cost_usd=usage.cost_usd,
            )

        transport = OpenAICompatTransport(
            get_provider("openrouter"), "k", "anthropic/claude-haiku-5.5",
            http=client, on_usage=on_usage, max_retries=0,
        )
        from sellbird.brain import Brain
        brain = Brain(state=state, transport=RouterTransport([transport]))
        try:
            text = await brain.think("profile acme", agent="scout", task="profile")
            return text, await state.usage_totals()
        finally:
            await client.aclose()

    text, totals = run(scenario())
    assert text == ANSWER
    assert totals["today"]["calls"] == 1
    assert totals["today"]["input_tokens"] == 8000
    assert totals["today"]["cost_usd"] == pytest.approx(0.0042)


def test_401_pauses_the_real_brain_and_stops_the_chain(tmp_path):
    async def scenario():
        brain, router, transport, recorder, client = await make_host(
            tmp_path, lambda n: httpx.Response(401, json={"error": "bad key"}))
        try:
            first = await brain.think("hi")
            paused = brain.paused_reason
            second = await brain.think("hi again")
            return first, second, paused, recorder
        finally:
            await client.aclose()

    first, second, paused, recorder = run(scenario())

    assert first == ""
    assert second == ""
    assert paused    # Brain reads the router's paused_reason
    assert recorder.count == 1


def test_budget_pause_reaches_the_brain(tmp_path):
    async def scenario():
        brain, router, transport, recorder, client = await make_host(
            tmp_path, lambda n: httpx.Response(200, json=chat_body(ANSWER)),
            budget=0.001)
        try:
            first = await brain.think("hi")
            paused = brain.paused_reason
            second = await brain.think("again")
            return first, second, paused, recorder
        finally:
            await client.aclose()

    first, second, paused, recorder = run(scenario())

    assert first == ANSWER
    assert second == ""
    assert "budget" in paused.lower()
    assert recorder.count == 1
