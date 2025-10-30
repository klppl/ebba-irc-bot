"""Swedish electricity spot price plugin (no external config required)."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from statistics import mean
from typing import Dict, Iterable, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


API_URL_TEMPLATE = "https://www.vattenfall.se/api/price/spot/pricearea/{date}/{date}/{area}"
USER_AGENT = "Mozilla/5.0 (Windows NT 6.1; Win64; x64; rv:60.0) Gecko/20100101 Firefox/60.0"
DEFAULT_TRIGGERS = ["el"]
AREA_MAPPING = {"1": "SN1", "2": "SN2", "3": "SN3", "4": "SN4"}


CONFIG_DEFAULTS = {
    "plugins": {
        "svensk_el": {
            "enabled": True,
            "triggers": list(DEFAULT_TRIGGERS),
        }
    }
}


@dataclass(frozen=True)
class PriceRecord:
    value: float
    timestamp: str


@dataclass(frozen=True)
class SvenskElSettings:
    triggers: List[str] = field(default_factory=lambda: list(DEFAULT_TRIGGERS))


def on_load(bot) -> None:
    settings = _settings_from_config(bot)
    trigger_text = ", ".join(f"{bot.prefix}{trigger}" for trigger in settings.triggers)
    logger.info("svensk_el plugin loaded from %s; responding to %s", __file__, trigger_text)


def on_unload(bot) -> None:
    logger.info("svensk_el plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    settings = _settings_from_config(bot)

    prefix = bot.prefix
    if not message.startswith(prefix):
        return

    command_line = message[len(prefix) :].strip()
    if not command_line:
        return

    parts = command_line.split(maxsplit=1)
    command = parts[0].lower()
    if command not in settings.triggers:
        return

    arg = parts[1].strip() if len(parts) > 1 else ""

    loop = asyncio.get_running_loop()
    loop.create_task(_handle_command(bot, channel, arg, settings, prefix))


def _settings_from_config(bot) -> SvenskElSettings:
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else {}
    section = {}
    if isinstance(plugins_section, dict):
        candidate = plugins_section.get("svensk_el")
        if isinstance(candidate, dict):
            section = candidate

    triggers = _parse_triggers(section.get("triggers"), DEFAULT_TRIGGERS)
    return SvenskElSettings(triggers=triggers)


async def _handle_command(
    bot, channel: str, argument: str, settings: SvenskElSettings, prefix: str
) -> None:
    argument = (argument or "").strip().lower()
    primary = settings.triggers[0] if settings.triggers else DEFAULT_TRIGGERS[0]
    if not argument:
        await bot.privmsg(
            channel, f"Användning: {prefix}{primary} [snitt|dag|1|2|3|4]"
        )
        return

    today = date.today().strftime("%Y-%m-%d")

    try:
        if argument == "snitt":
            response = await _fetch_averages(today)
        elif argument == "dag":
            response = await _fetch_day_series("SN3", today)
        elif argument in AREA_MAPPING:
            area_code = AREA_MAPPING[argument]
            area_label = f"SE{argument}"
            response = await _fetch_area_details(area_label, area_code, today)
        else:
            response = f"Användning: {prefix}{primary} [snitt|dag|1|2|3|4]"
    except Exception as exc:
        logger.exception("svensk_el command failed")
        response = f"Error: {exc}"

    await bot.privmsg(channel, response)


async def _fetch_averages(date_str: str) -> str:
    loop = asyncio.get_running_loop()

    def _compute() -> str:
        areas = ["SN1", "SN2", "SN3", "SN4"]
        averages = []
        for area in areas:
            data = _fetch_data(area, date_str)
            values = [record.value for record in data]
            if not values:
                raise ValueError(f"No data for {area} on {date_str}")
            averages.append(mean(values))
        return (
            f"Snittpris: SE1 {round(averages[0])} öre/kWh | "
            f"SE2 {round(averages[1])} öre/kWh | "
            f"SE3 {round(averages[2])} öre/kWh | "
            f"SE4 {round(averages[3])} öre/kWh"
        )

    return await loop.run_in_executor(None, _compute)


async def _fetch_day_series(area_code: str, date_str: str) -> str:
    loop = asyncio.get_running_loop()

    def _compute() -> str:
        data = _fetch_data(area_code, date_str)
        if not data:
            return f"No data available for SE3 on {date_str}"
        relevant = [
            f"[{record.timestamp} - {record.value}]"
            for record in data
            if "06:00" <= record.timestamp <= "23:00"
        ]
        return "Elpriser idag SE3 (öre/kWh): " + " ".join(relevant)

    return await loop.run_in_executor(None, _compute)


async def _fetch_area_details(area_label: str, area_code: str, date_str: str) -> str:
    loop = asyncio.get_running_loop()

    def _compute() -> str:
        data = _fetch_data(area_code, date_str)
        if not data:
            return f"No data available for {area_label} on {date_str}"
        values = [(record.value, record.timestamp) for record in data]
        average = mean(value for value, _ in values)
        max_value, max_time = max(values, key=lambda x: x[0])
        min_value, min_time = min(values, key=lambda x: x[0])
        return (
            f"Snittpris {area_label}: {round(average)} öre/kWh | "
            f"Max: {max_value} öre/kWh - kl {max_time} | "
            f"Lägst: {min_value} öre/kWh - kl {min_time}"
        )

    return await loop.run_in_executor(None, _compute)


@lru_cache(maxsize=128)
def _fetch_data(area: str, date_str: str) -> List[PriceRecord]:
    headers = {"User-Agent": USER_AGENT}
    url = API_URL_TEMPLATE.format(date=date_str, area=area)
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise RuntimeError(f"Failed to fetch data for {area}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON response for {area}: {exc}") from exc

    records = []
    for item in payload:
        try:
            value = float(item["Value"])
            timestamp = str(item["TimeStampHour"])
            records.append(PriceRecord(value=value, timestamp=timestamp))
        except (KeyError, TypeError, ValueError):
            logger.debug("Skipping malformed record: %s", item)
            continue
    return records


def _parse_triggers(raw: object, fallback: Iterable[str]) -> List[str]:
    if isinstance(raw, str):
        cleaned = raw.strip().lower()
        return [cleaned] if cleaned else list(fallback)
    if isinstance(raw, Iterable):
        values: List[str] = []
        for item in raw:
            try:
                text = str(item).strip().lower()
            except Exception:
                continue
            if text and text not in values:
                values.append(text)
        return values or list(fallback)
    return list(fallback)
