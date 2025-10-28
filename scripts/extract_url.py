"""Preview page metadata for posted links (config: `plugins.extract_url`, optional)."""

import asyncio
import logging
import re
import textwrap
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, Iterable, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


URL_PATTERN = re.compile(r"https?://[^\s<>\u0000-\u001f]+", re.IGNORECASE)

DEFAULT_USER_AGENT = "ebba-irc-bot metadata extractor (+https://github.com/alex/ebba-irc-bot)"
MAX_URLS_PER_MESSAGE = 2


@dataclass
class ExtractTemplates:
    summary: str = "{title}{description_part} ({host})"


@dataclass
class ExtractSettings:
    user_agent: str = DEFAULT_USER_AGENT
    timeout: int = 10
    max_urls_per_message: int = MAX_URLS_PER_MESSAGE
    include_description: bool = True
    max_description_chars: int = 200
    templates: ExtractTemplates = field(default_factory=ExtractTemplates)


def on_load(bot) -> None:
    logger.info("extract_url plugin loaded from %s", __file__)


def on_unload(bot) -> None:
    logger.info("extract_url plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    matches = list(_iter_urls(message))
    if not matches:
        return

    settings = _settings_from_config(bot)
    loop = asyncio.get_running_loop()
    for url in matches[: settings.max_urls_per_message]:
        loop.create_task(_handle_extract(bot, channel, url, settings))


def _iter_urls(message: str) -> Iterable[str]:
    for match in URL_PATTERN.finditer(message):
        yield match.group(0).rstrip(").,!?")


async def _handle_extract(bot, channel: str, url: str, settings: ExtractSettings) -> None:
    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(
            None, lambda: _fetch_and_format(url, settings, timeout=bot.request_timeout)
        )
    except requests.RequestException:
        logger.warning("Metadata fetch request error for %s", url, exc_info=True)
        return
    except Exception:
        logger.exception("Metadata lookup failed for %s", url)
        return

    if reply:
        await bot.privmsg(channel, reply)


def _settings_from_config(bot) -> ExtractSettings:
    config = getattr(bot, "config", {})
    section = config.get("extract_url") if isinstance(config, dict) else {}
    if not isinstance(section, dict):
        section = {}

    defaults = ExtractSettings()
    templates = ExtractTemplates(
        summary=str(section.get("summary_template", defaults.templates.summary)),
    )

    def _get_int(name: str, fallback: int) -> int:
        value = section.get(name, fallback)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return fallback

    return ExtractSettings(
        user_agent=str(section.get("user_agent", defaults.user_agent)),
        timeout=_get_int("timeout", defaults.timeout),
        max_urls_per_message=_get_int("max_urls_per_message", defaults.max_urls_per_message),
        include_description=bool(section.get("include_description", defaults.include_description)),
        max_description_chars=_get_int("max_description_chars", defaults.max_description_chars),
        templates=templates,
    )


def _fetch_and_format(url: str, settings: ExtractSettings, timeout: int) -> Optional[str]:
    headers = {"User-Agent": settings.user_agent, "Accept": "text/html,application/xhtml+xml"}
    request_timeout = max(1, min(settings.timeout, timeout or settings.timeout))
    response = requests.get(url, headers=headers, timeout=request_timeout)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" not in content_type:
        logger.debug("Skipping non-HTML content for %s (%s)", url, content_type)
        return None

    parser = _MetadataParser()
    parser.feed(response.text)
    data = parser.metadata()
    if not data:
        return None

    host = urlparse(url).netloc or url
    title = _select_first(
        data,
        [
            "og:title",
            "twitter:title",
            "title",
        ],
    )
    if not title:
        return None
    title = _clean_text(title)

    description = _select_first(
        data,
        [
            "og:description",
            "twitter:description",
            "description",
        ],
    )
    description_part = ""
    if settings.include_description and description:
        summary = textwrap.shorten(
            _clean_text(description),
            width=settings.max_description_chars,
            placeholder="...",
        )
        if summary:
            description_part = f" — {summary}"

    return settings.templates.summary.format(
        title=title,
        description_part=description_part,
        host=host,
    )


def _select_first(data: Dict[str, str], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return None


def _clean_text(value: str) -> str:
    return " ".join(value.split())


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._meta: Dict[str, str] = {}
        self._in_title = False
        self._title_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attr_map = dict(attrs)
        if tag == "title":
            self._in_title = True
            self._title_chunks.clear()
            return

        if tag != "meta":
            return

        content = attr_map.get("content")
        if not content:
            return

        prop = attr_map.get("property", "").lower()
        name = attr_map.get("name", "").lower()

        if prop.startswith("og:"):
            self._meta[prop] = content
        elif name.startswith("twitter:"):
            self._meta[f"twitter:{name[8:]}"] = content
        elif name == "description" and "description" not in self._meta:
            self._meta["description"] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            if self._title_chunks and "title" not in self._meta:
                self._meta["title"] = "".join(self._title_chunks).strip()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_chunks.append(data)

    def metadata(self) -> Dict[str, str]:
        return dict(self._meta)
