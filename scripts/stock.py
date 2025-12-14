"""Stock ticker plugin using yfinance.

Usage:
  .s <symbol>
  .a <symbol>
"""

import asyncio
import logging
import yfinance as yf

logger = logging.getLogger(__name__)

CONFIG_DEFAULTS = {
    "plugins": {
        "stock": {
            "enabled": True,
            "triggers": ["s", "a", "stock", "aktie"],
        }
    }
}


def on_load(bot):
    triggers = _triggers(bot)
    prefix = getattr(bot, "prefix", ".")
    names = ", ".join(f"{prefix}{trigger}" for trigger in triggers) or "no trigger"
    logger.info("stock plugin loaded; responding to %s", names)


def on_unload(bot):
    logger.info("stock plugin unloaded")


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
            bot.privmsg(channel, f"Usage: {prefix}{command} <symbol>")
        )
        return

    symbol = msg_parts[1].upper()
    
    # Run network call in a separate thread to avoid blocking the bot's loop
    asyncio.get_running_loop().create_task(
        _fetch_and_reply(bot, channel, symbol)
    )


async def _fetch_and_reply(bot, channel, symbol):
    try:
        # Run blocking yfinance code in executor
        loop = asyncio.get_running_loop()
        ticker_data = await loop.run_in_executor(None, _get_stock_data, symbol)
        
        if ticker_data:
            price = ticker_data['price']
            currency = ticker_data['currency']
            name = ticker_data.get('shortName') or symbol
            display_symbol = ticker_data.get('symbol', symbol)
            
            await bot.privmsg(channel, f"{name} ({display_symbol}): {price} {currency}")
        else:
            await bot.privmsg(channel, f"Could not find data for {symbol}")
            
    except Exception as e:
        logger.error(f"Error fetching stock data for {symbol}: {e}")
        await bot.privmsg(channel, f"Error fetching data for {symbol}")


import requests

# ... (existing imports)

def _get_stock_data(symbol):
    """Blocking function to get stock data."""
    # 1. Try to search if it doesn't look like a standard ticker (optional, or just fallback)
    # But user wants "apple" -> AAPL. "apple" is not a valid ticker usually, or data won't be found.
    # Let's try to search first if it's not a short alphanumeric string, or just search if direct lookup fails?
    # Actually, yfinance Ticker("apple") might fail or return nothing.
    # Let's try a search first if the user provides something that looks like a name, 
    # OR better: always search if we want robust "best match".
    # However, searching adds latency.
    # Compromise: Try direct ticker first (fast), if no price, try search api, then get price for result.
    
    # Actually, "apple" is 5 chars, could be a ticker in some markets.
    # Let's try the search API to resolve the symbol if we aren't sure.
    # A simple approach: invoke search API. If the top result's symbol matches the input case-insensitive, use it.
    # If not, and we can't find data for input, use top result.
    
    # Let's do:
    # 1. Search API to find best match ticker.
    # 2. Get data for that ticker.
    
    # Start with search because "apple" -> AAPL is the goal.
    search_name = None
    try:
        search_url = "https://query2.finance.yahoo.com/v1/finance/search"
        headers = {'User-Agent': 'Mozilla/5.0'}
        params = {'q': symbol, 'quotesCount': 1, 'newsCount': 0}
        
        resp = requests.get(search_url, params=params, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if 'quotes' in data and len(data['quotes']) > 0:
                best_match = data['quotes'][0]
                found_symbol = best_match['symbol']
                symbol = found_symbol 
                search_name = best_match.get('shortname')
    except Exception as e:
        logger.error(f"Search API failed: {e}")
        # Fallback to original symbol.
    
    try:
        ticker = yf.Ticker(symbol)
        if hasattr(ticker, 'fast_info'):
            price = ticker.fast_info.last_price
            currency = ticker.fast_info.currency
            
            # Use search_name if available, else symbol fallback
            name = search_name or symbol
            
            return {
                'price': round(price, 2),
                'currency': currency,
                'shortName': name,
                'symbol': symbol
            }

        info = ticker.info
        if info:
            price = info.get('currentPrice') or info.get('regularMarketPrice')
            if price:
                 return {
                     'price': round(price, 2),
                     'currency': info.get('currency', 'USD'),
                     'shortName': info.get('shortName', search_name or symbol),
                     'symbol': symbol
                 }
        
        return None
    except Exception as e:
        logger.error(f"yfinance error for {symbol}: {e}")
        return None
    except Exception as e:
        logger.error(f"yfinance error for {symbol}: {e}")
        return None


def _triggers(bot):
    try:
        return list(bot.config["plugins"]["stock"]["triggers"])
    except (KeyError, TypeError):
        return list(CONFIG_DEFAULTS["plugins"]["stock"]["triggers"])
