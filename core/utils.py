import asyncio
import contextlib
import functools
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from filelock import FileLock

import yaml


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with timestamped output."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@dataclass
class IRCMessage:
    prefix: Optional[str]
    command: str
    params: list
    trailing: Optional[str]
    tags: Optional[Dict[str, str]] = None


def parse_irc_message(line: str) -> IRCMessage:
    """Parse a raw IRC protocol line into its components."""
    prefix = None
    trailing = None
    params = []
    tags: Optional[Dict[str, str]] = None

    rest = line.strip("\r\n")

    # IRCv3 message tags: "@tag1=val;tag2 :prefix COMMAND ..."
    if rest.startswith("@"):
        tag_part, _, remainder = rest[1:].partition(" ")
        tags = {}
        for item in tag_part.split(";"):
            if not item:
                continue
            key, sep, value = item.partition("=")
            tags[key] = value if sep else ""
        rest = remainder.lstrip(" ")

    if rest.startswith(":"):
        parts = rest[1:].split(" ", 1)
        prefix = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

    if " :" in rest:
        rest, trailing = rest.split(" :", 1)

    if rest:
        params = rest.split()

    command = params.pop(0) if params else ""
    return IRCMessage(
        prefix=prefix, command=command, params=params, trailing=trailing, tags=tags
    )


class AsyncRateLimiter:
    """Simple async rate limiter based on a sliding time window."""

    def __init__(self, max_messages: int, per_seconds: float) -> None:
        self.max_messages = max_messages
        self.per_seconds = per_seconds
        self._events: Deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until sending a message would respect the limit."""
        async with self._lock:
            now = time.monotonic()
            while self._events and now - self._events[0] > self.per_seconds:
                self._events.popleft()

            if len(self._events) < self.max_messages:
                self._events.append(now)
                return

            wait_time = self.per_seconds - (now - self._events[0])
            if wait_time > 0:
                await asyncio.sleep(wait_time)

            now = time.monotonic()
            while self._events and now - self._events[0] > self.per_seconds:
                self._events.popleft()

            self._events.append(now)

    def is_idle(self) -> bool:
        """True if no events fall within the current window (safe to discard)."""
        if not self._events:
            return True
        return time.monotonic() - self._events[-1] > self.per_seconds


def validate_required_keys(config: Dict[str, object], required: Dict[str, type]) -> None:
    """Ensure required keys exist and match expected types."""
    missing = [key for key in required if key not in config]
    if missing:
        raise KeyError(f"Missing required config keys: {', '.join(missing)}")

    for key, expected_type in required.items():
        if not isinstance(config[key], expected_type):
            raise TypeError(f"Config key '{key}' must be of type {expected_type.__name__}")


def validate_config(config: Dict[str, Any]) -> None:
    """
    Validate the configuration schema.
    Raises ValueError or TypeError if the configuration is invalid.
    """
    required_keys = {
        "server": str,
        "port": int,
        "nickname": str,
        "username": str,
        "realname": str,
        "channels": list,
    }
    validate_required_keys(config, required_keys)

    # Optional keys validation
    if "use_tls" in config and not isinstance(config["use_tls"], bool):
        raise TypeError("Config key 'use_tls' must be of type bool")

    if "owner_nicks" in config and not isinstance(config["owner_nicks"], list):
        raise TypeError("Config key 'owner_nicks' must be of type list")

    if "sasl" in config and not isinstance(config["sasl"], bool):
        raise TypeError("Config key 'sasl' must be of type bool")

    for key in ("sasl_username", "sasl_password"):
        if key in config and not isinstance(config[key], str):
            raise TypeError(f"Config key '{key}' must be of type str")


def load_yaml_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


@contextlib.contextmanager
def file_lock(lock_path: Path):
    """Cross-platform file locking using filelock."""
    # Ensure directory exists
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path))
    with lock:
        yield


def get_plugin_config(bot, plugin_name: str) -> dict:
    """Return the config dict for a plugin, or empty dict if missing."""
    config = getattr(bot, "config", {})
    if not isinstance(config, dict):
        return {}
    plugins_section = config.get("plugins")
    if not isinstance(plugins_section, dict):
        return {}
    candidate = plugins_section.get(plugin_name)
    if isinstance(candidate, dict):
        return candidate
    return {}


async def run_blocking(func, *args, **kwargs):
    """Run a blocking function in the default executor."""
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, call)


async def async_http_get(url: str, *, params=None, timeout: int = 10, **kwargs):
    """Run requests.get in an executor to avoid blocking the event loop."""
    import requests

    loop = asyncio.get_running_loop()
    call = functools.partial(requests.get, url, params=params, timeout=timeout, **kwargs)
    return await loop.run_in_executor(None, call)


async def async_http_post(url: str, *, data=None, json=None, timeout: int = 10, **kwargs):
    """Run requests.post in an executor to avoid blocking the event loop."""
    import requests

    loop = asyncio.get_running_loop()
    call = functools.partial(requests.post, url, data=data, json=json, timeout=timeout, **kwargs)
    return await loop.run_in_executor(None, call)


def atomic_write_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)
    os.replace(tmp_path, path)


def load_json(path: Path, default: Any = None) -> Any:
    """Load JSON from ``path``, returning ``default`` if missing or unreadable."""
    import json

    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def atomic_write_json(path: Path, data: Any, **dump_kwargs: Any) -> None:
    """Write JSON to ``path`` atomically (temp file + os.replace).

    A crash mid-write leaves the original file intact instead of a
    truncated/corrupt one. Extra kwargs are forwarded to ``json.dump``.
    """
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, **dump_kwargs)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
