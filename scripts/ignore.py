"""Plugin providing owner-only commands to ignore noisy users."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Iterable, Set

import yaml

logger = logging.getLogger(__name__)


CONFIG_DEFAULTS = {
    "plugins": {
        "ignore": {
            "enabled": True,
            "ignored_nicks": [],
        }
    }
}


STATE_KEY = "_ignore_plugin_state"


def on_load(bot) -> None:
    ignored = set(_load_ignored_from_config(bot))
    state = _get_state(bot)
    state["ignored"] = ignored
    bot.ignored_nicks = set(nick.lower() for nick in ignored)
    logger.info("ignore plugin loaded with %d ignored nick(s)", len(ignored))


def on_unload(bot) -> None:
    bot.__dict__.pop(STATE_KEY, None)
    bot.ignored_nicks = set()
    logger.info("ignore plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    prefix = getattr(bot, "prefix", ".")
    if not message.startswith(prefix):
        return

    command_body = message[len(prefix) :].strip()
    if not command_body:
        return

    parts = command_body.split(maxsplit=1)
    command = parts[0].lower()
    if command not in {"ignore", "unignore", "ignored"}:
        return

    if not _is_owner(bot, user):
        loop = asyncio.get_running_loop()
        loop.create_task(bot.privmsg(channel, "You do not have permission for that command."))
        return

    argument = parts[1].strip() if len(parts) > 1 else ""
    if command == "ignored":
        _handle_list(bot, channel)
        return

    if not argument:
        loop = asyncio.get_running_loop()
        loop.create_task(
            bot.privmsg(channel, f"Usage: {prefix}{command} <nick>")
        )
        return

    if command == "ignore":
        _handle_ignore(bot, channel, argument)
    elif command == "unignore":
        _handle_unignore(bot, channel, argument)


def _handle_ignore(bot, channel: str, nick: str) -> None:
    normalized = nick.strip().lower()
    if not normalized:
        loop = asyncio.get_running_loop()
        loop.create_task(bot.privmsg(channel, "Please provide a valid nickname."))
        return

    ignored = _get_ignored_set(bot)
    if normalized in ignored:
        loop = asyncio.get_running_loop()
        loop.create_task(bot.privmsg(channel, f"Already ignoring {nick}."))
        return

    ignored.add(normalized)
    bot.ignored_nicks = set(ignored)
    _persist_ignored(bot, ignored)
    loop = asyncio.get_running_loop()
    loop.create_task(bot.privmsg(channel, f"Now ignoring {nick}."))


def _handle_unignore(bot, channel: str, nick: str) -> None:
    normalized = nick.strip().lower()
    if not normalized:
        loop = asyncio.get_running_loop()
        loop.create_task(bot.privmsg(channel, "Please provide a valid nickname."))
        return

    ignored = _get_ignored_set(bot)
    if normalized not in ignored:
        loop = asyncio.get_running_loop()
        loop.create_task(bot.privmsg(channel, f"{nick} was not being ignored."))
        return

    ignored.remove(normalized)
    bot.ignored_nicks = set(ignored)
    _persist_ignored(bot, ignored)
    loop = asyncio.get_running_loop()
    loop.create_task(bot.privmsg(channel, f"No longer ignoring {nick}."))


def _handle_list(bot, channel: str) -> None:
    ignored = sorted(_get_ignored_set(bot))
    if not ignored:
        message = "No nicknames are currently ignored."
    else:
        message = "Ignored nicknames: " + ", ".join(ignored)
    loop = asyncio.get_running_loop()
    loop.create_task(bot.privmsg(channel, message))


def _load_ignored_from_config(bot) -> Iterable[str]:
    plugins_section = getattr(bot, "config", {}).get("plugins", {})
    if not isinstance(plugins_section, dict):
        return []

    ignore_section = plugins_section.get("ignore", {})
    if not isinstance(ignore_section, dict):
        return []

    ignored = ignore_section.get("ignored_nicks", [])
    if not isinstance(ignored, list):
        return []

    result = []
    for entry in ignored:
        if isinstance(entry, str) and entry.strip():
            result.append(entry.strip().lower())
    return result


def _persist_ignored(bot, ignored: Set[str]) -> None:
    _update_runtime_config(bot, ignored)
    config_path = getattr(bot.plugin_manager, "get_config_path", lambda: None)()
    if not config_path:
        return

    try:
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        else:
            data = {}
    except Exception:
        logger.warning("Failed to read config file when persisting ignore list", exc_info=True)
        return

    if not isinstance(data, dict):
        data = {}

    plugins_section = data.setdefault("plugins", {})
    if not isinstance(plugins_section, dict):
        plugins_section = {}
        data["plugins"] = plugins_section

    ignore_section = plugins_section.setdefault("ignore", {})
    if not isinstance(ignore_section, dict):
        ignore_section = {}
        plugins_section["ignore"] = ignore_section

    ignore_section["ignored_nicks"] = sorted(ignored)

    try:
        with config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
    except Exception:
        logger.warning("Failed to write ignore list to config file", exc_info=True)


def _update_runtime_config(bot, ignored: Set[str]) -> None:
    if not hasattr(bot, "config") or not isinstance(bot.config, dict):
        return

    plugins_section = bot.config.setdefault("plugins", {})
    if not isinstance(plugins_section, dict):
        return

    ignore_section = plugins_section.setdefault("ignore", {})
    if not isinstance(ignore_section, dict):
        return

    ignore_section["ignored_nicks"] = sorted(ignored)


def _get_state(bot) -> Dict[str, object]:
    state = getattr(bot, STATE_KEY, None)
    if isinstance(state, dict):
        return state
    state = {"ignored": set()}
    setattr(bot, STATE_KEY, state)
    return state


def _get_ignored_set(bot) -> Set[str]:
    state = _get_state(bot)
    ignored = state.get("ignored")
    if isinstance(ignored, set):
        return ignored
    ignored_set = set()
    state["ignored"] = ignored_set
    return ignored_set


def _is_owner(bot, user: str) -> bool:
    checker = getattr(bot, "_has_owner_access", None)
    if callable(checker):
        try:
            return bool(checker(user))
        except Exception:
            logger.exception("ignore plugin failed to validate owner access")
            return False
    nick = user.split("!", 1)[0]
    owner_nicks = getattr(bot, "owner_nicks", set())
    return nick.lower() in {name.lower() for name in owner_nicks}

