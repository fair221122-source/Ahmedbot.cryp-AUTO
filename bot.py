from flask import Flask
import threading
import os
import time
import logging
import requests
import pandas as pd
import numpy as np
import telebot
from telebot import types

# ================== منع تكرار الإرسال ==================
LAST_SENT = {}
LAST_SENT_SYMBOL = {}

def can_send(symbol):
    now = time.time()
    if symbol in LAST_SENT:
        if now - LAST_SENT[symbol] < 1800:
            return False
    LAST_SENT[symbol] = now
    return True

# ================== Flask ==================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

# ================== إعداد اللوج ==================
logging.basicConfig(level=logging.CRITICAL)

# ================== البوت ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN)

LAST_CHAT_ID = None

# ================== قائمة العملات ==================
def top_20_liquid_coins():
    return [
        "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
        "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
        "MATICUSDT","NEARUSDT","TRXUSDT","LTCUSDT","UNIUSDT",
        "ARBUSDT","OPUSDT","SUIUSDT","FILUSDT","STXUSDT"
    ]

# ================== جلب البيانات ==================
def fetch_klines(symbol, interval="1h", limit=200):
    urls = [
        "https://fapi.binance.com/fapi/v1/klines",
        "https://fapi1.binance.com/fapi/v1/klines",
        "https://fapi2.binance.com/fapi/v1/klines"
    ]
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    for url in urls:
        for _ in range(3):
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
            except:
                time.sleep(0.3)
                continue
    return None

# ================== أدوات التحليل ==================
def calc_candle_features(df):
    o = df["o"]; h = df["h"]; l = df["l"]; c = df["c"]
    body = (c - o).abs()
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    return body, upper, lower

def detect_trend(df, lookback=50):
    df = df.iloc[-lookback:]
    highs = df["h"]; lows = df["l"]

    if highs.iloc[-1] > highs.mean() and lows.iloc[-1] > lows.mean():
        trend = "bull"
    elif highs.iloc[-1] < highs.mean() and lows.iloc[-1] < lows.mean():
        trend = "bear"
    else:
        trend = "side"

    last5 = df.iloc[-5:]
    body, _, _ = calc_candle_features(last5)
    momentum = "strong" if body.mean() > (df["c"] - df["o"]).abs().mean() else "weak"

    desc = ("اتجاه صاعد" if trend=="bull" else "اتجاه هابط" if trend=="bear" else "اتجاه جانبي")
    desc += " + زخم قوي" if momentum=="strong" else " + زخم ضعيف"

    return trend, momentum, desc

def calc_percent_metrics(df):
    close_last = df["c"].iloc[-1]
    close_24 = df["c"].iloc[-24] if len(df)>=24 else df["c"].iloc[0]
    change_24 = (close_last - close_24) / close_24 * 100 if close_24 else 0

    close_prev = df["c"].iloc[-2] if len(df)>=2 else df["c"].iloc[0]
    change_1h = (close_last - close_prev) / close_prev * 100 if close_prev else 0

    low = df["l"].min(); high = df["h"].max()
    pos = (close_last - low) / (high - low) * 100 if high!=low else 50

    return change_1h, change_24, pos

# ================== تحليل 1h ==================
def analyze_symbol_1h(symbol):
    df = fetch_klines(symbol, "1h", 200)
    if df is None or len(df)<60:
        return None
    trend, momentum, desc = detect_trend(df)
    ch1, ch24, pos = calc_percent_metrics(df)
    return {
        "symbol": symbol,
        "trend": trend,
        "momentum": momentum,
        "description": desc,
        "last_price": df["c"].iloc[-1],
        "change_1h": ch1,
        "change_24h": ch24,
        "pos_range": pos
    }

# ================== تحليل 4h ==================
def analyze_symbol_4h(symbol):
    df = fetch_klines(symbol, "4h", 200)
    if df is None or len(df)<60:
        return None
    trend, momentum, desc = detect_trend(df)
    return {
        "trend_4h": trend,
        "momentum_4h": momentum,
        "desc_4h": desc
    }

# ================== دخول 15m ==================
def detect_entry_15m(df, trend):
    last = df.iloc[-5:]
    body, upper, lower = calc_candle_features(last)

    for i in range(4, -1, -1):
        if trend=="bull" and lower.iloc[i] > body.iloc[i]*1.5 and last["c"].iloc[i] > last["o"].iloc[i]:
            return {"type":"long","entry_type":"Market","entry_price":last["c"].iloc[i],"reason":"شمعة رفض هبوط"}
        if trend=="bear" and upper.iloc[i] > body.iloc[i]*1.5 and last["c"].iloc[i] < last["o"].iloc[i]:
            return {"type":"short","entry_type":"Market","entry_price":last["c"].iloc[i],"reason":"شمعة رفض صعود"}
    return None

# ================== تنسيق ==================
def fmt_price(x): return f"{x:.4f}"
def fmt_pct(x): return f"{x:.2f}%"

# ================== أفضل الصفقات ==================
def find_best_trades(symbols):
    results = []

    for sym in symbols:
        info1 = analyze_symbol_1h(sym)
        if not info1: continue

        info4 = analyze_symbol_4h(sym)
        if not info4: continue

        # شرط الاتجاه: لا ندخل عكس 4h
        if info1["trend"] != info4["trend_4h"]:
            continue

        df15 = fetch_klines(sym, "15m", 200)
        if df15 is None: continue

        entry = detect_entry_15m(df15, info1["trend"])
        if not entry: continue

        ep = entry["entry_price"]
        if info1["trend"]=="bull":
            sl = ep * 0.985
            tp = ep * 1.02
        else:
            sl = ep * 1.015
            tp = ep * 0.98

        rr = abs((tp-ep)/(ep-sl))

        results.append({
            "symbol": sym,
            "trend": info1["trend"],
            "trend_4h": info4["trend_4h"],
            "momentum": info1["momentum"],
            "momentum_4h": info4["momentum_4h"],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "change_1h": info1["change_1h"],
            "change_24h": info1["change_24h"],
            "pos_range": info1["pos_range"]
        })

    results = sorted(results, key=lambda x: x["rr"], reverse=True)
    return results[:2]

# ================== تحليل السوق ==================
def analyze_top_coins(symbols):
    out = []
    for sym in symbols:
        info = analyze_symbol_1h(sym)
        if info:
            info["score"] = abs(info["change_24h"]) + (2 if info["momentum"]=="strong" else 1)
            out.append(info)
    return sorted(out, key=lambda x: x["score"], reverse=True)[:5]

def build_analysis_message():
    msg = "📊 **تحليل السوق (1h + 4h)**\n\n"
    top5 = analyze_top_coins(top_20_liquid_coins())

    for i, c in enumerate(top5, 1):
        info4 = analyze_symbol_4h(c["symbol"])
        name = c["symbol"].replace("USDT","")

        msg += f"{i}) {name} ({c['symbol']})\n"
        msg += f"   • الاتجاه 1h: {c['trend']}\n"
        msg += f"   • الاتجاه 4h: {info4['trend_4h']}\n"
        msg += f"   • الزخم 1h: {c['momentum']}\n"
        msg += f"   • الزخم 4h: {info4['momentum_4h']}\n"
        msg += f"   • التغير 1h: {fmt_pct(c['change_1h'])}\n"
        msg += f"   • التغير 24h: {fmt_pct(c['change_24h'])}\n"
        msg += f"   • موقع السعر داخل الرينج: {c['pos_range']:.1f}%\n"
        msg += f"   • وصف 1h: {c['description']}\n"
        msg += f"   • وصف 4h: {info4['desc_4h']}\n\n"

    return msg

# ================== رسالة الصفقات ==================
def build_trades_message(trades=None):
    if trades is None:
        trades = find_best_trades(top_20_liquid_coins())

    if not trades:
        return "لا توجد صفقات واضحة حالياً."

    msg = "🎯 **أفضل صفقتين (1h + 4h + 15m)**\n\n━━━━━━━━━━━━━━━━━━\n\n"

    for i, t in enumerate(trades, 1):
        ep = t["entry"]["entry_price"]
        sl = t["sl"]; tp = t["tp"]

        sl_pct = (sl-ep)/ep*100 if t["trend"]=="bull" else (ep-sl)/ep*100
        tp_pct = (tp-ep)/ep*100 if t["trend"]=="bull" else (ep-tp)/ep*100

        msg += f"{'🏆' if i==1 else '🥈'} Trade #{i} — {t['symbol']}\n"
        msg += f"• الاتجاه: {t['trend']} (1h) | {t['trend_4h']} (4h)\n"
        msg += f"• Entry: {fmt_price(ep)}\n"
        msg += f"• SL: {fmt_price(sl)} ({fmt_pct(-abs(sl_pct))})\n"
        msg += f"• TP: {fmt_price(tp)} ({fmt_pct(abs(tp_pct))})\n"
        msg += f"• R:R = 1:{t['rr']:.2f}\n"
        msg += f"• السبب: {t['entry']['reason']}\n\n"
        msg += "━━━━━━━━━━━━━━━━━━\n\n"

    return msg

# ================== الفحص التلقائي ==================
def auto_scan_loop():
    global LAST_CHAT_ID, LAST_SENT_SYMBOL
    while True:
        try:
            if LAST_CHAT_ID:
                trades = find_best_trades(top_20_liquid_coins())
                if trades:
                    now = time.time()
                    filtered = []
                    for t in trades:
                        sym = t["symbol"]
                        if sym in LAST_SENT_SYMBOL and now - LAST_SENT_SYMBOL[sym] < 1800:
                            continue
                        LAST_SENT_SYMBOL[sym] = now
                        filtered.append(t)

                    if filtered:
                        msg = "⏰ فحص تلقائي:\n\n" + build_trades_message(filtered)
                        if can_send(msg):
                            bot.send_message(LAST_CHAT_ID, msg)

        except Exception as e:
            print("Auto scan error:", e)

        time.sleep(600)

# ================== الهاندلرز ==================
@bot.message_handler(commands=['start'])
def start(m):
    global LAST_CHAT_ID
    LAST_CHAT_ID = m.chat.id
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("تحليل","صفقات")
    bot.reply_to(m, "🚀 أهلاً! اختر تحليل أو صفقات.", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text in ["تحليل","صفقات"])
def main_handler(m):
    global LAST_CHAT_ID
    LAST_CHAT_ID = m.chat.id

    if m.text=="تحليل":
        wait = bot.reply_to(m, "جاري تحليل السوق...")
        try:
            msg = build_analysis_message()
            bot.edit_message_text(msg, m.chat.id, wait.message_id)
        except:
            bot.edit_message_text("حدث خطأ أثناء التحليل.", m.chat.id, wait.message_id)

    else:
        wait = bot.reply_to(m, "جاري البحث عن أفضل الصفقات...")
        try:
            msg = build_trades_message()
            bot.edit_message_text(msg, m.chat.id, wait.message_id)
        except:
            bot.edit_message_text("حدث خطأ أثناء توليد الصفقات.", m.chat.id, wait.message_id)

# ================== التشغيل ==================
print("Bot is running...")

threading.Thread(target=auto_scan_loop, daemon=True).start()

def start_bot():
    bot.infinity_polling(skip_pending=True)

threading.Thread(target=start_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port)
