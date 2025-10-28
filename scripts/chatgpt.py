"""
ChatGPT responder plugin.

Configuration (config.yaml):

```
plugins:
  chatgpt:
    api_key: "<openai_api_key>"
    model: "gpt-4o-mini"  # optional, defaults to gpt-4o-mini
```

Only `api_key` is required. If omitted, the plugin stays disabled.
"""

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

try:
    from openai import AuthenticationError, OpenAI, OpenAIError, RateLimitError
except ImportError:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[assignment]
    AuthenticationError = OpenAIError = RateLimitError = Exception  # type: ignore[misc]

logger = logging.getLogger(__name__)


DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_SYSTEM_PROMPT = (
    "Du är en neutral AI. Kort, saklig och korrekt. Svara som i ett sms: inget fluff, inga känslor. "
    "Fakta, logik och tydlighet först. Om något är okänt, säg det direkt."
)


DEFAULT_HISTORY_LIMIT = 50
DEFAULT_MAX_MESSAGE_LENGTH = 450
DEFAULT_MESSAGE_DELAY = 1.0
DEFAULT_RATE_LIMIT = 5.0


@dataclass
class ChatGPTSettings:
    api_key: Optional[str]
    model: str = DEFAULT_MODEL
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    history_limit: int = DEFAULT_HISTORY_LIMIT
    max_message_length: int = DEFAULT_MAX_MESSAGE_LENGTH
    message_delay_secs: float = DEFAULT_MESSAGE_DELAY
    rate_limit_secs: float = DEFAULT_RATE_LIMIT
    enabled: bool = False


@dataclass
class ChatGPTState:
    settings: ChatGPTSettings
    client: Optional[OpenAI] = None  # type: ignore[type-arg]
    history: Dict[str, Deque[Tuple[str, str]]] = field(default_factory=dict)
    last_request: Dict[str, float] = field(default_factory=dict)


state: Optional[ChatGPTState] = None


def on_load(bot) -> None:
    global state
    settings = _settings_from_config(bot)
    if not settings.enabled:
        logger.info("ChatGPT plugin disabled (API key not configured)")
        state = ChatGPTState(settings=settings)
        return

    if OpenAI is None:
        logger.info(
            "ChatGPT plugin disabled (python 'openai' package not installed). Run 'pip install openai'."
        )
        settings.enabled = False
        state = ChatGPTState(settings=settings)
        return

    client = None
    try:
        client = OpenAI(api_key=settings.api_key)
    except Exception as exc:  # pragma: no cover - library init
        logger.error("Failed to initialise OpenAI client: %s", exc)
        settings.enabled = False
        state = ChatGPTState(settings=settings)
        return

    state = ChatGPTState(settings=settings, client=client)
    logger.info("ChatGPT plugin loaded with model %s", settings.model)


def on_unload(bot) -> None:
    global state
    state = None
    logger.info("ChatGPT plugin unloaded")


def on_message(bot, user: str, channel: str, message: str) -> None:
    global state
    if state is None or not state.settings.enabled:
        return

    # Handle reset command first
    prefix = bot.prefix
    if message.startswith(prefix):
        command = message[len(prefix) :].strip().lower()
        if command == "reset":
            asyncio.get_running_loop().create_task(_reset_history(bot, channel))
            return

    # Only react when bot is addressed
    pattern = re.compile(rf"^{re.escape(bot.nickname)}(\s+|[:,]\s+)(.*)", re.IGNORECASE)
    match = pattern.match(message.strip())
    if not match:
        return

    prompt = match.group(2).strip()
    if not prompt:
        return

    loop = asyncio.get_running_loop()
    loop.create_task(_handle_prompt(bot, channel, user, prompt))


async def _handle_prompt(bot, channel: str, nick: str, prompt: str) -> None:
    assert state is not None
    settings = state.settings

    current_time = time.time()
    last = state.last_request.get(channel, 0.0)
    if current_time - last < settings.rate_limit_secs:
        await bot.privmsg(channel, "Whoa, slow down! I'm still compiling the last response.")
        return
    state.last_request[channel] = current_time

    history = state.history.setdefault(
        channel, deque(maxlen=settings.history_limit)
    )
    history.append((nick, prompt))

    system_prompt = settings.system_prompt or ""
    try:
        system_prompt = system_prompt.format(nick=bot.nickname)
    except Exception:
        logger.warning("System prompt formatting failed; using raw prompt.")

    if prompt.lower().startswith("tell me a joke"):
        system_prompt += (
            " When asked for a joke, keep it short, witty, and on theme with Linux, "
            "programming, or prepping."
        )
    if system_prompt:
        system_prompt += " Do not include your name or any prefix in your responses."

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for sender, text in history:
        role = "assistant" if sender.lower() == bot.nickname.lower() else "user"
        messages.append({"role": role, "content": text})

    try:
        response_text = await _call_openai(messages, settings)
    except RateLimitError:
        await bot.privmsg(
            channel, "The AI's rationing its brainpower like I ration MREs. Try again soon!"
        )
        return
    except AuthenticationError:
        await bot.privmsg(channel, "My API key's gone AWOL. Yell at the admin!")
        return
    except OpenAIError as exc:  # type: ignore[misc]
        logger.error("OpenAI error: %s", exc)
        await bot.privmsg(channel, f"AI hiccup: {exc}. Try again?")
        return
    except Exception as exc:
        logger.exception("Unexpected ChatGPT error")
        await bot.privmsg(
            channel, f"Something broke like a bad script in prod: {exc}. Retry?"
        )
        return

    if not response_text:
        await bot.privmsg(channel, "My joke generator's out of RAM. Try again?")
        return

    history.append((bot.nickname, response_text))
    await _send_split_response(bot, channel, response_text, settings)


async def _call_openai(messages: List[Dict[str, str]], settings: ChatGPTSettings) -> str:
    if state is None or not settings.enabled:
        return ""
    client = state.client
    if client is None:
        raise RuntimeError("OpenAI client unavailable")

    loop = asyncio.get_running_loop()

    def _request():
        return client.chat.completions.create(
            model=settings.model,
            messages=messages,
        )

    response = await loop.run_in_executor(None, _request)
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    message = choices[0].message
    content = getattr(message, "content", "") if message else ""
    return content.strip() if content else ""


async def _send_split_response(
    bot,
    channel: str,
    text: str,
    settings: ChatGPTSettings,
) -> None:
    parts: List[str] = []
    remaining = text
    limit = max(1, settings.max_message_length)
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        split_at = remaining.rfind(" ", 0, limit)
        if split_at == -1:
            parts.append(remaining[:limit] + "…")
            remaining = remaining[limit:]
        else:
            parts.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip()

    for idx, chunk in enumerate(parts):
        await bot.privmsg(channel, chunk)
        if idx < len(parts) - 1:
            await asyncio.sleep(max(0.0, settings.message_delay_secs))
    if len(parts) > 1 and len(text) > limit:
        await bot.privmsg(channel, "Response truncated. Ask for details if needed!")


async def _reset_history(bot, channel: str) -> None:
    if state is None:
        return
    if channel in state.history:
        state.history[channel].clear()
        state.last_request.pop(channel, None)
        await bot.privmsg(channel, "Conversation history wiped cleaner than a fresh Arch install!")
    else:
        await bot.privmsg(channel, "No history to reset. We're already off-grid!")


def _settings_from_config(bot) -> ChatGPTSettings:
    config = getattr(bot, "config", {})
    plugins_section = config.get("plugins") if isinstance(config, dict) else {}
    section = {}
    if isinstance(plugins_section, dict):
        section = plugins_section.get("chatgpt", {})
    if not isinstance(section, dict):
        section = {}

    api_key = section.get("api_key")
    model = section.get("model", DEFAULT_MODEL)
    enabled = bool(api_key)
    return ChatGPTSettings(
        api_key=str(api_key) if api_key else None,
        model=str(model),
        enabled=enabled,
    )
