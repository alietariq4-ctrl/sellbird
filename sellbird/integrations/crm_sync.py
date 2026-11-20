"""CRM & Webhook Sync — Twenty CRM REST API + Generic Signed JSON Webhooks.

Pushes hot leads, positive replies, accepted offers, and booked meetings to:
  - Twenty CRM (``TWENTY_API_KEY`` + ``TWENTY_BASE_URL``, default ``https://api.twenty.com``)
  - Outbound JSON Webhooks (``WEBHOOK_URL`` / ``CRM_WEBHOOK_URL``, optional ``WEBHOOK_SECRET``)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("sellbird.crm_sync")

DEFAULT_TWENTY_URL = "https://api.twenty.com"
TIMEOUT = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)

OPPORTUNITY_EVENTS = frozenset({
    "interested", "accepted_offer", "offer_accepted", "meeting_booked", "closing", "positive_reply",
})


def _extract_prospect_dict(prospect, company=None) -> dict:
    if prospect is None:
        return {}
    if isinstance(prospect, dict):
        p = dict(prospect)
    else:
        p = {
            "id": getattr(prospect, "id", ""),
            "first_name": getattr(prospect, "first_name", ""),
            "last_name": getattr(prospect, "last_name", ""),
            "email": getattr(prospect, "email", ""),
            "title": getattr(prospect, "title", ""),
            "company": getattr(prospect, "company", ""),
            "industry": getattr(prospect, "industry", ""),
            "status": getattr(prospect, "status", ""),
            "linkedin_url": getattr(prospect, "linkedin_url", ""),
        }
    if company is not None:
        if isinstance(company, dict):
            p["company_domain"] = company.get("domain", "")
            p["company_location"] = company.get("location", "")
            p["company_website"] = company.get("website", "")
        else:
            p["company_domain"] = getattr(company, "domain", "")
            p["company_location"] = getattr(company, "location", "")
            p["company_website"] = getattr(company, "website", "")
    return p


class CRMSyncClient:
    """Syncs lead lifecycle events to Twenty CRM and/or generic JSON webhooks."""

    def __init__(self, config=None, env=None, *, transport=None):
        self.config = config
        self.env = env
        self._transport = transport

    def _val(self, attr: str, env_var: str, default: str = "") -> str:
        crm_cfg = getattr(self.config, "crm", None)
        v = (
            (str(getattr(crm_cfg, attr, "") or "") if crm_cfg is not None else "")
            or (str(getattr(self.env, attr, "") or "") if self.env is not None else "")
            or os.getenv(env_var, "")
        ).strip()
        return v or default

    @property
    def twenty_api_key(self) -> str:
        return self._val("twenty_api_key", "TWENTY_API_KEY")

    @property
    def twenty_base_url(self) -> str:
        return self._val("twenty_base_url", "TWENTY_BASE_URL", DEFAULT_TWENTY_URL).rstrip("/")

    @property
    def webhook_url(self) -> str:
        return self._val("webhook_url", "WEBHOOK_URL") or self._val("crm_webhook_url", "CRM_WEBHOOK_URL")

    @property
    def webhook_secret(self) -> str:
        return self._val("webhook_secret", "WEBHOOK_SECRET")

    def is_configured(self) -> bool:
        return bool(self.twenty_api_key or self.webhook_url)

    def _client(self) -> httpx.AsyncClient:
        extra = {"transport": self._transport} if self._transport is not None else {}
        return httpx.AsyncClient(timeout=TIMEOUT, **extra)

    async def sync_twenty(self, event_type: str, lead: dict, details: dict | None = None) -> dict:
        """Create/sync Person (and Opportunity on high-intent events) in Twenty CRM REST API."""
        api_key = self.twenty_api_key
        if not api_key:
            return {"ok": False, "skipped": True}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        person_payload: dict = {
            "name": {
                "firstName": lead.get("first_name") or (lead.get("email") or "").split("@")[0],
                "lastName": lead.get("last_name") or "",
            },
            "emails": {"primaryEmail": lead.get("email") or ""},
            "jobTitle": lead.get("title") or "",
        }
        if lead.get("company_location"):
            person_payload["city"] = str(lead["company_location"])[:80]

        person_id = ""
        async with self._client() as client:
            resp = await client.post(
                f"{self.twenty_base_url}/rest/people",
                headers=headers,
                json=person_payload,
            )
            if resp.status_code >= 400:
                return {"ok": False, "error": f"Twenty /rest/people HTTP {resp.status_code}"}
            try:
                body = resp.json() or {}
                data_node = body.get("data") if isinstance(body, dict) else {}
                if isinstance(data_node, dict):
                    person_obj = data_node.get("createPerson") or data_node
                    if isinstance(person_obj, dict):
                        person_id = str(person_obj.get("id") or "")
            except ValueError:
                pass

            opp_created = False
            if event_type in OPPORTUNITY_EVENTS:
                opp_name = f"{lead.get('company') or lead.get('email') or 'Lead'} — {event_type}"
                stage_map = {
                    "meeting_booked": "MEETING",
                    "closing": "MEETING",
                    "accepted_offer": "PROPOSAL",
                    "offer_accepted": "PROPOSAL",
                }
                opp_payload: dict = {
                    "name": opp_name[:120],
                    "stage": stage_map.get(event_type, "NEW"),
                }
                if person_id:
                    opp_payload["pointOfContactId"] = person_id
                opp_resp = await client.post(
                    f"{self.twenty_base_url}/rest/opportunities",
                    headers=headers,
                    json=opp_payload,
                )
                opp_created = opp_resp.status_code < 400

        return {"ok": True, "person_id": person_id, "opportunity_created": opp_created}

    async def sync_webhook(self, event_type: str, lead: dict, details: dict | None = None) -> dict:
        """POST a structured JSON event to WEBHOOK_URL with optional HMAC-SHA256 signature."""
        url = self.webhook_url
        if not url:
            return {"ok": False, "skipped": True}

        envelope = {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "lead": lead,
            "details": details or {},
        }
        raw_bytes = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "Sellbird-Webhook/1.0"}
        if self.webhook_secret:
            sig = hmac.new(self.webhook_secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
            headers["X-Sellbird-Signature"] = f"sha256={sig}"

        async with self._client() as client:
            resp = await client.post(url, content=raw_bytes, headers=headers)
        if resp.status_code >= 400:
            return {"ok": False, "error": f"Webhook HTTP {resp.status_code}"}
        return {"ok": True, "status_code": resp.status_code}

    async def sync_event(
        self,
        event_type: str,
        *,
        prospect=None,
        company=None,
        details: dict | None = None,
    ) -> dict:
        if not self.is_configured():
            return {"ok": True, "synced": [], "errors": []}

        lead = _extract_prospect_dict(prospect, company)
        synced: list[str] = []
        errors: list[str] = []

        if self.twenty_api_key:
            try:
                res = await self.sync_twenty(event_type, lead, details)
                if res.get("ok"):
                    synced.append("twenty")
                elif not res.get("skipped"):
                    errors.append(str(res.get("error") or "twenty failed"))
            except Exception as exc:
                logger.warning("CRM sync (Twenty) failed: %s", exc)
                errors.append(f"twenty: {exc}")

        if self.webhook_url:
            try:
                res = await self.sync_webhook(event_type, lead, details)
                if res.get("ok"):
                    synced.append("webhook")
                elif not res.get("skipped"):
                    errors.append(str(res.get("error") or "webhook failed"))
            except Exception as exc:
                logger.warning("CRM sync (Webhook) failed: %s", exc)
                errors.append(f"webhook: {exc}")

        return {"ok": not errors, "synced": synced, "errors": errors}


async def sync_lead_event(
    event_type: str,
    *,
    prospect=None,
    company=None,
    details: dict | None = None,
    config=None,
    env=None,
    transport=None,
) -> dict:
    """Convenience helper that never raises — safe inside Handler's hot loop."""
    try:
        client = CRMSyncClient(config=config, env=env, transport=transport)
        return await client.sync_event(
            event_type, prospect=prospect, company=company, details=details
        )
    except Exception as exc:
        logger.debug("sync_lead_event error: %s", exc)
        return {"ok": False, "synced": [], "errors": [str(exc)]}
