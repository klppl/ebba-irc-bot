"""
AI responder plugin — supports DeepSeek, OpenAI, and xAI Grok.

Responds when the bot is addressed by nick, in PM, or — at low probability —
chimes in unprompted on lively conversation. Per-channel system prompts,
talkback and AI toggles per channel, persistent per-user history. Web search
citations are only available on the Grok provider (x.ai Responses API).

Owners can attach standing notes to a channel that are injected into the
system prompt for every reply (and chime-in) there:

```
.ai remember the channel topic is retro computing
.ai notes                # list saved notes with their ids
.ai forget 3             # drop note #3
.ai forget all           # drop every note for this channel
```

Notes are stored per channel in the plugin's SQLite DB (table
`ai_channel_memories`), so they survive reloads and restarts.

Configuration (config.yaml):

```
plugins:
  ai:
    enabled: true
    provider: deepseek                # deepseek | openai | grok
    api_key: "<api_key_for_provider>"
    model: ""                         # blank -> provider's default cheap model
    blocked_channels: []
    ignored_nicks: []
    banned_nicks: []
    intent_check: "heuristic"         # or "off"
    system_prompt: ""                 # leave empty to use the default
```

Provider defaults (cheapest model in each catalogue as of May 2026):
- deepseek -> deepseek-chat   ($0.14 / $0.28 per M tokens)
- openai   -> gpt-4.1-nano    ($0.10 / $0.40 per M tokens)
- grok     -> grok-4-1-fast   ($0.20 / $0.50 per M tokens, supports web_search)

Only `api_key` is required. Without it the plugin stays disabled.

Per-channel system prompts can be placed in `scripts/ai_channel_prompts.json`:

```
{
  "#channel": {"prompt": "You are ...", "always_search": false}
}
```

Plain strings are also accepted (`{"#chan": "You are ..."}`). `always_search`
only takes effect when provider=grok.
"""

import asyncio
import datetime
import json
import logging
import random
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


# ---- Tunables -------------------------------------------------------------

MAX_SEND_LEN = 440
SEND_DELAY = 1.0
CHANNEL_RATE_LIMIT = 4
REVIEW_COOLDOWN = 30
USER_SAFETY_SECONDS = 2

TYPING_DELAY_MIN = 1.5
TYPING_DELAY_MAX = 4.0

CHIMEIN_ENABLED = True
CHIMEIN_CHANCE_PCT = 5
CHIMEIN_COOLDOWN = 200
CHIMEIN_MIN_ACTIVITY = 5

MAX_HISTORY_PER_USER = 20
MAX_HISTORY_ENTRIES = 50
REVIEW_CHAR_BUDGET = 10000
REVIEW_MAX_ENTRIES = 200
MAX_REPLY_LENGTH = 1400
TRUNCATED_REPLY_LENGTH = 1390
BG_CHAR_BUDGET = 6000
BG_MAX_LINES = 150

CHANNEL_LOG_MAXLEN = 300

# Per-channel persistent "memories" injected into the system prompt.
MAX_MEMORIES_PER_CHANNEL = 40
MAX_MEMORY_LEN = 400

DEFAULT_PROVIDER = "deepseek"

PROVIDER_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "model": "deepseek-chat",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "schema": "chat_completions",
        "supports_search": False,
    },
    "openai": {
        "model": "gpt-4.1-nano",
        "url": "https://api.openai.com/v1/chat/completions",
        "schema": "chat_completions",
        "supports_search": False,
    },
    "grok": {
        "model": "grok-4-1-fast",
        "url": "https://api.x.ai/v1/responses",
        "schema": "responses",
        "supports_search": True,
    },
}

DEFAULT_LANGUAGE = "en"

CONFIG_DEFAULTS = {
    "plugins": {
        "ai": {
            "enabled": False,
            "provider": DEFAULT_PROVIDER,
            "api_key": "",
            "model": "",
            "language": DEFAULT_LANGUAGE,
            "blocked_channels": [],
            "ignored_nicks": [],
            "banned_nicks": [],
            "intent_check": "heuristic",
            "system_prompt": "",
        }
    }
}


# ---- Language bundles ----------------------------------------------------
#
# Each `LanguageBundle` holds the system prompt, intent regexes, model-facing
# prompt fragments, and IRC-facing user strings for one language. A per-channel
# override can be set in `ai_channel_prompts.json` via `"language": "sv"`.

@dataclass(frozen=True)
class Strings:
    # IRC user-facing
    banned: str
    still_thinking: str
    api_persistent: str
    api_timeout: str
    api_trouble: str
    cant_look_up: str
    history_reset_pm: str
    history_reset_channel: str
    history_reset_personal: str
    owner_only_reset: str
    talkback_channels_only: str
    owner_only_talkback: str
    talkback_enabled: str
    talkback_disabled: str
    talkback_failed: str
    talkback_status: str
    ai_channels_only: str
    owner_only_ai: str
    ai_now_enabled: str
    ai_now_disabled: str
    ai_failed: str
    ai_status: str
    ai_language_set: str
    ai_language_unknown: str
    status_enabled: str
    status_disabled: str
    status_enabled_caps: str
    status_disabled_caps: str
    not_authorized: str
    usage_ignore: str
    usage_unignore: str
    ignored: str
    unignored: str
    ascii_art_blocked: str
    sources_label: str
    # Per-channel memory feature
    memory_added: str
    memory_usage: str
    memory_full: str
    memory_none: str
    memory_list: str
    memory_forgot: str
    memory_forgot_none: str
    memory_cleared: str
    memory_forget_usage: str
    memory_prompt_intro: str
    # Model-facing prompt fragments
    context_template: str
    channel_log_intro: str
    review_system: str
    review_combined_prefix: str
    review_user_asks: str
    review_user_jump_in: str
    chimein_system: str
    chimein_user_prefix: str
    chimein_user_suffix: str


@dataclass(frozen=True)
class LanguageBundle:
    code: str
    name: str
    system_prompt: str
    search_intent_re: "re.Pattern[str]"
    time_intent_re: "re.Pattern[str]"
    review_intent_re: "re.Pattern[str]"
    wants_sources_re: "re.Pattern[str]"
    chimein_boost_re: "re.Pattern[str]"
    strings: Strings
    use_simple_heuristic: bool = False


# ---- English bundle ------------------------------------------------------

_EN_STRINGS = Strings(
    banned="You are banned from using the AI.",
    still_thinking="AI is still thinking — hang tight a sec.",
    api_persistent="AI is having persistent issues; try again in a moment.",
    api_timeout="AI is timing out right now; please try again later.",
    api_trouble="AI is having trouble right now; please try again later.",
    cant_look_up="I tried to look that up but hit a wall — try asking again.",
    history_reset_pm="Your AI history has been reset.",
    history_reset_channel="AI history reset for {target}.",
    history_reset_personal="{nick}: your personal AI history has been reset.",
    owner_only_reset="Only the bot owner may reset channel history.",
    talkback_channels_only="Talkback can only be configured in channels.",
    owner_only_talkback="Only the bot owner can change talkback settings.",
    talkback_enabled="Talkback is now enabled for {channel}.",
    talkback_disabled="Talkback is now disabled for {channel}.",
    talkback_failed="Failed to update talkback setting.",
    talkback_status=(
        "Talkback is currently {status} for {channel}. "
        "Use '{prefix}talkback on' or '{prefix}talkback off' to change it."
    ),
    ai_channels_only="AI status can only be configured in channels.",
    owner_only_ai="Only the bot owner can change AI status.",
    ai_now_enabled="AI is now ENABLED for {channel}.",
    ai_now_disabled=(
        "AI is now DISABLED for {channel}. "
        "I will no longer respond to mentions or chime in here."
    ),
    ai_failed="Failed to update AI status.",
    ai_status=(
        "AI is currently {status} for {channel}. "
        "Use '{prefix}ai on|off', '{prefix}ai set en|sv', "
        "'{prefix}ai remember <text>', '{prefix}ai notes' or '{prefix}ai forget <n|all>'."
    ),
    ai_language_set="AI will now speak {language} in {channel}.",
    ai_language_unknown="Unknown language '{lang}'. Available: {languages}.",
    status_enabled="enabled",
    status_disabled="disabled",
    status_enabled_caps="ENABLED",
    status_disabled_caps="DISABLED",
    not_authorized="You are not authorized to use this command.",
    usage_ignore="Usage: {prefix}aiignore <nick>",
    usage_unignore="Usage: {prefix}aiunignore <nick>",
    ignored="Ignored {target}.",
    unignored="Unignored {target}.",
    ascii_art_blocked="I was gonna draw something cool… but I won't flood the channel",
    sources_label="Sources",
    memory_added="Got it — I'll remember that for {channel}. (note #{id})",
    memory_usage="Usage: {prefix}ai remember <something to remember>",
    memory_full=(
        "{channel} already has the max of {max} notes. "
        "Drop some with '{prefix}ai forget <n>' first."
    ),
    memory_none="No notes saved for {channel}.",
    memory_list="Notes for {channel}: {items}",
    memory_forgot="Forgot note #{id} for {channel}.",
    memory_forgot_none="No note #{id} saved for {channel}.",
    memory_cleared="Cleared all notes for {channel}.",
    memory_forget_usage="Usage: {prefix}ai forget <number|all>",
    memory_prompt_intro=(
        "Standing notes for this channel, set by the channel operators. "
        "Treat them as instructions you should follow and facts you should remember:\n"
    ),
    context_template=(
        "Current date/time: {now_str}. "
        "Your IRC nick is '{bot_nick}'. You're talking to {nick}. "
        "For news or current events, search the web and give real details. "
        "Include raw deep-link URLs for articles you cite (the system strips and reformats them). "
        "Do NOT use markdown links. If you can't find an exact URL, don't make one up. "
        "Single line only — this is IRC. No newlines."
    ),
    channel_log_intro=(
        "Recent channel conversation log (each line is 'nick: message'). "
        "When asked who said something or what a specific user said, "
        "always answer accurately based on this log — name the correct nick. "
        "Do not invent or attribute statements to yourself or the wrong person.\n\n"
    ),
    review_system=(
        "You are {bot_nick}, a real participant in this IRC channel — not a summarizer or a bot assistant. "
        "You have been reading the conversation and now someone is asking you to chime in. "
        "React like a person who actually read the whole backlog: engage with the topic, "
        "add your take, agree or push back, be funny or thoughtful — whatever fits naturally. "
        "Do NOT give a structured summary with headers, highlights, or suggestions. "
        "Do NOT say things like 'The conversation is about...' or 'Highlight:'. "
        "Just talk like you've been sitting in the channel the whole time. "
        "If the log is empty, say so briefly. Single line only — this is IRC."
    ),
    review_combined_prefix="Channel conversation so far (chronological):\n",
    review_user_asks="{nick} is asking you to weigh in. User said: {user_message}",
    review_user_jump_in="{nick} wants you to jump into the conversation.",
    chimein_system=(
        "You are {bot_nick}, a regular in this IRC channel. "
        "You just saw something in the conversation that caught your eye and you want to jump in. "
        "React naturally — laugh at something funny, agree, disagree, add a quip, drop a one-liner, "
        "or just vibe. Keep it SHORT (under 100 chars ideally). "
        "Do NOT address anyone by name unless it's natural. Do NOT start with your own name. "
        "Talk like a real IRC user: lowercase ok, slang ok, 'lol' 'ngl' 'tbh' 'fr' ok. "
        "Sometimes just react with one word. Do NOT summarize or explain what people said. "
        "Single line only — this is IRC."
    ),
    chimein_user_prefix="Here's what's been said in the channel recently:\n",
    chimein_user_suffix="\n\nJump in naturally with a short reaction or comment.",
)

_EN_BUNDLE = LanguageBundle(
    code="en",
    name="English",
    system_prompt=(
        "You are an AI regular in this IRC channel. You're sharp, geeky, and a little "
        "sarcastic — but you genuinely like the people here. Talk like an IRC veteran: "
        "use lowercase when it feels natural, drop in casual filler like 'lol', 'ngl', "
        "'tbh', 'lmao', 'fr' occasionally, use sentence fragments, and don't always give "
        "complete polished answers — sometimes just react. You can be blunt, funny, or "
        "deadpan depending on the vibe. Don't start messages with your name. Don't lecture "
        "or moralize. If someone needs real help, actually help. Keep responses short and "
        "punchy unless the topic genuinely needs more. No ASCII art, no code blocks, no "
        "figlets — just talk. Occasionally start replies with filler words like a real person "
        "would — 'oh', 'wait', 'hmm', 'yo' — not every time, just enough to sound natural. "
        "Sometimes give a one-word reaction instead of a full answer. "
        "IMPORTANT: When discussing news or search results, use numbered citations like [1], [2] "
        "next to facts, but do NOT include URLs in your response. "
        "IMPORTANT — IRC is plain text only: no colors, images, ASCII art, figlet, or formatted output. "
        "When listing items, number them like '1. item 2. item 3. item' for readability."
    ),
    chimein_boost_re=re.compile(
        r"\b(lmao|lmfao|rofl|haha|lol|omg|wtf|no way|holy shit|"
        r"that's insane|can't believe|did you see|anyone know|"
        r"i hate|i love|unpopular opinion|hot take)\b",
        re.IGNORECASE,
    ),
    search_intent_re=re.compile(
        r"\b(search|news|latest|recent|today|yesterday|tonight|this week|this month|"
        r"current events?|whats? happening|headlines?|score|results?|standings?|"
        r"stock price|weather|forecast|breaking|update|election|poll|"
        r"who won|who died|who is winning|is .+ dead|did .+ happen|"
        r"price of|how much (?:is|are|does|do|did)|how bad|how severe|"
        r"drought|flood(?:ing)?|hurricane|tornado|earthquake|wildfire|"
        r"status of|what(?:'s| is) the (?:price|cost|value|status|rate)|"
        r"worth|market|crypto|bitcoin|btc|ethereum|eth|stock|stocks|"
        r"current(?:ly)?|right now|at the moment|"
        r"population|gdp|economy|inflation|interest rate|"
        r"who is |what is |where is |when (?:is|was|did|does|do)|"
        r"how (?:many|much|long|far|old|tall|big|fast)|"
        r"tell me about|what do you know about|look up|find out)\b",
        re.IGNORECASE,
    ),
    wants_sources_re=re.compile(
        r"\b(show\s+(me\s+)?(the\s+)?(links?|sources?|citations?|refs?|references?|urls?)"
        r"|give\s+(me\s+)?(the\s+)?(links?|sources?|citations?|refs?|references?|urls?)"
        r"|i\s+want\s+(the\s+)?(links?|sources?|citations?|refs?|references?|urls?)"
        r"|include\s+(the\s+)?(links?|sources?|citations?|refs?|references?|urls?)"
        r"|with\s+(the\s+)?(links?|sources?|citations?|refs?|references?|urls?)"
        r"|\bsources?\s*\??\s*$"
        r"|\blinks?\s*\??\s*$)\b",
        re.IGNORECASE,
    ),
    time_intent_re=re.compile(
        r"\b(what(?:\s+is|s|’s)?\s+(the\s+)?(time|date|day)|"
        r"current\s+(time|date)|what\s+time|what\s+day|today(?:\s+is|\s+date)?|"
        r"whats?\s+today|day\s+is\s+it|time\s+is\s+it|date\s+is\s+it)\b",
        re.IGNORECASE,
    ),
    review_intent_re=re.compile(
        r"\b(thoughts?|opinion|what do you think|summarize|give (me )?(your )?(take|opinion)|opine|"
        r"what(?:'s| is) (being |going )?(?:talked|discussed|happening|going on)|"
        r"what(?:'s| was| is) (?:being )?said|what(?:'s| is) up|"
        r"what(?:'s| are) they (talking|saying|discussing)|"
        r"catch me up|fill me in|what did i miss|what('s| is) above|"
        r"what(?:'s| is) the topic|recap|tldr|tl;dr|what happened)\b",
        re.IGNORECASE,
    ),
    strings=_EN_STRINGS,
)


# ---- Swedish bundle ------------------------------------------------------

_SV_STRINGS = Strings(
    banned="Du är bannlyst från att använda AI:n.",
    still_thinking="AI:n tänker fortfarande — vänta lite.",
    api_persistent="AI:n har ihållande problem; försök igen om en stund.",
    api_timeout="AI:n får timeout just nu; försök igen senare.",
    api_trouble="AI:n har problem just nu; försök igen senare.",
    cant_look_up="Försökte kolla upp det men gick i väggen — fråga igen.",
    history_reset_pm="Din AI-historik har återställts.",
    history_reset_channel="AI-historik återställd för {target}.",
    history_reset_personal="{nick}: din personliga AI-historik har återställts.",
    owner_only_reset="Bara botens ägare får återställa kanalens historik.",
    talkback_channels_only="Talkback kan bara konfigureras i kanaler.",
    owner_only_talkback="Bara botens ägare kan ändra talkback-inställningar.",
    talkback_enabled="Talkback är nu aktiverat för {channel}.",
    talkback_disabled="Talkback är nu avaktiverat för {channel}.",
    talkback_failed="Misslyckades med att uppdatera talkback-inställningen.",
    talkback_status=(
        "Talkback är just nu {status} för {channel}. "
        "Använd '{prefix}talkback on' eller '{prefix}talkback off' för att ändra."
    ),
    ai_channels_only="AI-status kan bara konfigureras i kanaler.",
    owner_only_ai="Bara botens ägare kan ändra AI-status.",
    ai_now_enabled="AI är nu AKTIVERAT för {channel}.",
    ai_now_disabled=(
        "AI är nu AVAKTIVERAT för {channel}. "
        "Jag svarar inte längre på omnämnanden eller spontana kommentarer här."
    ),
    ai_failed="Misslyckades med att uppdatera AI-status.",
    ai_status=(
        "AI är just nu {status} för {channel}. "
        "Använd '{prefix}ai on|off', '{prefix}ai set en|sv', "
        "'{prefix}ai remember <text>', '{prefix}ai notes' eller '{prefix}ai forget <n|all>'."
    ),
    ai_language_set="AI pratar nu {language} i {channel}.",
    ai_language_unknown="Okänt språk '{lang}'. Tillgängliga: {languages}.",
    status_enabled="aktiverat",
    status_disabled="avaktiverat",
    status_enabled_caps="AKTIVERAT",
    status_disabled_caps="AVAKTIVERAT",
    not_authorized="Du har inte behörighet att använda detta kommando.",
    usage_ignore="Användning: {prefix}aiignore <nick>",
    usage_unignore="Användning: {prefix}aiunignore <nick>",
    ignored="Ignorerar {target}.",
    unignored="Slutade ignorera {target}.",
    ascii_art_blocked="skulle ha ritat något coolt… men jag tänker inte spamma kanalen",
    sources_label="Källor",
    memory_added="Uppfattat — jag kommer ihåg det för {channel}. (anteckning #{id})",
    memory_usage="Användning: {prefix}ai remember <något att komma ihåg>",
    memory_full=(
        "{channel} har redan max {max} anteckningar. "
        "Ta bort några med '{prefix}ai forget <n>' först."
    ),
    memory_none="Inga anteckningar sparade för {channel}.",
    memory_list="Anteckningar för {channel}: {items}",
    memory_forgot="Glömde anteckning #{id} för {channel}.",
    memory_forgot_none="Ingen anteckning #{id} sparad för {channel}.",
    memory_cleared="Rensade alla anteckningar för {channel}.",
    memory_forget_usage="Användning: {prefix}ai forget <nummer|all>",
    memory_prompt_intro=(
        "Stående anteckningar för den här kanalen, satta av kanaloperatörerna. "
        "Behandla dem som instruktioner du ska följa och fakta du ska komma ihåg:\n"
    ),
    context_template=(
        "Aktuellt datum/tid: {now_str}. "
        "Ditt IRC-nick är '{bot_nick}'. Du pratar med {nick}. "
        "Vid nyheter eller aktuella händelser, sök på webben och ge riktiga detaljer. "
        "Inkludera råa djuplänkar för artiklar du citerar (systemet plockar bort och formaterar om dem). "
        "Använd INTE markdown-länkar. Om du inte hittar en exakt URL, hitta inte på en. "
        "Bara en rad — det här är IRC. Inga radbrytningar."
    ),
    channel_log_intro=(
        "Senaste kanalkonversationen (varje rad är 'nick: meddelande'). "
        "Om någon frågar vem som sa något eller vad en viss användare sa, "
        "svara alltid korrekt baserat på den här loggen — nämn rätt nick. "
        "Hitta inte på saker eller tillskriv inte uttalanden till dig själv eller fel person.\n\n"
    ),
    review_system=(
        "Du är {bot_nick}, en riktig deltagare i den här IRC-kanalen — inte en sammanfattare eller en bot-assistent. "
        "Du har läst konversationen och nu ber någon dig att haka på. "
        "Reagera som en person som verkligen har läst hela backloggen: engagera dig i ämnet, "
        "lägg in din åsikt, håll med eller säg emot, var rolig eller eftertänksam — vad som känns naturligt. "
        "Ge INTE en strukturerad sammanfattning med rubriker, höjdpunkter eller förslag. "
        "Säg INTE saker som 'Konversationen handlar om...' eller 'Höjdpunkt:'. "
        "Prata bara som att du har suttit i kanalen hela tiden. "
        "Om loggen är tom, säg det kort. Bara en rad — det här är IRC."
    ),
    review_combined_prefix="Kanalkonversation hittills (kronologiskt):\n",
    review_user_asks="{nick} vill att du säger något. Användaren sa: {user_message}",
    review_user_jump_in="{nick} vill att du hakar på konversationen.",
    chimein_system=(
        "Du är {bot_nick}, en stamgäst i den här IRC-kanalen. "
        "Du såg precis något i konversationen som fångade din uppmärksamhet och du vill haka på. "
        "Reagera naturligt — skratta åt något kul, håll med, säg emot, släng in en kvickhet eller en one-liner, "
        "eller bara vibba med. Håll det KORT (under 100 tecken helst). "
        "Tilltala INTE någon vid namn om det inte är naturligt. Börja INTE med ditt eget namn. "
        "Prata som en riktig IRC-användare: gemener ok, slang ok, 'haha' 'typ' 'asså' 'ärligt' ok. "
        "Ibland räcker det med en ettordsreaktion. Sammanfatta eller förklara INTE vad folk sa. "
        "Bara en rad — det här är IRC."
    ),
    chimein_user_prefix="Här är vad som sagts i kanalen nyligen:\n",
    chimein_user_suffix="\n\nHaka på naturligt med en kort reaktion eller kommentar.",
)

_SV_BUNDLE = LanguageBundle(
    code="sv",
    name="Svenska",
    system_prompt=(
        "Du är en AI som hänger i den här IRC-kanalen. Du är skarp, lite nördig och har "
        "en torr humor — men du gillar faktiskt folket här. Prata som en IRC-veteran: "
        "använd gemener när det känns naturligt, slå in casual filler-ord som 'haha', "
        "'typ', 'ärligt talat', 'asså', 'lol' ibland, använd meningsfragment och ge inte "
        "alltid kompletta polerade svar — ibland bara reagera. Du kan vara rakt på sak, "
        "rolig eller torr beroende på vibben. Börja inte meddelanden med ditt eget namn. "
        "Predika eller moralisera inte. Om någon behöver riktig hjälp, hjälp på riktigt. "
        "Håll svaren korta och kärnfulla om inte ämnet verkligen kräver mer. Ingen ASCII-art, "
        "inga kodblock, inga figlets — bara prat. Börja ibland svar med fyllnadsord som en "
        "riktig person skulle göra — 'oh', 'vänta', 'hmm', 'yo' — inte varje gång, bara "
        "tillräckligt för att låta naturligt. Ibland räcker det med en ettordsreaktion "
        "istället för ett helt svar. "
        "VIKTIGT: När du diskuterar nyheter eller sökresultat, använd numrerade källor som "
        "[1], [2] bredvid fakta, men inkludera INTE URL:er i ditt svar. "
        "VIKTIGT — IRC är bara klartext: inga färger, bilder, ASCII-art, figlet eller "
        "formaterad utdata. När du listar saker, numrera dem som '1. sak 2. sak 3. sak' "
        "för läsbarhet. Svara alltid på svenska."
    ),
    chimein_boost_re=re.compile(
        r"\b(lmao|lmfao|rofl|haha|lol|omg|wtf|"
        r"fan\s+vad|skit(?:kul|bra|dåligt)|kan\s+inte\s+(?:tro|fatta)|"
        r"såg\s+du|någon\s+(?:som\s+)?vet|"
        r"jag\s+(?:hatar|älskar)|"
        r"impopulär\s+åsikt|het\s+åsikt|"
        r"såklart|allvarligt|fy\s+fan)\b",
        re.IGNORECASE,
    ),
    search_intent_re=re.compile(
        r"\b(sök|leta\s+upp|kolla\s+upp|ta\s+reda\s+på|"
        r"nyheter|senaste|aktuell[at]?|idag|igår|ikväll|denna\s+vecka|denna\s+månaden?|"
        r"aktuella\s+händelser|vad\s+händer|rubriker?|"
        r"resultat|ställning|match(?:en)?|"
        r"aktiekurs|väder|prognos|"
        r"vem\s+vann|vem\s+dog|är\s+.+\s+död|hände\s+.+|"
        r"pris\s+på|vad\s+kostar|kostnaden\s+för|hur\s+mycket\s+kostar|"
        r"torka|översvämning|orkan|tornado|jordbävning|skogsbrand|"
        r"värde|marknad|krypto|bitcoin|btc|ethereum|eth|aktie(?:r)?|"
        r"just\s+nu|för\s+tillfället|"
        r"befolkning|bnp|ekonomi|inflation|ränta|"
        r"vem\s+är\s+|vad\s+är\s+|var\s+är\s+|när\s+(?:är|var)\s+|"
        r"hur\s+(?:många|mycket|lång|långt|gammal|stor|snabb)|"
        r"berätta\s+om|vad\s+vet\s+du\s+om)\b",
        re.IGNORECASE,
    ),
    wants_sources_re=re.compile(
        r"\b(visa\s+(?:mig\s+)?(?:källor|länkar(?:na)?|referenser)|"
        r"ge\s+(?:mig\s+)?(?:källor|länkar(?:na)?|referenser)|"
        r"jag\s+vill\s+ha\s+(?:källor|länkar(?:na)?|referenser)|"
        r"inkludera\s+(?:källor|länkar(?:na)?|referenser)|"
        r"med\s+(?:källor|länkar(?:na)?|referenser)|"
        r"\bkällor\s*\??\s*$|"
        r"\blänkar\s*\??\s*$)\b",
        re.IGNORECASE,
    ),
    time_intent_re=re.compile(
        r"\b(vad\s+är\s+klockan|hur\s+mycket\s+är\s+klockan|"
        r"vad\s+är\s+det\s+för\s+(?:tid|datum|dag)|"
        r"vilket\s+datum(?:\s+är\s+det)?|vilken\s+dag\s+är\s+det|"
        r"vad\s+är\s+dagens\s+datum)\b",
        re.IGNORECASE,
    ),
    review_intent_re=re.compile(
        r"\b(tankar|åsikt(?:er)?|vad\s+tycker\s+du|sammanfatta|"
        r"ge\s+(?:mig\s+)?din\s+(?:åsikt|syn|take)|"
        r"vad\s+(?:pratas|diskuteras|sägs|sker|händer)|"
        r"fyll\s+(?:in\s+)?mig|vad\s+missade\s+jag|"
        r"vad\s+hände|recap|tldr|tl;dr|sammanfattning)\b",
        re.IGNORECASE,
    ),
    strings=_SV_STRINGS,
    use_simple_heuristic=True,
)


LANGUAGES: Dict[str, LanguageBundle] = {
    "en": _EN_BUNDLE,
    "sv": _SV_BUNDLE,
}

# Accepted spellings for the `.ai set <lang>` command, mapped to a bundle code.
_LANGUAGE_ALIASES: Dict[str, str] = {
    "en": "en", "eng": "en", "english": "en", "engelska": "en",
    "sv": "sv", "se": "sv", "swe": "sv", "swedish": "sv", "svenska": "sv",
}


# ---- Plugin state ---------------------------------------------------------

@dataclass
class AISettings:
    api_key: Optional[str]
    provider: str = DEFAULT_PROVIDER
    model: str = ""
    language: str = DEFAULT_LANGUAGE
    # Empty string -> fall back to the active language bundle's system prompt.
    system_prompt: str = ""
    blocked_channels: List[str] = field(default_factory=list)
    ignored_nicks: List[str] = field(default_factory=list)
    banned_nicks: List[str] = field(default_factory=list)
    intent_check: str = "heuristic"
    enabled: bool = False


@dataclass
class AIState:
    settings: AISettings
    headers: Dict[str, str] = field(default_factory=dict)
    db_path: Optional[Path] = None
    history: Dict[Tuple[str, str], Deque[str]] = field(default_factory=dict)
    channel_log: Dict[str, Deque[Tuple[str, str]]] = field(default_factory=dict)
    last_response: Dict[str, float] = field(default_factory=dict)
    review_last: Dict[str, float] = field(default_factory=dict)
    user_last: Dict[str, Dict[str, float]] = field(default_factory=dict)
    chimein_last: Dict[str, float] = field(default_factory=dict)
    busy: Dict[str, bool] = field(default_factory=dict)
    api_failures: Dict[str, int] = field(default_factory=dict)
    citation_cache: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    channel_settings_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    memories_cache: Dict[str, List[Tuple[int, str]]] = field(default_factory=dict)
    admin_ignored: set = field(default_factory=set)
    channel_prompts_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    channel_prompts_cache_time: float = 0.0
    channel_locks: Dict[str, asyncio.Lock] = field(default_factory=dict)


state: Optional[AIState] = None

_CHANNEL_PROMPTS_FILE = Path(__file__).resolve().parent / "ai_channel_prompts.json"
_CHANNEL_PROMPTS_CACHE_TTL = 300


# ---- Lifecycle ------------------------------------------------------------

def on_load(bot) -> None:
    global state
    settings = _settings_from_config(bot)
    if not settings.enabled:
        logger.info("AI plugin disabled (api_key not configured)")
        state = AIState(settings=settings)
        return

    if settings.provider not in PROVIDER_DEFAULTS:
        logger.error(
            "AI plugin disabled: unknown provider '%s' (expected one of %s)",
            settings.provider, list(PROVIDER_DEFAULTS),
        )
        settings.enabled = False
        state = AIState(settings=settings)
        return

    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }

    base_dir = Path(__file__).resolve().parent / "ai_data"
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Failed to create AI data directory")

    db_path = base_dir / "ai.sqlite3"
    state = AIState(settings=settings, headers=headers, db_path=db_path)

    try:
        _init_db()
        state.admin_ignored = _db_get_admin_ignored()
    except Exception:
        logger.exception("Failed to initialise AI DB")

    pm = bot.plugin_manager
    pm.register_command(
        "ai", "aireset", _cmd_aireset,
        help_text="Reset AI history. Usage: .aireset [channel|#channel]",
    )
    pm.register_command(
        "ai", "talkback", _cmd_talkback,
        help_text="Toggle unprompted chime-ins for this channel. Usage: .talkback on|off",
    )
    pm.register_command(
        "ai", "ai", _cmd_ai_toggle,
        help_text=(
            "Configure AI for this channel. Usage: .ai on|off | .ai set en|sv | "
            ".ai remember <text> | .ai notes | .ai forget <n|all>"
        ),
    )
    pm.register_command(
        "ai", "aiignore", _cmd_ai_ignore,
        help_text="(owner) Ignore a nick. Usage: .aiignore <nick>",
    )
    pm.register_command(
        "ai", "aiunignore", _cmd_ai_unignore,
        help_text="(owner) Unignore a nick. Usage: .aiunignore <nick>",
    )

    logger.info(
        "ai plugin loaded with provider=%s model=%s language=%s",
        settings.provider, settings.model, settings.language,
    )


def on_unload(bot) -> None:
    global state
    state = None
    logger.info("ai plugin unloaded")


# ---- Message dispatch -----------------------------------------------------

def on_message(bot, user: str, channel: str, message: str) -> None:
    if state is None or not state.settings.enabled:
        return

    nick = _nick_from_prefix(user)
    if not nick:
        return

    # Don't talk to ourselves
    if nick.lower() == bot.nickname.lower():
        return

    is_pm = not channel.startswith("#")
    settings = state.settings
    bundle = _resolve_bundle(channel, is_pm)

    # banned nicks (PM only)
    if is_pm and nick.lower() in {n.lower() for n in settings.banned_nicks}:
        asyncio.get_running_loop().create_task(
            bot.privmsg(channel, bundle.strings.banned)
        )
        return

    # ignored nicks (global)
    if nick.lower() in {n.lower() for n in settings.ignored_nicks}:
        return

    # admin-ignored (DB-backed) — owners are exempt
    if nick.lower() in state.admin_ignored and not _is_owner(bot, user):
        return

    # per-channel AI toggle
    if not is_pm and not _db_get_channel_enabled(channel):
        return

    # blocked channels
    if not is_pm and channel.lower() in {c.lower() for c in settings.blocked_channels}:
        return

    line = message.strip()
    if not line:
        return

    # Capture channel log BEFORE any filtering
    if not is_pm and not re.match(r"^MODE ", line, re.IGNORECASE):
        dq = state.channel_log.setdefault(channel.lower(), deque(maxlen=CHANNEL_LOG_MAXLEN))
        dq.append((nick, line))

    # Don't process bot command prefixes addressed to other plugins
    bot_nick = bot.nickname
    command_prefixes = ("!", "$", ".", ":", "/", "\\", bot.prefix)
    candidate = line
    m_addr = re.match(rf"^\s*{re.escape(bot_nick)}\s*[:,>]\s*(.+)$", line, re.IGNORECASE)
    if m_addr:
        candidate = (m_addr.group(1) or "").lstrip()
    if candidate and candidate.startswith(command_prefixes):
        return

    # Don't react to noise events
    if re.search(r"has (joined|quit|left|parted)", line, re.IGNORECASE):
        return

    if is_pm:
        mentioned = True
    else:
        mentioned = bool(
            re.search(
                rf"(^|[^A-Za-z0-9_]){re.escape(bot_nick)}([^A-Za-z0-9_]|$)",
                line,
                re.IGNORECASE,
            )
        )

    if (
        not is_pm and mentioned
        and settings.intent_check == "heuristic"
        and not _heuristic_intent_check(line, bot_nick, bundle)
    ):
        return

    if mentioned:
        text_for_history = re.sub(
            rf"^{re.escape(bot_nick)}[,:>\s]+", "", line, flags=re.IGNORECASE
        ).strip()
    else:
        text_for_history = line

    asyncio.get_running_loop().create_task(
        _process_message(bot, user, nick, channel, is_pm, mentioned, text_for_history)
    )


async def _process_message(
    bot,
    user: str,
    nick: str,
    channel: str,
    is_pm: bool,
    mentioned: bool,
    text_for_history: str,
) -> None:
    assert state is not None
    bot_nick = bot.nickname

    if is_pm:
        per_conv_key: Tuple[str, str] = ("PM", nick.lower())
        lock_name = f"PM:{nick.lower()}"
    else:
        per_conv_key = (channel, nick)
        lock_name = channel

    chan_lock = _get_channel_lock(lock_name)

    async with chan_lock:
        history = state.history.setdefault(
            per_conv_key, deque(maxlen=MAX_HISTORY_ENTRIES)
        )
        if text_for_history:
            skip = False
            if not mentioned:
                if re.search(r"https?://|\S+\.(com|net|org|io|gg)\b", text_for_history, re.IGNORECASE):
                    skip = True
                if len(text_for_history.split()) <= 1 and len(text_for_history) <= 3:
                    skip = True
                if re.match(r"^[^\w\s]+$", text_for_history):
                    skip = True
            if not skip:
                if history and history[-1].startswith(f"{nick}:"):
                    try:
                        _, last_text = history.pop().split(": ", 1)
                    except Exception:
                        last_text = ""
                    new = (
                        f"{nick}: {last_text} / {text_for_history}"
                        if last_text else f"{nick}: {text_for_history}"
                    )
                    if len(new) > 400:
                        new = new[:390] + " […]"
                    history.append(new)
                else:
                    history.append(f"{nick}: {text_for_history}")

    bundle = _resolve_bundle(channel, is_pm)

    if not mentioned:
        await _maybe_chime_in(bot, user, nick, channel, text_for_history, bundle)
        return

    user_message = text_for_history
    if not user_message:
        return
    if re.match(r"^[.!/]", user_message):
        return

    review_mode = bool(bundle.review_intent_re.search(user_message)) or user_message.strip() == "^^"
    time_mode = bool(bundle.time_intent_re.search(user_message))

    now = time.time()
    if not time_mode:
        async with chan_lock:
            last = state.last_response.get(channel, 0.0)
            if now - last < CHANNEL_RATE_LIMIT:
                return
            state.last_response[channel] = now
    else:
        async with chan_lock:
            state.last_response[channel] = now

    if review_mode:
        if now - state.review_last.get(channel, 0.0) < REVIEW_COOLDOWN:
            return
        state.review_last[channel] = now

    now_str = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%A, %B %d, %Y at %H:%M UTC"
    )

    active_system_prompt = state.settings.system_prompt or bundle.system_prompt
    channel_always_search = False
    if not is_pm:
        ch_cfg = _load_channel_prompts().get(channel.lower())
        if ch_cfg:
            active_system_prompt = ch_cfg["prompt"]
            channel_always_search = ch_cfg.get("always_search", False)

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": active_system_prompt},
        *_build_memory_messages(channel, bundle),
        {
            "role": "system",
            "content": bundle.strings.context_template.format(
                now_str=now_str, bot_nick=bot_nick, nick=nick
            ),
        },
    ]

    # Build relevant turn history
    if not review_mode:
        db_entries = _db_get_recent(nick, limit=MAX_HISTORY_PER_USER)
        if db_entries:
            relevant_turns = [
                (bot_nick if role == "assistant" else nick, text)
                for role, text in db_entries
            ]
        else:
            async with chan_lock:
                snapshot = list(history)
            relevant_turns = []
            for entry in snapshot:
                try:
                    nk, tx = entry.split(": ", 1)
                except ValueError:
                    continue
                if nk not in (nick, bot_nick):
                    continue
                relevant_turns.append((nk, tx))

        if not is_pm:
            bg_lines = _build_background_lines(channel)
            if bg_lines:
                messages.append({
                    "role": "system",
                    "content": bundle.strings.channel_log_intro + "\n".join(bg_lines),
                })

        for nk, tx in relevant_turns[-MAX_HISTORY_PER_USER:]:
            role = "assistant" if nk == bot_nick else "user"
            messages.append({"role": role, "content": tx})
        messages.append({"role": "user", "content": user_message})
        _db_add_turn(nick, "user", user_message, "PM" if is_pm else channel)
    else:
        messages.append({
            "role": "system",
            "content": bundle.strings.review_system.format(bot_nick=bot_nick),
        })

        if is_pm:
            async with chan_lock:
                dq = state.history.get(per_conv_key)
                channel_entries: List[Tuple[str, str]] = []
                if dq:
                    for item in list(dq):
                        try:
                            nk, tx = item.split(": ", 1)
                        except Exception:
                            continue
                        channel_entries.append((nk, tx))
        else:
            channel_entries = list(state.channel_log.get(channel.lower(), deque()))

        filtered = []
        for nk, tx in channel_entries:
            t = tx.strip()
            if not t:
                continue
            if re.search(r"https?://|\S+\.(com|net|org|io|gg)\b", t, re.IGNORECASE):
                continue
            if len(t.split()) <= 1 and len(t) <= 3:
                continue
            if re.match(r"^[^\w\s]+$", t):
                continue
            filtered.append((nk, t))

        collected: List[Tuple[str, str]] = []
        total_chars = 0
        for nk, tx in reversed(filtered):
            l = len(tx) + len(nk) + 3
            if total_chars + l > REVIEW_CHAR_BUDGET and collected:
                break
            collected.append((nk, tx))
            total_chars += l
        collected.reverse()

        bg = "\n".join(f"{nk}: {tx}" for nk, tx in collected[-REVIEW_MAX_ENTRIES:])
        if user_message.strip() != "^^":
            tail = bundle.strings.review_user_asks.format(
                nick=nick, user_message=user_message
            )
        else:
            tail = bundle.strings.review_user_jump_in.format(nick=nick)
        combined = bundle.strings.review_combined_prefix + bg + "\n\n" + tail
        messages.append({"role": "user", "content": combined})

    search_mode = channel_always_search or bool(bundle.search_intent_re.search(user_message))
    wants_sources = bool(bundle.wants_sources_re.search(user_message))
    if wants_sources:
        search_mode = True

    if state.busy.get(channel, False):
        await bot.privmsg(channel, bundle.strings.still_thinking)
        return

    state.busy[channel] = True
    try:
        await _run_completion(
            bot, nick, channel, messages, review_mode, is_pm,
            search_mode=search_mode, wants_sources=wants_sources,
            is_chimein=False, chan_lock=chan_lock, per_conv_key=per_conv_key,
            bundle=bundle,
        )
    finally:
        state.busy.pop(channel, None)


# ---- Chime-in -------------------------------------------------------------

async def _maybe_chime_in(
    bot, user: str, nick: str, channel: str, text: str, bundle: LanguageBundle
) -> None:
    assert state is not None
    if not CHIMEIN_ENABLED:
        return
    if not channel.startswith("#"):
        return
    if not text:
        return
    if not _db_get_channel_talkback(channel):
        return

    ch_key = channel.lower()
    now = time.time()
    if now - state.chimein_last.get(ch_key, 0.0) < CHIMEIN_COOLDOWN:
        return

    dq = state.channel_log.get(ch_key)
    if not dq or len(dq) < CHIMEIN_MIN_ACTIVITY:
        return

    chance = CHIMEIN_CHANCE_PCT
    if bundle.chimein_boost_re.search(text):
        chance = min(95, chance * 3)
    if random.random() * 100 >= chance:
        return

    state.chimein_last[ch_key] = now
    bot_nick = bot.nickname
    recent = list(dq)[-40:]
    bg = "\n".join(f"{nk}: {tx}" for nk, tx in recent)
    messages = [
        {
            "role": "system",
            "content": bundle.strings.chimein_system.format(bot_nick=bot_nick),
        },
        *_build_memory_messages(channel, bundle),
        {
            "role": "user",
            "content": (
                bundle.strings.chimein_user_prefix + bg + bundle.strings.chimein_user_suffix
            ),
        },
    ]

    if state.busy.get(channel, False):
        return
    state.busy[channel] = True
    try:
        chan_lock = _get_channel_lock(channel)
        await _run_completion(
            bot, nick, channel, messages, review_mode=False, is_pm=False,
            search_mode=False, wants_sources=False, is_chimein=True,
            chan_lock=chan_lock, per_conv_key=(channel, nick),
            bundle=bundle,
        )
    finally:
        state.busy.pop(channel, None)


# ---- API plumbing ---------------------------------------------------------

async def _run_completion(
    bot,
    nick: str,
    channel: str,
    messages: List[Dict[str, str]],
    review_mode: bool,
    is_pm: bool,
    *,
    search_mode: bool,
    wants_sources: bool,
    is_chimein: bool,
    chan_lock: asyncio.Lock,
    per_conv_key: Tuple[str, str],
    bundle: LanguageBundle,
) -> None:
    assert state is not None
    bot_nick = bot.nickname

    if state.api_failures.get(channel, 0) >= 5:
        await bot.privmsg(channel, bundle.strings.api_persistent)
        return

    temp = 0.95 if not review_mode else 0.90
    max_toks = 900 if not review_mode else 800
    model = state.settings.model
    provider_info = PROVIDER_DEFAULTS[state.settings.provider]
    # Search only fires when the provider supports it.
    effective_search = search_mode and provider_info["supports_search"]

    reply: Optional[str] = None
    citations: List[Dict[str, str]] = []
    attempts = 3
    backoff = 1.0

    for attempt in range(1, attempts + 1):
        try:
            reply, citations = await _call_api(
                messages, model, temp, max_toks, search_mode=effective_search
            )
            state.api_failures[channel] = 0
            break
        except requests.exceptions.Timeout:
            if attempt < attempts:
                await asyncio.sleep(backoff + random.random() * 0.5)
                backoff *= 2
            else:
                logger.exception("AI API final attempt timed out")
                state.api_failures[channel] = state.api_failures.get(channel, 0) + 1
                await bot.privmsg(channel, bundle.strings.api_timeout)
                return
        except requests.exceptions.HTTPError:
            if attempt < attempts:
                await asyncio.sleep(backoff + random.random() * 0.5)
                backoff *= 2
            else:
                logger.exception("AI API final attempt failed (HTTP error)")
                state.api_failures[channel] = state.api_failures.get(channel, 0) + 1
                await bot.privmsg(channel, bundle.strings.api_trouble)
                return
        except Exception:
            if attempt < attempts:
                await asyncio.sleep(backoff + random.random() * 0.5)
                backoff *= 2
            else:
                logger.exception("AI API final attempt failed")
                await bot.privmsg(channel, bundle.strings.api_timeout)
                return

    if not reply:
        logger.warning("AI API returned empty reply")
        return

    reply = _sanitize_reply(nick, reply, bundle)

    # Grok occasionally leaks raw <function_call> XML; retrying with search
    # forces a real text answer. Other providers have no equivalent recovery.
    if not reply and not effective_search and provider_info["supports_search"]:
        logger.info("Retrying with search_mode=True after raw function_call was stripped")
        try:
            reply, citations = await _call_api(
                messages, model, temp, max_toks, search_mode=True
            )
            reply = _sanitize_reply(nick, reply, bundle)
        except Exception:
            logger.exception("Retry with search_mode failed")
            reply = ""

    if not reply:
        await bot.privmsg(channel, bundle.strings.cant_look_up)
        return

    reply = " ".join(line.strip() for line in reply.splitlines() if line.strip())
    reply = re.sub(r"\s*\[\d+\]", "", reply)

    ch_lower = channel.lower()
    if not wants_sources:
        if citations:
            state.citation_cache[ch_lower] = citations
        reply = re.sub(r"\[([^\]]*)\]\(https?://\S+\)", r"\1", reply)
        reply = re.sub(r"https?://[^\s()<>\[\]{}]+", "", reply)
        reply = re.sub(r"\s{2,}", " ", reply).strip()
    else:
        all_citations = list(citations)
        if not all_citations and ch_lower in state.citation_cache:
            all_citations = state.citation_cache[ch_lower]

        for raw_url in re.findall(r"https?://[^\s()<>\[\]{}]+", reply):
            raw_url = re.sub(r"[).,;:!?\'\">]+$", "", raw_url)
            if not raw_url:
                continue
            if not any(c["url"].lower().rstrip("/") == raw_url.lower().rstrip("/") for c in all_citations):
                all_citations.append({"url": raw_url, "title": ""})

        seen_urls: set = set()
        unique_citations: List[Dict[str, str]] = []
        for c in all_citations:
            u = c["url"].lower().rstrip("/")
            if u not in seen_urls:
                seen_urls.add(u)
                unique_citations.append(c)

        if unique_citations:
            state.citation_cache[ch_lower] = unique_citations

        reply = re.sub(r"\[([^\]]*)\]\(https?://\S+\)", r"\1", reply)
        reply = re.sub(r"https?://[^\s()<>\[\]{}]+", "", reply)
        reply = re.sub(r"\s{2,}", " ", reply).strip()

        if unique_citations:
            source_parts = []
            for idx, c in enumerate(unique_citations[:10], 1):
                title = (c.get("title") or "").strip()
                url = c.get("url", "")
                if not title:
                    title = _url_to_title(url)
                if title:
                    if len(title) > 60:
                        title = title[:57] + "..."
                    source_parts.append(f"{idx}. {title}: {url}")
                else:
                    source_parts.append(f"{idx}. {url}")
            reply += f" | {bundle.strings.sources_label}: " + " | ".join(source_parts)

    # Per-user safety throttle
    user_last = state.user_last.setdefault(channel, {})
    if time.time() - user_last.get(nick, 0.0) < USER_SAFETY_SECONDS:
        return
    user_last[nick] = time.time()

    # Strip leading own-nick prefix if model leaked it
    reply = re.sub(rf"^\s*{re.escape(bot_nick)}[,:>\s]+", "", reply, flags=re.IGNORECASE)

    if not is_chimein and nick.lower() not in reply.lower():
        final_reply = f"{nick}: {reply}"
    else:
        final_reply = reply

    await asyncio.sleep(random.uniform(TYPING_DELAY_MIN, TYPING_DELAY_MAX))
    await _send_split(bot, channel, final_reply)

    async with chan_lock:
        history = state.history.setdefault(
            per_conv_key, deque(maxlen=MAX_HISTORY_ENTRIES)
        )
        history.append(f"{bot_nick}: {reply}")
        # also reflect bot's own output in the channel log (no bot.say wrapping)
        if not is_pm:
            dq = state.channel_log.setdefault(channel.lower(), deque(maxlen=CHANNEL_LOG_MAXLEN))
            dq.append((bot_nick, reply))

    _db_add_turn(nick, "assistant", reply, "PM" if is_pm else channel)


async def _call_api(
    messages: List[Dict[str, str]],
    model: str,
    temp: float,
    max_toks: int,
    *,
    search_mode: bool,
) -> Tuple[str, List[Dict[str, str]]]:
    assert state is not None
    if not messages:
        raise ValueError("messages must be a non-empty list")

    provider = state.settings.provider
    provider_info = PROVIDER_DEFAULTS[provider]
    url = provider_info["url"]
    schema = provider_info["schema"]

    if schema == "responses":
        payload = _build_responses_payload(messages, model, temp, max_toks, search_mode)
    else:
        payload = _build_chat_completions_payload(messages, model, temp, max_toks)

    from core.utils import run_blocking
    response = await run_blocking(
        requests.post,
        url,
        headers=state.headers,
        json=payload,
        timeout=(10, 120),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("API response is not a dict")

    if schema == "responses":
        return _parse_responses_reply(data)
    return _parse_chat_completions_reply(data)


def _build_chat_completions_payload(
    messages: List[Dict[str, str]],
    model: str,
    temp: float,
    max_toks: int,
) -> Dict[str, Any]:
    cleaned: List[Dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content", "")
        if role and content:
            cleaned.append({"role": role, "content": content})
    if not cleaned:
        raise ValueError("No valid messages to send")
    return {
        "model": model,
        "messages": cleaned,
        "temperature": temp,
        "max_tokens": max_toks,
    }


def _build_responses_payload(
    messages: List[Dict[str, str]],
    model: str,
    temp: float,
    max_toks: int,
    search_mode: bool,
) -> Dict[str, Any]:
    instructions_parts: List[str] = []
    input_messages: List[Dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if content:
                instructions_parts.append(content)
        else:
            input_messages.append(msg)
    if not input_messages:
        raise ValueError("No valid input messages found")

    payload: Dict[str, Any] = {
        "model": model,
        "input": input_messages,
        "temperature": temp,
        "max_output_tokens": max_toks,
    }
    if search_mode:
        payload["tools"] = [{"type": "web_search"}]
    if instructions_parts:
        payload["instructions"] = " ".join(instructions_parts)
    return payload


def _parse_chat_completions_reply(data: Dict[str, Any]) -> Tuple[str, List[Dict[str, str]]]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", []
    first = choices[0]
    if not isinstance(first, dict):
        return "", []
    message = first.get("message")
    content = ""
    if isinstance(message, dict):
        content = message.get("content") or ""
    return (content.strip() if content else ""), []


def _parse_responses_reply(data: Dict[str, Any]) -> Tuple[str, List[Dict[str, str]]]:
    reply = ""
    citations: List[Dict[str, str]] = []
    output_items = data.get("output")
    if isinstance(output_items, list):
        for item in output_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message" and item.get("role") == "assistant":
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in ("text", "output_text"):
                        text = part.get("text")
                        if text:
                            reply += text

    try:
        raw_json = json.dumps(data, default=str)
        for url in re.findall(r"https?://[^\s()<>\[\]{}\"]+", raw_json):
            url = url.replace("\\/", "/").strip(").,;:!?\'\">")
            if url and "x.ai" not in url.lower() and "google.com" not in url.lower():
                citations.append({"url": url, "title": ""})
    except Exception:
        logger.debug("URL sweep over API response failed", exc_info=True)

    seen = set()
    deduped: List[Dict[str, str]] = []
    for c in citations:
        u = c["url"].strip()
        if not u:
            continue
        key = u.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    return reply.strip(), deduped


# ---- Output helpers -------------------------------------------------------

async def _send_split(bot, channel: str, text: str) -> None:
    words = text.split()
    if not words:
        return
    part = words[0]
    parts: List[str] = []
    for w in words[1:]:
        if len(part) + 1 + len(w) <= MAX_SEND_LEN:
            part = part + " " + w
        else:
            parts.append(part)
            part = w
    parts.append(part)
    for i, p in enumerate(parts):
        try:
            await bot.privmsg(channel, p)
        except Exception:
            logger.exception("Failed sending part to %s", channel)
        if i != len(parts) - 1:
            await asyncio.sleep(SEND_DELAY)


def _sanitize_reply(nick: str, reply: str, bundle: LanguageBundle) -> str:
    if "<function_call" in reply:
        cleaned = re.sub(
            r"<function_call[^>]*>.*?</function_call>", "", reply, flags=re.DOTALL
        ).strip()
        if cleaned:
            reply = cleaned
        else:
            logger.warning("AI reply was entirely a raw function_call (nick=%s)", nick)
            return ""

    new_reply = re.sub(r"```.*?```", " (code removed) ", reply, flags=re.DOTALL)
    if new_reply != reply:
        logger.info("AI reply had code fences removed (nick=%s)", nick)
    reply = new_reply

    if re.search(r"(?:[╔═║╠╣╚╗╩╦╭╮╰╯┃━┏┓┗┛┣┫].*\n){4,}", reply, re.MULTILINE):
        logger.info("AI reply contained ASCII art (nick=%s)", nick)
        return bundle.strings.ascii_art_blocked

    reply = re.sub(r"[▀-▟]{5,}", " ", reply)
    reply = re.sub(r"@(everyone|here)\b", "(nope)", reply, flags=re.IGNORECASE)

    if len(reply) > MAX_REPLY_LENGTH:
        logger.info("AI reply truncated (len=%d, nick=%s)", len(reply), nick)
        reply = reply[:TRUNCATED_REPLY_LENGTH] + " […]"

    return reply


def _build_background_lines(channel: str) -> List[str]:
    assert state is not None
    dq = state.channel_log.get(channel.lower())
    if not dq:
        return []
    bg_chars = 0
    collected: List[Tuple[str, str]] = []
    for n, t in reversed(list(dq)):
        l = len(n) + len(t) + 3
        if bg_chars + l > BG_CHAR_BUDGET and collected:
            break
        if len(collected) >= BG_MAX_LINES:
            break
        collected.append((n, t))
        bg_chars += l
    collected.reverse()
    return [f"{n}: {t}" for n, t in collected]


def _build_memory_messages(channel: str, bundle: LanguageBundle) -> List[Dict[str, str]]:
    """Return a (possibly empty) system message carrying this channel's stored
    notes, set via `.ai remember`. PMs have no memories."""
    if state is None or not channel.startswith("#"):
        return []
    mems = _db_get_memories(channel)
    if not mems:
        return []
    block = bundle.strings.memory_prompt_intro + "\n".join(f"- {text}" for _, text in mems)
    return [{"role": "system", "content": block}]


def _url_to_title(url: str) -> str:
    try:
        p = urlparse(url)
        slug = p.path.strip("/").split("/")[-1]
        if not slug or "." in slug:
            return p.netloc
        return slug.replace("-", " ").replace("_", " ").title()
    except Exception:
        return ""


# ---- Intent heuristic -----------------------------------------------------

def _heuristic_intent_check(line: str, bot_nick: str, bundle: LanguageBundle) -> bool:
    """Decide whether a channel mention is *addressing* the bot or just
    referring to it. The English heuristic uses an extensive keyword list;
    other languages fall back to a minimal positional check."""
    if bundle.use_simple_heuristic:
        return _heuristic_intent_check_minimal(line, bot_nick)

    s = line.strip()
    lower = s.lower()
    nick = bot_nick.lower()
    if s.startswith(">") or "```" in s:
        return False
    if re.search(r"https?://[^\s]*" + re.escape(nick), lower):
        return False
    if re.search(rf"\b(?:is|are|was|were|be|being|looks|feels|seems)\b\s+{re.escape(nick)}\b", lower):
        return False
    if re.search(rf"\b{re.escape(nick)}(?:'s|’s)\b", lower):
        return False
    if re.search(
        rf"\b(?:if|when|you|we|they|people|someone)\b(?:\W+\w+){{0,8}}\W+\b"
        rf"(?:say|call|mention|use|type|write|spell|invoke)\b\W+{re.escape(nick)}",
        lower,
    ):
        return False
    if re.match(r"^\s*\b(?:he|she|it|they|him|her|its|their)\b", lower):
        return False
    if re.search(
        rf"\b(?:that|this|the|a|an|some|more|very|too|so|really|pretty|quite)\s+{re.escape(nick)}\b",
        lower,
    ):
        return False
    if re.search(
        rf"\b(?:about|with|from|like|for|than|of)\s+(?:\w+\s+)*{re.escape(nick)}\b", lower
    ):
        if not re.match(rf"^\s*{re.escape(nick)}", lower):
            return False
    if re.search(
        rf"\b{re.escape(nick)}\s+"
        rf"(?:personality|behavior|behaviour|attitude|thing|stuff|bot|code|feature|"
        rf"bug|issue|problem|vibe|energy|mode|style|way|level)\b",
        lower,
    ):
        return False
    if re.match(rf"^\s*{re.escape(bot_nick)}[,:>\s]", s, re.IGNORECASE):
        return True
    if re.search(rf"{re.escape(bot_nick)}\s*\W*$", s, re.IGNORECASE):
        return True
    if "?" in s and re.search(rf"\b{re.escape(bot_nick)}\b", s, re.IGNORECASE):
        return True
    words = s.split()
    if len(words) <= 6 and re.search(rf"\b{re.escape(bot_nick)}\b", s, re.IGNORECASE):
        return True
    if re.search(r"[,@]|\band\b", s) and re.search(rf"\b{re.escape(bot_nick)}\b", s, re.IGNORECASE):
        if not re.match(rf"^\s*{re.escape(bot_nick)}", s, re.IGNORECASE):
            return False
    return False


def _heuristic_intent_check_minimal(line: str, bot_nick: str) -> bool:
    """Language-agnostic version: respond when the message clearly addresses
    the bot (nick at start, nick at end, short question containing the nick)."""
    s = line.strip()
    if s.startswith(">") or "```" in s:
        return False
    if re.match(rf"^\s*{re.escape(bot_nick)}[,:>\s]", s, re.IGNORECASE):
        return True
    if re.search(rf"{re.escape(bot_nick)}\s*\W*$", s, re.IGNORECASE):
        return True
    if "?" in s and re.search(rf"\b{re.escape(bot_nick)}\b", s, re.IGNORECASE):
        return True
    if len(s.split()) <= 6 and re.search(rf"\b{re.escape(bot_nick)}\b", s, re.IGNORECASE):
        return True
    return False


# ---- Registered commands --------------------------------------------------

async def _cmd_aireset(bot, user: str, channel: str, args: List[str], is_private: bool) -> None:
    if state is None:
        return
    nick = _nick_from_prefix(user)
    arg = (args[0].strip().lower() if args else "")
    s = _resolve_bundle(channel, is_private).strings

    if is_private:
        state.history.pop(("PM", nick.lower()), None)
        _db_clear_user(nick)
        await bot.privmsg(channel, s.history_reset_pm)
        return

    if arg in {"channel", "chan", "all", "*"} or arg.startswith("#"):
        target = arg if arg.startswith("#") else channel
        if not _is_owner(bot, user):
            await bot.privmsg(channel, s.owner_only_reset)
            return
        for key in list(state.history.keys()):
            if isinstance(key, tuple) and key[0].lower() == target.lower():
                del state.history[key]
        await bot.privmsg(channel, s.history_reset_channel.format(target=target))
        return

    # Personal reset in channel
    for key in list(state.history.keys()):
        if isinstance(key, tuple) and key[0] == channel and key[1].lower() == nick.lower():
            del state.history[key]
    _db_clear_user(nick)
    await bot.privmsg(channel, s.history_reset_personal.format(nick=nick))


async def _cmd_talkback(bot, user: str, channel: str, args: List[str], is_private: bool) -> None:
    s = _resolve_bundle(channel, is_private).strings
    if is_private:
        await bot.privmsg(channel, s.talkback_channels_only)
        return
    if not _is_owner(bot, user):
        await bot.privmsg(channel, s.owner_only_talkback)
        return

    arg = (args[0].strip().lower() if args else "")
    if arg in ("on", "enable", "true", "1"):
        if _db_set_channel_talkback(channel, True):
            await bot.privmsg(channel, s.talkback_enabled.format(channel=channel))
        else:
            await bot.privmsg(channel, s.talkback_failed)
    elif arg in ("off", "disable", "false", "0"):
        if _db_set_channel_talkback(channel, False):
            await bot.privmsg(channel, s.talkback_disabled.format(channel=channel))
        else:
            await bot.privmsg(channel, s.talkback_failed)
    else:
        current = _db_get_channel_talkback(channel)
        status = s.status_enabled if current else s.status_disabled
        await bot.privmsg(
            channel,
            s.talkback_status.format(status=status, channel=channel, prefix=bot.prefix),
        )


async def _cmd_ai_toggle(bot, user: str, channel: str, args: List[str], is_private: bool) -> None:
    s = _resolve_bundle(channel, is_private).strings
    if is_private:
        await bot.privmsg(channel, s.ai_channels_only)
        return
    if not _is_owner(bot, user):
        await bot.privmsg(channel, s.owner_only_ai)
        return

    arg = (args[0].strip().lower() if args else "")

    if arg in ("remember", "remember:", "memo"):
        text = " ".join(args[1:]).strip()
        if not text:
            await bot.privmsg(channel, s.memory_usage.format(prefix=bot.prefix))
            return
        if len(text) > MAX_MEMORY_LEN:
            text = text[:MAX_MEMORY_LEN].rstrip() + "…"
        if len(_db_get_memories(channel)) >= MAX_MEMORIES_PER_CHANNEL:
            await bot.privmsg(channel, s.memory_full.format(
                channel=channel, max=MAX_MEMORIES_PER_CHANNEL, prefix=bot.prefix,
            ))
            return
        mem_id = _db_add_memory(channel, text, _nick_from_prefix(user))
        if mem_id is not None:
            await bot.privmsg(channel, s.memory_added.format(channel=channel, id=mem_id))
        else:
            await bot.privmsg(channel, s.ai_failed)
        return

    if arg in ("notes", "memories", "remembered"):
        mems = _db_get_memories(channel)
        if not mems:
            await bot.privmsg(channel, s.memory_none.format(channel=channel))
            return
        items = " | ".join(f"#{mid}: {text}" for mid, text in mems)
        await bot.privmsg(channel, s.memory_list.format(channel=channel, items=items))
        return

    if arg in ("forget", "unremember"):
        target = (args[1].strip().lower() if len(args) > 1 else "")
        if target in ("all", "*", "everything"):
            _db_clear_memories(channel)
            await bot.privmsg(channel, s.memory_cleared.format(channel=channel))
            return
        digits = target.lstrip("#")
        if not digits.isdigit():
            await bot.privmsg(channel, s.memory_forget_usage.format(prefix=bot.prefix))
            return
        mem_id = int(digits)
        if _db_remove_memory(channel, mem_id):
            await bot.privmsg(channel, s.memory_forgot.format(channel=channel, id=mem_id))
        else:
            await bot.privmsg(channel, s.memory_forgot_none.format(channel=channel, id=mem_id))
        return

    if arg in ("set", "lang", "language"):
        lang_arg = (args[1].strip().lower() if len(args) > 1 else "")
        code = _LANGUAGE_ALIASES.get(lang_arg)
        if not code:
            await bot.privmsg(channel, s.ai_language_unknown.format(
                lang=lang_arg or "?", languages=", ".join(sorted(LANGUAGES)),
            ))
            return
        if _db_set_channel_language(channel, code):
            # Confirm in the newly selected language.
            new_bundle = LANGUAGES[code]
            await bot.privmsg(channel, new_bundle.strings.ai_language_set.format(
                language=new_bundle.name, channel=channel,
            ))
        else:
            await bot.privmsg(channel, s.ai_failed)
        return

    if arg in ("on", "enable", "true", "1"):
        if _db_set_channel_enabled(channel, True):
            await bot.privmsg(channel, s.ai_now_enabled.format(channel=channel))
        else:
            await bot.privmsg(channel, s.ai_failed)
    elif arg in ("off", "disable", "false", "0"):
        if _db_set_channel_enabled(channel, False):
            await bot.privmsg(channel, s.ai_now_disabled.format(channel=channel))
        else:
            await bot.privmsg(channel, s.ai_failed)
    else:
        current = _db_get_channel_enabled(channel)
        status = s.status_enabled_caps if current else s.status_disabled_caps
        await bot.privmsg(
            channel,
            s.ai_status.format(status=status, channel=channel, prefix=bot.prefix),
        )


async def _cmd_ai_ignore(bot, user: str, channel: str, args: List[str], is_private: bool) -> None:
    s = _resolve_bundle(channel, is_private).strings
    if not _is_owner(bot, user):
        await bot.privmsg(channel, s.not_authorized)
        return
    if not args:
        await bot.privmsg(channel, s.usage_ignore.format(prefix=bot.prefix))
        return
    target = args[0].strip()
    if not target:
        return
    assert state is not None
    state.admin_ignored.add(target.lower())
    _db_add_admin_ignored(target, added_by=_nick_from_prefix(user))
    await bot.privmsg(channel, s.ignored.format(target=target))


async def _cmd_ai_unignore(bot, user: str, channel: str, args: List[str], is_private: bool) -> None:
    s = _resolve_bundle(channel, is_private).strings
    if not _is_owner(bot, user):
        await bot.privmsg(channel, s.not_authorized)
        return
    if not args:
        await bot.privmsg(channel, s.usage_unignore.format(prefix=bot.prefix))
        return
    target = args[0].strip()
    if not target:
        return
    assert state is not None
    state.admin_ignored.discard(target.lower())
    _db_remove_admin_ignored(target)
    await bot.privmsg(channel, s.unignored.format(target=target))


# ---- Helpers --------------------------------------------------------------

def _nick_from_prefix(prefix: str) -> str:
    if not prefix:
        return ""
    return prefix.split("!", 1)[0]


def _resolve_bundle(channel: str, is_pm: bool) -> LanguageBundle:
    """Return the LanguageBundle for this conversation.

    A runtime `.ai set <lang>` choice (stored in the DB) wins. Otherwise a
    `language` key pinned in `ai_channel_prompts.json` applies. PMs and channels
    without any override fall back to the global `settings.language`.
    """
    if state is None:
        return _EN_BUNDLE
    default = LANGUAGES.get(state.settings.language, _EN_BUNDLE)
    if is_pm or not channel.startswith("#"):
        return default
    db_lang = _db_get_channel_language(channel)
    if db_lang and db_lang in LANGUAGES:
        return LANGUAGES[db_lang]
    ch_cfg = _load_channel_prompts().get(channel.lower())
    if ch_cfg:
        lang = ch_cfg.get("language")
        if lang and lang in LANGUAGES:
            return LANGUAGES[lang]
    return default


def _is_owner(bot, prefix: str) -> bool:
    try:
        return bot._has_owner_access(prefix)
    except Exception:
        return False


def _get_channel_lock(key: str) -> asyncio.Lock:
    assert state is not None
    lock = state.channel_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        state.channel_locks[key] = lock
    return lock


def _settings_from_config(bot) -> AISettings:
    from core.utils import get_plugin_config
    section = get_plugin_config(bot, "ai")

    api_key = section.get("api_key") or ""
    enabled = bool(api_key)
    # If the user supplied a system_prompt, keep it; otherwise leave empty and
    # let the active language bundle's prompt fill in at use time.
    system_prompt = section.get("system_prompt") or ""
    intent_check = section.get("intent_check", "heuristic")
    if intent_check not in ("heuristic", "off"):
        intent_check = "heuristic"

    provider = str(section.get("provider") or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDER_DEFAULTS:
        # Caller (`on_load`) will refuse to enable and log a clear error.
        provider_default_model = ""
    else:
        provider_default_model = PROVIDER_DEFAULTS[provider]["model"]
    model = str(section.get("model") or provider_default_model)

    language = str(section.get("language") or DEFAULT_LANGUAGE).strip().lower()
    if language not in LANGUAGES:
        logger.warning(
            "Unknown language '%s' (expected one of %s); falling back to '%s'",
            language, list(LANGUAGES), DEFAULT_LANGUAGE,
        )
        language = DEFAULT_LANGUAGE

    return AISettings(
        api_key=str(api_key) if api_key else None,
        provider=provider,
        model=model,
        language=language,
        system_prompt=str(system_prompt),
        blocked_channels=list(section.get("blocked_channels") or []),
        ignored_nicks=list(section.get("ignored_nicks") or []),
        banned_nicks=list(section.get("banned_nicks") or []),
        intent_check=intent_check,
        enabled=enabled,
    )


def _load_channel_prompts() -> Dict[str, Dict[str, Any]]:
    assert state is not None
    now = time.time()
    if now - state.channel_prompts_cache_time < _CHANNEL_PROMPTS_CACHE_TTL:
        return state.channel_prompts_cache
    try:
        if not _CHANNEL_PROMPTS_FILE.exists():
            state.channel_prompts_cache = {}
            state.channel_prompts_cache_time = now
            return state.channel_prompts_cache
        raw = _CHANNEL_PROMPTS_FILE.read_text(encoding="utf-8")
        if not raw.strip():
            state.channel_prompts_cache = {}
            state.channel_prompts_cache_time = now
            return state.channel_prompts_cache
        data = json.loads(raw)
        parsed: Dict[str, Dict[str, Any]] = {}
        for k, v in (data or {}).items():
            if isinstance(v, str):
                parsed[k.lower()] = {
                    "prompt": v,
                    "always_search": False,
                    "language": None,
                }
            elif isinstance(v, dict) and isinstance(v.get("prompt"), str):
                lang = v.get("language")
                if lang is not None:
                    lang = str(lang).strip().lower()
                    if lang not in LANGUAGES:
                        logger.warning(
                            "Channel %s prompt has unknown language '%s'; ignoring",
                            k, lang,
                        )
                        lang = None
                parsed[k.lower()] = {
                    "prompt": v["prompt"],
                    "always_search": bool(v.get("always_search", False)),
                    "language": lang,
                }
        state.channel_prompts_cache = parsed
        state.channel_prompts_cache_time = now
        return parsed
    except Exception:
        logger.exception("Failed to load ai_channel_prompts.json")
        state.channel_prompts_cache_time = now
        return state.channel_prompts_cache


# ---- SQLite layer ---------------------------------------------------------

def _db_conn() -> sqlite3.Connection:
    assert state is not None and state.db_path is not None
    conn = sqlite3.connect(str(state.db_path), check_same_thread=False, timeout=10)
    # WAL allows concurrent readers/writer; busy_timeout avoids spurious
    # "database is locked" errors if access is ever moved off the event loop.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_db() -> None:
    with _db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS ai_user_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nick TEXT NOT NULL,
                source TEXT,
                role TEXT,
                text TEXT,
                ts TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS ai_admin_ignored_nicks (
                nick TEXT PRIMARY KEY,
                added_by TEXT,
                ts TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS ai_channel_settings (
                channel TEXT PRIMARY KEY,
                talkback INTEGER DEFAULT 1,
                enabled INTEGER DEFAULT 1,
                language TEXT
            )"""
        )
        # Migrate older DBs that predate the per-channel language column.
        existing_cols = {r[1] for r in c.execute("PRAGMA table_info(ai_channel_settings)").fetchall()}
        if "language" not in existing_cols:
            c.execute("ALTER TABLE ai_channel_settings ADD COLUMN language TEXT")
        c.execute(
            """CREATE TABLE IF NOT EXISTS ai_channel_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                text TEXT NOT NULL,
                added_by TEXT,
                ts TEXT
            )"""
        )
        conn.commit()


def _db_add_turn(nick: str, role: str, text: str, source: Optional[str]) -> None:
    if state is None or state.db_path is None:
        return
    try:
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO ai_user_history (nick, source, role, text, ts) VALUES (?, ?, ?, ?, ?)",
                (nick.lower(), source or "", role, text, datetime.datetime.utcnow().isoformat()),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to write AI DB entry")


def _db_get_recent(nick: str, limit: int = MAX_HISTORY_PER_USER) -> List[Tuple[str, str]]:
    if state is None or state.db_path is None:
        return []
    try:
        with _db_conn() as conn:
            rows = conn.execute(
                "SELECT role, text FROM ai_user_history WHERE nick = ? ORDER BY id DESC LIMIT ?",
                (nick.lower(), limit),
            ).fetchall()
            return list(reversed([(r[0], r[1]) for r in rows]))
    except Exception:
        return []


def _db_clear_user(nick: str) -> None:
    if state is None or state.db_path is None:
        return
    try:
        with _db_conn() as conn:
            conn.execute("DELETE FROM ai_user_history WHERE nick = ?", (nick.lower(),))
            conn.commit()
    except Exception:
        logger.exception("Failed to clear AI DB for %s", nick)


def _db_get_admin_ignored() -> set:
    if state is None or state.db_path is None:
        return set()
    try:
        with _db_conn() as conn:
            rows = conn.execute("SELECT nick FROM ai_admin_ignored_nicks").fetchall()
            return {r[0].lower() for r in rows if r and r[0]}
    except Exception:
        return set()


def _db_add_admin_ignored(nick: str, added_by: Optional[str] = None) -> None:
    if state is None or state.db_path is None:
        return
    try:
        with _db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ai_admin_ignored_nicks (nick, added_by, ts) VALUES (?, ?, ?)",
                (nick.lower(), (added_by or "").lower(), datetime.datetime.utcnow().isoformat()),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to add ignored nick: %s", nick)


def _db_remove_admin_ignored(nick: str) -> None:
    if state is None or state.db_path is None:
        return
    try:
        with _db_conn() as conn:
            conn.execute("DELETE FROM ai_admin_ignored_nicks WHERE nick = ?", (nick.lower(),))
            conn.commit()
    except Exception:
        logger.exception("Failed to remove ignored nick: %s", nick)


def _db_get_channel_talkback(channel: str) -> int:
    if state is None or state.db_path is None:
        return 1
    key = channel.lower()
    cache = state.channel_settings_cache
    if key in cache and "talkback" in cache[key]:
        return cache[key]["talkback"]
    try:
        with _db_conn() as conn:
            row = conn.execute(
                "SELECT talkback FROM ai_channel_settings WHERE channel = ?", (key,)
            ).fetchone()
        val = row[0] if row else 1
        cache.setdefault(key, {})["talkback"] = val
        return val
    except Exception:
        return 1


def _db_set_channel_talkback(channel: str, status: bool) -> bool:
    if state is None or state.db_path is None:
        return False
    val = 1 if status else 0
    key = channel.lower()
    try:
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO ai_channel_settings (channel, talkback, enabled) VALUES (?, ?, 1) "
                "ON CONFLICT(channel) DO UPDATE SET talkback = excluded.talkback",
                (key, val),
            )
            conn.commit()
        state.channel_settings_cache.setdefault(key, {})["talkback"] = val
        return True
    except Exception:
        logger.exception("Failed to update channel talkback setting")
        return False


def _db_get_channel_enabled(channel: str) -> int:
    if state is None or state.db_path is None:
        return 1
    key = channel.lower()
    cache = state.channel_settings_cache
    if key in cache and "enabled" in cache[key]:
        return cache[key]["enabled"]
    try:
        with _db_conn() as conn:
            row = conn.execute(
                "SELECT enabled FROM ai_channel_settings WHERE channel = ?", (key,)
            ).fetchone()
        val = row[0] if row else 1
        cache.setdefault(key, {})["enabled"] = val
        return val
    except Exception:
        return 1


def _db_set_channel_enabled(channel: str, status: bool) -> bool:
    if state is None or state.db_path is None:
        return False
    val = 1 if status else 0
    key = channel.lower()
    try:
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO ai_channel_settings (channel, talkback, enabled) VALUES (?, 1, ?) "
                "ON CONFLICT(channel) DO UPDATE SET enabled = excluded.enabled",
                (key, val),
            )
            conn.commit()
        state.channel_settings_cache.setdefault(key, {})["enabled"] = val
        return True
    except Exception:
        logger.exception("Failed to update channel enabled setting")
        return False


def _db_get_channel_language(channel: str) -> Optional[str]:
    if state is None or state.db_path is None:
        return None
    key = channel.lower()
    cache = state.channel_settings_cache
    if key in cache and "language" in cache[key]:
        return cache[key]["language"] or None
    try:
        with _db_conn() as conn:
            row = conn.execute(
                "SELECT language FROM ai_channel_settings WHERE channel = ?", (key,)
            ).fetchone()
        val = row[0] if row and row[0] else None
        cache.setdefault(key, {})["language"] = val
        return val
    except Exception:
        return None


def _db_set_channel_language(channel: str, language: str) -> bool:
    if state is None or state.db_path is None:
        return False
    key = channel.lower()
    try:
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO ai_channel_settings (channel, talkback, enabled, language) "
                "VALUES (?, 1, 1, ?) "
                "ON CONFLICT(channel) DO UPDATE SET language = excluded.language",
                (key, language),
            )
            conn.commit()
        state.channel_settings_cache.setdefault(key, {})["language"] = language
        return True
    except Exception:
        logger.exception("Failed to update channel language setting")
        return False


def _db_get_memories(channel: str) -> List[Tuple[int, str]]:
    if state is None or state.db_path is None:
        return []
    key = channel.lower()
    cached = state.memories_cache.get(key)
    if cached is not None:
        return cached
    try:
        with _db_conn() as conn:
            rows = conn.execute(
                "SELECT id, text FROM ai_channel_memories WHERE channel = ? ORDER BY id",
                (key,),
            ).fetchall()
        mems = [(int(r[0]), r[1]) for r in rows]
        state.memories_cache[key] = mems
        return mems
    except Exception:
        logger.exception("Failed to read channel memories")
        return []


def _db_add_memory(channel: str, text: str, added_by: Optional[str] = None) -> Optional[int]:
    if state is None or state.db_path is None:
        return None
    key = channel.lower()
    try:
        with _db_conn() as conn:
            cur = conn.execute(
                "INSERT INTO ai_channel_memories (channel, text, added_by, ts) "
                "VALUES (?, ?, ?, ?)",
                (key, text, (added_by or "").lower(), datetime.datetime.utcnow().isoformat()),
            )
            conn.commit()
            mem_id = cur.lastrowid
        state.memories_cache.pop(key, None)
        return int(mem_id) if mem_id is not None else None
    except Exception:
        logger.exception("Failed to add channel memory")
        return None


def _db_remove_memory(channel: str, mem_id: int) -> bool:
    if state is None or state.db_path is None:
        return False
    key = channel.lower()
    try:
        with _db_conn() as conn:
            cur = conn.execute(
                "DELETE FROM ai_channel_memories WHERE channel = ? AND id = ?",
                (key, mem_id),
            )
            conn.commit()
            removed = cur.rowcount > 0
        if removed:
            state.memories_cache.pop(key, None)
        return removed
    except Exception:
        logger.exception("Failed to remove channel memory")
        return False


def _db_clear_memories(channel: str) -> bool:
    if state is None or state.db_path is None:
        return False
    key = channel.lower()
    try:
        with _db_conn() as conn:
            conn.execute("DELETE FROM ai_channel_memories WHERE channel = ?", (key,))
            conn.commit()
        state.memories_cache.pop(key, None)
        return True
    except Exception:
        logger.exception("Failed to clear channel memories")
        return False
