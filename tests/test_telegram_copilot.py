"""The two-way Telegram copilot (sellbird/integrations/copilot.py).

The same bot that pushes alerts answers commands (/status, /approve,
/reject) and inline Approve/Reject buttons, polled once per heartbeat
cycle. Telegram has no built-in operator auth, so the configured chat id
is the allowlist: any other chat is read and dropped. All fixtures are
fictional; the Bot API is a MockTransport, nothing leaves the process.
"""

import json

import httpx
import pytest

from sellbird.config import SellbirdConfig
from sellbird.integrations.copilot import TelegramCopilot
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

CHAT = 100100100
TOKEN = "test-token"


def _cfg():
    return SellbirdConfig(
        persona={"name": "Sellbird", "company": "Acme", "role": "BD",
                 "email": "s@acme.example", "linkedin": "", "tone": "direct"},
        product={"name": "Widget", "description": "Widgets", "pricing": "$10",
                 "key_benefits": ["fast"], "objection_responses": {}},
        icp={"industries": ["software"], "company_size": "10-50",
             "titles": ["VP"], "geography": ["US"]},
        copilot={"telegram_bot_token": TOKEN, "telegram_chat_id": str(CHAT)},
    )


class FakeTelegram:
    """Records Bot API calls; answers getUpdates from a script."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        if method == "getUpdates":
            updates, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(200, json={"ok": True, "result": True})

    def push(self, update: dict):
        self.updates.append(update)

    def sent_texts(self):
        return [p.get("text", "") for m, p in self.calls if m == "sendMessage"]

    def answered_callbacks(self):
        return [p for m, p in self.calls if m == "answerCallbackQuery"]


async def _state(tmp_path):
    state = StateManager(str(tmp_path / "sellbird.db"))
    await state.init_db()
    return state


async def _seed_outbox(state, email="dana@harbor.example"):
    company = Company(name="Harbor Studio", domain="harbor.example")
    company.id = await state.add_company(company)
    p = Prospect(first_name="Dana", last_name="Reed", email=email,
                 company="Harbor Studio", company_id=company.id,
                 title="Owner", status="contacted")
    p.id = await state.add_prospect(p)
    item_id = await state.add_outbox_item(
        prospect_id=p.id, to_email=email, subject="Quick question",
        body="Hi Dana,", send_at="2026-11-21T14:00:00",
        step=1, kind="sequence", status="pending_review")
    return p, item_id


def _copilot(state, fake):
    return TelegramCopilot(state, _cfg(), transport=httpx.MockTransport(fake.handler))


def _message(text, chat=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def _callback(data, chat=CHAT):
    return {"update_id": 1, "callback_query": {"id": "cb1", "from": {"id": chat},
                                               "data": data}}


# ── the allowlist ──


@pytest.mark.asyncio
async def test_a_stranger_chat_is_ignored(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    p, item_id = await _seed_outbox(state)
    copilot = _copilot(state, fake)
    fake.push(_message("/approve " + item_id, chat=999))
    assert await copilot.poll() == 0
    assert fake.sent_texts() == []
    assert (await state.get_outbox(status="pending_review"))


@pytest.mark.asyncio
async def test_a_stranger_button_tap_is_answered_but_ignored(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    p, item_id = await _seed_outbox(state)
    copilot = _copilot(state, fake)
    fake.push(_callback(f"approve:{item_id}", chat=999))
    await copilot.poll()
    # Answered (the client's spinner stops) but nothing was approved.
    assert fake.answered_callbacks() and "Not authorized." in fake.answered_callbacks()[0]["text"]
    assert (await state.get_outbox(status="pending_review"))


# ── commands ──


@pytest.mark.asyncio
async def test_status_reports_the_pipeline(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    p, _ = await _seed_outbox(state)
    copilot = _copilot(state, fake)
    fake.push(_message("/status"))
    await copilot.poll()
    text = fake.sent_texts()[0]
    assert "Sellbird status:" in text
    assert "outbox waiting: 1" in text


@pytest.mark.asyncio
async def test_approve_all_clears_the_outbox(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    p, _ = await _seed_outbox(state)
    copilot = _copilot(state, fake)
    fake.push(_message("/approve all"))
    await copilot.poll()
    assert "Approved 1" in fake.sent_texts()[0]
    assert not (await state.get_outbox(status="pending_review"))


@pytest.mark.asyncio
async def test_approve_by_id_and_reject_by_id(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    p, item_id = await _seed_outbox(state)
    copilot = _copilot(state, fake)

    fake.push(_message(f"/approve {item_id}"))
    await copilot.poll()
    assert "Approved." in fake.sent_texts()[0]
    assert not (await state.get_outbox(status="pending_review"))

    # A second item: reject it by id.
    p2, item2 = await _seed_outbox(state, email="eli@harbor.example")
    fake.push(_message(f"/reject {item2}"))
    await copilot.poll()
    assert "Rejected." in fake.sent_texts()[-1]
    assert not (await state.get_outbox(status="pending_review"))


@pytest.mark.asyncio
async def test_an_unknown_id_is_reported_not_crashed(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    copilot = _copilot(state, fake)
    fake.push(_message("/approve no-such-id"))
    await copilot.poll()
    assert "Not found" in fake.sent_texts()[0]


# ── /edit ──


@pytest.mark.asyncio
async def test_edit_rewrites_a_waiting_email(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    p, item_id = await _seed_outbox(state)
    copilot = _copilot(state, fake)

    fake.push(_message(f"/edit {item_id} Hi Dana, new text here."))
    await copilot.poll()
    assert "Edited." in fake.sent_texts()[0]
    item = await state.get_outbox_item(item_id)
    assert item["body"] == "Hi Dana, new text here."
    # Still waiting: editing never approves.
    assert item["status"] == "pending_review"


@pytest.mark.asyncio
async def test_edit_only_touches_a_waiting_email(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    p, item_id = await _seed_outbox(state)
    await state.update_outbox_item(item_id, status="approved")
    copilot = _copilot(state, fake)

    fake.push(_message(f"/edit {item_id} this should not land"))
    await copilot.poll()
    assert "Can't edit" in fake.sent_texts()[0]
    assert (await state.get_outbox_item(item_id))["body"] == "Hi Dana,"


@pytest.mark.asyncio
async def test_edit_without_arguments_shows_usage(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    copilot = _copilot(state, fake)
    fake.push(_message("/edit"))
    await copilot.poll()
    assert "Usage: /edit" in fake.sent_texts()[0]


@pytest.mark.asyncio
async def test_edit_caps_a_huge_paste(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    p, item_id = await _seed_outbox(state)
    copilot = _copilot(state, fake)
    fake.push(_message(f"/edit {item_id} " + "x" * 5000))
    await copilot.poll()
    assert len((await state.get_outbox_item(item_id))["body"]) == 2000


@pytest.mark.asyncio
async def test_the_bot_name_suffix_is_accepted(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    copilot = _copilot(state, fake)
    fake.push(_message("/status@sellbird_bot"))
    await copilot.poll()
    assert "Sellbird status:" in fake.sent_texts()[0]


# ── inline buttons ──


@pytest.mark.asyncio
async def test_a_button_tap_approves_the_email(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    p, item_id = await _seed_outbox(state)
    copilot = _copilot(state, fake)
    fake.push(_callback(f"approve:{item_id}"))
    await copilot.poll()
    assert not (await state.get_outbox(status="pending_review"))
    assert fake.answered_callbacks()[0]["text"] == "Approved."


@pytest.mark.asyncio
async def test_a_button_card_carries_short_callback_data(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    copilot = _copilot(state, fake)
    ok = await copilot.notify_with_buttons("Email waiting", "abc123", "To dana@…")
    assert ok
    method, payload = fake.calls[-1]
    assert method == "sendMessage"
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "approve:abc123"
    assert buttons[1]["callback_data"] == "reject:abc123"
    for b in buttons:
        assert len(b["callback_data"]) <= 64


# ── unconfigured / robustness ──


@pytest.mark.asyncio
async def test_an_unconfigured_bot_polls_nothing(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()
    copilot = TelegramCopilot(state, None, transport=httpx.MockTransport(fake.handler))
    assert await copilot.poll() == 0
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_bad_api_response_is_survived(tmp_path):
    state, fake = await _state(tmp_path), FakeTelegram()

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False})

    copilot = TelegramCopilot(state, _cfg(), transport=httpx.MockTransport(broken))
    fake.push(_message("/status"))
    assert await copilot.poll() == 0
