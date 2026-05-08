Suggested categories that cover the workflow from capture to execution, along with how they would function in your specific use cases:
Suggested Categories for NoterAI
    Reference
        Purpose: Static facts, documentation, specs, and "just-in-case" knowledge.
        Use Case: Perfect for your example, "What kind of memory does my motherboard use?" The RAG would prioritize this category for factual, non-actionable queries.
        Content: Manuals, datasheets, technical specs, encyclopedic notes.
     

    Ideas
        Purpose: Raw brainstorming, half-formed thoughts, and creative sparks without immediate action items.
        Use Case: Capturing concepts that might become future projects but aren't ready for a task list yet.
        Content: Brainstorming sessions, random thoughts, project concepts.
     

    Todo
        Purpose: Actionable items with deadlines or immediate next steps.
        Use Case: Your example, "What should I be working on?" The RAG would filter specifically for items tagged as Todo and potentially sort them by urgency or date.
        Content: To-do lists, bug fixes, scheduled tasks.
     

    Review Later
        Purpose: Information that requires future processing or periodic re-evaluation (e.g., the "Save for Later" function).
        Use Case: Articles to read, links to revisit, or tasks that need a status check next week.
        Content: Curated links, draft articles, pending decisions.
     

    Project
        Purpose: Centralized hubs for active, multi-step endeavors.
        Use Case: Distinguishing between a single Todo item and the broader context of a Project (e.g., "Redesign Website"). The RAG can summarize progress by aggregating notes from this category.
        Content: Project roadmaps, meeting notes, status updates.
     

    Journal
        Purpose: Personal logs, daily reflections, and time-based entries.
        Use Case: When a user asks, "How did I feel about that meeting last month?" The RAG would search this category chronologically.
        Content: Daily logs, mood tracking, meeting reflections.
     

    Resources
        Purpose: Collections of external tools, templates, or assets.
        Use Case: A place to store links to software, templates, or code snippets that are referenced frequently but aren't "Reference" facts per se.
        Content: Software licenses, template links, asset libraries.
     

    Archive
        Purpose: Completed projects or obsolete information that should be preserved but excluded from active search results by default.
        Use Case: Keeping the RAG context clean by ignoring old, irrelevant data unless explicitly asked to search history.
        Content: Finished projects, old meeting notes, deprecated specs.
    
    unfiled:
        inbox for unfiled and unindexed content that is wating to be catagorized and processed.

---

# Multi-User Support Plan

## Context

NoterAI is currently single-user with no authentication — all notes, attachments, and settings are globally shared. The goal is to add support for ~10 named users with full data isolation (each user sees only their own notes, attachments, tags, folders, settings). Authentication is username + password with JWT (stored in localStorage). No self-registration — users are created via a CLI script by the owner. Existing data migrates to a first "admin" user.

---

## Approach

### 1. New dependencies

Add to `requirements.txt` (or install directly):
- `bcrypt` — password hashing
- `PyJWT` — JWT encode/decode

### 2. Database schema changes (`backend/database.py`)

**New `users` table:**
```sql
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
)
```

**Migration — add `user_id` to `notes`:**
```sql
ALTER TABLE notes ADD COLUMN user_id TEXT REFERENCES users(id)
```
Then update all existing rows: `UPDATE notes SET user_id = '<admin_id>'`

**New `user_settings` table** (replaces `app_settings`):
```sql
CREATE TABLE user_settings (
  user_id TEXT NOT NULL REFERENCES users(id),
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
)
```
Migration: copy all `app_settings` rows into `user_settings` with admin's `user_id`.

**Update all CRUD functions** in `database.py` to accept and filter by `user_id`:
- `get_notes(db, user_id, ...)` — `WHERE user_id = ?`
- `create_note(db, user_id, ...)` — set `user_id`
- `get_note(db, note_id, user_id)` — verify ownership
- `update_note(db, note_id, user_id, ...)` — verify ownership
- `delete_note(db, note_id, user_id)` — verify ownership
- `get_settings(db, user_id)` / `update_setting(db, user_id, key, value)`
- Attachments inherit ownership via `note_id → notes.user_id` — verify at the note level

### 3. Auth module (`backend/auth.py`) — new file

```python
# password hashing
def hash_password(password: str) -> str
def verify_password(password: str, hashed: str) -> bool

# JWT
def create_token(user_id: str) -> str   # HS256, exp = 30 days
def decode_token(token: str) -> str     # returns user_id, raises on invalid/expired

# FastAPI dependency
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db = Depends(get_db)
) -> UserRow
```

JWT secret read from `settings.jwt_secret` (required `.env` key: `JWT_SECRET`).

### 4. Config changes (`backend/config.py`)

Add:
```python
jwt_secret: str  # required
jwt_expiry_days: int = 30
```

### 5. New/updated routes (`backend/main.py`)

**New auth routes (no auth required):**
- `POST /api/auth/login` — `{username, password}` → `{token, username}`
- `GET /api/auth/me` — returns current user (requires auth) — used for session validation on app load

**All existing routes** get `current_user: User = Depends(get_current_user)` injected, then pass `user_id` to DB calls. Return 404 (not 403) when a resource exists but belongs to another user (avoids leaking existence).

**Settings routes** switch from `app_settings` to `user_settings`, filtering by `current_user.id`.

### 6. Vector store changes (`backend/vector_store.py` + indexing pipeline)

When indexing note chunks, add `user_id` to Chroma metadata:
```python
metadata = {"note_id": note_id, "user_id": user_id, ...}
```

When searching (`backend/main.py` search route), add where filter:
```python
where={"user_id": current_user.id}
```

Re-indexing all existing notes (after migration) will backfill `user_id` in Chroma metadata.

### 7. User creation CLI (`backend/create_user.py`) — new file

```bash
python -m backend.create_user --username alice
# Prompts for password, creates user in DB
```

Also doubles as migration entry point: `--migrate` flag creates admin user and assigns existing data.

### 8. Frontend changes

**`frontend/index.html`:**
- Add login form (shown when no valid JWT in localStorage)
- Add logout button + username display in sidebar

**`frontend/app.js`:**
- On load: check localStorage for token, call `GET /api/auth/me`; if 401, show login form
- Modify `apiFetch()` to inject `Authorization: Bearer <token>` on every request
- On any 401 response: clear token, show login form
- Add `login(username, password)` function: `POST /api/auth/login`, store token
- Add `logout()` function: clear token, show login form

**`frontend/style.css`:**
- Style login form (reuse existing card/input styles)

---

## Migration steps (run once on deployment)

1. `python -m backend.create_user --username admin --migrate` — creates admin user, assigns all existing notes/settings
2. Restart app — new schema is live
3. Run `/api/reindex` (bulk reindex) to backfill `user_id` in Chroma metadata
4. Create remaining users: `python -m backend.create_user --username <name>`

---

## Critical files to modify

| File | Change |
|------|--------|
| `backend/database.py` | Schema migrations, add `user_id` to all CRUD |
| `backend/main.py` | Add auth routes, inject `get_current_user` on all routes |
| `backend/models.py` | Add `UserResponse`, `LoginRequest`, `TokenResponse` |
| `backend/config.py` | Add `jwt_secret`, `jwt_expiry_days` |
| `backend/vector_store.py` | Pass `user_id` in metadata + where filter |
| `frontend/index.html` | Login form, logout button |
| `frontend/app.js` | Auth flow, token injection in `apiFetch()` |
| `frontend/style.css` | Login form styles |

**New files:**
- `backend/auth.py`
- `backend/create_user.py`

---

## Verification

1. Start app fresh — visiting `/` should show login form
2. `python -m backend.create_user --username testuser` → log in → see notes
3. Create a second user → confirm their note list is empty (isolation works)
4. Create a note as user A → search returns only user A's results
5. Try fetching user A's note ID as user B → 404
6. Settings (Telegram, timezone) changed as user A → not visible to user B
7. Logout → token cleared → redirect to login
8. Expired/invalid JWT → 401 → redirect to login
