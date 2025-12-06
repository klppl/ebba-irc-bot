"""Avanza stock lookup (trigger: `.avanza`)."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.avanza.se"
SEARCH_ENDPOINT = f"{BASE_URL}/_api/search/filtered-search"
PRICE_ENDPOINT = f"{BASE_URL}/_api/price-chart/stock/{{orderbook_id}}"


CONFIG_DEFAULTS = {
    "plugins": {
        "avanza": {
            "enabled": True,
            "triggers": ["avanza"],
        }
    }
}


@dataclass
class AvanzaSettings:
    triggers: Tuple[str, ...] = tuple(CONFIG_DEFAULTS["plugins"]["avanza"]["triggers"])


settings = AvanzaSettings()


def on_load(bot) -> None:
    global settings
    settings = _settings_from_config(bot)
    prefix = getattr(bot, "prefix", ".")
    trigger_text = ", ".join(f"{prefix}{trigger}" for trigger in settings.triggers)
    logger.info("avanza plugin loaded from %s; responding to %s", __file__, trigger_text)


def on_unload(bot) -> None:
    global settings
    settings = AvanzaSettings()
    logger.info("avanza plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    prefix = getattr(bot, "prefix", ".")
    if not message.startswith(prefix):
        return

    command_line = message[len(prefix) :].strip()
    if not command_line:
        return

    parts = command_line.split(maxsplit=1)
    command = parts[0].lower()
    if command not in settings.triggers:
        return

    query = parts[1].strip() if len(parts) > 1 else ""
    loop = asyncio.get_running_loop()
    loop.create_task(_handle_avanza(bot, channel, query), name="avanza-command")


async def _handle_avanza(bot, channel: str, query: str) -> None:
    if not query:
        await bot.privmsg(channel, f"Usage: {bot.prefix}avanza <stock name>")
        return

    try:
        response = await _fetch_quote_text(query, bot.request_timeout)
    except requests.RequestException:
        logger.warning("Avanza request failed for query %s", query, exc_info=True)
        await bot.privmsg(channel, "Avanza request failed; try again later.")
        return
    except Exception:
        logger.exception("Unhandled error during Avanza lookup for %s", query)
        await bot.privmsg(channel, "Avanza lookup failed.")
        return

    await bot.privmsg(channel, response)


async def _fetch_quote_text(query: str, timeout: int) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _blocking_fetch_quote_text(query, timeout)
    )


def _blocking_fetch_quote_text(query: str, timeout: int) -> str:
    ob_id, hit = search_stock(query, timeout)
    if not ob_id:
        return "No order book id found; cannot fetch chart."

    try:
        payload = fetch_chart_data(ob_id, timeout)
    except ValueError:
        return "Non-JSON response"
    latest = latest_close_from_chart(payload)
    if latest is None:
        return "No OHLC data found."

    title = hit.get("title") or f"Order book {ob_id}"
    url = f"{BASE_URL}/aktier/om-aktien.html/{ob_id}"
    return f"{title} - Price: {latest} - {url}"


def latest_close_from_chart(payload: Dict[str, Any]) -> Optional[float]:
    ohlc = payload.get("ohlc") or []
    if not ohlc:
        return None
    latest = ohlc[-1]
    try:
        return float(latest.get("close"))
    except (TypeError, ValueError):
        return None


def fetch_chart_data(orderbook_id: int, timeout: int) -> Dict[str, Any]:
    params = {"timePeriod": "today"}
    response = requests.get(
        PRICE_ENDPOINT.format(orderbook_id=orderbook_id),
        params=params,
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        raise ValueError("Non-JSON response from Avanza chart API")


def search_stock(query: str, timeout: int) -> Tuple[Optional[int], Dict[str, Any]]:
    options = {
        "query": query,
        "searchFilter": {"types": ["STOCK"]},
        "pagination": {"from": 0, "size": 10},
    }
    response = requests.post(SEARCH_ENDPOINT, json=options, timeout=timeout)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        return None, {}

    hits = data.get("hits") or []
    if not hits:
        return None, data

    first = hits[0]

    for key in ("orderBookId", "orderbookId", "orderbookid", "id"):
        if key in first:
            try:
                return int(first[key]), first
            except (TypeError, ValueError):
                continue

    path = first.get("path") or ""
    digits = "".join(ch if ch.isdigit() else " " for ch in path).split()
    if digits:
        try:
            return int(digits[-1]), first
        except ValueError:
            pass

    return None, first


def _settings_from_config(bot) -> AvanzaSettings:
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else {}
    if not isinstance(plugins_section, dict):
        return AvanzaSettings()

    section = plugins_section.get("avanza")
    if isinstance(section, dict):
        # Reserved for future settings; triggers remain script-defined
        _ = section

    return AvanzaSettings()
