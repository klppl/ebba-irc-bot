"""BTC price responder using CoinGecko (trigger: `.bitcoin`)."""

import asyncio
import logging

import requests

API_URL = "https://api.coingecko.com/api/v3/simple/price"
API_PARAMS = {"ids": "bitcoin", "vs_currencies": "usd"}
logger = logging.getLogger(__name__)


def on_load(bot) -> None:
    logger.info("bitcoin plugin loaded from %s; responding to %sbitcoin", __file__, bot.prefix)


def on_unload(bot) -> None:
    logger.info("bitcoin plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    prefix = bot.prefix
    if not message.startswith(prefix):
        return

    command = message[len(prefix) :].strip().split()
    if not command or command[0].lower() != "bitcoin":
        return

    loop = asyncio.get_running_loop()
    loop.create_task(_handle_bitcoin_command(bot, channel))


async def _handle_bitcoin_command(bot, channel: str) -> None:
    try:
        price = await _fetch_price(bot.request_timeout)
    except Exception:
        logger.exception("Failed to fetch BTC price")
        await bot.privmsg(channel, "BTC price unavailable")
        return

    await bot.privmsg(channel, f"$ {price}")


async def _fetch_price(timeout: int) -> int:
    loop = asyncio.get_running_loop()

    def request_price() -> int:
        response = requests.get(API_URL, params=API_PARAMS, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        price = payload.get("bitcoin", {}).get("usd")
        if price is None:
            raise ValueError("Unexpected response payload")
        return int(round(float(price)))

    return await loop.run_in_executor(None, request_price)
