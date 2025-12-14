"""Reminder plugin triggered by `.remindme`.

Allows users to set reminders for relative time durations.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_NAME = "reminders.json"
DEFAULT_MAX_PER_USER = 10
MAX_DURATION_SECONDS = 365 * 24 * 60 * 60 * 2  # 2 years limit

CONFIG_DEFAULTS = {
    "plugins": {
        "remindme": {
            "enabled": True,
            "storage_path": DEFAULT_STORAGE_NAME,
            "max_reminders_per_user": DEFAULT_MAX_PER_USER,
            "triggers": ["remindme", "remind"],
        }
    }
}


@dataclass
class Reminder:
    user: str
    channel: str
    message: str
    created_at: float
    trigger_at: float
    id: str = field(default_factory=lambda: str(time.time_ns()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user": self.user,
            "channel": self.channel,
            "message": self.message,
            "created_at": self.created_at,
            "trigger_at": self.trigger_at,
            "id": self.id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["Reminder"]:
        try:
            return cls(
                user=str(data["user"]),
                channel=str(data["channel"]),
                message=str(data["message"]),
                created_at=float(data["created_at"]),
                trigger_at=float(data["trigger_at"]),
                id=str(data.get("id", str(time.time_ns()))),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class ReminderSettings:
    storage_path: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / DEFAULT_STORAGE_NAME
    )
    max_reminders_per_user: int = DEFAULT_MAX_PER_USER
    triggers: List[str] = field(
        default_factory=lambda: list(CONFIG_DEFAULTS["plugins"]["remindme"]["triggers"])
    )


@dataclass
class PluginState:
    settings: ReminderSettings
    reminders: List[Reminder] = field(default_factory=list)
    active_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)


state: Optional[PluginState] = None


def on_load(bot) -> None:
    global state
    settings = _settings_from_config(bot)
    reminders = _load_reminders(settings.storage_path)
    state = PluginState(settings=settings, reminders=reminders)
    
    _schedule_loaded_reminders(bot)
    
    triggers = ", ".join(f"{getattr(bot, 'prefix', '.')}{t}" for t in settings.triggers)
    logger.info(
        "Remindme plugin loaded. Storage: %s. Triggers: %s. Active reminders: %d",
        settings.storage_path,
        triggers,
        len(reminders),
    )


def on_unload(bot) -> None:
    global state
    if state:
        for task in state.active_tasks.values():
            if not task.done():
                task.cancel()
        _save_reminders()
        state = None
    logger.info("Remindme plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    global state
    if state is None:
        return

    prefix = getattr(bot, "prefix", ".")
    if not message.startswith(prefix):
        return

    parts = message[len(prefix) :].strip().split(maxsplit=2)
    if not parts:
        return

    trigger = parts[0].lower()
    if trigger not in state.settings.triggers:
        return

    if len(parts) < 3:
        pass_msg = "Format: <duration> <message>"
        asyncio.create_task(bot.privmsg(channel, f"Usage: {prefix}{trigger} {pass_msg} (e.g. 1h30m Check oven)"))
        return

    first_arg = parts[1]
    
    if first_arg.lower() == "at":
        # Absolute time handling
        if len(parts) < 3:
            asyncio.create_task(bot.privmsg(channel, "correct format is YYYY-MM-DD HH:MM"))
            return

        at_content = parts[2].strip()
        delta, reminder_msg, error = _parse_absolute(at_content)
        
        if error:
            asyncio.create_task(bot.privmsg(channel, error))
            return
            
    else:
        # Relative duration handling
        duration_str = first_arg
        reminder_msg = parts[2]
        
        try:
            delta = _parse_duration(duration_str)
        except Exception as e:
            logger.exception("Duration parse failed for input: %r", duration_str)
            asyncio.create_task(bot.privmsg(channel, f"Error parsing duration: {e}"))
            return

    if delta is None:
        asyncio.create_task(bot.privmsg(channel, f"Invalid duration: {first_arg}. Use 1h, 30m, 1d, etc."))
        return

    if delta.total_seconds() > MAX_DURATION_SECONDS:
        asyncio.create_task(bot.privmsg(channel, "That reminder is too far in the future."))
        return
        
    if delta.total_seconds() < 1:
        asyncio.create_task(bot.privmsg(channel, "Reminder must be at least 1 second in the future."))
        return

    nick = user.split("!", 1)[0]
    user_reminders = [r for r in state.reminders if r.user == nick]
    if len(user_reminders) >= state.settings.max_reminders_per_user:
        asyncio.create_task(bot.privmsg(channel, f"You have reached the limit of {state.settings.max_reminders_per_user} active reminders."))
        return

    now = time.time()
    trigger_at = now + delta.total_seconds()
    
    reminder = Reminder(
        user=nick,
        channel=channel,
        message=reminder_msg,
        created_at=now,
        trigger_at=trigger_at
    )
    
    state.reminders.append(reminder)
    _save_reminders()
    _schedule_reminder(bot, reminder)

    readable_time = _format_delta(delta)
    asyncio.create_task(bot.privmsg(channel, f"I will remind you in {readable_time}: {reminder_msg}"))


def _schedule_loaded_reminders(bot):
    if not state:
        return
    
    now = time.time()
    # Sort so we process overdue first? Order doesn't matter much for async scheduling
    for reminder in state.reminders:
        _schedule_reminder(bot, reminder)


def _schedule_reminder(bot, reminder: Reminder):
    if not state:
        return

    # Cancel existing task if any (shouldn't happen usually unless re-scheduling)
    if reminder.id in state.active_tasks:
        state.active_tasks[reminder.id].cancel()

    task = asyncio.create_task(_reminder_coro(bot, reminder))
    state.active_tasks[reminder.id] = task
    task.add_done_callback(lambda t: _cleanup_task(reminder.id))


def _cleanup_task(reminder_id: str):
    if state and reminder_id in state.active_tasks:
        del state.active_tasks[reminder_id]


async def _reminder_coro(bot, reminder: Reminder):
    delay = reminder.trigger_at - time.time()
    if delay > 0:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

    # Fire reminder
    if state:
        if reminder in state.reminders:
            state.reminders.remove(reminder)
            _save_reminders()

    # If it's very old, maybe add a note?
    # For now just send it.
    msg = f"{reminder.user}: Reminder! {reminder.message}"
    
    # Try sending to channel; if failed fallback might be needed but for now simple privmsg
    try:
        await bot.privmsg(reminder.channel, msg)
    except Exception:
        logger.exception("Failed to send reminder for %s", reminder.user)


def _parse_duration(s: str) -> Optional[timedelta]:
    """Parse duration string like 1h30m, 1d, 1y."""
    # Units map
    units = {
        "s": 1, "sec": 1, "seconds": 1, "second": 1,
        "m": 60, "min": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
        "w": 604800, "week": 604800, "weeks": 604800,
        "mo": 2628000, "month": 2628000, "months": 2628000, # Approx 30.4 days
        "y": 31536000, "year": 31536000, "years": 31536000, # 365 days
    }
    
    # Simple regex to find pairs of number + unit
    # Matches "1h", "30m", "10 s" (optional space? let's stick to user request "1sec/1min...")
    # User request example: "1sec/1min/1h/1d/1m/1y and so (for example 43min or 2w)"
    
    pattern = re.compile(r"(\d+)\s*([a-zA-Z]+)")
    matches = pattern.findall(s)
    
    if not matches:
        return None
        
    total_seconds = 0
    found_any = False
    
    for amount_str, unit_str in matches:
        unit = unit_str.lower()
        if unit not in units:
            continue
            
        amount = int(amount_str)
        total_seconds += amount * units[unit]
        found_any = True
        
    if not found_any:
        return None
        
    return timedelta(seconds=total_seconds)


def _format_delta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _settings_from_config(bot) -> ReminderSettings:
    config = getattr(bot, "config", {})
    plugins = config.get("plugins", {})
    
    # Safe access
    if not isinstance(plugins, dict):
        plugins = {}
    
    settings = plugins.get("remindme", {})
    if not isinstance(settings, dict):
        settings = {}
        
    default_path = Path(__file__).resolve().parent / DEFAULT_STORAGE_NAME
    raw_path = settings.get("storage_path")
    if raw_path:
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            path = (default_path.parent / path).resolve()
        storage_path = path
    else:
        storage_path = default_path
        
    max_reminders = settings.get("max_reminders_per_user", DEFAULT_MAX_PER_USER)
    
    conf_triggers = settings.get("triggers")
    if isinstance(conf_triggers, list):
        triggers = [str(t) for t in conf_triggers]
    else:
        triggers = list(CONFIG_DEFAULTS["plugins"]["remindme"]["triggers"])
        
    return ReminderSettings(
        storage_path=storage_path,
        max_reminders_per_user=max_reminders,
        triggers=triggers
    )


def _load_reminders(path: Path) -> List[Reminder]:
    if not path.exists():
        return []
    
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                return []
            
            loaded = []
            for item in data:
                r = Reminder.from_dict(item)
                if r:
                    loaded.append(r)
            return loaded
    except Exception:
        logger.warning("Failed to load reminders from %s", path, exc_info=True)
        return []


def _save_reminders() -> None:
    if not state:
        return
        
    try:
        path = state.settings.storage_path
        path.parent.mkdir(parents=True, exist_ok=True)
        
        data = [r.to_dict() for r in state.reminders]
        
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        logger.error("Failed to save reminders", exc_info=True)


def _parse_absolute(at_content: str) -> Tuple[Optional[timedelta], str, Optional[str]]:
    """Parse 'YYYY-MM-DD HH:MM <msg>' content.
    
    Returns (delta, message, error_msg).
    """
    parts = at_content.split(maxsplit=2)
    if len(parts) < 2:
        return None, "", "correct format is YYYY-MM-DD HH:MM"
        
    date_str = parts[0]
    time_str = parts[1]
    reminder_msg = parts[2] if len(parts) > 2 else "Reminder"
    
    dt_str = f"{date_str} {time_str}"
    try:
        target_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return None, "", "correct format is YYYY-MM-DD HH:MM"
        
    now_dt = datetime.now()
    delta = target_dt - now_dt
    
    return delta, reminder_msg, None
