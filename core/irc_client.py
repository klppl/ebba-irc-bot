import asyncio
import base64
import contextlib
import hmac
import logging
import signal
import ssl
from asyncio import StreamReader, StreamWriter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

# A connection that stayed registered at least this long is considered healthy,
# so the reconnect backoff starts over from reconnect_delay_secs.
STABLE_CONNECTION_SECS = 60
SEND_QUEUE_MAXSIZE = 100

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

    def __init__(
        self,
        config: Dict[str, Any],
        plugin_manager: PluginManager,
        env_overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.config = config
        # Values that came from environment variables. They are re-applied
        # whenever the config is reloaded from disk, which doesn't contain them.
        self._env_overrides = dict(env_overrides or {})
        self.plugin_manager = plugin_manager
        self.logger = logging.getLogger("IRCClient")
        self.server = str(config["server"])
        self.port = int(config["port"])
        self.use_tls = bool(config.get("use_tls", False))
        self.nickname = str(config["nickname"])
        self._configured_nick = self.nickname
        self.username = str(config["username"])
        self.realname = str(config["realname"])
        self.channels = list(config.get("channels", []))
        self._channels_lower: Set[str] = {ch.lower() for ch in self.channels if isinstance(ch, str)}
        self.prefix = str(config.get("prefix", "."))
        self._owner_records = self._load_owner_records(config)
        self.owner_nicks = {record.nick for record in self._owner_records.values()}
        self.reconnect_delay = int(config.get("reconnect_delay_secs", 5))
        self.request_timeout = int(config.get("request_timeout_secs", 10))
        self.max_backoff = int(config.get("max_reconnect_delay_secs", 60))
        self.join_delay_secs = float(config.get("join_delay_secs", 0.4))
        # Send a PING after this much silence from the server; drop the
        # connection if the silence lasts twice as long.
        self.ping_timeout = max(10.0, float(config.get("ping_timeout_secs", 120)))
        rate_count = int(config.get("privmsg_rate_count", 4))
        rate_window = float(config.get("privmsg_rate_window_secs", 2.0))
        self._rate_limiter = AsyncRateLimiter(rate_count, rate_window)
        per_target_count = int(config.get("per_target_rate_count", 2))
        per_target_window = float(config.get("per_target_rate_window_secs", 2.0))
        self._per_target_count = per_target_count
        self._per_target_window = per_target_window
        self._target_rate_limiters: Dict[str, AsyncRateLimiter] = {}
        self.ignored_nicks: Set[str] = set()

        # SASL (PLAIN) authentication — optional, negotiated via IRCv3 CAP.
        self.sasl_enabled = bool(config.get("sasl", False))
        self.sasl_username = str(config.get("sasl_username") or self.username or self.nickname)
        self.sasl_password = config.get("sasl_password")
        if self.sasl_enabled and not self.sasl_password:
            self.logger.warning("SASL enabled but no sasl_password configured; disabling SASL")
            self.sasl_enabled = False
        self._cap_ls_buffer = ""

        self.reader: Optional[StreamReader] = None
        self.writer: Optional[StreamWriter] = None
        self._send_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=SEND_QUEUE_MAXSIZE)
        self._writer_task: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._join_task: Optional[asyncio.Task] = None
        self._background_tasks: Set[asyncio.Task] = set()
        self._registered = False
        self._registered_at: Optional[float] = None
        self._last_rx: float = 0.0
        # Single worker so config writes land in the order they were issued.
        self._config_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="config-writer")
        self._stop_event = asyncio.Event()
        self._signals_registered = False
        self._last_connect_time: Optional[float] = None
        self._last_disconnect_time: Optional[float] = None

    async def start(self) -> None:
        """Attempt to connect and stay connected with exponential backoff."""
        base_delay = max(self.reconnect_delay, 1)
        backoff = base_delay
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.exception("Connection error: %s", exc)
            await self._cleanup_connection()
            if self._stop_event.is_set():
                break

            # Always wait before reconnecting, even after a clean server-side
            # close, so a server that drops us right away isn't hammered.
            now = asyncio.get_running_loop().time()
            if self._registered_at is not None and now - self._registered_at >= STABLE_CONNECTION_SECS:
                backoff = max(self.reconnect_delay, 1)
            self.logger.info("Reconnecting in %s seconds", backoff)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            backoff = min(backoff * 2, max(self.max_backoff, 1))

    async def stop(self, reason: str = "Shutting down") -> None:
        self._stop_event.set()
        writer = self.writer
        if writer is not None and not writer.is_closing():
            with contextlib.suppress(Exception):
                writer.write(f"QUIT :{reason}\r\n".encode("utf-8"))
                await asyncio.wait_for(writer.drain(), timeout=2)
        await self._cleanup_connection()
        self._config_executor.shutdown(wait=True)

    async def _connect_once(self) -> None:
        ssl_context = ssl.create_default_context() if self.use_tls else None
        # Fresh state per connection: try the configured nick again and drop
        # anything queued while we were disconnected.
        self.nickname = self._configured_nick
        self._registered = False
        self._registered_at = None
        self._send_queue = asyncio.Queue(maxsize=SEND_QUEUE_MAXSIZE)
        self.logger.info("Connecting to %s:%s (TLS=%s)", self.server, self.port, self.use_tls)
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.server,
                self.port,
                ssl=ssl_context,
                server_hostname=self.server if self.use_tls else None,
            ),
            timeout=30,
        )
        loop = asyncio.get_running_loop()
        self._last_connect_time = loop.time()
        self._last_rx = loop.time()
        await self._register()

        if not self._signals_registered:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, lambda: self._spawn_background(self.stop()))
                except NotImplementedError:
                    # Not available on Windows event loop
                    pass
            self._signals_registered = True

        self._writer_task = asyncio.create_task(self._writer_loop(), name="irc-writer")
        self._reader_task = asyncio.create_task(self._reader_loop(), name="irc-reader")
        self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="irc-keepalive")

        # If either side of the connection dies, tear the whole thing down
        # rather than leaving a reader with no writer (or vice versa).
        done, _ = await asyncio.wait(
            {self._writer_task, self._reader_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                exc = task.exception()
                self.logger.error("%s failed: %s", task.get_name(), exc, exc_info=exc)

    async def _register(self) -> None:
        if self.sasl_enabled:
            # Begin capability negotiation; registration is held until CAP END.
            self._cap_ls_buffer = ""
            await self.send_raw("CAP LS 302")
        await self.send_raw(f"NICK {self.nickname}")
        await self.send_raw(f"USER {self.username} 0 * :{self.realname}")

    async def _handle_cap(self, message: IRCMessage) -> None:
        # Format: CAP <nick/*> <LS|ACK|NAK> [*] :<caps>
        subcommand = message.params[1] if len(message.params) >= 2 else ""
        caps = message.trailing
        if caps is None:
            # A single capability may be sent without the ':' marker.
            caps = message.params[-1] if len(message.params) >= 3 and message.params[-1] != "*" else ""
        if subcommand == "LS":
            more_coming = len(message.params) >= 3 and message.params[2] == "*"
            self._cap_ls_buffer += " " + caps
            if more_coming:
                return
            available = set(self._cap_ls_buffer.split())
            self._cap_ls_buffer = ""
            if self.sasl_enabled and "sasl" in available:
                await self.send_raw("CAP REQ :sasl")
            else:
                if self.sasl_enabled:
                    self.logger.warning("Server does not advertise SASL; continuing without it")
                await self.send_raw("CAP END")
        elif subcommand == "ACK":
            acked = set(caps.split())
            if "sasl" in acked:
                await self.send_raw("AUTHENTICATE PLAIN")
            else:
                await self.send_raw("CAP END")
        elif subcommand == "NAK":
            self.logger.warning("Server rejected requested capabilities; continuing without SASL")
            await self.send_raw("CAP END")

    async def _handle_authenticate(self, message: IRCMessage) -> None:
        token = message.params[0] if message.params else (message.trailing or "")
        if token != "+":
            return
        if not self.sasl_password:
            await self.send_raw("CAP END")
            return
        # SASL PLAIN payload: authzid \0 authcid \0 passwd (empty authzid).
        payload = f"\0{self.sasl_username}\0{self.sasl_password}".encode("utf-8")
        encoded = base64.b64encode(payload).decode("ascii")
        await self.send_raw(f"AUTHENTICATE {encoded}")

    async def _handle_sasl_result(self, message: IRCMessage) -> None:
        if message.command == "903":
            self.logger.info("SASL authentication successful")
        else:
            self.logger.warning("SASL authentication failed (numeric %s)", message.command)
        await self.send_raw("CAP END")

    async def _cleanup_connection(self) -> None:
        self._registered = False
        for task in (self._join_task, self._keepalive_task, self._writer_task, self._reader_task):
            if task is None or task is asyncio.current_task():
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        writer = self.writer
        if writer:
            try:
                writer.close()
                # wait_closed() can hang on a dead TLS peer; don't let it stall reconnects.
                await asyncio.wait_for(writer.wait_closed(), timeout=5)
            except Exception:
                pass

        if writer is not None:
            self._last_disconnect_time = asyncio.get_running_loop().time()
        self.reader = None
        self.writer = None
        self._writer_task = None
        self._reader_task = None
        self._keepalive_task = None
        self._join_task = None

    def _spawn_background(self, coro) -> asyncio.Task:
        # Keep a strong reference so the task isn't garbage-collected mid-run.
        task = asyncio.get_running_loop().create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        return task

    def _background_task_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            self.logger.error("Background task failed: %s", exc, exc_info=exc)

    async def _keepalive_loop(self) -> None:
        """Detect half-open connections that would otherwise hang readline() forever."""
        loop = asyncio.get_running_loop()
        ping_sent = False
        while True:
            await asyncio.sleep(min(self.ping_timeout / 4, 30))
            silence = loop.time() - self._last_rx
            if silence < self.ping_timeout:
                ping_sent = False
                continue
            if silence >= self.ping_timeout * 2:
                self.logger.warning(
                    "No data from server for %ds; dropping connection", int(silence)
                )
                if self.writer is not None:
                    # abort() makes readline() return EOF, ending the reader loop.
                    self.writer.transport.abort()
                return
            if not ping_sent:
                await self.send_raw(f"PING :{self.server}")
                ping_sent = True

    async def _writer_loop(self) -> None:
        writer = self.writer
        assert writer is not None
        queue = self._send_queue
        while not self._stop_event.is_set():
            message = await queue.get()
            writer.write(f"{message}\r\n".encode("utf-8"))
            try:
                await writer.drain()
            except ConnectionError:
                self.logger.warning("Connection lost during write")
                break

    async def _reader_loop(self) -> None:
        reader = self.reader
        assert reader is not None
        loop = asyncio.get_running_loop()
        debug = self.logger.isEnabledFor(logging.DEBUG)
        while not self._stop_event.is_set():
            raw = await reader.readline()
            if not raw:
                self.logger.warning("Server closed the connection")
                break
            self._last_rx = loop.time()
            line = raw.decode("utf-8", errors="replace").strip("\r\n")
            if not line:
                continue
            if debug:
                self.logger.debug("< %s", line)
            # One bad line (or a bug in a handler) must not kill the connection.
            try:
                await self._handle_message(parse_irc_message(line))
            except Exception:
                self.logger.exception("Error handling line: %s", line[:200])

    async def send_raw(self, message: str) -> None:
        # A CR/LF/NUL in plugin output (page titles, API responses, AI replies)
        # would otherwise let the text inject arbitrary IRC commands.
        for bad in ("\r", "\n", "\0"):
            if bad in message:
                message = message.split(bad, 1)[0]
        if not message:
            return
        self.logger.debug("> %s", message)
        try:
            self._send_queue.put_nowait(message)
        except asyncio.QueueFull:
            if message.startswith("PONG "):
                # Never drop a PONG: missing one gets us disconnected.
                writer = self.writer
                if writer is not None and not writer.is_closing():
                    writer.write(f"{message}\r\n".encode("utf-8"))
                    return
            self.logger.warning("Send queue full; dropping message: %s", message[:200])
            return

    async def privmsg(self, target: str, text: str) -> None:
        target = target.strip()
        if not target or any(c in target for c in " \r\n\0"):
            self.logger.warning("Refusing to send to invalid target %r", target)
            return
        for line in self._split_message(target, text):
            await self._acquire_target_rate(target)
            await self._rate_limiter.acquire()
            if not self._registered:
                self.logger.debug("Not connected; dropping message to %s", target)
                return
            await self.send_raw(f"PRIVMSG {target} :{line}")

    @staticmethod
    def _split_message(target: str, text: str) -> List[str]:
        """Split text on newlines and into chunks the server won't truncate."""
        limit = max(64, 510 - len(f"PRIVMSG {target} :".encode("utf-8")))
        chunks: List[str] = []
        for line in str(text).replace("\0", "").splitlines():
            line = line.rstrip()
            while len(line.encode("utf-8")) > limit:
                # Largest prefix that fits, preferring to break at a space.
                cut = len(line.encode("utf-8")[:limit].decode("utf-8", errors="ignore"))
                space = line.rfind(" ", 0, cut)
                if space > cut // 2:
                    cut = space
                chunks.append(line[:cut].rstrip())
                line = line[cut:].lstrip()
            if line:
                chunks.append(line)
        return chunks

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
            payload = message.trailing or (message.params[0] if message.params else "server")
            await self.send_raw(f"PONG :{payload}")
            return

        if message.command == "PONG":
            return

        if message.command == "CAP":
            await self._handle_cap(message)
            return

        if message.command == "AUTHENTICATE":
            await self._handle_authenticate(message)
            return

        if message.command in {"903", "904", "905", "906", "907", "908", "902"}:
            await self._handle_sasl_result(message)
            return

        if message.command == "001":
            self._registered = True
            self._registered_at = asyncio.get_running_loop().time()
            # The server tells us the nick it actually registered.
            if message.params:
                self.nickname = message.params[0]
            # Joining sleeps between channels; don't stall the reader (and PONGs) on it.
            self._join_task = asyncio.create_task(self._join_initial_channels(), name="irc-join")
            return

        if message.command == "433":
            # Only relevant while registering; after that our nick is unchanged.
            if not self._registered:
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
        for channel in list(self.channels):
            if not first and self.join_delay_secs > 0:
                try:
                    await asyncio.sleep(self.join_delay_secs)
                except Exception:
                    pass
            await self.join(channel)
            first = False

    def refresh_runtime_settings(self) -> None:
        # A reload replaces config with the on-disk file; restore env-provided
        # values. Channels are skipped: the file already holds the live list.
        for key, value in self._env_overrides.items():
            if key != "channels":
                self.config[key] = value
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
            self._channels_lower = {ch.lower() for ch in self.channels if isinstance(ch, str)}

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

    # Prune idle per-target limiters once the dict grows past this many entries.
    _TARGET_LIMITER_PRUNE_THRESHOLD = 512

    def _prune_idle_target_limiters(self) -> None:
        # Drop limiters with no in-window events; they carry no useful state and
        # would otherwise accumulate one entry per nick/channel forever.
        idle = [
            key
            for key, limiter in self._target_rate_limiters.items()
            if limiter.is_idle()
        ]
        for key in idle:
            self._target_rate_limiters.pop(key, None)

    async def _acquire_target_rate(self, target: str) -> None:
        limiter = self._target_rate_limiters.get(target.lower())
        if limiter is None:
            if len(self._target_rate_limiters) >= self._TARGET_LIMITER_PRUNE_THRESHOLD:
                self._prune_idle_target_limiters()
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
        if message.prefix is None:
            return
        text = message.trailing
        if text is None and len(message.params) >= 2:
            # RFC 1459 allows a single-word message without the ':' marker.
            text = message.params[-1]
        if text is None:
            return
        user = message.prefix
        target = message.params[0] if message.params else ""
        nick = user.split("!", 1)[0]
        is_private = target.lower() == self.nickname.lower()
        channel = nick if is_private else target
        if nick.lower() in getattr(self, "ignored_nicks", set()):
            self.logger.debug("Ignoring message from %s due to ignore list", nick)
            return
        if text.startswith(self.prefix):
            # Built-ins await rate-limited replies; run them off the reader loop
            # so a burst of commands can't delay PONGs or stall reading.
            self._spawn_background(
                self._handle_builtin_commands(nick, user, channel, text, is_private)
            )
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

        # Update in-memory list (O(1) case-insensitive dedupe).
        lowered = channel.lower()
        changed = False
        if lowered not in self._channels_lower:
            self.channels.append(channel)
            self._channels_lower.add(lowered)
            changed = True

        channels_list = self.config.setdefault("channels", [])
        if isinstance(channels_list, list):
            if not any(isinstance(existing, str) and existing.lower() == lowered for existing in channels_list):
                channels_list.append(channel)
                changed = True
        else:
            self.config["channels"] = [channel]
            changed = True

        # Our own JOIN echo lands here too; skip the disk round-trip when nothing changed.
        if changed:
            self._persist_channels()

    def _forget_channel(self, channel: str) -> None:
        channel = channel.strip()
        if not channel:
            return

        target_lower = channel.lower()
        if target_lower not in self._channels_lower and not any(
            isinstance(existing, str) and existing.lower() == target_lower
            for existing in self.config.get("channels") or []
        ):
            return
        self.channels = [
            existing
            for existing in self.channels
            if isinstance(existing, str) and existing.lower() != target_lower
        ]
        self._channels_lower.discard(target_lower)

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

        # Snapshot for the background write
        channels_snapshot = list(normalized_channels)

        def _write() -> None:
            lock_path = config_path.with_suffix(config_path.suffix + ".lock")
            with file_lock(lock_path):
                try:
                    data = load_yaml_file(config_path, strict=True)
                except Exception:
                    self.logger.warning(
                        "Not updating channels: could not read %s", config_path, exc_info=True
                    )
                    return

                existing_section = data.get("channels")
                if isinstance(existing_section, list):
                    existing_channels = [
                        str(item).strip() for item in existing_section if isinstance(item, str)
                    ]
                else:
                    existing_channels = []

                if existing_channels == channels_snapshot:
                    return

                data["channels"] = channels_snapshot

                try:
                    atomic_write_yaml(config_path, data)
                except Exception:
                    self.logger.warning(
                        "Failed to write updated channels to config", exc_info=True
                    )

        self._submit_config_write(_write)

    def _submit_config_write(self, write) -> None:
        try:
            self._config_executor.submit(write)
        except RuntimeError:
            # Executor already shut down (we're stopping); write synchronously.
            write()

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

        # Snapshot for the background write
        serialized_snapshot = list(serialized)

        def _write() -> None:
            lock_path = config_path.with_suffix(config_path.suffix + ".lock")
            with file_lock(lock_path):
                try:
                    data = load_yaml_file(config_path, strict=True)
                except Exception:
                    self.logger.warning(
                        "Not updating owners: could not read %s", config_path, exc_info=True
                    )
                    return
                data["owner_nicks"] = serialized_snapshot

                try:
                    atomic_write_yaml(config_path, data)
                except Exception:
                    self.logger.warning(
                        "Failed to write updated owner records to config", exc_info=True
                    )

        self._submit_config_write(_write)

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

        if not hmac.compare_digest(password.encode("utf-8"), record.password.encode("utf-8")):
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
            await self._cmd_auth(nick, prefix, reply_target, args, is_private)
            return
        if command == "whoami":
            await self._cmd_whoami(prefix, reply_target)
            return
        if command == "plugins":
            await self._cmd_plugins(reply_target)
            return
        if command in {"load", "unload", "reload"}:
            await self._cmd_load(command, prefix, reply_target, args)
            return
        if command in {"say", "join", "part"}:
            await self._cmd_say_join_part(command, prefix, reply_target, args)
            return
        if command in {"health", "status"}:
            await self._handle_status(reply_target)
            return
        if command == "help":
            await self._handle_help(reply_target, args)
            return

        # Dispatch to registered plugin commands (need full prefix for owner checks)
        if prefix:
            self.plugin_manager.dispatch_registered_command(
                self, prefix, channel, command, args, is_private
            )

    async def _cmd_auth(
        self, nick: str, prefix: Optional[str], reply_target: str, args: List[str], is_private: bool
    ) -> None:
        if not is_private:
            await self.privmsg(reply_target, "Authentication must be sent in a private message.")
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

    async def _cmd_whoami(self, prefix: Optional[str], reply_target: str) -> None:
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

    async def _cmd_plugins(self, reply_target: str) -> None:
        enabled, disabled = self.plugin_manager.list_plugin_status()
        enabled_str = ", ".join(enabled) if enabled else "none"
        disabled_str = ", ".join(disabled) if disabled else "none"
        message = f"Enabled plugins: {enabled_str} | Disabled plugins: {disabled_str}"
        await self.privmsg(reply_target, message)

    async def _cmd_load(
        self, command: str, prefix: Optional[str], reply_target: str, args: List[str]
    ) -> None:
        if not self._has_owner_access(prefix):
            await self.privmsg(reply_target, "You do not have permission for that command.")
            return
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

    async def _cmd_say_join_part(
        self, command: str, prefix: Optional[str], reply_target: str, args: List[str]
    ) -> None:
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
            await self.privmsg(reply_target, f"Joining {target_channel}")
        elif command == "part":
            if not args:
                await self.privmsg(reply_target, f"Usage: {self.prefix}part <#channel>")
                return
            target_channel = args[0]
            reason = " ".join(args[1:]) if len(args) > 1 else ""
            await self.part(target_channel, reason)
            await self.privmsg(reply_target, f"Parting {target_channel}")
