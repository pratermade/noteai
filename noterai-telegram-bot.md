# NoterAI: Telegram Bot Integration — Claude Code Spec

## Overview

Create a Telegram bot that acts as a chat interface to NoterAI's existing RAG chat API (`/v1/chat/completions` on port 8084). The bot uses long-polling (no inbound ports needed), maintains per-chat conversation history, and restricts access to whitelisted Telegram user IDs.

---

## Dependencies

Create a separate `requirements-telegram.txt` (or add to existing `requirements.txt`):

```
python-telegram-bot>=21.0
httpx>=0.27.0
apscheduler>=3.10.0
```

Use `httpx` for async streaming requests to the RAG chat API. Do not use `requests` — the bot is fully async.

---

## Configuration (`.env`)

Add these variables:

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | _(required)_ | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | _(required)_ | Comma-separated list of allowed Telegram user IDs (integers) |
| `TELEGRAM_MAX_HISTORY` | `20` | Max conversation turns (user+assistant pairs) kept per chat |
| `TELEGRAM_RAG_URL` | `http://localhost:8084` | Base URL of the NoterAI RAG chat API |
| `TELEGRAM_RAG_MODEL` | `noterai-rag` | Model name to pass to the chat API |
| `TELEGRAM_REMINDER_HOURS` | `8,14` | Comma-separated hours (24h) to send scheduled reminders |
| `TELEGRAM_REMINDER_CHAT_ID` | _(required)_ | Telegram chat ID to send scheduled reminders to (your DM with the bot) |

---

## File Structure

```
backend/
  telegram_bot.py      # Bot entry point — standalone process
  telegram_config.py   # Config loading from .env
```

The bot runs as a separate process alongside the main app and chat API. It is **not** mounted inside the FastAPI app.

---

## Bot Implementation

### `backend/telegram_config.py`

Load and validate config from environment:

```python
import os

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_ALLOWED_USERS = [
    int(uid.strip())
    for uid in os.environ["TELEGRAM_ALLOWED_USERS"].split(",")
]
TELEGRAM_MAX_HISTORY = int(os.environ.get("TELEGRAM_MAX_HISTORY", "20"))
TELEGRAM_RAG_URL = os.environ.get("TELEGRAM_RAG_URL", "http://localhost:8084")
TELEGRAM_RAG_MODEL = os.environ.get("TELEGRAM_RAG_MODEL", "noterai-rag")
TELEGRAM_REMINDER_HOURS = [
    int(h.strip())
    for h in os.environ.get("TELEGRAM_REMINDER_HOURS", "8,14").split(",")
]
TELEGRAM_REMINDER_CHAT_ID = int(os.environ["TELEGRAM_REMINDER_CHAT_ID"])
```

### `backend/telegram_bot.py`

Core bot logic. Key design decisions:

#### Access control

Implement as a decorator or check at the top of every handler. If `update.effective_user.id` is not in `TELEGRAM_ALLOWED_USERS`, reply with "Not authorized." and return. Apply this to all handlers including commands.

#### Conversation history

Store in a module-level dict:

```python
# chat_id -> list of {"role": "user"|"assistant", "content": str}
conversations: dict[int, list[dict]] = {}
```

On each message:
1. Append `{"role": "user", "content": message.text}` to the chat's history
2. Send the full history to the RAG chat API
3. Append the assistant response to history
4. Trim to `TELEGRAM_MAX_HISTORY * 2` messages (user+assistant pairs) — remove from the front

History is in-memory only. It resets when the bot process restarts. This is fine for a personal bot.

#### RAG chat API request

Use `httpx.AsyncClient` with streaming enabled:

```python
async def query_rag(messages: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{TELEGRAM_RAG_URL}/v1/chat/completions",
            json={
                "model": TELEGRAM_RAG_MODEL,
                "messages": messages,
                "stream": False
            }
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
```

Use `stream: False` for simplicity. The RAG API does the heavy lifting (intent classification, vector search, LLM call) — waiting for the full response is simpler and avoids Telegram message-edit rate limits.

#### Response formatting

The RAG chat API returns markdown with `[1]`, `[2]` citation markers and a sources footer. Telegram MarkdownV2 is fragile, so:

- Send responses with `parse_mode=None` (plain text) by default
- Strip or preserve citation markers as-is — they're readable in plain text
- If a response contains code blocks (triple backticks), attempt `parse_mode="MarkdownV2"` with proper escaping, and fall back to plain text on `telegram.error.BadRequest`

Implement a helper:

```python
async def safe_reply(update, text: str):
    """Try MarkdownV2, fall back to plain text."""
    if "```" in text:
        try:
            escaped = escape_markdown_v2(text)
            await update.message.reply_text(escaped, parse_mode="MarkdownV2")
            return
        except BadRequest:
            pass
    await update.message.reply_text(text)
```

Don't over-invest in MarkdownV2 escaping. Plain text is fine for 95% of responses.

#### Typing indicator

Send `ChatAction.TYPING` before the RAG API call so the user sees "NoterAI is typing..." in the Telegram UI:

```python
await update.message.chat.send_action(ChatAction.TYPING)
```

#### Message length

Telegram messages are capped at 4096 characters. If the RAG response exceeds this, split it at the last newline before the limit and send as multiple messages.

---

## Bot Commands

### `/start`

Reply with a short welcome message:

```
NoterAI RAG Chat

Send me a question and I'll search your notes for answers.

Commands:
/clear — Reset conversation history
/status — Check RAG API health
/chatid — Show this chat's ID (for reminder config)
```

### `/clear`

Clear the conversation history for the current chat. Reply: `"Conversation cleared."`

### `/status`

Hit the NoterAI health endpoint and report back:

```python
async def status_command(update, context):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{TELEGRAM_RAG_URL.replace(':8084', ':8443')}/health")
            r.raise_for_status()
        await update.message.reply_text("NoterAI is online.")
    except Exception:
        await update.message.reply_text("NoterAI is unreachable.")
```

Adjust the health endpoint URL based on actual deployment — the main app and chat API may be on different ports.

---

## Scheduled Reminders

### Overview

The bot proactively sends reminder messages via Telegram at configured times of day. Rather than querying SQLite directly, it sends a canned prompt to the RAG chat API so the LLM generates a natural, in-character response. Since the RAG chat API already injects overdue and due-today reminders into the system prompt on every request, this works out of the box — the LLM sees the reminders and responds conversationally.

### APScheduler setup

Use `APScheduler`'s `AsyncIOScheduler` to schedule jobs inside the bot process. Initialize it after the Telegram `Application` is built but before `run_polling()`:

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

async def post_init(application):
    """Called after the bot application initializes."""
    scheduler = AsyncIOScheduler()
    for hour in TELEGRAM_REMINDER_HOURS:
        scheduler.add_job(
            send_scheduled_reminder,
            CronTrigger(hour=hour, minute=0),
            args=[application.bot],
            id=f"reminder_{hour}",
            replace_existing=True
        )
    scheduler.start()
```

Wire it up via the `post_init` hook on the application builder:

```python
app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
```

### Reminder function

```python
async def send_scheduled_reminder(bot):
    """Send a proactive reminder by querying the RAG chat API."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are sending a proactive scheduled reminder to the user via Telegram. "
                "Summarize what tasks are due or overdue. Be concise, conversational, "
                "and a little opinionated about what they should tackle first. "
                "If nothing is due, say so briefly and wish them well."
            )
        },
        {
            "role": "user",
            "content": "What tasks are due today or overdue?"
        }
    ]

    try:
        response = await query_rag(messages)

        # Only send if the LLM actually had something to say
        # (it will still respond if nothing is due, which is fine)
        await bot.send_message(
            chat_id=TELEGRAM_REMINDER_CHAT_ID,
            text=response
        )
    except Exception as e:
        # Log but don't crash the scheduler
        print(f"Scheduled reminder failed: {e}")
```

### Design notes

- The reminder uses the same `query_rag()` function as interactive messages, so it benefits from the same intent classification and folder routing. The prompt "What tasks are due today or overdue?" will route to the Todo folder.
- The system message steers the LLM to be proactive rather than reactive in tone. Adjust this prompt to taste — you could make it terse, sarcastic, encouraging, whatever fits.
- Scheduled reminders are **not** added to the conversation history dict. They're fire-and-forget messages, not part of an ongoing chat session.
- If the RAG API is down when the scheduler fires, the error is logged and the job runs again at the next scheduled hour. APScheduler does not retry missed jobs by default, which is the right behavior here — you don't want a backlog of reminders.
- The `TELEGRAM_REMINDER_CHAT_ID` is your personal chat ID with the bot, not a group. You can find it by sending `/start` to the bot and logging `update.effective_chat.id` in the handler.

### Finding your chat ID

Add a `/chatid` command (can be removed after setup):

```python
async def chatid_command(update, context):
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")
```

Use the returned value for `TELEGRAM_REMINDER_CHAT_ID` in `.env`.

---

## Entry Point

The bot runs as a standalone script:

```python
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Telegram bot started (long-polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
```

---

## Docker Integration

### Option A: Same container (simpler)

Add the Telegram bot as a second process in the existing container using a process manager like `supervisord`, or just a shell script that backgrounds both:

```bash
#!/bin/bash
uvicorn backend.main:app --ssl-keyfile ... --host 0.0.0.0 --port 8443 &
uvicorn backend.chat_api:app --port 8084 &
python -m backend.telegram_bot &
wait
```

### Option B: Separate container (cleaner)

Add a `telegram-bot` service to `docker-compose.yml`:

```yaml
telegram-bot:
  build: .
  command: python -m backend.telegram_bot
  env_file: .env
  depends_on:
    - noterai  # or whatever the main service is named
  restart: unless-stopped
  network_mode: host  # or use Docker network to reach localhost:8084
```

Option B is preferred — it isolates the bot and restarts independently.

### Outbound access

The bot container needs outbound HTTPS to `api.telegram.org`. No inbound ports are required — long-polling is outbound-only.

---

## Systemd Service (non-Docker)

If running without Docker, create a systemd user service:

```ini
# ~/.config/systemd/user/noterai-telegram.service
[Unit]
Description=NoterAI Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/noterai
EnvironmentFile=/path/to/noterai/.env
ExecStart=/path/to/noterai/.venv/bin/python -m backend.telegram_bot
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

---

## `.env.example` Additions

```bash
# Telegram Bot
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=          # comma-separated Telegram user IDs
TELEGRAM_MAX_HISTORY=20
TELEGRAM_RAG_URL=http://localhost:8084
TELEGRAM_RAG_MODEL=noterai-rag
TELEGRAM_REMINDER_HOURS=8,14     # hours (24h) to send scheduled reminders
TELEGRAM_REMINDER_CHAT_ID=       # your chat ID with the bot (use /chatid to find)
```

---

## Testing Checklist

- [ ] Bot token loads from `.env` and bot connects to Telegram via long-polling
- [ ] Messages from non-whitelisted user IDs are rejected
- [ ] `/start` replies with help text
- [ ] A plain text question hits the RAG chat API and returns a response with citations
- [ ] Conversation history is maintained across messages within a session
- [ ] `/clear` resets history and subsequent questions have no prior context
- [ ] `/status` reports NoterAI health correctly (online and offline cases)
- [ ] Responses longer than 4096 characters are split correctly
- [ ] Typing indicator shows while waiting for the RAG API
- [ ] Bot recovers gracefully if the RAG API is down (error message, no crash)
- [ ] Bot process restarts cleanly via Docker or systemd
- [ ] Works alongside the existing NoterAI containers without port conflicts
- [ ] `/chatid` returns the correct chat ID
- [ ] Scheduled reminder fires at configured hours and sends a message to Telegram
- [ ] Reminder response is conversational and reflects actual due/overdue tasks
- [ ] Reminder gracefully reports "nothing due" when no tasks are pending
- [ ] Scheduler recovers if RAG API is down at reminder time (logs error, no crash)
- [ ] Scheduled reminders do not pollute interactive conversation history
