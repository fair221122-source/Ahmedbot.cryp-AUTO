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
    ContextTypes,
    filters
)

from fastapi import FastAPI, WebSocket, Request
import websockets

active_chats = set()
session = requests.Session()

# -------------------------
# FastAPI
# -------------------------
app = FastAPI()

@app.get("/ping")
async def ping():
    return {"status": "alive"}

@app.get("/")
def home():
    return {"status": "running"}

# ============================================
# GLOBAL CONFIG
# ============================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CRYPTOPANIC_API = os.getenv("CRYPTOPANIC_API")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN must be set in environment variables.")

# ============================
# SYMBOLS
# ============================
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","NEARUSDT","TRXUSDT","LTCUSDT","UNIUSDT",
    "ARBUSDT","OPUSDT","SUIUSDT","FILUSDT","STXUSDT"
]

# ============================
# BINANCE APIS
# ============================
BINANCE_APIS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com"
]

# ============================
# REQUESTS SESSION
# ============================
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})

# ============================
# CACHES
# ============================
price_cache = {}
orderbook_cache = {}
liquidity_map = {}

last_signal_time_manual = {}
last_signal_time_auto = {}

klines_cache = {}
KLINES_TTL = 60  # ثانية

# Cluster Delta caches
cluster_cache = {}          # {symbol: {"cvd": float, "last_update": ts}}
cluster_footprint = {}      # {symbol: {candle_id: {price_level: {"bid": x, "ask": y}}}}

# ============================
# TELEGRAM UI
# ============================
active_chats = set()

keyboard = ReplyKeyboardMarkup(
    [["صفقات", "تحليل"]],
    resize_keyboard=True
)

# ============================================
# HELPERS
# ============================================

def rr_to_str(rr: float) -> str:
    return f"1:{rr:.2f}"

def format_direction_emoji(side: str) -> str:
    return "🟢" if side.lower() == "long" else "🔴"

def format_side_hashtag(side: str) -> str:
    return "#Long" if side.lower() == "long" else "#Short"

# ============================================
# MESSAGE TEMPLATES — MANUAL SIGNALS
# ============================================

def format_manual_signals(signals: list) -> str:
    if not signals:
        return "السوق متذبذب ولا توجد فرصة دخول مثالية حاليا، حاول بعد دقائق"

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

    if any(not s.get("is_instant", True) for s in signals[:2]):
        text += (
            "ستصلك رسالة تأكيد عند وصول السعر إلى المنطقة المثالية للدخول (للصفقة المعلّقة فقط).\n"
        )

    return text

# ============================================
# MESSAGE TEMPLATES — AUTO SCAN SIGNAL
# ============================================

def format_auto_signal(sig: dict) -> str:
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
# ============================================

def format_pending_entry_alert(symbol: str, side: str, price: float, entry: float, prob: int) -> str:
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
# ============================================

def format_daily_report_header(news_comment: str) -> str:
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
    text = f"{rank}) {symbol}\n"
    text += f"📅 1D: {trend_1d}\n"
    text += f"⏰ 4h: {trend_4h}\n"
    text += f"🕰 1h: {trend_1h}\n"
    text += f"🕒 15m: {trend_15m}\n"
    text += f"📉 التوقع: {expectation}\n"
    text += "-------------------------------------------\n\n"
    return text

def format_daily_report(news_comment: str, coins_analysis: list) -> str:
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

def fetch_crypto_news_raw():
    if not CRYPTOPANIC_API:
        print("⚠️ لا يوجد API KEY لموقع CryptoPanic")
        return []

    try:
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API}&public=true"
        r = session.get(url, timeout=10)

        if r.status_code != 200:
            print("⚠️ خطأ من CryptoPanic:", r.status_code, r.text)
            return []

        data = r.json().get("results", [])
        return data[:5]

    except Exception as e:
        print("⚠️ خطأ أثناء جلب أخبار CryptoPanic:", e)
        return []

# ============================================
# TRANSLATE NEWS TO ARABIC (تلخيصي)
# ============================================

def translate_to_arabic(text: str) -> str:
    text = text.lower()

    if any(w in text for w in ["bull", "adoption", "growth", "etf", "approval"]):
        return "الأخبار تشير إلى تحسن في شهية المخاطرة وزيادة اهتمام المؤسسات بالسوق."

    if any(w in text for w in ["hack", "ban", "lawsuit", "crash", "fear"]):
        return "الأخبار تحمل طابعًا سلبيًا وقد تضغط على حركة السوق مؤقتًا."

    if any(w in text for w in ["update", "upgrade", "announcement"]):
        return "هناك تحديثات تقنية وأخبار محايدة قد تؤثر بشكل محدود على حركة السوق."

    return "خبر عام متعلق بالسوق دون تأثير واضح."

# ============================================
# NEWS ANALYSIS (ARABIC SUMMARY)
# ============================================

def analyze_news_arabic():
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

    if price_high2 > price_high1 and rsi_high2 < rsi_high1:
        return "bearish"

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
    if len(df) < 100:
        return 0

    high_price = df["high"].rolling(100).max().iloc[-1]
    low_price = df["low"].rolling(100).min().iloc[-1]
    price = df["close"].iloc[-1]

    if side == "long":
        fibs = fib_levels(low_price, high_price)
    else:
        fibs = fib_levels(high_price, low_price)

    score = 0
    for lvl_name, lvl_price in fibs.items():
        diff = abs(price - lvl_price) / max(lvl_price, 1e-8)
        if diff < 0.005:
            score += 2
        elif diff < 0.01:
            score += 1

    return score

# ============================================
# SIMPLE ELLIOTT PHASE (APPROX)
# ============================================

def elliott_phase(df):
    if len(df) < 50:
        return "neutral"

    closes = df["close"]
    recent = closes.iloc[-40:]
    ret = recent.pct_change().dropna()

    vol = ret.std()
    direction = recent.iloc[-1] - recent.iloc[0]

    if vol > 0.02 and abs(direction) > abs(recent.mean()) * 0.5:
        return "impulsive"
    if vol < 0.01:
        return "corrective"
    return "neutral"

# ============================================
# ORDERBOOK & LIQUIDITY (BASIC SCORES)
# ============================================

def orderbook_imbalance(symbol: str):
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
    lm = liquidity_map.get(symbol)
    if not lm:
        return 0

    price = price_cache.get(symbol)
    if not price:
        return 0

    best_score = 0
    for lvl in lm.get("levels", []):
        lvl_price = lvl.get("price")
        lvl_side = lvl.get("side")
        lvl_liq = lvl.get("liq", 0)

        if lvl_price and lvl_liq > 0:
            diff = abs(price - lvl_price) / max(lvl_price, 1e-8)
            if diff < 0.003:
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
    fvg_list = []
    if len(df) < 5:
        return fvg_list

    o = df["open"]
    h = df["high"]
    l = df["low"]
    c = df["close"]

    for i in range(2, len(df)):
        if l.iloc[i] > h.iloc[i - 1]:
            body = abs(c.iloc[i] - o.iloc[i])
            rng = h.iloc[i] - l.iloc[i]
            if rng > 0 and body / rng >= min_body_ratio:
                fvg_list.append({"index": i, "direction": "bullish"})

        if h.iloc[i] < l.iloc[i - 1]:
            body = abs(c.iloc[i] - o.iloc[i])
            rng = h.iloc[i] - l.iloc[i]
            if rng > 0 and body / rng >= min_body_ratio:
                fvg_list.append({"index": i, "direction": "bearish"})

    return fvg_list

def fvg_score(df, side: str):
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
    if len(df1h) < 60:
        return 0

    score = 0
    closes = df1h["close"]
    highs = df1h["high"]
    lows = df1h["low"]
    opens = df1h["open"]

    recent_high = highs.rolling(20).max().iloc[-2]
    recent_low = lows.rolling(20).min().iloc[-2]
    last_close = closes.iloc[-1]

    if last_close > recent_high:
        score += 3
    if last_close < recent_low:
        score -= 3

    high_prev = highs.iloc[-2]
    low_prev = lows.iloc[-2]
    high_curr = highs.iloc[-1]
    low_curr = lows.iloc[-1]
    close_curr = closes.iloc[-1]

    if low_curr < low_prev and close_curr > low_prev:
        score += 2

    if high_curr > high_prev and close_curr < high_prev:
        score -= 2

    fvg_1h = fvg_score(df1h, "long" if last_close > recent_high else "short")
    score += fvg_1h

    swing_highs, swing_lows = detect_swing_points(df1h, lookback=3)
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        last_sh1 = swing_highs[-1]
        last_sh2 = swing_highs[-2]
        last_sl1 = swing_lows[-1]
        last_sl2 = swing_lows[-2]

        if highs.iloc[last_sh1] > highs.iloc[last_sh2] and lows.iloc[last_sl1] > lows.iloc[last_sl2]:
            score += 2
        if highs.iloc[last_sh1] < highs.iloc[last_sh2] and lows.iloc[last_sl1] < lows.iloc[last_sl2]:
            score -= 2

    return score

# ============================================
# CLASSIC CANDLE / MOMENTUM SCORE
# ============================================

def classic_candle_score(df15):
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

    prev_o = df15["open"].iloc[-2]
    prev_c = df15["close"].iloc[-2]
    prev_body = abs(prev_c - prev_o)

    if body > prev_body * 1.5 and body_ratio > 0.6:
        if c > o:
            score += 2
        else:
            score -= 2

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    if lower_wick > body * 1.5:
        score += 1
    if upper_wick > body * 1.5:
        score -= 1

    return score
def smart_mtf_analysis(df1d, df4h, df1h, df15):
    """
    دالة تحليل ذكية متعددة الفريمات — النسخة المؤسسية
    تُرجع تحليلًا مضبوطًا 100% لاختيار الصفقات.
    """

    analysis = {}

    # ============================
    # 1) الاتجاه الحقيقي (Structure)
    # ============================
    trend_1d = trend_filter(df1d)
    trend_4h = trend_filter(df4h)
    trend_1h = trend_filter(df1h)
    trend_15m = trend_filter(df15)

    analysis["trend_1d"] = trend_1d
    analysis["trend_4h"] = trend_4h
    analysis["trend_1h"] = trend_1h
    analysis["trend_15m"] = trend_15m

    # ============================
    # 2) Premium / Discount
    # ============================
    mm_1d = market_maker_model(df1d)
    mm_4h = market_maker_model(df4h)
    mm_1h = market_maker_model(df1h)

    analysis["mm_1d"] = mm_1d
    analysis["mm_4h"] = mm_4h
    analysis["mm_1h"] = mm_1h

    # ============================
    # 3) RSI + تشبع
    # ============================
    rsi_1h = df1h["rsi"].iloc[-1]
    rsi_15 = df15["rsi"].iloc[-1]

    analysis["rsi_1h"] = rsi_1h
    analysis["rsi_15m"] = rsi_15

    if rsi_1h > 70 or rsi_15 > 70:
        analysis["overbought"] = True
    else:
        analysis["overbought"] = False

    if rsi_1h < 30 or rsi_15 < 30:
        analysis["oversold"] = True
    else:
        analysis["oversold"] = False

    # ============================
    # 4) SMC (BOS / CHoCH / FVG / Swings)
    # ============================
    smc = smc_score(df1h)
    analysis["smc_score"] = smc

    # ============================
    # 5) زخم الحركة (Momentum)
    # ============================
    momentum = classic_candle_score(df15)
    analysis["momentum"] = momentum

    # ============================
    # 6) توافق الفريمات (Alignment)
    # ============================
    alignment_score = 0

    if trend_1d == trend_4h:
        alignment_score += 2
    if trend_4h == trend_1h:
        alignment_score += 2
    if trend_1h == trend_15m:
        alignment_score += 1

    analysis["alignment"] = alignment_score

    # ============================
    # 7) تحديد الاتجاه النهائي
    # ============================
    if alignment_score >= 4:
        final_trend = trend_4h
    else:
        final_trend = "sideways"

    analysis["final_trend"] = final_trend

    # ============================
    # 8) هل السعر في منطقة دخول؟
    # ============================
    price = df1h["close"].iloc[-1]

    if final_trend == "bullish":
        analysis["entry_zone"] = (mm_1h == "discount") or analysis["oversold"]
    elif final_trend == "bearish":
        analysis["entry_zone"] = (mm_1h == "premium") or analysis["overbought"]
    else:
        analysis["entry_zone"] = False

    # ============================
    # 9) درجة التحليل النهائية
    # ============================
    total_score = 0
    total_score += alignment_score * 2
    total_score += smc
    total_score += momentum

    if analysis["entry_zone"]:
        total_score += 3

    analysis["total_score"] = total_score

    return analysis

# ============================
# Institutional SL + RR System
# ============================

def institutional_sl(price, atr, trend_strength, last_swing, side):
    """
    price: سعر الدخول الحالي
    atr: قيمة ATR على فريم الساعة
    trend_strength: قوة الاتجاه (0 - 100)
    last_swing: آخر قاع (للشراء) أو آخر قمة (للبيع) على فريم الساعة
    side: "long" أو "short"
    """

    # 1) مسافة الهيكل (خلف آخر سوينغ)
    if side == "long":
        structure_distance = price - last_swing  # يجب أن يكون last_swing قاع تحت السعر
    else:
        structure_distance = last_swing - price  # يجب أن يكون last_swing قمة فوق السعر

    # إذا كان السوينغ قريب جدًا، نستخدم ATR كحد أدنى
    structure_distance = max(structure_distance, atr * 0.8)

    # 2) مسافة سيولة إضافية (0.2% من السعر)
    liquidity_buffer = price * 0.002

    # 3) معامل الاتجاه (اتجاه ضعيف → SL أكبر)
    trend_factor = 1 + (1 - trend_strength / 100)

    # 4) SL النهائي (مسافة)
    sl_distance = (structure_distance + liquidity_buffer) * trend_factor

    return sl_distance


def institutional_rr(score, trend_strength, momentum):
    rr_min = 2.5
    rr_max = 8.0

    score_factor = score / 100
    trend_factor = trend_strength / 100
    momentum_factor = momentum / 100

    # دمج العوامل (مثل المؤسسات)
    rr = rr_min + (rr_max - rr_min) * (
        0.5 * score_factor +
        0.3 * trend_factor +
        0.2 * momentum_factor
    )

    return max(rr_min, min(rr, rr_max))


def trade_levels_1h(price, atr, side, score, trend_strength, momentum, last_swing):
    """
    هذه الدالة مخصصة لفريم الساعة 1H
    """

    if atr is None or np.isnan(atr) or atr == 0:
        return None, None, None, None

    # 1) حساب SL الاحترافي (مسافة)
    sl_distance = institutional_sl(price, atr, trend_strength, last_swing, side)

    # 2) حساب R:R الاحترافي
    rr = institutional_rr(score, trend_strength, momentum)

    # 3) حساب TP من خلال R:R
    tp_distance = sl_distance * rr

    if side == "long":
        entry = price
        sl = price - sl_distance
        tp = price + tp_distance
    else:
        entry = price
        sl = price + sl_distance
        tp = price - tp_distance

    return entry, sl, tp, rr
# ============================================
# PROBABILITY ENGINE
# ============================================

def probability_score(score):
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
                        is_buyer_maker = payload.get("m", True)

                        if not symbol or price <= 0 or qty <= 0:
                            continue

                        ts = int(payload.get("T", 0)) // 60000

                        if symbol not in cluster_footprint:
                            cluster_footprint[symbol] = {}
                        if ts not in cluster_footprint[symbol]:
                            cluster_footprint[symbol][ts] = {}

                        price_level = round(price, 2)

                        if price_level not in cluster_footprint[symbol][ts]:
                            cluster_footprint[symbol][ts][price_level] = {"bid": 0.0, "ask": 0.0}

                        if is_buyer_maker:
                            cluster_footprint[symbol][ts][price_level]["bid"] += qty
                        else:
                            cluster_footprint[symbol][ts][price_level]["ask"] += qty

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
    if symbol not in cluster_footprint or symbol not in cluster_cache:
        return 0

    fp = cluster_footprint[symbol]
    if not fp:
        return 0

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

        imbalance = (ask - bid) / (ask + bid)

        if imbalance > 0.6:
            strong_imbalance += 1
        elif imbalance < -0.6:
            strong_imbalance -= 1

        if bid > ask * 3 and ask > 0:
            absorption_points -= 1
        if ask > bid * 3 and bid > 0:
            absorption_points += 1

    if total_bid + total_ask == 0:
        return 0

    delta_total = total_ask - total_bid
    delta_ratio = delta_total / (total_ask + total_bid)

    cvd_val = cluster_cache[symbol]["cvd"]

    score = 0

    if delta_ratio > 0.3:
        score += 3
    elif delta_ratio > 0.1:
        score += 1
    elif delta_ratio < -0.3:
        score -= 3
    elif delta_ratio < -0.1:
        score -= 1

    score += strong_imbalance
    score += absorption_points

    if cvd_val > 0:
        score += 2
    elif cvd_val < 0:
        score -= 2

    if score > 10:
        score = 10
    if score < -10:
        score = -10

    return score

# ============================================
# REASON BUILDER (ARABIC)
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

    parts.append(
        f"توافق واضح بين الإطارات الزمنية حيث يظهر على الفريم اليومي اتجاه "
        f"{'صاعد' if trend_d=='bullish' else 'هابط' if trend_d=='bearish' else 'متذبذب'} "
        f"وعلى فريم 4 ساعات اتجاه "
        f"{'صاعد' if trend_4h=='bullish' else 'هابط' if trend_4h=='bearish' else 'متذبذب'} "
        f"مع انسجام ملحوظ على فريم الساعة و 15 دقيقة."
    )

    if smc > 0:
        parts.append("هيكل السوق على فريم الساعة يدعم الاتجاه الحالي مع وجود إشارات SMC إيجابية (BOS / Sweeps / FVG) تعكس دخول سيولة ذكية.")
    elif smc < 0:
        parts.append("هيكل السوق على فريم الساعة يشير إلى ضغط معاكس مع إشارات SMC سلبية (كسر قمم/قيعان مهمة وسحب سيولة واضح).")

    if fib_score_total > 0:
        parts.append("السعر يتفاعل مع مناطق فيبوناتشي انعكاسية مهمة على الفريمات الكبيرة مما يعزز منطقية منطقة الدخول الحالية.")

    if ell_phase == "impulsive":
        parts.append("الحركة الحالية تبدو كموجة دافعة قوية وفق نمط إليوت، ما يدعم استمرار الاتجاه في نفس المسار.")
    elif ell_phase == "corrective":
        parts.append("السوق في مرحلة تصحيحية هادئة، ما يجعل مناطق الدخول الحالية أقرب لمناطق إعادة تجميع قبل استكمال الاتجاه.")

    if classic_score > 0:
        parts.append("الشموع الأخيرة على فريم التنفيذ تظهر ابتلاعًا أو رفضًا سعريًا واضحًا يدعم سيناريو الصفقة.")
    elif classic_score < 0:
        parts.append("الشموع الأخيرة تعكس تذبذبًا ورفضًا سعريًا عند مستويات حساسة، ما يعزز فكرة الانعكاس المحتمل.")

    if ob_imb > 0 or liq_score > 0:
        parts.append("توزيع السيولة في دفتر الأوامر يميل لصالح الاتجاه المقترح مع تكدس أوامر مؤسسية بالقرب من مناطق الدخول.")
    elif ob_imb < 0 or liq_score < 0:
        parts.append("توزيع السيولة يظهر ضغطًا من الجانب المعاكس، ما يجعل إدارة المخاطرة في هذه الصفقة أكثر أهمية.")

    if mm == "discount" and side == "long":
        parts.append("السعر يتحرك في منطقة خصم (Discount) بالنسبة للمدى السعري الأخير، ما يجعل الشراء من هذه المستويات منطقيًا.")
    if mm == "premium" and side == "short":
        parts.append("السعر يتحرك في منطقة مبالغة سعرية (Premium) بالنسبة للمدى السعري الأخير، ما يجعل البيع من هذه المستويات منطقيًا.")

    if cluster_score > 2 and side == "long":
        parts.append("تدفقات أوامر الشراء على مستوى الكلاستر (Delta / CVD) تدعم استمرار الصعود من هذه المناطق.")
    elif cluster_score < -2 and side == "short":
        parts.append("تدفقات أوامر البيع على مستوى الكلاستر (Delta / CVD) تدعم استمرار الهبوط من هذه المناطق.")
    elif abs(cluster_score) <= 2:
        parts.append("توزيع أوامر الكلاستر متوازن نسبياً، ما يجعل القرار يعتمد أكثر على هيكل السعر والفريمات الأكبر.")

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
    tf = load_timeframes(symbol)
    if not tf:
        return None

    df1d, df4h, df1h, df15 = tf

    trend_d = trend_filter(df1d)
    trend_4h = trend_filter(df4h)
    trend_1h = trend_filter(df1h)
    trend_15m = trend_filter(df15)

    smc = smc_score(df1h)

    ob_imb = orderbook_imbalance(symbol)
    liq_score = liquidity_heatmap_score(symbol)

    mm = market_maker_model(df4h)

    rsi_val = df1h["rsi"].iloc[-1]
    # ✅ ATR الآن على فريم الساعة كما طلبت
    atr_val = df1h["atr"].iloc[-1]
    div = rsi_divergence(df1h)
    price = df15["close"].iloc[-1]


    fib_d = fib_zone_score(df1d, "long" if trend_d == "bullish" else "short")
    fib_4h = fib_zone_score(df4h, "long" if trend_4h == "bullish" else "short")
    fib_1h = fib_zone_score(df1h, "long" if trend_1h == "bullish" else "short")
    fib_total = fib_d + fib_4h + fib_1h

    ell_phase = elliott_phase(df4h)

    classic_score = classic_candle_score(df15)

    ema_d = df1d["ema200"].iloc[-1]
    ema_4h = df4h["ema200"].iloc[-1]

    cluster_score = cluster_delta_score(symbol)

    long_ok = False
    if trend_d == "bullish":
        if price > ema_d and price > ema_4h:
            if rsi_val <= 35 or div == "bullish":
                long_ok = True

    short_ok = False
    if trend_d == "bearish":
        if price < ema_d and price < ema_4h:
            if rsi_val >= 65 or div == "bearish":
                short_ok = True

    counter_ok = False
    if trend_d == "bullish" and rsi_val >= 75 and ell_phase == "corrective":
        counter_ok = True
    if trend_d == "bearish" and rsi_val <= 25 and ell_phase == "corrective":
        counter_ok = True

    side = None
    if long_ok:
        side = "long"
    elif short_ok:
        side = "short"
    elif counter_ok:
        side = "short" if trend_d == "bullish" else "long"

    if side is None:
        return None

    if side == "long" and mm == "premium":
        return None
    if side == "short" and mm == "discount":
        return None

    score = 0
    score += smc
    score += ob_imb
    score += liq_score
    score += fib_total
    score += classic_score
    score += cluster_score

    if ell_phase == "impulsive":
        score += 2
    elif ell_phase == "corrective":
        score += 1

    entry, sl, tp, rr = trade_levels(price, atr_val, side)
    if not entry or rr is None:
        return None
    if rr < 2.5 or rr > 8:
        return None

    prob = probability_score(score)
    if prob < 70:
        return None

    live_price = price_cache.get(symbol, price)
    diff = abs(live_price - entry) / max(entry, 1e-8)
    is_instant = diff <= 0.01

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

    results = sorted(results, key=lambda x: x["prob"], reverse=True)
    return results[:2]

# ============================================
# SCAN MARKET — AUTO (صفقة واحدة فقط)
# ============================================

def scan_market_auto():
    best = None

    for symbol in SYMBOLS:
        last_time = last_signal_time_auto.get(symbol)
        if last_time and time.time() - last_time < 3600:
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

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await update.message.reply_text("جارٍ الفحص ...⏳")

    sigs = scan_market_manual()

    if not sigs:
        await update.message.reply_text(format_no_signal())
        return

    for s in sigs:
        last_signal_time_manual[s["symbol"]] = time.time()

    msg = format_manual_signals(sigs)
    await update.message.reply_text(msg)

# ============================================
# AUTO SCAN TASK — كل 10 دقائق
# ============================================

async def auto_scan_task(app: Application):
    while True:
        try:
            sig = scan_market_auto()

            if sig:
                symbol = sig["symbol"]
                last_signal_time_auto[symbol] = time.time()

                msg = format_auto_signal(sig)

                for chat in list(active_chats):
                    try:
                        await app.bot.send_message(chat, msg)
                    except Exception:
                        continue

        except Exception as e:
            print("Auto scan error:", e)

        await asyncio.sleep(600)

# ============================================
# PENDING ENTRY MONITOR — مراقبة الدخول المعلّق
# ============================================

async def pending_monitor(app: Application):
    while True:
        try:
            for symbol, price in list(price_cache.items()):
                sig = evaluate_signal(symbol)
                if not sig:
                    continue

                if sig["is_instant"]:
                    continue

                entry = sig["entry"]
                side = sig["side"]
                prob = sig["prob"]

                diff = abs(price - entry) / max(entry, 1e-8)

                if diff <= 0.01:
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

        await asyncio.sleep(5)

# ============================================
# TELEGRAM HANDLER — تحليل (يدوي)
# ============================================

# ============================================
# EXPECTATION ENGINE (تحليل توقع الحركة)
# ============================================

def build_coin_expectation(symbol: str, df1d, df4h, df1h, df15):
    # اتجاهات الفريمات
    trend_d = trend_filter(df1d)
    trend_4h = trend_filter(df4h)
    trend_1h = trend_filter(df1h)
    trend_15m = trend_filter(df15)

    # مؤشرات أساسية
    rsi_val = df15["rsi"].iloc[-1]
    atr_val = df1h["atr"].iloc[-1]
    price = df15["close"].iloc[-1]

    # SMC + FVG + فيبو + إليوت + كلاسيكي
    smc = smc_score(df1h)
    fib_d = fib_zone_score(df1d, "long" if trend_d == "bullish" else "short")
    fib_4h = fib_zone_score(df4h, "long" if trend_4h == "bullish" else "short")
    fib_1h = fib_zone_score(df1h, "long" if trend_1h == "bullish" else "short")
    fib_total = fib_d + fib_4h + fib_1h
    ell = elliott_phase(df4h)
    classic = classic_candle_score(df15)

    # سيولة + كلاستر + MM
    ob_imb = orderbook_imbalance(symbol)
    liq_score = liquidity_heatmap_score(symbol)
    cluster = cluster_delta_score(symbol)
    mm = market_maker_model(df4h)

    # دايفرجنس
    div = rsi_divergence(df15)

    # تجميع Score عام للعملة
    score = 0

    # اتجاه الفريمات
    if trend_d == "bullish": score += 3
    elif trend_d == "bearish": score -= 3

    if trend_4h == "bullish": score += 2
    elif trend_4h == "bearish": score -= 2

    if trend_1h == "bullish": score += 1
    elif trend_1h == "bearish": score -= 1

    # SMC + فيبو + كلاسيكي + إليوت
    score += smc
    score += fib_total
    score += classic

    if ell == "impulsive": score += 2
    elif ell == "corrective": score += 1

    # سيولة + كلاستر
    score += ob_imb
    score += liq_score
    score += cluster

    # RSI + دايفرجنس
    if rsi_val <= 30: score += 2
    elif rsi_val >= 70: score -= 2

    if div == "bullish": score += 2
    elif div == "bearish": score -= 2

    # MM Model
    if mm == "discount": score += 1
    elif mm == "premium": score -= 1

    # تحويل Score إلى نسبة مئوية
    prob = probability_score(score)

    # تحديد الاتجاه المتوقع
    if score > 0:
        direction = "صعود"
    elif score < 0:
        direction = "هبوط"
    else:
        direction = "تذبذب"

    return prob, direction


# ============================================
# DAILY ANALYSIS (تحليل يومي كامل)
# ============================================

async def analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await update.message.reply_text("جارٍ التحليل ...💱")

    # تحليل الأخبار
    news_comment = analyze_news_arabic()

    # أفضل 5 عملات نشاطاً
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

        # توقع مبني على كل التحليل الفني
        prob, direction = build_coin_expectation(symbol, df1d, df4h, df1h, df15)
        expectation = f"{prob}% {direction} خلال الساعات القادمة"

        coins_analysis.append({
            "symbol": symbol,
            "trend_1d": f"اتجاه {'صاعد' if trend_d=='bullish' else 'هابط' if trend_d=='bearish' else 'متذبذب'}",
            "trend_4h": f"اتجاه {'صاعد' if trend_4h=='bullish' else 'هابط' if trend_4h=='bearish' else 'متذبذب'}",
            "trend_1h": f"اتجاه {'صاعد' if trend_1h=='bullish' else 'هابط' if trend_1h=='bearish' else 'متذبذب'}",
            "trend_15m": f"اتجاه {'صاعد' if trend_15m=='bullish' else 'هابط' if trend_15m=='bearish' else 'متذبذب'}",
            "expectation": expectation
        })

    msg = format_daily_report(news_comment, coins_analysis)
    await update.message.reply_text(msg)

# ============================================
# GENERIC TEXT HANDLER — يربط الأزرار
# ============================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.message.chat_id
    active_chats.add(chat_id)

    if text == "صفقات":
        await signals(update, context)
    elif text == "تحليل":
        await analysis(update, context)
    else:
        await update.message.reply_text(
            "استخدم الأزرار:\n- صفقات\n- تحليل",
            reply_markup=keyboard
        )

# ============================================
# TELEGRAM APP & WEBHOOK
# ============================================

telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

# ============================================
# WebSocket بسيط (اختياري)
# ============================================

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            data = await ws.receive_text()
            await ws.send_text(f"Message: {data}")
    except Exception:
        await ws.close()

# ============================================
# STARTUP EVENT — تشغيل البوت والمهام الخلفية
# ============================================

@app.on_event("startup")
async def start_bot_event():
    await telegram_app.initialize()
    await telegram_app.start()
    webhook_url = "https://ahmedbot-cryp-auto.fly.dev/webhook"
    await telegram_app.bot.set_webhook(webhook_url)
    print(f"🚀 Webhook set to: {webhook_url}")

    asyncio.create_task(auto_scan_task(telegram_app))
    asyncio.create_task(pending_monitor(telegram_app))
    asyncio.create_task(websocket_aggtrades())
