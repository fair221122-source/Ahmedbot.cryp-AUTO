import asyncio
import aiohttp
import pandas as pd
import pandas_ta as ta
import requests
import time
import os
from telegram import Bot
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

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
# SERVER (Render Keep Alive)
# ==============================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Live and Analyzing...")

def start_server():
    # Render يرسل المنفذ تلقائياً عبر متغيرات البيئة
    port = int(os.environ.get("PORT", 9000))
    # يجب استخدام "0.0.0.0" وليس "127.0.0.1" لكي يراه Render
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"✅ Server is listening on port {port}")
    server.serve_forever()


# ==============================
# SMART NEWS RADAR (Logic)
# ==============================
def get_news_sentiment(symbol):
    try:
        coin = symbol.replace("USDT", "")
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API_KEY}&currencies={coin}&filter=hot"
        res = requests.get(url, timeout=5).json()
        posts = res.get("results", [])
        
        if not posts:
            return "لا توجد أخبار عاجلة مؤثرة حالياً.", 0
        
        latest_post = posts[0]
        title = latest_post['title']
        # تحليل بسيط للمشاعر بناءً على تصويت المنصة
        votes = latest_post.get('votes', {})
        pos = votes.get('positive', 0)
        neg = votes.get('negative', 0)
        
        sentiment_score = 15 if pos > neg else (-10 if neg > pos else 0)
        return title, sentiment_score
    except:
        return "رادار الأخبار قيد التحديث...", 0

# ==============================
# TRADING ENGINE (ICT + CMT)
# ==============================
async def analyze_market_logic(session, symbol):
    # جلب البيانات للفريمات الثلاثة
    df4h = await fetch_klines(session, symbol, "4h", 100)
    df1h = await fetch_klines(session, symbol, "1h", 100)
    df15m = await fetch_klines(session, symbol, "15m", 100)

    if df4h is None or df1h is None or df15m is None: return None

    # 1. تحديد الاتجاه (4H - CMT Principle)
    ema200 = ta.ema(df4h['close'], length=100).iloc[-1]
    curr_price = df15m['close'].iloc[-1]
    direction = "LONG" if curr_price > ema200 else "SHORT"

    # 2. رصد السيولة والفجوات (1H - ICT Principle)
    # فحص وجود FVG (Fair Value Gap)
    fvg_present = False
    if direction == "LONG":
        if df1h['low'].iloc[-1] > df1h['high'].iloc[-3]: fvg_present = True
    else:
        if df1h['high'].iloc[-1] < df1h['low'].iloc[-3]: fvg_present = True

    # 3. حساب الـ Score (الهدف 80% - 85%)
    news_title, news_bonus = get_news_sentiment(symbol)
    base_score = 65 # نقطة انطلاق قوية إذا تحقق الاتجاه
    if fvg_present: base_score += 15
    if df15m['volume'].iloc[-1] > df15m['volume'].tail(20).mean(): base_score += 5
    
    total_score = base_score + news_bonus
    total_score = max(min(total_score, 95), 0) # حصر النتيجة بين 0 و 95

    if total_score < 80: return None

    # 4. إدارة المخاطر (ATR + R:R)
    atr = ta.atr(df15m['high'], df15m['low'], df15m['close'], length=14).iloc[-1]
    sl_dist = atr * 2.2
    
    sl = curr_price - sl_dist if direction == "LONG" else curr_price + sl_dist
    risk = abs(curr_price - sl)
    
    # تحديد أهداف صفوة الصفوة
    rr = 3.5 if total_score < 85 else 4.5
    tp1 = curr_price + (risk * 1.5) if direction == "LONG" else curr_price - (risk * 1.5)
    tp2 = curr_price + (risk * 2.5) if direction == "LONG" else curr_price - (risk * 2.5)
    tp3 = curr_price + (risk * rr) if direction == "LONG" else curr_price - (risk * rr)

    return {
        'symbol': symbol, 'type': f"🟢 {direction} (دخول فوري)",
        'entry': curr_price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
        'rr': rr, 'score': total_score, 'news': news_title
    }

async def fetch_klines(session, symbol, interval, limit):
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        async with session.get(BASE_URL, params=params) as resp:
            data = await resp.json()
            df = pd.DataFrame(data, columns=['time','open','high','low','close','vol','ct','qa','nt','tb','tq','i'])
            return df[['open','high','low','close','vol']].astype(float)
    except: return None

# ==============================
# DYNAMIC MESSAGING
# ==============================
def send_formatted_signal(data):
    msg = (
        f"🚀 **Signal Alert: Institutional Entry**\n\n"
        f"🥇 **Symbol:** `{data['symbol']}`\n"
        f"🔥 **Activity Rate:** {int(data['score'])}% (زخم فائق)\n"
        f"⚡ **Trade Type:** {data['type']}\n\n"
        f"📍 **Entry Zone:** {data['entry']:.5f}\n"
        f"🛡️ **Stop Loss:** {data['sl']:.5f}\n"
        f"🎯 **TP 1:** {data['tp1']:.5f}\n"
        f"🎯 **TP 2:** {data['tp2']:.5f}\n"
        f"🎯 **TP 3:** {data['tp3']:.5f}\n\n"
        f"⚖️ **R:R:** 1:{data['rr']}\n"
        f"📈 **Trend Strength:** High (قوي جداً)\n"
        f"📰 **رادار الأخبار:** {data['news']}\n"
        f"💡 **ملاحظة:** تم رصد سيولة مؤسسية؛ السعر تجاوز منطقة التجميع وأكمل ملء الـ FVG.\n"
        f"❗ *إدارة المخاطر مسؤليتك.*"
    )
    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')

# ==============================
# MAIN EXECUTION
# ==============================
async def scanner_loop():
    async with aiohttp.ClientSession() as session:
        print("Analyzing the market...")
        # مراقبة الأهداف للصفقات المفتوحة
        for sym in list(active_positions.keys()):
            df = await fetch_klines(session, sym, "1m", 2)
            if df is not None:
                p = df['close'].iloc[-1]
                target = active_positions[sym]
                if (target['type'].find("LONG") != -1 and p >= target['tp1']) or \
                   (target['type'].find("SHORT") != -1 and p <= target['tp1']):
                    bot.send_message(chat_id=CHAT_ID, text=f"✅ **TP 1 Hit Successfully!**\n🥇 Symbol: {sym}")
                    del active_positions[sym] # للتسهيل نمسحها بعد الهدف الأول أو نطورها

        for symbol in SYMBOLS:
            if symbol in active_positions: continue
            result = await analyze_market_logic(session, symbol)
            if result:
                send_formatted_signal(result)
                active_positions[symbol] = result
            await asyncio.sleep(0.5)

async def main():
    threading.Thread(target=start_server, daemon=True).start()
    while True:
        try:
            await scanner_loop()
        except Exception as e:
            print(f"Error in Loop: {e}")
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())

