"""Instagram post/reel formatter (optional config: `plugins.instagram`)."""

import asyncio
import html
import json
import logging
import re
import textwrap
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


URL_PATTERN = re.compile(
    r"https?://(?:www\.|m\.)?instagram\.com/(?:p|reel|reels|tv)/[^\s]+",
    re.IGNORECASE,
)

SUPPORTED_PATHS = {"p", "reel", "reels", "tv"}


CONFIG_DEFAULTS = {
    "plugins": {
        "instagram": {
            "enabled": True,
            "user_agent": "ebba-irc-bot instagram plugin (+https://github.com/alex/ebba-irc-bot)",
            "timeout": 10,
            "max_urls_per_message": 2,
            "include_caption": True,
            "caption_max_chars": 160,
            "summary_template": "{username}{verified} | likes {likes}{caption_part}",
        }
    }
}


@dataclass
class InstagramTemplates:
    summary: str = "{username}{verified} | likes {likes}{caption_part}"


@dataclass
class InstagramSettings:
    user_agent: str = CONFIG_DEFAULTS["plugins"]["instagram"]["user_agent"]
    timeout: int = CONFIG_DEFAULTS["plugins"]["instagram"]["timeout"]
    max_urls_per_message: int = CONFIG_DEFAULTS["plugins"]["instagram"]["max_urls_per_message"]
    include_caption: bool = CONFIG_DEFAULTS["plugins"]["instagram"]["include_caption"]
    caption_max_chars: int = CONFIG_DEFAULTS["plugins"]["instagram"]["caption_max_chars"]
    templates: InstagramTemplates = field(default_factory=InstagramTemplates)


@dataclass
class InstagramResult:
    username: str
    is_verified: bool
    likes: Optional[int]
    caption: Optional[str]


def on_load(bot) -> None:
    logger.info("instagram plugin loaded from %s", __file__)


def on_unload(bot) -> None:
    logger.info("instagram plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    matches = list(_iter_urls(message))
    if not matches:
        return

    settings = _settings_from_config(bot)
    loop = asyncio.get_running_loop()
    for url in matches[: settings.max_urls_per_message]:
        loop.create_task(_handle_instagram_url(bot, channel, url, settings))


def _iter_urls(message: str) -> Iterable[str]:
    for match in URL_PATTERN.finditer(message):
        yield match.group(0).rstrip(").,!?")


async def _handle_instagram_url(
    bot, channel: str, url: str, settings: InstagramSettings
) -> None:
    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(
            None, lambda: _fetch_and_format(url, settings, timeout=bot.request_timeout)
        )
    except requests.RequestException:
        logger.warning("Instagram request error for %s", url, exc_info=True)
        return
    except Exception:
        logger.exception("Instagram lookup failed for %s", url)
        return

    if reply:
        await bot.privmsg(channel, reply)


def _settings_from_config(bot) -> InstagramSettings:
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else {}
    section: Dict[str, object] = {}
    if isinstance(plugins_section, dict):
        candidate = plugins_section.get("instagram")
        if isinstance(candidate, dict):
            section = candidate

    defaults = InstagramSettings()
    templates = InstagramTemplates(
        summary=str(section.get("summary_template", defaults.templates.summary)),
    )

    def _get_int(name: str, fallback: int) -> int:
        value = section.get(name, fallback)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return fallback

    return InstagramSettings(
        user_agent=str(section.get("user_agent", defaults.user_agent)),
        timeout=_get_int("timeout", defaults.timeout),
        max_urls_per_message=_get_int("max_urls_per_message", defaults.max_urls_per_message),
        include_caption=bool(section.get("include_caption", defaults.include_caption)),
        caption_max_chars=_get_int("caption_max_chars", defaults.caption_max_chars),
        templates=templates,
    )


def _fetch_and_format(
    url: str, settings: InstagramSettings, timeout: int
) -> Optional[str]:
    media_type, shortcode = _extract_path_and_shortcode(url)
    if not shortcode or not media_type:
        logger.debug("Unable to parse Instagram URL: %s", url)
        return None

    result = _fetch_instagram_data(media_type, shortcode, settings, timeout)
    if result is None:
        return None

    return _render_summary(result, settings)


def _extract_path_and_shortcode(url: str) -> Tuple[Optional[str], Optional[str]]:
    parsed = urlparse(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return None, None

    for idx, segment in enumerate(segments):
        if segment.lower() in SUPPORTED_PATHS and idx + 1 < len(segments):
            return segment.lower(), segments[idx + 1]

    if segments[0].lower() in SUPPORTED_PATHS:
        return segments[0].lower(), segments[1] if len(segments) > 1 else None

    return None, None


def _fetch_instagram_data(
    media_type: str, shortcode: str, settings: InstagramSettings, timeout: int
) -> Optional[InstagramResult]:
    embed_path = "reel" if media_type == "reels" else media_type
    embed_url = f"https://www.instagram.com/{embed_path}/{shortcode}/embed/captioned/"
    headers = {
        "User-Agent": settings.user_agent,
        "Referer": "https://www.instagram.com/",
    }

    request_timeout = max(1, min(settings.timeout, timeout or settings.timeout))
    response = requests.get(embed_url, headers=headers, timeout=request_timeout)
    response.raise_for_status()

    return _parse_embed_html(response.text)


def _parse_embed_html(source: str) -> Optional[InstagramResult]:
    likes = _parse_number(r">([\d.,]+)([kKmM]?)\s+likes<", source)
    username = _parse_username(source)
    caption = _parse_caption(source)
    verified = bool(re.search(r'"is_verified":true', source))

    if not username and likes is None and caption is None:
        return None

    return InstagramResult(
        username=username or "unknown",
        is_verified=verified,
        likes=likes,
        caption=caption,
    )


def _parse_number(pattern: str, source: str) -> Optional[int]:
    match = re.search(pattern, source, flags=re.IGNORECASE)
    if not match:
        return None
    number_text = match.group(1).replace(",", "")
    suffix = ""
    if match.lastindex and match.lastindex >= 2:
        group = match.group(2)
        if group:
            suffix = group.lower()
    try:
        value = float(number_text)
    except ValueError:
        return None

    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000

    try:
        return int(round(value))
    except (TypeError, ValueError):
        return None


def _parse_username(source: str) -> Optional[str]:
    match = re.search(
        r'<a class="CaptionUsername"[^>]*>([^<]+)</a>', source, flags=re.IGNORECASE
    )
    if match:
        return html.unescape(match.group(1).strip())

    match = re.search(r'\\"username\\":\\"([^\\"]+)\\"', source)
    if match:
        return _decode_json_string(match.group(1))
    return None


def _parse_caption(source: str) -> Optional[str]:
    match = re.search(r'<div class="Caption">(.*?)</div>', source, flags=re.DOTALL)
    if not match:
        return None

    raw = match.group(1)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"<.*?>", "", raw)
    cleaned = html.unescape(cleaned).strip()
    cleaned = re.split(r"\bView all \d+ comments\b", cleaned, maxsplit=1, flags=re.IGNORECASE)[
        0
    ].strip()
    return cleaned or None


def _decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def _render_summary(result: InstagramResult, settings: InstagramSettings) -> str:
    likes = _format_metric(result.likes)

    caption_part = ""
    if settings.include_caption and result.caption:
        summary = textwrap.shorten(
            result.caption, width=settings.caption_max_chars, placeholder="..."
        )
        caption_part = f" | caption: {summary}"

    verified = " ✓" if result.is_verified else ""
    return settings.templates.summary.format(
        username=result.username,
        verified=verified,
        likes=likes,
        caption_part=caption_part,
    )


def _format_metric(value: Optional[int]) -> str:
    if value is None:
        return "n/a"
    return f"{value:,}"
