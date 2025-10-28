"""Twitter/X oEmbed formatter (config: `plugins.twitter`, optional)."""

import asyncio
import json
import logging
import re
import textwrap
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional

import requests

logger = logging.getLogger(__name__)


URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:mobile\.)?(?:twitter|x)\.com/[^\s]+/status/\d+",
    re.IGNORECASE,
)

OEMBED_ENDPOINT = "https://publish.twitter.com/oembed"
DEFAULT_TEMPLATE = "{name} (@{nick}): {content} - {date}"
DEFAULT_USER_AGENT = "ebba-irc-bot twitter plugin (+https://github.com/alex/ebba-irc-bot)"
MAX_URLS_PER_MESSAGE = 2


CONFIG_DEFAULTS = {
    "plugins": {
        "twitter": {
            "enabled": True,
            "template": DEFAULT_TEMPLATE,
            "user_agent": DEFAULT_USER_AGENT,
            "timeout": 10,
            "max_urls_per_message": MAX_URLS_PER_MESSAGE,
            "max_content_chars": 240,
        }
    }
}


@dataclass
class TwitterSettings:
    enabled: bool = True
    template: str = DEFAULT_TEMPLATE
    user_agent: str = DEFAULT_USER_AGENT
    timeout: int = 10
    max_urls_per_message: int = MAX_URLS_PER_MESSAGE
    max_content_chars: int = 240


def on_load(bot) -> None:
    logger.info("twitter plugin loaded from %s", __file__)


def on_unload(bot) -> None:
    logger.info("twitter plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    settings = _settings_from_config(bot)
    if not settings.enabled:
        return

    matches = list(_iter_urls(message))
    if not matches:
        return

    loop = asyncio.get_running_loop()
    for url in matches[: settings.max_urls_per_message]:
        loop.create_task(_handle_twitter_url(bot, channel, url, settings))


def _iter_urls(message: str) -> Iterable[str]:
    for match in URL_PATTERN.finditer(message):
        yield match.group(0).rstrip(").,!?")


async def _handle_twitter_url(
    bot, channel: str, url: str, settings: TwitterSettings
) -> None:
    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(
            None, lambda: _fetch_and_format(url, settings, timeout=bot.request_timeout)
        )
    except requests.RequestException:
        logger.warning("Twitter request error for %s", url, exc_info=True)
        return
    except Exception:
        logger.exception("Twitter lookup failed for %s", url)
        return

    if reply:
        await bot.privmsg(channel, reply)


def _settings_from_config(bot) -> TwitterSettings:
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else {}
    section: Dict[str, object] = {}
    if isinstance(plugins_section, dict):
        candidate = plugins_section.get("twitter")
        if isinstance(candidate, dict):
            section = candidate

    defaults = TwitterSettings()
    enabled = bool(section.get("enabled", defaults.enabled))
    template = str(section.get("template", defaults.template))
    user_agent = str(section.get("user_agent", defaults.user_agent))

    timeout = section.get("timeout", defaults.timeout)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = defaults.timeout

    max_urls = section.get("max_urls_per_message", defaults.max_urls_per_message)
    try:
        max_urls = max(1, int(max_urls))
    except (TypeError, ValueError):
        max_urls = defaults.max_urls_per_message

    max_content_chars = section.get("max_content_chars", defaults.max_content_chars)
    try:
        max_content_chars = max(10, int(max_content_chars))
    except (TypeError, ValueError):
        max_content_chars = defaults.max_content_chars

    return TwitterSettings(
        enabled=enabled,
        template=template,
        user_agent=user_agent,
        timeout=timeout,
        max_urls_per_message=max_urls,
        max_content_chars=max_content_chars,
    )


def _fetch_and_format(url: str, settings: TwitterSettings, timeout: int) -> Optional[str]:
    headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    request_timeout = max(1, min(settings.timeout, timeout or settings.timeout))
    resp = requests.get(
        OEMBED_ENDPOINT,
        params={"url": url, "omit_script": "true"},
        headers=headers,
        timeout=request_timeout,
    )
    resp.raise_for_status()

    data = resp.json()
    html = data.get("html")
    if not html:
        return None

    content, name, nick, date = _parse_tweet_html(html)
    if not content:
        return None

    content = textwrap.shorten(content, width=settings.max_content_chars, placeholder="...")

    try:
        return settings.template.format(content=content, name=name, nick=nick, date=date)
    except Exception:
        logger.exception("Failed to render twitter template")
        return f"{name} (@{nick}): {content} - {date}".strip()


def _parse_tweet_html(html: str):
    # Expected: <blockquote> ... <p>content</p> &mdash; Name (@handle) <a ...>date</a></blockquote>
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        logger.warning("BeautifulSoup required for twitter plugin; install bs4")
        return None, "", "", ""

    soup = BeautifulSoup(html, "html.parser")
    paragraph = soup.find("p")
    if paragraph:
        content = paragraph.get_text(" ").strip()
    else:
        content = soup.get_text(" ").strip()

    text = soup.get_text(" ").strip()
    match = re.search(r"—\s*(.*?)\s*\((@[^)]+)\)\s*(.+)$", text)
    if match:
        name = match.group(1).strip()
        nick = match.group(2).strip().lstrip("@")
        date = match.group(3).strip()
    else:
        name = ""
        nick = ""
        date = ""

    return content, name, nick, date
