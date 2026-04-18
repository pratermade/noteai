from __future__ import annotations

import os

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]

TELEGRAM_ALLOWED_USERS: list[int] = [
    int(u.strip()) for u in os.environ["TELEGRAM_ALLOWED_USERS"].split(",") if u.strip()
]

TELEGRAM_MAX_HISTORY: int = int(os.environ.get("TELEGRAM_MAX_HISTORY", "20"))

TELEGRAM_RAG_URL: str = os.environ.get("TELEGRAM_RAG_URL", "http://localhost:8084")

TELEGRAM_RAG_MODEL: str = os.environ.get("TELEGRAM_RAG_MODEL", "noterai-rag")

TELEGRAM_REMINDER_HOURS: list[int] = [
    int(h.strip())
    for h in os.environ.get("TELEGRAM_REMINDER_HOURS", "8,14").split(",")
    if h.strip()
]

TELEGRAM_REMINDER_CHAT_ID: int = int(os.environ["TELEGRAM_REMINDER_CHAT_ID"])
