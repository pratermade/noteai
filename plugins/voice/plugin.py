from __future__ import annotations

import logging

from backend.core.plugin_base import NotePlugin, PluginContext

logger = logging.getLogger(__name__)


class VoicePlugin(NotePlugin):
    name = "voice"
    version = "1.0.0"
    description = "Wyoming/Whisper voice transcription for journal dictation"

    async def register(self, ctx: PluginContext) -> None:
        from .transcriber import router
        ctx.app.include_router(router, prefix="/api")


plugin = VoicePlugin()
