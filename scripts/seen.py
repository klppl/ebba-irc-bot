"""Track when users were last seen and respond to `.seen <nick>`."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_NAME = "seen_users.json"

CONFIG_DEFAULTS = {
    "plugins": {
        "seen": {
            "enabled": True,
            "storage_path": DEFAULT_STORAGE_NAME,
        }
    }
}


@dataclass
class SeenEntry:
    nick: str
    user_mask: str
    channel: str
    event: str
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nick": self.nick,
            "user_mask": self.user_mask,
            "channel": self.channel,
            "event": self.event,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["SeenEntry"]:
        try:
            return cls(
                nick=str(data["nick"]),
                user_mask=str(data.get("user_mask", "")),
                channel=str(data.get("channel", "")),
                event=str(data.get("event", "")),
                timestamp=float(data["timestamp"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class SeenSettings:
    storage_path: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / DEFAULT_STORAGE_NAME
    )


@dataclass
class SeenState:
    settings: SeenSettings
    entries: Dict[str, Dict[str, SeenEntry]] = field(default_factory=dict)


state: Optional[SeenState] = None


def on_load(bot) -> None:
    global state
    settings = _settings_from_config(bot)
    entries = _load_entries(settings.storage_path)
    state = SeenState(settings=settings, entries=entries)

    bot.plugin_manager.register_command(
        "seen",
        "seen",
        _handle_seen_command,
        help_text="Check when a user was last seen. Usage: .seen <nick>"
    )

    logger.info(
        "seen plugin loaded with storage at %s",
        settings.storage_path,
    )


def on_unload(bot) -> None:
    global state
    try:
        _persist_entries()
    finally:
        state = None
    logger.info("seen plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    if state is None:
        return
    nick = _nick_from_prefix(user)
    if not nick:
        return
    _record_seen(nick, user, channel, "message")


def on_join(bot, user: str, channel: str) -> None:
    if state is None:
        return
    nick = _nick_from_prefix(user)
    if not nick:
        return
    _record_seen(nick, user, channel, "join")


def on_part(bot, user: str, channel: str) -> None:
    if state is None:
        return
    nick = _nick_from_prefix(user)
    if not nick:
        return
    _record_seen(nick, user, channel, "part")


def on_nick(bot, user: str, new_nick: str) -> None:
    if state is None:
        return
    old_nick = _nick_from_prefix(user)
    if not old_nick:
        return
    # Record that old nick changed to new nick
    _record_seen(old_nick, user, "", f"nick change to {new_nick}")
    # Record new nick appearing
    _record_seen(new_nick, f"{new_nick}!{user.split('!', 1)[1] if '!' in user else ''}", "", f"nick change from {old_nick}")


def on_kick(bot, channel: str, target: str, kicker: str, reason: str) -> None:
    if state is None:
        return
    # Record target being kicked
    # We might not have the user mask for target, so we leave it empty or guess
    _record_seen(target, "", channel, f"kicked by {_nick_from_prefix(kicker)}")


def on_quit(bot, user: str, reason: str) -> None:
    if state is None:
        return
    nick = _nick_from_prefix(user)
    if not nick:
        return
    _record_seen(nick, user, "", f"quit ({reason})")


async def _handle_seen_command(bot, user: str, channel: str, args: List[str], is_private: bool) -> None:
    if not args:
        await bot.privmsg(channel, f"Usage: {bot.prefix}seen <nick>")
        return

    target = args[0].strip()
    await _handle_seen_query(bot, channel, target)


async def _handle_seen_query(bot, channel: str, target: str) -> None:
    assert state is not None
    nick_key = target.lower()

    # Check if we have any entries for this nick
    nick_entries = state.entries.get(nick_key, {})
    if not nick_entries:
        await bot.privmsg(channel, f"I have not seen {target} around.")
        return

    # Find the most recent entry across all channels
    best_entry: Optional[SeenEntry] = None
    for entry in nick_entries.values():
        if best_entry is None or entry.timestamp > best_entry.timestamp:
            best_entry = entry

    if best_entry is None:
        await bot.privmsg(channel, f"I have not seen {target} around.")
        return

    now = time.time()
    delta = max(0, now - best_entry.timestamp)
    delta_text = _format_timespan(delta)

    activity = _format_activity(best_entry)

    await bot.privmsg(
        channel,
        f"{best_entry.nick} was last seen {delta_text} ago {activity}.",
    )


def _record_seen(nick: str, user_mask: str, channel: str, event: str) -> None:
    assert state is not None
    nick_key = nick.lower()
    channel_key = channel.lower() if channel else "_global"

    entry = SeenEntry(
        nick=nick,
        user_mask=user_mask,
        channel=channel,
        event=event,
        timestamp=time.time(),
    )

    bucket = state.entries.setdefault(nick_key, {})
    bucket[channel_key] = entry
    _persist_entries()


def _format_timespan(seconds: float) -> str:
    units = [
        ("year", 365 * 24 * 3600),
        ("month", 30 * 24 * 3600),
        ("day", 24 * 3600),
        ("hour", 3600),
        ("minute", 60),
    ]

    parts = []
    remaining = int(seconds)
    for name, span in units:
        value = remaining // span
        if value:
            parts.append(f"{value} {name}{'s' if value != 1 else ''}")
            remaining -= value * span
        if len(parts) == 2:
            break

    if not parts:
        parts.append(f"{max(0, remaining)} seconds")

    return " ".join(parts)


def _format_activity(entry: SeenEntry) -> str:
    event = entry.event.lower() if entry.event else ""
    channel = entry.channel or ""

    if "nick change" in event or "quit" in event:
        location = ""
    elif channel.startswith("#"):
        location = f"in {channel}"
    elif channel:
        location = f"with {channel}"
    else:
        location = "somewhere"

    if event == "message":
        action = "talking"
    elif event == "join":
        action = "joining"
    elif event == "part":
        action = "parting"
    elif "kicked" in event:
        action = "getting kicked"
    elif "nick change to" in event:
        new_nick = event.split(" to ")[-1]
        action = f"changing nick to {new_nick}"
    elif "nick change from" in event:
        old_nick = event.split(" from ")[-1]
        action = f"changing nick from {old_nick}"
    elif "quit" in event:
        reason = event.split("(", 1)[1].rstrip(")") if "(" in event else ""
        action = f"quitting ({reason})" if reason else "quitting"
    else:
        action = event

    return f"{action} {location}".strip()


def _settings_from_config(bot) -> SeenSettings:
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else None
    section: Dict[str, Any] = {}
    if isinstance(plugins_section, dict):
        candidate = plugins_section.get("seen")
        if isinstance(candidate, dict):
            section = candidate

    default_path = Path(__file__).resolve().parent / DEFAULT_STORAGE_NAME
    raw_path = section.get("storage_path")
    storage_path = default_path
    if isinstance(raw_path, str) and raw_path.strip():
        candidate_path = Path(raw_path.strip()).expanduser()
        if not candidate_path.is_absolute():
            storage_path = (default_path.parent / candidate_path).resolve()
        else:
            storage_path = candidate_path

    return SeenSettings(storage_path=storage_path)


def _load_entries(path: Path) -> Dict[str, Dict[str, SeenEntry]]:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        logger.warning("Failed to load seen data from %s", path, exc_info=True)
        return {}

    entries: Dict[str, Dict[str, SeenEntry]] = {}
    if not isinstance(payload, dict):
        return entries

    for key, entry_data in payload.items():
        _ingest_loaded_entry(entries, key, entry_data)
    return entries


def _persist_entries() -> None:
    if state is None:
        return

    path = state.settings.storage_path
    payload: Dict[str, Dict[str, Any]] = {}
    for nick_key, channel_entries in state.entries.items():
        if not channel_entries:
            continue
        payload[nick_key] = {
            channel_key: entry.to_dict()
            for channel_key, entry in channel_entries.items()
        }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
    except Exception:
        logger.warning("Failed to persist seen data to %s", path, exc_info=True)


def _nick_from_prefix(prefix: str) -> str:
    if not prefix:
        return ""
    return prefix.split("!", 1)[0]


def _ingest_loaded_entry(
    store: Dict[str, Dict[str, SeenEntry]], key: Any, payload: Any
) -> None:
    if not isinstance(key, str) or not isinstance(payload, dict):
        return

    if "timestamp" in payload:
        entry = SeenEntry.from_dict(payload)
        if entry is None:
            return
        nick_key = (entry.nick or key).lower()
        channel_key = (entry.channel or "").lower()
        store.setdefault(nick_key, {})[channel_key] = entry
        return

    for channel_label, channel_payload in payload.items():
        if not isinstance(channel_label, str) or not isinstance(channel_payload, dict):
            continue
        entry = SeenEntry.from_dict(channel_payload)
        if entry is None:
            continue
        if not entry.channel:
            entry.channel = channel_label
        nick_key = (entry.nick or key).lower()
        channel_key = (entry.channel or channel_label or "").lower()
        store.setdefault(nick_key, {})[channel_key] = entry
