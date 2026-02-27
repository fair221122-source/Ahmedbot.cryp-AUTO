import asyncio
import requests
import pandas as pd
from telegram import Bot
import yfinance as yf
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import time

# --- حل مشكلة Render Port Binding ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Running")

def run_port_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_port_server, daemon=True).start()

# ==========================================
# 🔑 بيانات الربط (أدخل بياناتك هنا)
# ==========================================
TELEGRAM_TOKEN = '8568994708:AAFXTPTK3MyEe1wfrWTYBBUPfbi8zayOxi0'
CHAT_ID = '986199874'
CRYPTOPANIC_API_KEY = 'a5563e90848ba81e4aeca929e26d90069b2d1b9f'

bot = Bot(token=TELEGRAM_TOKEN)

# قائمة العملات القوية والواضحة (بدون أصفار كثيرة) - فبراير 2026 [cite: 2026-02-15]
SYMBOLS = [
symbols = [
        "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD",
        "ADA-USD", "AVAX-USD", "LINK-USD", "DOT-USD", "TRX-USD",
        "BCH-USD", "UNI-USD", "LDO-USD", "ETC-USD", "ATOM-USD",
        "ICP-USD", "FTM-USD", "SEI-USD", "TAO-USD", "NEAR-USD",
        "LTC-USD", "APT-USD", "FIL-USD", "OP-USD", "ARB-USD",
        "TIA-USD", "STX-USD", "INJ-USD", "RNDR-USD", "SUI-USD",
        "PYTH-USD", "WLD-USD", "GALA-USD", "FET-USD", "ORDI-USD",
        "AAVE-USD", "IMX-USD", "JUP-USD", "DYDX-USD", "POL-USD"
]

active_trades = {}

def calculate_indicators(df):
    if df.empty or len(df) < 200: return df
    df['ema_200'] = df['Close'].ewm(span=200, adjust=False).mean()
    df['tr'] = pd.concat([df['High']-df['Low'], (df['High']-df['Close'].shift()).abs(), (df['Low']-df['Close'].shift()).abs()], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(window=14).mean()
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    df['rsi'] = 100 - (100 / (1 + (gain / loss)))
    return df

def get_global_news():
    try:
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API_KEY}&filter=hot"
        res = requests.get(url, timeout=10).json()
        return res.get('results', [])
    except: return []

async def analyze_market(symbol):
    try:
        # فريم 4 ساعات لتحديد الاتجاه [cite: 2026-02-26]
        data_4h = yf.download(symbol, period='1mo', interval='4h', progress=False)
        df_4h = calculate_indicators(data_4h)
        is_up = df_4h['Close'].iloc[-1] > df_4h['ema_200'].iloc[-1]

        # فريم الساعة للتحليل [cite: 2026-02-26]
        data_1h = yf.download(symbol, period='1mo', interval='1h', progress=False)
        df_1h = calculate_indicators(data_1h)
        activity = int(df_1h['rsi'].iloc[-1])
        
        # فريم 15 دقيقة للدخول [cite: 2026-02-26]
        data_15m = yf.download(symbol, period='5d', interval='15m', progress=False)
        df_15m = calculate_indicators(data_15m)
        
        fvg = False
        if is_up and df_15m['Low'].iloc[-1] > df_15m['High'].iloc[-3]: fvg = True
        elif not is_up and df_15m['High'].iloc[-1] < df_15m['Low'].iloc[-3]: fvg = True

        price = df_15m['Close'].iloc[-1]
        atr = df_15m['atr'].iloc[-1]
        return is_up, price, atr, activity, fvg
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

async def process_market():
    print("Analyzing the market...") # [cite: 2026-02-22]
    global_news = get_global_news()
    for sym in SYMBOLS:
        res = await analyze_market(sym)
        if res and (res[3] >= 80 or res[4]) and sym not in active_trades:
            is_up, price, atr, act, fvg = res
            sl = price - (atr * 1.5) if is_up else price + (atr * 1.5)
            diff = abs(price - sl)
            # ريسك ريوارد 1:5 [cite: 2026-02-22]
            tp1, tp2, tp3 = (price + diff*2, price + diff*3, price + diff*5) if is_up else (price - diff*2, price - diff*3, price - diff*5)
            
            coin_news = "استقرار في حركة السيولة المؤسسية حالياً."
            sentiment = "Neutral ⚖️"
            coin_name = sym.split('-')[0]
            for post in global_news:
                if coin_name.lower() in post['title'].lower():
                    coin_news = post['title']; sentiment = "Positive ✅" if is_up else "Negative 🔴"; break

            active_trades[sym] = {'entry': price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'is_up': is_up, 'tp1_hit': False, 'tp2_hit': False}

            msg = (f"🚀 تنبيه: دخول سيولة مؤسسية ضخمة!\n🥇 العملة: {sym}\n"
                   f"🔥 نسبة النشاط: {act}% ({'زخم شرائي' if is_up else 'ضغط بيعي'})\n⚡ نوع العملية: {'LONG 🟢' if is_up else 'SHORT 🔴'}\n"
                   f"📍 Entry: {price:.4f}\n🛡️ S.L   : {sl:.4f}\n🎯 T.P 1 : {tp1:.4f}\n🎯 T.P 2 : {tp2:.4f}\n🎯 T.P 3 : {tp3:.4f}\n"
                   f"⚖️ R:R    : 1:5\n📈 قوة الاتجاه: High (قوي جداً)\n💬 آخر خبر: \"{coin_news}\" ({sentiment})\n"
                   f"❗إدارة المخاطر مسؤوليتك.")
            await bot.send_message(CHAT_ID, msg)

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(process_market())
        except Exception as e:
            print(f"Global Error: {e}")
        time.sleep(120) # فحص كل دقيقتين [cite: 2026-02-15]

