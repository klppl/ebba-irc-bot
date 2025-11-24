"""Channel message logging plugin (stores messages in SQLite database)."""

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_NAME = "irc_logs.db"
DEFAULT_MAX_MESSAGE_LENGTH = 2000

CONFIG_DEFAULTS = {
    "plugins": {
        "log": {
            "enabled": True,
            "storage_path": DEFAULT_STORAGE_NAME,
            "channels": [],
            "max_message_length": DEFAULT_MAX_MESSAGE_LENGTH,
            "log_joins_parts": False,
            "log_actions": True,
        }
    }
}


@dataclass
class LogSettings:
    storage_path: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / DEFAULT_STORAGE_NAME
    )
    channels: Set[str] = field(default_factory=set)
    max_message_length: int = DEFAULT_MAX_MESSAGE_LENGTH
    log_joins_parts: bool = False
    log_actions: bool = True


@dataclass
class LogState:
    settings: LogSettings
    db: Optional[sqlite3.Connection] = None
    _write_queue: asyncio.Queue = field(default_factory=asyncio.Queue)


state: Optional[LogState] = None


def on_load(bot) -> None:
    global state
    settings = _settings_from_config(bot)
    db = _init_database(settings.storage_path)
    state = LogState(settings=settings, db=db)
    _start_writer_task()
    channel_list = ", ".join(sorted(settings.channels)) if settings.channels else "none"
    logger.info(
        "log plugin loaded from %s; logging channels: %s",
        __file__,
        channel_list,
    )
    # Register commands
    try:
        bot.plugin_manager.register_command(
            "log",
            "logsearch",
            _handle_log_search,
            aliases=["logs"],
            help_text="Search logged messages (owner only)",
        )
        bot.plugin_manager.register_command(
            "log",
            "log",
            _handle_log_command,
            aliases=[],
            help_text="Manage channel logging: enable/disable/list (owner only)",
        )
    except Exception:
        logger.warning("Failed to register log commands", exc_info=True)


def on_unload(bot) -> None:
    global state
    if state:
        _stop_writer_task()
        if state.db:
            state.db.close()
        state = None
    logger.info("log plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    global state
    if state is None:
        return

    # Check if we should log this channel
    if not _should_log_channel(channel):
        return

    # Extract nick from user prefix
    nick = _nick_from_prefix(user)
    if not nick:
        return

    # Truncate message if needed
    if len(message) > state.settings.max_message_length:
        message = message[: state.settings.max_message_length] + "..."

    # Queue for async write
    _queue_log_entry("message", nick, user, channel, message)


def on_join(bot, user: str, channel: str) -> None:
    global state
    if state is None or not state.settings.log_joins_parts:
        return

    if not _should_log_channel(channel):
        return

    nick = _nick_from_prefix(user)
    if not nick:
        return

    _queue_log_entry("join", nick, user, channel, "")


def on_part(bot, user: str, channel: str) -> None:
    global state
    if state is None or not state.settings.log_joins_parts:
        return

    if not _should_log_channel(channel):
        return

    nick = _nick_from_prefix(user)
    if not nick:
        return

    _queue_log_entry("part", nick, user, channel, "")


def _should_log_channel(channel: str) -> bool:
    """Check if a channel should be logged."""
    if state is None:
        return False
    if not state.settings.channels:
        return False
    # Case-insensitive match
    return channel.lower() in {c.lower() for c in state.settings.channels}


def _nick_from_prefix(prefix: str) -> str:
    """Extract nick from IRC user prefix."""
    if not prefix:
        return ""
    return prefix.split("!", 1)[0]


def _queue_log_entry(event_type: str, nick: str, user: str, channel: str, message: str) -> None:
    """Queue a log entry for async writing."""
    if state is None:
        return
    try:
        state._write_queue.put_nowait((event_type, nick, user, channel, message, time.time()))
    except asyncio.QueueFull:
        logger.warning("Log write queue full; dropping entry")


def _init_database(db_path: Path) -> sqlite3.Connection:
    """Initialize SQLite database with messages table."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            event_type TEXT NOT NULL,
            nick TEXT NOT NULL,
            user TEXT NOT NULL,
            channel TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_channel ON messages(channel)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_nick ON messages(nick)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_channel_timestamp ON messages(channel, timestamp)
    """)
    conn.commit()
    return conn


def _settings_from_config(bot) -> LogSettings:
    """Load settings from bot config."""
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else {}
    section: Dict[str, Any] = {}
    if isinstance(plugins_section, dict):
        candidate = plugins_section.get("log")
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

    channels_raw = section.get("channels", [])
    channels: Set[str] = set()
    if isinstance(channels_raw, list):
        for item in channels_raw:
            if isinstance(item, str) and item.strip():
                channels.add(item.strip())
    elif isinstance(channels_raw, str):
        # Support comma-separated string
        for item in channels_raw.split(","):
            if item.strip():
                channels.add(item.strip())

    max_length = section.get("max_message_length", DEFAULT_MAX_MESSAGE_LENGTH)
    try:
        max_length = max(1, int(max_length))
    except (TypeError, ValueError):
        max_length = DEFAULT_MAX_MESSAGE_LENGTH

    log_joins_parts = bool(section.get("log_joins_parts", False))
    log_actions = bool(section.get("log_actions", True))

    return LogSettings(
        storage_path=storage_path,
        channels=channels,
        max_message_length=max_length,
        log_joins_parts=log_joins_parts,
        log_actions=log_actions,
    )


_writer_task: Optional[asyncio.Task] = None


def _start_writer_task() -> None:
    """Start the async writer task."""
    global _writer_task
    if _writer_task is None or _writer_task.done():
        loop = asyncio.get_event_loop()
        _writer_task = loop.create_task(_writer_loop(), name="log-writer")


def _stop_writer_task() -> None:
    """Stop the async writer task."""
    global _writer_task
    if _writer_task and not _writer_task.done():
        _writer_task.cancel()
        try:
            asyncio.get_event_loop().run_until_complete(_writer_task)
        except asyncio.CancelledError:
            pass
    _writer_task = None


async def _writer_loop() -> None:
    """Background task that writes queued log entries to database."""
    if state is None or state.db is None:
        return

    batch: List[tuple] = []
    batch_size = 50
    flush_interval = 5.0

    while True:
        try:
            # Wait for entry with timeout for periodic flush
            try:
                entry = await asyncio.wait_for(state._write_queue.get(), timeout=flush_interval)
                batch.append(entry)
            except asyncio.TimeoutError:
                # Timeout - flush if we have entries
                if batch:
                    _flush_batch(batch)
                    batch.clear()
                continue

            # Flush when batch is full
            if len(batch) >= batch_size:
                _flush_batch(batch)
                batch.clear()

        except asyncio.CancelledError:
            # Flush remaining entries on shutdown
            if batch:
                _flush_batch(batch)
            break
        except Exception:
            logger.exception("Error in log writer loop")


def _flush_batch(batch: List[tuple]) -> None:
    """Write a batch of log entries to the database."""
    if state is None or state.db is None or not batch:
        return

    try:
        state.db.executemany(
            """
            INSERT INTO messages (event_type, nick, user, channel, message, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            batch,
        )
        state.db.commit()
    except Exception:
        logger.exception("Failed to write log batch to database")
        try:
            state.db.rollback()
        except Exception:
            pass


async def _handle_log_search(bot, user: str, channel: str, args: List[str], is_private: bool) -> None:
    """Handle log search command (owner only)."""
    global state
    if state is None or state.db is None:
        await bot.privmsg(channel, "Log plugin not initialized.")
        return

    # Check owner access (user is already the full IRC prefix: nick!ident@host)
    if not bot._has_owner_access(user):
        await bot.privmsg(channel, "You do not have permission for that command.")
        return

    if not args:
        await bot.privmsg(
            channel,
            f"Usage: {bot.prefix}logsearch <channel> [nick] [limit] | "
            f"Example: {bot.prefix}logsearch #channel alice 10",
        )
        return

    target_channel = args[0]
    target_nick = args[1].lower() if len(args) > 1 else None
    limit = 10
    if len(args) > 2:
        try:
            limit = max(1, min(50, int(args[2])))
        except (TypeError, ValueError):
            pass

    try:
        results = _search_logs(target_channel, target_nick, limit)
        if not results:
            await bot.privmsg(channel, f"No log entries found for {target_channel}.")
            return

        # Send results (may need to split if too long)
        for entry in results:
            event_type, nick, msg, ts = entry
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            if event_type == "message":
                preview = msg[:100] + "..." if len(msg) > 100 else msg
                await bot.privmsg(
                    channel,
                    f"[{time_str}] {nick} in {target_channel}: {preview}",
                )
            else:
                await bot.privmsg(
                    channel,
                    f"[{time_str}] {nick} {event_type} {target_channel}",
                )
            await asyncio.sleep(0.3)  # Rate limit output

    except Exception as exc:
        logger.exception("Error searching logs")
        await bot.privmsg(channel, f"Error searching logs: {exc}")


def _search_logs(channel: str, nick: Optional[str] = None, limit: int = 10) -> List[tuple]:
    """Search log entries from database."""
    if state is None or state.db is None:
        return []

    try:
        # Case-insensitive channel search
        if nick:
            cursor = state.db.execute(
                """
                SELECT event_type, nick, message, timestamp
                FROM messages
                WHERE LOWER(channel) = LOWER(?) AND LOWER(nick) = LOWER(?)
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (channel, nick, limit),
            )
        else:
            cursor = state.db.execute(
                """
                SELECT event_type, nick, message, timestamp
                FROM messages
                WHERE LOWER(channel) = LOWER(?)
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (channel, limit),
            )
        return cursor.fetchall()
    except Exception:
        logger.exception("Error searching logs")
        return []


async def _handle_log_command(bot, user: str, channel: str, args: List[str], is_private: bool) -> None:
    """Handle log management commands: enable/disable/list (owner only)."""
    global state
    if state is None:
        await bot.privmsg(channel, "Log plugin not initialized.")
        return

    # Check owner access (user is already the full IRC prefix: nick!ident@host)
    # Extract nick for logging
    nick = user.split("!", 1)[0] if "!" in user else user
    logger.info("Log command called by %s (user=%s)", nick, user)
    
    has_access = bot._has_owner_access(user)
    logger.info("Log command access check: user=%s, has_access=%s", user, has_access)
    
    if not has_access:
        # Try to get more info for debugging
        try:
            nick_check, ident_host_check = bot._extract_owner_identity(user)
            owner_records = getattr(bot, "_owner_records", {})
            record = owner_records.get(nick_check.lower() if nick_check else "")
            hosts_info = list(record.hosts) if record else []
            logger.warning(
                "Log command denied: user=%s, nick=%s, ident_host=%s, stored_hosts=%s",
                user,
                nick_check,
                ident_host_check,
                hosts_info,
            )
        except Exception as e:
            logger.exception("Error getting debug info: %s", e)
        await bot.privmsg(channel, "You do not have permission for that command.")
        return

    if not args:
        await bot.privmsg(
            channel,
            f"Usage: {bot.prefix}log <enable|disable|list> [channel] | "
            f"Example: {bot.prefix}log enable #channel",
        )
        return

    subcommand = args[0].lower()

    if subcommand == "list":
        channels = sorted(state.settings.channels)
        if channels:
            await bot.privmsg(
                channel,
                f"Currently logging channels: {', '.join(channels)}",
            )
        else:
            await bot.privmsg(channel, "No channels are currently being logged.")
        return

    if subcommand in ("enable", "disable"):
        if len(args) < 2:
            await bot.privmsg(
                channel,
                f"Usage: {bot.prefix}log {subcommand} <channel> | "
                f"Example: {bot.prefix}log {subcommand} #channel",
            )
            return

        target_channel = args[1].strip()
        if not target_channel.startswith("#"):
            target_channel = f"#{target_channel}"

        if subcommand == "enable":
            # Check if already enabled (case-insensitive)
            if any(c.lower() == target_channel.lower() for c in state.settings.channels):
                await bot.privmsg(channel, f"Channel {target_channel} is already being logged.")
                return

            state.settings.channels.add(target_channel)
            await bot.privmsg(channel, f"Enabled logging for {target_channel}.")
            logger.info("Logging enabled for channel %s by %s", target_channel, _nick_from_prefix(user))

        else:  # disable
            # Find and remove (case-insensitive)
            to_remove = None
            for c in state.settings.channels:
                if c.lower() == target_channel.lower():
                    to_remove = c
                    break

            if to_remove is None:
                await bot.privmsg(channel, f"Channel {target_channel} is not being logged.")
                return

            state.settings.channels.remove(to_remove)
            await bot.privmsg(channel, f"Disabled logging for {target_channel}.")
            logger.info("Logging disabled for channel %s by %s", target_channel, _nick_from_prefix(user))

        # Persist to config file
        _persist_channels_to_config(bot)
        return

    await bot.privmsg(
        channel,
        f"Unknown subcommand '{subcommand}'. Use: enable, disable, or list",
    )


def _persist_channels_to_config(bot) -> None:
    """Persist current channel list to config.yaml."""
    if state is None:
        return

    config_path = bot.plugin_manager.get_config_path()
    if not config_path:
        return

    try:
        from core.utils import file_lock, load_yaml_file, atomic_write_yaml

        lock_path = config_path.with_suffix(config_path.suffix + ".lock")
        with file_lock(lock_path):
            data = load_yaml_file(config_path)

            plugins_section = data.setdefault("plugins", {})
            if not isinstance(plugins_section, dict):
                plugins_section = {}
                data["plugins"] = plugins_section

            log_section = plugins_section.setdefault("log", {})
            if not isinstance(log_section, dict):
                log_section = {}
                plugins_section["log"] = log_section

            # Update channels list
            log_section["channels"] = sorted(state.settings.channels)

            atomic_write_yaml(config_path, data)
            logger.debug("Persisted log channels to config file")

    except Exception:
        logger.exception("Failed to persist log channels to config")

