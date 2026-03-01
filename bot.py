print("NEW VERSION FIXED")

import asyncio
import aiohttp
import pandas as pd
import requests
import time
import os
from telegram import Bot
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# ==============================
# SERVER (Render Keep Alive)
# ==============================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Running")

def start_server():
    port = int(os.environ.get("PORT", 9000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Server running on port {port}")
    server.serve_forever()

# ==============================
# CONFIG
# ==============================

TELEGRAM_TOKEN = "8524445307:AAEDw5THEah-iBwpgsTqvK2Pi7abpzWarZk"
CHAT_ID = "986199874"
CRYPTOPANIC_API_KEY = "a5563e90848ba81e4aeca929e26d90069b2d1b9f"

bot = Bot(token=TELEGRAM_TOKEN)

BASE_URL = "https://api.binance.com/api/v3/klines"

SYMBOLS = [
"BNBUSDT","XRPUSDT","SOLUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","TRXUSDT",
"LTCUSDT","UNIUSDT","XLMUSDT","DOGEUSDT","SHIBUSDT","MATICUSDT","ATOMUSDT","APTUSDT",
"SUIUSDT","FILUSDT","NEARUSDT","ICPUSDT","TONUSDT","BCHUSDT","ARBUSDT","OPUSDT",
"INJUSDT","RNDRUSDT","SEIUSDT","TIAUSDT","STXUSDT","AAVEUSDT","IMXUSDT","DYDXUSDT",
"JUPUSDT","FETUSDT","GALAUSDT","ORDIUSDT","PYTHUSDT","WLDUSDT","TAOUSDT"
]

active_trades = {}

# ==============================
# FETCH DATA
# ==============================

async def fetch_klines(session, symbol, interval, limit=200):
    url = f"{BASE_URL}?symbol={symbol}&interval={interval}&limit={limit}"
    async with session.get(url) as resp:
        data = await resp.json()

        if not isinstance(data, list):
            return None

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "c1","c2","c3","c4","c5","c6"
        ])
        df = df.astype(float)
        return df

# ==============================
# INDICATORS
# ==============================

def calculate_indicators(df):
    if df is None or len(df) < 50:
        return None

    df["ema200"] = df["close"].ewm(span=200).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.rolling(14).mean()

    return df

# ==============================
# NEWS
# ==============================

def get_news():
    try:
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API_KEY}&filter=hot"
        r = requests.get(url, timeout=10).json()
        return r.get("results", [])
    except:
        return []

# ==============================
# ANALYSIS
# ==============================

async def analyze_symbol(session, symbol, market_bias, news):

    df1h = await fetch_klines(session, symbol, "1h")
    df15 = await fetch_klines(session, symbol, "15m")

    df1h = calculate_indicators(df1h)
    df15 = calculate_indicators(df15)

    if df1h is None or df15 is None:
        return None

    price = df15["close"].iloc[-1]
    atr = df15["atr"].iloc[-1]

    if pd.isna(atr) or atr == 0:
        return None

    score = 0

    direction = "LONG" if market_bias == "BULL" else "SHORT"

    if direction == "LONG" and price > df1h["ema200"].iloc[-1]:
        score += 2
    if direction == "SHORT" and price < df1h["ema200"].iloc[-1]:
        score += 2

    if direction == "LONG" and price > df15["ema50"].iloc[-1]:
        score += 1
    if direction == "SHORT" and price < df15["ema50"].iloc[-1]:
        score += 1

    if df15["volume"].iloc[-1] > df15["volume"].rolling(20).mean().iloc[-1]:
        score += 1

    # تم تخفيف الشرط ليعطي إشارات أكثر
    if score < 2:
        return None

    sl = price - atr*1.5 if direction=="LONG" else price + atr*1.5
    risk = abs(price - sl)

    tp1 = price + risk*2 if direction=="LONG" else price - risk*2
    tp2 = price + risk*3 if direction=="LONG" else price - risk*3
    tp3 = price + risk*4 if direction=="LONG" else price - risk*4

    coin = symbol.replace("USDT","")
    coin_news = "لا يوجد خبر مؤثر حالياً."

    for n in news:
        if coin.lower() in n["title"].lower():
            coin_news = n["title"]
            break

    return {
        "symbol": symbol,
        "price": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "direction": direction,
        "score": score,
        "news": coin_news
    }

# ==============================
# MESSAGE
# ==============================

def send_signal(data):

    activity = min(95, data["score"] * 20)

    msg = f"""🚀 تنبيه صفقة جديدة

العملة: {data['symbol']}
النوع: {data['direction']}

سعر الدخول: {data['price']:.4f}
وقف الخسارة: {data['sl']:.4f}

الهدف 1: {data['tp1']:.4f}
الهدف 2: {data['tp2']:.4f}
الهدف 3: {data['tp3']:.4f}

رادار الأخبار:
{data['news']}
"""

    bot.send_message(chat_id=CHAT_ID, text=msg)

# ==============================
# MAIN LOOP
# ==============================

async def run():

    async with aiohttp.ClientSession() as session:

        news = get_news()

        btc = await fetch_klines(session, "BTCUSDT", "1h")
        eth = await fetch_klines(session, "ETHUSDT", "1h")

        btc = calculate_indicators(btc)
        eth = calculate_indicators(eth)

        if btc is None or eth is None:
            return

        # حتى لو السوق محايد سيعمل LONG
        if btc["close"].iloc[-1] > btc["ema200"].iloc[-1] and \
           eth["close"].iloc[-1] > eth["ema200"].iloc[-1]:
            market_bias = "BULL"
        else:
            market_bias = "BULL"

        opportunities = []

        for sym in SYMBOLS:
            result = await analyze_symbol(session, sym, market_bias, news)
            if result:
                opportunities.append(result)
            await asyncio.sleep(0.1)

        opportunities = sorted(opportunities, key=lambda x: x["score"], reverse=True)

        sent = 0
        now = time.time()

        for opp in opportunities:

            if opp["symbol"] in active_trades and now - active_trades[opp["symbol"]] < 3600:
                continue

            send_signal(opp)
            active_trades[opp["symbol"]] = now
            sent += 1

            if sent == 2:
                break

async def main_loop():
    print("Bot Started Successfully")
    while True:
        try:
            await run()
        except Exception as e:
            print("Error:", e)
        await asyncio.sleep(3600)

if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    asyncio.run(main_loop())
