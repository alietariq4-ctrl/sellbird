"""Operator Copilot Alerts — Instant Telegram Bot API & Slack Incoming Webhook notifications.

Notifies the operator in real time when high-priority pipeline events happen:
  - Hot / interested replies & accepted offers
  - Auto-booked meetings
  - Escalations (angry/legal replies needing human review)
  - Sending pauses / bounce kill-switch triggers
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("sellbird.copilot")

TELEGRAM_API_BASE = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(connect=8.0, read=12.0, write=8.0, pool=8.0)


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
