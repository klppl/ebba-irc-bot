import asyncio
import contextlib
import logging
import signal
import ssl
from asyncio import StreamReader, StreamWriter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import yaml

from .plugin_manager import PluginManager
from .utils import (
    AsyncRateLimiter,
    IRCMessage,
    atomic_write_yaml,
    file_lock,
    load_yaml_file,
    parse_irc_message,
)


@dataclass
class OwnerRecord:
    nick: str
    password: Optional[str] = None
    hosts: Set[str] = field(default_factory=set)

    def has_host(self, ident_host: str) -> bool:
        candidate = ident_host.lower()
        # Direct match
        if any(existing.lower() == candidate for existing in self.hosts):
            return True
        # Try matching with/without ~ prefix (some IRC servers use ~ for no ident)
        if "@" in candidate:
            ident_part, host_part = candidate.split("@", 1)
            # Try without ~ prefix
            if ident_part.startswith("~"):
                alt_candidate = f"{ident_part[1:]}@{host_part}"
                if any(existing.lower() == alt_candidate for existing in self.hosts):
                    return True
            # Try with ~ prefix
            else:
                alt_candidate = f"~{ident_part}@{host_part}"
                if any(existing.lower() == alt_candidate for existing in self.hosts):
                    return True
        return False

    def add_host(self, ident_host: str) -> bool:
        ident_host = ident_host.strip()
        if not ident_host:
            return False
        if self.has_host(ident_host):
            return False
        self.hosts.add(ident_host)
        return True


class IRCClient:
    """Asyncio based IRC client with plugin dispatch."""

    def __init__(self, config: Dict[str, Any], plugin_manager: PluginManager) -> None:
        self.config = config
        self.plugin_manager = plugin_manager
        self.logger = logging.getLogger("IRCClient")
        self.server = str(config["server"])
        self.port = int(config["port"])
        self.use_tls = bool(config.get("use_tls", False))
        self.nickname = str(config["nickname"])
        self.username = str(config["username"])
        self.realname = str(config["realname"])
        self.channels = list(config.get("channels", []))
        self.prefix = str(config.get("prefix", "."))
        self._owner_records = self._load_owner_records(config)
        self.owner_nicks = {record.nick for record in self._owner_records.values()}
        self.reconnect_delay = int(config.get("reconnect_delay_secs", 5))
        self.request_timeout = int(config.get("request_timeout_secs", 10))
        self.max_backoff = int(config.get("max_reconnect_delay_secs", 60))
        self.join_delay_secs = float(config.get("join_delay_secs", 0.4))
        rate_count = int(config.get("privmsg_rate_count", 4))
        rate_window = float(config.get("privmsg_rate_window_secs", 2.0))
        self._rate_limiter = AsyncRateLimiter(rate_count, rate_window)
        per_target_count = int(config.get("per_target_rate_count", 2))
        per_target_window = float(config.get("per_target_rate_window_secs", 2.0))
        self._per_target_count = per_target_count
        self._per_target_window = per_target_window
        self._target_rate_limiters: Dict[str, AsyncRateLimiter] = {}
        self.ignored_nicks: Set[str] = set()

        self.reader: Optional[StreamReader] = None
        self.writer: Optional[StreamWriter] = None
        self._send_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._writer_task: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._signals_registered = False
        self._last_connect_time: Optional[float] = None
        self._last_disconnect_time: Optional[float] = None

    async def start(self) -> None:
        """Attempt to connect and stay connected with exponential backoff."""
        backoff = max(self.reconnect_delay, 1)
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
                backoff = max(self.reconnect_delay, 1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.exception("Connection error: %s", exc)
                await self._cleanup_connection()
                if self._stop_event.is_set():
                    break
                self.logger.info("Reconnecting in %s seconds", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)

    async def stop(self) -> None:
        self._stop_event.set()
        await self._cleanup_connection()

    async def _connect_once(self) -> None:
        ssl_context = ssl.create_default_context() if self.use_tls else None
        self.logger.info("Connecting to %s:%s (TLS=%s)", self.server, self.port, self.use_tls)
        self.reader, self.writer = await asyncio.open_connection(
            self.server,
            self.port,
            ssl=ssl_context,
            server_hostname=self.server if self.use_tls else None,
        )
        self._last_connect_time = asyncio.get_running_loop().time()
        await self._register()

        loop = asyncio.get_running_loop()
        if not self._signals_registered:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.stop()))
                except NotImplementedError:
                    # Not available on Windows event loop
                    pass
            self._signals_registered = True

        self._writer_task = asyncio.create_task(self._writer_loop(), name="irc-writer")
        self._reader_task = asyncio.create_task(self._reader_loop(), name="irc-reader")

        await self._reader_task
        await self._cleanup_connection()

    async def _register(self) -> None:
        await self.send_raw(f"NICK {self.nickname}")
        await self.send_raw(f"USER {self.username} 0 * :{self.realname}")

    async def _cleanup_connection(self) -> None:
        if self._writer_task:
            self._writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer_task
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task

        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

        self.reader = None
        self.writer = None
        self._writer_task = None
        self._reader_task = None
        self._send_queue = asyncio.Queue()
        self._last_disconnect_time = asyncio.get_running_loop().time()

    async def _writer_loop(self) -> None:
        assert self.writer is not None
        while not self._stop_event.is_set():
            message = await self._send_queue.get()
            self.writer.write(f"{message}\r\n".encode("utf-8"))
            try:
                await self.writer.drain()
            except ConnectionError:
                self.logger.warning("Connection lost during write")
                break

    async def _reader_loop(self) -> None:
        assert self.reader is not None
        while not self._stop_event.is_set():
            raw = await self.reader.readline()
            if not raw:
                self.logger.warning("Server closed the connection")
                break
            line = raw.decode("utf-8", errors="ignore").strip("\r\n")
            if not line:
                continue
            self.logger.debug("< %s", line)
            message = parse_irc_message(line)
            await self._handle_message(message)

    async def send_raw(self, message: str) -> None:
        self.logger.debug("> %s", message)
        try:
            self._send_queue.put_nowait(message)
        except asyncio.QueueFull:
            self.logger.warning("Send queue full; dropping message: %s", message[:200])
            return

    async def privmsg(self, target: str, text: str) -> None:
        await self._acquire_target_rate(target)
        await self._rate_limiter.acquire()
        await self.send_raw(f"PRIVMSG {target} :{text}")

    async def join(self, channel: str) -> None:
        await self.send_raw(f"JOIN {channel}")
        self._remember_channel(channel)

    async def part(self, channel: str, reason: str = "") -> None:
        if reason:
            await self.send_raw(f"PART {channel} :{reason}")
        else:
            await self.send_raw(f"PART {channel}")
        self._forget_channel(channel)

    async def _handle_message(self, message: IRCMessage) -> None:
        if message.command == "PING":
            payload = message.trailing or "server"
            await self.send_raw(f"PONG :{payload}")
            return

        if message.command == "001":
            await self._join_initial_channels()
            return

        if message.command == "433":
            self.logger.error("Nickname %s already in use", self.nickname)
            self.nickname = f"{self.nickname}_"
            await self.send_raw(f"NICK {self.nickname}")
            return

        if message.command == "JOIN":
            await self._handle_join(message)
            return

        if message.command == "PART":
            await self._handle_part(message)
            return

        if message.command == "PRIVMSG":
            await self._handle_privmsg(message)
            return

        if message.command == "NICK":
            await self._handle_nick(message)
            return

        if message.command == "KICK":
            await self._handle_kick(message)
            return

        if message.command == "QUIT":
            await self._handle_quit(message)

    async def _join_initial_channels(self) -> None:
        first = True
        for channel in self.channels:
            if not first and self.join_delay_secs > 0:
                try:
                    await asyncio.sleep(self.join_delay_secs)
                except Exception:
                    pass
            await self.join(channel)
            first = False

    def refresh_runtime_settings(self) -> None:
        self.prefix = str(self.config.get("prefix", self.prefix))
        self.reconnect_delay = int(self.config.get("reconnect_delay_secs", self.reconnect_delay))
        self.request_timeout = int(self.config.get("request_timeout_secs", self.request_timeout))
        self.max_backoff = int(self.config.get("max_reconnect_delay_secs", self.max_backoff))
        self.join_delay_secs = float(self.config.get("join_delay_secs", self.join_delay_secs))
        self._per_target_count = int(self.config.get("per_target_rate_count", self._per_target_count))
        self._per_target_window = float(
            self.config.get("per_target_rate_window_secs", self._per_target_window)
        )

        channels = self.config.get("channels")
        if isinstance(channels, list):
            self.channels = list(channels)

        # Reset per-target limiters with updated settings
        self._target_rate_limiters.clear()

    async def _handle_status(self, reply_target: str) -> None:
        now = asyncio.get_running_loop().time()
        def _fmt(ts: Optional[float]) -> str:
            return f"{int(now - ts)}s ago" if ts is not None else "n/a"

        queue_size = getattr(self._send_queue, "qsize", lambda: 0)()
        enabled, disabled = self.plugin_manager.list_plugin_status()
        parts = [
            f"channels={len(self.channels)}",
            f"connected={_fmt(self._last_connect_time)}",
            f"disconnected={_fmt(self._last_disconnect_time)}",
            f"queue={queue_size}",
            f"plugins=+{len(enabled)}/-{len(disabled)}",
        ]
        await self.privmsg(reply_target, "Status: " + " | ".join(parts))

    async def _acquire_target_rate(self, target: str) -> None:
        limiter = self._target_rate_limiters.get(target.lower())
        if limiter is None:
            limiter = AsyncRateLimiter(self._per_target_count, self._per_target_window)
            self._target_rate_limiters[target.lower()] = limiter
        await limiter.acquire()

    async def _handle_help(self, reply_target: str, args: List[str]) -> None:
        specs = self.plugin_manager.list_commands()
        if not specs:
            await self.privmsg(reply_target, "No plugin commands registered.")
            return

        # Basic summary help; could be expanded to detailed command help with args[0]
        parts = []
        for spec in specs:
            alias_part = ""
            aliases = sorted(a for a in spec.aliases if a != spec.name)
            if aliases:
                alias_part = f" (aliases: {', '.join(aliases)})"
            help_text = spec.help_text or "no description"
            parts.append(f"{self.prefix}{spec.name}{alias_part} - {help_text}")

        await self.privmsg(reply_target, "Commands: " + " | ".join(parts))

    async def _handle_privmsg(self, message: IRCMessage) -> None:
        if message.prefix is None or message.trailing is None:
            return
        user = message.prefix
        target = message.params[0] if message.params else ""
        text = message.trailing
        nick = user.split("!", 1)[0]
        is_private = target.lower() == self.nickname.lower()
        channel = nick if is_private else target
        if nick.lower() in getattr(self, "ignored_nicks", set()):
            self.logger.debug("Ignoring message from %s due to ignore list", nick)
            return
        await self._handle_builtin_commands(nick, user, channel, text, is_private)
        self.plugin_manager.dispatch_message(self, user, channel, text)

    async def _handle_join(self, message: IRCMessage) -> None:
        prefix = message.prefix
        if prefix is None:
            return

        channel = ""
        if message.trailing:
            channel = message.trailing
        elif message.params:
            channel = message.params[0]

        if not channel:
            return

        nick = prefix.split("!", 1)[0]
        if nick.lower() == self.nickname.lower():
            self._remember_channel(channel)

        self.plugin_manager.dispatch_join(self, prefix, channel)

    async def _handle_part(self, message: IRCMessage) -> None:
        prefix = message.prefix
        if prefix is None:
            return

        if not message.params:
            return
        channel = message.params[0].strip()
        if not channel:
            return

        nick = prefix.split("!", 1)[0]
        if nick.lower() == self.nickname.lower():
            self._forget_channel(channel)

        self.plugin_manager.dispatch_part(self, prefix, channel)

    async def _handle_nick(self, message: IRCMessage) -> None:
        prefix = message.prefix
        if prefix is None:
            return

        new_nick = ""
        if message.trailing:
            new_nick = message.trailing
        elif message.params:
            new_nick = message.params[0]

        if not new_nick:
            return

        old_nick = prefix.split("!", 1)[0]
        if old_nick.lower() == self.nickname.lower():
            self.nickname = new_nick
            self.logger.info("My nickname changed from %s to %s", old_nick, new_nick)

        self.plugin_manager.dispatch_nick(self, prefix, new_nick)

    async def _handle_kick(self, message: IRCMessage) -> None:
        # KICK <channel> <target> :<reason>
        prefix = message.prefix
        if prefix is None or len(message.params) < 2:
            return

        channel = message.params[0]
        target = message.params[1]
        reason = message.trailing or ""

        if target.lower() == self.nickname.lower():
            self.logger.warning("I was kicked from %s by %s: %s", channel, prefix, reason)
            self._forget_channel(channel)
            # Optional: auto-rejoin logic could go here

        self.plugin_manager.dispatch_kick(self, channel, target, prefix, reason)

    async def _handle_quit(self, message: IRCMessage) -> None:
        prefix = message.prefix
        if prefix is None:
            return

        reason = message.trailing or ""
        self.plugin_manager.dispatch_quit(self, prefix, reason)

    def _remember_channel(self, channel: str) -> None:
        channel = channel.strip()
        if not channel:
            return

        # Update in-memory list (case-insensitive dedupe).
        if not any(existing.lower() == channel.lower() for existing in self.channels):
            self.channels.append(channel)

        channels_list = self.config.setdefault("channels", [])
        if isinstance(channels_list, list):
            if not any(existing.lower() == channel.lower() for existing in channels_list):
                channels_list.append(channel)
        else:
            self.config["channels"] = [channel]

        self._persist_channels()

    def _forget_channel(self, channel: str) -> None:
        channel = channel.strip()
        if not channel:
            return

        target_lower = channel.lower()
        self.channels = [
            existing
            for existing in self.channels
            if isinstance(existing, str) and existing.lower() != target_lower
        ]

        channels_list = self.config.get("channels")
        if isinstance(channels_list, list):
            self.config["channels"] = [
                existing
                for existing in channels_list
                if isinstance(existing, str) and existing.lower() != target_lower
            ]
        else:
            self.config["channels"] = list(self.channels)

        self._persist_channels()

    def _persist_channels(self) -> None:
        config_path = self.plugin_manager.get_config_path()
        if not config_path:
            return

        normalized_channels = []
        seen_lower = set()
        for channel in self.channels:
            if not isinstance(channel, str):
                continue
            channel_name = channel.strip()
            if not channel_name:
                continue
            lowered = channel_name.lower()
            if lowered in seen_lower:
                continue
            normalized_channels.append(channel_name)
            seen_lower.add(lowered)

        self.channels = list(normalized_channels)
        self.config["channels"] = list(normalized_channels)

        lock_path = config_path.with_suffix(config_path.suffix + ".lock")
        with file_lock(lock_path):
            data = load_yaml_file(config_path)

            existing_section = data.get("channels")
            if isinstance(existing_section, list):
                existing_channels = [
                    str(item).strip() for item in existing_section if isinstance(item, str)
                ]
            else:
                existing_channels = []

            if existing_channels == normalized_channels:
                return

            data["channels"] = normalized_channels

            try:
                atomic_write_yaml(config_path, data)
            except Exception:
                self.logger.warning(
                    "Failed to write updated channels to config", exc_info=True
                )

    def _load_owner_records(self, config: Dict[str, Any]) -> Dict[str, OwnerRecord]:
        raw_entries = config.get("owner_nicks", []) or []
        if not isinstance(raw_entries, list):
            raise ValueError("Config key 'owner_nicks' must be a list.")

        records: Dict[str, OwnerRecord] = {}
        normalized_entries = []

        for entry in raw_entries:
            if isinstance(entry, str):
                raise ValueError(
                    "Owner entries must include at least a password or hosts. "
                    f"Convert '{entry}' to a mapping with 'nick', 'password', and/or 'hosts'."
                )
            if not isinstance(entry, dict):
                continue

            nick = entry.get("nick")
            if not isinstance(nick, str) or not nick.strip():
                raise ValueError("Owner entry missing required 'nick' string.")
            nick = nick.strip()

            password = entry.get("password")
            if password is not None and not isinstance(password, str):
                raise ValueError(f"Password for owner '{nick}' must be a string.")

            hosts_field = entry.get("hosts") or []
            if hosts_field and not isinstance(hosts_field, list):
                raise ValueError(f"'hosts' for owner '{nick}' must be a list.")

            hosts: Set[str] = set()
            for host_entry in hosts_field:
                if isinstance(host_entry, str) and host_entry.strip():
                    hosts.add(host_entry.strip())

            if not hosts and not password:
                raise ValueError(
                    f"Owner '{nick}' must define a password when no hosts are configured."
                )

            key = nick.lower()
            if key in records:
                raise ValueError(f"Duplicate owner nick '{nick}' detected in config.")

            record = OwnerRecord(nick=nick, password=password, hosts=hosts)
            records[key] = record

            normalized_entry: Dict[str, Any] = {"nick": nick}
            if password:
                normalized_entry["password"] = password
            if hosts:
                normalized_entry["hosts"] = sorted(hosts)
            normalized_entries.append(normalized_entry)

        config["owner_nicks"] = normalized_entries
        return records

    def _persist_owner_records(self) -> None:
        config_path = self.plugin_manager.get_config_path()
        if not config_path:
            return

        serialized = []
        for record in self._owner_records.values():
            entry: Dict[str, Any] = {"nick": record.nick}
            if record.password:
                entry["password"] = record.password
            if record.hosts:
                entry["hosts"] = sorted(record.hosts)
            serialized.append(entry)

        self.config["owner_nicks"] = serialized
        self.owner_nicks = {record.nick for record in self._owner_records.values()}

        lock_path = config_path.with_suffix(config_path.suffix + ".lock")
        with file_lock(lock_path):
            data = load_yaml_file(config_path)
            data["owner_nicks"] = serialized

            try:
                atomic_write_yaml(config_path, data)
            except Exception:
                self.logger.warning(
                    "Failed to write updated owner records to config", exc_info=True
                )

    def _extract_owner_identity(self, prefix: str) -> tuple[Optional[str], Optional[str]]:
        if "!" not in prefix:
            return prefix or None, None
        nick, rest = prefix.split("!", 1)
        if "@" not in rest:
            return nick, None
        ident, host = rest.split("@", 1)
        ident = ident.strip()
        host = host.strip()
        ident_host = f"{ident}@{host}" if ident and host else None
        return nick, ident_host

    def _authenticate_owner(self, prefix: str, password: str) -> bool:
        nick, ident_host = self._extract_owner_identity(prefix)
        if not nick or not ident_host:
            return False

        record = self._owner_records.get(nick.lower())
        if record is None or record.password is None:
            return False

        if password != record.password:
            return False

        added = record.add_host(ident_host)
        if added:
            self._persist_owner_records()
        return True

    def _has_owner_access(self, prefix: Optional[str]) -> bool:
        if not prefix:
            return False
        nick, ident_host = self._extract_owner_identity(prefix)
        if not nick or not ident_host:
            self.logger.debug("Owner access check failed: missing nick or ident_host for prefix %s", prefix)
            return False

        record = self._owner_records.get(nick.lower())
        if record is None:
            self.logger.debug("Owner access check failed: no record for nick %s", nick)
            return False

        if not record.hosts:
            self.logger.debug("Owner access check failed: no hosts configured for nick %s", nick)
            return False

        has_access = record.has_host(ident_host)
        if not has_access:
            self.logger.debug(
                "Owner access check failed: ident_host %s not in hosts %s for nick %s",
                ident_host,
                list(record.hosts),
                nick,
            )
        return has_access

    async def _handle_builtin_commands(
        self, nick: str, prefix: Optional[str], channel: str, text: str, is_private: bool
    ) -> None:
        if not text.startswith(self.prefix):
            return

        parts = text[len(self.prefix) :].strip().split()
        if not parts:
            return

        command = parts[0].lower()
        args = parts[1:]
        reply_target = nick if is_private else channel

        if command == "auth":
            if not is_private:
                await self.privmsg(
                    reply_target, "Authentication must be sent in a private message."
                )
                return
            if not args:
                await self.privmsg(nick, f"Usage: {self.prefix}auth <password>")
                return
            password = " ".join(args)
            if not prefix:
                await self.privmsg(nick, "Authentication failed (missing prefix).")
                return
            if self._authenticate_owner(prefix, password):
                nick_check, ident_host_check = self._extract_owner_identity(prefix)
                await self.privmsg(
                    nick,
                    f"Authentication successful. Your ident_host: {ident_host_check}",
                )
            else:
                await self.privmsg(nick, "Authentication failed.")
            return

        if command == "whoami":
            if not prefix:
                await self.privmsg(reply_target, "Unable to determine your identity.")
                return
            nick_check, ident_host_check = self._extract_owner_identity(prefix)
            record = self._owner_records.get(nick_check.lower() if nick_check else "")
            if record:
                hosts_list = ", ".join(sorted(record.hosts)) if record.hosts else "none"
                has_access = self._has_owner_access(prefix)
                await self.privmsg(
                    reply_target,
                    f"Nick: {nick_check} | Ident_host: {ident_host_check} | "
                    f"Stored hosts: {hosts_list} | Has access: {has_access}",
                )
            else:
                await self.privmsg(
                    reply_target,
                    f"Nick: {nick_check} | Ident_host: {ident_host_check} | No owner record",
                )
            return

        if command == "plugins":
            enabled, disabled = self.plugin_manager.list_plugin_status()
            enabled_str = ", ".join(enabled) if enabled else "none"
            disabled_str = ", ".join(disabled) if disabled else "none"
            message = f"Enabled plugins: {enabled_str} | Disabled plugins: {disabled_str}"
            await self.privmsg(reply_target, message)
            return

        if command in {"load", "unload", "reload"}:
            if not args:
                await self.privmsg(reply_target, f"Usage: {self.prefix}{command} <plugin>")
                return
            plugin_name = args[0]
            try:
                if command == "load":
                    self.plugin_manager.load(plugin_name, self, refresh_config=True)
                elif command == "unload":
                    self.plugin_manager.unload(plugin_name, self)
                else:
                    self.plugin_manager.reload(plugin_name, self)
            except Exception as exc:
                self.logger.exception("Error handling %s command", command)
                await self.privmsg(reply_target, f"{command.title()} failed: {exc}")
            else:
                status_text = "enabled" if command == "load" else "disabled"
                if command == "reload":
                    status_text = "reloaded"
                await self.privmsg(
                    reply_target, f"{command.title()}ed plugin '{plugin_name}' ({status_text})."
                )
            return

        if command in {"say", "join", "part"}:
            if not self._has_owner_access(prefix):
                await self.privmsg(reply_target, "You do not have permission for that command.")
                return

            if command == "say":
                if len(args) < 2:
                    await self.privmsg(reply_target, f"Usage: {self.prefix}say <target> <text>")
                    return
                target = args[0]
                text_to_send = " ".join(args[1:])
                await self.privmsg(target, text_to_send)
                await self.privmsg(reply_target, "Message sent.")
            elif command == "join":
                if not args:
                    await self.privmsg(reply_target, f"Usage: {self.prefix}join <#channel>")
                    return
                target_channel = args[0]
                await self.join(target_channel)
                self._remember_channel(target_channel)
                await self.privmsg(reply_target, f"Joining {target_channel}")
            elif command == "part":
                if not args:
                    await self.privmsg(reply_target, f"Usage: {self.prefix}part <#channel>")
                    return
                target_channel = args[0]
                reason = " ".join(args[1:]) if len(args) > 1 else ""
                await self.part(target_channel, reason)
                self._forget_channel(target_channel)
                await self.privmsg(reply_target, f"Parting {target_channel}")
        if command in {"health", "status"}:
            await self._handle_status(reply_target)
            return

        if command == "help":
            await self._handle_help(reply_target, args)
            return

        # Dispatch to registered plugin commands (need full prefix for owner checks)
        if prefix:
            handled = self.plugin_manager.dispatch_registered_command(
                self, prefix, channel, command, args, is_private
            )
            if handled:
                return
