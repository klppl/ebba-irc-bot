"""Minimal plugin template for new scripts.

Copy this file to `scripts/<name>.py` and adjust the values below. Files whose
names start with an underscore are ignored by the loader.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

CONFIG_DEFAULTS = {
    "plugins": {
        "example": {
            "enabled": False,
            "triggers": ["example"],
        }
    }
}


def on_load(bot):
    triggers = _triggers(bot)
    prefix = getattr(bot, "prefix", ".")
    names = ", ".join(f"{prefix}{trigger}" for trigger in triggers) or "no trigger"
    logger.info("example plugin loaded from %s; responding to %s", __file__, names)


def on_unload(bot):
    logger.info("example plugin unloaded")


def on_message(bot, user, channel, message):
    prefix = getattr(bot, "prefix", ".")
    if not message.startswith(prefix):
        return

    command = message[len(prefix) :].strip().lower()
    if command not in _triggers(bot):
        return

    asyncio.get_running_loop().create_task(
        bot.privmsg(channel, "Example plugin response")
    )


def _triggers(bot):
    default_triggers = CONFIG_DEFAULTS["plugins"]["example"]["triggers"]
    return list(default_triggers)


 
