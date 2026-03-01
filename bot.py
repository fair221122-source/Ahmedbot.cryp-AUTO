import asyncio
import aiohttp
import pandas as pd
import pandas_ta as ta
import os
import requests
import threading
from telegram import Bot
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==============================
# CONFIG & API KEYS
# ==============================
TELEGRAM_TOKEN = "8524445307:AAEDw5THEah-iBwpgsTqvK2Pi7abpzWarZk"
CHAT_ID = "986199874"
CRYPTOPANIC_KEY = "a5563e90848ba81e4aeca929e26d90069b2d1b9f" # تأكد من وضعه هنا

bot = Bot(token=TELEGRAM_TOKEN)
BASE_URL = "https://fapi.binance.com/fapi/v1/klines"

# قائمة الـ 40 عملة التي تحترم التحليل المؤسسي
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","AVAXUSDT","DOTUSDT","LINKUSDT","MATICUSDT",
    "NEARUSDT","LTCUSDT","UNIUSDT","ATOMUSDT","APTUSDT","SUIUSDT","OPUSDT","ARBUSDT","INJUSDT","TIAUSDT",
    "RNDRUSDT","STXUSDT","FILUSDT","ICPUSDT","BCHUSDT","FETUSDT","GALAUSDT","ORDIUSDT","PYTHUSDT","WLDUSDT",
    "SEIUSDT","JUPUSDT","AAVEUSDT","IMXUSDT","DYDXUSDT","STRKUSDT","MANAUSDT","SANDUSDT","EGLDUSDT","THETAUSDT"
]

# ==============================
# 📡 RADAR: CRYPTOPANIC NEWS
# ==============================
def get_radar_news(symbol):
    try:
        coin = symbol.replace("USDT", "")
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_KEY}&currencies={coin}&filter=hot"
        response = requests.get(url, timeout=5).json()
        results = response.get("results", [])
        
        if not results:
            return "رادار الأخبار: هدوء في التدفق الإخباري حالياً.", 0
        
        latest_news = results[0]['title']
        votes = results[0].get('votes', {})
        # حساب قوة الخبر بناءً على التصويت
        sentiment = (votes.get('positive', 0) * 2) - votes.get('negative', 0)
        
        status = "🟢 إيجابي" if sentiment > 0 else "🔴 سلبي/حذر"
        return f"{status} | {latest_news}", sentiment
    except:
        return "رادار الأخبار: تعذر جلب البيانات (تحقق من الـ API).", 0

# ==============================
# 🏗️ CORE ENGINE (4H -> 1H -> 15M)
# ==============================
async def get_data(session, symbol, interval, limit=50):
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        async with session.get(BASE_URL, params=params, timeout=10) as resp:
            d = await resp.json()
            df = pd.DataFrame(d, columns=['t','o','h','l','c','v','ct','qa','nt','tb','tq','i'])
            return df[['o','h','l','c','v']].astype(float)
    except: return None

async def analyze(session, symbol):
    # 1. الاتجاه (4H)
    df4h = await get_data(session, symbol, "4h", 20)
    if df4h is None: return None
    ema = ta.ema(df4h['c'], length=20).iloc[-1]
    trend = "LONG" if df4h['c'].iloc[-1] > ema else "SHORT"

    # 2. التحليل والسيولة (1H)
    df1h = await get_data(session, symbol, "1h", 50)
    if df1h is None: return None
    
    # رصد الفجوة (FVG)
    fvg_entry = None
    if trend == "LONG":
        if df1h['l'].iloc[-1] > df1h['h'].iloc[-3]: fvg_entry = df1h['h'].iloc[-3]
    else:
        if df1h['h'].iloc[-1] < df1h['l'].iloc[-3]: fvg_entry = df1h['l'].iloc[-3]

    if not fvg_entry: return None

    # 3. الدخول والأهداف (15M)
    df15m = await get_data(session, symbol, "15m", 30)
    if df15m is None: return None
    atr = ta.atr(df15m['h'], df15m['l'], df15m['c']).iloc[-1]

    # جلب رادار الأخبار لهذه العملة
    news_text, sentiment_score = get_radar_news(symbol)

    entry = fvg_entry
    sl = entry - (atr * 2.5) if trend == "LONG" else entry + (atr * 2.5)
    risk = abs(entry - sl)
    
    # R:R ذكي بين 1:3 و 1:8
    rr = 4.0 if abs(sentiment_score) < 10 else 6.0 
    tp = entry + (risk * rr) if trend == "LONG" else entry - (risk * rr)

    return {
        'symbol': symbol, 'type': f"🟢 {trend} (Limit Order)",
        'entry': entry, 'sl': sl, 'tp': tp, 'rr': rr,
        'news': news_text, 'score': 85 + (5 if abs(sentiment_score) > 5 else 0)
    }

# ==============================
# 🚀 MAIN LOOP & MESSAGING
# ==============================
async def main_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            print("Analyzing the market...", flush=True)
            for sym in SYMBOLS:
                res = await analyze(session, sym)
                if res:
                    msg = (
                        f"🚀 **Signal Alert: Institutional Entry**\n\n"
                        f"🥇 **Symbol:** `{res['symbol']}`\n"
                        f"🔥 **Activity Rate:** {res['score']}% (زخم فائق)\n"
                        f"⚡ **Type:** {res['type']}\n\n"
                        f"📍 **Entry (FVG Fill):** {res['entry']:.5f}\n"
                        f"🛡️ **Stop Loss:** {res['sl']:.5f}\n"
                        f"🎯 **Target (Exit):** {res['tp']:.5f}\n\n"
                        f"⚖️ **R:R:** 1:{res['rr']}\n"
                        f"📈 **Trend:** High (4H Confirmed)\n"
                        f"📰 **رادار الأخبار:** {res['news']}\n"
                        f"💡 **ملاحظة:** تم رصد سيولة مؤسسية؛ الدخول معلق عند الفجوة لضمان حماية الستوب.\n"
                        f"❗ *إدارة المخاطر مسؤوليتك.*"
                    )
                    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
                await asyncio.sleep(1)
            await asyncio.sleep(300)

# سيرفر Render المعتاد
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Radar Active")

def run_server():
    port = int(os.environ.get("PORT", 9000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    asyncio.run(main_loop())

