from flask import Flask
import threading
import os
import time
import logging
import threading
import requests
import pandas as pd
import numpy as np
import telebot
from telebot import types
LAST_SENT = {}
def can_send(symbol):
    now = time.time()
    if symbol in LAST_SENT:
        # 1800 ثانية = نصف ساعة
        if now - LAST_SENT[symbol] < 1800:
            return False
    LAST_SENT[symbol] = now
    return True
    
# ================== إعداد اللوج ==================
logging.basicConfig(level=logging.CRITICAL)

# ================== توكن البوت ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # ← ضع التوكن في Render Environment
bot = telebot.TeleBot(BOT_TOKEN)

# آخر شات تم التفاعل معه (لإرسال الفرص التلقائية)
LAST_CHAT_ID = None

# ================== قائمة 20 عملة عالية السيولة (Futures) ==================
def top_20_liquid_coins():
    return [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
        "MATICUSDT", "NEARUSDT", "TRXUSDT", "LTCUSDT", "UNIUSDT",
        "ARBUSDT", "OPUSDT", "SUIUSDT", "FILUSDT", "STXUSDT"
    ]

# ================== جلب البيانات من Binance Futures ==================
def fetch_klines(symbol, interval="1h", limit=200):
    urls = [
        "https://fapi.binance.com/fapi/v1/klines",
        "https://fapi1.binance.com/fapi/v1/klines",
        "https://fapi2.binance.com/fapi/v1/klines"
    ]

    params = {"symbol": symbol, "interval": interval, "limit": limit}

    for url in urls:
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, timeout=10)
                data = r.json()

                if isinstance(data, dict) and data.get("code"):
                    time.sleep(0.3)
                    continue

                df = pd.DataFrame(data, columns=[
                    "t","o","h","l","c","v","ct","qv","n","tbb","tbq","i"
                ])
                df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)
                return df[["o","h","l","c","v"]]

            except Exception:
                time.sleep(0.3)
                continue

    print(f"⚠️ فشل في جلب البيانات: {symbol}")
    return None

# ================== أدوات تحليل الشموع ==================
def calc_candle_features(df):
    o = df["o"]
    h = df["h"]
    l = df["l"]
    c = df["c"]
    body = (c - o).abs()
    upper_wick = h - np.maximum(o, c)
    lower_wick = np.minimum(o, c) - l
    return body, upper_wick, lower_wick

def detect_trend_1h(df_1h, lookback=50):
    df = df_1h.iloc[-lookback:].copy()
    highs = df["h"]
    lows = df["l"]

    if highs.iloc[-1] > highs.mean() and lows.iloc[-1] > lows.mean():
        trend = "bull"
    elif highs.iloc[-1] < highs.mean() and lows.iloc[-1] < lows.mean():
        trend = "bear"
    else:
        trend = "side"

    last5 = df.iloc[-5:]
    body_last5, _, _ = calc_candle_features(last5)
    avg_body_last5 = body_last5.mean()
    avg_body_all = (df["c"] - df["o"]).abs().mean()

    momentum = "strong" if avg_body_last5 > avg_body_all else "weak"

    desc_parts = []
    if trend == "bull":
        desc_parts.append("اتجاه صاعد")
    elif trend == "bear":
        desc_parts.append("اتجاه هابط")
    else:
        desc_parts.append("اتجاه جانبي")

    if momentum == "strong":
        desc_parts.append("زخم قوي")
    else:
        desc_parts.append("زخم ضعيف")

    description = " + ".join(desc_parts)
    return trend, momentum, description

def calc_percent_metrics(df):
    # تغيّر آخر 24 شمعة (تقريباً 24 ساعة على فريم الساعة)
    close_last = df["c"].iloc[-1]
    close_24 = df["c"].iloc[-24] if len(df) >= 24 else df["c"].iloc[0]
    change_24 = (close_last - close_24) / close_24 * 100 if close_24 != 0 else 0

    # تغيّر آخر شمعة (تقريباً آخر ساعة)
    close_prev = df["c"].iloc[-2] if len(df) >= 2 else df["c"].iloc[0]
    change_1h = (close_last - close_prev) / close_prev * 100 if close_prev != 0 else 0

    # موقع السعر داخل الرينج
    low_range = df["l"].min()
    high_range = df["h"].max()
    if high_range != low_range:
        pos_in_range = (close_last - low_range) / (high_range - low_range) * 100
    else:
        pos_in_range = 50.0

    return change_1h, change_24, pos_in_range

def analyze_symbol_1h(symbol):
    df_1h = fetch_klines(symbol, "1h", 200)
    if df_1h is None or len(df_1h) < 60:
        return None
    trend, momentum, desc = detect_trend_1h(df_1h, lookback=50)
    last_close = df_1h["c"].iloc[-1]
    ch_1h, ch_24h, pos = calc_percent_metrics(df_1h)
    return {
        "symbol": symbol,
        "trend": trend,
        "momentum": momentum,
        "description": desc,
        "last_price": last_close,
        "change_1h": ch_1h,
        "change_24h": ch_24h,
        "pos_range": pos
    }

# ================== تحليل السوق العام (BTC / ETH فقط) ==================
def analyze_market_overview():
    symbols = {
        "BTCUSDT": "Bitcoin",
        "ETHUSDT": "Ethereum"
    }
    results = {}
    for sym, name in symbols.items():
        info = analyze_symbol_1h(sym)
        if info:
            results[name] = info

    bias = "neutral"
    comment = "صورة السوق متوازنة حالياً."

    btc = results.get("Bitcoin")
    eth = results.get("Ethereum")

    if btc and eth:
        bull_count = sum(1 for x in [btc, eth] if x["trend"] == "bull")
        bear_count = sum(1 for x in [btc, eth] if x["trend"] == "bear")
        if bull_count == 2:
            bias = "bullish"
            comment = "الكريبتو يميل للصعود خلال الـ 24 ساعة القادمة."
        elif bear_count == 2:
            bias = "bearish"
            comment = "الكريبتو يميل للهبوط خلال الـ 24 ساعة القادمة."
        else:
            bias = "neutral"
            comment = "لا يوجد اتجاه واضح قوي للكريبتو حالياً."

    return {
        "assets": results,
        "market_bias_24h": bias,
        "comment": comment
    }

# ================== أفضل 5 عملات نشطة ==================
def analyze_top_coins(symbols):
    analyzed = []
    for sym in symbols:
        info = analyze_symbol_1h(sym)
        if not info:
            continue
        score = 0
        if info["trend"] in ["bull", "bear"]:
            score += 2
        if info["momentum"] == "strong":
            score += 2
        else:
            score += 1
        # نضيف قوة إضافية حسب نسبة التغير 24 ساعة
        score += abs(info["change_24h"]) / 5.0
        info["score"] = score
        analyzed.append(info)
        time.sleep(0.05)
    analyzed = sorted(analyzed, key=lambda x: x["score"], reverse=True)
    return analyzed[:5]

# ================== منطق دخول على 15m ==================
def detect_entry_15m(df_15m, trend):
    last = df_15m.iloc[-5:]
    body, upper, lower = calc_candle_features(last)

    if trend == "bull":
        for i in range(len(last)-1, -1, -1):
            if lower.iloc[i] > body.iloc[i] * 1.5 and last["c"].iloc[i] > last["o"].iloc[i]:
                return {
                    "type": "long",
                    "entry_type": "Market",
                    "entry_price": last["c"].iloc[i],
                    "reason": "دخول من منطقة طلب مع شمعة رفض هبوط على فريم 15 دقيقة"
                }

    if trend == "bear":
        for i in range(len(last)-1, -1, -1):
            if upper.iloc[i] > body.iloc[i] * 1.5 and last["c"].iloc[i] < last["o"].iloc[i]:
                return {
                    "type": "short",
                    "entry_type": "Market",
                    "entry_price": last["c"].iloc[i],
                    "reason": "دخول من منطقة عرض مع شمعة رفض صعود على فريم 15 دقيقة"
                }

    return None

# ================== البحث عن أفضل صفقتين ==================
def find_best_trades(symbols):
def find_best_trades(symbols):
    candidates = []

    for sym in symbols:
        # بيانات 4 ساعات + ساعة + 15 دقيقة
        df_4h = fetch_klines(sym, "4h", 200)
        df_1h = fetch_klines(sym, "1h", 200)
        df_15m = fetch_klines(sym, "15m", 200)

        if df_4h is None or df_1h is None or df_15m is None:
            continue

        # تأكد من وجود بيانات كافية
        if len(df_4h) < 100 or len(df_1h) < 100 or len(df_15m) < 50:
            continue

        # ============================
        # 1) فلتر الاتجاه العام (4h)
        # ============================
        df_4h["ema50"] = df_4h["close"].ewm(span=50).mean()
        df_4h["ema200"] = df_4h["close"].ewm(span=200).mean()

        if df_4h["ema50"].iloc[-1] > df_4h["ema200"].iloc[-1]:
            main_trend = "bull"
        elif df_4h["ema50"].iloc[-1] < df_4h["ema200"].iloc[-1]:
            main_trend = "bear"
        else:
            continue

        # ============================
        # 2) فلتر الاتجاه المتوسط (1h)
        # ============================
        df_1h["ema50"] = df_1h["close"].ewm(span=50).mean()
        df_1h["ema200"] = df_1h["close"].ewm(span=200).mean()

        if main_trend == "bull" and not (df_1h["ema50"].iloc[-1] > df_1h["ema200"].iloc[-1]):
            continue

        if main_trend == "bear" and not (df_1h["ema50"].iloc[-1] < df_1h["ema200"].iloc[-1]):
            continue

        # ============================
        # 3) فلتر الفوليوم (15m)
        # ============================
        df_15m["vol_ma20"] = df_15m["volume"].rolling(20).mean()
        if df_15m["volume"].iloc[-1] < df_15m["vol_ma20"].iloc[-1]:
            continue

        # ============================
        # 4) شمعة تأكيد (Breakout)
        # ============================
        last_close = df_15m["close"].iloc[-1]
        last_open = df_15m["open"].iloc[-1]

        if main_trend == "bull":
            if not (last_close > last_open and last_close > df_15m["close"].rolling(20).max().iloc[-2]):
                continue

        if main_trend == "bear":
            if not (last_close < last_open and last_close < df_15m["close"].rolling(20).min().iloc[-2]):
                continue

        # ============================
        # 5) SL و TP واقعيين
        # ============================
        if main_trend == "bull":
            sl = df_15m["low"].rolling(10).min().iloc[-1]
            tp = last_close + (last_close - sl) * 2
        else:
            sl = df_15m["high"].rolling(10).max().iloc[-1]
            tp = last_close - (sl - last_close) * 2

        entry_price = last_close
        risk = abs(entry_price - sl)
        reward = abs(tp - entry_price)
        rr = reward / risk if risk > 0 else 0

        if rr < 1.5:
            continue

        # ============================
        # 6) إضافة الصفقة للقائمة
        # ============================
        candidates.append({
            "symbol": sym,
            "trend": main_trend,
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "volume": df_15m["volume"].iloc[-1]
        })

        time.sleep(0.05)

    # ترتيب حسب أعلى R:R
    candidates = sorted(candidates, key=lambda x: x["rr"], reverse=True)
    return candidates[:2]
    
# ================== بناء رسالة التحليل ==================
def fmt_pct(v):
    return f"{v:+.2f}%"

def fmt_price(v):
    return f"{v:.4f}"

def build_analysis_message():
    market = analyze_market_overview()
    assets = market["assets"]
    bias = market["market_bias_24h"]
    comment = market["comment"]

    msg = "📊 تحليل السوق العام – فريم الساعة (آخر 50 شمعة)\n\n"

    # ترتيب BTC و ETH حسب قوة التغير 24 ساعة
    ordered = []
    for name in ["Bitcoin", "Ethereum"]:
        if name in assets:
            ordered.append(assets[name])
    ordered = sorted(ordered, key=lambda x: abs(x["change_24h"]), reverse=True)

    for asset in ordered:
        name = asset["symbol"].replace("USDT", "")
        msg += f"🔹 {name} ({asset['symbol']})\n"
        msg += f"• الاتجاه: {'صاعد' if asset['trend']=='bull' else 'هابط' if asset['trend']=='bear' else 'جانبي'}\n"
        msg += f"• الزخم: {'قوي' if asset['momentum']=='strong' else 'ضعيف'}\n"
        msg += f"• التغير 1h: {fmt_pct(asset['change_1h'])}\n"
        msg += f"• التغير 24h: {fmt_pct(asset['change_24h'])}\n"
        msg += f"• موقع السعر داخل الرينج: {asset['pos_range']:.1f}%\n"
        msg += f"• الوصف: {asset['description']}\n"
        msg += f"• آخر سعر: {fmt_price(asset['last_price'])}\n\n"

    msg += "━━━━━━━━━━━━━━━━━━\n\n"
    msg += "📌 خلاصة السوق خلال الـ 24 ساعة القادمة\n"
    msg += f"• {comment}\n\n"
    msg += "📌 التوقع العام:\n"
    if bias == "bullish":
        msg += "السوق يميل للصعود مع احتمالية تصحيح بسيط قبل استمرار الاتجاه."
    elif bias == "bearish":
        msg += "السوق يميل للهبوط مع احتمالية ارتدادات قصيرة داخل الاتجاه."
    else:
        msg += "الصورة غير واضحة تماماً، ويفضل انتظار حركة أوضح."

    msg += "\n\n🔹 أفضل 5 عملات نشطة من قائمة السيولة:\n"
    top5 = analyze_top_coins(top_20_liquid_coins())
    for i, c in enumerate(top5, 1):
        name = c["symbol"].replace("USDT", "")
        msg += f"{i}) {name} ({c['symbol']})\n"
        msg += f"   • الاتجاه: {'صاعد' if c['trend']=='bull' else 'هابط' if c['trend']=='bear' else 'جانبي'}\n"
        msg += f"   • الزخم: {'قوي' if c['momentum']=='strong' else 'ضعيف'}\n"
        msg += f"   • التغير 1h: {fmt_pct(c['change_1h'])}\n"
        msg += f"   • التغير 24h: {fmt_pct(c['change_24h'])}\n"
        msg += f"   • موقع السعر داخل الرينج: {c['pos_range']:.1f}%\n"
        msg += f"   • الوصف: {c['description']}\n"

    return msg

# ================== بناء رسالة الصفقات ==================
def build_trades_message(trades=None):
    if trades is None:
        coins = top_20_liquid_coins()
        trades = find_best_trades(coins)

    if not trades:
        return "لا توجد صفقات واضحة حالياً وفق شروط الزخم ونسبة المخاطرة."

    msg = "🎯 Best 2 Trade Setups\n\n"
    msg += "━━━━━━━━━━━━━━━━━━\n\n"

    for i, t in enumerate(trades, 1):
        symbol = t["symbol"]
        direction = "Long 🟢" if t["trend"] == "bull" else "Short 🔴"
        entry_type = t["entry"]["entry_type"]
        entry_price = t["entry"]["entry_price"]
        sl = t["sl"]
        tp = t["tp"]
        rr = t["rr"]
        reason = t["entry"]["reason"]

        sl_pct = (sl - entry_price) / entry_price * 100 if t["trend"] == "bull" else (entry_price - sl) / entry_price * 100
        tp_pct = (tp - entry_price) / entry_price * 100 if t["trend"] == "bull" else (entry_price - tp) / entry_price * 100

        title = "🏆 Trade #1" if i == 1 else "🥈 Trade #2"
        msg += f"{title} — {symbol}\n"
        msg += f"• Direction: {direction}\n"
        msg += f"• Entry ({entry_type}): {fmt_price(entry_price)}\n"
        msg += f"• S.L: {fmt_price(sl)} ({fmt_pct(-abs(sl_pct))})\n"
        msg += f"• T.P: {fmt_price(tp)} ({fmt_pct(abs(tp_pct))})\n"
        msg += f"• R:R: 1:{rr:.2f}\n"
        msg += f"• التغير 1h: {fmt_pct(t['change_1h'])} | التغير 24h: {fmt_pct(t['change_24h'])}\n"
        msg += f"• موقع السعر داخل الرينج: {t['pos_range']:.1f}%\n"
        msg += f"• السبب: {reason}\n\n"
        msg += "━━━━━━━━━━━━━━━━━━\n\n"

    msg += "⚠️ الملاحظة: إدارة رأس المال مسؤوليتك، والسوق قد يعيد اختبار مناطق السيولة قبل الانطلاق."
    return msg

# ================== فحص تلقائي للفرص كل 5 دقائق ==================
def auto_scan_loop():
    global LAST_CHAT_ID, LAST_SENT_SYMBOL
    while True:
        try:
            if LAST_CHAT_ID is not None:
                coins = top_20_liquid_coins()
                trades = find_best_trades(coins)

                if trades:
                    filtered_trades = []
                    now = time.time()

                    # فلترة الصفقات لمنع تكرار نفس العملة خلال 30 دقيقة
                    for t in trades:
                        sym = t["symbol"]
                        if sym in LAST_SENT_SYMBOL:
                            if now - LAST_SENT_SYMBOL[sym] < 1800:
                                continue
                        filtered_trades.append(t)
                        LAST_SENT_SYMBOL[sym] = now

                    # إذا لم يبقَ أي صفقة بعد الفلترة → لا ترسل شيء
                    if not filtered_trades:
                        time.sleep(600)
                        continue

                    # بناء الرسالة
                    msg = "⏰ فحص تلقائي — فرص مؤكدة:\n\n"
                    msg += build_trades_message(filtered_trades)

                    # منع تكرار نفس الرسالة
                    if can_send(msg):
                        bot.send_message(LAST_CHAT_ID, msg)

        except Exception as e:
            print("Auto scan error:", e)

        time.sleep(600)  # كل 10 دقائق
        
# ================== هاندلر الأوامر ==================
@bot.message_handler(commands=['start'])
def start(m):
    global LAST_CHAT_ID
    LAST_CHAT_ID = m.chat.id
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn1 = types.KeyboardButton("تحليل")
    btn2 = types.KeyboardButton("صفقات")
    markup.add(btn1, btn2)
    bot.reply_to(m, "🚀 نظام التحليل مفعل.\nاختر: تحليل أو صفقات.", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text in ["تحليل", "صفقات"])
def main_handler(m):
    global LAST_CHAT_ID
    LAST_CHAT_ID = m.chat.id
    if m.text == "تحليل":
        wait = bot.reply_to(m, "جاري تحليل السوق العام...")
        try:
            msg = build_analysis_message()
            bot.edit_message_text(msg, m.chat.id, wait.message_id)
        except Exception:
            bot.edit_message_text("حدث خطأ أثناء التحليل.", m.chat.id, wait.message_id)
    else:
        wait = bot.reply_to(m, "جاري البحث عن أفضل الصفقات...")
        try:
            msg = build_trades_message()
            bot.edit_message_text(msg, m.chat.id, wait.message_id)
        except Exception:
            bot.edit_message_text("حدث خطأ أثناء توليد الصفقات.", m.chat.id, wait.message_id)

# ================== تشغيل البوت ==================

print("Bot is running...")

# تشغيل حلقة الفحص التلقائي في ثريد منفصل
threading.Thread(target=auto_scan_loop, daemon=True).start()

# ================== Flask Server لفتح Port ==================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running", 200

def run_flask():
    app.run(host="0.0.0.0", port=10000)

# تشغيل Flask في Thread
threading.Thread(target=run_flask).start()

# تشغيل البوت
bot.infinity_polling(skip_pending=True)
