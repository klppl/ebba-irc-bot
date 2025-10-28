"""Plugin template demonstrating structure and config defaults.

Copy this file to `scripts/<name>.py` and adjust the metadata below. Files whose
names start with an underscore are ignored by the loader, so this file is safe
to keep in-tree as a reference.

Configuration defaults declared in `CONFIG_DEFAULTS` are merged automatically
into `config.yaml` (and the running bot config) the first time the plugin is
loaded.
"""

import asyncio
import logging

CONFIG_DEFAULTS = {
    "plugins": {
        "example": {
            "enabled": False,
            "api_key": "",
        }
    }
}

logger = logging.getLogger(__name__)


def on_load(bot) -> None:
    """Called right after the plugin module is imported."""

    logger.info("example plugin loaded from %s", __file__)


def on_unload(bot) -> None:
    """Called before the plugin is removed or the bot exits."""

    logger.info("example plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    """Handle PRIVMSG events.

    Always return quickly; move long-running work into a background task.
    """

    if message.strip() != f"{bot.prefix}example":
        return

    loop = asyncio.get_running_loop()
    loop.create_task(bot.privmsg(channel, "Example plugin response"))
