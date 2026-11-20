"""A server running Sellbird for others plugs its own model calls in through
``Brain(transport=...)``, and a home with no prompts folder still gets the
shipped prompts and skills."""

import asyncio
from pathlib import Path

import pytest

from sellbird.brain import Brain
from sellbird.workspace_home import Home, use_home


class Transport:
    def __init__(self, answer="ok", paused=""):
        self.answer, self.paused_reason, self.calls = answer, paused, []

    async def __call__(self, prompt, *, agent, task, timeout, expect_json):
        self.calls.append((prompt, agent, task, expect_json))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.asyncio
async def test_think_goes_through_the_transport():
    t = Transport('{"subject": "a b", "body": "hi"}')
    brain = Brain(state=None, transport=t)
    assert await brain.think_json("write", agent="writer", task="personal_email") == {
        "subject": "a b", "body": "hi"}
    prompt, agent, task, _ = t.calls[0]
    assert prompt.startswith("write") and (agent, task) == ("writer", "personal_email")


@pytest.mark.asyncio
async def test_a_failing_transport_answers_empty_not_raises():
    brain = Brain(state=None, transport=Transport(RuntimeError("api down")))
    assert await brain.think("x") == ""


@pytest.mark.asyncio
async def test_a_slow_transport_times_out():
    class Slow(Transport):
        async def __call__(self, prompt, **kw):
            await asyncio.sleep(5)
            return "late"
    assert await Brain(state=None, transport=Slow()).think("x", timeout=0.05) == ""


@pytest.mark.asyncio
async def test_a_paused_transport_pauses_the_brain():
    t = Transport(paused="daily AI budget reached")
    brain = Brain(state=None, transport=t)
    assert brain.paused_reason == "daily AI budget reached"
    assert await brain.think("x") == "" and t.calls == []


def test_a_home_without_prompts_gets_the_shipped_ones(tmp_path):
    nowhere = tmp_path / "nonexistent"
    home = Home.for_root(nowhere)
    with use_home(home):
        brain = Brain(state=None)
        assert "{{persona_company}}" in brain.load_prompt("writer")
        assert brain.load_skill("email_frameworks")
        assert brain.load_skill("product_knowledge_that_does_not_exist") == ""


def test_the_homes_own_prompt_wins(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "writer.md").write_text("mine {{x}}")
    with use_home(Home.for_root(Path(tmp_path))):
        assert Brain(state=None).load_prompt("writer", x="1") == "mine 1"


# ── A transport needs no CLI preflight (the bug that stopped the loop) ──


@pytest.mark.asyncio
async def test_a_transport_preflights_ok_without_any_claude_cli(monkeypatch):
    """A transport install has no Claude CLI, and must still be ready.

    The Claude path probes ``claude --help`` for its isolation flags and
    paused the whole heartbeat when the binary was missing — even though the
    transport, not Claude, was going to answer every call. ``check_cli``
    raising here IS the assertion: if preflight reaches for the CLI, this
    test fails instead of quietly passing.
    """
    from sellbird import claude_runtime

    def _forbidden(env=None):
        raise AssertionError("preflight probed the Claude CLI for a transport")

    monkeypatch.setattr(claude_runtime, "check_cli", _forbidden)
    brain = Brain(state=None, transport=Transport("ok"))
    assert await brain.preflight() == ""


@pytest.mark.asyncio
async def test_a_transport_still_reports_its_own_pause():
    """No CLI probe, but a paused transport is still not "ready"."""
    brain = Brain(state=None, transport=Transport("ok", paused="budget spent"))
    assert await brain.preflight() == ""          # not the CLI's business
    assert brain.paused_reason == "budget spent"  # the transport's own word


def test_an_explicit_transport_beats_the_configured_factory(monkeypatch):
    """``transport=`` is the host seam: config never overrides it."""
    from sellbird.config import BrainConfig

    monkeypatch.setattr("sellbird.brain._resolve_transport",
                        lambda spec: pytest.fail("factory consulted"))
    given = Transport("x")
    brain = Brain(state=None, transport=given,
                  brain_config=BrainConfig(transport_factory="anywhere:build"))
    assert brain.transport is given


def test_a_bad_transport_spec_is_ignored_not_raised(caplog):
    """A typo in sellbird.yaml must not take the whole CLI down."""
    from sellbird.config import BrainConfig
    import sellbird.brain as brain_mod

    spec = "no_such_module_anywhere:build"
    brain_mod._TRANSPORT_SPEC_REPORTED.discard(spec)
    with caplog.at_level("ERROR", logger="sellbird.brain"):
        brain = Brain(state=None,
                      brain_config=BrainConfig(transport_factory=spec))
    assert brain.transport is None
    assert brain.provider == "claude"      # config's provider, untouched
    assert "did not load" in caplog.text


def test_a_factory_resolves_from_a_bare_module_path():
    """``pkg.module`` alone calls its ``build``."""
    from sellbird.config import BrainConfig

    brain = Brain(state=None, brain_config=BrainConfig(
        transport_factory="tests.fake_transport_module"))
    assert brain.transport is not None and callable(brain.transport)


def test_a_chain_that_failed_to_load_is_reported_as_failed():
    """``status --check`` must not read healthy when the factory blew up.

    The transport is None, so every real call falls through to the Claude CLI
    — which needs a login even though this check does not test for one.
    """
    from sellbird.brain import _resolve_transport
    from sellbird.cli import _transport_failure_line
    from sellbird.config import BrainConfig

    spec = "no.such.module:build"
    brain = Brain(state=None, brain_config=BrainConfig(transport_factory=spec))
    assert brain.transport is None
    assert _resolve_transport(spec) is None
    assert _transport_failure_line(spec, brain.provider_label) == (
        "  Brain:                transport_factory no.such.module:build "
        "FAILED to load\n"
        "                        The configured provider chain is unusable "
        "(usually: every provider's API key is empty, or the dotted path is "
        "wrong). See the log line \"brain.transport_factory ... did not "
        "load\". Falling back to Claude, which needs a CLI login."
    )
