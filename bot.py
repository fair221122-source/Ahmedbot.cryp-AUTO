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
    def data_ok(df, min_len=60):
    return (
        df is not None and
        len(df) >= min_len and
        df.isnull().sum().sum() == 0
    )
def fetch_funding_rate(symbol):
    try:
        url = "https://fapi.binance.com/fapi/v1/fundingRate"
        params = {"symbol": symbol, "limit": 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            return float(data[0]["fundingRate"])
    except:
        return 0.0
    return 0.0
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
def avg_volume(df, period=50):
    if df is None or len(df) < period:
        return None
    return df["v"].iloc[-period:].mean()
    
# FVG مبسط على فريم الساعة
def detect_fvg_1h(df):
    # نموذج 3 شموع: الشمعة 1 – 2 – 3
    if len(df) < 3:
        return False

    h1 = df["h"].iloc[-3]
    l1 = df["l"].iloc[-3]
    h2 = df["h"].iloc[-2]
    l2 = df["l"].iloc[-2]
    h3 = df["h"].iloc[-1]
    l3 = df["l"].iloc[-1]

    # FVG صاعد: لو الشمعة 2 فتحت فجوة بين high الشمعة 1 و low الشمعة 3
    bullish_fvg = l3 > h1 and l2 > h1

    # FVG هابط: فجوة بين low الشمعة 1 و high الشمعة 3
    bearish_fvg = h3 < l1 and h2 < l1

    return bullish_fvg or bearish_fvg
# ================== تحليل 1D ==================
def analyze_symbol_1d(symbol):
    df = fetch_klines(symbol, "1d", 200)
    if not data_ok(df, 120):
    return None
    trend, momentum, desc = detect_trend(df)
    ch1, ch24, pos = calc_percent_metrics(df)
    atr = calc_atr(df, period=14)

    # تقدير Premium / Discount من موقع السعر داخل الرينج اليومي
    zone = "neutral"
    if pos > 70:
        zone = "premium"
    elif pos < 30:
        zone = "discount"

    return {
        "trend_1d": trend,
        "momentum_1d": momentum,
        "desc_1d": desc,
        "pos_1d": pos,
        "zone_1d": zone,
        "atr_1d": atr
    }

# ================== تحليل 1h ==================
def analyze_symbol_1h(symbol):
    df = fetch_klines(symbol, "1h", 200)
    if not data_ok(df, 120):
    return None
    trend, momentum, desc = detect_trend(df)
    ch1, ch24, pos = calc_percent_metrics(df)
    atr = calc_atr(df, period=14)
    fvg = detect_fvg_1h(df)
    vol_avg = avg_volume(df, 50)
vol_last = df["v"].iloc[-1]
    funding = fetch_funding_rate(symbol)
    return {
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
    "has_fvg": fvg,
    "funding": funding,
    "vol_avg": vol_avg,
    "vol_last": vol_last
    }
        
# ================== تحليل 4h ==================
def analyze_symbol_4h(symbol):
    df = fetch_klines(symbol, "4h", 200)
    if not data_ok(df, 80):
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

    vol_avg = avg_volume(df, 50)
if vol_avg is None:
    return None
    for i in range(4, -1, -1):
        if trend=="bull" and lower.iloc[i] > body.iloc[i]*1.5 and last["c"].iloc[i] > last["o"].iloc[i] and last["v"].iloc[i] > vol_avg * 0.7:
            return {
                "type":"long",
                "entry_type":"فوري",
                "entry_price":last["c"].iloc[i],
                "reason":"شمعة رفض هبوط على 15m"
            }
        if trend=="bear" and upper.iloc[i] > body.iloc[i]*1.5 and last["c"].iloc[i] < last["o"].iloc[i] and last["v"].iloc[i] > vol_avg * 0.7:
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

    # توافق الاتجاه بين 1D و 4H و 1H
    if t["trend_1d"] == t["trend_4h"] == t["trend"]:
        score += 25
    elif t["trend_1d"] == t["trend_4h"] or t["trend_1d"] == t["trend"]:
        score += 15

    # الزخم
    if t["momentum"] == "strong":
        score += 10
    if t["momentum_4h"] == "strong":
        score += 10
    if t["momentum_1d"] == "strong":
        score += 10

    # موقع السعر داخل الرينج (كلاستر سعري / سيولة)
    pos = t["pos_range"]
    if 30 <= pos <= 70:
        score += 10
    elif 20 <= pos < 30 or 70 < pos <= 80:
        score += 5

    # FVG على الساعة
    if t.get("has_fvg_1h"):
        score += 5

    # Premium / Discount من اليومي
    if t["trend_1d"] == "bull" and t["zone_1d"] == "discount":
        score += 10
    if t["trend_1d"] == "bear" and t["zone_1d"] == "premium":
        score += 10
# تأثير الـ Funding Rate
if "funding" in t:
    f = t["funding"]
    # إذا الاتجاه صاعد والتمويل سلبي قوي → نخفض النجاح
    if t["trend"] == "bull" and f < -0.0005:
        score -= 10
    # إذا الاتجاه هابط والتمويل إيجابي قوي → نخفض النجاح
    if t["trend"] == "bear" and f > 0.0005:
        score -= 10
        # تأثير الحجم
if "vol_avg" in t and "vol_last" in t:
    if t["vol_last"] < t["vol_avg"] * 0.6:
        score -= 10
    score = max(50, min(95, score))
    return score

# ================== اختيار R:R ديناميكي ==================
def choose_rr(info1, info4, info1d):
    base = 2.5
    rr = base

    # توافق الاتجاه بين 1D و 4H و 1H
    if info1d["trend_1d"] == info4["trend_4h"] == info1["trend"] and \
       info1["momentum"]=="strong" and info4["momentum_4h"]=="strong" and info1d["momentum_1d"]=="strong":
        rr = 5.0
    elif info1d["trend_1d"] == info4["trend_4h"] == info1["trend"]:
        rr = 4.0
    elif info1d["trend_1d"] == info4["trend_4h"] or info1d["trend_1d"] == info1["trend"]:
        rr = 3.0

    # إذا الحركة اليومية قوية في اتجاه الترند
    if info1d["trend_1d"] == "bull" and info1["change_24h"] > 5:
        rr = max(rr, 4.0)
    if info1d["trend_1d"] == "bear" and info1["change_24h"] < -5:
        rr = max(rr, 4.0)

    return min(max(rr, 2.5), 7.0)

# ================== أفضل الصفقات ==================
def find_best_trades(symbols):
    results = []

    for sym in symbols:
        info1d = analyze_symbol_1d(sym)
        if not info1d:
            continue

        info1 = analyze_symbol_1h(sym)
        if not info1:
            continue

        info4 = analyze_symbol_4h(sym)
        if not info4:
            continue

        # شرط الاتجاه: لا ندخل عكس الاتجاه اليومي
        if info1["trend"] != info1d["trend_1d"] or info4["trend_4h"] != info1d["trend_1d"]:
            continue

        df15 = fetch_klines(sym, "15m", 200)
        if not data_ok(df15, 80):
    continue

        entry = detect_entry_15m(df15, info1["trend"])
        if not entry:
            continue

        ep = entry["entry_price"]

        # ATR آمن من فريم الساعة
        atr = info1["atr_1h"]
        if atr is None or atr <= 0:
            continue

        rr = choose_rr(info1, info4, info1d)
        if rr < 2.5:
            continue

        if info1["trend"]=="bull":
            sl = ep - 1.5 * atr
            tp = ep + rr * 1.5 * atr
        else:
            sl = ep + 1.5 * atr
            tp = ep - rr * 1.5 * atr

        if (info1["trend"]=="bull" and not (sl < ep < tp)) or (info1["trend"]=="bear" and not (tp < ep < sl)):
            continue

        real_rr = abs((tp-ep)/(ep-sl))
        if real_rr < 2.5:
            continue

        results.append({
            "symbol": sym,
            "trend": info1["trend"],
            "trend_4h": info4["trend_4h"],
            "trend_1d": info1d["trend_1d"],
            "momentum": info1["momentum"],
            "momentum_4h": info4["momentum_4h"],
            "momentum_1d": info1d["momentum_1d"],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": real_rr,
            "change_1h": info1["change_1h"],
            "change_24h": info1["change_24h"],
            "pos_range": info1["pos_range"],
            "has_fvg_1h": info1["has_fvg"],
            "zone_1d": info1d["zone_1d"]
        })

        time.sleep(0.05)

    results = sorted(results, key=lambda x: x["rr"], reverse=True)
    return results[:2]

# ================== تحليل السوق ==================
def analyze_top_coins(symbols):
    out = []
    for sym in symbols:
        info1d = analyze_symbol_1d(sym)
        info1 = analyze_symbol_1h(sym)
        info4 = analyze_symbol_4h(sym)
        if info1d and info1 and info4:
            score = 0
            score += abs(info1["change_24h"])
            if info1["momentum"]=="strong":
                score += 5
            if info4["momentum_4h"]=="strong":
                score += 5
            if info1d["momentum_1d"]=="strong":
                score += 5
            if info1["trend"] == info4["trend_4h"] == info1d["trend_1d"]:
                score += 15
            info1["score"] = score
            info1["trend_4h"] = info4["trend_4h"]
            info1["momentum_4h"] = info4["momentum_4h"]
            info1["trend_1d"] = info1d["trend_1d"]
            info1["momentum_1d"] = info1d["momentum_1d"]
            info1["zone_1d"] = info1d["zone_1d"]
            out.append(info1)
    return sorted(out, key=lambda x: x["score"], reverse=True)[:5]

def build_analysis_message():
    coins = analyze_top_coins(top_20_liquid_coins())
    if not coins:
        return "لا يوجد تحليل متاح حالياً."

    msg = "📊 تحليل السوق العام على الإطارات الزمنية التالية:   (1D + 4H + 1H + 15M)\n\n"

    for i, c in enumerate(coins, 1):
        symbol = c["symbol"]

        base = 50
        if c["trend_1d"] == "bull":
            base += 10
        if c["trend_1d"] == c["trend_4h"] == c["trend"]:
            base += 15
        if c["momentum"] == "strong":
            base += 5
        if c["momentum_4h"] == "strong":
            base += 5
        if c["momentum_1d"] == "strong":
            base += 5
        if c["pos_range"] > 70 and c["trend"]=="bull":
            base += 5
        if c["pos_range"] < 30 and c["trend"]=="bear":
            base += 5

        prob = max(40, min(90, base))

        # 1D
        if c["trend_1d"]=="bull":
            txt1d = "اتجاه صاعد رئيسي"
        elif c["trend_1d"]=="bear":
            txt1d = "اتجاه هابط رئيسي"
        else:
            txt1d = "اتجاه يومي جانبي"

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

        direction_icon = "📈" if c["trend_1d"]=="bull" else "📉" if c["trend_1d"]=="bear" else "⚖️"
        direction_word = "صعود" if direction_icon=="📈" else "هبوط" if direction_icon=="📉" else "حركة جانبية"

        msg += f"{i}) {symbol}\n"
        msg += f"📅 1D: {txt1d}\n"
        msg += f"🕓 4h: {txt4}\n"
        msg += f"🕐 1h: {txt1}\n"
        msg += f"🕒 15m: {txt15}\n"
        msg += f"{direction_icon} التوقع: {prob:.0f}% {direction_word} خلال الساعات القادمة\n\n"

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

        rank_icon = "🥇" if i == 1 else "🥈"

        msg += f"{rank_icon} {t['symbol']} — {direction_icon} ({t['entry']['entry_type']})\n\n"
        msg += f"Entry: {fmt_price(ep)}\n"
        msg += f"SL: {fmt_price(sl)}\n"
        msg += f"TP: {fmt_price(tp)}\n"
        msg += f"R:R = 1:{rr:.2f}\n"
        msg += f"نسبة النجاح المتوقعة: {success:.0f}%\n\n"

        reason = t["entry"]["reason"]
        extra_reason = "توافق الإطارات الزمنية 1D+4H+1H"
        if t.get("has_fvg_1h"):
            extra_reason += " + وجود FVG على 1H"
        msg += f"📌 السبب: {extra_reason} + {reason}\n\n"

    return msg

# ================== رسالة الفحص التلقائي ==================
def build_auto_scan_message(trades):
    if not trades:
        return None

    t = trades[0]
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
    extra_reason = "شمعة رفض على 15m + اتجاه 1D/4H/1H متوافق"
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
        wait = bot.reply_to(m, "جاري تحليل السوق على (1D + 4H + 1H + 15M)...")
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
