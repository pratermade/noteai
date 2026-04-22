from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from .config import settings
from .telegram_config import (
    TELEGRAM_ALLOWED_USERS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_MAX_HISTORY,
    TELEGRAM_RAG_MODEL,
    TELEGRAM_RAG_URL,
    TELEGRAM_REMINDER_CHAT_ID,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_DEFAULT_CHARACTER_PROMPT = (
    "You are Alfred, a dry witty butler assistant. "
    "You are helpful but value the user's time, so you keep banter quick and dry."
)

# Held at module level so the GC never collects it.
_scheduler: AsyncIOScheduler | None = None

# chat_id -> list of {"role": "user"|"assistant", "content": str}
conversations: dict[int, list[dict]] = {}

_TELEGRAM_LIMIT = 4096


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def restricted(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id not in TELEGRAM_ALLOWED_USERS:
            await update.message.reply_text("Not authorized.")
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# RAG API
# ---------------------------------------------------------------------------

async def _get_bot_user_id() -> str | None:
    """Return the user_id the bot operates as (stored in global app_settings)."""
    try:
        import sqlite3
        with sqlite3.connect(settings.database_url) as conn:
            cur = conn.execute("SELECT value FROM app_settings WHERE key = 'bot_user_id'")
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        logger.warning("Could not read bot_user_id: %s", exc)
        return None


async def query_rag(messages: list[dict], skip_reminders: bool = False) -> str:
    bot_user_id = await _get_bot_user_id()
    payload: dict = {
        "model": TELEGRAM_RAG_MODEL,
        "messages": messages,
        "stream": False,
        "skip_reminders": skip_reminders,
    }
    if bot_user_id:
        payload["user_id"] = bot_user_id
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{TELEGRAM_RAG_URL}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Reply helpers
# ---------------------------------------------------------------------------

async def safe_reply(update: Update, text: str) -> None:
    """Send response as MarkdownV2 if it contains code blocks, else plain text."""
    if "```" in text:
        try:
            await update.message.reply_text(
                escape_markdown(text, version=2), parse_mode="MarkdownV2"
            )
            return
        except BadRequest:
            pass
    await update.message.reply_text(text)


async def send_long(update: Update, text: str) -> None:
    """Split messages that exceed Telegram's 4096-char limit."""
    while len(text) > _TELEGRAM_LIMIT:
        cut = text.rfind("\n", 0, _TELEGRAM_LIMIT)
        if cut <= 0:
            cut = _TELEGRAM_LIMIT
        await safe_reply(update, text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        await safe_reply(update, text)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@restricted
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "NoterAI RAG Chat\n\n"
        "Send me a question and I'll search your notes for answers.\n\n"
        "Commands:\n"
        "/clear — Reset conversation history\n"
        "/status — Check RAG API health\n"
        "/remind — Send a reminder now (for testing)\n"
        "/chatid — Show this chat's ID (for reminder config)"
    )


@restricted
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conversations.pop(update.effective_chat.id, None)
    await update.message.reply_text("Conversation cleared.")


@restricted
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{TELEGRAM_RAG_URL}/v1/models")
            r.raise_for_status()
        await update.message.reply_text("NoterAI is online.")
    except Exception:
        await update.message.reply_text("NoterAI is unreachable.")


@restricted
async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")


@restricted
async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action(ChatAction.TYPING)
    await send_scheduled_reminder(context.bot)


@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text

    history = conversations.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        response = await query_rag(history)
    except Exception as exc:
        logger.error("RAG query failed: %s", exc)
        await update.message.reply_text("Sorry, I couldn't reach NoterAI right now.")
        history.pop()  # don't store failed turn
        return

    history.append({"role": "assistant", "content": response})

    # Trim history to MAX_HISTORY user+assistant pairs
    max_msgs = TELEGRAM_MAX_HISTORY * 2
    if len(history) > max_msgs:
        del history[: len(history) - max_msgs]

    await send_long(update, response)


# ---------------------------------------------------------------------------
# Scheduled reminders
# ---------------------------------------------------------------------------

async def send_scheduled_reminder(bot) -> None:
    bot_user_id = await _get_bot_user_id()
    char_prompt = await _get_character_prompt(bot_user_id)
    tz = await _get_timezone(bot_user_id)
    now_str = (datetime.now(tz) if tz else datetime.now()).strftime("%A, %B %-d %Y, %H:%M")
    today = (datetime.now(tz) if tz else datetime.now()).strftime("%Y-%m-%d")
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            if bot_user_id:
                async with conn.execute(
                    "SELECT title, reminder_at FROM notes "
                    "WHERE user_id = ? AND reminder_at <= ? AND reminder_done = 0 ORDER BY reminder_at",
                    (bot_user_id, today),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with conn.execute(
                    "SELECT title, reminder_at FROM notes "
                    "WHERE reminder_at <= ? AND reminder_done = 0 ORDER BY reminder_at",
                    (today,),
                ) as cur:
                    rows = await cur.fetchall()
    except Exception as exc:
        logger.error("Failed to fetch due tasks: %s", exc)
        return

    if not settings.summary_base_url:
        if not rows:
            await bot.send_message(chat_id=TELEGRAM_REMINDER_CHAT_ID, text="Nothing due today. Well done.")
        else:
            task_lines = [f"- {'Overdue' if r['reminder_at'] < today else 'Due today'} ({r['reminder_at']}): {r['title']}" for r in rows]
            await bot.send_message(chat_id=TELEGRAM_REMINDER_CHAT_ID, text="Due tasks:\n" + "\n".join(task_lines))
        return

    if not rows:
        messages = [
            {
                "role": "system",
                "content": (
                    f"Current date and time: {now_str}\n\n"
                    f"{char_prompt} "
                    "The user has no overdue or due tasks today — they are all caught up. "
                    "Send a brief, genuine well-done (1-2 sentences). "
                    "Warm but understated. No bullet points, no markdown, no headers."
                ),
            },
            {"role": "user", "content": "I have no tasks due today."},
        ]
    else:
        task_lines = []
        for r in rows:
            label = "Overdue" if r["reminder_at"] < today else "Due today"
            task_lines.append(f"- {label} ({r['reminder_at']}): {r['title']}")
        task_list = "Here are my due and overdue tasks:\n" + "\n".join(task_lines)
        messages = [
            {
                "role": "system",
                "content": (
                    f"Current date and time: {now_str}\n\n"
                    f"{char_prompt} "
                    "You are sending a proactive scheduled reminder via Telegram. "
                    "Summarize the provided due tasks — helpful, brief, and a touch wry. "
                    "Be a little opinionated about what they should tackle first. No bullet points."
                ),
            },
            {"role": "user", "content": task_list},
        ]
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.summary_base_url}/v1/chat/completions",
                json={"model": settings.summary_model, "messages": messages, "stream": False},
            )
            resp.raise_for_status()
            response = resp.json()["choices"][0]["message"]["content"]
        while len(response) > _TELEGRAM_LIMIT:
            cut = response.rfind("\n", 0, _TELEGRAM_LIMIT)
            if cut <= 0:
                cut = _TELEGRAM_LIMIT
            await bot.send_message(chat_id=TELEGRAM_REMINDER_CHAT_ID, text=response[:cut].rstrip())
            response = response[cut:].lstrip()
        if response:
            await bot.send_message(chat_id=TELEGRAM_REMINDER_CHAT_ID, text=response)
    except Exception as exc:
        logger.error("Scheduled reminder failed: %s", exc)


def _user_setting_sql(user_id: str | None, key: str) -> tuple[str, tuple]:
    if user_id:
        return (
            "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
            (user_id, key),
        )
    return ("SELECT value FROM app_settings WHERE key = ?", (key,))


async def _get_timezone(user_id: str | None = None) -> ZoneInfo | None:
    """Read server_timezone from user_settings (or app_settings fallback) and return a ZoneInfo."""
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            sql, params = _user_setting_sql(user_id, "server_timezone")
            async with conn.execute(sql, params) as cur:
                row = await cur.fetchone()
            tz_str = (row[0].strip() if row and row[0] else "") or os.environ.get("TZ", "")
        if tz_str:
            return ZoneInfo(tz_str)
    except ZoneInfoNotFoundError as exc:
        logger.warning("Unknown timezone '%s', falling back to system local: %s", exc, exc)
    except Exception as exc:
        logger.warning("Could not read server_timezone: %s", exc)
    return None


async def _get_reminder_times(user_id: str | None = None) -> list[tuple[int, int]]:
    """Return configured reminder times as (hour, minute) tuples from the DB."""
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            sql, params = _user_setting_sql(user_id, "reminder_times")
            async with conn.execute(sql, params) as cur:
                row = await cur.fetchone()
            if row:
                result = []
                for t in row[0].split(","):
                    t = t.strip()
                    if ":" in t:
                        h, m = t.split(":", 1)
                        result.append((int(h), int(m)))
                return result
            # Fall back to legacy reminder_hours
            sql2, params2 = _user_setting_sql(user_id, "reminder_hours")
            async with conn.execute(sql2, params2) as cur:
                row = await cur.fetchone()
            if row:
                return [(int(h.strip()), 0) for h in row[0].split(",") if h.strip()]
    except Exception as exc:
        logger.warning("Could not read reminder times from DB, using env default: %s", exc)
    return []


async def _get_journal_reminder_times(user_id: str | None = None) -> list[tuple[int, int]]:
    """Return configured journal reminder times from DB. Empty list = user hasn't opted in."""
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            sql, params = _user_setting_sql(user_id, "journal_reminder_times")
            async with conn.execute(sql, params) as cur:
                row = await cur.fetchone()
            if row and row[0].strip():
                result = []
                for t in row[0].split(","):
                    t = t.strip()
                    if ":" in t:
                        h, m = t.split(":", 1)
                        result.append((int(h), int(m)))
                return result
    except Exception as exc:
        logger.warning("Could not read journal_reminder_times from DB: %s", exc)
    return []


async def _get_character_prompt(user_id: str | None = None) -> str:
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            sql, params = _user_setting_sql(user_id, "character_prompt")
            async with conn.execute(sql, params) as cur:
                row = await cur.fetchone()
            if row and row[0].strip():
                return row[0].strip()
    except Exception:
        pass
    return _DEFAULT_CHARACTER_PROMPT


async def _check_and_send_journal_reminder(bot) -> None:
    """Check if a journal entry exists for today; nudge the user if not."""
    bot_user_id = await _get_bot_user_id()
    char_prompt = await _get_character_prompt(bot_user_id)
    tz = await _get_timezone(bot_user_id)
    now_str = (datetime.now(tz) if tz else datetime.now()).strftime("%A, %B %-d %Y, %H:%M")
    today = (datetime.now(tz) if tz else datetime.now()).strftime("%Y-%m-%d")
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            if bot_user_id:
                async with conn.execute(
                    "SELECT COUNT(*) FROM notes WHERE user_id = ? AND folder = 'Journal'"
                    " AND substr(created_at, 1, 10) = ?",
                    (bot_user_id, today),
                ) as cur:
                    row = await cur.fetchone()
            else:
                async with conn.execute(
                    "SELECT COUNT(*) FROM notes WHERE folder = 'Journal' AND substr(created_at, 1, 10) = ?",
                    (today,),
                ) as cur:
                    row = await cur.fetchone()
            count = row[0] if row else 0
    except Exception as exc:
        logger.error("Journal check failed: %s", exc)
        return

    if not settings.summary_base_url:
        if count > 0:
            await bot.send_message(chat_id=TELEGRAM_REMINDER_CHAT_ID, text="Journal entry written. Well done.")
        else:
            await bot.send_message(chat_id=TELEGRAM_REMINDER_CHAT_ID, text="Don't forget to write your journal entry today.")
        return

    if count > 0:
        logger.info("Journal entry exists for %s — sending kudos", today)
        messages = [
            {
                "role": "system",
                "content": (
                    f"Current date and time: {now_str}\n\n"
                    f"{char_prompt} "
                    "The user has already written their journal entry today. "
                    "Send a brief, genuine well-done (1-2 sentences). "
                    "Warm but understated. No bullet points, no markdown, no headers."
                ),
            },
            {"role": "user", "content": "I've written my journal entry for today."},
        ]
    else:
        logger.info("No journal entry for %s — sending reminder", today)
        messages = [
            {
                "role": "system",
                "content": (
                    f"Current date and time: {now_str}\n\n"
                    f"{char_prompt} "
                    "The user has not written a journal entry today. "
                    "Write a short nudge (2-3 sentences, under 100 words) encouraging them to "
                    "take a few minutes and reflect on their day. "
                    "Helpful, brief, and a touch wry. No bullet points, no markdown, no headers."
                ),
            },
            {"role": "user", "content": "Remind me to write my journal entry for today."},
        ]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.summary_base_url}/v1/chat/completions",
                json={"model": settings.summary_model, "messages": messages, "stream": False},
            )
            resp.raise_for_status()
            response = resp.json()["choices"][0]["message"]["content"]
        await bot.send_message(chat_id=TELEGRAM_REMINDER_CHAT_ID, text=response)
    except Exception as exc:
        logger.error("Journal reminder send failed: %s", exc)


async def _reschedule_journal_reminders(bot) -> None:
    """Read journal reminder times from DB and rebuild per-time cron jobs."""
    if _scheduler is None:
        return
    for job in _scheduler.get_jobs():
        if job.id.startswith("journal_") and job.id != "journal_manager":
            _scheduler.remove_job(job.id)

    bot_user_id = await _get_bot_user_id()
    times = await _get_journal_reminder_times(bot_user_id)
    tz = await _get_timezone(bot_user_id)
    for h, m in times:
        job_id = f"journal_{h:02d}{m:02d}"
        _scheduler.add_job(
            _check_and_send_journal_reminder,
            CronTrigger(hour=h, minute=m, timezone=tz),
            args=[bot],
            id=job_id,
            replace_existing=True,
        )
    logger.info("Journal reminders rescheduled: %s (tz=%s)", [f"{h:02d}:{m:02d}" for h, m in times], tz)


async def _reschedule_reminders(bot) -> None:
    """Read current reminder times from DB and rebuild the per-time cron jobs."""
    if _scheduler is None:
        return
    for job in _scheduler.get_jobs():
        if job.id.startswith("reminder_") and job.id != "reminder_manager":
            _scheduler.remove_job(job.id)

    bot_user_id = await _get_bot_user_id()
    times = await _get_reminder_times(bot_user_id)
    tz = await _get_timezone(bot_user_id)
    for h, m in times:
        job_id = f"reminder_{h:02d}{m:02d}"
        _scheduler.add_job(
            send_scheduled_reminder,
            CronTrigger(hour=h, minute=m, timezone=tz),
            args=[bot],
            id=job_id,
            replace_existing=True,
        )
    logger.info("Reminders rescheduled: %s (tz=%s)", [f"{h:02d}:{m:02d}" for h, m in times], tz)


async def post_init(application) -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler()
    bot_user_id = await _get_bot_user_id()
    tz = await _get_timezone(bot_user_id)
    _scheduler.add_job(
        _reschedule_reminders,
        CronTrigger(minute="0,30", timezone=tz),
        args=[application.bot],
        id="reminder_manager",
        replace_existing=True,
    )
    _scheduler.add_job(
        _reschedule_journal_reminders,
        CronTrigger(minute="0,30", timezone=tz),
        args=[application.bot],
        id="journal_manager",
        replace_existing=True,
    )
    _scheduler.start()
    await _reschedule_reminders(application.bot)
    await _reschedule_journal_reminders(application.bot)
    logger.info("Reminder scheduler started (tz=%s)", tz)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("chatid", chatid_command))
    app.add_handler(CommandHandler("remind", remind_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Telegram bot started (long-polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
