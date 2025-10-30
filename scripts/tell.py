"""Deferred message delivery plugin triggered by `.tell`."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_NAME = "tell_messages.json"
DEFAULT_MAX_PER_TARGET = 5
DEFAULT_MAX_MESSAGE_LENGTH = 400
DEFAULT_TRIGGERS = ["tell"]

CONFIG_DEFAULTS = {
    "plugins": {
        "tell": {
            "enabled": True,
            "storage_path": DEFAULT_STORAGE_NAME,
            "max_messages_per_target": DEFAULT_MAX_PER_TARGET,
            "max_message_length": DEFAULT_MAX_MESSAGE_LENGTH,
            "triggers": list(DEFAULT_TRIGGERS),
        }
    }
}


@dataclass
class TellEntry:
    sender: str
    origin: str
    message: str
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sender": self.sender,
            "origin": self.origin,
            "message": self.message,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["TellEntry"]:
        try:
            sender = str(data["sender"])
            origin = str(data["origin"])
            message = str(data["message"])
            created_at = float(data["created_at"])
        except (KeyError, TypeError, ValueError):
            return None
        return cls(sender=sender, origin=origin, message=message, created_at=created_at)


@dataclass
class TellSettings:
    storage_path: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / DEFAULT_STORAGE_NAME
    )
    max_messages_per_target: int = DEFAULT_MAX_PER_TARGET
    max_message_length: int = DEFAULT_MAX_MESSAGE_LENGTH
    triggers: List[str] = field(default_factory=lambda: list(DEFAULT_TRIGGERS))


@dataclass
class TellState:
    settings: TellSettings
    pending: Dict[str, Dict[str, List[TellEntry]]] = field(default_factory=dict)
    delivering: Set[str] = field(default_factory=set)


state: Optional[TellState] = None


def on_load(bot) -> None:
    global state

    settings = _settings_from_config(bot)
    pending = _load_pending(settings.storage_path)
    state = TellState(settings=settings, pending=pending)
    trigger_text = ", ".join(f"{bot.prefix}{trigger}" for trigger in settings.triggers)
    logger.info(
        "tell plugin loaded with storage at %s; responding to %s",
        settings.storage_path,
        trigger_text,
    )


def on_unload(bot) -> None:
    global state
    try:
        _save_pending()
    finally:
        state = None
    logger.info("tell plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    global state
    if state is None:
        return

    nick = _nick_from_prefix(user)
    if not nick:
        return

    if _has_pending_for(nick, channel):
        loop = asyncio.get_running_loop()
        loop.create_task(_deliver_pending(bot, nick, channel))

    prefix = bot.prefix
    if not message.startswith(prefix):
        return

    command_body = message[len(prefix) :].strip()
    if not command_body:
        return

    parts = command_body.split(maxsplit=2)
    if not parts:
        return

    if parts[0].lower() not in state.settings.triggers:
        return

    if len(parts) < 2:
        loop = asyncio.get_running_loop()
        primary = state.settings.triggers[0] if state.settings.triggers else DEFAULT_TRIGGERS[0]
        loop.create_task(
            bot.privmsg(channel, f"Usage: {prefix}{primary} <nick> <message>")
        )
        return

    if len(parts) < 3 or not parts[2].strip():
        loop = asyncio.get_running_loop()
        loop.create_task(
            bot.privmsg(channel, f"Please provide a message to pass on.")
        )
        return

    target = parts[1]
    text = parts[2].strip()
    loop = asyncio.get_running_loop()
    loop.create_task(_handle_tell_command(bot, nick, channel, target, text))


def on_join(bot, user: str, channel: str) -> None:
    global state
    if state is None:
        return

    nick = _nick_from_prefix(user)
    if not nick:
        return

    if _has_pending_for(nick, channel):
        loop = asyncio.get_running_loop()
        loop.create_task(_deliver_pending(bot, nick, channel))


def _settings_from_config(bot) -> TellSettings:
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else None
    section = {}
    if isinstance(plugins_section, dict):
        candidate = plugins_section.get("tell")
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

    max_messages = section.get("max_messages_per_target", DEFAULT_MAX_PER_TARGET)
    if not isinstance(max_messages, int) or max_messages < 1:
        max_messages = DEFAULT_MAX_PER_TARGET

    max_length = section.get("max_message_length", DEFAULT_MAX_MESSAGE_LENGTH)
    if not isinstance(max_length, int) or max_length < 1:
        max_length = DEFAULT_MAX_MESSAGE_LENGTH

    triggers = _parse_triggers(section.get("triggers"), DEFAULT_TRIGGERS)

    return TellSettings(
        storage_path=storage_path,
        max_messages_per_target=max_messages,
        max_message_length=max_length,
        triggers=triggers,
    )


def _load_pending(path: Path) -> Dict[str, Dict[str, List[TellEntry]]]:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as handle:
            raw_data = json.load(handle)
    except Exception:
        logger.warning("Failed to load tells from %s", path, exc_info=True)
        return {}

    pending: Dict[str, Dict[str, List[TellEntry]]] = {}
    if not isinstance(raw_data, dict):
        return pending

    for nick_key, entries in raw_data.items():
        _ingest_pending_entry(pending, nick_key, entries)
    return pending


async def _handle_tell_command(
    bot, sender: str, origin: str, target: str, message: str
) -> None:
    assert state is not None
    settings = state.settings

    sender_norm = sender.lower()
    target_norm = target.lower()
    origin_key = _origin_key(origin)

    if sender_norm == target_norm:
        await bot.privmsg(origin, "You cannot leave messages for yourself.")
        return

    if len(message) > settings.max_message_length:
        await bot.privmsg(
            origin,
            f"Message too long (limit {settings.max_message_length} characters).",
        )
        return

    entry_map = state.pending.setdefault(target_norm, {})
    queued_total = sum(len(bucket) for bucket in entry_map.values())
    if queued_total >= settings.max_messages_per_target:
        await bot.privmsg(
            origin,
            f"{target} already has {settings.max_messages_per_target} pending messages.",
        )
        return

    entry = TellEntry(
        sender=sender,
        origin=origin,
        message=message,
        created_at=time.time(),
    )
    entry_map.setdefault(origin_key, []).append(entry)
    _save_pending()
    await bot.privmsg(origin, f"Sure thing, I will let {target} know.")


async def _deliver_pending(bot, nick: str, context_channel: str) -> None:
    assert state is not None
    key = nick.lower()
    channel_key = _origin_key(context_channel)

    if key in state.delivering:
        return

    entry_map = state.pending.get(key)
    if not entry_map:
        return

    entries = entry_map.get(channel_key)
    if not entries:
        return

    state.delivering.add(key)
    try:
        while entries:
            entry = entries[0]
            text = _format_delivery(nick, entry)
            delivery_target = entry.origin or context_channel or nick
            if delivery_target.lower() == bot.nickname.lower():
                delivery_target = nick
            try:
                await bot.privmsg(delivery_target, text)
            except Exception:
                logger.exception("Failed to deliver tell for %s", nick)
                _save_pending()
                return
            entries.pop(0)

        if not entries:
            entry_map.pop(channel_key, None)
        if not entry_map:
            state.pending.pop(key, None)
        _save_pending()
    finally:
        state.delivering.discard(key)


def _format_delivery(nick: str, entry: TellEntry) -> str:
    sender = entry.sender
    message = entry.message
    return f"While you were away {nick} | {sender}: {message}"


def _save_pending() -> None:
    if state is None:
        return

    path = state.settings.storage_path
    payload: Dict[str, Dict[str, Any]] = {}
    for nick, channel_map in state.pending.items():
        channel_payload: Dict[str, Any] = {}
        for channel_key, entries in channel_map.items():
            serialized = [
                entry.to_dict() for entry in entries if isinstance(entry, TellEntry)
            ]
            if not serialized:
                continue
            channel_label = entries[0].origin or channel_key
            channel_payload[channel_label] = serialized
        if channel_payload:
            payload[nick] = channel_payload

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
    except Exception:
        logger.warning("Failed to persist tell storage to %s", path, exc_info=True)


def _nick_from_prefix(prefix: str) -> str:
    if not prefix:
        return ""
    return prefix.split("!", 1)[0]


def _origin_key(origin: str) -> str:
    return origin.lower().strip() if origin else ""


def _parse_triggers(raw: Any, fallback: Iterable[str]) -> List[str]:
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


def _has_pending_for(nick: str, channel: str) -> bool:
    if state is None:
        return False
    entry_map = state.pending.get(nick.lower())
    if not entry_map:
        return False
    channel_key = _origin_key(channel)
    entries = entry_map.get(channel_key)
    return bool(entries)


def _ingest_pending_entry(
    store: Dict[str, Dict[str, List[TellEntry]]], key: Any, payload: Any
) -> None:
    if not isinstance(key, str):
        return

    nick_key = key.lower()
    buckets = store.setdefault(nick_key, {})

    if isinstance(payload, list):
        for entry_data in payload:
            _append_entry_from_payload(buckets, entry_data)
        return

    if isinstance(payload, dict):
        for channel_label, entries in payload.items():
            if not isinstance(entries, list):
                continue
            for entry_data in entries:
                _append_entry_from_payload(
                    buckets, entry_data, channel_label=channel_label
                )


def _append_entry_from_payload(
    buckets: Dict[str, List[TellEntry]], data: Any, channel_label: Optional[str] = None
) -> None:
    if not isinstance(data, dict):
        return
    entry = TellEntry.from_dict(data)
    if entry is None:
        return
    origin = entry.origin or (channel_label or "")
    origin_key = _origin_key(origin)
    if channel_label and not entry.origin:
        entry.origin = channel_label
    buckets.setdefault(origin_key, []).append(entry)
