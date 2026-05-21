from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from backend.core.plugin_base import NotePlugin, PluginContext

logger = logging.getLogger(__name__)


class TelegramPlugin(NotePlugin):
    name = "telegram"
    version = "1.0.0"
    description = "Telegram bot with scheduled task and journal reminders"

    _tasks: list[asyncio.Task]

    def __init__(self):
        self._tasks = []

    async def register(self, ctx: PluginContext) -> None:
        from .bot import start_all_bots
        self._tasks = await start_all_bots()

    async def shutdown(self, ctx: PluginContext) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def get_health(self) -> dict:
        running = sum(1 for t in self._tasks if not t.done())
        if not self._tasks:
            return {"status": "ok", "bots": 0}
        if running == 0:
            return {"status": "degraded", "reason": "all bot tasks have stopped", "bots": 0}
        return {"status": "ok", "bots": running}


plugin = TelegramPlugin()
