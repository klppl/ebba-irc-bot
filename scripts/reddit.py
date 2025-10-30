"""Reddit link summarizer (optional config: `plugins.reddit`)."""

import asyncio
import datetime as dt
import html
import logging
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)


URL_PATTERN = re.compile(
    r"https?://(?:www\.|old\.|new\.|np\.|amp\.)?reddit\.com[^\s<>]+|https?://redd\.it/[^\s<>]+",
    re.IGNORECASE,
)

CONFIG_DEFAULTS = {
    "plugins": {
        "reddit": {
            "enabled": True,
            "max_chars": 240,
            "user_agent": "ebba-irc-bot reddit plugin (+https://github.com/alex/ebba-irc-bot)",
            "timeout": 10,
            "max_urls_per_message": 2,
            "link_thread_template": "[Reddit] /r/{subreddit} - {title} | {points} | {comments} | {age}",
            "text_thread_template": "[Reddit] /r/{subreddit} (Self) - {title} | {points} | {age} | {extract}",
            "comment_template": "[Reddit] Comment by {author} | {points} | {age} | {extract}",
            "user_template": "[Reddit] User: {name} | Karma: {link_karma} / {comment_karma} | {age}",
        }
    }
}


 


@dataclass
class RedditTemplates:
    link_thread: str = "[Reddit] /r/{subreddit} - {title} | {points} | {comments} | {age}"
    text_thread: str = "[Reddit] /r/{subreddit} (Self) - {title} | {points} | {age} | {extract}"
    comment: str = "[Reddit] Comment by {author} | {points} | {age} | {extract}"
    user: str = "[Reddit] User: {name} | Karma: {link_karma} / {comment_karma} | {age}"


@dataclass
class RedditSettings:
    max_chars: int = CONFIG_DEFAULTS["plugins"]["reddit"]["max_chars"]
    user_agent: str = CONFIG_DEFAULTS["plugins"]["reddit"]["user_agent"]
    timeout: int = CONFIG_DEFAULTS["plugins"]["reddit"]["timeout"]
    max_urls_per_message: int = CONFIG_DEFAULTS["plugins"]["reddit"]["max_urls_per_message"]
    templates: RedditTemplates = field(default_factory=RedditTemplates)


def on_load(bot) -> None:
    logger.info("reddit plugin loaded from %s", __file__)


def on_unload(bot) -> None:
    logger.info("reddit plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    matches = list(_iter_urls(message))
    if not matches:
        return

    loop = asyncio.get_running_loop()
    settings = _settings_from_config(bot)
    for url in matches[: settings.max_urls_per_message]:
        loop.create_task(_handle_reddit_url(bot, channel, url, settings))


def _iter_urls(message: str) -> Iterable[str]:
    for match in URL_PATTERN.finditer(message):
        url = match.group(0).rstrip(").,!?")
        yield url


async def _handle_reddit_url(bot, channel: str, url: str, settings: RedditSettings) -> None:
    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(
            None, lambda: _fetch_and_format(url, settings, timeout=bot.request_timeout)
        )
    except requests.RequestException:
        logger.warning("Reddit request error for %s", url, exc_info=True)
        return
    except Exception:
        logger.exception("Reddit lookup failed for %s", url)
        return

    if reply:
        await bot.privmsg(channel, reply)


def _settings_from_config(bot) -> RedditSettings:
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else {}
    section: Dict[str, object] = {}
    if isinstance(plugins_section, dict):
        candidate = plugins_section.get("reddit")
        if isinstance(candidate, dict):
            section = candidate

    defaults = RedditSettings()

    template_defaults = defaults.templates
    templates = RedditTemplates(
        link_thread=str(section.get("link_thread_template", template_defaults.link_thread)),
        text_thread=str(section.get("text_thread_template", template_defaults.text_thread)),
        comment=str(section.get("comment_template", template_defaults.comment)),
        user=str(section.get("user_template", template_defaults.user)),
    )

    raw_max_chars = section.get("max_chars")
    max_chars = defaults.max_chars
    if isinstance(raw_max_chars, int):
        max_chars = raw_max_chars
    elif isinstance(raw_max_chars, str):
        try:
            max_chars = int(raw_max_chars)
        except ValueError:
            max_chars = defaults.max_chars

    user_agent = str(section.get("user_agent", defaults.user_agent))

    raw_timeout = section.get("timeout")
    timeout = defaults.timeout
    if isinstance(raw_timeout, int):
        timeout = raw_timeout
    elif isinstance(raw_timeout, str):
        try:
            timeout = int(raw_timeout)
        except ValueError:
            timeout = defaults.timeout

    raw_max_urls = section.get("max_urls_per_message", defaults.max_urls_per_message)
    try:
        max_urls = max(1, int(raw_max_urls))
    except (TypeError, ValueError):
        max_urls = defaults.max_urls_per_message

    return RedditSettings(
        max_chars=max_chars,
        user_agent=user_agent,
        timeout=timeout,
        max_urls_per_message=max_urls,
        templates=templates,
    )


def _fetch_and_format(url: str, settings: RedditSettings, timeout: int) -> Optional[str]:
    parsed = _resolve_reddit_url(url)
    if parsed is None:
        return None

    link_type, api_url, meta = parsed
    headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}

    request_timeout = max(1, min(settings.timeout, timeout or settings.timeout))
    response = requests.get(api_url, headers=headers, timeout=request_timeout)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise requests.RequestException(f"Invalid JSON from Reddit for {api_url}") from exc
    link_type, data, extract = _extract_payload(link_type, payload, settings.max_chars)
    if data is None:
        return None

    fields = _build_template_fields(link_type, data, extract, meta)
    return _render_template(link_type, fields, settings.templates)


def _resolve_reddit_url(url: str) -> Optional[Tuple[str, str, Dict[str, str]]]:
    info = urlparse(url)
    path = info.path.rstrip("/")
    host = info.netloc.lower()

    if "reddit" not in host and host != "redd.it":
        return None

    if host == "redd.it":
        thread = path.strip("/")
        if not thread:
            return None
        api_url = f"https://www.reddit.com/comments/{thread}.json"
        return ("thread", api_url, {"permalink": f"/comments/{thread}/"})

    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return None

    if segments[0] in {"u", "user"} and len(segments) >= 2:
        user_name = segments[1]
        api_url = f"https://www.reddit.com/user/{user_name}/about.json"
        return ("user", api_url, {"name": user_name})

    if segments[0] == "comments" and len(segments) >= 1:
        thread_id = segments[1] if len(segments) >= 2 else None
        if not thread_id:
            return None
        api_url = f"https://www.reddit.com/comments/{thread_id}.json"
        return ("thread", api_url, {"permalink": f"/comments/{thread_id}/"})

    if segments[0] == "r" and len(segments) >= 3 and segments[2] == "comments":
        subreddit = segments[1]
        thread_id = segments[3] if len(segments) >= 4 else None
        if not thread_id:
            return None

        slug = segments[4] if len(segments) >= 5 else ""
        comment_id = None

        if len(segments) >= 6:
            comment_id = segments[5]
        elif info.fragment:
            qs = parse_qs(info.fragment)
            comment_id = qs.get("comment", [None])[0]
        else:
            qs = parse_qs(info.query)
            comment_id = qs.get("comment", [None])[0]

        if comment_id:
            slug = slug or "_"
            api_url = (
                f"https://www.reddit.com/r/{subreddit}/comments/{thread_id}/{slug}/{comment_id}.json"
            )
            return (
                "comment",
                api_url,
                {"subreddit": subreddit, "thread_id": thread_id, "comment_id": comment_id},
            )

        api_url = f"https://www.reddit.com/r/{subreddit}/comments/{thread_id}.json"
        return ("thread", api_url, {"subreddit": subreddit, "thread_id": thread_id})

    return None


def _extract_payload(
    link_type: str, payload, max_chars: int
) -> Tuple[str, Optional[Mapping[str, Any]], str]:
    try:
        if link_type == "thread":
            data = payload[0]["data"]["children"][0]["data"]
            extract = data.get("selftext", "") if data.get("is_self") else ""
            if data.get("is_self"):
                link_type = "text_thread"
        elif link_type == "comment":
            data = payload[1]["data"]["children"][0]["data"]
            extract = data.get("body", "")
        elif link_type == "user":
            data = payload["data"]
            extract = ""
        else:
            return link_type, None, ""
    except (KeyError, IndexError, TypeError):
        logger.debug("Unexpected Reddit JSON structure for type %s", link_type)
        return link_type, None, ""

    extract = _prepare_extract(extract, max_chars)
    return link_type, data, extract


def _prepare_extract(text: str, max_chars: int) -> str:
    if not text:
        return ""

    cleaned = html.unescape(text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if len(cleaned) <= max_chars:
        return cleaned

    try:
        shortened = textwrap.shorten(cleaned, width=max_chars, placeholder="...")
    except ValueError:
        shortened = cleaned[: max(0, max_chars - 3)].rstrip() + "..."

    return shortened


def _build_template_fields(
    link_type: str, data: Mapping[str, Any], extract: str, meta: Dict[str, str]
) -> Dict[str, str]:
    created_ts = data.get("created_utc")
    created_text, age_text = _format_age(created_ts)

    score = _coerce_int(data.get("score", 0))
    num_comments = data.get("num_comments")
    upvote_ratio = data.get("upvote_ratio")

    fields = {
        "title": data.get("title", ""),
        "subreddit": data.get("subreddit", meta.get("subreddit", "")),
        "url": data.get("url", ""),
        "author": data.get("author", ""),
        "name": data.get("name", meta.get("name", "")),
        "created": created_text,
        "age": age_text,
        "points": _format_points(score),
        "score": _format_number(score),
        "comments": _format_comments(num_comments),
        "percent": f"{int(upvote_ratio * 100)}%" if isinstance(upvote_ratio, (float, int)) else "",
        "link_karma": _format_number(data.get("link_karma")),
        "comment_karma": _format_number(data.get("comment_karma")),
        "extract": extract,
        "permalink": data.get("permalink", meta.get("permalink", "")),
        "domain": data.get("domain", ""),
    }

    if link_type == "comment":
        fields.setdefault("subreddit", meta.get("subreddit", ""))

    return fields


def _render_template(link_type: str, fields: Dict[str, str], templates: RedditTemplates) -> str:
    template_map = {
        "link_thread": templates.link_thread,
        "text_thread": templates.text_thread,
        "comment": templates.comment,
        "user": templates.user,
    }
    template = template_map.get(link_type, "[Reddit] {title}")
    try:
        return template.format(**fields)
    except Exception:
        logger.exception("Failed to render Reddit template %s", link_type)
        return fields.get("title", "")


def _format_age(created_utc) -> Tuple[str, str]:
    if not isinstance(created_utc, (int, float)):
        return "", ""

    created = dt.datetime.utcfromtimestamp(created_utc)
    now = dt.datetime.utcnow()
    delta = now - created

    if delta.days < 0:
        return created.strftime("%Y-%m-%d"), ""

    if delta.days == 0:
        if delta.seconds < 60:
            age = "just now"
        elif delta.seconds < 3600:
            minutes = delta.seconds // 60
            age = f"{minutes}m ago"
        else:
            hours = delta.seconds // 3600
            age = f"{hours}h ago"
    elif delta.days == 1:
        age = "yesterday"
    elif delta.days < 365:
        age = f"{delta.days}d ago"
    else:
        years = delta.days // 365
        days = delta.days % 365
        age = f"{years}y {days}d ago" if days else f"{years}y ago"

    return created.strftime("%Y-%m-%d"), age


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _format_number(value) -> str:
    number = _coerce_int(value)
    return f"{number:,}"


def _format_points(score: int) -> str:
    label = "point" if abs(score) == 1 else "points"
    return f"{_format_number(score)} {label}"


def _format_comments(value) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "0 comments"
    label = "comment" if abs(number) == 1 else "comments"
    return f"{_format_number(number)} {label}"
