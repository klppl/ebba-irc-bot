"""Minimal plugin template for new scripts.

Copy this file to `scripts/<name>.py` and adjust the values below. Files whose
names start with an underscore are ignored by the loader.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

DEFAULT_TRIGGERS = ["example"]

CONFIG_DEFAULTS = {
    "plugins": {
        "example": {
            "enabled": False,
            "triggers": DEFAULT_TRIGGERS,
        }
    }
}


def on_load(bot):
    triggers = _triggers(bot)
    prefix = getattr(bot, "prefix", ".")
    names = ", ".join(f"{prefix}{trigger}" for trigger in triggers) or "no trigger"
    logger.info("example plugin ready (listening to %s)", names)


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
    config = getattr(bot, "config", {}) or {}
    plugins = config.get("plugins", {}) if isinstance(config, dict) else {}
    section = plugins.get("example", {}) if isinstance(plugins, dict) else {}
    raw = section.get("triggers")
    normalized = _normalize_triggers(raw)
    return normalized or list(DEFAULT_TRIGGERS)


def _normalize_triggers(raw):
    if isinstance(raw, str):
        text = raw.strip().lower()
        return [text] if text else []

    if isinstance(raw, (list, tuple, set)):
        result = []
        for item in raw:
            text = str(item).strip().lower()
            if text and text not in result:
                result.append(text)
        return result

    return []
