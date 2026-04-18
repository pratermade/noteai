from __future__ import annotations

import asyncio
import logging
from functools import wraps

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import ChatAction, Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from .telegram_config import (
    TELEGRAM_ALLOWED_USERS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_MAX_HISTORY,
    TELEGRAM_RAG_MODEL,
    TELEGRAM_RAG_URL,
    TELEGRAM_REMINDER_CHAT_ID,
    TELEGRAM_REMINDER_HOURS,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

async def query_rag(messages: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{TELEGRAM_RAG_URL}/v1/chat/completions",
            json={"model": TELEGRAM_RAG_MODEL, "messages": messages, "stream": False},
        )
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
    messages = [
        {
            "role": "system",
            "content": (
                "You are sending a proactive scheduled reminder to the user via Telegram. "
                "Summarize what tasks are due or overdue. Be concise, conversational, "
                "and a little opinionated about what they should tackle first. "
                "If nothing is due, say so briefly and wish them well."
            ),
        },
        {"role": "user", "content": "What tasks are due today or overdue?"},
    ]
    try:
        response = await query_rag(messages)
        # Split if over limit
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


async def post_init(application) -> None:
    scheduler = AsyncIOScheduler()
    for hour in TELEGRAM_REMINDER_HOURS:
        scheduler.add_job(
            send_scheduled_reminder,
            CronTrigger(hour=hour, minute=0),
            args=[application.bot],
            id=f"reminder_{hour}",
            replace_existing=True,
        )
    scheduler.start()
    logger.info("Reminder scheduler started for hours: %s", TELEGRAM_REMINDER_HOURS)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("chatid", chatid_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Telegram bot started (long-polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
