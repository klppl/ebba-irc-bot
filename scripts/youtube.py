"""YouTube metadata responder for linked videos and playlists."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)

VIDEO_API_URL = "https://www.googleapis.com/youtube/v3/videos"
PLAYLIST_API_URL = "https://www.googleapis.com/youtube/v3/playlists"

YOUTUBE_URL_PATTERN = re.compile(
    r"https?://(?:(?:www|m)\.)?(?:youtube\.com|youtu\.be)/[^\s<>]+",
    re.IGNORECASE,
)

ISO_DURATION_RE = re.compile(
    r"^PT"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?$",
    re.IGNORECASE,
)

ALLOWED_INFO_ITEMS = {"length", "uploader", "views", "likes", "date"}
IGNORE_PLAYLIST_IDS = {"WL", "LL", "FL", "LM"}


CONFIG_DEFAULTS = {
    "plugins": {
        "youtube": {
            "enabled": True,
            "api_key": "",
            "info_items": ["length", "uploader", "views", "likes", "date"],
            "playlist_watch": True,
            "timeout": 10,
            "max_urls_per_message": 2,
        }
    }
}


@dataclass(frozen=True)
class YouTubeTarget:
    video_id: Optional[str]
    playlist_id: Optional[str]
    original_url: str


@dataclass
class YouTubeSettings:
    api_key: str = CONFIG_DEFAULTS["plugins"]["youtube"]["api_key"]
    info_items: Tuple[str, ...] = tuple(
        CONFIG_DEFAULTS["plugins"]["youtube"]["info_items"]
    )
    playlist_watch: bool = CONFIG_DEFAULTS["plugins"]["youtube"]["playlist_watch"]
    timeout: int = CONFIG_DEFAULTS["plugins"]["youtube"]["timeout"]
    max_urls_per_message: int = CONFIG_DEFAULTS["plugins"]["youtube"][
        "max_urls_per_message"
    ]


_API_KEY_WARNED = False


def on_load(bot) -> None:
    logger.info("youtube plugin loaded from %s", __file__)


def on_unload(bot) -> None:
    logger.info("youtube plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    targets = list(_iter_targets(message))
    if not targets:
        return

    settings = _settings_from_config(bot)
    if not settings.api_key:
        _warn_missing_api_key_once()
        return

    loop = asyncio.get_running_loop()
    for target in targets[: settings.max_urls_per_message]:
        loop.create_task(_handle_youtube(bot, channel, target, settings))


def _iter_targets(message: str) -> Iterable[YouTubeTarget]:
    seen: Set[Tuple[Optional[str], Optional[str]]] = set()
    for match in YOUTUBE_URL_PATTERN.finditer(message):
        raw_url = match.group(0).rstrip(").,!?")
        video_id, playlist_id = _extract_ids(raw_url)
        if not video_id and not playlist_id:
            continue
        key = (video_id, playlist_id)
        if key in seen:
            continue
        seen.add(key)
        yield YouTubeTarget(video_id=video_id, playlist_id=playlist_id, original_url=raw_url)


def _extract_ids(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        parsed = urlparse(url)
    except Exception:
        return None, None

    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    query = parse_qs(parsed.query)
    video_id: Optional[str] = None
    playlist_id: Optional[str] = None

    if "v" in query:
        video_id = _normalize_video_id(query.get("v", [""])[0])

    if host.endswith("youtu.be"):
        segments = [segment for segment in path.split("/") if segment]
        if segments:
            video_id = _normalize_video_id(segments[0])

    if "youtube.com" in host:
        if path.startswith("/shorts/") or path.startswith("/live/") or path.startswith("/embed/"):
            segments = [segment for segment in path.split("/") if segment]
            if len(segments) >= 2:
                video_id = _normalize_video_id(segments[1])
        elif path.startswith("/watch"):
            # already handled via ?v=
            pass
        elif path.startswith("/playlist"):
            playlist_id = _normalize_playlist_id(query.get("list", [""])[0])

    if "list" in query:
        playlist_id = _normalize_playlist_id(query.get("list", [""])[0])

    if not video_id and path.strip("/"):
        # For youtu.be style or other direct paths
        segments = [segment for segment in path.split("/") if segment]
        if segments:
            video_id = _normalize_video_id(segments[-1])

    return video_id, playlist_id


def _normalize_video_id(candidate: str) -> Optional[str]:
    if not candidate:
        return None
    stripped = candidate.strip()
    if len(stripped) < 6:  # basic sanity check
        return None
    return stripped


def _normalize_playlist_id(candidate: str) -> Optional[str]:
    if not candidate:
        return None
    stripped = candidate.strip()
    if not stripped or stripped.upper() in IGNORE_PLAYLIST_IDS:
        return None
    return stripped


async def _handle_youtube(bot, channel: str, target: YouTubeTarget, settings: YouTubeSettings) -> None:
    loop = asyncio.get_running_loop()
    request_timeout = getattr(bot, "request_timeout", 0)
    try:
        lines = await loop.run_in_executor(
            None,
            lambda: _fetch_and_format(target, settings, request_timeout),
        )
    except Exception:
        logger.exception("youtube plugin failed to process target %s", target.original_url)
        return

    for line in lines:
        if line:
            await bot.privmsg(channel, line)


def _fetch_and_format(
    target: YouTubeTarget, settings: YouTubeSettings, bot_timeout: int
) -> List[str]:
    request_timeout = _resolve_timeout(settings.timeout, bot_timeout)
    messages: List[str] = []

    if target.video_id:
        video_data = _fetch_video_data(target.video_id, settings, request_timeout)
        if video_data is not None:
            messages.append(_format_video_message(video_data, target.video_id, settings))

    if target.playlist_id and settings.playlist_watch:
        playlist_data = _fetch_playlist_data(
            target.playlist_id, settings, request_timeout
        )
        if playlist_data is not None:
            messages.append(_format_playlist_message(playlist_data, target.playlist_id))

    return messages


def _fetch_video_data(
    video_id: str, settings: YouTubeSettings, timeout: int
) -> Optional[Dict[str, object]]:
    params = {
        "id": video_id,
        "part": "snippet,contentDetails,statistics",
        "key": settings.api_key,
        "maxResults": 1,
    }
    try:
        response = requests.get(VIDEO_API_URL, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items") or []
        if not items:
            return None
        item = items[0]
        snippet = item.get("snippet") or {}
        details = item.get("contentDetails") or {}
        statistics = item.get("statistics") or {}
        return {
            "title": snippet.get("title"),
            "channel": snippet.get("channelTitle"),
            "published_at": snippet.get("publishedAt"),
            "duration": details.get("duration"),
            "views": statistics.get("viewCount"),
            "likes": statistics.get("likeCount"),
        }
    except requests.RequestException as exc:
        logger.warning("YouTube video lookup failed for %s: %s", video_id, exc)
        return None


def _fetch_playlist_data(
    playlist_id: str, settings: YouTubeSettings, timeout: int
) -> Optional[Dict[str, object]]:
    params = {
        "id": playlist_id,
        "part": "snippet,contentDetails",
        "key": settings.api_key,
        "maxResults": 1,
    }
    try:
        response = requests.get(PLAYLIST_API_URL, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items") or []
        if not items:
            return None
        item = items[0]
        snippet = item.get("snippet") or {}
        details = item.get("contentDetails") or {}
        return {
            "title": snippet.get("title"),
            "channel": snippet.get("channelTitle"),
            "published_at": snippet.get("publishedAt"),
            "item_count": details.get("itemCount"),
        }
    except requests.RequestException as exc:
        logger.warning("YouTube playlist lookup failed for %s: %s", playlist_id, exc)
        return None


def _format_video_message(
    video: Dict[str, object], video_id: str, settings: YouTubeSettings
) -> str:
    parts = [f"{video.get('title') or 'Unknown title'}"]
    info_items = settings.info_items
    for item in info_items:
        if item == "uploader":
            channel = video.get("channel")
            if channel:
                parts.append(f"Channel: {channel}")
        elif item == "date":
            date_text = _format_date(video.get("published_at"))
            if date_text:
                parts.append(f"Uploaded: {date_text}")
        elif item == "length":
            duration = _format_duration(video.get("duration"))
            if duration:
                parts.append(f"Length: {duration}")
        elif item == "views":
            views_text = _format_number(video.get("views"))
            if views_text:
                parts.append(f"{views_text} views")
        elif item == "likes":
            likes_text = _format_number(video.get("likes"))
            if likes_text:
                parts.append(f"{likes_text} likes")

    parts.append(f"Link: https://youtu.be/{video_id}")
    return " | ".join(parts)


def _format_playlist_message(playlist: Dict[str, object], playlist_id: str) -> str:
    parts = [
        f"[YouTube] Playlist: {playlist.get('title') or 'Unknown title'}",
    ]
    channel = playlist.get("channel")
    if channel:
        parts.append(f"Channel: {channel}")
    item_count = playlist.get("item_count")
    if isinstance(item_count, int):
        parts.append(f"Items: {item_count}")
    date_text = _format_date(playlist.get("published_at"))
    if date_text:
        parts.append(f"Created: {date_text}")
    parts.append(f"Link: https://www.youtube.com/playlist?list={playlist_id}")
    return " | ".join(parts)


def _format_number(value: object) -> Optional[str]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return f"{number:,}"


def _format_date(raw: object) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d")


def _format_duration(raw: object) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    match = ISO_DURATION_RE.fullmatch(raw)
    if not match:
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    if minutes:
        return f"{minutes}:{seconds:02d}"
    return f"0:{seconds:02d}"


def _settings_from_config(bot) -> YouTubeSettings:
    config = getattr(bot, "config", {}) or {}
    plugins_section = config.get("plugins", {}) if isinstance(config, dict) else {}
    section = {}
    if isinstance(plugins_section, dict):
        candidate = plugins_section.get("youtube")
        if isinstance(candidate, dict):
            section = candidate

    defaults = YouTubeSettings()
    api_key = str(section.get("api_key", defaults.api_key)).strip()
    playlist_watch = bool(section.get("playlist_watch", defaults.playlist_watch))
    timeout = _coerce_int(section.get("timeout"), defaults.timeout, minimum=1)
    max_urls = _coerce_int(
        section.get("max_urls_per_message"),
        defaults.max_urls_per_message,
        minimum=1,
    )
    info_items = _normalize_info_items(section.get("info_items"), defaults.info_items)

    return YouTubeSettings(
        api_key=api_key,
        info_items=info_items,
        playlist_watch=playlist_watch,
        timeout=timeout,
        max_urls_per_message=max_urls,
    )


def _normalize_info_items(raw: object, fallback: Tuple[str, ...]) -> Tuple[str, ...]:
    if isinstance(raw, str):
        raw_items = [raw]
    elif isinstance(raw, Iterable):
        raw_items = list(raw)
    else:
        return tuple(fallback)

    normalized: List[str] = []
    for item in raw_items:
        try:
            text = str(item).strip().lower()
        except Exception:
            continue
        if not text or text not in ALLOWED_INFO_ITEMS:
            continue
        if text not in normalized:
            normalized.append(text)

    return tuple(normalized or fallback)


def _coerce_int(value: object, fallback: int, minimum: int = 0) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, coerced)


def _resolve_timeout(settings_timeout: int, bot_timeout: int) -> int:
    if isinstance(bot_timeout, int) and bot_timeout > 0:
        return max(1, min(settings_timeout, bot_timeout))
    return max(1, settings_timeout)


def _warn_missing_api_key_once() -> None:
    global _API_KEY_WARNED
    if not _API_KEY_WARNED:
        logger.warning("YouTube plugin requires an API key; configure 'plugins.youtube.api_key'")
        _API_KEY_WARNED = True


