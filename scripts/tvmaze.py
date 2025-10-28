"""TVMaze next-episode lookup (config: `plugins.tvmaze.timezone`, optional)."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)


API_URL = "https://api.tvmaze.com/singlesearch/shows"
API_HEADERS = {"User-Agent": "ebba-irc-bot tvmaze plugin (+https://github.com/alex/ebba-irc-bot)"}
DEFAULT_TIMEZONE = "UTC"

COMMANDS = {"next", "n"}


@dataclass
class TvMazeSettings:
    timezone: ZoneInfo


def on_load(bot) -> None:
    logger.info("tvmaze plugin loaded from %s", __file__)


def on_unload(bot) -> None:
    logger.info("tvmaze plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    prefix = bot.prefix
    if not message.startswith(prefix):
        return

    command_line = message[len(prefix) :].strip()
    if not command_line:
        return

    parts = command_line.split(maxsplit=1)
    command = parts[0].lower()
    if command not in COMMANDS:
        return

    query = parts[1].strip() if len(parts) > 1 else ""

    loop = asyncio.get_running_loop()
    loop.create_task(_handle_tvmaze(bot, channel, query))


async def _handle_tvmaze(bot, channel: str, query: str) -> None:
    settings = _settings_from_config(bot)

    if not query:
        await bot.privmsg(channel, "🎬 Enter a TV show to search for.")
        return

    try:
        info = await _fetch_show_info(query, settings, bot.request_timeout)
    except requests.RequestException:
        logger.warning("tvmaze request failed for query %s", query, exc_info=True)
        await bot.privmsg(channel, f"🎬 Unable to reach TVMaze for '{query}'.")
        return
    except Exception:
        logger.exception("tvmaze lookup failed for query %s", query)
        await bot.privmsg(channel, f"🎬 Something went wrong fetching '{query}'.")
        return

    if info is None:
        await bot.privmsg(channel, f"🎬 Could not find upcoming episodes for '{query}'.")
        return

    await bot.privmsg(channel, f"🎬 {info}")


def _settings_from_config(bot) -> TvMazeSettings:
    config = getattr(bot, "config", {})
    section = config.get("tvmaze") if isinstance(config, dict) else {}

    tz_name = DEFAULT_TIMEZONE
    if isinstance(section, dict):
        tz_name = section.get("timezone", DEFAULT_TIMEZONE)

    try:
        tz = ZoneInfo(str(tz_name))
    except Exception:
        logger.warning("Invalid timezone '%s'; falling back to UTC", tz_name)
        tz = ZoneInfo(DEFAULT_TIMEZONE)

    return TvMazeSettings(timezone=tz)


async def _fetch_show_info(query: str, settings: TvMazeSettings, timeout: int) -> Optional[str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _blocking_fetch_show_info(query, settings.timezone, timeout)
    )


def _blocking_fetch_show_info(query: str, tz: ZoneInfo, timeout: int) -> Optional[str]:
    params = {"q": query, "embed": "nextepisode"}
    response = requests.get(API_URL, params=params, headers=API_HEADERS, timeout=timeout)
    if response.status_code == 404:
        return None
    response.raise_for_status()

    data = response.json()
    return _format_show_response(data, tz)


def _format_show_response(data: Dict[str, Any], tz: ZoneInfo) -> Optional[str]:
    name = data.get("name")
    if not name:
        return None

    embedded = data.get("_embedded") or {}
    next_ep = embedded.get("nextepisode")
    if not next_ep:
        return f"{name} - no next episode :("

    season = next_ep.get("season")
    number = next_ep.get("number")
    air_stamp = next_ep.get("airstamp")
    if air_stamp is None:
        return f"{name} - next episode data unavailable"

    air_dt = _parse_iso_datetime(air_stamp)
    if air_dt is None:
        return f"{name} - invalid airtime data"

    local_dt = air_dt.astimezone(tz)
    time_str = local_dt.strftime("%Y-%m-%d %H:%M %Z")
    parts = [f"{name} - season {season}, episode {number} airs at {time_str}"]

    countdown = _format_countdown(air_dt)
    if countdown:
        parts.append(f"(in {countdown})")

    return " ".join(parts)


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("Unable to parse datetime %s", value)
        return None


def _format_countdown(air_dt: datetime) -> Optional[str]:
    if air_dt.tzinfo is None:
        air_dt = air_dt.replace(tzinfo=timezone.utc)

    now = datetime.now(tz=air_dt.tzinfo)
    if air_dt <= now:
        return None

    delta: timedelta = air_dt - now
    days = delta.days
    seconds = delta.seconds

    if days > 0:
        hours = round(seconds / 3600)
        return f"{days}d {hours}h"
    if seconds >= 3600:
        hours = round(seconds / 3600)
        minutes = round((seconds % 3600) / 60)
        return f"{hours}h {minutes}m"
    minutes = max(1, round(seconds / 60))
    return f"{minutes}m"
