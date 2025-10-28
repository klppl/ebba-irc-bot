# Ebba IRC Bot

Async Python 3 IRC bot with a hot-reloadable plugin layer and persistent plugin state.

## Features
- asyncio IRC core with automatic reconnects, flood protection, and `.load/.unload/.reload`.
- Plugins live in `scripts/`; each module exposes optional `on_load`, `on_unload`, `on_message`.
- Plugin enable/disable state is written back to `config.yaml` so settings survive restarts.
- Sample integrations: CoinGecko BTC price, TV Maze, Twitter oEmbed, Reddit summaries, Instagram, ChatGPT responder, Swedish electricity prices, SMHI weather, etc.

## Quickstart
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config_sample.yaml config.yaml
python bot.py
```
Edit `config.yaml` (or override via env vars) for server, nickname, channels, and any plugin keys such as `plugins.chatgpt.api_key`.

## Plugin Ops
- `.plugins` → lists enabled/disabled plugins.
- `.load foo` / `.unload foo` / `.reload foo` → hot swap modules (`foo` = filename without `.py`).
- Admins (configured in `owner_nicks` with passwords/host masks) also get `.say`, `.join`, `.part` after authenticating with `.auth <password>` in a private message.

## Plugin Skeleton
```python
import asyncio

def on_message(bot, user, channel, message):
    if message.strip() == f"{bot.prefix}ping":
        asyncio.get_running_loop().create_task(bot.privmsg(channel, "pong"))
```
Use `bot.request_timeout` for HTTP timeouts, and offload blocking work with `run_in_executor`.

## TODO
- Add coverage for HTTP plugins (mock external APIs in tests).
- Document plugin configuration keys in `config_sample.yaml`.
- Ship optional Dockerfile + compose for quick deployment.
- Implement structured logging / log rotation hooks.
- Add automated lint/format tooling (ruff, black, mypy) to CI.
