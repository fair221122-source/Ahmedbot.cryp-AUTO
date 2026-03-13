import nest_asyncio
nest_asyncio.apply()

import os
import asyncio
import json
import time
from datetime import datetime, timedelta

import requests
import numpy as np
import pandas as pd

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters
)

import threading
import uvicorn
from fastapi import FastAPI, WebSocket, Request
import websockets

# -------------------------
# FastAPI + Port for Render
# -------------------------
app = FastAPI()

# 🔥 تمت إضافة /ping هنا فقط — بدون أي تعديل آخر
@app.get("/ping")
async def ping():
    return {"status": "alive"}

@app.get("/")
def home():
    return {"status": "running"}
    
# -------------------------
# Telegram Webhook Section
# -------------------------

TOKEN = os.getenv("TELEGRAM_TOKEN")   # التوكن من البيئة

# إنشاء تطبيق التليجرام
telegram_app = Application.builder().token(TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
# مسار الويبهوك
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

def run_api():
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# ⭐ تشغيل FastAPI
threading.Thread(target=run_api).start()

# ⭐ تشغيل بوت التليجرام (Webhook)
async def start_bot():
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook("https://ahmedbot-cryp-auto.fly.dev/webhook")


if __name__ == "__main__":
    asyncio.run(start_bot())
# ============================================
# GLOBAL CONFIG
# ============================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CRYPTOPANIC_API = os.getenv("CRYPTOPANIC_API")

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","NEARUSDT","TRXUSDT","LTCUSDT","UNIUSDT",
    "ARBUSDT","OPUSDT","SUIUSDT","FILUSDT","STXUSDT"
]

BINANCE_APIS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com"
]

session = requests.Session()

# كاشات عامة
price_cache = {}
orderbook_cache = {}
liquidity_map = {}
last_signal_time_manual = {}   # للصفقات اليدوية (أمر "صفقات")
last_signal_time_auto = {}     # للصفقات الآلية (الفحص كل 10 دقائق)

klines_cache = {}
KLINES_TTL = 60  # ثانية

# Cluster Delta caches
cluster_cache = {}          # {symbol: {"cvd": float, "last_update": ts}}
cluster_footprint = {}      # {symbol: {candle_id: {price_level: {"bid": x, "ask": y}}}}

# ============================================
# TELEGRAM UI
# ============================================

active_chats = set()

keyboard = ReplyKeyboardMarkup(
    [["صفقات", "تحليل"]],
    resize_keyboard=True
)

# ============================================
# HELPERS
# ============================================

def rr_to_str(rr: float) -> str:
    """
    يحوّل قيمة RR مثل 4.0 إلى نص بالشكل: 1:4.00
    """
    return f"1:{rr:.2f}"

def format_direction_emoji(side: str) -> str:
    """
    يعيد 🟢 للـ Long و 🔴 للـ Short
    """
    return "🟢" if side.lower() == "long" else "🔴"

def format_side_hashtag(side: str) -> str:
    """
    يعيد #Long أو #Short
    """
    return "#Long" if side.lower() == "long" else "#Short"

# ============================================
# MESSAGE TEMPLATES — MANUAL SIGNALS
# (لا تغيير في الشكل إطلاقاً)
# ============================================

def format_manual_signals(signals: list) -> str:
    """
    تنسيق رسالة: أفضل صفقتين في السوق حالياً (يدوي — عند إرسال كلمة صفقات)
    كل عنصر في signals هو dict يحتوي على:
    symbol, side, entry, sl, tp, rr, prob, is_instant, reason_text
    is_instant: True = (فوري) / False = (معلّق)
    """
    if not signals:
        return (
            "السوق متذبذب ولا توجد فرصة دخول مثالية حاليا، حاول بعد دقائق"
        )

    text = "أفضل صفقتين في السوق حاليا:\n"
    text += "-------------------------------------------\n"

    medals = ["🥇", "🥈", "🥉"]

    for i, sig in enumerate(signals[:2]):
        medal = medals[i] if i < len(medals) else "•"
        symbol = sig["symbol"]
        side = sig["side"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp = sig["tp"]
        rr = sig["rr"]
        prob = sig["prob"]
        reason = sig.get("reason_text", "").strip()
        is_instant = sig.get("is_instant", True)

        kind = "فوري" if is_instant else "معلّق"
        emoji = format_direction_emoji(side)
        hashtag = format_side_hashtag(side)

        text += f"{medal} {symbol} — {emoji} ({kind})\n"
        text += f"{hashtag}\n"
        text += f"Entry: {entry:.4f}\n"
        text += f"SL: {sl:.4f}\n"
        text += f"TP: {tp:.4f}\n"
        text += f"R:R = {rr_to_str(rr)}\n"
        text += f"نسبة النجاح المتوقعة: {prob}%\n"
        text += "-------------------------------------------\n"
        text += "📌 السبب: "
        if reason:
            text += reason + "\n"
        else:
            text += "توافق الإطارات الزمنية مع سلوك سعري قوي وسيولة مؤسسية واضحة.\n"
        text += "--------------------------------\n"

    # ملاحظة خاصة للصفقة المعلقة (إن وجدت)
    if any(not s.get("is_instant", True) for s in signals[:2]):
        text += (
            "ستصلك رسالة تأكيد عند وصول السعر إلى المنطقة المثالية للدخول (للصفقة المعلّقة فقط).\n"
        )

    return text

# ============================================
# MESSAGE TEMPLATES — AUTO SCAN SIGNAL
# (لا تغيير في الشكل إطلاقاً)
# ============================================

def format_auto_signal(sig: dict) -> str:
    """
    تنسيق رسالة الفحص الآلي:
    ⏰ فحص آلي — فرصة جديدة
    """
    symbol = sig["symbol"]
    side = sig["side"]
    entry = sig["entry"]
    sl = sig["sl"]
    tp = sig["tp"]
    rr = sig["rr"]
    prob = sig["prob"]
    reason = sig.get("reason_text", "").strip()
    is_instant = sig.get("is_instant", True)

    kind = "فوري" if is_instant else "معلّق"
    emoji = format_direction_emoji(side)
    hashtag = format_side_hashtag(side)

    text = "⏰ فحص آلي — فرصة جديدة\n"
    text += "------------------------------------------\n"
    text += f"🎯 {symbol} — {emoji} ({kind})\n"
    text += f"{hashtag}\n"
    text += f"Entry: {entry:.4f}\n"
    text += f"SL: {sl:.4f}\n"
    text += f"TP: {tp:.4f}\n"
    text += f"R:R = {rr_to_str(rr)}\n"
    text += f"نسبة النجاح المتوقعة: {prob}%\n"
    text += "------------------------------------------------\n"
    text += "📌 السبب: "
    if reason:
        text += reason + "\n"
    else:
        text += (
            "انفجار سعري ملحوظ مع تفاعل قوي عند مناطق عرض/طلب رئيسية "
            "وتوافق مع اتجاه السوق العام.\n"
        )
    return text

# ============================================
# MESSAGE TEMPLATES — PENDING ENTRY ALERT
# (لا تغيير في الشكل إطلاقاً)
# ============================================

def format_pending_entry_alert(symbol: str, side: str, price: float, entry: float, prob: int) -> str:
    """
    رسالة التذكير عند وصول السعر إلى المنطقة المثالية للدخول
    (للصفقات المعلقة فقط)
    """
    text = "تذكير ... 🔔\n"
    text += "السعر وصل إلى المنطقة المثالية للدخول، خذ نظرة و قرر\n\n"
    text += f"{symbol}\n\n"
    text += f"السعر الحالي: {price:.4f}\n"
    text += f"منطقة الدخول المقترحة: {entry:.4f}\n\n"
    text += f"الاتجاه: {'Long' if side.lower() == 'long' else 'Short'}\n"
    text += f"نسبة النجاح المتوقعة: {prob}%\n"
    return text

# ============================================
# MESSAGE TEMPLATES — NO SIGNAL
# ============================================

def format_no_signal() -> str:
    return "السوق متذبذب ولا توجد فرصة دخول مثالية حاليا، حاول بعد دقائق"

# ============================================
# MESSAGE TEMPLATES — DAILY REPORT
# (لا تغيير في الشكل إطلاقاً)
# ============================================

def format_daily_report_header(news_comment: str) -> str:
    """
    رأس رسالة التحليل اليومي + تعليق إخباري مترجم بالعربي
    """
    text = (
        "التحليل اليومي لسوق الكريبتو حسب بيانات السوق والأخبار الواردة من موقع CryptoPanic\n"
        "-------------------------------------------\n"
    )
    if news_comment:
        text += news_comment.strip() + "\n"
    text += "أكثر خمس عملات رقمية نشطة حاليا صعود أو هبوط حسب اتجاه السوق:\n\n"
    return text

def format_daily_coin_line(
    rank: int,
    symbol: str,
    trend_1d: str,
    trend_4h: str,
    trend_1h: str,
    trend_15m: str,
    expectation: str
) -> str:
    """
    تنسيق سطر تحليل عملة واحدة ضمن أفضل 5 عملات نشطة
    """
    text = f"{rank}) {symbol}\n"
    text += f"📅 1D: {trend_1d}\n"
    text += f"⏰ 4h: {trend_4h}\n"
    text += f"🕰 1h: {trend_1h}\n"
    text += f"🕒 15m: {trend_15m}\n"
    text += f"📉 التوقع: {expectation}\n"
    text += "-------------------------------------------\n\n"
    return text

def format_daily_report(news_comment: str, coins_analysis: list) -> str:
    """
    news_comment: نص عربي يشرح تأثير الأخبار
    coins_analysis: قائمة من dict لكل عملة
    """
    text = format_daily_report_header(news_comment)

    if not coins_analysis:
        text += "لا توجد بيانات كافية لتحليل العملات النشطة حالياً.\n"
        return text

    for i, c in enumerate(coins_analysis[:5], start=1):
        text += format_daily_coin_line(
            i,
            c["symbol"],
            c["trend_1d"],
            c["trend_4h"],
            c["trend_1h"],
            c["trend_15m"],
            c["expectation"]
        )

    return text

# ============================================
# TELEGRAM COMMAND: /start
# ============================================

async def start(update, context):
    chat_id = update.message.chat_id
    active_chats.add(chat_id)

    msg = (
        "بوت التحليل الاحترافي جاهز ✅\n"
        "مهمتي مساعدتك في تحليل سوق الفيوتشر واستخراج أفضل الفرص.\n\n"
        "استخدم الأزرار بالأسفل:\n"
        "- صفقات: لاستخراج أفضل وأقوى الفرص المتاحة الآن\n"
        "- تحليل: للحصول على نظرة يومية عامة عن السوق"
    )
    await update.message.reply_text(msg, reply_markup=keyboard)

# ============================================
# FETCH KLINES WITH CACHING
# ============================================

def fetch_klines(symbol: str, interval: str, limit: int = 500):
    """
    جلب شموع بايننس مع كاش لمدة 60 ثانية لتخفيف الضغط.
    """
    key = f"{symbol}_{interval}"
    now = time.time()

    if key in klines_cache:
        cached = klines_cache[key]
        if now - cached["time"] < KLINES_TTL:
            return cached["data"]

    for api in BINANCE_APIS:
        try:
            url = f"{api}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
            r = session.get(url, timeout=5)
            data = r.json()

            df = pd.DataFrame(data, columns=[
                "open_time","open","high","low","close","volume",
                "close_time","qav","trades","tbbav","tbqav","ignore"
            ])

            df["open"] = df["open"].astype(float)
            df["high"] = df["high"].astype(float)
            df["low"] = df["low"].astype(float)
            df["close"] = df["close"].astype(float)
            df["volume"] = df["volume"].astype(float)

            klines_cache[key] = {"time": now, "data": df}
            return df

        except Exception:
            continue

    return None

# ============================================
# INDICATORS
# ============================================

def ema(series, period=200):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(df, period=14):
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()

    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def add_indicators(df):
    df = df.copy()
    df["ema200"] = ema(df["close"], 200)
    df["rsi"] = rsi(df["close"], 14)
    df["atr"] = atr(df, 14)
    return df

# ============================================
# CRYPTOPANIC NEWS (RAW)
# ============================================

def fetch_crypto_news_raw():
    """
    جلب آخر الأخبار من CryptoPanic (بالإنجليزي).
    """
    if not CRYPTOPANIC_API:
        return []

    try:
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API}&public=true"
        r = session.get(url, timeout=10)
        data = r.json().get("results", [])
        return data[:5]
    except:
        return []

# ============================================
# TRANSLATE NEWS TO ARABIC (تلخيصي)
# ============================================

def translate_to_arabic(text: str) -> str:
    """
    ترجمة بسيطة بدون API خارجي — تحويل الإنجليزية إلى عربية بأسلوب تلخيصي.
    ليست ترجمة حرفية، بل إعادة صياغة عربية مفهومة.
    """
    text = text.lower()

    # كلمات مفتاحية إيجابية
    if any(w in text for w in ["bull", "adoption", "growth", "etf", "approval"]):
        return "الأخبار تشير إلى تحسن في شهية المخاطرة وزيادة اهتمام المؤسسات بالسوق."

    # كلمات مفتاحية سلبية
    if any(w in text for w in ["hack", "ban", "lawsuit", "crash", "fear"]):
        return "الأخبار تحمل طابعًا سلبيًا وقد تضغط على حركة السوق مؤقتًا."

    # كلمات محايدة
    if any(w in text for w in ["update", "upgrade", "announcement"]):
        return "هناك تحديثات تقنية وأخبار محايدة قد تؤثر بشكل محدود على حركة السوق."

    # fallback
    return "خبر عام متعلق بالسوق دون تأثير واضح."

# ============================================
# NEWS ANALYSIS (ARABIC SUMMARY)
# ============================================

def analyze_news_arabic():
    """
    يعيد تعليق عربي احترافي عن حالة الأخبار.
    """
    raw_news = fetch_crypto_news_raw()
    if not raw_news:
        return "لا توجد أخبار مؤثرة حالياً."

    comments = []
    for n in raw_news:
        title = n.get("title", "")
        translated = translate_to_arabic(title)
        comments.append(translated)

    if not comments:
        return "لا توجد أخبار مؤثرة حالياً."

    positive = sum("تحسن" in c or "زيادة" in c for c in comments)
    negative = sum("سلبي" in c or "تضغط" in c for c in comments)

    if positive > negative:
        summary = "تفيد آخر الأخبار بأن السوق يميل إلى الإيجابية مع دخول سيولة مؤسسية."
    elif negative > positive:
        summary = "تفيد آخر الأخبار بأن السوق يواجه ضغوطًا بسبب أخبار سلبية."
    else:
        summary = "الأخبار محايدة وتأثيرها محدود على حركة السوق."

    return summary

# ============================================
# TOP 5 ACTIVE COINS (VOLUME + MOVEMENT)
# ============================================

def get_top5_active():
    """
    اختيار أكثر 5 عملات نشاطًا (حركة + سيولة).
    """
    ranking = []

    for s in SYMBOLS:
        df = fetch_klines(s, "1h", 50)
        if df is None or len(df) < 20:
            continue

        move = abs(df["close"].iloc[-1] - df["close"].iloc[-10])
        vol = df["volume"].iloc[-1]
        score = move * vol

        ranking.append((s, score))

    ranking = sorted(ranking, key=lambda x: x[1], reverse=True)
    return [x[0] for x in ranking[:5]]

# ============================================
# MULTI TIMEFRAME LOADER
# ============================================

def load_timeframes(symbol: str):
    df1d = fetch_klines(symbol, "1d", 300)
    df4h = fetch_klines(symbol, "4h", 300)
    df1h = fetch_klines(symbol, "1h", 300)
    df15 = fetch_klines(symbol, "15m", 300)

    if df1d is None or df4h is None or df1h is None or df15 is None:
        return None

    df1d = add_indicators(df1d)
    df4h = add_indicators(df4h)
    df1h = add_indicators(df1h)
    df15 = add_indicators(df15)

    return df1d, df4h, df1h, df15

# ============================================
# TREND FILTERS
# ============================================

def trend_filter(df):
    """
    اتجاه بسيط: مقارنة السعر مع EMA200 + ميل المتوسط.
    """
    if len(df) < 20:
        return "sideways"

    close = df["close"].iloc[-1]
    ema200 = df["ema200"].iloc[-1]
    ema_prev = df["ema200"].iloc[-10]

    if close > ema200 and ema200 > ema_prev:
        return "bullish"
    if close < ema200 and ema200 < ema_prev:
        return "bearish"
    return "sideways"

# ============================================
# RSI DIVERGENCE (BASIC)
# ============================================

def rsi_divergence(df):
    """
    دايفرجنس بسيط بين السعر و RSI على آخر 30 شمعة.
    """
    if len(df) < 15:
        return None

    closes = df["close"]
    rsi_vals = df["rsi"]

    price_high1 = closes.iloc[-10]
    price_high2 = closes.iloc[-3]
    rsi_high1 = rsi_vals.iloc[-10]
    rsi_high2 = rsi_vals.iloc[-3]

    price_low1 = closes.iloc[-10]
    price_low2 = closes.iloc[-3]
    rsi_low1 = rsi_vals.iloc[-10]
    rsi_low2 = rsi_vals.iloc[-3]

    # دايفرجنس بيعي: السعر يصعد و RSI يهبط
    if price_high2 > price_high1 and rsi_high2 < rsi_high1:
        return "bearish"

    # دايفرجنس شرائي: السعر يهبط و RSI يصعد
    if price_low2 < price_low1 and rsi_low2 > rsi_low1:
        return "bullish"

    return None

# ============================================
# FIBONACCI LEVELS
# ============================================

def fib_levels(high_price, low_price):
    diff = high_price - low_price
    return {
        "0.236": low_price + diff * 0.236,
        "0.382": low_price + diff * 0.382,
        "0.5": low_price + diff * 0.5,
        "0.618": low_price + diff * 0.618,
        "0.786": low_price + diff * 0.786,
    }

def detect_swing_points(df, lookback=5):
    """
    اكتشاف قمم وقيعان بسيطة لاستخدامها في فيبوناتشي و SMC.
    """
    highs = df["high"]
    lows = df["low"]

    swing_highs = []
    swing_lows = []

    for i in range(lookback, len(df) - lookback):
        window_high = highs.iloc[i - lookback:i + lookback + 1]
        window_low = lows.iloc[i - lookback:i + lookback + 1]

        if highs.iloc[i] == window_high.max():
            swing_highs.append(i)
        if lows.iloc[i] == window_low.min():
            swing_lows.append(i)

    return swing_highs, swing_lows

def fib_zone_score(df, side: str):
    """
    يعطي نقاط إذا السعر قريب من مناطق فيبو الانعكاسية.
    للشراء نفضّل 0.5 - 0.618 - 0.786 من قاع إلى قمة.
    للبيع نفضّل 0.5 - 0.618 - 0.786 من قمة إلى قاع.
    """
    if len(df) < 100:
        return 0

    high_price = df["high"].rolling(100).max().iloc[-1]
    low_price = df["low"].rolling(100).min().iloc[-1]
    price = df["close"].iloc[-1]

    if side == "long":
        fibs = fib_levels(low_price, high_price)  # من قاع إلى قمة
    else:
        fibs = fib_levels(high_price, low_price)  # من قمة إلى قاع

    score = 0
    for lvl_name, lvl_price in fibs.items():
        diff = abs(price - lvl_price) / max(lvl_price, 1e-8)
        if diff < 0.005:  # قريب جداً من مستوى مهم
            score += 2
        elif diff < 0.01:
            score += 1

    return score

# ============================================
# SIMPLE ELLIOTT PHASE (APPROX)
# ============================================

def elliott_phase(df):
    """
    تقدير بسيط لمرحلة الحركة (دافعة / تصحيحية) بناءً على الزخم والتذبذب.
    """
    if len(df) < 50:
        return "neutral"

    closes = df["close"]
    recent = closes.iloc[-40:]
    ret = recent.pct_change().dropna()

    vol = ret.std()
    direction = recent.iloc[-1] - recent.iloc[0]

    if vol > 0.02 and abs(direction) > abs(recent.mean()) * 0.5:
        return "impulsive"  # موجة دافعة قوية
    if vol < 0.01:
        return "corrective"  # تصحيح هادئ
    return "neutral"

# ============================================
# ORDERBOOK & LIQUIDITY (BASIC SCORES)
# ============================================

def orderbook_imbalance(symbol: str):
    """
    يرجع قيمة تقريبية لعدم توازن الطلب/العرض من الكاش.
    """
    ob = orderbook_cache.get(symbol)
    if not ob:
        return 0

    bids = ob.get("bids", [])
    asks = ob.get("asks", [])

    bid_vol = sum(b[1] for b in bids[:10]) if bids else 0
    ask_vol = sum(a[1] for a in asks[:10]) if asks else 0

    if bid_vol + ask_vol == 0:
        return 0

    imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)

    if imbalance > 0.25:
        return 3
    if imbalance > 0.1:
        return 2
    if imbalance > 0.03:
        return 1
    if imbalance < -0.25:
        return -3
    if imbalance < -0.1:
        return -2
    if imbalance < -0.03:
        return -1
    return 0

def liquidity_heatmap_score(symbol: str):
    """
    درجة تقريبية لقرب السعر من مناطق سيولة قوية.
    """
    lm = liquidity_map.get(symbol)
    if not lm:
        return 0

    price = price_cache.get(symbol)
    if not price:
        return 0

    best_score = 0
    for lvl in lm.get("levels", []):
        lvl_price = lvl.get("price")
        lvl_side = lvl.get("side")  # "buy" أو "sell"
        lvl_liq = lvl.get("liq", 0)

        if lvl_price and lvl_liq > 0:
            diff = abs(price - lvl_price) / max(lvl_price, 1e-8)
            if diff < 0.003:  # قريب جداً من مستوى سيولة
                if lvl_side == "buy":
                    best_score += 2
                elif lvl_side == "sell":
                    best_score -= 2
            elif diff < 0.01:
                if lvl_side == "buy":
                    best_score += 1
                elif lvl_side == "sell":
                    best_score -= 1

    return best_score

# ============================================
# MARKET MAKER MODEL (PREMIUM / DISCOUNT)
# ============================================

def market_maker_model(df):
    """
    تقسيم بسيط للسعر بالنسبة للمدى الأخير:
    Premium / Discount / Equilibrium
    """
    if len(df) < 200:
        return "equilibrium"

    high_price = df["high"].rolling(200).max().iloc[-1]
    low_price = df["low"].rolling(200).min().iloc[-1]
    price = df["close"].iloc[-1]

    mid = (high_price + low_price) / 2

    if price > mid * 1.02:
        return "premium"
    if price < mid * 0.98:
        return "discount"
    return "equilibrium"

# ============================================
# FVG DETECTION (1H / 4H)
# ============================================

def detect_fvg(df, min_body_ratio=0.6):
    """
    اكتشاف FVG بسيط:
    - فجوة بين شمعتين متتاليتين
    - جسم شمعة قوي
    يعاد: قائمة من dict فيها:
      {"index": i, "direction": "bullish"/"bearish"}
    """
    fvg_list = []
    if len(df) < 5:
        return fvg_list

    o = df["open"]
    h = df["high"]
    l = df["low"]
    c = df["close"]

    for i in range(2, len(df)):
        # فجوة صاعدة: قاع الشمعة الحالية أعلى من قمة الشمعة السابقة
        if l.iloc[i] > h.iloc[i - 1]:
            body = abs(c.iloc[i] - o.iloc[i])
            rng = h.iloc[i] - l.iloc[i]
            if rng > 0 and body / rng >= min_body_ratio:
                fvg_list.append({"index": i, "direction": "bullish"})

        # فجوة هابطة: قمة الشمعة الحالية أسفل من قاع الشمعة السابقة
        if h.iloc[i] < l.iloc[i - 1]:
            body = abs(c.iloc[i] - o.iloc[i])
            rng = h.iloc[i] - l.iloc[i]
            if rng > 0 and body / rng >= min_body_ratio:
                fvg_list.append({"index": i, "direction": "bearish"})

    return fvg_list

def fvg_score(df, side: str):
    """
    يعطي نقاط بناءً على وجود FVG حديثة تدعم الاتجاه.
    """
    fvgs = detect_fvg(df)
    if not fvgs:
        return 0

    last_fvg = fvgs[-1]
    direction = last_fvg["direction"]

    if side == "long" and direction == "bullish":
        return 2
    if side == "short" and direction == "bearish":
        return 2
    return 0

# ============================================
# SMC SCORE (محسّن)
# ============================================

def smc_score(df1h):
    """
    تقدير نقاط SMC على فريم الساعة:
    - BOS / CHOCH
    - FVG
    - Sweeps
    - هيكل HH/HL/LH/LL
    """
    if len(df1h) < 60:
        return 0

    score = 0
    closes = df1h["close"]
    highs = df1h["high"]
    lows = df1h["low"]
    opens = df1h["open"]

    # BOS / CHOCH بسيط: اختراق قمة/قاع آخر 20 شمعة
    recent_high = highs.rolling(20).max().iloc[-2]
    recent_low = lows.rolling(20).min().iloc[-2]
    last_close = closes.iloc[-1]

    if last_close > recent_high:
        score += 3  # BOS up قوي
    if last_close < recent_low:
        score -= 3  # BOS down قوي

    # Sweeps: كسر قمة/قاع ثم إغلاق عكسي
    high_prev = highs.iloc[-2]
    low_prev = lows.iloc[-2]
    high_curr = highs.iloc[-1]
    low_curr = lows.iloc[-1]
    close_curr = closes.iloc[-1]

    # Sweep sell-side (كسر قاع ثم إغلاق فوقه)
    if low_curr < low_prev and close_curr > low_prev:
        score += 2

    # Sweep buy-side (كسر قمة ثم إغلاق تحتها)
    if high_curr > high_prev and close_curr < high_prev:
        score -= 2

    # FVG على فريم الساعة
    fvg_1h = fvg_score(df1h, "long" if last_close > recent_high else "short")
    score += fvg_1h

    # هيكل HH/HL/LH/LL بسيط
    swing_highs, swing_lows = detect_swing_points(df1h, lookback=3)
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        last_sh1 = swing_highs[-1]
        last_sh2 = swing_highs[-2]
        last_sl1 = swing_lows[-1]
        last_sl2 = swing_lows[-2]

        # اتجاه صاعد: HH + HL
        if highs.iloc[last_sh1] > highs.iloc[last_sh2] and lows.iloc[last_sl1] > lows.iloc[last_sl2]:
            score += 2
        # اتجاه هابط: LH + LL
        if highs.iloc[last_sh1] < highs.iloc[last_sh2] and lows.iloc[last_sl1] < lows.iloc[last_sl2]:
            score -= 2

    return score

# ============================================
# CLASSIC CANDLE / MOMENTUM SCORE
# ============================================

def classic_candle_score(df15):
    """
    تقييم بسيط للشموع على فريم التنفيذ (15m):
    - شموع ابتلاعية
    - شموع انعكاسية
    - زخم
    """
    if len(df15) < 5:
        return 0

    o = df15["open"].iloc[-1]
    c = df15["close"].iloc[-1]
    h = df15["high"].iloc[-1]
    l = df15["low"].iloc[-1]

    body = abs(c - o)
    range_ = h - l
    if range_ == 0:
        return 0

    body_ratio = body / range_

    score = 0

    # شمعة ابتلاعية قوية
    prev_o = df15["open"].iloc[-2]
    prev_c = df15["close"].iloc[-2]
    prev_body = abs(prev_c - prev_o)

    if body > prev_body * 1.5 and body_ratio > 0.6:
        if c > o:
            score += 2  # ابتلاعية صاعدة
        else:
            score -= 2  # ابتلاعية هابطة

    # شمعة ذات ذيل طويل (رفض سعري)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    if lower_wick > body * 1.5:
        score += 1  # رفض هبوط (إيجابي)
    if upper_wick > body * 1.5:
        score -= 1  # رفض صعود (سلبي)

    return score

# ============================================
# TRADE LEVELS + RR
# ============================================

def trade_levels(price, atr_val, side):
    if atr_val is None or np.isnan(atr_val) or atr_val == 0:
        return None, None, None, None

    # استخدام ATR أكثر تحفظاً
    risk_mult = 1.8
    reward_mult = 4.5

    if side == "long":
        entry = price
        sl = price - atr_val * risk_mult
        tp = price + atr_val * reward_mult
        rr = (tp - entry) / max(entry - sl, 1e-8)
    else:
        entry = price
        sl = price + atr_val * risk_mult
        tp = price - atr_val * reward_mult
        rr = (entry - tp) / max(sl - entry, 1e-8)

    return entry, sl, tp, rr

# ============================================
# PROBABILITY ENGINE
# ============================================

def probability_score(score):
    """
    تحويل مجموع النقاط إلى نسبة نجاح تقريبية.
    """
    base = 65
    prob = base + abs(score) * 2.5
    if prob > 96:
        prob = 96
    if prob < 60:
        prob = 60
    return int(prob)

# ============================================
# CLUSTER DELTA PRO — A2 FULL
# ============================================

async def websocket_aggtrades():
    """
    WebSocket لتجميع AggTrades لكل عملة وبناء Cluster Delta كامل:
    - Bid/Ask per trade
    - Delta per price level
    - CVD (Cumulative Volume Delta)
    - Imbalance / Absorption / Aggression
    """
    streams = "/".join([s.lower() + "@aggTrade" for s in SYMBOLS])
    url = f"wss://fstream.binance.com/stream?streams={streams}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                while True:
                    msg = await ws.recv()
                    try:
                        data = json.loads(msg)
                        payload = data.get("data", {})
                        symbol = payload.get("s")
                        price = float(payload.get("p", 0))
                        qty = float(payload.get("q", 0))
                        is_buyer_maker = payload.get("m", True)  # True = sell aggressor

                        if not symbol or price <= 0 or qty <= 0:
                            continue

                        # تحديد الشمعة الحالية (على فريم 1m تقريباً)
                        ts = int(payload.get("T", 0)) // 60000  # candle id بالدقيقة

                        # تهيئة الكلاستر للرمز
                        if symbol not in cluster_footprint:
                            cluster_footprint[symbol] = {}
                        if ts not in cluster_footprint[symbol]:
                            cluster_footprint[symbol][ts] = {}

                        # تقريب السعر لمستوى (Tick Cluster)
                        price_level = round(price, 2)

                        if price_level not in cluster_footprint[symbol][ts]:
                            cluster_footprint[symbol][ts][price_level] = {"bid": 0.0, "ask": 0.0}

                        # Aggressive buyer = taker buy = m=False
                        if is_buyer_maker:
                            # Aggressive seller → bid volume
                            cluster_footprint[symbol][ts][price_level]["bid"] += qty
                        else:
                            # Aggressive buyer → ask volume
                            cluster_footprint[symbol][ts][price_level]["ask"] += qty

                        # تحديث CVD
                        if symbol not in cluster_cache:
                            cluster_cache[symbol] = {"cvd": 0.0, "last_update": time.time()}

                        delta = qty if not is_buyer_maker else -qty
                        cluster_cache[symbol]["cvd"] += delta
                        cluster_cache[symbol]["last_update"] = time.time()

                    except Exception:
                        continue
        except Exception as e:
            print("AggTrades WS error:", e)
            await asyncio.sleep(5)

def cluster_delta_score(symbol: str):
    """
    حساب Score للكلاستر السعري (A2 Full):
    - Delta per price level
    - Imbalance
    - Absorption
    - CVD trend
    يعيد قيمة تقريبية من -10 إلى +10
    """
    if symbol not in cluster_footprint or symbol not in cluster_cache:
        return 0

    fp = cluster_footprint[symbol]
    if not fp:
        return 0

    # آخر شمعة كلاستر
    last_candle_id = max(fp.keys())
    levels = fp[last_candle_id]

    if not levels:
        return 0

    total_bid = 0.0
    total_ask = 0.0
    strong_imbalance = 0
    absorption_points = 0

    for price_level, vals in levels.items():
        bid = vals.get("bid", 0.0)
        ask = vals.get("ask", 0.0)
        total_bid += bid
        total_ask += ask

        if bid + ask == 0:
            continue

        # Imbalance قوي على مستوى السعر
        imbalance = (ask - bid) / (ask + bid)

        if imbalance > 0.6:
            strong_imbalance += 1  # ضغط شراء قوي
        elif imbalance < -0.6:
            strong_imbalance -= 1  # ضغط بيع قوي

        # Absorption: حجم كبير على جانب واحد مع عدم تحرك السعر كثيراً (تقريبي)
        if bid > ask * 3 and ask > 0:
            absorption_points -= 1  # امتصاص شراء (ضغط بيعي مخفي)
        if ask > bid * 3 and bid > 0:
            absorption_points += 1  # امتصاص بيع (ضغط شرائي مخفي)

    if total_bid + total_ask == 0:
        return 0

    # Delta عام
    delta_total = total_ask - total_bid
    delta_ratio = delta_total / (total_ask + total_bid)

    # CVD
    cvd_val = cluster_cache[symbol]["cvd"]

    score = 0

    # تأثير الدلتا العامة
    if delta_ratio > 0.3:
        score += 3
    elif delta_ratio > 0.1:
        score += 1
    elif delta_ratio < -0.3:
        score -= 3
    elif delta_ratio < -0.1:
        score -= 1

    # تأثير الـ Imbalance
    score += strong_imbalance

    # تأثير الـ Absorption
    score += absorption_points

    # تأثير CVD (تقريبي)
    if cvd_val > 0:
        score += 2
    elif cvd_val < 0:
        score -= 2

    # حصر النتيجة بين -10 و +10
    if score > 10:
        score = 10
    if score < -10:
        score = -10

    return score

# ============================================
# REASON BUILDER (ARABIC) — مؤسسي
# ============================================

def build_reason_text(
    symbol: str,
    side: str,
    trend_d: str,
    trend_4h: str,
    trend_1h: str,
    trend_15m: str,
    ell_phase: str,
    fib_score_total: int,
    smc: int,
    classic_score: int,
    ob_imb: int,
    liq_score: int,
    rsi_val: float,
    div: str,
    mm: str,
    cluster_score: int
) -> str:
    parts = []

    # اتجاه الفريمات
    parts.append(
        f"توافق واضح بين الإطارات الزمنية حيث يظهر على الفريم اليومي اتجاه "
        f"{'صاعد' if trend_d=='bullish' else 'هابط' if trend_d=='bearish' else 'متذبذب'} "
        f"وعلى فريم 4 ساعات اتجاه "
        f"{'صاعد' if trend_4h=='bullish' else 'هابط' if trend_4h=='bearish' else 'متذبذب'} "
        f"مع انسجام ملحوظ على فريم الساعة و 15 دقيقة."
    )

    # SMC
    if smc > 0:
        parts.append("هيكل السوق على فريم الساعة يدعم الاتجاه الحالي مع وجود إشارات SMC إيجابية (BOS / Sweeps / FVG) تعكس دخول سيولة ذكية.")
    elif smc < 0:
        parts.append("هيكل السوق على فريم الساعة يشير إلى ضغط معاكس مع إشارات SMC سلبية (كسر قمم/قيعان مهمة وسحب سيولة واضح).")

    # فيبوناتشي
    if fib_score_total > 0:
        parts.append("السعر يتفاعل مع مناطق فيبوناتشي انعكاسية مهمة على الفريمات الكبيرة مما يعزز منطقية منطقة الدخول الحالية.")

    # إليوت
    if ell_phase == "impulsive":
        parts.append("الحركة الحالية تبدو كموجة دافعة قوية وفق نمط إليوت، ما يدعم استمرار الاتجاه في نفس المسار.")
    elif ell_phase == "corrective":
        parts.append("السوق في مرحلة تصحيحية هادئة، ما يجعل مناطق الدخول الحالية أقرب لمناطق إعادة تجميع قبل استكمال الاتجاه.")

    # كلاسيكي / شموع
    if classic_score > 0:
        parts.append("الشموع الأخيرة على فريم التنفيذ تظهر ابتلاعًا أو رفضًا سعريًا واضحًا يدعم سيناريو الصفقة.")
    elif classic_score < 0:
        parts.append("الشموع الأخيرة تعكس تذبذبًا ورفضًا سعريًا عند مستويات حساسة، ما يعزز فكرة الانعكاس المحتمل.")

    # سيولة دفتر الأوامر
    if ob_imb > 0 or liq_score > 0:
        parts.append("توزيع السيولة في دفتر الأوامر يميل لصالح الاتجاه المقترح مع تكدس أوامر مؤسسية بالقرب من مناطق الدخول.")
    elif ob_imb < 0 or liq_score < 0:
        parts.append("توزيع السيولة يظهر ضغطًا من الجانب المعاكس، ما يجعل إدارة المخاطرة في هذه الصفقة أكثر أهمية.")

    # MM Model
    if mm == "discount" and side == "long":
        parts.append("السعر يتحرك في منطقة خصم (Discount) بالنسبة للمدى السعري الأخير، ما يجعل الشراء من هذه المستويات منطقيًا.")
    if mm == "premium" and side == "short":
        parts.append("السعر يتحرك في منطقة مبالغة سعرية (Premium) بالنسبة للمدى السعري الأخير، ما يجعل البيع من هذه المستويات منطقيًا.")

    # Cluster Delta A2 FULL
    if cluster_score > 2 and side == "long":
        parts.append("تدفقات أوامر الشراء على مستوى الكلاستر (Delta / CVD) تدعم استمرار الصعود من هذه المناطق.")
    elif cluster_score < -2 and side == "short":
        parts.append("تدفقات أوامر البيع على مستوى الكلاستر (Delta / CVD) تدعم استمرار الهبوط من هذه المناطق.")
    elif abs(cluster_score) <= 2:
        parts.append("توزيع أوامر الكلاستر متوازن نسبياً، ما يجعل القرار يعتمد أكثر على هيكل السعر والفريمات الأكبر.")

    # RSI / Divergence
    if side == "long":
        if rsi_val <= 30:
            parts.append("مؤشر RSI في منطقة تشبع بيعي مما يدعم سيناريو الارتداد الصاعد من هذه المستويات.")
        if div == "bullish":
            parts.append("يوجد دايفرجنس شرائي واضح بين السعر و RSI يعزز احتمالية الانعكاس للأعلى.")
    else:
        if rsi_val >= 65:
            parts.append("مؤشر RSI في منطقة تشبع شرائي مما يدعم سيناريو التصحيح الهابط من هذه المستويات.")
        if div == "bearish":
            parts.append("يوجد دايفرجنس بيعي واضح بين السعر و RSI يعزز احتمالية الانعكاس للأسفل.")

    return " ".join(parts)

# ============================================
# MAIN SIGNAL EVALUATION
# ============================================

def evaluate_signal(symbol: str):
    """
    تقييم صفقة واحدة على كل الفريمات مع كل الفلاتر:
    - SMC + FVG + كلاسيكي + فيبو + إليوت + EMA200 + RSI + ATR + سيولة + MM Model + Cluster Delta A2 FULL
    - R:R بين 1:2.5 و 1:8
    - نسبة نجاح ≥ 70%
    - تمييز فوري / معلّق حسب قرب السعر من الدخول
    """
    tf = load_timeframes(symbol)
    if not tf:
        return None

    df1d, df4h, df1h, df15 = tf

    # اتجاهات
    trend_d = trend_filter(df1d)
    trend_4h = trend_filter(df4h)
    trend_1h = trend_filter(df1h)
    trend_15m = trend_filter(df15)

    # SMC على الساعة
    smc = smc_score(df1h)

    # سيولة دفتر الأوامر
    ob_imb = orderbook_imbalance(symbol)
    liq_score = liquidity_heatmap_score(symbol)

    # MM Model على 4 ساعات
    mm = market_maker_model(df4h)

    # مؤشرات
    rsi_val = df15["rsi"].iloc[-1]
    atr_val = df15["atr"].iloc[-1]
    div = rsi_divergence(df15)
    price = df15["close"].iloc[-1]

    # فيبوناتشي (على 1D / 4H / 1H)
    fib_d = fib_zone_score(df1d, "long" if trend_d == "bullish" else "short")
    fib_4h = fib_zone_score(df4h, "long" if trend_4h == "bullish" else "short")
    fib_1h = fib_zone_score(df1h, "long" if trend_1h == "bullish" else "short")
    fib_total = fib_d + fib_4h + fib_1h

    # إليوت (على 4H)
    ell_phase = elliott_phase(df4h)

    # كلاسيكي / شموع على 15m
    classic_score = classic_candle_score(df15)

    # EMA200 فلتر اتجاه عام
    ema_d = df1d["ema200"].iloc[-1]
    ema_4h = df4h["ema200"].iloc[-1]

    # Cluster Delta A2 FULL
    cluster_score = cluster_delta_score(symbol)

    # ================= LONG CONDITIONS =================
    long_ok = False
    if trend_d == "bullish":
        if price > ema_d and price > ema_4h:
            if rsi_val <= 35 or div == "bullish":
                long_ok = True

    # ================= SHORT CONDITIONS =================
    short_ok = False
    if trend_d == "bearish":
        if price < ema_d and price < ema_4h:
            if rsi_val >= 65 or div == "bearish":
                short_ok = True

    # استثناء: صفقة عكس الاتجاه على 15m فقط عند تصحيح قوي جداً
    counter_ok = False
    if trend_d == "bullish" and rsi_val >= 75 and ell_phase == "corrective":
        counter_ok = True  # شورت عكسي على 15m
    if trend_d == "bearish" and rsi_val <= 25 and ell_phase == "corrective":
        counter_ok = True  # لونج عكسي على 15m

    side = None
    if long_ok:
        side = "long"
    elif short_ok:
        side = "short"
    elif counter_ok:
        side = "short" if trend_d == "bullish" else "long"

    if side is None:
        return None

    # MM Model فلتر
    if side == "long" and mm == "premium":
        return None
    if side == "short" and mm == "discount":
        return None

    # تجميع نقاط
    score = 0
    score += smc
    score += ob_imb
    score += liq_score
    score += fib_total
    score += classic_score
    score += cluster_score  # إضافة تأثير الكلاستر

    # إليوت
    if ell_phase == "impulsive":
        score += 2
    elif ell_phase == "corrective":
        score += 1

    # فلتر R:R + ATR
    entry, sl, tp, rr = trade_levels(price, atr_val, side)
    if not entry or rr is None:
        return None
    if rr < 2.5 or rr > 8:
        return None

    # نسبة النجاح
    prob = probability_score(score)
    if prob < 70:
        return None

    # تحديد فوري / معلّق حسب قرب السعر من الدخول
    live_price = price_cache.get(symbol, price)
    diff = abs(live_price - entry) / max(entry, 1e-8)
    is_instant = diff <= 0.01  # 1% انحراف مسموح

    # بناء سبب الصفقة (مؤسسي + Cluster A2 FULL)
    reason_text = build_reason_text(
        symbol,
        side,
        trend_d,
        trend_4h,
        trend_1h,
        trend_15m,
        ell_phase,
        fib_total,
        smc,
        classic_score,
        ob_imb,
        liq_score,
        rsi_val,
        div,
        mm,
        cluster_score
    )

    return {
        "symbol": symbol,
        "side": "Long" if side == "long" else "Short",
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "prob": prob,
        "score": score,
        "is_instant": is_instant,
        "reason_text": reason_text
    }

# ============================================
# SCAN MARKET — MANUAL (أفضل صفقتين)
# ============================================

def scan_market_manual():
    """
    فحص السوق عند إرسال كلمة (صفقات)
    يعيد أفضل صفقتين فقط — مرتبتين حسب نسبة النجاح
    """
    results = []

    for symbol in SYMBOLS:
        try:
            sig = evaluate_signal(symbol)
            if sig:
                results.append(sig)
        except Exception:
            continue

    if not results:
        return []

    # ترتيب حسب نسبة النجاح
    results = sorted(results, key=lambda x: x["prob"], reverse=True)

    # أفضل صفقتين فقط
    return results[:2]

# ============================================
# SCAN MARKET — AUTO (صفقة واحدة فقط)
# ============================================

def scan_market_auto():
    """
    فحص آلي — يعيد أفضل صفقة واحدة فقط بنسبة نجاح ≥ 70%
    ولا يكرر نفس العملة خلال ساعة
    """
    best = None

    for symbol in SYMBOLS:
        # فلتر عدم التكرار
        last_time = last_signal_time_auto.get(symbol)
        if last_time and time.time() - last_time < 3600:  # ساعة
            continue

        try:
            sig = evaluate_signal(symbol)
            if not sig:
                continue

            if sig["prob"] < 70:
                continue

            if best is None or sig["prob"] > best["prob"]:
                best = sig

        except Exception:
            continue

    return best

# ============================================
# TELEGRAM HANDLER — صفقات (يدوي)
# ============================================

async def signals(update, context):
    chat_id = update.message.chat_id
    await update.message.reply_text("جارٍ الفحص ...⏳")

    sigs = scan_market_manual()

    if not sigs:
        await update.message.reply_text(format_no_signal())
        return

    # تحديث وقت آخر صفقة يدوية
    for s in sigs:
        last_signal_time_manual[s["symbol"]] = time.time()

    # إرسال الرسالة بصيغة مطابقة 1:1
    msg = format_manual_signals(sigs)
    await update.message.reply_text(msg)

# ============================================
# AUTO SCAN TASK — كل 10 دقائق
# ============================================

async def auto_scan_task(app):
    while True:
        try:
            sig = scan_market_auto()

            if sig:
                symbol = sig["symbol"]
                last_signal_time_auto[symbol] = time.time()

                msg = format_auto_signal(sig)

                # إرسال لجميع الشاتات النشطة
                for chat in list(active_chats):
                    try:
                        await app.bot.send_message(chat, msg)
                    except Exception:
                        continue

        except Exception as e:
            print("Auto scan error:", e)

        await asyncio.sleep(600)  # كل 10 دقائق

# ============================================
# PENDING ENTRY MONITOR — مراقبة الدخول المعلّق
# ============================================

async def pending_monitor(app):
    """
    يراقب السعر الحي عبر WebSocket
    ويرسل رسالة تذكير عند وصول السعر إلى منطقة الدخول
    (للصفقات المعلقة فقط)
    """
    while True:
        try:
            for symbol, price in list(price_cache.items()):
                sig = evaluate_signal(symbol)
                if not sig:
                    continue

                if sig["is_instant"]:
                    continue  # هذه صفقة فورية، لا تحتاج تذكير

                entry = sig["entry"]
                side = sig["side"]
                prob = sig["prob"]

                diff = abs(price - entry) / max(entry, 1e-8)

                if diff <= 0.01:  # 1% انحراف مسموح
                    alert = format_pending_entry_alert(
                        symbol, side, price, entry, prob
                    )

                    for chat in list(active_chats):
                        try:
                            await app.bot.send_message(chat, alert)
                        except Exception:
                            continue

        except Exception as e:
            print("Pending monitor error:", e)

        await asyncio.sleep(5)  # تحديث سريع كل 5 ثوانٍ

# ============================================
# TELEGRAM HANDLER — تحليل (يدوي)
# ============================================

async def analysis(update, context):
    chat_id = update.message.chat_id
    await update.message.reply_text("جارٍ التحليل ...💱")

    # تحليل الأخبار بالعربي
    news_comment = analyze_news_arabic()

    # أفضل 5 عملات نشاطًا
    top5 = get_top5_active()

    coins_analysis = []

    for symbol in top5:
        tf = load_timeframes(symbol)
        if not tf:
            continue

        df1d, df4h, df1h, df15 = tf

        trend_d = trend_filter(df1d)
        trend_4h = trend_filter(df4h)
        trend_1h = trend_filter(df1h)
        trend_15m = trend_filter(df15)

        # توقع بسيط
        last = df1h["close"].iloc[-1]
        prev = df1h["close"].iloc[-10]
        direction = "صعود" if last > prev else "هبوط"
        percent = round(abs((last - prev) / max(prev, 1e-8)) * 100, 2)

        expectation = f"{percent}% {direction} خلال الساعات القادمة"

        coins_analysis.append({
            "symbol": symbol,
            "trend_1d": f"اتجاه {'صاعد' if trend_d=='bullish' else 'هابط' if trend_d=='bearish' else 'متذبذب'}",
            "trend_4h": f"اتجاه {'صاعد' if trend_4h=='bullish' else 'هابط' if trend_4h=='bearish' else 'متذبذب'}",
            "trend_1h": f"زخم {'إيجابي' if trend_1h=='bullish' else 'سلبي' if trend_1h=='bearish' else 'ضعيف'}",
            "trend_15m": f"سلوك {'إيجابي' if trend_15m=='bullish' else 'سلبي' if trend_15m=='bearish' else 'متذبذب'}",
            "expectation": expectation
        })

    # إرسال التحليل بصيغة مطابقة 1:1
    msg = format_daily_report(news_comment, coins_analysis)
    await update.message.reply_text(msg)

# ============================================
# FASTAPI APP
# ============================================


@app_api.get("/ping")
def ping():
    return {"status": "alive"}

@app_api.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            data = await ws.receive_text()
            await ws.send_text(f"Message: {data}")
    except Exception:
        await ws.close()

def run_api():
    uvicorn.run(
        app_api,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080"))
    )

# ============================================
# WEBSOCKETS — PRICE STREAM
# ============================================

async def websocket_price_stream():
    """
    WebSocket لأسعار العقود الدائمة من بايننس فيوتشرز.
    يحدث price_cache بشكل لحظي.
    مع إعادة اتصال تلقائية عند الانقطاع.
    """
    streams = "/".join([s.lower() + "@markPrice" for s in SYMBOLS])
    url = f"wss://fstream.binance.com/stream?streams={streams}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                while True:
                    msg = await ws.recv()
                    try:
                        data = json.loads(msg)
                        payload = data.get("data", {})
                        symbol = payload.get("s")
                        price = float(payload.get("p", 0))

                        if symbol and price > 0:
                            price_cache[symbol] = price
                    except Exception:
                        continue
        except Exception as e:
            print("Price WS error:", e)
            await asyncio.sleep(5)

# ============================================
# WEBSOCKETS — ORDERBOOK STREAM
# ============================================

async def websocket_orderbook():
    """
    WebSocket للأوردر بوك (عمق السوق) لتقدير السيولة والـ imbalance.
    يحدث orderbook_cache و liquidity_map.
    مع إعادة اتصال تلقائية عند الانقطاع.
    """
    streams = "/".join([s.lower() + "@depth10@100ms" for s in SYMBOLS])
    url = f"wss://fstream.binance.com/stream?streams={streams}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                while True:
                    msg = await ws.recv()
                    try:
                        data = json.loads(msg)
                        payload = data.get("data", {})
                        symbol = payload.get("s")
                        bids = payload.get("b", [])
                        asks = payload.get("a", [])

                        if not symbol:
                            continue

                        # تحويل إلى أرقام
                        bids_parsed = [(float(p), float(q)) for p, q in bids]
                        asks_parsed = [(float(p), float(q)) for p, q in asks]

                        orderbook_cache[symbol] = {
                            "bids": bids_parsed,
                            "asks": asks_parsed
                        }

                        # بناء خريطة سيولة بسيطة
                        levels = []
                        for p, q in bids_parsed[:5]:
                            levels.append({"price": p, "liq": q, "side": "buy"})
                        for p, q in asks_parsed[:5]:
                            levels.append({"price": p, "liq": q, "side": "sell"})

                        liquidity_map[symbol] = {"levels": levels}

                    except Exception:
                        continue
        except Exception as e:
            print("Orderbook WS error:", e)
            await asyncio.sleep(5)

# ============================================
# BOOT TASKS
# ============================================

async def BOOT(app):
    print("🚀 Booting institutional trading bot...")

    asyncio.create_task(websocket_price_stream())
    asyncio.create_task(websocket_orderbook())
    asyncio.create_task(websocket_aggtrades())   # A2 FULL Cluster Delta
    asyncio.create_task(auto_scan_task(app))
    asyncio.create_task(pending_monitor(app))

    print("✅ System online")

# ============================================
# TELEGRAM MESSAGE ROUTER
# ============================================

async def handle(update, context):
    """
    موجه الرسائل النصية:
    - "صفقات" → أفضل صفقتين
    - "تحليل" → تحليل يومي
    - غير ذلك → رسالة مساعدة بسيطة
    """
    chat_id = update.message.chat_id
    text = (update.message.text or "").strip()

    active_chats.add(chat_id)

    if text == "صفقات":
        await signals(update, context)
    elif text == "تحليل":
        await analysis(update, context)
    else:
        msg = (
            "مرحباً 👋\n"
            "استخدم الأزرار بالأسفل أو أرسل:\n"
            "- كلمة (صفقات) لاستخراج أفضل الفرص الحالية.\n"
            "- كلمة (تحليل) للحصول على نظرة يومية عامة."
        )
        await update.message.reply_text(msg, reply_markup=keyboard)

# ============================================
# MAIN
# ============================================

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set in environment variables")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    loop = asyncio.get_event_loop()
    loop.create_task(BOOT(application))

    return application


if __name__ == "__main__":
    # تشغيل FastAPI
    threading.Thread(target=run_api).start()

    # تشغيل بوت التليجرام + تفعيل الويبهوك
    app_instance = main()

    asyncio.get_event_loop().run_until_complete(app_instance.initialize())
    asyncio.get_event_loop().run_until_complete(app_instance.start())
    asyncio.get_event_loop().run_until_complete(
        app_instance.bot.set_webhook("https://ahmedbot-cryp-auto.fly.dev/webhook")
    )
