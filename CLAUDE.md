# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Ebba is an async Python 3 IRC bot with hot-reloadable plugins and YAML-based persistent configuration. Built on `asyncio` with no IRC framework dependency — the IRC protocol is implemented directly in `core/irc_client.py`.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config_sample.yaml config.yaml

# Run
python bot.py                              # uses config.yaml
CONFIG_PATH=/path/to/config.yaml python bot.py  # custom config

# Tests
python -m pytest tests/ -v                 # all tests
python -m pytest tests/test_remindme_plugin.py -v  # single test file
```

## Architecture

**Entry point:** `bot.py` — loads YAML config (with env var overrides), creates `PluginManager` and `IRCClient`, then runs the async event loop with reconnection backoff.

**Core modules (`core/`):**
- `irc_client.py` — IRC protocol client: TLS connections, NICK/USER registration, PING/PONG, reader/writer loops, rate limiting (global + per-target), owner authentication, and built-in commands (`.plugins`, `.load`, `.reload`, `.auth`, `.help`, etc.)
- `plugin_manager.py` — Plugin lifecycle: dynamic loading via `importlib`, command registration, event dispatching (message/join/part/nick/kick/quit), task spawning with semaphore (100 max) and 10s timeout, config merging with file locking
- `utils.py` — IRC message parsing, config validation, `AsyncRateLimiter`, atomic YAML writes, file locking

**Plugin system (`scripts/`):**
- Plugins are Python modules auto-discovered from `scripts/`. Files starting with `_` are ignored.
- See `scripts/_template.py` for the full interface. All hooks are optional:
  - `on_load(bot)` / `on_unload(bot)` — lifecycle
  - `on_message(bot, user, channel, message)` — every PRIVMSG
  - `on_join`, `on_part`, `on_nick`, `on_kick`, `on_quit` — IRC events
- `CONFIG_DEFAULTS` dict in a plugin is merged into in-memory config at runtime (not persisted to disk)
- Plugins register commands via `bot.plugin_manager.register_command(plugin_name, name, handler, aliases, help_text)`
- Handlers receive `(bot, user, channel, args, is_private)`
- Use `bot.privmsg(target, text)` to send messages (rate-limited)
- Blocking I/O (HTTP requests) must run in executor: `loop.run_in_executor(None, func)`
- Plugins are enabled by default unless `plugins.<name>.enabled: false` in config.yaml
- Config is only written when user explicitly acts (`.load`/`.unload`)

**Message flow:** IRC server → reader loop → `parse_irc_message()` → `_handle_privmsg()` → built-in command check → `dispatch_message()` to all plugins + `dispatch_registered_command()` for prefix commands → plugin handlers spawned as async tasks → `bot.privmsg()` → rate limiter → writer loop → IRC server

## Key Conventions

- Config is YAML with env var overrides (see `CONFIG_ENV_MAP` in `bot.py`)
- State persistence is per-plugin: JSON files or SQLite, no shared abstraction
- All plugin handlers are non-blocking (spawned as tasks with 10s timeout)
- Command prefix is configurable (default `.`)
- Rate limiting: global (4 msgs/2s) + per-target (2 msgs/2s)
- Owner auth: password via `.auth` in PM, optional host-based trust
