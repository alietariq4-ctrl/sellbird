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


class _CrmBase:
    """The plumbing every CRM client here shares: config/env resolution and
    an httpx client. Subclasses name their own credentials."""

    def __init__(self, config=None, env=None, *, transport=None):
        self.config = config
        self.env = env
        self._transport = transport

    def _crm(self, attr: str, default=None):
        crm_cfg = getattr(self.config, "crm", None)
        if crm_cfg is None:
            return default
        v = getattr(crm_cfg, attr, default)
        return default if v is None else v

    def _val(self, attr: str, env_var: str, default: str = "") -> str:
        crm_cfg = getattr(self.config, "crm", None)
        v = (
            (str(getattr(crm_cfg, attr, "") or "") if crm_cfg is not None else "")
            or (str(getattr(self.env, attr, "") or "") if self.env is not None else "")
            or os.getenv(env_var, "")
        ).strip()
        return v or default

    def _client(self) -> httpx.AsyncClient:
        extra = {"transport": self._transport} if self._transport is not None else {}
        return httpx.AsyncClient(timeout=TIMEOUT, **extra)


class CRMSyncClient(_CrmBase):
    """Syncs lead lifecycle events to Twenty CRM and/or generic JSON webhooks."""

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


# ── Inbound: pull do-not-contact people from Twenty into suppressions ──

TWENTY_PAGE_SIZE = 60  # Twenty's default and a safe page size


class TwentyInboundSync(_CrmBase):
    """Reads people marked do-not-contact in Twenty and suppresses them here.

    Twenty's Person object has no native unsubscribe field, so this reads a
    custom boolean field (``crm.inbound_field``, default ``doNotContact``)
    with a server-side filter and feeds each matching person's email into
    Sellbird's suppressions (reason ``imported``). Polling, not webhooks:
    the dashboard is loopback-only. Never raises — a CRM hiccup must not
    stall a heartbeat cycle.
    """

    def __init__(self, state, config=None, env=None, *, transport=None):
        super().__init__(config, env, transport=transport)
        self.state = state

    @property
    def api_key(self) -> str:
        return self._val("twenty_api_key", "TWENTY_API_KEY")

    @property
    def base_url(self) -> str:
        return self._val("twenty_base_url", "TWENTY_BASE_URL", DEFAULT_TWENTY_URL).rstrip("/")

    @property
    def field(self) -> str:
        return str(self._crm("inbound_field", "doNotContact") or "doNotContact")

    @property
    def max_records(self) -> int:
        try:
            n = int(self._crm("inbound_max_records", 1000) or 1000)
        except (TypeError, ValueError):
            n = 1000
        return max(1, min(n, 100_000))

    def is_configured(self) -> bool:
        crm_cfg = getattr(self.config, "crm", None)
        enabled = bool(getattr(crm_cfg, "inbound_enabled", False)) if crm_cfg is not None else False
        return bool(enabled and self.api_key)

    @staticmethod
    def _emails_from_person(person: dict) -> list[str]:
        """Every address on a Twenty person, from the nested ``emails`` shape."""
        out = []
        emails = person.get("emails") if isinstance(person, dict) else None
        if isinstance(emails, dict):
            for key in ("primaryEmail", "additionalEmails"):
                val = emails.get(key)
                if isinstance(val, str) and val.strip():
                    out.append(val.strip())
                elif isinstance(val, list):
                    out.extend(str(x).strip() for x in val if str(x).strip())
        elif isinstance(emails, list):
            out.extend(str(x).strip() for x in emails if str(x).strip())
        return out

    async def fetch_do_not_contact(self) -> list[str]:
        """Emails of people whose do-not-contact field is true. [] on any error."""
        if not self.api_key:
            return []
        field = self.field
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        found: list[str] = []
        starting_after = ""
        seen_pages = 0
        max_pages = (self.max_records // TWENTY_PAGE_SIZE) + 2
        async with self._client() as client:
            while seen_pages < max_pages and len(found) < self.max_records:
                params = {
                    "limit": TWENTY_PAGE_SIZE,
                    "filter": f"{field}[eq]:true",
                    "fields": "id,emails",
                }
                if starting_after:
                    params["starting_after"] = starting_after
                try:
                    resp = await client.get(
                        f"{self.base_url}/rest/people", headers=headers, params=params)
                except Exception as exc:
                    logger.warning("Twenty inbound sync: request failed: %s", exc)
                    break
                if resp.status_code >= 400:
                    logger.warning("Twenty inbound sync: HTTP %s", resp.status_code)
                    break
                try:
                    body = resp.json() or {}
                except ValueError:
                    break
                data = body.get("data") if isinstance(body, dict) else {}
                people = data.get("people") if isinstance(data, dict) else None
                if not isinstance(people, list) or not people:
                    break
                for person in people:
                    if isinstance(person, dict):
                        found.extend(self._emails_from_person(person))
                # Cursor for the next page: the last record's id.
                last = people[-1]
                starting_after = str(last.get("id") or "") if isinstance(last, dict) else ""
                seen_pages += 1
                page_info = data.get("pageInfo") if isinstance(data, dict) else None
                if (isinstance(page_info, dict) and page_info.get("hasNextPage") is False) or not starting_after:
                    break
        return found[: self.max_records]

    async def run(self) -> int:
        """One poll: suppress every do-not-contact address. Returns how many
        were newly added. Never raises."""
        if not self.is_configured():
            return 0
        try:
            emails = await self.fetch_do_not_contact()
        except Exception as exc:
            logger.warning("Twenty inbound sync failed: %s", exc)
            return 0
        added = 0
        for email in emails:
            try:
                if await self.state.add_suppression(
                    "address", email, reason="imported", source="twenty"
                ):
                    added += 1
            except ValueError:
                continue  # not a usable address; skip it
            except Exception as exc:
                logger.debug("Twenty inbound sync: could not suppress %s: %s", email, exc)
        if added:
            logger.info("Twenty inbound sync: suppressed %s address(es) marked "
                        "do-not-contact in the CRM.", added)
        return added


# ── Outbound: push company observations into Twenty Company custom fields ──


class TwentyObservationSync(_CrmBase):
    """Writes each company's latest signal findings to its Twenty record.

    The operator names the mapping (``crm.observation_fields``:
    signal code → custom field on Twenty's Company object) because custom
    fields only exist once someone creates them in Twenty. For every
    company with a finding for a mapped code, the newest observation's
    value goes to the mapped field via ``PATCH /rest/companies/{id}``,
    matched by domain name. Never raises — a CRM hiccup must not stall a
    heartbeat cycle.
    """

    # One PATCH per company per cycle is plenty; the CRM is a mirror, not
    # a queue.
    MAX_COMPANIES_PER_RUN = 200

    def __init__(self, state, config=None, env=None, *, transport=None):
        super().__init__(config, env, transport=transport)
        self.state = state

    @property
    def api_key(self) -> str:
        return self._val("twenty_api_key", "TWENTY_API_KEY")

    @property
    def base_url(self) -> str:
        return self._val("twenty_base_url", "TWENTY_BASE_URL", DEFAULT_TWENTY_URL).rstrip("/")

    @property
    def fields(self) -> dict[str, str]:
        raw = self._crm("observation_fields", {}) or {}
        return dict(raw) if isinstance(raw, dict) else {}

    def is_configured(self) -> bool:
        crm_cfg = getattr(self.config, "crm", None)
        enabled = bool(getattr(crm_cfg, "observation_sync_enabled", False)) if crm_cfg is not None else False
        return bool(enabled and self.api_key and self.fields)

    @staticmethod
    def _value_of(observation: object):
        """The value a CRM field should carry: the number when there is one
        (a rank, a count), else the text (an agency name), else True for a
        bare boolean finding."""
        if not isinstance(observation, dict):
            return None
        num = observation.get("value_num")
        if num is not None:
            try:
                f = float(num)
            except (TypeError, ValueError):
                f = None
            if f is not None:
                return int(f) if f.is_integer() else f
        text = str(observation.get("value_text") or "").strip()
        if text:
            return text
        # No value at all: the row's existence is the finding (a boolean
        # signal recorded as checked-and-true).
        return True

    async def _find_company_id(self, client: httpx.AsyncClient, domain: str) -> str:
        """Twenty's Company record for a domain, by exact domainName match.
        '' when there is none or the lookup fails."""
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        try:
            resp = await client.get(
                f"{self.base_url}/rest/companies",
                headers=headers,
                params={"filter": f"domainName[eq]:{domain}", "limit": "1", "fields": "id"},
            )
        except Exception as exc:
            logger.debug("Twenty observation sync: company lookup failed for %s: %s", domain, exc)
            return ""
        if resp.status_code >= 400:
            logger.debug("Twenty observation sync: company lookup HTTP %s for %s",
                         resp.status_code, domain)
            return ""
        try:
            body = resp.json() or {}
        except ValueError:
            return ""
        data = body.get("data") if isinstance(body, dict) else {}
        companies = data.get("companies") if isinstance(data, dict) else None
        if isinstance(companies, list) and companies and isinstance(companies[0], dict):
            return str(companies[0].get("id") or "")
        return ""

    # Settings keys: the last values pushed per company (so unchanged
    # findings cost nothing), and Twenty's record id per domain (it never
    # changes, so the GET lookup happens once per domain, ever).
    _LAST_KEY = "twenty.obs.last."
    _ID_KEY = "twenty.obs.company_id."

    def _last_key(self, domain: str) -> str:
        return self._LAST_KEY + domain.lower()

    def _id_key(self, domain: str) -> str:
        return self._ID_KEY + domain.lower()

    async def run(self) -> int:
        """One pass: write each mapped company's latest findings to Twenty.
        Returns how many companies were updated. Never raises."""
        if not self.is_configured():
            return 0
        mapping = self.fields
        updated = 0
        try:
            companies = await self.state.get_companies_with_domains(
                limit=self.MAX_COMPANIES_PER_RUN)
        except Exception as exc:
            logger.warning("Twenty observation sync: could not read companies: %s", exc)
            return 0
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json", "Accept": "application/json"}
        async with self._client() as client:
            for company in companies:
                domain = str(getattr(company, "domain", "") or "").strip()
                if not domain:
                    continue
                try:
                    observations = await self.state.get_observations(
                        company_id=getattr(company, "id", ""), latest_only=True)
                except Exception as exc:
                    logger.debug("Twenty observation sync: observations read failed: %s", exc)
                    continue
                # What this company's record should say, per the mapping.
                patch: dict = {}
                for obs in observations:
                    code = str(obs.get("signal_code") or "").strip().upper()
                    field = mapping.get(code)
                    if not field:
                        continue
                    value = self._value_of(obs)
                    if value is None:
                        continue
                    patch[field] = value
                if not patch:
                    continue
                # Unchanged since the last push: no GET, no PATCH.
                try:
                    last = await self.state.get_setting(self._last_key(domain))
                except Exception:
                    last = ""
                if last and last == json.dumps(patch, sort_keys=True):
                    continue
                try:
                    company_id = await self._cached_company_id(client, domain)
                except Exception:
                    company_id = ""
                if not company_id:
                    continue
                try:
                    resp = await client.patch(
                        f"{self.base_url}/rest/companies/{company_id}",
                        headers=headers, json=patch)
                except Exception as exc:
                    logger.debug("Twenty observation sync: PATCH failed for %s: %s", domain, exc)
                    continue
                if resp.status_code >= 400:
                    logger.debug("Twenty observation sync: PATCH HTTP %s for %s",
                                 resp.status_code, domain)
                    continue
                try:
                    await self.state.set_setting(self._last_key(domain),
                                                 json.dumps(patch, sort_keys=True))
                except Exception:
                    pass
                updated += 1
        if updated:
            logger.info("Twenty observation sync: updated %s company record(s).", updated)
        return updated

    async def _cached_company_id(self, client: httpx.AsyncClient, domain: str) -> str:
        """Twenty's record id for a domain, looked up once and then kept in
        settings — a Twenty company id never changes."""
        try:
            cached = await self.state.get_setting(self._id_key(domain))
        except Exception:
            cached = ""
        if cached:
            return cached
        company_id = await self._find_company_id(client, domain)
        if company_id:
            try:
                await self.state.set_setting(self._id_key(domain), company_id)
            except Exception:
                pass
        return company_id
