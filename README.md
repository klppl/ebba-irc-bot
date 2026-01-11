# Ebba IRC Bot

A lightweight, async Python 3 IRC bot with hot-reloadable plugins and persistent configuration.

## Key Features
- **Hot-Reloading:** Load, unload, and reload plugins without restarting the bot.
- **Persistence:** Plugin states (enabled/disabled) are saved automatically to `config.yaml`.
- **Async Core:** Built on `asyncio` for efficient handling of IO and multiple connections.
- **Batteries Included:** Comes with plugins for ChatGPT, Bitcoin prices, Reddit, Instagram, Weather (SMHI), and more.

## Quick Start

1. **Install dependencies:**
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure:**
   ```bash
   cp config_sample.yaml config.yaml
   # Edit config.yaml with your IRC server details and API keys
   ```

3. **Run:**
   ```bash
   python bot.py
   ```

## Usage

### Plugin Management
- `.plugins` - List all loaded plugins.
- `.load <plugin>` - Load a plugin (e.g., `.load chatgpt`).
- `.unload <plugin>` - Unload a plugin.
- `.reload <plugin>` - Reload a plugin to apply code changes immediately.

### Admin Commands
Admins defined in `config.yaml` can authenticate to perform sensitive actions:
1. Message the bot: `.auth <password>`
2. Use admin commands: `.join <channel>`, `.part <channel>`, `.say <channel> <message>`.

## Development

Plugins are located in the `scripts/` directory.

To create a new plugin:
1. Create a file in `scripts/` (e.g., `myplugin.py`).
2. Implement `on_message(bot, user, channel, message)`.
3. (Optional) Implement `on_load(bot)` and `on_unload(bot)`.

See `scripts/_template.py` for a complete example.
