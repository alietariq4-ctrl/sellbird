"""Inbound Twenty CRM sync: people marked do-not-contact stop being emailed.

The outbound direction (pushing lead events to Twenty) already existed.
This covers the reverse: a custom boolean on Twenty's Person ("doNotContact",
since Person has no native unsubscribe field) is read with a server-side
filter and fed into Sellbird's suppressions, so a blocklist kept in the CRM
actually holds here. Opt-in, Twenty only, and it never raises — a CRM hiccup
must not stall a heartbeat cycle. All fixtures are fictional.
"""

from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest
import pytest_asyncio

from sellbird.config import CrmConfig
from sellbird.integrations.crm_sync import TwentyInboundSync
from sellbird.state import StateManager


def _config(**crm):
    return SimpleNamespace(crm=CrmConfig(**crm))


def _env(**kw):
    base = {"twenty_api_key": "tw_key", "twenty_base_url": "https://crm.example"}
    base.update(kw)
    return SimpleNamespace(**base)


def _people(*emails, field="doNotContact"):
    return [
        {"id": f"p{i}", field: True, "emails": {"primaryEmail": e}}
        for i, e in enumerate(emails)
    ]


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "crm.db"))
    await s.init_db()
    return s


def test_inbound_is_off_unless_enabled_and_keyed():
    assert TwentyInboundSync(None, config=_config(), env=_env()).is_configured() is False
    # Enabled but no key: still off.
    assert TwentyInboundSync(None, config=_config(inbound_enabled=True),
                             env=SimpleNamespace(twenty_api_key="", twenty_base_url="")).is_configured() is False
    assert TwentyInboundSync(None, config=_config(inbound_enabled=True), env=_env()).is_configured() is True


def test_a_bad_field_name_falls_back_to_the_default():
    assert CrmConfig(inbound_field="doNotContact").inbound_field == "doNotContact"
    assert CrmConfig(inbound_field="has space").inbound_field == "doNotContact"
    assert CrmConfig(inbound_field="custom_dnc").inbound_field == "custom_dnc"


@pytest.mark.asyncio
async def test_do_not_contact_people_are_suppressed(state):
    seen_urls = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_urls.append(unquote(str(req.url)))
        # The filter must be server-side and on the configured field.
        assert "doNotContact[eq]:true" in unquote(str(req.url))
        return httpx.Response(200, json={
            "data": {"people": _people("a@one.example", "b@two.example")},
            "pageInfo": {"hasNextPage": False},
        })

    sync = TwentyInboundSync(state, config=_config(inbound_enabled=True), env=_env(),
                             transport=httpx.MockTransport(handler))
    added = await sync.run()
    assert added == 2
    assert await state.is_suppressed("a@one.example") == "imported"
    assert await state.is_suppressed("b@two.example") == "imported"


@pytest.mark.asyncio
async def test_a_second_poll_adds_nothing_new(state):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"people": _people("a@one.example")}})

    sync = TwentyInboundSync(state, config=_config(inbound_enabled=True), env=_env(),
                             transport=httpx.MockTransport(handler))
    assert await sync.run() == 1
    assert await sync.run() == 0  # already suppressed: idempotent


@pytest.mark.asyncio
async def test_pagination_follows_the_cursor(state):
    pages = [
        {"data": {"people": _people("a@one.example", "b@two.example")}},
        {"data": {"people": _people("c@three.example")}},
        {"data": {"people": []}},
    ]
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        body = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return httpx.Response(200, json=body)

    sync = TwentyInboundSync(state, config=_config(inbound_enabled=True), env=_env(),
                             transport=httpx.MockTransport(handler))
    added = await sync.run()
    assert added == 3
    assert await state.is_suppressed("c@three.example") == "imported"
    # Second page carried the last id of the first as its cursor.
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_additional_emails_are_read_too(state):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"people": [
            {"id": "p1", "doNotContact": True,
             "emails": {"primaryEmail": "main@co.example",
                        "additionalEmails": ["other@co.example"]}},
        ]}})

    sync = TwentyInboundSync(state, config=_config(inbound_enabled=True), env=_env(),
                             transport=httpx.MockTransport(handler))
    assert await sync.run() == 2
    assert await state.is_suppressed("other@co.example") == "imported"


@pytest.mark.asyncio
async def test_an_http_error_is_swallowed_and_adds_nothing(state):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    sync = TwentyInboundSync(state, config=_config(inbound_enabled=True), env=_env(),
                             transport=httpx.MockTransport(handler))
    assert await sync.run() == 0  # never raises


@pytest.mark.asyncio
async def test_a_disabled_sync_makes_no_request(state):
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not call Twenty when disabled")

    sync = TwentyInboundSync(state, config=_config(), env=_env(),
                             transport=httpx.MockTransport(handler))
    assert await sync.run() == 0


@pytest.mark.asyncio
async def test_the_custom_field_name_is_honoured(state):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(unquote(str(req.url)))
        return httpx.Response(200, json={"data": {"people": []}})

    sync = TwentyInboundSync(
        state, config=_config(inbound_enabled=True, inbound_field="customDnc"),
        env=_env(), transport=httpx.MockTransport(handler))
    await sync.run()
    assert any("customDnc[eq]:true" in u for u in seen)
