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
    TELEGRAM_MAX_HISTORY,
    TELEGRAM_RAG_MODEL,
    TELEGRAM_RAG_URL,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_DEFAULT_CHARACTER_PROMPT = (
    "You are Alfred, a dry witty butler assistant. "
    "You are helpful but value the user's time, so you keep banter quick and dry."
)

# chat_id -> list of {"role": "user"|"assistant", "content": str}
conversations: dict[int, list[dict]] = {}

# token (first 10 chars) -> owner NoterAI user_id — populated in main() before bots start
_bot_owner_map: dict[str, str] = {}

_TELEGRAM_LIMIT = 4096


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

async def _get_all_users_with_setting(key: str) -> list[tuple[str, str]]:
    """Return [(user_id, value)] for all users who have `key` set in user_settings."""
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            async with conn.execute(
                "SELECT user_id, value FROM user_settings WHERE key = ?", (key,)
            ) as cur:
                rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows if r[1] and r[1].strip()]
    except Exception as exc:
        logger.warning("Could not read user settings for %s: %s", key, exc)
        return []


async def _get_user_setting(user_id: str, key: str, default: str = "") -> str:
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            sql, params = _user_setting_sql(user_id, key)
            async with conn.execute(sql, params) as cur:
                row = await cur.fetchone()
            return row[0].strip() if row and row[0] else default
    except Exception:
        return default


async def _get_all_allowed_users() -> set[int]:
    """Union of all users' telegram_allowed_users settings, with legacy app_settings fallback."""
    entries = await _get_all_users_with_setting("telegram_allowed_users")
    result: set[int] = set()
    for _, val in entries:
        for u in val.split(","):
            u = u.strip()
            if u:
                try:
                    result.add(int(u))
                except ValueError:
                    pass
    if not result:
        # Fall back to legacy global setting if no per-user entries exist yet
        try:
            import sqlite3
            with sqlite3.connect(settings.database_url) as conn:
                cur = conn.execute("SELECT value FROM app_settings WHERE key = 'telegram_allowed_users'")
                row = cur.fetchone()
                if row and row[0]:
                    for u in row[0].split(","):
                        u = u.strip()
                        if u:
                            try:
                                result.add(int(u))
                            except ValueError:
                                pass
        except Exception:
            pass
    return result


async def _get_notera_user_id_for_telegram(telegram_user_id: int) -> str | None:
    """Return the NoterAI user_id whose telegram_user_id setting matches."""
    entries = await _get_all_users_with_setting("telegram_user_id")
    for user_id, val in entries:
        try:
            if int(val.strip()) == telegram_user_id:
                return user_id
        except ValueError:
            pass
    return None


async def _get_allowed_users_for_bot(owner_user_id: str | None) -> set[int]:
    """Return allowed Telegram user IDs for this bot's owner."""
    if not owner_user_id:
        return await _get_all_allowed_users()
    val = await _get_user_setting(owner_user_id, "telegram_allowed_users", "")
    result: set[int] = set()
    for u in val.split(","):
        u = u.strip()
        if u:
            try:
                result.add(int(u))
            except ValueError:
                pass
    return result


def restricted(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_user_id = _bot_owner_map.get(context.bot.token)
        allowed = await _get_allowed_users_for_bot(owner_user_id)
        if update.effective_user and update.effective_user.id not in allowed:
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


async def query_rag(messages: list[dict], user_id: str | None = None, skip_reminders: bool = False) -> str:
    rag_url   = (await _get_user_setting(user_id, "telegram_rag_url",   TELEGRAM_RAG_URL))   if user_id else TELEGRAM_RAG_URL
    rag_model = (await _get_user_setting(user_id, "telegram_rag_model", TELEGRAM_RAG_MODEL)) if user_id else TELEGRAM_RAG_MODEL
    payload: dict = {
        "model": rag_model,
        "messages": messages,
        "stream": False,
        "skip_reminders": skip_reminders,
        "skip_footer": True,
    }
    if user_id:
        payload["user_id"] = user_id
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{rag_url}/v1/chat/completions", json=payload)
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
    owner_user_id = _bot_owner_map.get(context.bot.token)
    rag_url = await _get_user_setting(owner_user_id, "telegram_rag_url", TELEGRAM_RAG_URL) if owner_user_id else TELEGRAM_RAG_URL
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{rag_url}/v1/models")
            r.raise_for_status()
        await update.message.reply_text("NoterAI is online.")
    except Exception:
        await update.message.reply_text("NoterAI is unreachable.")


async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")


@restricted
async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action(ChatAction.TYPING)
    owner_user_id = _bot_owner_map.get(context.bot.token)
    telegram_sender_id = update.effective_user.id if update.effective_user else None
    user_id = None
    if telegram_sender_id:
        user_id = await _get_notera_user_id_for_telegram(telegram_sender_id)
    if not user_id:
        user_id = owner_user_id or await _get_bot_user_id()
    if user_id:
        await send_scheduled_reminder(context.bot, user_id)


@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text
    owner_user_id = _bot_owner_map.get(context.bot.token)
    logger.info("handle_message: bot=%s owner_user_id=%s", context.bot.token[:10], owner_user_id)

    telegram_sender_id = update.effective_user.id if update.effective_user else None
    notera_user_id = None
    if telegram_sender_id:
        notera_user_id = await _get_notera_user_id_for_telegram(telegram_sender_id)
    if not notera_user_id:
        notera_user_id = owner_user_id or await _get_bot_user_id()

    history = conversations.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        response = await query_rag(history, user_id=notera_user_id)
    except Exception as exc:
        logger.error("RAG query failed: %s", exc)
        await update.message.reply_text("Sorry, I couldn't reach NoterAI right now.")
        history.pop()  # don't store failed turn
        return

    history.append({"role": "assistant", "content": response})

    max_history_raw = await _get_user_setting(notera_user_id or "", "telegram_max_history", str(TELEGRAM_MAX_HISTORY))
    max_msgs = int(max_history_raw) * 2
    if len(history) > max_msgs:
        del history[: len(history) - max_msgs]

    await send_long(update, response)


# ---------------------------------------------------------------------------
# Scheduled reminders
# ---------------------------------------------------------------------------

async def send_scheduled_reminder(bot, user_id: str) -> None:
    chat_id_raw = await _get_user_setting(user_id, "telegram_reminder_chat_id", "")
    if not chat_id_raw:
        return
    chat_id = int(chat_id_raw)
    char_prompt = await _get_character_prompt(user_id)
    tz = await _get_timezone(user_id)
    now_str = (datetime.now(tz) if tz else datetime.now()).strftime("%A, %B %-d %Y, %H:%M")
    today = (datetime.now(tz) if tz else datetime.now()).strftime("%Y-%m-%d")
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT title, reminder_at FROM notes "
                "WHERE user_id = ? AND reminder_at <= ? AND reminder_done = 0 ORDER BY reminder_at",
                (user_id, today),
            ) as cur:
                rows = await cur.fetchall()
    except Exception as exc:
        logger.error("Failed to fetch due tasks: %s", exc)
        return

    if not settings.summary_base_url:
        if not rows:
            await bot.send_message(chat_id=chat_id, text="Nothing due today. Well done.")
        else:
            task_lines = [f"- {'Overdue' if r['reminder_at'] < today else 'Due today'} ({r['reminder_at']}): {r['title']}" for r in rows]
            await bot.send_message(chat_id=chat_id, text="Due tasks:\n" + "\n".join(task_lines))
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
            await bot.send_message(chat_id=chat_id, text=response[:cut].rstrip())
            response = response[cut:].lstrip()
        if response:
            await bot.send_message(chat_id=chat_id, text=response)
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


async def _check_and_send_journal_reminder(bot, user_id: str) -> None:
    """Check if a journal entry exists for today; nudge the user if not."""
    chat_id_raw = await _get_user_setting(user_id, "telegram_reminder_chat_id", "")
    if not chat_id_raw:
        return
    chat_id = int(chat_id_raw)
    char_prompt = await _get_character_prompt(user_id)
    tz = await _get_timezone(user_id)
    now_str = (datetime.now(tz) if tz else datetime.now()).strftime("%A, %B %-d %Y, %H:%M")
    today = (datetime.now(tz) if tz else datetime.now()).strftime("%Y-%m-%d")
    try:
        async with aiosqlite.connect(settings.database_url) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM notes WHERE user_id = ? AND folder = 'Journal'"
                " AND substr(created_at, 1, 10) = ?",
                (user_id, today),
            ) as cur:
                row = await cur.fetchone()
            count = row[0] if row else 0
    except Exception as exc:
        logger.error("Journal check failed: %s", exc)
        return

    if not settings.summary_base_url:
        if count > 0:
            await bot.send_message(chat_id=chat_id, text="Journal entry written. Well done.")
        else:
            await bot.send_message(chat_id=chat_id, text="Don't forget to write your journal entry today.")
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
        await bot.send_message(chat_id=chat_id, text=response)
    except Exception as exc:
        logger.error("Journal reminder send failed: %s", exc)


async def _reschedule_reminders_for_user(bot, user_id: str, scheduler: AsyncIOScheduler) -> None:
    prefix = f"reminder_{user_id[:8]}_"
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            scheduler.remove_job(job.id)
    tz = await _get_timezone(user_id)
    times = await _get_reminder_times(user_id)
    for h, m in times:
        scheduler.add_job(
            send_scheduled_reminder,
            CronTrigger(hour=h, minute=m, timezone=tz),
            args=[bot, user_id],
            id=f"{prefix}{h:02d}{m:02d}",
            replace_existing=True,
        )
    logger.info("Reminders rescheduled for user %s (%d jobs)", user_id, len(times))


async def _reschedule_journal_for_user(bot, user_id: str, scheduler: AsyncIOScheduler) -> None:
    prefix = f"journal_{user_id[:8]}_"
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            scheduler.remove_job(job.id)
    tz = await _get_timezone(user_id)
    times = await _get_journal_reminder_times(user_id)
    for h, m in times:
        scheduler.add_job(
            _check_and_send_journal_reminder,
            CronTrigger(hour=h, minute=m, timezone=tz),
            args=[bot, user_id],
            id=f"{prefix}{h:02d}{m:02d}",
            replace_existing=True,
        )
    logger.info("Journal reminders rescheduled for user %s (%d jobs)", user_id, len(times))


def make_post_init(owner_user_id: str):
    async def _post_init(application) -> None:
        logger.info("post_init: owner_user_id=%s bot=%s", owner_user_id, application.bot.token[:10])
        scheduler = AsyncIOScheduler()
        application.bot_data["owner_user_id"] = owner_user_id
        application.bot_data["scheduler"] = scheduler
        logger.info("post_init: bot_data set for user=%s", owner_user_id)
        tz = await _get_timezone(owner_user_id)
        scheduler.add_job(
            _reschedule_reminders_for_user,
            CronTrigger(minute="0,30", timezone=tz),
            args=[application.bot, owner_user_id, scheduler],
            id=f"reminder_manager_{owner_user_id[:8]}",
            replace_existing=True,
        )
        scheduler.add_job(
            _reschedule_journal_for_user,
            CronTrigger(minute="0,30", timezone=tz),
            args=[application.bot, owner_user_id, scheduler],
            id=f"journal_manager_{owner_user_id[:8]}",
            replace_existing=True,
        )
        scheduler.start()
        await _reschedule_reminders_for_user(application.bot, owner_user_id, scheduler)
        await _reschedule_journal_for_user(application.bot, owner_user_id, scheduler)
        logger.info("Bot started for user %s (tz=%s)", owner_user_id, tz)
    return _post_init


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _add_handlers(app) -> None:
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("chatid", chatid_command))
    app.add_handler(CommandHandler("remind", remind_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


async def _run_app(app) -> None:
    async with app:
        # In PTB v20+, post_init is only called by run_polling/run_webhook, not initialize()
        if app.post_init:
            await app.post_init(app)
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await asyncio.sleep(float("inf"))


async def main() -> None:
    tokens = await _get_all_users_with_setting("telegram_bot_token")
    if not tokens:
        logger.error("No telegram_bot_token found in user_settings. Configure via web UI Settings.")
        return

    apps = []
    for owner_user_id, token in tokens:
        _bot_owner_map[token] = owner_user_id
        app = ApplicationBuilder().token(token).post_init(make_post_init(owner_user_id)).build()
        _add_handlers(app)
        apps.append(app)
        logger.info("Built bot for user %s token=%s", owner_user_id, token[:10])

    logger.info("Starting %d bot(s)", len(apps))
    async with asyncio.TaskGroup() as tg:
        for app in apps:
            tg.create_task(_run_app(app))


if __name__ == "__main__":
    asyncio.run(main())
