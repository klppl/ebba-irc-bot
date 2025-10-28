"""Responds to Swedish “är det fredag?” queries with weekday status."""

import asyncio
import datetime
import logging
import re

FRIDAY_PATTERN = re.compile(r"är\s+det\s+fredag\??", re.IGNORECASE)
FRIDAY_INDEX = 4  # Monday=0
FRIDAY_URL = "https://rebecca.blackfriday"
logger = logging.getLogger(__name__)

CONFIG_DEFAULTS = {
    "plugins": {
        "fredag": {
            "enabled": True,
        }
    }
}


def on_load(bot) -> None:
    logger.info("fredag plugin loaded from %s", __file__)


def on_unload(bot) -> None:
    logger.info("fredag plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    if not FRIDAY_PATTERN.search(message):
        return

    loop = asyncio.get_running_loop()
    loop.create_task(_handle_fredag(bot, channel))


async def _handle_fredag(bot, channel: str) -> None:
    try:
        weekday = datetime.datetime.today().weekday()
    except Exception:
        logger.exception("fredag plugin failed to determine weekday")
        await bot.privmsg(channel, "Ursäkta, jag kan inte kolla datum just nu.")
        return

    response = f"JA! {FRIDAY_URL}" if weekday == FRIDAY_INDEX else "NEJ, det är inte fredag."
    await bot.privmsg(channel, response)
