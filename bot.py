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
LAST_SENT = {}                 # لمنع تكرار نفس الرسالة
LAST_TRADE_SIGNATURE = {}      # لمنع تكرار نفس الصفقة الذهبية

def can_send(key):
    now = time.time()
    if key in LAST_SENT:
        if now - LAST_SENT[key] < 1800:  # نصف ساعة
            return False
    LAST_SENT[key] = now
    return True

def make_signature(t):
    return f"{t['symbol']}-{t['trend']}-{t['entry']['entry_price']:.4f}-{t['sl']:.4f}-{t['tp']:.4f}"

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

def calc_atr(df, period=14):
    high = df["h"]; low = df["l"]; close = df["c"]
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr.iloc[-1] if not np.isnan(atr.iloc[-1]) else tr.mean()

# FVG مبسط على فريم الساعة: فجوة بين high السابقة و low الحالية أو العكس
def detect_fvg_1h(df):
    if len(df) < 3:
        return False
    h_prev = df["h"].iloc[-2]
    l_prev = df["l"].iloc[-2]
    l_cur = df["l"].iloc[-1]
    h_cur = df["h"].iloc[-1]
    # فجوة صاعدة أو هابطة بسيطة
    if l_cur > h_prev or h_cur < l_prev:
        return True
    return False

# ================== تحليل 1h ==================
def analyze_symbol_1h(symbol):
    df = fetch_klines(symbol, "1h", 200)
    if df is None or len(df)<60:
        return None
    trend, momentum, desc = detect_trend(df)
    ch1, ch24, pos = calc_percent_metrics(df)
    atr = calc_atr(df, period=14)
    fvg = detect_fvg_1h(df)
    return {
        "symbol": symbol,
        "trend": trend,
        "momentum": momentum,
        "description": desc,
        "last_price": df["c"].iloc[-1],
        "change_1h": ch1,
        "change_24h": ch24,
        "pos_range": pos,
        "atr_1h": atr,
        "has_fvg": fvg
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
            return {
                "type":"long",
                "entry_type":"فوري",
                "entry_price":last["c"].iloc[i],
                "reason":"شمعة رفض هبوط على 15m"
            }
        if trend=="bear" and upper.iloc[i] > body.iloc[i]*1.5 and last["c"].iloc[i] < last["o"].iloc[i]:
            return {
                "type":"short",
                "entry_type":"فوري",
                "entry_price":last["c"].iloc[i],
                "reason":"شمعة رفض صعود على 15m"
            }
    return None

# ================== تنسيق ==================
def fmt_price(x): return f"{x:.4f}"
def fmt_pct(x): return f"{x:.2f}%"

# ================== حساب نسبة النجاح المتوقعة ==================
def calc_success_prob(t):
    score = 0

    rr = t["rr"]
    # R:R
    if rr >= 2.5:
        score += 30
    if rr >= 3.5:
        score += 10
    if rr >= 5:
        score += 10
    if rr >= 7:
        score += 5

    # توافق الاتجاه بين 1h و 4h
    if t["trend"] == t["trend_4h"]:
        score += 20

    # الزخم
    if t["momentum"] == "strong":
        score += 10
    if t["momentum_4h"] == "strong":
        score += 10

    # موقع السعر داخل الرينج (proxy لمناطق السيولة/الـ pool/الـ swing)
    pos = t["pos_range"]
    if 30 <= pos <= 70:
        score += 10
    elif 20 <= pos < 30 or 70 < pos <= 80:
        score += 5

    # FVG على الساعة (فرصة عودة السعر لملئها)
    if t.get("has_fvg_1h"):
        score += 5

    score = max(50, min(95, score))
    return score

# ================== اختيار R:R ديناميكي ==================
def choose_rr(info1, info4):
    base = 2.5
    rr = base
    # توافق الاتجاه + زخم قوي يرفع الهدف
    if info1["trend"] == info4["trend_4h"] and info1["momentum"]=="strong" and info4["momentum_4h"]=="strong":
        rr = 4.0
    elif info1["trend"] == info4["trend_4h"] and (info1["momentum"]=="strong" or info4["momentum_4h"]=="strong"):
        rr = 3.0
    # إذا السوق قوي جدًا ممكن نسمح أعلى
    if rr >= 4 and info1["change_24h"]* (1 if info1["trend"]=="bull" else -1) > 5:
        rr = 5.0
    # سقف 7 لو حاب توسع لاحقًا
    return min(rr, 7.0)

# ================== أفضل الصفقات ==================
def find_best_trades(symbols):
    results = []

    for sym in symbols:
        info1 = analyze_symbol_1h(sym)
        if not info1:
            continue

        info4 = analyze_symbol_4h(sym)
        if not info4:
            continue

        # شرط الاتجاه: لا ندخل عكس 4h
        if info1["trend"] != info4["trend_4h"]:
            continue

        df15 = fetch_klines(sym, "15m", 200)
        if df15 is None:
            continue

        entry = detect_entry_15m(df15, info1["trend"])
        if not entry:
            continue

        ep = entry["entry_price"]

        # ATR آمن من فريم الساعة
        atr = info1["atr_1h"]
        if atr is None or atr <= 0:
            continue

        rr = choose_rr(info1, info4)
        if rr < 2.5:
            continue

        if info1["trend"]=="bull":
            sl = ep - 1.5 * atr
            tp = ep + rr * 1.5 * atr
        else:
            sl = ep + 1.5 * atr
            tp = ep - rr * 1.5 * atr

        # تأكد أن SL و TP منطقيين
        if (info1["trend"]=="bull" and not (sl < ep < tp)) or (info1["trend"]=="bear" and not (tp < ep < sl)):
            continue

        real_rr = abs((tp-ep)/(ep-sl))
        if real_rr < 2.5:
            continue

        results.append({
            "symbol": sym,
            "trend": info1["trend"],
            "trend_4h": info4["trend_4h"],
            "momentum": info1["momentum"],
            "momentum_4h": info4["momentum_4h"],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": real_rr,
            "change_1h": info1["change_1h"],
            "change_24h": info1["change_24h"],
            "pos_range": info1["pos_range"],
            "has_fvg_1h": info1["has_fvg"]
        })

        time.sleep(0.05)

    results = sorted(results, key=lambda x: x["rr"], reverse=True)
    return results[:2]

# ================== تحليل السوق ==================
def analyze_top_coins(symbols):
    out = []
    for sym in symbols:
        info1 = analyze_symbol_1h(sym)
        info4 = analyze_symbol_4h(sym)
        if info1 and info4:
            score = 0
            # قوة الحركة اليومية
            score += abs(info1["change_24h"])
            # زخم
            if info1["momentum"]=="strong":
                score += 5
            if info4["momentum_4h"]=="strong":
                score += 5
            # توافق الاتجاه
            if info1["trend"] == info4["trend_4h"]:
                score += 10
            info1["score"] = score
            info1["trend_4h"] = info4["trend_4h"]
            info1["momentum_4h"] = info4["momentum_4h"]
            out.append(info1)
    return sorted(out, key=lambda x: x["score"], reverse=True)[:5]

def build_analysis_message():
    coins = analyze_top_coins(top_20_liquid_coins())
    if not coins:
        return "لا يوجد تحليل متاح حالياً."

    msg = "📊 تحليل السوق العام على الإطارات الزمنية التالية:   (4H + 1H + 15M)\n\n"

    for i, c in enumerate(coins, 1):
        symbol = c["symbol"]
        # تقدير بسيط لنسبة التوقع
        base = 50
        if c["trend_4h"] == "bull" and c["trend"] == "bull":
            base += 15
        if c["momentum"] == "strong":
            base += 10
        if c["momentum_4h"] == "strong":
            base += 10
        if c["pos_range"] > 70 and c["trend"]=="bull":
            base += 5
        if c["pos_range"] < 30 and c["trend"]=="bear":
            base += 5
        prob = max(40, min(90, base))

        # توصيف مبسط للفريمات
        # 4h
        if c["trend_4h"]=="bull":
            txt4 = "اتجاه صاعد قوي" if c["momentum_4h"]=="strong" else "اتجاه صاعد"
        elif c["trend_4h"]=="bear":
            txt4 = "اتجاه هابط قوي" if c["momentum_4h"]=="strong" else "اتجاه هابط"
        else:
            txt4 = "اتجاه جانبي"

        # 1h
        if c["trend"]=="bull":
            txt1 = "زخم إيجابي مستمر" if c["momentum"]=="strong" else "زخم إيجابي"
        elif c["trend"]=="bear":
            txt1 = "زخم بيعي" if c["momentum"]=="strong" else "زخم ضعيف"
        else:
            txt1 = "حركة جانبية"

        # 15m (تقدير من موقع السعر)
        if 40 <= c["pos_range"] <= 60:
            txt15 = "تماسك قبل اندفاع محتمل"
        elif c["pos_range"] > 70 and c["trend"]=="bull":
            txt15 = "منطقة قرب مقاومة محتملة"
        elif c["pos_range"] < 30 and c["trend"]=="bear":
            txt15 = "ضغط بيعي قرب دعم مكسور"
        else:
            txt15 = "حركة متذبذبة"

        direction_icon = "📈" if c["trend_4h"]=="bull" or c["trend"]=="bull" else "📉"

        msg += f"{i}) {symbol}\n"
        msg += f"🕓 4h: {txt4}\n"
        msg += f"🕐 1h: {txt1}\n"
        msg += f"🕒 15m: {txt15}\n"
        msg += f"{direction_icon} التوقع: {prob:.0f}% {'صعود' if direction_icon=='📈' else 'هبوط'} خلال الساعات القادمة\n\n"

    return msg

# ================== رسالة الصفقات اليدوية ==================
def build_trades_message(trades=None):
    if trades is None:
        trades = find_best_trades(top_20_liquid_coins())

    if not trades:
        return "لا توجد صفقات واضحة حالياً."

    msg = ""

    for i, t in enumerate(trades, 1):
        ep = t["entry"]["entry_price"]
        sl = t["sl"]; tp = t["tp"]
        rr = t["rr"]
        success = calc_success_prob(t)
        direction_icon = "🟢" if t["trend"]=="bull" else "🔴"

        if i == 1:
            rank_icon = "🥇"
        else:
            rank_icon = "🥈"

        msg += f"{rank_icon} {t['symbol']} — {direction_icon} ({t['entry']['entry_type']})\n\n"
        msg += f"Entry: {fmt_price(ep)}\n"
        msg += f"SL: {fmt_price(sl)}\n"
        msg += f"TP: {fmt_price(tp)}\n"
        msg += f"R:R = 1:{rr:.2f}\n"
        msg += f"نسبة النجاح المتوقعة: {success:.0f}%\n\n"

        reason = t["entry"]["reason"]
        extra_reason = "توافق الإطارات الزمنية 4H+1H"
        if t.get("has_fvg_1h"):
            extra_reason += " + وجود FVG على 1H"
        msg += f"📌 السبب: {extra_reason} + {reason}\n\n"

    return msg

# ================== رسالة الفحص التلقائي ==================
def build_auto_scan_message(trades):
    if not trades:
        return None

    t = trades[0]  # نأخذ أفضل صفقة واحدة في الفحص التلقائي
    ep = t["entry"]["entry_price"]
    sl = t["sl"]; tp = t["tp"]
    rr = t["rr"]
    success = calc_success_prob(t)
    direction = "Long" if t["trend"]=="bull" else "Short"

    msg = "⏰ فحص تلقائي — فرصة جديدة\n\n"
    msg += f"🎯 {t['symbol']} — {direction} (فوري)\n"
    msg += f"Entry: {fmt_price(ep)}\n"
    msg += f"SL: {fmt_price(sl)}\n"
    msg += f"TP: {fmt_price(tp)}\n"
    msg += f"R:R = 1:{rr:.2f}\n"
    msg += f"نسبة النجاح المتوقعة: {success:.0f}%\n\n"

    reason = t["entry"]["reason"]
    extra_reason = "شمعة رفض على 15m + اتجاه 4h/1h متوافق"
    if t.get("has_fvg_1h"):
        extra_reason += " + FVG على 1H"
    msg += f"📌 السبب: {extra_reason}\n"

    return msg

# ================== الفحص التلقائي ==================
def auto_scan_loop():
    global LAST_CHAT_ID, LAST_TRADE_SIGNATURE
    while True:
        try:
            if LAST_CHAT_ID:
                trades = find_best_trades(top_20_liquid_coins())
                if trades:
                    now = time.time()
                    filtered = []
                    for t in trades:
                        sig = make_signature(t)
                        if sig in LAST_TRADE_SIGNATURE and now - LAST_TRADE_SIGNATURE[sig] < 1800:
                            continue
                        LAST_TRADE_SIGNATURE[sig] = now
                        filtered.append(t)

                    if filtered:
                        msg = build_auto_scan_message(filtered)
                        if msg and can_send("auto_scan_msg"):
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
    bot.reply_to(m, "🚀 أهلاً بك، اختر:\n• تحليل\n• صفقات", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text in ["تحليل","صفقات"])
def main_handler(m):
    global LAST_CHAT_ID
    LAST_CHAT_ID = m.chat.id

    if m.text=="تحليل":
        wait = bot.reply_to(m, "جاري تحليل السوق على (4H + 1H + 15M)...")
        try:
            msg = build_analysis_message()
            bot.edit_message_text(msg, m.chat.id, wait.message_id)
        except Exception as e:
            bot.edit_message_text("حدث خطأ أثناء التحليل.", m.chat.id, wait.message_id)
            print("Analysis error:", e)

    else:
        wait = bot.reply_to(m, "جاري البحث عن أفضل الصفقات...")
        try:
            msg = build_trades_message()
            bot.edit_message_text(msg, m.chat.id, wait.message_id)
        except Exception as e:
            bot.edit_message_text("حدث خطأ أثناء توليد الصفقات.", m.chat.id, wait.message_id)
            print("Trades error:", e)

# ================== التشغيل ==================
print("Bot is running...")

threading.Thread(target=auto_scan_loop, daemon=True).start()

def start_bot():
    bot.infinity_polling(skip_pending=True)

threading.Thread(target=start_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port)
