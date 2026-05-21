from __future__ import annotations

import os
import sqlite3

_DATABASE_URL = os.environ.get("DATABASE_URL", "./notes.db")


def _db_get(key: str) -> str | None:
    """Read from global app_settings."""
    try:
        with sqlite3.connect(_DATABASE_URL) as conn:
            cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _db_get_user(user_id: str, key: str) -> str | None:
    """Read from per-user user_settings."""
    try:
        with sqlite3.connect(_DATABASE_URL) as conn:
            cur = conn.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                (user_id, key),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _cfg(db_key: str, env_key: str, default: str = "") -> str:
    """user_settings[bot_user_id] → app_settings (legacy) → env var → default."""
    bot_user_id = _db_get("bot_user_id")
    if bot_user_id:
        val = _db_get_user(bot_user_id, db_key)
        if val:
            return val
    return _db_get(db_key) or os.environ.get(env_key) or default


# ── Required settings ────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = _cfg("telegram_bot_token", "TELEGRAM_BOT_TOKEN")

_reminder_chat_raw = _cfg("telegram_reminder_chat_id", "TELEGRAM_REMINDER_CHAT_ID", "0")
TELEGRAM_REMINDER_CHAT_ID: int = int(_reminder_chat_raw) if _reminder_chat_raw.strip() else 0

# ── Optional / defaulted settings ────────────────────────────────────────────

TELEGRAM_ALLOWED_USERS: list[int] = [
    int(u.strip())
    for u in _cfg("telegram_allowed_users", "TELEGRAM_ALLOWED_USERS").split(",")
    if u.strip()
]

TELEGRAM_MAX_HISTORY: int = int(_cfg("telegram_max_history", "TELEGRAM_MAX_HISTORY", "20"))

TELEGRAM_RAG_URL: str = _cfg("telegram_rag_url", "TELEGRAM_RAG_URL", "http://localhost:8084")

TELEGRAM_RAG_MODEL: str = _cfg("telegram_rag_model", "TELEGRAM_RAG_MODEL", "noterai-rag")
