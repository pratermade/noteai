from __future__ import annotations

import importlib
import logging
from pathlib import Path

from .plugin_base import NotePlugin, PluginContext

logger = logging.getLogger(__name__)


def discover_plugins() -> list[NotePlugin]:
    """
    Scans the plugins/ directory at repo root.
    For each subdirectory with __init__.py that exports a `plugin` NotePlugin instance,
    collects and returns it. Ordered alphabetically for determinism.
    ImportError per plugin is caught and logged — one broken plugin won't prevent others.
    """
    plugins_dir = Path(__file__).parent.parent.parent / "plugins"
    result: list[NotePlugin] = []
    if not plugins_dir.exists():
        logger.info("No plugins/ directory found at %s — skipping discovery", plugins_dir)
        return result
    for subdir in sorted(plugins_dir.iterdir()):
        if not subdir.is_dir() or not (subdir / "__init__.py").exists():
            continue
        try:
            mod = importlib.import_module(f"plugins.{subdir.name}")
            plugin = getattr(mod, "plugin", None)
            if isinstance(plugin, NotePlugin):
                result.append(plugin)
            else:
                logger.warning("plugins.%s: no `plugin` NotePlugin instance — skipping", subdir.name)
        except ImportError:
            logger.error("Failed to import plugin %s", subdir.name, exc_info=True)
    return result


async def register_all_plugins(plugins: list[NotePlugin], ctx: PluginContext) -> None:
    for plugin in plugins:
        try:
            logger.info("Registering plugin %s", plugin.name)
            await plugin.register(ctx)
            logger.info("Plugin registered: %s v%s", plugin.name, plugin.version)
        except Exception:
            logger.error("Plugin registration failed: %s", plugin.name, exc_info=True)


async def shutdown_all_plugins(plugins: list[NotePlugin], ctx: PluginContext) -> None:
    for plugin in plugins:
        try:
            await plugin.shutdown(ctx)
        except Exception:
            logger.error("Plugin shutdown failed: %s", plugin.name, exc_info=True)
