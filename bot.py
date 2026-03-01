import asyncio
import aiohttp
import pandas as pd
import pandas_ta as ta
import requests
import os
import threading
import sys
from telegram import Bot
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==============================
# CONFIG & API KEYS
# ==============================
TELEGRAM_TOKEN = "8524445307:AAEDw5THEah-iBwpgsTqvK2Pi7abpzWarZk"
CHAT_ID = "986199874"
CRYPTOPANIC_API_KEY = "a5563e90848ba81e4aeca929e26d90069b2d1b9f"

bot = Bot(token=TELEGRAM_TOKEN)
BASE_URL = "https://fapi.binance.com/fapi/v1/klines"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","TRXUSDT",
    "LTCUSDT","UNIUSDT","XLMUSDT","DOGEUSDT","SHIBUSDT","MATICUSDT","ATOMUSDT","APTUSDT","SUIUSDT","FILUSDT",
    "NEARUSDT","ICPUSDT","TONUSDT","BCHUSDT","ARBUSDT","OPUSDT","INJUSDT","RNDRUSDT","SEIUSDT","TIAUSDT",
    "STXUSDT","AAVEUSDT","IMXUSDT","DYDXUSDT","JUPUSDT","FETUSDT","GALAUSDT","ORDIUSDT","PYTHUSDT","WLDUSDT"
]

active_positions = {}

# ==============================
# SERVER (Render Critical Fix)
# ==============================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"OK - Scanner is running")

def run_health_server():
    port = int(os.environ.get("PORT", 9000))
    # نستخدم 0.0.0.0 للسماح لـ Render بالوصول للسيرفر
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"--- Server started on port {port} ---", flush=True)
    server.serve_forever()

# ==============================
# DATA & ANALYSIS
# ==============================
async def fetch_klines(session, symbol, interval, limit):
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        async with session.get(BASE_URL, params=params, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                df = pd.DataFrame(data, columns=['time','open','high','low','close','vol','ct','qa','nt','tb','tq','i'])
                return df[['open','high','low','close','vol']].astype(float)
    except: return None

async def analyze_market_logic(session, symbol):
    df4h = await fetch_klines(session, symbol, "4h", 100)
    df1h = await fetch_klines(session, symbol, "1h", 100)
    df15m = await fetch_klines(session, symbol, "15m", 100)

    if df4h is None or df1h is None or df15m is None: return None

    ema200 = ta.ema(df4h['close'], length=100).iloc[-1]
    curr_p = df15m['close'].iloc[-1]
    direction = "LONG" if curr_p > ema200 else "SHORT"
    
    fvg = False
    if direction == "LONG" and df1h['low'].iloc[-1] > df1h['high'].iloc[-3]: fvg = True
    elif direction == "SHORT" and df1h['high'].iloc[-1] < df1h['low'].iloc[-3]: fvg = True

    score = 70 + (15 if fvg else 0) # تبسيط السكور للتأكد من عمل البوت
    
    if score < 80: return None

    atr = ta.atr(df15m['high'], df15m['low'], df15m['close'], length=14).iloc[-1]
    sl_dist = atr * 2.5
    sl = curr_p - sl_dist if direction == "LONG" else curr_p + sl_dist
    risk = abs(curr_p - sl)
    
    return {
        'symbol': symbol, 'type': f"🟢 {direction}", 'entry': curr_p, 'sl': sl,
        'tp1': curr_p + (risk * 1.5) if direction == "LONG" else curr_p - (risk * 1.5),
        'tp2': curr_p + (risk * 2.5) if direction == "LONG" else curr_p - (risk * 2.5),
        'tp3': curr_p + (risk * 4.0) if direction == "LONG" else curr_p - (risk * 4.0),
        'rr': 4.0, 'score': score
    }

# ==============================
# MAIN ENGINE
# ==============================
async def scanner_loop():
    print("--- Starting Scanner Loop ---", flush=True)
    async with aiohttp.ClientSession() as session:
        while True:
            print("Analyzing the market...", flush=True)
            for symbol in SYMBOLS:
                try:
                    res = await analyze_market_logic(session, symbol)
                    if res and symbol not in active_positions:
                        msg = f"🚀 **Signal Alert**\n🥇 Symbol: `{res['symbol']}`\n⚡ Type: {res['type']}\n📍 Entry: {res['entry']:.5f}\n🛡️ SL: {res['sl']:.5f}\n🎯 TP3: {res['tp3']:.5f}"
                        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
                        active_positions[symbol] = res
                    await asyncio.sleep(0.3)
                except Exception as e:
                    print(f"Error in {symbol}: {e}", flush=True)
            await asyncio.sleep(300)

async def main():
    # تشغيل السيرفر في خيط منفصل
    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()
    
    # تشغيل حلقة المسح
    await scanner_loop()

if __name__ == "__main__":
    # استخدام سطر صريح للتشغيل
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except Exception as e:
        print(f"CRITICAL ERROR: {e}", flush=True)

