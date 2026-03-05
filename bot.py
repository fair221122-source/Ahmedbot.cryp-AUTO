import os
import time
import logging
import requests
import pandas as pd
import numpy as np
import telebot
from telebot import types

# ================== إعداد اللوج ==================
logging.basicConfig(level=logging.CRITICAL)

# ================== توكن البوت ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # ← ضع التوكن في Render Environment
bot = telebot.TeleBot(BOT_TOKEN)

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

def analyze_symbol_1h(symbol):
    df_1h = fetch_klines(symbol, "1h", 200)
    if df_1h is None or len(df_1h) < 60:
        return None
    trend, momentum, desc = detect_trend_1h(df_1h, lookback=50)
    last_close = df_1h["c"].iloc[-1]
    return {
        "symbol": symbol,
        "trend": trend,
        "momentum": momentum,
        "description": desc,
        "last_price": last_close
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
    candidates = []
    for sym in symbols:
        df_1h = fetch_klines(sym, "1h", 200)
        df_15m = fetch_klines(sym, "15m", 200)
        if df_1h is None or df_15m is None:
            continue
        if len(df_1h) < 60 or len(df_15m) < 50:
            continue

        trend, momentum, desc = detect_trend_1h(df_1h, lookback=50)
        if trend == "side":
            continue

        entry = detect_entry_15m(df_15m, trend)
        if not entry:
            continue

        if trend == "bull":
            sl = entry["entry_price"] * 0.99
            tp = entry["entry_price"] * 1.03
        else:
            sl = entry["entry_price"] * 1.01
            tp = entry["entry_price"] * 0.97

        risk = abs(entry["entry_price"] - sl)
        reward = abs(tp - entry["entry_price"])
        rr = reward / risk if risk > 0 else 0

        if rr < 3:
            continue

        score = 0
        if momentum == "strong":
            score += 2
        else:
            score += 1

        candidates.append({
            "symbol": sym,
            "trend": trend,
            "momentum": momentum,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "score": score
        })
        time.sleep(0.05)

    candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
    return candidates[:2]

# ================== بناء رسالة التحليل ==================
def build_analysis_message():
    market = analyze_market_overview()
    assets = market["assets"]
    bias = market["market_bias_24h"]
    comment = market["comment"]

    msg = "📊 تحليل السوق العام – فريم الساعة (آخر 50 شمعة)\n\n"

    btc = assets.get("Bitcoin")
    eth = assets.get("Ethereum")

    if btc:
        msg += "🔹 Bitcoin (BTC)\n"
        msg += f"• الاتجاه: { 'صاعد' if btc['trend']=='bull' else 'هابط' if btc['trend']=='bear' else 'جانبي' }\n"
        msg += f"• الزخم: { 'قوي' if btc['momentum']=='strong' else 'ضعيف' }\n"
        msg += f"• الوصف: {btc['description']}\n"
        msg += f"• آخر سعر: {round(btc['last_price'], 2)}\n\n"

    if eth:
        msg += "🔹 Ethereum (ETH)\n"
        msg += f"• الاتجاه: { 'صاعد' if eth['trend']=='bull' else 'هابط' if eth['trend']=='bear' else 'جانبي' }\n"
        msg += f"• الزخم: { 'قوي' if eth['momentum']=='strong' else 'ضعيف' }\n"
        msg += f"• الوصف: {eth['description']}\n"
        msg += f"• آخر سعر: {round(eth['last_price'], 2)}\n\n"

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
        msg += f"{i}) {c['symbol']}\n"
        msg += f"   • الاتجاه: { 'صاعد' if c['trend']=='bull' else 'هابط' if c['trend']=='bear' else 'جانبي' }\n"
        msg += f"   • الزخم: { 'قوي' if c['momentum']=='strong' else 'ضعيف' }\n"
        msg += f"   • الوصف: {c['description']}\n"

    return msg

# ================== بناء رسالة الصفقات ==================
def build_trades_message():
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
        rr = round(t["rr"], 1)
        reason = t["entry"]["reason"]

        title = "🏆 Trade #1" if i == 1 else "🥈 Trade #2"
        msg += f"{title} — {symbol}\n"
        msg += f"• Direction: {direction}\n"
        msg += f"• Entry Type: {entry_type}\n"
        msg += f"• Entry: {round(entry_price, 2)}\n"
        msg += f"• Stop Loss: {round(sl, 2)}\n"
        msg += f"• Take Profit: {round(tp, 2)}\n"
        msg += f"• Risk/Reward: 1:{rr}\n"
        msg += f"• السبب: {reason}\n\n"
        msg += "━━━━━━━━━━━━━━━━━━\n\n"

    msg += "⚠️ الملاحظة: إدارة رأس المال مسؤوليتك، والسوق قد يعيد اختبار مناطق السيولة قبل الانطلاق."
    return msg

# ================== هاندلر الأوامر ==================
@bot.message_handler(commands=['start'])
def start(m):
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn1 = types.KeyboardButton("تحليل")
    btn2 = types.KeyboardButton("صفقات")
    markup.add(btn1, btn2)
    bot.reply_to(m, "🚀 نظام التحليل مفعل.\nاختر: تحليل أو صفقات.", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text in ["تحليل", "صفقات"])
def main_handler(m):
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
bot.infinity_polling(skip_pending=True)
