"""Real Auto-Booking Calendar Integration — Cal.com API v2, Google Calendar & Microsoft Graph.

Backward-compatible with 0-arg ``CalendarClient()`` construction and
``get_available_slots(date_range)`` / ``book_meeting(prospect_email, slot)``
while supporting real slot querying and instant meeting creation across:
  1. Cal.com API v2 (``CALCOM_API_KEY`` + ``CALCOM_EVENT_TYPE_ID``)
  2. Google Calendar v3 FreeBusy / Events (``GOOGLE_CALENDAR_ACCESS_TOKEN`` or ``GOOGLE_CALENDAR_ID``)
  3. Microsoft Graph Calendar (``M365_CALENDAR_ACCESS_TOKEN``)
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("sellbird.calendar")

CALCOM_API_BASE = "https://api.cal.com/v2"
CALCOM_API_VERSION = "2024-09-04"
GOOGLE_CAL_BASE = "https://www.googleapis.com/calendar/v3"
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


def _parse_date_range(date_range: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Parse ISO ranges ('2026-11-18..2026-11-25'), single dates, or 'next_N_days'."""
    anchor = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw = str(date_range or "").strip()
    if not raw:
        return anchor, anchor + timedelta(days=5)

    iso_dates = re.findall(r"\d{4}-\d{2}-\d{2}", raw)
    if len(iso_dates) >= 2:
        try:
            start = datetime.fromisoformat(iso_dates[0]).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(iso_dates[1]).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            if end > start:
                return start, end
        except ValueError:
            pass
    elif len(iso_dates) == 1:
        try:
            start = datetime.fromisoformat(iso_dates[0]).replace(tzinfo=timezone.utc)
            return start, start + timedelta(days=1)
        except ValueError:
            pass

    days_match = re.search(r"(\d+)\s*d", raw.lower())
    days = int(days_match.group(1)) if days_match else 5
    days = max(1, min(days, 30))
    return anchor, anchor + timedelta(days=days)


def _generate_free_slots_from_busy(
    start_dt: datetime,
    end_dt: datetime,
    busy_ranges: list[tuple[datetime, datetime]],
    *,
    duration_minutes: int = 30,
    provider: str = "google",
    max_slots: int = 12,
) -> list[dict]:
    """Compute open weekday business-hour slots (09:00-17:00 UTC) outside busy intervals."""
    slots: list[dict] = []
    step = timedelta(minutes=duration_minutes)
    cursor = start_dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    while cursor + step <= end_dt and len(slots) < max_slots:
        if cursor.weekday() >= 5:
            cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=0)
            continue
        if cursor.hour < 9:
            cursor = cursor.replace(hour=9, minute=0)
            continue
        if cursor.hour >= 17:
            cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=0)
            continue

        slot_end = cursor + step
        conflict = any(cursor < b_end and slot_end > b_start for b_start, b_end in busy_ranges)
        if not conflict:
            slots.append({
                "start": cursor.isoformat().replace("+00:00", "Z"),
                "end": slot_end.isoformat().replace("+00:00", "Z"),
                "provider": provider,
            })
            cursor = slot_end + timedelta(minutes=30)
        else:
            cursor += step
    return slots


def format_slots_for_reply(slots: list[dict], limit: int = 3) -> str:
    """Format up to ``limit`` available slot dicts into concise prose for Handler prompts."""
    rendered: list[str] = []
    for slot in (slots or [])[:limit]:
        raw = str(slot.get("start") or "").strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            rendered.append(dt.strftime("%A %b %d at %I:%M %p %Z").replace(" 0", " "))
        except ValueError:
            rendered.append(raw)
    return "; ".join(rendered)


class CalendarClient:
    """Auto-booking calendar client supporting Cal.com v2, Google Calendar, and Microsoft Graph."""

    def __init__(self, config=None, env=None, *, transport=None):
        self.config = config
        self.env = env
        self._transport = transport

    def _cfg_or_env(self, attr: str, env_var: str, default: str = "") -> str:
        cal_cfg = getattr(self.config, "calendar", None)
        val = (
            (str(getattr(cal_cfg, attr, "") or "") if cal_cfg is not None else "")
            or (str(getattr(self.env, attr, "") or "") if self.env is not None else "")
            or os.getenv(env_var, "")
        ).strip()
        return val or default

    def _client(self) -> httpx.AsyncClient:
        extra = {"transport": self._transport} if self._transport is not None else {}
        return httpx.AsyncClient(timeout=TIMEOUT, **extra)

    @property
    def backend(self) -> str:
        explicit = self._cfg_or_env("provider", "CALENDAR_PROVIDER").lower()
        if explicit in ("calcom", "cal.com", "google", "microsoft"):
            return "calcom" if explicit == "cal.com" else explicit
        if self._cfg_or_env("calcom_api_key", "CALCOM_API_KEY"):
            return "calcom"
        if self._cfg_or_env("google_calendar_access_token", "GOOGLE_CALENDAR_ACCESS_TOKEN"):
            return "google"
        if self._cfg_or_env("m365_calendar_access_token", "M365_CALENDAR_ACCESS_TOKEN"):
            return "microsoft"
        return ""

    def is_configured(self) -> bool:
        b = self.backend
        if b == "calcom":
            return bool(
                self._cfg_or_env("calcom_api_key", "CALCOM_API_KEY")
                and self._cfg_or_env("calcom_event_type_id", "CALCOM_EVENT_TYPE_ID")
            )
        if b == "google":
            return bool(self._cfg_or_env("google_calendar_access_token", "GOOGLE_CALENDAR_ACCESS_TOKEN"))
        if b == "microsoft":
            return bool(self._cfg_or_env("m365_calendar_access_token", "M365_CALENDAR_ACCESS_TOKEN"))
        return False

    async def get_available_slots(
        self,
        date_range: str = "next_5_days",
        *,
        duration_minutes: int = 30,
        timezone_name: str = "UTC",
    ) -> list[dict]:
        """Return available meeting slots across Cal.com v2, Google Calendar, or Microsoft Graph."""
        b = self.backend
        if not b or not self.is_configured():
            logger.debug("CalendarClient not configured; returning no slots.")
            return []

        start_dt, end_dt = _parse_date_range(date_range)
        try:
            if b == "calcom":
                return await self._calcom_slots(start_dt, end_dt, duration_minutes=duration_minutes, timezone_name=timezone_name)
            if b == "google":
                return await self._google_slots(start_dt, end_dt, duration_minutes=duration_minutes)
            if b == "microsoft":
                return await self._microsoft_slots(start_dt, end_dt, duration_minutes=duration_minutes)
        except Exception as exc:
            logger.warning("Calendar get_available_slots (%s) failed: %s", b, exc)
        return []

    async def book_meeting(
        self,
        prospect_email: str,
        slot: dict,
        *,
        prospect_name: str = "",
        notes: str = "",
        timezone_name: str = "UTC",
    ) -> bool:
        """Book a meeting with ``prospect_email`` at ``slot`` (must contain ``start``)."""
        if not prospect_email or not isinstance(slot, dict) or not slot.get("start"):
            logger.warning("book_meeting called with invalid arguments.")
            return False

        b = str(slot.get("provider") or self.backend or "").lower()
        if not b or not self.is_configured():
            logger.info("Calendar integration not configured; skipping auto-booking.")
            return False

        try:
            if b in ("calcom", "cal.com"):
                return await self._calcom_book(prospect_email, slot, prospect_name=prospect_name, notes=notes, timezone_name=timezone_name)
            if b == "google":
                return await self._google_book(prospect_email, slot, prospect_name=prospect_name, notes=notes)
            if b == "microsoft":
                return await self._microsoft_book(prospect_email, slot, prospect_name=prospect_name, notes=notes)
        except Exception as exc:
            logger.warning("Calendar book_meeting (%s) failed: %s", b, exc)
        return False

    # ── Cal.com API v2 ──

    async def _calcom_slots(
        self, start_dt: datetime, end_dt: datetime, *, duration_minutes: int, timezone_name: str
    ) -> list[dict]:
        api_key = self._cfg_or_env("calcom_api_key", "CALCOM_API_KEY")
        event_type_id = self._cfg_or_env("calcom_event_type_id", "CALCOM_EVENT_TYPE_ID")
        api_base = self._cfg_or_env("calcom_api_url", "CALCOM_API_URL", CALCOM_API_BASE).rstrip("/")

        params: dict[str, str | int] = {
            "start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "eventTypeId": int(event_type_id) if event_type_id.isdigit() else event_type_id,
            "timeZone": timezone_name,
        }
        async with self._client() as client:
            resp = await client.get(
                f"{api_base}/slots",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "cal-api-version": CALCOM_API_VERSION,
                },
                params=params,
            )
        if resp.status_code >= 400:
            logger.warning("Cal.com /v2/slots returned %d", resp.status_code)
            return []

        payload = resp.json()
        raw_data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(raw_data, dict) and isinstance(raw_data.get("slots"), dict):
            raw_data = raw_data["slots"]

        slots: list[dict] = []
        step = timedelta(minutes=duration_minutes)

        def _append_item(item):
            start_str = item.get("start") or item.get("time") if isinstance(item, dict) else (item if isinstance(item, str) else "")
            if not start_str:
                return
            end_str = item.get("end", "") if isinstance(item, dict) else ""
            if not end_str:
                try:
                    dt = datetime.fromisoformat(str(start_str).replace("Z", "+00:00"))
                    end_str = (dt + step).isoformat().replace("+00:00", "Z")
                except ValueError:
                    end_str = ""
            slots.append({
                "start": str(start_str),
                "end": str(end_str),
                "event_type_id": event_type_id,
                "provider": "calcom",
            })

        if isinstance(raw_data, dict):
            for day_key in sorted(raw_data):
                day_items = raw_data[day_key]
                if isinstance(day_items, list):
                    for item in day_items:
                        _append_item(item)
        elif isinstance(raw_data, list):
            for item in raw_data:
                _append_item(item)
        return slots

    async def _calcom_book(
        self, prospect_email: str, slot: dict, *, prospect_name: str, notes: str, timezone_name: str
    ) -> bool:
        api_key = self._cfg_or_env("calcom_api_key", "CALCOM_API_KEY")
        event_type_raw = str(slot.get("event_type_id") or self._cfg_or_env("calcom_event_type_id", "CALCOM_EVENT_TYPE_ID"))
        api_base = self._cfg_or_env("calcom_api_url", "CALCOM_API_URL", CALCOM_API_BASE).rstrip("/")

        event_type_id: int | str = int(event_type_raw) if event_type_raw.isdigit() else event_type_raw
        attendee_name = (prospect_name or slot.get("name") or prospect_email.split("@")[0]).strip()
        tz = str(slot.get("timeZone") or timezone_name or "UTC")

        body: dict = {
            "start": str(slot["start"]),
            "eventTypeId": event_type_id,
            "attendee": {
                "name": attendee_name,
                "email": prospect_email.strip(),
                "timeZone": tz,
                "language": "en",
            },
        }
        if notes:
            body["metadata"] = {"notes": notes[:500]}

        async with self._client() as client:
            resp = await client.post(
                f"{api_base}/bookings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "cal-api-version": CALCOM_API_VERSION,
                    "Content-Type": "application/json",
                },
                json=body,
            )
        return resp.status_code in (200, 201)

    # ── Google Calendar v3 ──

    async def _google_slots(
        self, start_dt: datetime, end_dt: datetime, *, duration_minutes: int
    ) -> list[dict]:
        token = self._cfg_or_env("google_calendar_access_token", "GOOGLE_CALENDAR_ACCESS_TOKEN")
        cal_id = self._cfg_or_env("google_calendar_id", "GOOGLE_CALENDAR_ID", "primary")
        async with self._client() as client:
            resp = await client.post(
                f"{GOOGLE_CAL_BASE}/freeBusy",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "timeMin": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "timeMax": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "items": [{"id": cal_id}],
                },
            )
        if resp.status_code >= 400:
            return []
        cals = (resp.json() or {}).get("calendars") or {}
        busy_raw = (cals.get(cal_id) or {}).get("busy") or []
        busy: list[tuple[datetime, datetime]] = []
        for b in busy_raw:
            try:
                bs = datetime.fromisoformat(str(b["start"]).replace("Z", "+00:00"))
                be = datetime.fromisoformat(str(b["end"]).replace("Z", "+00:00"))
                busy.append((bs, be))
            except (KeyError, ValueError):
                continue
        return _generate_free_slots_from_busy(start_dt, end_dt, busy, duration_minutes=duration_minutes, provider="google")

    async def _google_book(
        self, prospect_email: str, slot: dict, *, prospect_name: str, notes: str
    ) -> bool:
        token = self._cfg_or_env("google_calendar_access_token", "GOOGLE_CALENDAR_ACCESS_TOKEN")
        cal_id = self._cfg_or_env("google_calendar_id", "GOOGLE_CALENDAR_ID", "primary")
        start_str = str(slot["start"])
        end_str = str(slot.get("end") or "")
        if not end_str:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_str = (dt + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        persona = getattr(self.config, "persona", None)
        host_name = getattr(persona, "company", "") or getattr(persona, "name", "") or "Intro Call"
        summary = f"{host_name} <> {prospect_name or prospect_email}"
        async with self._client() as client:
            resp = await client.post(
                f"{GOOGLE_CAL_BASE}/calendars/{cal_id}/events",
                params={"sendUpdates": "all"},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "summary": summary,
                    "description": notes,
                    "start": {"dateTime": start_str},
                    "end": {"dateTime": end_str},
                    "attendees": [{"email": prospect_email.strip()}],
                },
            )
        return resp.status_code in (200, 201)

    # ── Microsoft Graph Calendar ──

    async def _microsoft_slots(
        self, start_dt: datetime, end_dt: datetime, *, duration_minutes: int
    ) -> list[dict]:
        token = self._cfg_or_env("m365_calendar_access_token", "M365_CALENDAR_ACCESS_TOKEN")
        async with self._client() as client:
            resp = await client.get(
                f"{MS_GRAPH_BASE}/me/calendarView",
                params={
                    "startDateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "endDateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "$select": "start,end,showAs",
                },
                headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'},
            )
        if resp.status_code >= 400:
            return []
        busy: list[tuple[datetime, datetime]] = []
        for ev in (resp.json() or {}).get("value") or []:
            if str(ev.get("showAs") or "").lower() == "free":
                continue
            try:
                bs = datetime.fromisoformat(str(ev["start"]["dateTime"]).replace("Z", "+00:00"))
                be = datetime.fromisoformat(str(ev["end"]["dateTime"]).replace("Z", "+00:00"))
                if bs.tzinfo is None:
                    bs = bs.replace(tzinfo=timezone.utc)
                if be.tzinfo is None:
                    be = be.replace(tzinfo=timezone.utc)
                busy.append((bs, be))
            except (KeyError, ValueError, TypeError):
                continue
        return _generate_free_slots_from_busy(start_dt, end_dt, busy, duration_minutes=duration_minutes, provider="microsoft")

    async def _microsoft_book(
        self, prospect_email: str, slot: dict, *, prospect_name: str, notes: str
    ) -> bool:
        token = self._cfg_or_env("m365_calendar_access_token", "M365_CALENDAR_ACCESS_TOKEN")
        start_str = str(slot["start"])
        end_str = str(slot.get("end") or "")
        if not end_str:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_str = (dt + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        persona = getattr(self.config, "persona", None)
        host_name = getattr(persona, "company", "") or getattr(persona, "name", "") or "Intro Call"
        async with self._client() as client:
            resp = await client.post(
                f"{MS_GRAPH_BASE}/me/events",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "subject": f"{host_name} <> {prospect_name or prospect_email}",
                    "body": {"contentType": "Text", "content": notes},
                    "start": {"dateTime": start_str, "timeZone": "UTC"},
                    "end": {"dateTime": end_str, "timeZone": "UTC"},
                    "attendees": [{
                        "emailAddress": {"address": prospect_email.strip(), "name": prospect_name or prospect_email},
                        "type": "required",
                    }],
                },
            )
        return resp.status_code in (200, 201)
