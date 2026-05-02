"""Nextcloud CalDAV integration — push notes with due dates, fetch upcoming events."""

import asyncio
import logging
from datetime import date, timedelta, datetime, timezone

import httpx
from icalendar import Calendar, Event, Todo, vDatetime, vDate, vText

from .config import settings as app_settings_obj
from . import database as db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory event cache
# ---------------------------------------------------------------------------

_event_cache: dict[str, list[dict]] = {}
_cache_lock = asyncio.Lock()


def get_cached_events(user_id: str) -> list[dict]:
    return _event_cache.get(user_id, [])


# ---------------------------------------------------------------------------
# NextcloudClient
# ---------------------------------------------------------------------------

class NextcloudClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._client = httpx.AsyncClient(
            auth=(username, password),
            verify=True,
            timeout=15.0,
        )

    def _cal_path(self, calendar_name: str) -> str:
        return f"/remote.php/dav/calendars/{self._username}/{calendar_name}/"

    def _event_path(self, calendar_name: str, uid: str) -> str:
        return f"/remote.php/dav/calendars/{self._username}/{calendar_name}/{uid}.ics"

    async def propfind(self, path: str) -> int:
        r = await self._client.request("PROPFIND", self._base + path,
                                        headers={"Depth": "0"})
        return r.status_code

    async def mkcalendar(self, path: str, display_name: str) -> None:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            "<D:set><D:prop>"
            f"<D:displayname>{display_name}</D:displayname>"
            "</D:prop></D:set>"
            "</C:mkcalendar>"
        )
        r = await self._client.request(
            "MKCALENDAR",
            self._base + path,
            content=body.encode(),
            headers={"Content-Type": "application/xml; charset=utf-8"},
        )
        r.raise_for_status()

    async def put(self, path: str, body: str) -> None:
        r = await self._client.put(
            self._base + path,
            content=body.encode(),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        r.raise_for_status()

    async def delete(self, path: str) -> None:
        r = await self._client.delete(self._base + path)
        if r.status_code == 404:
            return
        r.raise_for_status()

    async def report(self, path: str, start: date, end: date) -> str:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
            "<C:filter><C:comp-filter name=\"VCALENDAR\">"
            "<C:comp-filter name=\"VEVENT\">"
            f"<C:time-range start=\"{start.strftime('%Y%m%dT000000Z')}\" "
            f"end=\"{end.strftime('%Y%m%dT000000Z')}\"/>"
            "</C:comp-filter></C:comp-filter></C:filter>"
            "</C:calendar-query>"
        )
        r = await self._client.request(
            "REPORT",
            self._base + path,
            content=body.encode(),
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        )
        r.raise_for_status()
        return r.text

    async def report_todos(self, path: str) -> str:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
            "<C:filter><C:comp-filter name=\"VCALENDAR\">"
            "<C:comp-filter name=\"VTODO\"/>"
            "</C:comp-filter></C:filter>"
            "</C:calendar-query>"
        )
        r = await self._client.request(
            "REPORT",
            self._base + path,
            content=body.encode(),
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        )
        r.raise_for_status()
        return r.text

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Calendar management
# ---------------------------------------------------------------------------

async def ensure_calendars_exist(client: NextcloudClient, username: str,
                                  calendar_name: str, tasks_calendar_name: str) -> None:
    for cal in (calendar_name, tasks_calendar_name):
        try:
            path = client._cal_path(cal)
            status = await client.propfind(path)
            if status == 404:
                await client.mkcalendar(path, cal)
                logger.info("Created Nextcloud calendar: %s", cal)
        except Exception:
            logger.warning("ensure_calendars_exist failed for %s", cal, exc_info=True)


# ---------------------------------------------------------------------------
# iCal builders
# ---------------------------------------------------------------------------

def _utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_vtodo(note: dict) -> str:
    cal = Calendar()
    cal.add("prodid", "-//NoterAI//NoterAI//EN")
    cal.add("version", "2.0")
    todo = Todo()
    uid = f"noterai-{note['id']}@noterai"
    todo.add("uid", uid)
    todo.add("summary", note["title"])
    description = (note.get("content") or "")[:500]
    if description:
        todo.add("description", description)
    if note.get("reminder_at"):
        due_date = date.fromisoformat(note["reminder_at"][:10])
        todo["DUE"] = vDate(due_date)
    status = "COMPLETED" if note.get("reminder_done") else "NEEDS-ACTION"
    todo.add("status", status)
    todo.add("last-modified", datetime.now(timezone.utc))
    cal.add_component(todo)
    return cal.to_ical().decode()


def _build_vevent(note: dict) -> str:
    cal = Calendar()
    cal.add("prodid", "-//NoterAI//NoterAI//EN")
    cal.add("version", "2.0")
    event = Event()
    uid = f"noterai-{note['id']}@noterai"
    event.add("uid", uid)
    event.add("summary", note["title"])
    description = (note.get("content") or "")[:500]
    if description:
        event.add("description", description)
    if note.get("reminder_at"):
        due_date = date.fromisoformat(note["reminder_at"][:10])
        event["DTSTART"] = vDate(due_date)
        event["DTEND"] = vDate(due_date + timedelta(days=1))
    event.add("last-modified", datetime.now(timezone.utc))
    cal.add_component(event)
    return cal.to_ical().decode()


# ---------------------------------------------------------------------------
# Push / delete
# ---------------------------------------------------------------------------

async def push_note(client: NextcloudClient, note: dict,
                    username: str, calendar_name: str,
                    tasks_calendar_name: str) -> str:
    uid = f"noterai-{note['id']}@noterai"
    if note.get("folder") == "Todo":
        ical = _build_vtodo(note)
        cal = tasks_calendar_name
    else:
        ical = _build_vevent(note)
        cal = calendar_name
    path = client._event_path(cal, uid)
    await client.put(path, ical)
    return uid


async def delete_note_event(client: NextcloudClient, note: dict,
                             username: str, calendar_name: str,
                             tasks_calendar_name: str) -> None:
    uid = note.get("nextcloud_uid") or f"noterai-{note['id']}@noterai"
    cal = tasks_calendar_name if note.get("folder") == "Todo" else calendar_name
    path = client._event_path(cal, uid)
    await client.delete(path)


# ---------------------------------------------------------------------------
# Fetch upcoming events (inbound from Nextcloud)
# ---------------------------------------------------------------------------

def _parse_ical_events(raw_xml: str, calendar_label: str,
                        lookahead_end: date) -> list[dict]:
    events: list[dict] = []
    # Extract calendar-data blocks from REPORT XML
    import re
    for chunk in re.findall(r"BEGIN:VCALENDAR.*?END:VCALENDAR", raw_xml,
                            re.DOTALL | re.IGNORECASE):
        try:
            cal = Calendar.from_ical(chunk)
            for component in cal.walk():
                if component.name not in ("VEVENT", "VTODO"):
                    continue
                uid = str(component.get("uid", ""))
                if uid.startswith("noterai-"):
                    continue  # skip NoterAI-originated items
                summary = str(component.get("summary", ""))
                # Extract date
                event_date: date | None = None
                for field in ("dtstart", "due", "dtend"):
                    val = component.get(field)
                    if val is None:
                        continue
                    dt = val.dt if hasattr(val, "dt") else val
                    if isinstance(dt, datetime):
                        event_date = dt.date()
                    elif isinstance(dt, date):
                        event_date = dt
                    if event_date:
                        break
                if event_date and event_date <= lookahead_end:
                    events.append({
                        "title": summary,
                        "date": event_date,
                        "calendar": calendar_label,
                        "uid": uid,
                    })
        except Exception:
            logger.debug("Failed to parse ical chunk", exc_info=True)
    return events


async def fetch_upcoming_events(client: NextcloudClient, username: str,
                                 calendar_name: str, tasks_calendar_name: str,
                                 days: int) -> list[dict]:
    today = date.today()
    end = today + timedelta(days=days)
    events: list[dict] = []
    for cal, label in ((calendar_name, calendar_name), (tasks_calendar_name, tasks_calendar_name)):
        try:
            xml = await client.report(client._cal_path(cal), today, end)
            events.extend(_parse_ical_events(xml, label, end))
        except Exception:
            logger.warning("fetch_upcoming_events failed for calendar %s", cal, exc_info=True)
    events.sort(key=lambda e: e["date"])
    return events


# ---------------------------------------------------------------------------
# Per-user client factory
# ---------------------------------------------------------------------------

async def get_user_client(user_id: str) -> NextcloudClient | None:
    conn = await db.get_db()
    try:
        url = await db.get_setting(conn, "nextcloud_url")
        username = await db.get_user_setting(conn, user_id, "nextcloud_username")
        password = await db.get_user_setting(conn, user_id, "nextcloud_app_password")
    finally:
        await conn.close()
    if not (url and username and password):
        return None
    return NextcloudClient(url, username, password)


async def _get_user_cal_names(user_id: str, conn) -> tuple[str, str]:
    cal = await db.get_user_setting(conn, user_id, "nextcloud_calendar_name") or "noterai"
    tasks_cal = await db.get_user_setting(conn, user_id, "nextcloud_tasks_calendar_name") or "noterai-tasks"
    return cal, tasks_cal


# ---------------------------------------------------------------------------
# Full sync sweep for a single user
# ---------------------------------------------------------------------------

async def sync_user(user_id: str) -> None:
    conn = await db.get_db()
    try:
        url = await db.get_setting(conn, "nextcloud_url")
        username = await db.get_user_setting(conn, user_id, "nextcloud_username")
        password = await db.get_user_setting(conn, user_id, "nextcloud_app_password")
        if not (url and username and password):
            return
        cal_name, tasks_cal_name = await _get_user_cal_names(user_id, conn)
        lookahead = int(await db.get_setting(conn, "nextcloud_rag_lookahead_days") or "7")

        async with conn.execute(
            "SELECT id, title, content, folder, reminder_at, reminder_done, nextcloud_uid"
            " FROM notes WHERE user_id = ? AND reminder_at IS NOT NULL",
            (user_id,),
        ) as cur:
            notes = [dict(zip([d[0] for d in cur.description], row))
                     async for row in cur]
    finally:
        await conn.close()

    client = NextcloudClient(url, username, password)
    try:
        await ensure_calendars_exist(client, username, cal_name, tasks_cal_name)
        for note in notes:
            try:
                uid = await push_note(client, note, username, cal_name, tasks_cal_name)
                if note.get("nextcloud_uid") != uid:
                    conn2 = await db.get_db()
                    try:
                        await db.set_note_nextcloud_uid(conn2, note["id"], uid)
                    finally:
                        await conn2.close()
            except Exception:
                logger.warning("sync_user: push failed for note %s", note["id"], exc_info=True)

        try:
            events = await fetch_upcoming_events(client, username, cal_name, tasks_cal_name, lookahead)
            async with _cache_lock:
                _event_cache[user_id] = events
        except Exception:
            logger.warning("sync_user: fetch_upcoming_events failed for user %s", user_id, exc_info=True)
    finally:
        await client.aclose()
