"""Untappd beer lookup plugin.

Usage:
  .untappd <beer name>
  .ut <beer name>
  .beer <beer name>
"""

import asyncio
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

CONFIG_DEFAULTS = {
    "plugins": {
        "untappd": {
            "enabled": True,
            "triggers": ["untappd", "ut", "beer"],
        }
    }
}


def on_load(bot):
    triggers = _triggers(bot)
    prefix = getattr(bot, "prefix", ".")
    names = ", ".join(f"{prefix}{trigger}" for trigger in triggers) or "no trigger"
    logger.info("untappd plugin loaded; responding to %s", names)


def on_unload(bot):
    logger.info("untappd plugin unloaded")


def on_message(bot, user, channel, message):
    prefix = getattr(bot, "prefix", ".")
    if not message.startswith(prefix):
        return

    msg_parts = message[len(prefix) :].strip().split()
    if not msg_parts:
        return

    command = msg_parts[0].lower()
    if command not in _triggers(bot):
        return

    if len(msg_parts) < 2:
        asyncio.get_running_loop().create_task(
            bot.privmsg(channel, f"Usage: {prefix}{command} <beer name>")
        )
        return

    query = " ".join(msg_parts[1:])

    # Run network call in a separate thread
    asyncio.get_running_loop().create_task(
        _fetch_and_reply(bot, channel, query)
    )


async def _fetch_and_reply(bot, channel, query):
    try:
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, _search_untappd, query)

        if not results:
            await bot.privmsg(channel, f"Could not find beer: {query}")
            return

        if len(results) == 1:
            result = results[0]
            name = result['name']
            rating = result['rating']
            url = result['url']
            await bot.privmsg(channel, f"{name} | Rating: {rating} | {url}")
        else:
            names = [r['name'] for r in results]
            names_str = ", ".join(names)
            
            # IRC message limit safety (simple truncation)
            msg_prefix = f"Found {len(results)} beers. Did you mean: "
            max_len = 400 - len(msg_prefix)
            
            if len(names_str) > max_len:
                names_str = names_str[:max_len].rsplit(',', 1)[0] + "..."
                
            await bot.privmsg(channel, f"{msg_prefix}{names_str}")

    except Exception as e:
        logger.error(f"Error fetching untappd data for {query}: {e}")
        await bot.privmsg(channel, f"Error fetching data for {query}")


def _search_untappd(query):
    """Blocking function to search Untappd via Algolia API."""
    url = "https://9wbo4rq3ho-dsn.algolia.net/1/indexes/beer/query"
    params = {
        "x-algolia-agent": "Algolia for JavaScript (3.35.1); Browser (lite)",
        "x-algolia-application-id": "9WBO4RQ3HO",
        "x-algolia-api-key": "1d347324d67ec472bb7132c66aead485"
    }
    
    # Algolia payload
    data = {
        "params": f"query={urllib.parse.quote(query)}&hitsPerPage=15"
    }
    
    try:
        resp = requests.post(url, headers=params, json=data, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"Untappd API returned status {resp.status_code}")
            return []
            
        json_data = resp.json()
        hits = json_data.get('hits', [])
        
        results = []
        for hit in hits:
            name = hit.get('beer_name')
            bid = hit.get('bid')
            rating = hit.get('rating_score')
            brewery = hit.get('brewery_name')
            
            if not name or not bid:
                continue
                
            full_name = f"{brewery} {name}" if brewery else name
            
            # Round rating to 3 decimal places to match previous output style if possible, 
            # or just stringify. The API returns floats.
            if rating:
                 rating = f"({rating:.3f})"
            else:
                 rating = "(N/A)"
            
            results.append({
                "name": full_name,
                "rating": rating,
                "url": f"https://untappd.com/beer/{bid}"
            })
            
        return results

    except Exception as e:
        logger.error(f"API search failed: {e}")
        return []


def _triggers(bot):
    try:
        return list(bot.config["plugins"]["untappd"]["triggers"])
    except (KeyError, TypeError):
        return list(CONFIG_DEFAULTS["plugins"]["untappd"]["triggers"])
