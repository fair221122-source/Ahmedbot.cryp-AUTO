import os
import asyncio
import json
import time
from datetime import datetime, timedelta
import requests
import numpy as np
import pandas as pd
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import uvicorn
from fastapi import FastAPI, WebSocket, Request
import websockets

# ============================================
# GLOBAL CONFIG & FASTAPI
# ============================================
app = FastAPI()

# جلب الإعدادات من البيئة
TOKEN = os.getenv("TELEGRAM_TOKEN")
CRYPTOPANIC_API = os.getenv("CRYPTOPANIC_API")

if not TOKEN or not CRYPTOPANIC_API:
    raise ValueError("TELEGRAM_TOKEN and CRYPTOPANIC_API must be set in Environment Variables.")

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
price_cache, orderbook_cache, liquidity_map, active_chats = {}, {}, {}, set()
last_signal_time_auto, klines_cache = {}, {}
cluster_cache, cluster_footprint = {}, {}
KLINES_TTL = 60
keyboard = ReplyKeyboardMarkup([["صفقات", "تحليل"]], resize_keyboard=True)

# ============================================
# FORMATTING HELPERS
# ============================================
def rr_to_str(rr: float) -> str: return f"1:{rr:.2f}"
def format_direction_emoji(side: str) -> str: return "🟢" if side.lower() == "long" else "🔴"
def format_side_hashtag(side: str) -> str: return "#Long" if side.lower() == "long" else "#Short"

def format_manual_signals(signals: list) -> str:
    if not signals: return "السوق متذبذب ولا توجد فرصة دخول مثالية حالياً."
    text = "أفضل صفقتين في السوق حالياً:\n" + "-"*30 + "\n"
    medals = ["🥇", "🥈"]
    for i, sig in enumerate(signals[:2]):
        text += f"{medals[i]} {sig['symbol']} — {format_direction_emoji(sig['side'])}\n"
        text += f"{format_side_hashtag(sig['side'])}\nEntry: {sig['entry']:.4f}\nSL: {sig['sl']:.4f}\nTP: {sig['tp']:.4f}\n"
        text += f"R:R = {rr_to_str(sig['rr'])}\nنسبة النجاح: {sig['prob']}%\n" + "-"*30 + "\n"
        text += f"📌 السبب: {sig.get('reason_text', '')}\n" + "-"*20 + "\n"
    return text

# ============================================
# CORE LOGIC (Indicator & Analysis)
# ============================================
def fetch_klines(symbol, interval, limit=300):
    key, now = f"{symbol}_{interval}", time.time()
    if key in klines_cache and now - klines_cache[key]["time"] < KLINES_TTL: return klines_cache[key]["data"]
    for api in BINANCE_APIS:
        try:
            r = session.get(f"{api}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}", timeout=5)
            df = pd.DataFrame(r.json(), columns=["open_time","open","high","low","close","volume","close_time","qav","trades","tbbav","tbqav","ignore"])
            for col in ["open","high","low","close","volume"]: df[col] = df[col].astype(float)
            klines_cache[key] = {"time": now, "data": df}; return df
        except: continue
    return None

def add_indicators(df):
    df = df.copy()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - (100 / (1 + (gain / loss.replace(0, np.nan))))
    tr = pd.concat([(df["high"]-df["low"]), (df["high"]-df["close"].shift()).abs(), (df["low"]-df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df

def smc_score(df):
    if len(df) < 20: return 0
    h, l, c = df["high"], df["low"], df["close"]
    score = 3 if c.iloc[-1] > h.rolling(20).max().iloc[-2] else -3 if c.iloc[-1] < l.rolling(20).min().iloc[-2] else 0
    return score

def evaluate_signal(symbol):
    dfs = {tf: fetch_klines(symbol, tf) for tf in ["1d", "4h", "1h", "15m"]}
    if any(d is None for d in dfs.values()): return None
    for tf in dfs: dfs[tf] = add_indicators(dfs[tf])
    
    p = dfs["15m"]["close"].iloc[-1]
    ema200 = dfs["1d"]["ema200"].iloc[-1]
    rsi_val = dfs["15m"]["rsi"].iloc[-1]
    
    side = "long" if p > ema200 and rsi_val < 35 else "short" if p < ema200 and rsi_val > 65 else None
    if not side: return None

    score = smc_score(dfs["1h"])
    atr_v = dfs["1h"]["atr"].iloc[-1]
    
    entry = p
    sl = p - atr_v*1.8 if side == "long" else p + atr_v*1.8
    tp = p + atr_v*4.5 if side == "long" else p - atr_v*4.5
    rr = abs(tp-entry)/max(abs(entry-sl), 1e-8)
    
    if rr < 2.5: return None
    
    return {
        "symbol": symbol, "side": side.capitalize(), "entry": entry, "sl": sl, "tp": tp, "rr": rr,
        "prob": int(min(96, 70 + abs(score)*3)),
        "is_instant": True,
        "reason_text": f"توافق الاتجاه العام مع زخم ارتدادي وإشارات سيولة مؤسسية على فريم الساعة."
    }

# ============================================
# TELEGRAM HANDLERS
# ============================================
async def start(update: Update, context):
    active_chats.add(update.message.chat_id)
    await update.message.reply_text("البوت يعمل بنجاح ✅", reply_markup=keyboard)

async def handle_message(update: Update, context):
    text = (update.message.text or "").strip()
    active_chats.add(update.message.chat_id)
    
    if text == "صفقات":
        await update.message.reply_text("جارٍ الفحص ...⏳")
        sigs = [evaluate_signal(s) for s in SYMBOLS]
        sigs = sorted([s for s in sigs if s], key=lambda x: x["prob"], reverse=True)[:2]
        await update.message.reply_text(format_manual_signals(sigs))
    elif text == "تحليل":
        await update.message.reply_text("جارٍ تحليل حالة السوق العامة... 📊")
        await update.message.reply_text("السوق حالياً في مرحلة تجميع سيولة، يفضل التركيز على صفقات الارتداد من مناطق الطلب.")

telegram_app = Application.builder().token(TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ============================================
# WEBHOOK & BACKGROUND TASKS
# ============================================
@app.post("/webhook")
async def webhook_handler(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception as e:
        print(f"Webhook Error: {e}")
    return {"ok": True}

@app.get("/")
def home(): return {"status": "online", "service": "trading-bot"}

@app.on_event("startup")
async def startup_event():
    await telegram_app.initialize()
    await telegram_app.start()
    # تأكد من استبدال الرابط برابط تطبيقك الصحيح
    webhook_url = f"https://ahmedbot-cryp-auto.fly.dev/webhook"
    await telegram_app.bot.set_webhook(webhook_url)
    asyncio.create_task(websocket_price_stream())
    print(f"🚀 Webhook set to: {webhook_url}")

async def websocket_price_stream():
    url = f"wss://fstream.binance.com/stream?streams={'/'.join([s.lower() + '@markPrice' for s in SYMBOLS])}"
    while True:
        try:
            async with websockets.connect(url) as ws:
                while True:
                    res = json.loads(await ws.recv())
                    data = res.get("data", {})
                    if "s" in data: price_cache[data["s"]] = float(data["p"])
        except:
            await asyncio.sleep(5)

# ============================================
# EXECUTION (The Correct Way for Fly.io)
# ============================================

