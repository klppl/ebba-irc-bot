"""BTC price responder using CoinGecko (triggers: `.bitcoin`, `.b`)."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Iterable, Tuple

import requests

API_URL = "https://api.coingecko.com/api/v3/simple/price"
API_PARAMS = {"ids": "bitcoin", "vs_currencies": "usd"}
logger = logging.getLogger(__name__)

CONFIG_DEFAULTS = {
    "plugins": {
        "bitcoin": {
            "enabled": True,
            "triggers": ["bitcoin", "b"],
        }
    }
}

DEFAULT_TRIGGERS: Tuple[str, ...] = ("bitcoin", "b")


@dataclass
class BitcoinSettings:
    triggers: Tuple[str, ...] = DEFAULT_TRIGGERS


settings: BitcoinSettings = BitcoinSettings()


def on_load(bot) -> None:
    global settings
    settings = _settings_from_config(bot)
    trigger_text = ", ".join(f"{bot.prefix}{trigger}" for trigger in settings.triggers)
    logger.info("bitcoin plugin loaded from %s; responding to %s", __file__, trigger_text)


def on_unload(bot) -> None:
    global settings
    settings = BitcoinSettings()
    logger.info("bitcoin plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    prefix = bot.prefix
    if not message.startswith(prefix):
        return

    command = message[len(prefix) :].strip().split()
    if not command:
        return

    if command[0].lower() not in settings.triggers:
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


def _settings_from_config(bot) -> BitcoinSettings:
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else {}
    section = {}
    if isinstance(plugins_section, dict):
        candidate = plugins_section.get("bitcoin")
        if isinstance(candidate, dict):
            section = candidate

    triggers = _parse_triggers(section.get("triggers"), DEFAULT_TRIGGERS)
    return BitcoinSettings(triggers=triggers)


def _parse_triggers(raw: object, fallback: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(raw, str):
        cleaned = raw.strip()
        if cleaned:
            return (cleaned.lower(),)
    elif isinstance(raw, Iterable):
        normalized = []
        for item in raw:
            try:
                text = str(item).strip()
            except Exception:
                continue
            if text:
                normalized.append(text.lower())
        if normalized:
            return tuple(dict.fromkeys(normalized))
    return tuple(fallback)
