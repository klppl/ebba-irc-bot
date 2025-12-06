import asyncio
import contextlib
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


def parse_irc_message(line: str) -> IRCMessage:
    """Parse a raw IRC protocol line into its components."""
    prefix = None
    trailing = None
    params = []

    rest = line.strip("\r\n")

    if rest.startswith(":"):
        prefix, rest = rest[1:].split(" ", 1)

    if " :" in rest:
        rest, trailing = rest.split(" :", 1)

    if rest:
        params = rest.split()

    command = params.pop(0) if params else ""
    return IRCMessage(prefix=prefix, command=command, params=params, trailing=trailing)


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


def validate_required_keys(config: Dict[str, object], required: Dict[str, type]) -> None:
    """Ensure required keys exist and match expected types."""
    missing = [key for key in required if key not in config]
    if missing:
        raise KeyError(f"Missing required config keys: {', '.join(missing)}")

    for key, expected_type in required.items():
        if not isinstance(config[key], expected_type):
            raise TypeError(f"Config key '{key}' must be of type {expected_type.__name__}")


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


def atomic_write_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)
    os.replace(tmp_path, path)
