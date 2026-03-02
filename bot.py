import asyncio
import aiohttp
import pandas as pd
import pandas_ta as ta
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Bot
from telegram.constants import ParseMode
import threading

# ==============================
# المفاتيح
# ==============================
TELEGRAM_TOKEN = "8524445307:AAEDw5THEah-iBwpgsTqvK2Pi7abpzWarZk"
CHAT_ID = "986199874"
CRYPTOPANIC_KEY = "a5563e90848ba81e4aeca929e26d90069b2d1b9f"

bot = Bot(token=TELEGRAM_TOKEN)
BASE_URL = "https://fapi.binance.com/fapi/v1/klines"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","AVAXUSDT","DOTUSDT","LINKUSDT","MATICUSDT",
    "NEARUSDT","LTCUSDT","UNIUSDT","ATOMUSDT","APTUSDT","SUIUSDT","OPUSDT","ARBUSDT","INJUSDT","TIAUSDT",
    "RNDRUSDT","STXUSDT","FILUSDT","ICPUSDT","BCHUSDT","FETUSDT","GALAUSDT","ORDIUSDT","PYTHUSDT","WLDUSDT",
    "SEIUSDT","JUPUSDT","AAVEUSDT","IMXUSDT","DYDXUSDT","STRKUSDT","MANAUSDT","SANDUSDT","EGLDUSDT","THETAUSDT"
]

# ==============================
# الأخبار العربية (حدث + نتيجة + تأثير)
# ==============================
async def get_news(session, symbol):
    try:
        coin = symbol.replace("USDT", "")
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_KEY}&currencies={coin}&filter=hot"

        async with session.get(url, timeout=10) as res:
            if res.status != 200:
                return "لا توجد أخبار مؤثرة على العملة حالياً", 0

            data = await res.json()
            results = data.get("results", [])
            if not results:
                return "لا توجد أخبار مؤثرة على العملة حالياً", 0

            news_title = results[0]['title']

            votes = results[0].get('votes', {})
            sentiment = (votes.get('positive', 0) * 2) - votes.get('negative', 0)

            if sentiment > 5:
                status = "🟢 خبر إيجابي"
            elif sentiment < -5:
                status = "🔴 خبر سلبي"
            else:
                status = "⚪ خبر محايد"

            translate_url = "https://api.mymemory.translated.net/get"
            params = {"q": news_title, "langpair": "en|ar"}

            async with session.get(translate_url, params=params) as t_res:
                t_data = await t_res.json()
                translated = t_data.get("responseData", {}).get("translatedText", news_title)

            event = translated

            if any(word in event for word in ["ارتفاع", "زيادة", "شراء", "تدفقات", "انتعاش"]):
                effect = "النتيجة: الخبر يشير إلى دعم صعودي محتمل."
            elif any(word in event for word in ["انخفاض", "بيع", "خروج", "تحذير", "هبوط"]):
                effect = "النتيجة: الخبر يشير إلى ضغط بيعي محتمل."
            else:
                effect = "النتيجة: التأثير غير واضح لكنه يستحق المتابعة."

            final_text = f"{status} — {event}. {effect}"

            return final_text, sentiment

    except Exception as e:
        print("News Error:", e)
        return "لا توجد أخبار مؤثرة على العملة حالياً", 0

# ==============================
# جلب بيانات Binance
# ==============================
async def fetch_klines(session, symbol, interval, limit=50):
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        async with session.get(BASE_URL, params=params, timeout=10) as resp:
            if resp.status != 200:
                return None

            data = await resp.json()
            df = pd.DataFrame(data, columns=['t','o','h','l','c','v','ct','qa','nt','tb','tq','i'])

            for col in ['o','h','l','c']:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            return df[['o','h','l','c']].dropna()

    except Exception as e:
        print("Klines Error:", e)
        return None

# ==============================
# كشف فجوة FVG
# ==============================
def detect_fvg(df, trend):
    try:
        if len(df) < 5:
            return None

        if trend == "LONG":
            for i in range(2, 4):
                if df['l'].iloc[-1] > df['h'].iloc[-i-1]:
                    return (df['h'].iloc[-i-1] + df['l'].iloc[-1]) / 2

        else:
            for i in range(2, 4):
                if df['h'].iloc[-1] < df['l'].iloc[-i-1]:
                    return (df['l'].iloc[-i-1] + df['h'].iloc[-1]) / 2

        return None

    except Exception as e:
        print("FVG Error:", e)
        return None

# ==============================
# تحليل العملة
# ==============================
async def analyze_symbol(session, symbol):
    try:
        df4h = await fetch_klines(session, symbol, "4h", 30)
        if df4h is None or len(df4h) < 20:
            return None

        ema = ta.ema(df4h['c'], length=20)
        if ema is None or len(ema) == 0:
            return None

        trend = "LONG" if df4h['c'].iloc[-1] > ema.iloc[-1] else "SHORT"

        df1h = await fetch_klines(session, symbol, "1h", 30)
        if df1h is None:
            return None

        entry = detect_fvg(df1h, trend)
        if entry is None:
            return None

        df15m = await fetch_klines(session, symbol, "15m", 30)
        if df15m is None or len(df15m) < 20:
            return None

        atr = ta.atr(df15m['h'], df15m['l'], df15m['c'], length=14)
        if atr is None or len(atr) == 0:
            return None

        atr_val = atr.iloc[-1]
        if atr_val <= 0:
            return None

        news_text, sentiment = await get_news(session, symbol)

        sl = entry - (atr_val * 2.5) if trend == "LONG" else entry + (atr_val * 2.5)
        risk = abs(entry - sl)
        rr = 5.0 if abs(sentiment) > 8 else 4.0
        tp = entry + (risk * rr) if trend == "LONG" else entry - (risk * rr)

        confidence = 85
        if abs(sentiment) > 5:
            confidence += 5

        return {
            'symbol': symbol,
            'trend': trend,
            'entry': round(entry, 5),
            'sl': round(sl, 5),
            'tp': round(tp, 5),
            'rr': rr,
            'news': news_text,
            'confidence': confidence
        }

    except Exception as e:
        print("Analyze Error:", e)
        return None

# ==============================
# الحلقة الرئيسية
# ==============================
async def main_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            print(f"\n🔄 بدء تحليل السوق - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            for symbol in SYMBOLS:
                result = await analyze_symbol(session, symbol)

                if result:
                    arrow = "🟢" if result['trend'] == "LONG" else "🔴"

                    msg = (
                        f"⚡ **إشارة تداول - تحليل فني + أخبار** ⚡\n\n"
                        f"العملة: `{result['symbol']}`\n"
                        f"الاتجاه: {arrow} {result['trend']}\n"
                        f"الثقة: {result['confidence']}%\n\n"
                        f"📍 **الدخول:** `{result['entry']}`\n"
                        f"🛑 **وقف الخسارة:** `{result['sl']}`\n"
                        f"🎯 **الهدف:** `{result['tp']}`\n"
                        f"📊 **نسبة العائد/المخاطرة:** 1:{result['rr']}\n\n"
                        f"📰 **أخبار:** {result['news']}\n\n"
                        f"💡 فجوة سعرية (FVG) على الإطار 1 ساعة مع تأكيد الاتجاه من 4 ساعات"
                    )

                    try:
                        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)
                        print(f"✅ تم إرسال إشارة لـ {result['symbol']}")
                    except Exception as e:
                        print("Telegram Error:", e)

                await asyncio.sleep(1)

            print("⏳ انتظار 5 دقائق...")
            await asyncio.sleep(300)

# ==============================
# Health Check Server
# ==============================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"🌐 Health server running on port {port}")
    server.serve_forever()

# ==============================
# تشغيل البوت
# ==============================
if __name__ == "__main__":
    print("🚀 بدء تشغيل البوت...")
    threading.Thread(target=run_health_server, daemon=True).start()
    asyncio.run(main_loop())
