import asyncio
import yfinance as yf
import pandas as pd
import requests
import time
from telegram import Bot

# =============================
# 🔐 بياناتك
# =============================
TELEGRAM_TOKEN = "8568994708:AAFXTPTK3MyEe1wfrWTYBBUPfbi8zayOxi0"
CHAT_ID = "986199874"
CRYPTOPANIC_API_KEY = "a5563e90848ba81e4aeca929e26d90069b2d1b9f"

bot = Bot(token=TELEGRAM_TOKEN)

# =============================
# أفضل 25 عملة سيولة
# =============================
SYMBOLS = [
"ETH-USD","BNB-USD","XRP-USD","SOL-USD","ADA-USD",
"AVAX-USD","LINK-USD","DOT-USD","TRX-USD","LTC-USD",
"UNI-USD","XLM-USD","DOGE-USD","SHIB-USD","MATIC-USD",
"ATOM-USD","APT-USD","SUI-USD","FIL-USD","NEAR-USD",
"ICP-USD","TON-USD","BCH-USD","ARB-USD","OP-USD"
]

active_trades = {}

# =============================
# Indicators
# =============================
def calculate_indicators(df):
    if df.empty or len(df) < 200:
        return None
    df["ema200"] = df["Close"].ewm(span=200).mean()
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df

# =============================
# News
# =============================
def get_news():
    try:
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API_KEY}&filter=hot"
        r = requests.get(url, timeout=10).json()
        return r.get("results", [])
    except:
        return []

# =============================
# SMT مقارنة مع BTC
# =============================
def smt_divergence(alt_df, btc_df):
    if alt_df is None or btc_df is None:
        return False
    alt_high = alt_df["High"].iloc[-1]
    alt_prev = alt_df["High"].iloc[-5]
    btc_high = btc_df["High"].iloc[-1]
    btc_prev = btc_df["High"].iloc[-5]
    return alt_high > alt_prev and btc_high <= btc_prev

# =============================
# تحليل العملة
# =============================
def analyze(symbol, btc_15m, news):
    try:
        df1h = yf.download(symbol, period="7d", interval="1h", progress=False)
        df15 = yf.download(symbol, period="5d", interval="15m", progress=False)

        df1h = calculate_indicators(df1h)
        df15 = calculate_indicators(df15)

        if df1h is None or df15 is None:
            return None

        price = df15["Close"].iloc[-1]
        atr = df15["atr"].iloc[-1]
        ema = df1h["ema200"].iloc[-1]

        if pd.isna(atr) or atr == 0:
            return None

        score = 0

        # اتجاه
        direction = "LONG" if price > ema else "SHORT"
        score += 1

        # Liquidity Sweep بسيط
        if df15["High"].iloc[-1] > df15["High"].iloc[-5]:
            score += 1

        # SMT
        if smt_divergence(df15, btc_15m):
            score += 1

        if score < 3:
            return None

        sl = price - atr*1.5 if direction=="LONG" else price + atr*1.5
        risk = abs(price - sl)

        tp1 = price + risk*2 if direction=="LONG" else price - risk*2
        tp2 = price + risk*3 if direction=="LONG" else price - risk*3
        tp3 = price + risk*4 if direction=="LONG" else price - risk*4

        coin_news = "لا يوجد خبر مؤثر حالياً."
        sentiment = "Neutral ⚖️"
        name = symbol.split("-")[0]

        for n in news:
            if name.lower() in n["title"].lower():
                coin_news = n["title"]
                sentiment = "Positive ✅" if direction=="LONG" else "Negative 🔴"
                break

        return {
            "symbol": symbol,
            "price": price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "direction": direction,
            "news": coin_news,
            "sentiment": sentiment,
            "score": score
        }

    except:
        return None

# =============================
# إرسال الصفقة
# =============================
def send_signal(data):
    msg = f"""
🚀 تنبيه: دخول سيولة مؤسسية ضخمة!

🥇 العملة: {data['symbol']}

⚡ نوع العملية: {data['direction']}

📍 Entry: {data['price']:.4f}
🛡️ S.L   : {data['sl']:.4f}

🎯 TARGET:
-------------------------------
T.P 1 : {data['tp1']:.4f}
T.P 2 : {data['tp2']:.4f}
T.P 3 : {data['tp3']:.4f}

R:R    : 1:4

📰 رادار الأخبار:
💬 "{data['news']}" ({data['sentiment']})

❗إدارة المخاطر مسؤوليتك.
"""
    bot.send_message(CHAT_ID, msg)

# =============================
# متابعة الأهداف
# =============================
def monitor_targets():
    for sym, trade in list(active_trades.items()):
        df = yf.download(sym, period="1d", interval="5m", progress=False)
        if df.empty:
            continue
        price = df["Close"].iloc[-1]

        if not trade["tp1_hit"] and (
            (trade["direction"]=="LONG" and price>=trade["tp1"]) or
            (trade["direction"]=="SHORT" and price<=trade["tp1"])
        ):
            bot.send_message(CHAT_ID, f"✅ {sym} تم ضرب الهدف الأول (TP1)")
            trade["tp1_hit"] = True

        if not trade["tp2_hit"] and (
            (trade["direction"]=="LONG" and price>=trade["tp2"]) or
            (trade["direction"]=="SHORT" and price<=trade["tp2"])
        ):
            bot.send_message(CHAT_ID, f"🔥 {sym} تم ضرب الهدف الثاني (TP2)")
            trade["tp2_hit"] = True

# =============================
# المحرك الرئيسي
# =============================
async def run():
    print("Scanning market...")
    news = get_news()
    btc_15m = calculate_indicators(
        yf.download("BTC-USD", period="5d", interval="15m", progress=False)
    )

    opportunities = []

    for sym in SYMBOLS:
        if sym in active_trades:
            continue
        result = analyze(sym, btc_15m, news)
        if result:
            opportunities.append(result)

    opportunities = sorted(opportunities, key=lambda x: x["score"], reverse=True)[:2]

    for op in opportunities:
        send_signal(op)
        active_trades[op["symbol"]] = {
            "tp1": op["tp1"],
            "tp2": op["tp2"],
            "direction": op["direction"],
            "tp1_hit": False,
            "tp2_hit": False
        }

    monitor_targets()

# =============================
# Loop
# =============================
if __name__ == "__main__":
    while True:
        try:
            asyncio.run(run())
        except Exception as e:
            print("Error:", e)
        time.sleep(300)
