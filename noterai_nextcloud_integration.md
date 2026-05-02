# NoterAI — Nextcloud Calendar Integration Spec

## Overview

Bidirectional sync between NoterAI notes with due dates and Nextcloud Calendar/Tasks via the
Nextcloud CalDAV API (app password auth). Notes in the `Todo` folder push as `VTODO`; all other
folders push as `VEVENT`. Inbound Nextcloud events (not originating from NoterAI) are injected
into the RAG chat context window as upcoming reminders.

The Nextcloud **instance** is configured once by an admin. Each user supplies their own
credentials and calendar names, mirroring how Telegram bot tokens already work.

---

## New Dependencies

```
httpx       # async HTTP (may already be present)
icalendar   # iCal serialization and parsing
```

Add both to `requirements.txt`.

---

## Configuration

### Global admin settings (stored in the existing `settings` table)

Add the following columns:

```sql
ALTER TABLE settings ADD COLUMN nextcloud_url TEXT;
ALTER TABLE settings ADD COLUMN nextcloud_rag_lookahead_days INTEGER DEFAULT 7;
ALTER TABLE settings ADD COLUMN nextcloud_sync_interval_minutes INTEGER DEFAULT 30;
```

`nextcloud_url` is the only instance-level value (e.g. `https://cloud.example.com`).
Sync interval and RAG lookahead are also global since they affect the background task scheduler.

There are **no credentials in the global settings** — those are per-user.

### Per-user settings (stored in the `users` table)

Add the following columns:

```sql
ALTER TABLE users ADD COLUMN nextcloud_username TEXT;
ALTER TABLE users ADD COLUMN nextcloud_app_password TEXT;
ALTER TABLE users ADD COLUMN nextcloud_calendar_name TEXT DEFAULT 'noterai';
ALTER TABLE users ADD COLUMN nextcloud_tasks_calendar_name TEXT DEFAULT 'noterai-tasks';
```

The integration is **active for a given user** only when all three conditions are true:
- Global `nextcloud_url` is set
- `users.nextcloud_username` is set
- `users.nextcloud_app_password` is set

### Notes table

Add a column to track the CalDAV UID for each synced note:

```sql
ALTER TABLE notes ADD COLUMN nextcloud_uid TEXT;
```

Populated on first push as `noterai-{note_id}@noterai`. Used for idempotent upserts and
targeted deletes.

---

## New Backend Module: `backend/nextcloud.py`

### `NextcloudClient`

Thin async wrapper around `httpx.AsyncClient` with HTTP Basic auth. Accepts `base_url`,
`username`, `app_password` at construction. Implements:

- `propfind(path)` — check calendar existence
- `mkcol(path)` — create a calendar collection
- `put(path, ical_body)` — create or replace a calendar object
- `delete(path)` — remove a calendar object
- `report(path, time_range_start, time_range_end)` — CalDAV REPORT query for event fetch

All methods raise on HTTP errors; callers catch and log.

### `ensure_calendars_exist(client, username, calendar_name, tasks_calendar_name)`

On user credential save (and on startup for all configured users), PROPFIND both calendar
paths. If either returns 404, issue MKCOL to create it. Log a warning and return gracefully
if Nextcloud is unreachable — never crash the app.

CalDAV paths follow the pattern:
```
/remote.php/dav/calendars/{username}/{calendar_name}/
```

### `push_note(client, note, username, calendar_name, tasks_calendar_name) → str`

Builds the correct iCal payload and PUTs it to the UID-based URL. Returns the UID used.

**Routing:**
```
note.folder == "Todo"  →  VTODO  →  tasks_calendar_name
note.folder != "Todo"  →  VEVENT →  calendar_name
```

**VTODO payload:**

```
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//NoterAI//NoterAI//EN
BEGIN:VTODO
UID:noterai-{note.id}@noterai
SUMMARY:{note.title}
DESCRIPTION:{note.content[:500]}
DUE;VALUE=DATE:{note.due_date}
STATUS:{COMPLETED if note.done else NEEDS-ACTION}
LAST-MODIFIED:{utcnow}
END:VTODO
END:VCALENDAR
```

**VEVENT payload:**

```
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//NoterAI//NoterAI//EN
BEGIN:VEVENT
UID:noterai-{note.id}@noterai
SUMMARY:{note.title}
DESCRIPTION:{note.content[:500]}
DTSTART;VALUE=DATE:{note.due_date}
DTEND;VALUE=DATE:{note.due_date + timedelta(days=1)}
LAST-MODIFIED:{utcnow}
END:VEVENT
END:VCALENDAR
```

Always PUT to the UID-based URL — CalDAV treats PUT as create-or-replace.

### `delete_note_event(client, note, username, calendar_name, tasks_calendar_name)`

DELETEs the object at the UID-based URL from the correct calendar. Ignores 404 (already gone).
Called when `due_date` is cleared from a note that has a `nextcloud_uid`.

### `fetch_upcoming_events(client, username, calendar_name, tasks_calendar_name, days) → list[dict]`

Issues a CalDAV REPORT time-range query against both calendars for the next `days` days.
Parses the response with the `icalendar` library. Returns:

```python
[
    {"title": str, "date": date, "calendar": str, "uid": str},
    ...
]
```

Skips any objects whose UID starts with `noterai-` (those originate from NoterAI and are
already surfaced by the existing reminder system — no duplication).

### In-memory event cache

```python
_event_cache: dict[int, list[dict]] = {}  # user_id → list of upcoming events
_event_cache_lock: asyncio.Lock
```

`get_cached_events(user_id) → list[dict]` — returns the cached list or `[]`. Safe to call
from the RAG hot path without any I/O.

Cache is populated by the background sync task. TTL is implicitly the sync interval.

---

## Sync Trigger Points

### On note save (`POST /api/notes`, `PUT /api/notes/{id}`)

After DB commit, if the note's owner has Nextcloud configured:

- `due_date` set or changed → `push_note(...)` (async, fire-and-forget, logged on failure)
- `due_date` cleared and `nextcloud_uid` is set → `delete_note_event(...)`, then clear
  `notes.nextcloud_uid` in DB
- `done` flipped to `true` on a Todo note → re-PUT VTODO with `STATUS:COMPLETED`

### Background task (interval: `nextcloud_sync_interval_minutes`)

Runs on the same scheduler used for Telegram reminders. For each user with valid Nextcloud
credentials:

1. Full sweep of all notes with `due_date IS NOT NULL` belonging to that user — push any
   that are missing a `nextcloud_uid` or have been modified since last sync.
2. `fetch_upcoming_events(...)` and update `_event_cache[user_id]`.

A single failed note does not abort the sweep. Catch, log at WARNING, continue.

---

## RAG Chat API Changes (`backend/chat_api.py`)

The existing system prompt already prepends overdue/due-today reminders from SQLite. Extend
this to also append the in-memory Nextcloud event cache for the requesting user:

```
--- Upcoming calendar events (next {N} days) ---
2026-05-05  Team standup
2026-05-07  Doctor appointment
```

Appended after the existing reminders block as plain text. These events are **not** chunked
or embedded — context-window injection only, same pattern as the existing reminder injection.

Call `nextcloud.get_cached_events(user_id)` — if the list is empty (unconfigured, unreachable,
or between syncs) nothing is appended. No I/O on the hot path.

---

## API Endpoints

### `POST /api/nextcloud/test`

Authenticated. Validates the combination of global `nextcloud_url` + the requesting user's
credentials by issuing a PROPFIND against their calendars. Returns:

```json
{"ok": true}
// or
{"ok": false, "error": "..."}
```

Used by the Settings UI **Test Connection** button.

---

## Settings UI Changes

### Admin Settings page — new "Nextcloud" section

Fields:
- **Nextcloud Instance URL** — maps to global `nextcloud_url`
- **RAG Lookahead Days** — maps to `nextcloud_rag_lookahead_days`
- **Sync Interval (minutes)** — maps to `nextcloud_sync_interval_minutes`

No credentials in admin settings.

### User Settings page — new "Nextcloud" section

Same pattern as the existing Telegram section. Fields:
- **Nextcloud Username**
- **App Password** (masked `<input type="password">`)
- **Calendar Name** (default: `noterai`)
- **Tasks Calendar Name** (default: `noterai-tasks`)
- **Test Connection** button — calls `POST /api/nextcloud/test`

On save, call `ensure_calendars_exist(...)` for that user in the background.

---

## Error Handling Policy

- All Nextcloud calls on the note-save path are fire-and-forget. Failures are logged at
  `WARNING` level and never surfaced to the user as errors.
- Background sync catches and logs all exceptions per-note; a single bad note does not
  abort the sweep.
- If Nextcloud is unreachable at startup or during a sync cycle, the integration degrades
  gracefully: `get_cached_events(user_id)` returns `[]`, no outbound pushes until the next
  successful background run.
- 404 on DELETE is silently ignored.

---

## Out of Scope

- Importing Nextcloud events as NoterAI notes (inbound is context injection only)
- Conflict resolution — NoterAI is authoritative for note content; Nextcloud edits to
  NoterAI-originated events are overwritten on the next push
- Recurring events
- Attendees / invites / alarms
- Per-note calendar selection (calendar is determined by folder)
