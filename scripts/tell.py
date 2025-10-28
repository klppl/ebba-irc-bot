"""Deferred message delivery plugin triggered by `.tell`."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_NAME = "tell_messages.json"
DEFAULT_MAX_PER_TARGET = 5
DEFAULT_MAX_MESSAGE_LENGTH = 400

CONFIG_DEFAULTS = {
    "plugins": {
        "tell": {
            "enabled": True,
            "storage_path": DEFAULT_STORAGE_NAME,
            "max_messages_per_target": DEFAULT_MAX_PER_TARGET,
            "max_message_length": DEFAULT_MAX_MESSAGE_LENGTH,
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


@dataclass
class TellState:
    settings: TellSettings
    pending: Dict[str, List[TellEntry]] = field(default_factory=dict)
    delivering: Set[str] = field(default_factory=set)


state: Optional[TellState] = None


def on_load(bot) -> None:
    global state

    settings = _settings_from_config(bot)
    pending = _load_pending(settings.storage_path)
    state = TellState(settings=settings, pending=pending)
    logger.info("tell plugin loaded with storage at %s", settings.storage_path)


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

    if state.pending.get(nick.lower()):
        loop = asyncio.get_running_loop()
        loop.create_task(_deliver_pending(bot, nick, channel))

    prefix = bot.prefix
    if not message.startswith(prefix):
        return

    command_body = message[len(prefix) :].strip()
    if not command_body:
        return

    parts = command_body.split(maxsplit=2)
    if not parts or parts[0].lower() != "tell":
        return

    if len(parts) < 2:
        loop = asyncio.get_running_loop()
        loop.create_task(
            bot.privmsg(channel, f"Usage: {prefix}tell <nick> <message>")
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

    if state.pending.get(nick.lower()):
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

    return TellSettings(
        storage_path=storage_path,
        max_messages_per_target=max_messages,
        max_message_length=max_length,
    )


def _load_pending(path: Path) -> Dict[str, List[TellEntry]]:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as handle:
            raw_data = json.load(handle)
    except Exception:
        logger.warning("Failed to load tells from %s", path, exc_info=True)
        return {}

    pending: Dict[str, List[TellEntry]] = {}
    if not isinstance(raw_data, dict):
        return pending

    for nick_key, entries in raw_data.items():
        if not isinstance(nick_key, str) or not isinstance(entries, list):
            continue
        for entry_data in entries:
            if not isinstance(entry_data, dict):
                continue
            entry = TellEntry.from_dict(entry_data)
            if entry is None:
                continue
            pending.setdefault(nick_key.lower(), []).append(entry)
    return pending


async def _handle_tell_command(
    bot, sender: str, origin: str, target: str, message: str
) -> None:
    assert state is not None
    settings = state.settings

    sender_norm = sender.lower()
    target_norm = target.lower()

    if sender_norm == target_norm:
        await bot.privmsg(origin, "You cannot leave messages for yourself.")
        return

    if len(message) > settings.max_message_length:
        await bot.privmsg(
            origin,
            f"Message too long (limit {settings.max_message_length} characters).",
        )
        return

    entries = state.pending.setdefault(target_norm, [])
    if len(entries) >= settings.max_messages_per_target:
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
    entries.append(entry)
    _save_pending()
    await bot.privmsg(origin, f"Sure thing, I will let {target} know.")


async def _deliver_pending(bot, nick: str, context_channel: str) -> None:
    assert state is not None
    key = nick.lower()

    if key in state.delivering:
        return

    entries = state.pending.get(key)
    if not entries:
        return

    state.delivering.add(key)
    try:
        delivery_target = context_channel if context_channel else nick
        if delivery_target.lower() == bot.nickname.lower():
            delivery_target = nick

        while entries:
            entry = entries[0]
            text = _format_delivery(nick, entry)
            try:
                await bot.privmsg(delivery_target, text)
            except Exception:
                logger.exception("Failed to deliver tell for %s", nick)
                _save_pending()
                return
            entries.pop(0)

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
    payload = {
        nick: [entry.to_dict() for entry in entries if isinstance(entry, TellEntry)]
        for nick, entries in state.pending.items()
        if entries
    }

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
