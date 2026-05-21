from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from .config import Settings


@dataclass
class PluginContext:
    app: FastAPI
    scheduler: AsyncIOScheduler
    get_db: Callable
    settings: Settings
    get_current_user: Callable


class NotePlugin(ABC):
    name: str
    version: str
    description: str

    @abstractmethod
    async def register(self, ctx: PluginContext) -> None:
        """
        Called once at startup. Mount routes, schedule jobs, run migrations.
        Must complete in under 2 seconds. Must be idempotent.
        """

    async def shutdown(self, ctx: PluginContext) -> None:
        """Called on app shutdown. Cancel jobs, close connections."""

    async def get_health(self) -> dict:
        """Returns {"status": "ok"} or {"status": "degraded", "reason": "..."}."""
        return {"status": "ok"}
