"""The inbound Cal.com booking webhook (POST /api/webhooks/calcom).

A booking made on the operator's calendar should stop the sequence at once,
without waiting for the next reply poll. The route is public in the guard
(Cal.com cannot carry the dashboard token) and must authenticate with its
own shared secret: an HMAC-SHA256 of the raw body. Fail closed — no secret
configured, no bookings accepted.

All fixtures are fictional (example.test domains, invented people).
"""

import hashlib
import hmac
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.models.company import Company
from sellbird.models.conversation import Conversation, Message
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
SECRET = "test-webhook-secret"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "sellbird.db")
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "sellbird.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "sellbird.log")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    monkeypatch.setenv("CALCOM_WEBHOOK_SECRET", SECRET)
    return {"dir": tmp_path}


def _client():
    return TestClient(dash.app, base_url=ORIGIN)


def _sign(raw: bytes) -> str:
    return hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _booking_payload(email="dana@harbor.example"):
    return {
        "trigger": "booking_created",
        "booking": {
            "title": "15 minute meeting",
            "start": "2026-11-25T14:00:00Z",
            "attendees": [{"name": "Dana Reed", "email": email}],
        },
    }


async def _seed_prospect(tmp_path, email="dana@harbor.example"):
    state = StateManager(str(tmp_path / "data" / "sellbird.db"))
    await state.init_db()
    company = Company(name="Harbor Studio", domain="harbor.example")
    company.id = await state.add_company(company)
    prospect = Prospect(first_name="Dana", last_name="Reed", email=email,
                        company="Harbor Studio", company_id=company.id,
                        title="Owner", status="replied")
    prospect.id = await state.add_prospect(prospect)
    convo = Conversation(id="", prospect_id=prospect.id, channel="email",
                         thread=[Message(sender="prospect",
                                         content="Tuesday works.")])
    convo.id = await state.add_conversation(convo)
    # A follow-up still queued: the webhook must cancel it.
    await state.add_outbox_item(
        prospect_id=prospect.id, to_email=email,
        subject="Re: quick question", body="Following up.",
        send_at="2026-11-21T14:00:00",
        step=2, kind="sequence", status="pending_review")
    return state, prospect.id, convo.id


def _post(payload, *, secret=SECRET, header="x-cal-signature-256"):
    raw = json.dumps(payload).encode()
    headers = {}
    if secret is not None:
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        headers[header] = sig
    with _client() as c:
        return c.post("/api/webhooks/calcom", content=raw, headers=headers)


# ── authentication: fail closed ──


def test_without_a_secret_configured_no_booking_is_accepted(tmp_path, monkeypatch):
    monkeypatch.delenv("CALCOM_WEBHOOK_SECRET", raising=False)
    r = _post(_booking_payload(), secret=None)
    assert r.status_code == 503
    assert r.json()["error"] == "not_configured"


def test_a_wrong_signature_is_refused(env):
    r = _post(_booking_payload(), secret="not-the-secret")
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_signature"


def test_a_missing_signature_is_refused(env):
    r = _post(_booking_payload(), secret=None)
    assert r.status_code == 401


def test_the_signature_is_checked_against_the_raw_body_not_a_reparse(env):
    # Whitespace differences between what was signed and what arrives must
    # change the outcome: the HMAC covers the exact bytes Cal.com sent.
    raw = json.dumps(_booking_payload()).encode()
    sig = _sign(raw)
    with _client() as c:
        r = c.post("/api/webhooks/calcom",
                   content=json.dumps(_booking_payload(), indent=2).encode(),
                   headers={"x-cal-signature-256": sig})
    assert r.status_code == 401


def test_the_cal_signature_header_name_is_also_accepted(env):
    r = _post(_booking_payload(), header="x-cal-signature")
    assert r.status_code == 200


def test_malformed_json_with_a_valid_signature_is_rejected(env):
    raw = b"not json"
    sig = _sign(raw)
    with _client() as c:
        r = c.post("/api/webhooks/calcom", content=raw,
                   headers={"x-cal-signature-256": sig})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_json"


# ── matching and effects ──


@pytest.mark.asyncio
async def test_a_booking_stops_the_sequence_and_marks_the_meeting(env):
    state, pid, cid = await _seed_prospect(env["dir"])
    r = _post(_booking_payload())
    assert r.status_code == 200
    assert r.json()["matched"] is True
    assert r.json()["cancelled_emails"] == 1

    prospect = await state.get_prospect(pid)
    assert prospect.status == "meeting"
    convo = await state.get_conversation(cid)
    assert convo.status == "meeting_booked"
    remaining = await state.get_outbox(status="pending_review")
    assert remaining == []


@pytest.mark.asyncio
async def test_a_booking_for_a_stranger_is_acknowledged_and_ignored(env):
    await _seed_prospect(env["dir"])
    r = _post(_booking_payload(email="someone@other.example"))
    assert r.status_code == 200
    assert r.json()["matched"] is False


@pytest.mark.asyncio
async def test_a_payload_without_an_attendee_email_is_rejected(env):
    payload = _booking_payload()
    payload["booking"]["attendees"] = []
    r = _post(payload)
    assert r.status_code == 400
    assert r.json()["error"] == "no_attendee_email"


@pytest.mark.asyncio
async def test_a_non_booking_trigger_changes_nothing(env):
    state, pid, cid = await _seed_prospect(env["dir"])
    payload = _booking_payload()
    payload["trigger"] = "booking_cancelled"
    r = _post(payload)
    assert r.status_code == 200
    assert r.json()["handled"] is False
    prospect = await state.get_prospect(pid)
    assert prospect.status == "replied"
    assert (await state.get_outbox(status="pending_review"))


@pytest.mark.asyncio
async def test_the_booking_is_recorded_in_the_activity_log(env):
    state, pid, cid = await _seed_prospect(env["dir"])
    _post(_booking_payload())
    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT action_type, agent, details_json FROM actions "
            "WHERE action_type = 'meeting_booked' ORDER BY created_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
    assert row is not None
    assert row["agent"] == "calcom_webhook"
    details = json.loads(row["details_json"])
    assert details["prospect_id"] == pid
    assert details["cancelled_emails"] == 1
