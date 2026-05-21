"""
user_settings keys owned by the telegram plugin.
All keys are prefixed with 'telegram_'.
"""

SETTINGS_KEYS = [
    "telegram_bot_token",
    "telegram_allowed_users",
    "telegram_reminder_chat_id",
    "telegram_user_id",
    "telegram_rag_url",
    "telegram_rag_model",
    "telegram_max_history",
    "telegram_reminder_times",   # inherited from core reminder_times
    "telegram_journal_times",    # inherited from core journal_reminder_times
]
