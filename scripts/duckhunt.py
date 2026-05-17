"""DuckHunt plugin ported from Limnoria.

A game where ducks appear and users shoot or befriend them.
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from operator import itemgetter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_NAME = "duckhunt_data.json"

CONFIG_DEFAULTS = {
    "plugins": {
        "duckhunt": {
            "enabled": True,
            "storage_path": DEFAULT_STORAGE_NAME,
            "triggers": [
                "starthunt", "stophunt", "bang", "bef", "score", "huntscore",
                "mergescores", "mergetimes", "rmtime", "rmscore", "dayscores",
                "weekscores", "listscores", "total", "listtimes", "dbg", "listfriends"
            ],
            "autoRestart": False,
            "ducks": 10,
            "minthrottle": 30, # adjusted down from original 5400 for testability
            "maxthrottle": 300, # adjusted down from original 6000 for testability
            "reloadTime": 5,
            "missProbability": 0.2,
            "kickMode": False, # disabled by default
            "perfectbonus": 5,
        }
    }
}

@dataclass
class ChannelData:
    channelscores: Dict[str, int] = field(default_factory=dict)
    channeltimes: Dict[str, float] = field(default_factory=dict)
    channelworsttimes: Dict[str, float] = field(default_factory=dict)
    channelfriends: Dict[str, int] = field(default_factory=dict)
    channelweek: Dict[str, Dict[str, Dict[str, int]]] = field(default_factory=dict) # week -> day -> nick -> score

    def to_dict(self):
        return {
            "channelscores": self.channelscores,
            "channeltimes": self.channeltimes,
            "channelworsttimes": self.channelworsttimes,
            "channelfriends": self.channelfriends,
            "channelweek": self.channelweek,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            channelscores=data.get("channelscores", {}),
            channeltimes=data.get("channeltimes", {}),
            channelworsttimes=data.get("channelworsttimes", {}),
            channelfriends=data.get("channelfriends", {}),
            channelweek=data.get("channelweek", {}),
        )

@dataclass
class HuntState:
    started: bool = False
    duck: bool = False
    ducktype: str = "normal"
    last_spawn: float = 0.0
    
    shoots: int = 0
    scores: Dict[str, int] = field(default_factory=dict)
    toptimes: Dict[str, float] = field(default_factory=dict)
    worsttimes: Dict[str, float] = field(default_factory=dict)
    friends: Dict[str, int] = field(default_factory=dict)
    
    reloading: Dict[str, float] = field(default_factory=dict)
    reloadcount: Dict[str, int] = field(default_factory=dict)
    streaks: Dict[str, int] = field(default_factory=dict)
    huntLeader: Optional[str] = None

@dataclass
class DuckHuntSettings:
    storage_path: Path
    triggers: List[str]
    autoRestart: bool
    ducks: int
    minthrottle: int
    maxthrottle: int
    reloadTime: int
    missProbability: float
    kickMode: bool
    perfectbonus: int

@dataclass
class PluginState:
    settings: DuckHuntSettings
    persistent_data: Dict[str, ChannelData] = field(default_factory=dict) # channel -> data
    hunts: Dict[str, HuntState] = field(default_factory=dict) # channel -> hunt state
    duck_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)

state: Optional[PluginState] = None

def on_load(bot) -> None:
    global state
    settings = _settings_from_config(bot)
    data = _load_data(settings.storage_path)
    state = PluginState(settings=settings, persistent_data=data)
    
    triggers = ", ".join(f"{getattr(bot, 'prefix', '.')}{t}" for t in settings.triggers)
    logger.info("DuckHunt plugin loaded. Storage: %s. Triggers: %s.", settings.storage_path, triggers)

def on_unload(bot) -> None:
    global state
    if state:
        for task in state.duck_tasks.values():
            if not task.done():
                task.cancel()
        _save_data()
        state = None
    logger.info("DuckHunt plugin unloaded")

def _settings_from_config(bot) -> DuckHuntSettings:
    from core.utils import get_plugin_config
    settings = get_plugin_config(bot, "duckhunt")
        
    default_path = Path(__file__).resolve().parent / DEFAULT_STORAGE_NAME
    raw_path = settings.get("storage_path")
    if raw_path:
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            path = (default_path.parent / path).resolve()
        storage_path = path
    else:
        storage_path = default_path
        
    conf_triggers = settings.get("triggers", CONFIG_DEFAULTS["plugins"]["duckhunt"]["triggers"])
    
    return DuckHuntSettings(
        storage_path=storage_path,
        triggers=list(conf_triggers),
        autoRestart=settings.get("autoRestart", False),
        ducks=settings.get("ducks", 10),
        minthrottle=settings.get("minthrottle", 30),
        maxthrottle=settings.get("maxthrottle", 300),
        reloadTime=settings.get("reloadTime", 5),
        missProbability=settings.get("missProbability", 0.2),
        kickMode=settings.get("kickMode", False),
        perfectbonus=settings.get("perfectbonus", 5),
    )

def _load_data(path: Path) -> Dict[str, ChannelData]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return {ch: ChannelData.from_dict(cd) for ch, cd in data.items()}
    except Exception:
        logger.warning("Failed to load duckhunt data from %s", path, exc_info=True)
        return {}

def _save_data() -> None:
    if not state:
        return
    try:
        path = state.settings.storage_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {ch: cd.to_dict() for ch, cd in state.persistent_data.items()}
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        logger.error("Failed to save duckhunt data", exc_info=True)

def _get_channel_data(channel: str) -> ChannelData:
    if channel not in state.persistent_data:
        state.persistent_data[channel] = ChannelData()
    return state.persistent_data[channel]

def on_message(bot, user: str, channel: str, message: str) -> None:
    global state
    if state is None:
        return

    # Check for duck quack directly without prefix
    if message == "🌳🌳🌳 •*´¨`*•.¸¸.•*´¨`*•.¸¸.••*´¨`*•.¸¸ 🦆 QUACK!" or message == "🌳🌳🌳 •*´¨`*•.¸¸.•*´¨`*•.¸¸.••*´¨`*•.¸¸ 🌟 GOLDEN DUCK! 🌟":
        nick = user.split("!", 1)[0]
        if nick != bot.nick:
            msg = f"{nick}, don't pretend to be me!"
            asyncio.create_task(bot.privmsg(channel, msg))
        return

    prefix = getattr(bot, "prefix", ".")
    if not message.startswith(prefix):
        return

    parts = message[len(prefix):].strip().split()
    if not parts:
        return

    trigger = parts[0].lower()
    if trigger not in state.settings.triggers:
        return

    nick = user.split("!", 1)[0]
    args = parts[1:]

    # Handle commands
    if trigger == "starthunt":
        asyncio.create_task(_starthunt(bot, channel))
    elif trigger == "stophunt":
        asyncio.create_task(_stophunt(bot, channel))
    elif trigger == "bang":
        asyncio.create_task(_bang(bot, nick, channel))
    elif trigger == "bef":
        asyncio.create_task(_bef(bot, nick, channel))
    elif trigger == "score":
        target_nick = args[0] if args else nick
        asyncio.create_task(_score(bot, target_nick, channel))
    elif trigger == "huntscore":
        target_nick = args[0] if args else nick
        asyncio.create_task(_huntscore(bot, target_nick, channel))
    elif trigger == "total":
        target_channel = args[0] if args else channel
        asyncio.create_task(_total(bot, target_channel))
    elif trigger == "listscores":
        size = int(args[0]) if args and args[0].isdigit() else 10
        asyncio.create_task(_listscores(bot, size, channel))
    elif trigger == "listtimes":
        size = int(args[0]) if args and args[0].isdigit() else 10
        asyncio.create_task(_listtimes(bot, size, channel))
    elif trigger == "listfriends":
        size = int(args[0]) if args and args[0].isdigit() else 10
        asyncio.create_task(_listfriends(bot, size, channel))
    elif trigger == "dbg":
        asyncio.create_task(_dbg(bot, channel))

async def _starthunt(bot, channel: str):
    if channel not in state.hunts:
        state.hunts[channel] = HuntState()
    
    hunt = state.hunts[channel]
    if hunt.started:
        await bot.privmsg(channel, "✔️ There is already a hunt right now!")
        return
        
    hunt.started = True
    hunt.duck = False
    hunt.shoots = 0
    hunt.scores.clear()
    hunt.toptimes.clear()
    hunt.worsttimes.clear()
    hunt.friends.clear()
    hunt.reloading.clear()
    hunt.reloadcount.clear()
    hunt.streaks.clear()
    hunt.huntLeader = None
    
    # ensure data structure
    _get_channel_data(channel)
    
    if channel in state.duck_tasks:
        state.duck_tasks[channel].cancel()
    
    state.duck_tasks[channel] = asyncio.create_task(_duck_loop(bot, channel))
    
    await bot.privmsg(channel, "✔️ The hunt starts now! 🦆🦆🦆")

async def _duck_loop(bot, channel: str):
    hunt = state.hunts.get(channel)
    while hunt and hunt.started:
        throttle = random.randint(state.settings.minthrottle, state.settings.maxthrottle)
        await asyncio.sleep(throttle)
        
        if not hunt.started:
            break
            
        if not hunt.duck:
            hunt.duck = True
            hunt.last_spawn = time.time()
            is_golden = random.random() < 0.05
            hunt.ducktype = "golden" if is_golden else "normal"
            
            if is_golden:
                await bot.privmsg(channel, "🌳🌳🌳 •*´¨`*•.¸¸.•*´¨`*•.¸¸.••*´¨`*•.¸¸ 🌟 GOLDEN DUCK! 🌟")
            else:
                await bot.privmsg(channel, "🌳🌳🌳 •*´¨`*•.¸¸.•*´¨`*•.¸¸.••*´¨`*•.¸¸ 🦆 QUACK!")

async def _stophunt(bot, channel: str):
    hunt = state.hunts.get(channel)
    if not hunt or not hunt.started:
        await bot.privmsg(channel, "❗ Nothing to stop: there's no hunt right now.")
        return
        
    if channel in state.duck_tasks:
        state.duck_tasks[channel].cancel()
        
    await _end(bot, channel, auto_restart=False)

async def _bang(bot, nick: str, channel: str):
    hunt = state.hunts.get(channel)
    if not hunt or not hunt.started:
        await bot.privmsg(channel, "❗ There is no hunt right now! You can start a hunt with the 'starthunt' command")
        return

    now = time.time()
    bangdelay = now - hunt.last_spawn if hunt.duck else None
    
    # Reloading mechanic
    if nick in hunt.reloading and (now - hunt.reloading[nick] < state.settings.reloadTime):
        if hunt.reloadcount.get(nick, 0) < 1:
            hunt.reloadcount[nick] = hunt.reloadcount.get(nick, 0) + 1
            await bot.privmsg(channel, f"⏳ Reloading... ({state.settings.reloadTime}s)")
            return
        else:
            # Shot self while reloading
            hunt.scores[nick] = hunt.scores.get(nick, 0) - 1
            msg = f"❌ Shot yourself! {nick}: {hunt.scores[nick]}"
            if bangdelay:
                msg += f" ({bangdelay:.2f}s)"
            await bot.privmsg(channel, msg)
            return
            
    hunt.reloading[nick] = now
    hunt.reloadcount[nick] = 0
    
    # Duck handling
    if hunt.duck:
        if random.random() < state.settings.missProbability:
            hunt.streaks[nick] = 0
            await bot.privmsg(channel, "❌ Missed!")
        else:
            points = 1
            is_golden = (hunt.ducktype == "golden")
            if is_golden:
                points += 1
                
            hunt.streaks[nick] = hunt.streaks.get(nick, 0) + 1
            streak = hunt.streaks[nick]
            
            combo_bonus = 0
            if streak >= 6:
                combo_bonus = 2
            elif streak >= 3:
                combo_bonus = 1
                
            points += combo_bonus
            hunt.scores[nick] = hunt.scores.get(nick, 0) + points
            
            # Persistent update
            cd = _get_channel_data(channel)
            cd.channelscores[nick] = cd.channelscores.get(nick, 0) + points
            _save_data()
            
            if is_golden:
                await bot.privmsg(channel, f"🌟 GOLDEN! {nick} +{points}")
            else:
                await bot.privmsg(channel, f"🦆✔️ {nick}: {hunt.scores[nick]} ({bangdelay:.2f}s)")
                
            # Times
            if bangdelay is not None:
                if nick not in hunt.toptimes or bangdelay < hunt.toptimes[nick]:
                    hunt.toptimes[nick] = bangdelay
                if nick not in hunt.worsttimes or bangdelay > hunt.worsttimes[nick]:
                    hunt.worsttimes[nick] = bangdelay
                    
                if nick not in cd.channeltimes or bangdelay < cd.channeltimes[nick]:
                    cd.channeltimes[nick] = bangdelay
                if nick not in cd.channelworsttimes or bangdelay > cd.channelworsttimes[nick]:
                    cd.channelworsttimes[nick] = bangdelay
                _save_data()
                
            hunt.duck = False
            hunt.last_spawn = now
            hunt.shoots += 1
            
            # Leader check
            if hunt.scores:
                leader_nick = max(hunt.scores.items(), key=itemgetter(1))[0]
                if leader_nick != hunt.huntLeader:
                    if hunt.huntLeader:
                        await bot.privmsg(channel, f"🏆 {leader_nick} takes lead! ({hunt.scores[leader_nick]}pts)")
                    else:
                        await bot.privmsg(channel, f"🏆 {leader_nick} leads! ({hunt.scores[leader_nick]}pts)")
                    hunt.huntLeader = leader_nick
                    
            if hunt.shoots >= state.settings.ducks:
                if channel in state.duck_tasks:
                    state.duck_tasks[channel].cancel()
                await _end(bot, channel, auto_restart=state.settings.autoRestart)
    else:
        # Penalty
        hunt.scores[nick] = hunt.scores.get(nick, 0) - 1
        hunt.streaks[nick] = 0
        
        cd = _get_channel_data(channel)
        cd.channelscores[nick] = cd.channelscores.get(nick, 0) - 1
        _save_data()
        
        msg = f"❌ No duck! {nick}: {hunt.scores[nick]}"
        if bangdelay is not None:
             msg += f" ({bangdelay:.2f}s)"
        await bot.privmsg(channel, msg)

async def _bef(bot, nick: str, channel: str):
    hunt = state.hunts.get(channel)
    if not hunt or not hunt.started:
        await bot.privmsg(channel, "❗ There is no hunt right now! You can start a hunt with the 'starthunt' command")
        return

    if hunt.duck:
        if random.random() <= 0.8:
            hunt.friends[nick] = hunt.friends.get(nick, 0) + 1
            cd = _get_channel_data(channel)
            cd.channelfriends[nick] = cd.channelfriends.get(nick, 0) + 1
            _save_data()
            await bot.privmsg(channel, f"🦆❤️ {nick} +1 friend")
        else:
            hunt.friends[nick] = hunt.friends.get(nick, 0) - 1
            cd = _get_channel_data(channel)
            cd.channelfriends[nick] = cd.channelfriends.get(nick, 0) - 1
            _save_data()
            await bot.privmsg(channel, f"💨 {nick} duck flew away! -1")
            
        hunt.duck = False
        hunt.last_spawn = time.time()
    else:
        hunt.friends[nick] = hunt.friends.get(nick, 0) - 1
        cd = _get_channel_data(channel)
        cd.channelfriends[nick] = cd.channelfriends.get(nick, 0) - 1
        _save_data()
        await bot.privmsg(channel, f"😅 {nick} no duck! -1 friend")

async def _end(bot, channel: str, auto_restart: bool):
    hunt = state.hunts.get(channel)
    if not hunt:
        return
        
    hunt.started = False
    cd = _get_channel_data(channel)
    
    if hunt.scores:
        winnernick = max(hunt.scores.items(), key=itemgetter(1))[0]
        winnerscore = hunt.scores[winnernick]
        
        if winnerscore == state.settings.ducks:
            hunt.scores[winnernick] += state.settings.perfectbonus
            cd.channelscores[winnernick] = cd.channelscores.get(winnernick, 0) + state.settings.perfectbonus
            _save_data()
            await bot.privmsg(channel, f"😮 Perfect! {winnernick}: {winnerscore}/{state.settings.ducks} +{state.settings.perfectbonus} 😮")
        else:
            reply = " ".join([f"({n}: {s})" for n, s in sorted(hunt.scores.items(), key=itemgetter(1), reverse=True)])
            if auto_restart:
                await bot.privmsg(channel, f"🦆 {' '.join(reply)}")
            else:
                await bot.privmsg(channel, f"❗ Hunt over! {' '.join(reply)}")
            
        time_parts = []
        if hunt.toptimes:
            key, val = min(hunt.toptimes.items(), key=itemgetter(1))
            record = ""
            if key in cd.channeltimes and val <= cd.channeltimes[key]:
                overall_best = min(cd.channeltimes.values())
                if val <= overall_best:
                    record = " 🏆rec!"
            time_parts.append(f"Best: {key} {val:.2f}s{record}")
        if hunt.worsttimes:
            key, val = max(hunt.worsttimes.items(), key=itemgetter(1))
            record = ""
            if key in cd.channelworsttimes and val >= cd.channelworsttimes[key]:
                overall_worst = max(cd.channelworsttimes.values())
                if val >= overall_worst:
                    record = " slowest!"
            if record:
                time_parts.append(f"Slowest: {key} {val:.2f}s{record}")
        if time_parts:
            await bot.privmsg(channel, f"🕒 {' · '.join(time_parts)}")
    else:
        if not auto_restart:
            await bot.privmsg(channel, "❗ Hunt over! 😮 Not a single duck was shot!")
        else:
            await bot.privmsg(channel, "😮 Not a single duck was shot during this hunt!")
        
    if hunt.friends:
        reply = " ".join([f"({n}: {s})" for n, s in sorted(hunt.friends.items(), key=itemgetter(1), reverse=True)])
        await bot.privmsg(channel, f"❤️ {reply}")
        
    if auto_restart:
        asyncio.create_task(_starthunt_delayed(bot, channel, 5))

async def _starthunt_delayed(bot, channel: str, delay: int):
    await asyncio.sleep(delay)
    await _starthunt(bot, channel)

async def _score(bot, target_nick: str, channel: str):
    cd = _get_channel_data(channel)
    score = cd.channelscores.get(target_nick)
    if score is not None:
        await bot.privmsg(channel, str(score))
    else:
        await bot.privmsg(channel, f"There is no persistent score for {target_nick} on {channel}")

async def _huntscore(bot, target_nick: str, channel: str):
    hunt = state.hunts.get(channel)
    if not hunt or not hunt.started:
        await bot.privmsg(channel, "❗ There is no hunt right now!")
        return
    s = hunt.scores.get(target_nick, 0)
    b = hunt.friends.get(target_nick, 0)
    await bot.privmsg(channel, f"{target_nick} — current hunt: shooting: {s} | befriending: {b}")

async def _total(bot, channel: str):
    cd = _get_channel_data(channel)
    s = sum(cd.channelscores.values())
    b = sum(cd.channelfriends.values())
    await bot.privmsg(channel, f"🦆 {s} shot · ❤️ {b} befriended in {channel}")

async def _listscores(bot, size: int, channel: str):
    cd = _get_channel_data(channel)
    if cd.channelscores:
        scores = sorted(cd.channelscores.items(), key=itemgetter(1), reverse=True)[:size]
        reply = " ".join([f"({n}: {s})" for n, s in scores])
        await bot.privmsg(channel, f"🏆 Top {size}: {reply}")
    else:
        await bot.privmsg(channel, "No scores for this channel yet.")

async def _listtimes(bot, size: int, channel: str):
    cd = _get_channel_data(channel)
    if cd.channeltimes:
        times = sorted(cd.channeltimes.items(), key=itemgetter(1))[:size]
        reply = " ".join([f"({n}: {s:.2f}s)" for n, s in times])
        await bot.privmsg(channel, f"🕒 Fastest: {reply}")
    else:
        await bot.privmsg(channel, "No best times for this channel yet.")

async def _listfriends(bot, size: int, channel: str):
    cd = _get_channel_data(channel)
    if cd.channelfriends:
        friends = sorted(cd.channelfriends.items(), key=itemgetter(1), reverse=True)[:size]
        reply = " ".join([f"({n}: {s})" for n, s in friends])
        await bot.privmsg(channel, f"❤️ Top friends: {reply}")
    else:
        await bot.privmsg(channel, "No friendship records for this channel yet.")

async def _dbg(bot, channel: str):
    # force spawn a duck
    hunt = state.hunts.get(channel)
    if hunt and hunt.started and not hunt.duck:
        hunt.duck = True
        hunt.last_spawn = time.time()
        is_golden = random.random() < 0.05
        hunt.ducktype = "golden" if is_golden else "normal"
        if is_golden:
            await bot.privmsg(channel, "🌳🌳🌳 •*´¨`*•.¸¸.•*´¨`*•.¸¸.••*´¨`*•.¸¸ 🌟 GOLDEN DUCK! 🌟")
        else:
            await bot.privmsg(channel, "🌳🌳🌳 •*´¨`*•.¸¸.•*´¨`*•.¸¸.••*´¨`*•.¸¸ 🦆 QUACK!")

