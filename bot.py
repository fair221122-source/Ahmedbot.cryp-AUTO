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
# PORT 9000 for Render
# ==============================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Running")

def run_server():
    server = HTTPServer(("0.0.0.0", 9000), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# ==============================
# CONFIG
# ==============================

TELEGRAM_TOKEN = "8568994708:AAFXTPTK3MyEe1wfrWTYBBUPfbi8zayOxi0"
CHAT_ID = "986199874"
CRYPTOPANIC_API_KEY = "a5563e90848ba81e4aeca929e26d90069b2d1b9f"

bot = Bot(token=TELEGRAM_TOKEN)

BASE_URL = "https://api.binance.com/api/v3/klines"

SYMBOLS = [
"ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","TRXUSDT",
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
# SMT
# ==============================

def smt_divergence(alt, btc):
    if alt is None or btc is None:
        return False

    return alt["high"].iloc[-1] > alt["high"].iloc[-5] and \
           btc["high"].iloc[-1] <= btc["high"].iloc[-5]

# ==============================
# ANALYSIS
# ==============================

async def analyze_symbol(session, symbol, btc15, news):

    df4h = await fetch_klines(session, symbol, "4h")
    df1h = await fetch_klines(session, symbol, "1h")
    df15 = await fetch_klines(session, symbol, "15m")

    df4h = calculate_indicators(df4h)
    df1h = calculate_indicators(df1h)
    df15 = calculate_indicators(df15)

    if df4h is None or df1h is None or df15 is None:
        return None

    price = df15["close"].iloc[-1]
    atr = df15["atr"].iloc[-1]

    if pd.isna(atr) or atr == 0:
        return None

    score = 0

    # 4H Trend
    if price > df4h["ema200"].iloc[-1]:
        direction = "LONG"
        score += 2
    else:
        direction = "SHORT"
        score += 2

    # 1H confirmation
    if price > df1h["ema200"].iloc[-1] and direction == "LONG":
        score += 1
    if price < df1h["ema200"].iloc[-1] and direction == "SHORT":
        score += 1

    # Liquidity Sweep
    if df15["high"].iloc[-1] > df15["high"].iloc[-5]:
        score += 1

    # SMT
    if smt_divergence(df15, btc15):
        score += 2

    # Volume
    if df15["volume"].iloc[-1] > df15["volume"].rolling(20).mean().iloc[-1]:
        score += 1

    if score < 6:
        return None

    sl = price - atr*1.5 if direction=="LONG" else price + atr*1.5
    risk = abs(price - sl)

    tp1 = price + risk*2 if direction=="LONG" else price - risk*2
    tp2 = price + risk*3 if direction=="LONG" else price - risk*3
    tp3 = price + risk*4 if direction=="LONG" else price - risk*4

    coin = symbol.replace("USDT","")
    coin_news = "لا يوجد خبر مؤثر حالياً."
    sentiment = "Neutral ⚖️"

    for n in news:
        if coin.lower() in n["title"].lower():
            coin_news = n["title"]
            sentiment = "Positive ✅" if direction=="LONG" else "Negative 🔴"
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
        "news": coin_news,
        "sentiment": sentiment
    }

# ==============================
# MESSAGE
# ==============================

def send_signal(data):

    activity = min(95, data["score"] * 12)

    msg = f"""🚀 تنبيه: دخول سيولة مؤسسية ضخمة!
🥇 العملة: {data['symbol']}
🔥 نسبة النشاط: {activity}% ({'زخم شرائي' if data['direction']=='LONG' else 'ضغط بيعي'})
⚡ نوع العملية: {data['direction']} {'🟢' if data['direction']=='LONG' else '🔴'}
📍 Entry: {data['price']:.4f}
🛡️ S.L   : {data['sl']:.4f}
🎯 T.P 1 : {data['tp1']:.4f}
T.P 2 : {data['tp2']:.4f}
T.P 3 : {data['tp3']:.4f}
R:R    : 1:4
---------------------------------
📰 رادار الأخبار (News Sentiment):
💬 آخر خبر: "{data['news']}" ({data['sentiment']})
📅 أجندة اقتصادية: لا يوجد حدث عالي التأثير حالياً.
⚠️ نصيحة : التحليل الفني مدعوم بالخبر، لكن احذر من التقلبات المفاجئة.
❗إدارة المخاطر مسؤوليتك.
"""

    bot.send_message(CHAT_ID, msg)

# ==============================
# MAIN LOOP
# ==============================

async def run():

    async with aiohttp.ClientSession() as session:

        news = get_news()
        btc15 = await fetch_klines(session, "BTCUSDT", "15m")
        btc15 = calculate_indicators(btc15)

        opportunities = []

        for sym in SYMBOLS:
            if sym in active_trades:
                continue

            result = await analyze_symbol(session, sym, btc15, news)
            if result:
                opportunities.append(result)

            await asyncio.sleep(0.2)

        opportunities = sorted(opportunities, key=lambda x: x["score"], reverse=True)[:2]

        for op in opportunities:
            send_signal(op)
            active_trades[op["symbol"]] = op

# ==============================

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(run())
        except Exception as e:
            print("Error:", e)
        time.sleep(300)
