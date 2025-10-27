# IRC Bot

Minimal yet extensible IRC bot built on Python 3.10+ with asyncio networking and a pluggable command system.

- Async IRC client with automatic reconnect, PING/PONG, and rate-limited messaging.
- Hot-loadable plugins from the `scripts/` directory.
- Environment-variable overrides for deployment flexibility.
- Example Bitcoin price command powered by CoinGecko.

## Prerequisites
- Python 3.10 or newer
- Access to an IRC server (default: `irc.libera.chat`)
- Internet connectivity for plugins that call external APIs (e.g., the Bitcoin plugin)

## Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run
1. Adjust `config.yaml` with your desired nickname and channels.
2. Launch the bot:
   ```bash
   python bot.py
   ```
3. Stop with `Ctrl+C`. The bot exits gracefully.

## Configuration Reference
All configuration lives in `config.yaml`. Environment variables override the file when set.

| Key | Type | Description | Env Override |
| --- | --- | --- | --- |
| `server` | str | IRC server hostname | `SERVER` |
| `port` | int | IRC server port | `PORT` |
| `use_tls` | bool | Wrap the connection with TLS | `USE_TLS` (`true/false`) |
| `nickname` | str | Bot nickname | `NICKNAME` |
| `username` | str | Username for `USER` handshake | `USERNAME` |
| `realname` | str | Real name/gecos string | `REALNAME` |
| `channels` | list[str] | Channels to auto-join | `CHANNELS` (comma-separated) |
| `prefix` | str | Command prefix for built-in and plugin commands | `PREFIX` |
| `owner_nicks` | list[str] | Nicknames allowed to run admin commands | `OWNER_NICKS` (comma-separated) |
| `reconnect_delay_secs` | int | Initial reconnect delay after disconnect | `RECONNECT_DELAY_SECS` |
| `request_timeout_secs` | int | Timeout passed to HTTP-based plugins | `REQUEST_TIMEOUT_SECS` |

Optional tuning keys (defaults shown):

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `max_reconnect_delay_secs` | int | `60` | Caps exponential backoff when reconnecting |
| `privmsg_rate_count` | int | `4` | Max PRIVMSGs per window |
| `privmsg_rate_window_secs` | float | `2.0` | Window length in seconds |

Set `CONFIG_PATH` to point at an alternate YAML file if desired.

## Plugin Development
Plugins are plain Python modules placed in `scripts/`. Each module can expose the following functions:

```python
def on_load(bot):
    """Runs after the plugin is imported."""

def on_unload(bot):
    """Runs before the plugin is removed."""

def on_message(bot, user, channel, message):
    """Called for every PRIVMSG the bot receives."""
```

`bot` is an instance of `core.irc_client.IRCClient`. Helpful methods include:
- `await bot.privmsg(target, text)` – send a message (rate-limited)
- `await bot.join(channel)` / `await bot.part(channel, reason="")`
- `bot.prefix`, `bot.request_timeout`, `bot.owner_nicks`

Plugins run inside the asyncio event loop. Long-running work should move to an executor or create async tasks to keep the bot responsive. See `scripts/bitcoin.py` for a complete example, including safe HTTP usage via `run_in_executor`.

### Minimal Template
```python
import asyncio

def on_message(bot, user, channel, message):
    if message.strip() == f"{bot.prefix}ping":
        asyncio.get_running_loop().create_task(bot.privmsg(channel, "pong"))
```

## Hot-Reload Instructions
While connected, use these built-in commands from any channel or private message:
- `.plugins` — List loaded plugins.
- `.load <plugin>` — Load a plugin module (filename without `.py`).
- `.unload <plugin>` — Unload a plugin.
- `.reload <plugin>` — Reload a plugin in place.

Admin-only commands (nick must be listed in `owner_nicks`):
- `.say <target> <text>` — Send a message to a channel or user.
- `.join <#channel>` — Join a channel.
- `.part <#channel> [reason]` — Leave a channel with optional reason.

## Troubleshooting
- **Nickname already in use:** The bot automatically appends `_` and retries. Adjust `nickname` if collisions persist.
- **Connection drops frequently:** Increase `reconnect_delay_secs` or check for network restrictions. Logs show reconnect attempts.
- **Rate limited / throttled:** The bot enforces a small PRIVMSG limit. Tune `privmsg_rate_count` and `privmsg_rate_window_secs` if channels allow faster messaging.
- **Bitcoin command returns "unavailable":** CoinGecko may be down, or outbound HTTPS is blocked. Inspect logs for HTTP errors and confirm internet access.
- **Plugin fails to load:** Review stack traces in the console. Syntax errors or missing dependencies will abort the load; fix and run `.reload <plugin>`.
