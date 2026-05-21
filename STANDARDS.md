# NoterAI Development Standards

Standards for logging, error handling, and frontend design. Follow these when adding or modifying code.

---

## Backend — Structured Logging

### Setup

Use `python-json-logger` (add to `requirements.txt`). Configure once in the `lifespan` block in `backend/main.py` before anything else runs. All module-level loggers created with `logging.getLogger(__name__)` inherit the root config automatically — no per-module setup needed.

```python
# backend/main.py — top of lifespan(), before DB init
import logging
from pythonjsonlogger import jsonlogger

handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
))
logging.root.setLevel(logging.INFO)
logging.root.handlers = [handler]
```

Log lines are newline-delimited JSON — readable with `docker logs` and filterable with `jq`:

```bash
docker logs noterai | jq 'select(.levelname == "ERROR")'
docker logs noterai | jq 'select(.note_id == "abc-123")'
docker logs noterai | jq 'select(.levelname == "WARNING") | .message'
```

### Standard Fields

Always present (from the formatter): `asctime`, `levelname`, `name` (module), `message`.

Add domain context as keyword args to every meaningful log call:

| Context | Fields to include |
|---|---|
| Note pipeline | `note_id` |
| Attachment pipeline | `attachment_id`, `note_id` |
| Search | `query` (truncated to 100 chars), `result_count` |
| Timing (slow ops) | `duration_ms` |
| Errors | `exc_info=True` (attaches traceback automatically) |

```python
# Good
logger.info("note indexed", extra={"note_id": note_id, "chunk_count": n, "duration_ms": ms})
logger.error("pdf extraction failed", extra={"attachment_id": att_id}, exc_info=True)

# Bad — no context, hard to correlate
logger.warning("extraction failed")
```

### Log Levels

| Level | When to use |
|---|---|
| `DEBUG` | Internal pipeline steps (chunk sizes, batch counts). Off in production. |
| `INFO` | Normal completions: note indexed, attachment extracted, server started. |
| `WARNING` | Degraded but recoverable: vector deletion failed, service unreachable at startup. |
| `ERROR` | Operation failed, data may be inconsistent: pipeline crashed, extraction failed. |

Never use `CRITICAL` — this app has no multi-process coordination that warrants it.

---

## Backend — Error Handling

### Rules

1. **No bare `except Exception: pass`** — always log at `WARNING` or higher with `exc_info=True`.

2. **Background tasks log `ERROR` with entity ID on failure.**
   The caller (FastAPI route) has already returned by the time a background task runs. Errors must be visible in logs.

   ```python
   # Good
   except Exception:
       logger.error("indexing pipeline failed", extra={"note_id": note_id}, exc_info=True)

   # Bad
   except Exception:
       pass
   ```

3. **Extraction errors log to stderr *and* store in DB.**
   Storing in `extraction_error` alone makes failures invisible to an operator watching logs.

   ```python
   except PDFExtractionError as exc:
       logger.warning("pdf extraction failed", extra={"attachment_id": att_id}, exc_info=True)
       await db.set_extraction_error(att_id, str(exc))  # also store for UI
   ```

4. **Always include `exc_info=True` in `except` blocks** so tracebacks appear in logs.
   Exception: `except HTTPException` re-raised immediately needs no logging.

5. **Service unavailability is `WARNING`, not `ERROR`.**
   ChromaDB or the embedding service being down at startup or during a background task is expected in local dev. Reserve `ERROR` for data-corrupting or unrecoverable failures.

---

## Frontend — Console Logging

Keep it simple — no logging library. The goal is that a developer can open DevTools and see what happened without adding print statements.

### Rules

1. **Every `catch` block calls `console.error`** with the operation name and the error object.

   ```js
   // Good
   try {
     await saveNote();
   } catch (err) {
     console.error('saveNote', err);
     toast('Save failed: ' + err.message, 'error');
   }

   // Bad — silent
   try {
     await saveNote();
   } catch {}
   ```

2. **Poll failures use `console.warn`**, not `console.error`. They are expected to fail transiently.

   ```js
   } catch (err) {
     console.warn('index poll failed', err);
   }
   ```

3. **Toast the user AND log to console.** Toasts disappear; the console persists for the session. Never replace one with the other.

4. **No `console.log` left in committed code.** Use `console.info` for deliberate informational traces that should persist (e.g. service worker registered), otherwise remove debug logs before committing.

---

## Frontend — Layout & Responsiveness

### Breakpoints

Two breakpoints only:

| Name | Width | Behavior |
|---|---|---|
| Mobile | `≤ 600px` | Sidebar hidden, hamburger menu visible, stacked layout |
| Tablet | `601px – 900px` | Sidebar visible but narrower (180px), editor metadata stacks |
| Desktop | `> 900px` | Full two-panel layout, 240px sidebar |

```css
/* Tablet */
@media (max-width: 900px) { ... }

/* Mobile */
@media (max-width: 600px) { ... }
```

### Mobile Navigation — Hamburger Menu

On mobile (`≤ 600px`) the sidebar is hidden. A hamburger button (`☰`) appears in the top-left of the main content area. Tapping it slides the sidebar in as an overlay (full height, fixed position, above content). A backdrop covers the rest of the screen; tapping it closes the drawer.

- Sidebar overlay: `position: fixed; top: 0; left: 0; height: 100%; z-index: 200`
- Backdrop: `position: fixed; inset: 0; background: rgba(0,0,0,0.3); z-index: 199`
- Transition: `transform: translateX(-100%)` → `translateX(0)`, 200ms ease

The hamburger button is part of the editor/note-list header bar, not floating.

### Touch Targets

All interactive elements must meet a **44×44px minimum tap target**. Apply padding to reach this size even if the visual element is smaller.

```css
/* Minimum for any button or icon button */
button, [role="button"] {
  min-height: 44px;
  padding: 10px 14px;  /* adjust per context */
}

/* Icon-only buttons (attachment actions, tag remove, etc.) */
.icon-btn {
  min-width: 44px;
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
```

### Safe Area Insets

The toast container and any fixed bottom elements must respect device notches:

```css
#toast-container {
  bottom: max(20px, env(safe-area-inset-bottom));
  right: max(16px, env(safe-area-inset-right));
}
```

### Typography

Font sizes are fixed — do not change them. Responsive spacing uses `clamp` where needed.

| Element | Size | Weight |
|---|---|---|
| App title | 15px | 700 |
| Body / notes | 14px | 400 |
| Buttons, labels | 13px | 400–500 |
| Meta (timestamp, tags) | 11–12px | 400 |
| Editor textarea | 13px monospace | 400 |

---

## Frontend — Component Standards

### Toasts

Use for async operation outcomes. Auto-dismiss after 4 seconds. Call `toast(message, type)` where type is `''` (neutral), `'success'`, `'error'`, or `'warning'`.

- **Do use toasts for:** save confirmation, delete confirmation, upload complete, search error, network error.
- **Do not use toasts for:** form validation errors (use inline), loading states (use spinner/badge), destructive confirmations (use inline confirm — see below).

### Inline Confirmation (Destructive Actions)

Do not use `window.confirm()`. For delete actions, replace the button in-place with a two-button confirm row:

```
[Delete note]  →  [Are you sure?]  [Yes, delete]  [Cancel]
```

The confirm row lives in the same DOM position as the original button. Auto-cancel after 5 seconds of no action (restore original button). This applies to: delete note, delete attachment.

### Loading States

Every async operation must show feedback. Never leave the UI blank or static while waiting.

| Context | Pattern |
|---|---|
| Save in progress | Save badge shows "saving…" (amber) |
| Indexing in progress | Save badge shows "indexed" pending, ⏳ on note card |
| Attachment processing | Row shows "extracting…" status |
| Reindex all | Progress bar + "X / Y notes" count |
| Upload | "uploading X%" in attachment row |
| Search | Disable input / show spinner while request is in flight |

Do not remove a loading indicator until the operation is confirmed complete or failed.

### Empty States

Every list or result container must have an empty state — never a blank space.

| Container | Empty state text |
|---|---|
| Note list | "No notes yet. Create one!" |
| Search results | "No results." |
| Attachments list | "No attachments. Drop a PDF here." |
| Folder list | *(show "All notes" always — no empty state needed)* |
| Tag list | *(hide section entirely if no tags exist)* |

### Poll Failure Handling

Polling loops (`startIndexPoll`, `startAttPoll`, `pollReindexJob`) catch errors silently today. Standard:

- Log `console.warn` on every poll failure
- After **3 consecutive failures**, stop the poll and show a toast: `"Could not reach server — please reload."`
- Reset the failure counter on any successful response

### Form Validation Errors

Show inline, directly below the relevant input. Do not use toasts for validation. Use `--danger` color (`#dc2626`) and 11px text. Clear the error as soon as the user starts correcting the field.

---

## Color Palette Reference

Defined as CSS variables in `:root` — always use variables, never hardcode hex values in new code.

| Variable | Value | Use |
|---|---|---|
| `--accent` | `#4f46e5` | Primary actions, links, focus rings |
| `--accent-hover` | `#4338ca` | Hover state on accent elements |
| `--bg` | `#f9fafb` | Page background |
| `--surface` | `#ffffff` | Cards, panels, inputs |
| `--border` | `#e5e7eb` | Dividers, input borders |
| `--text` | `#111827` | Primary text |
| `--muted` | `#6b7280` | Secondary text, placeholders |
| `--danger` | `#dc2626` | Destructive actions, errors |
| `--success` | `#16a34a` | Success states |
| `--warn` | `#d97706` | Warnings, in-progress states |
| `--radius` | `6px` | Border radius for all rounded elements |
| `--shadow` | `0 1px 3px rgba(0,0,0,.1)` | Card and panel shadows |

Never introduce a new color. If a new semantic color is genuinely needed, add it as a CSS variable here first.

---

## Plugin Standards

- Plugin name (slug): lowercase, underscores only — no hyphens (e.g. `chat_rag`, not `chat-rag`)
- `user_settings` keys owned by a plugin must be prefixed with the plugin name
- Plugins must not import from each other
- Plugins may import from `backend.core.*` freely; use absolute imports
- New DB tables created by a plugin: `CREATE TABLE IF NOT EXISTS` only, named `{plugin_name}_{table}`, created in `register()`
- Route prefix: `/api/{plugin_name}/` unless there's a strong compatibility reason not to (chat_rag uses `/v1/` for OpenAI compat — document why in `plugin.py`)
- Frontend: each plugin's `settings.js` must export an `init()` function; called after the fragment is injected
- `register()` must complete in under 2 seconds; defer heavy init to `asyncio.create_task()`
- Plugins must catch all exceptions internally — never let an exception propagate out of `register()` or scheduler job functions
- `shutdown()` must cancel all background tasks and close any open connections
