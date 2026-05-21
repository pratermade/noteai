from __future__ import annotations

import logging

from backend.core.plugin_base import NotePlugin, PluginContext

logger = logging.getLogger(__name__)


class ChatRagPlugin(NotePlugin):
    name = "chat_rag"
    version = "1.0.0"
    description = "OpenAI-compatible RAG chat completions (/v1/chat/completions)"

    _close_fn = None

    async def register(self, ctx: PluginContext) -> None:
        from .chat_api import router, close_llm_client
        self._close_fn = close_llm_client
        ctx.app.include_router(router)

    async def shutdown(self, ctx: PluginContext) -> None:
        if self._close_fn:
            await self._close_fn()


plugin = ChatRagPlugin()
