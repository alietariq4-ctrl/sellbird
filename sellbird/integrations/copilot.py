"""Operator Copilot Alerts — Instant Telegram Bot API & Slack Incoming Webhook notifications.

Notifies the operator in real time when high-priority pipeline events happen:
  - Hot / interested replies & accepted offers
  - Auto-booked meetings
  - Escalations (angry/legal replies needing human review)
  - Sending pauses / bounce kill-switch triggers

Two-way Telegram copilot: the same bot answers commands (/status, /approve,
/reject) and offers inline Approve/Reject buttons on waiting emails, polled
once per heartbeat cycle (the dashboard is loopback-only, so a webhook would
need a tunnel; polling adds at most one cycle of latency and no exposure).
Only the configured chat may command the bot — every other chat is ignored.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("sellbird.copilot")

TELEGRAM_API_BASE = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(connect=8.0, read=12.0, write=8.0, pool=8.0)
# Long polling: the read timeout must exceed the poll timeout we ask for.
POLL_TIMEOUT = httpx.Timeout(connect=8.0, read=35.0, write=8.0, pool=8.0)
LONG_POLL_SECONDS = 25


class OperatorCopilot:
    """Delivers real-time alerts to Telegram and/or Slack."""

    def __init__(self, config=None, env=None, *, transport=None):
        self.config = config
        self.env = env
        self._transport = transport

    def _val(self, attr: str, env_var: str, default: str = "") -> str:
        cop_cfg = getattr(self.config, "copilot", None)
        v = (
            (str(getattr(cop_cfg, attr, "") or "") if cop_cfg is not None else "")
            or (str(getattr(self.env, attr, "") or "") if self.env is not None else "")
            or os.getenv(env_var, "")
        ).strip()
        return v or default

    @property
    def telegram_bot_token(self) -> str:
        return self._val("telegram_bot_token", "TELEGRAM_BOT_TOKEN")

    @property
    def telegram_chat_id(self) -> str:
        return self._val("telegram_chat_id", "TELEGRAM_CHAT_ID")

    @property
    def slack_webhook_url(self) -> str:
        return self._val("slack_webhook_url", "SLACK_WEBHOOK_URL")

    def is_configured(self) -> bool:
        return bool(
            (self.telegram_bot_token and self.telegram_chat_id)
            or self.slack_webhook_url
        )

    def _client(self) -> httpx.AsyncClient:
        extra = {"transport": self._transport} if self._transport is not None else {}
        return httpx.AsyncClient(timeout=TIMEOUT, **extra)

    @staticmethod
    def _format_text(event_type: str, title: str, message: str = "", details: dict | None = None) -> str:
        tag = f"[{event_type.upper()}]" if event_type else "[SELLBIRD]"
        lines = [f"{tag} {title}".strip()]
        if message:
            lines.append(message.strip())
        for k, v in (details or {}).items():
            if v is not None and str(v).strip():
                lines.append(f"• {k}: {str(v).strip()[:300]}")
        return "\n".join(lines)[:3800]

    async def send_telegram(self, text: str) -> bool:
        token, chat_id = self.telegram_bot_token, self.telegram_chat_id
        if not (token and chat_id):
            return False
        url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
        async with self._client() as client:
            resp = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
        return resp.status_code == 200

    async def send_slack(self, text: str) -> bool:
        webhook = self.slack_webhook_url
        if not webhook:
            return False
        async with self._client() as client:
            resp = await client.post(
                webhook,
                json={"text": text},
            )
        return 200 <= resp.status_code < 300

    async def notify(
        self,
        event_type: str,
        title: str,
        message: str = "",
        *,
        details: dict | None = None,
    ) -> dict:
        if not self.is_configured():
            return {"ok": True, "channels": [], "errors": []}

        text = self._format_text(event_type, title, message, details)
        channels: list[str] = []
        errors: list[str] = []

        if self.telegram_bot_token and self.telegram_chat_id:
            try:
                if await self.send_telegram(text):
                    channels.append("telegram")
                else:
                    errors.append("telegram delivery failed")
            except Exception as exc:
                logger.warning("Copilot Telegram alert failed: %s", exc)
                errors.append(f"telegram: {exc}")

        if self.slack_webhook_url:
            try:
                if await self.send_slack(text):
                    channels.append("slack")
                else:
                    errors.append("slack delivery failed")
            except Exception as exc:
                logger.warning("Copilot Slack alert failed: %s", exc)
                errors.append(f"slack: {exc}")

        return {"ok": not errors, "channels": channels, "errors": errors}


async def notify_operator(
    event_type: str,
    title: str,
    message: str = "",
    *,
    details: dict | None = None,
    config=None,
    env=None,
    transport=None,
) -> dict:
    """Non-blocking operator alert helper — never raises."""
    try:
        copilot = OperatorCopilot(config=config, env=env, transport=transport)
        return await copilot.notify(event_type, title, message, details=details)
    except Exception as exc:
        logger.debug("notify_operator error: %s", exc)
        return {"ok": False, "channels": [], "errors": [str(exc)]}


# ── Two-way Telegram copilot ──


class TelegramCopilot:
    """Answers operator commands and button taps, polled once per cycle.

    The bot token grants full read/send, and Telegram has no built-in
    operator auth, so the configured TELEGRAM_CHAT_ID is the allowlist:
    messages and button taps from any other chat are read and dropped.
    """

    def __init__(self, state, config=None, env=None, *, transport=None, brain=None):
        self.state = state
        self.config = config
        self.env = env
        self._transport = transport
        self.brain = brain
        # Telegram confirms updates only when a higher offset is sent, and a
        # new instance is built every cycle — so the offset persists in
        # settings. Without that, every poll replays the last unconfirmed
        # commands (/approve, /reject, /edit) again and again.
        self._offset = 0

    async def load_offset(self):
        try:
            self._offset = int(await self.state.get_setting("telegram.offset", "0") or 0)
        except Exception:
            self._offset = 0

    async def _save_offset(self):
        try:
            await self.state.set_setting("telegram.offset", str(self._offset))
        except Exception:
            pass

    def _val(self, attr: str, env_var: str, default: str = "") -> str:
        cop_cfg = getattr(self.config, "copilot", None)
        v = (
            (str(getattr(cop_cfg, attr, "") or "") if cop_cfg is not None else "")
            or (str(getattr(self.env, attr, "") or "") if self.env is not None else "")
            or os.getenv(env_var, "")
        ).strip()
        return v or default

    @property
    def token(self) -> str:
        return self._val("telegram_bot_token", "TELEGRAM_BOT_TOKEN")

    @property
    def chat_id(self) -> str:
        return self._val("telegram_chat_id", "TELEGRAM_CHAT_ID")

    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _client(self, *, poll: bool = False) -> httpx.AsyncClient:
        extra = {"transport": self._transport} if self._transport is not None else {}
        return httpx.AsyncClient(timeout=POLL_TIMEOUT if poll else TIMEOUT, **extra)

    async def _api(self, method: str, payload: dict, *, poll: bool = False) -> dict:
        """One Bot API call. Returns {} on any failure — never raises."""
        try:
            async with self._client(poll=poll) as client:
                resp = await client.post(
                    f"{TELEGRAM_API_BASE}/bot{self.token}/{method}", json=payload)
            if resp.status_code != 200:
                logger.debug("Telegram %s -> HTTP %s", method, resp.status_code)
                return {}
            body = resp.json()
            return body.get("result") if body.get("ok") else {}
        except Exception as exc:
            logger.debug("Telegram %s failed: %s", method, exc)
            return {}

    def _allowed(self, chat) -> bool:
        """The allowlist: numeric chat ids compared as strings."""
        try:
            return str(int(chat)) == str(int(self.chat_id))
        except (TypeError, ValueError):
            return False

    # ── polling ──

    async def poll(self) -> int:
        """Fetch and handle one batch of updates. Returns commands handled."""
        if not self.is_configured():
            return 0
        await self.load_offset()
        result = await self._api(
            "getUpdates",
            {"offset": self._offset, "limit": 20, "timeout": LONG_POLL_SECONDS,
             "allowed_updates": ["message", "callback_query"]},
            poll=True,
        )
        if not isinstance(result, list):
            return 0
        start_offset = self._offset
        handled = 0
        for update in result:
            try:
                self._offset = int(update.get("update_id", 0)) + 1
                if await self._handle(update):
                    handled += 1
            except Exception as exc:
                logger.debug("Telegram update handling failed: %s", exc)
        if self._offset != start_offset:
            await self._save_offset()
        return handled

    async def _handle(self, update: dict) -> bool:
        if "callback_query" in update:
            return await self._on_callback(update["callback_query"])
        if "message" in update and update["message"].get("text"):
            return await self._on_message(update["message"])
        return False

    # ── commands ──

    async def _on_message(self, message: dict) -> bool:
        chat = (message.get("chat") or {}).get("id")
        if not self._allowed(chat):
            return False  # read and dropped: never answer another chat
        text = str(message.get("text") or "").strip()
        if not text:
            return False
        if not text.startswith("/"):
            if self.brain is None:
                return False
            try:
                from sellbird.agents.copilot_agent import SellbirdCopilotAgent
                res = await SellbirdCopilotAgent(
                    self.brain, self.state, self.config, self.env
                ).ask(text)
                answer = str((res or {}).get("answer") or "").strip()
                await self._send(
                    answer or "I couldn't gather enough pipeline data to answer that yet."
                )
                return True
            except Exception as exc:
                logger.debug("Telegram natural-language copilot error: %s", exc)
                return False

        command, _, arg = text.partition(" ")
        command = command.split("@")[0].lower()  # /approve@the_bot -> /approve
        arg = arg.strip()

        if command == "/status":
            await self._send(await self._status_text())
            return True
        if command == "/approve":
            await self._approve(arg)
            return True
        if command == "/reject":
            await self._reject(arg)
            return True
        if command == "/edit":
            await self._edit(text)
            return True
        if command in ("/help", "/start"):
            await self._send(self._help_text())
            return True
        return False

    async def _on_callback(self, query: dict) -> bool:
        """An inline button tap. Always answered (the client shows a spinner
        until answerCallbackQuery), but only the operator's chat acts."""
        query_id = str(query.get("id") or "")
        from_user = (query.get("from") or {}).get("id")
        data = str(query.get("data") or "")
        answer_note = ""

        if not self._allowed(from_user):
            await self._api("answerCallbackQuery",
                            {"callback_query_id": query_id, "text": "Not authorized."})
            return False

        command, _, arg = data.partition(":")
        if command == "approve":
            answer_note = await self._approve(arg.strip(), quiet=True)
        elif command == "reject":
            answer_note = await self._reject(arg.strip(), quiet=True)
        else:
            answer_note = "Unknown button."
        await self._api("answerCallbackQuery",
                        {"callback_query_id": query_id, "text": answer_note[:200]})
        return True

    # ── actions ──

    async def _approve(self, arg: str, *, quiet: bool = False) -> str:
        if arg.lower() == "all":
            n = await self.state.approve_outbox(None)
            note = f"Approved {n} email(s)." if n else "Nothing was waiting."
        elif arg:
            n = await self.state.approve_outbox(arg)
            note = "Approved." if n else "Not found, or not waiting for review."
        else:
            note = "Usage: /approve <id> or /approve all"
        if not quiet:
            await self._send(note)
        return note

    async def _reject(self, arg: str, *, quiet: bool = False) -> str:
        if not arg:
            note = "Usage: /reject <id>"
        else:
            n = await self.state.reject_outbox_item(arg)
            note = "Rejected." if n else "Not found, or already on its way."
        if not quiet:
            await self._send(note)
        return note

    async def _edit(self, text: str) -> str:
        """/edit <id> <new body> — rewrite a waiting email before approving.

        Only a pending_review item can be edited: once approved it is on its
        way, and a tap's callback_data is too short to carry a body anyway,
        so this is message-only. The body is capped where the Writer caps
        its own drafts (2000 chars), so a paste can't stage something the
        send gate would have to catch later.
        """
        _, _, rest = text.partition(" ")
        item_id, _, body = rest.partition(" ")
        item_id = item_id.strip()
        body = body.strip()
        if not item_id or not body:
            note = "Usage: /edit <id> <new text>"
        else:
            item = await self.state.get_outbox_item(item_id)
            if item is None:
                note = "Not found."
            elif item.get("status") != "pending_review":
                note = f"Can't edit: that email is {item.get('status')}."
            else:
                changed = await self.state.edit_outbox_draft(
                    item_id, item.get("subject") or "", body[:2000]
                )
                if changed:
                    note = "Edited. Approve it with /approve " + item_id
                else:
                    latest = await self.state.get_outbox_item(item_id)
                    if latest is None:
                        note = "Not found."
                    else:
                        note = f"Can't edit: that email is {latest.get('status')}."
        await self._send(note)
        return note

    async def _send(self, text: str, reply_markup: dict | None = None) -> bool:
        payload = {"chat_id": self.chat_id, "text": text[:4000],
                   "disable_web_page_preview": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return bool(await self._api("sendMessage", payload))

    async def _status_text(self) -> str:
        try:
            summary = await self.state.get_state_summary()
        except Exception as exc:
            return f"Status unavailable: {exc}"
        counts = summary.get("prospects") or {}
        lines = ["Sellbird status:"]
        for key in ("new", "queued", "contacted", "replied", "meeting", "opted_out"):
            n = int(counts.get(key, 0) or 0)
            if n:
                lines.append(f"  {key}: {n}")
        waiting = len(await self.state.get_outbox(status="pending_review", limit=500))
        lines.append(f"  outbox waiting: {waiting}")
        if summary.get("sending_paused"):
            lines.append("  sending: PAUSED")
        return "\n".join(lines)[:4000]

    @staticmethod
    def _help_text() -> str:
        return ("Sellbird copilot commands:\n"
                "/status — pipeline summary\n"
                "/approve <id> — approve one waiting email\n"
                "/approve all — approve everything waiting\n"
                "/reject <id> — reject one waiting email\n"
                "/edit <id> <text> — rewrite a waiting email\n"
                "/help — this message")

    # ── notifications with buttons ──

    async def notify_with_buttons(self, title: str, item_id: str,
                                  preview: str = "") -> bool:
        """Push an outbox card with Approve/Reject buttons (callback_data is
        'approve:<id>' / 'reject:<id>', both under the 64-byte limit)."""
        text = f"{title}\n{preview}".strip()[:4000]
        markup = {"inline_keyboard": [[
            {"text": "Approve", "callback_data": f"approve:{item_id}"},
            {"text": "Reject", "callback_data": f"reject:{item_id}"},
        ]]}
        return await self._send(text, reply_markup=markup)


async def poll_telegram_commands(state, config=None, env=None, transport=None, *, brain=None) -> int:
    """One poll per heartbeat cycle. Never raises."""
    try:
        copilot = TelegramCopilot(state, config=config, env=env, transport=transport, brain=brain)
        return await copilot.poll()
    except Exception as exc:
        logger.debug("poll_telegram_commands error: %s", exc)
        return 0
