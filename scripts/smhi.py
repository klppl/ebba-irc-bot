"""SMHI weather responder (commands: `.weather`, `.vädret`, `.smhi`)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "ebba-irc-bot smhi plugin (+https://github.com/alex/ebba-irc-bot)"
DEFAULT_LANGUAGE = "sv-SE"
DEFAULT_TIMEOUT = 15
SUPPORTED_LANGUAGES = {"sv-SE", "en-US"}

DEFAULT_TRIGGERS = ["weather", "vädret", "smhi"]

WEATHER_SYMBOLS: Dict[int, Dict[str, str]] = {
    1: {"en-US": "Clear sky", "sv-SE": "Klar himmel"},
    2: {"en-US": "Nearly clear sky", "sv-SE": "Nästan klar himmel"},
    3: {"en-US": "Variable cloudiness", "sv-SE": "Växlande molnighet"},
    4: {"en-US": "Halfclear sky", "sv-SE": "Halvklar himmel"},
    5: {"en-US": "Cloudy sky", "sv-SE": "Molnig himmel"},
    6: {"en-US": "Overcast", "sv-SE": "Mulet"},
    7: {"en-US": "Fog", "sv-SE": "Dimma"},
    8: {"en-US": "Light rain showers", "sv-SE": "Lätta regnskurar"},
    9: {"en-US": "Moderate rain showers", "sv-SE": "Måttliga regnskurar"},
    10: {"en-US": "Heavy rain showers", "sv-SE": "Kraftiga regnskurar"},
    11: {"en-US": "Thunderstorm", "sv-SE": "Åskoväder"},
    12: {"en-US": "Light sleet showers", "sv-SE": "Lätta snöblandade regnskurar"},
    13: {"en-US": "Moderate sleet showers", "sv-SE": "Måttliga snöblandade regnskurar"},
    14: {"en-US": "Heavy sleet showers", "sv-SE": "Kraftiga snöblandade regnskurar"},
    15: {"en-US": "Light snow showers", "sv-SE": "Lätta snöbyar"},
    16: {"en-US": "Moderate snow showers", "sv-SE": "Måttliga snöbyar"},
    17: {"en-US": "Heavy snow showers", "sv-SE": "Kraftiga snöbyar"},
    18: {"en-US": "Light rain", "sv-SE": "Duggregn"},
    19: {"en-US": "Moderate rain", "sv-SE": "Måttligt regn"},
    20: {"en-US": "Heavy rain", "sv-SE": "Kraftigt regn"},
    21: {"en-US": "Thunder", "sv-SE": "Åska"},
    22: {"en-US": "Light sleet", "sv-SE": "Lätt snöblandat regn"},
    23: {"en-US": "Moderate sleet", "sv-SE": "Måttligt snöblandat regn"},
    24: {"en-US": "Heavy sleet", "sv-SE": "Kraftigt snöblandat regn"},
    25: {"en-US": "Light snowfall", "sv-SE": "Lätt snöfall"},
    26: {"en-US": "Moderate snowfall", "sv-SE": "Måttligt snöfall"},
    27: {"en-US": "Heavy snowfall", "sv-SE": "Kraftigt snöfall"},
}

PRECIPITATION_CATEGORIES: Dict[int, Dict[str, str]] = {
    0: {"en-US": "No precipitation", "sv-SE": "Ingen nederbörd"},
    1: {"en-US": "Snow", "sv-SE": "Snö"},
    2: {"en-US": "Snow and rain", "sv-SE": "Snö och regn"},
    3: {"en-US": "Rain", "sv-SE": "Regn"},
    4: {"en-US": "Drizzle", "sv-SE": "Duggregn"},
    5: {"en-US": "Freezing rain", "sv-SE": "Frysande regn"},
    6: {"en-US": "Freezing drizzle", "sv-SE": "Underkylt regn"},
}

WIND_SPEED_DESCRIPTIONS: Dict[int, Dict[str, str]] = {
    0: {"en-US": "Calm", "sv-SE": "Stiltje"},
    1: {"en-US": "Light air", "sv-SE": "Nästan stiltje"},
    2: {"en-US": "Light breeze", "sv-SE": "Lätt bris"},
    3: {"en-US": "Gentle breeze", "sv-SE": "God bris"},
    4: {"en-US": "Moderate breeze", "sv-SE": "Frisk bris"},
    5: {"en-US": "Fresh breeze", "sv-SE": "Styv bris"},
    6: {"en-US": "Strong breeze", "sv-SE": "Hård bris"},
    7: {"en-US": "Near gale", "sv-SE": "Styv kuling"},
    8: {"en-US": "Gale", "sv-SE": "Hård kuling"},
    9: {"en-US": "Strong gale", "sv-SE": "Halv storm"},
    10: {"en-US": "Storm", "sv-SE": "Storm"},
    11: {"en-US": "Violent storm", "sv-SE": "Svår storm"},
    12: {"en-US": "Hurricane force", "sv-SE": "Orkan"},
}


CONFIG_DEFAULTS = {
    "plugins": {
        "smhi": {
            "enabled": True,
            "language": DEFAULT_LANGUAGE,
            "user_agent": DEFAULT_USER_AGENT,
            "timeout": DEFAULT_TIMEOUT,
            "triggers": list(DEFAULT_TRIGGERS),
        }
    }
}


@dataclass
class SMHISettings:
    language: str = DEFAULT_LANGUAGE
    user_agent: str = DEFAULT_USER_AGENT
    timeout: int = DEFAULT_TIMEOUT
    triggers: List[str] = field(default_factory=lambda: list(DEFAULT_TRIGGERS))


@dataclass
class SMHIForecast:
    timestamp: str
    temperature: float
    pressure: float
    visibility: float
    humidity: int
    wind_direction: int
    wind_speed: float
    wind_gust: float
    wind_description: str
    weather_symbol: int
    weather_description: str
    precipitation_category: int
    precipitation_description: str
    precipitation_intensity: float
    cloud_cover: int
    thunder_probability: int


def on_load(bot) -> None:
    settings = _settings_from_config(bot)
    trigger_text = ", ".join(f"{bot.prefix}{trigger}" for trigger in settings.triggers)
    logger.info("smhi plugin loaded from %s; responding to %s", __file__, trigger_text)


def on_unload(bot) -> None:
    logger.info("smhi plugin unloaded")


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

    if len(parts) == 1 or not parts[1].strip():
        response = {
            "sv-SE": "Användning: {prefix}{cmd} <stad>",
            "en-US": "Usage: {prefix}{cmd} <city>",
        }
        reply = response.get(settings.language, response["en-US"]).format(prefix=prefix, cmd=command)
        asyncio.get_running_loop().create_task(bot.privmsg(channel, reply))
        return

    city = parts[1].strip()
    loop = asyncio.get_running_loop()
    loop.create_task(_handle_weather_command(bot, channel, city, settings))


async def _handle_weather_command(
    bot, channel: str, city: str, settings: Optional[SMHISettings] = None
) -> None:
    if settings is None:
        settings = _settings_from_config(bot)
    timeout = settings.timeout
    request_timeout = getattr(bot, "request_timeout", 0)
    if isinstance(request_timeout, (int, float)) and request_timeout > 0:
        timeout = max(1, min(timeout, int(request_timeout)))

    loop = asyncio.get_running_loop()
    try:
        forecast, error = await loop.run_in_executor(
            None, lambda: _fetch_forecast_for_city(city, settings, timeout)
        )
    except Exception:  # pragma: no cover - network failures
        logger.exception("smhi lookup failed for %s", city)
        await bot.privmsg(channel, f"Kunde inte hämta väder för {city}.")
        return

    if error:
        await bot.privmsg(channel, error)
        return

    if forecast is None:
        await bot.privmsg(channel, f"Kunde inte hämta väder för {city}.")
        return

    response = _format_forecast(city, forecast, settings.language)
    await bot.privmsg(channel, response)


def _settings_from_config(bot) -> SMHISettings:
    config = getattr(bot, "config", {})
    plugin_section: Dict[str, Any] = {}
    if isinstance(config, dict):
        plugins = config.get("plugins")
        if isinstance(plugins, dict):
            candidate = plugins.get("smhi")
            if isinstance(candidate, dict):
                plugin_section = candidate

    language_raw = plugin_section.get("language", DEFAULT_LANGUAGE)
    language = str(language_raw).strip()
    if language not in SUPPORTED_LANGUAGES:
        language = DEFAULT_LANGUAGE

    user_agent = str(plugin_section.get("user_agent", DEFAULT_USER_AGENT)).strip() or DEFAULT_USER_AGENT

    timeout_raw = plugin_section.get("timeout", DEFAULT_TIMEOUT)
    try:
        timeout_value = int(timeout_raw)
    except (TypeError, ValueError):
        timeout_value = DEFAULT_TIMEOUT
    timeout_value = max(1, timeout_value)

    triggers = _parse_triggers(plugin_section.get("triggers"), DEFAULT_TRIGGERS)

    return SMHISettings(
        language=language,
        user_agent=user_agent,
        timeout=timeout_value,
        triggers=triggers,
    )


def _fetch_forecast_for_city(
    city: str, settings: SMHISettings, timeout: int
) -> Tuple[Optional[SMHIForecast], Optional[str]]:
    coords = _get_coordinates(city, settings, timeout)
    if not coords:
        message = {
            "sv-SE": f"Hittade inga koordinater för {city}.",
            "en-US": f"Could not find coordinates for {city}.",
        }
        return None, message.get(settings.language, message["en-US"])

    lon, lat = coords
    data = _get_point_forecast(lon, lat, settings, timeout)
    if not data:
        message = {
            "sv-SE": f"SMHI saknar prognosdata för {city}.",
            "en-US": f"SMHI does not have forecast data for {city}.",
        }
        return None, message.get(settings.language, message["en-US"])

    forecast = _parse_forecast(data, settings.language)
    if forecast is None:
        return None, {
            "sv-SE": "Kunde inte tolka SMHI-svaret.",
            "en-US": "Failed to parse SMHI response.",
        }.get(settings.language, "Failed to parse SMHI response.")

    return forecast, None


def _get_coordinates(
    city: str, settings: SMHISettings, timeout: int
) -> Optional[Tuple[float, float]]:
    for fetcher in (_get_coordinates_nominatim, _get_coordinates_geocodemaps):
        try:
            coords = fetcher(city, settings, timeout)
            if coords:
                return coords
        except Exception:
            logger.exception("Coordinate lookup failed via %s", fetcher.__name__)
    return None


def _get_coordinates_nominatim(
    city: str, settings: SMHISettings, timeout: int
) -> Optional[Tuple[float, float]]:
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": settings.user_agent, "Accept-Language": settings.language}
    params = {"q": city, "format": "json", "limit": 1}
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data:
        return None
    entry = data[0]
    return float(entry["lon"]), float(entry["lat"])


def _get_coordinates_geocodemaps(
    city: str, settings: SMHISettings, timeout: int
) -> Optional[Tuple[float, float]]:
    url = "https://geocode.maps.co/search"
    params = {"q": city, "limit": 1}
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data:
        return None
    entry = data[0]
    return float(entry["lon"]), float(entry["lat"])


def _get_point_forecast(
    longitude: float, latitude: float, settings: SMHISettings, timeout: int
) -> Optional[Dict[str, Any]]:
    base = "https://opendata-download-metfcst.smhi.se/api"
    mesan = "https://opendata-download-metanalys.smhi.se/api/category/mesan2g/version/1"
    endpoints = [
        f"{base}/category/pmp3g/version/2/geotype/point/lon/{longitude}/lat/{latitude}/data.json",
        f"{mesan}/geotype/point/lon/{longitude}/lat/{latitude}/data.json",
        f"{mesan}/geotype/point/lon/{longitude:.2f}/lat/{latitude:.2f}/data.json",
    ]

    headers = {"User-Agent": settings.user_agent}

    for url in endpoints:
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            data = response.json()
            if data:
                return data
        except requests.RequestException:
            logger.debug("SMHI endpoint failed: %s", url, exc_info=True)

    return None


def _parse_forecast(data: Dict[str, Any], language: str) -> Optional[SMHIForecast]:
    timeseries = data.get("timeSeries")
    if not timeseries:
        return None

    entry = timeseries[0]
    params = {}
    for parameter in entry.get("parameters", []):
        name = parameter.get("name")
        values = parameter.get("values")
        if name and values:
            params[name] = values[0]

    timestamp = entry.get("validTime", "")
    weather_symbol = int(params.get("Wsymb2", params.get("Wsymb", 1)))
    temperature = float(params.get("t", params.get("Temperature", 0.0)))

    pressure_raw = params.get("mslp", params.get("msl", params.get("Pressure")))
    if pressure_raw is None:
        pressure = 0.0
    else:
        pressure_value = float(pressure_raw)
        pressure = pressure_value if pressure_value < 100 else pressure_value / 100

    pcat_raw = params.get("pcat")
    precipitation_category = int(pcat_raw) if pcat_raw is not None else -1

    precipitation_intensity = float(params.get("pmean", params.get("Precipitation", 0.0)))
    if precipitation_intensity > 1000:
        precipitation_intensity /= 1000
    if precipitation_category == -1:
        if weather_symbol <= 7:
            precipitation_category = 0
            precipitation_intensity = 0.0
    if precipitation_category == 0:
        precipitation_intensity = 0.0

    wind_speed = float(params.get("ws", params.get("WindSpeed", 0.0)))
    wind_gust = float(params.get("gust", params.get("WindGustSpeed", 0.0)))
    wind_direction = int(params.get("wd", params.get("WindDirection", 0)))

    return SMHIForecast(
        timestamp=timestamp,
        temperature=temperature,
        pressure=pressure,
        visibility=float(params.get("vis", params.get("Visibility", 0.0))),
        humidity=int(params.get("r", params.get("Humidity", 0))),
        wind_direction=wind_direction,
        wind_speed=wind_speed,
        wind_gust=wind_gust,
        wind_description=_wind_speed_description(wind_speed, language),
        weather_symbol=weather_symbol,
        weather_description=_lookup_with_fallback(WEATHER_SYMBOLS, weather_symbol, language, "Unknown"),
        precipitation_category=precipitation_category,
        precipitation_description=_lookup_with_fallback(
            PRECIPITATION_CATEGORIES, precipitation_category, language, "Unknown"
        ),
        precipitation_intensity=precipitation_intensity,
        cloud_cover=int(params.get("tcc_mean", params.get("TotalCloudCover", 0))),
        thunder_probability=int(params.get("tstm", 0)),
    )


def _wind_speed_description(speed: float, language: str) -> str:
    beaufort_mapping = [
        (0.2, 0),
        (1.5, 1),
        (3.3, 2),
        (5.4, 3),
        (7.9, 4),
        (10.7, 5),
        (13.8, 6),
        (17.1, 7),
        (20.7, 8),
        (24.4, 9),
        (28.4, 10),
        (32.6, 11),
    ]
    for threshold, beaufort in beaufort_mapping:
        if speed <= threshold:
            return _lookup_with_fallback(WIND_SPEED_DESCRIPTIONS, beaufort, language, "Unknown")
    return _lookup_with_fallback(WIND_SPEED_DESCRIPTIONS, 12, language, "Unknown")


def _lookup_with_fallback(
    mapping: Dict[int, Dict[str, str]], key: int, language: str, default: str
) -> str:
    return mapping.get(int(key), {}).get(language) or mapping.get(int(key), {}).get("en-US") or default


def _format_forecast(city: str, forecast: SMHIForecast, language: str) -> str:
    timestamp = _format_timestamp(forecast.timestamp, language)

    if language == "sv-SE":
        parts = [
            f"Vädret i {city}: {forecast.weather_description}",
            f"{forecast.temperature:.1f}°C",
            f"vind {forecast.wind_speed:.1f} m/s ({forecast.wind_description})",
        ]
        if forecast.wind_gust > forecast.wind_speed:
            parts.append(f"byar {forecast.wind_gust:.1f} m/s")
        if forecast.precipitation_intensity > 0:
            precip_part = f"nederbörd {forecast.precipitation_intensity:.1f} mm/h"
            if forecast.precipitation_description and forecast.precipitation_description != "Unknown":
                precip_part += f" ({forecast.precipitation_description})"
            parts.append(precip_part)
        parts.append(f"luftfuktighet {forecast.humidity}%")
        if forecast.pressure:
            parts.append(f"tryck {forecast.pressure:.1f} hPa")
        if forecast.thunder_probability:
            parts.append(f"åskrisk {forecast.thunder_probability}%")
        return f"{' | '.join(parts)}. Uppdaterad {timestamp}."

    parts = [
        f"Weather in {city}: {forecast.weather_description}",
        f"{forecast.temperature:.1f}°C",
        f"wind {forecast.wind_speed:.1f} m/s ({forecast.wind_description})",
    ]
    if forecast.wind_gust > forecast.wind_speed:
        parts.append(f"gusts {forecast.wind_gust:.1f} m/s")
    if forecast.precipitation_intensity > 0:
        precip_part = f"precip {forecast.precipitation_intensity:.1f} mm/h"
        if forecast.precipitation_description and forecast.precipitation_description != "Unknown":
            precip_part += f" ({forecast.precipitation_description})"
        parts.append(precip_part)
    parts.append(f"humidity {forecast.humidity}%")
    if forecast.pressure:
        parts.append(f"pressure {forecast.pressure:.1f} hPa")
    if forecast.thunder_probability:
        parts.append(f"thunder risk {forecast.thunder_probability}%")
    return f"{' | '.join(parts)}. Updated {timestamp}."


def _format_timestamp(timestamp: str, language: str) -> str:
    if not timestamp:
        return ""
    try:
        when = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp

    if language == "sv-SE":
        return when.strftime("%Y-%m-%d %H:%M")
    return when.strftime("%Y-%m-%d %H:%M UTC")


def _parse_triggers(raw: Any, fallback: Iterable[str]) -> List[str]:
    if isinstance(raw, str):
        text = raw.strip().lower()
        return [text] if text else list(fallback)
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
