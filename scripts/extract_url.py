"""Preview page metadata for posted links (config: `plugins.extract_url`, optional)."""

import asyncio
import ipaddress
import logging
import re
import socket
import textwrap
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


URL_PATTERN = re.compile(r"https?://[^\s<>\u0000-\u001f]+", re.IGNORECASE)
DEFAULT_SUMMARY_TEMPLATE = "{title}{description_part} ({host})"
CONNECT_TIMEOUT_CAP_SECS = 5
MAX_CONTENT_BYTES = 512_000
MAX_REDIRECTS = 3
RETRY_TOTAL = 3
RETRY_BACKOFF_FACTOR = 0.5
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
RETRY_ALLOWED_METHODS = frozenset({"GET", "HEAD"})


CONFIG_DEFAULTS = {
    "plugins": {
        "extract_url": {
            "enabled": True,
            "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "timeout": 10,
            "max_urls_per_message": 2,
            "include_description": True,
            "max_description_chars": 200,
            "summary_template": DEFAULT_SUMMARY_TEMPLATE,
        }
    }
}


@dataclass
class ExtractTemplates:
    summary: str = DEFAULT_SUMMARY_TEMPLATE


@dataclass
class ExtractSettings:
    user_agent: str = CONFIG_DEFAULTS["plugins"]["extract_url"]["user_agent"]
    timeout: int = CONFIG_DEFAULTS["plugins"]["extract_url"]["timeout"]
    max_urls_per_message: int = CONFIG_DEFAULTS["plugins"]["extract_url"]["max_urls_per_message"]
    include_description: bool = CONFIG_DEFAULTS["plugins"]["extract_url"]["include_description"]
    max_description_chars: int = CONFIG_DEFAULTS["plugins"]["extract_url"]["max_description_chars"]
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
    except requests.Timeout as exc:
        logger.warning("Metadata fetch timed out for %s (%s)", url, exc)
        return
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", "unknown")
        reason = getattr(response, "reason", "")
        if reason:
            logger.warning("Metadata fetch HTTP %s %s for %s", status, reason, url)
        else:
            logger.warning("Metadata fetch HTTP %s for %s", status, url)
        return
    except requests.RequestException as exc:
        logger.warning("Metadata fetch request error for %s: %s", url, exc)
        return
    except Exception:
        logger.exception("Metadata lookup failed for %s", url)
        return

    if reply:
        await bot.privmsg(channel, reply)


def _settings_from_config(bot) -> ExtractSettings:
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else {}
    section: Dict[str, Any] = {}
    if isinstance(plugins_section, dict):
        candidate = plugins_section.get("extract_url")
        if isinstance(candidate, dict):
            section = candidate

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
    safe_url = _validate_url(url)
    if not safe_url:
        logger.debug("Skipping URL due to invalid scheme/host: %s", url)
        return None

    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    request_timeout = max(1, min(settings.timeout, timeout or settings.timeout))
    connect_timeout = min(CONNECT_TIMEOUT_CAP_SECS, request_timeout)

    with requests.Session() as session:
        retry = Retry(
            total=RETRY_TOTAL,
            read=RETRY_TOTAL,
            connect=RETRY_TOTAL,
            backoff_factor=RETRY_BACKOFF_FACTOR,
            status_forcelist=RETRY_STATUS_CODES,
            allowed_methods=RETRY_ALLOWED_METHODS,
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        body_text, final_url = _fetch_html_with_limits(
            session,
            safe_url,
            headers=headers,
            connect_timeout=connect_timeout,
            request_timeout=request_timeout,
        )
    if body_text is None or final_url is None:
        return None

    parser = _MetadataParser()
    parser.feed(body_text)
    data = parser.metadata()
    if not data:
        return None

    host = urlparse(final_url).netloc or final_url
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


def _validate_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if not parsed.hostname:
        return None
    if not _is_public_host(parsed.hostname):
        logger.debug("Rejected non-public host: %s", parsed.hostname)
        return None
    return url


def _is_public_host(hostname: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(hostname)
        return ip_obj.is_global
    except ValueError:
        pass  # Not a literal IP; resolve DNS below.

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            addr = ipaddress.ip_address(sockaddr[0])
        elif family == socket.AF_INET6:
            addr = ipaddress.ip_address(sockaddr[0])
        else:
            return False
        if not addr.is_global:
            return False
    return True


def _fetch_html_with_limits(
    session: requests.Session,
    url: str,
    *,
    headers: Dict[str, str],
    connect_timeout: float,
    request_timeout: float,
) -> tuple[Optional[str], Optional[str]]:
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        response = session.get(
            current_url,
            headers=headers,
            timeout=(connect_timeout, request_timeout),
            allow_redirects=False,
            stream=True,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                return None, None
            next_url = urljoin(current_url, location)
            parsed_next = urlparse(next_url)
            if parsed_next.scheme.lower() not in {"http", "https"}:
                logger.debug("Redirect blocked due to scheme: %s", next_url)
                return None, None
            host = parsed_next.hostname
            if not host or not _is_public_host(host):
                logger.debug("Redirect blocked to non-public host: %s", next_url)
                return None, None
            current_url = next_url
            continue

        response.raise_for_status()

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/html" not in content_type:
            logger.debug("Skipping non-HTML content for %s (%s)", current_url, content_type)
            response.close()
            return None, None

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_CONTENT_BYTES:
                    logger.debug("Skipping %s due to Content-Length %s > %s", current_url, content_length, MAX_CONTENT_BYTES)
                    response.close()
                    return None, None
            except ValueError:
                pass

        body = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_CONTENT_BYTES:
                logger.debug("Aborting fetch for %s: exceeded %s bytes", current_url, MAX_CONTENT_BYTES)
                response.close()
                return None, None
        response.close()

        encoding = response.encoding or response.apparent_encoding or "utf-8"
        try:
            text = body.decode(encoding, errors="replace")
        except Exception:
            logger.debug("Failed to decode response for %s with encoding %s", current_url, encoding)
            return None, None
        return text, current_url

    logger.debug("Exceeded redirect limit for %s", url)
    return None, None


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

        prop = (attr_map.get("property") or "").lower()
        name = (attr_map.get("name") or "").lower()

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
