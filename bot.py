import os
import time
import json
import asyncio
from typing import List, Dict, Any, Optional

import httpx
import websockets
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# =========================
# الإعدادات الأساسية
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
# تصحيح الروابط لتعمل مع الفيوتشر (العقود الآجلة)
BINANCE_REST_URL = "https://fapi.binance.com" 
BINANCE_WS_URL = "wss://fstream.binance.com/ws"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","NEARUSDT","TRXUSDT","LTCUSDT","UNIUSDT","ARBUSDT","OPUSDT","SUIUSDT","FILUSDT","STXUSDT",
    "APTUSDT","INJUSDT","SEIUSDT","PEPEUSDT","ORDIUSDT","TAOUSDT","ENAUSDT","FTMUSDT","HBARUSDT","ARUSDT"
]

AUTO_TRADE_COOLDOWN = 1800 
app = FastAPI()

# حالة البوت
last_auto_trade: Dict[str, float] = {}
pending_notified: Dict[str, bool] = {}
symbol_cvd: Dict[str, float] = {s: 0.0 for s in SYMBOLS}
AUTO_SCAN_CHAT_ID: Optional[int] = None
open_trades: Dict[str, Dict[str, Any]] = {}

# =========================
# دوال المساعدة والاتصال
# =========================
async def call_api(url: str, params=None):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        return r.json()

async def send_msg(chat_id: int, text: str):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    async with httpx.AsyncClient() as client:
        await client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

def wrap_rtl(text): return f"\u202B{text}"

# =========================
# أدوات التحليل المؤسسي (SMC/ICT)
# =========================
def get_institutional_checks(klines):
    if not klines or len(klines) < 10: return {"mss": False, "fvg": False, "pd_zone": "neutral"}
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    
    mss = False
    if closes[-1] > max(highs[-10:-5]): mss = "bullish"
    elif closes[-1] < min(lows[-10:-5]): mss = "bearish"
    
    fvg = False
    if float(klines[-3][3]) > float(klines[-1][2]): fvg = "bullish"
    elif float(klines[-3][2]) < float(klines[-1][3]): fvg = "bearish"
    
    rng_high, rng_low = max(highs[-50:]), min(lows[-50:])
    mid_point = (rng_high + rng_low) / 2
    pd_zone = "discount" if closes[-1] < mid_point else "premium"
    
    return {"mss": mss, "fvg": fvg, "pd_zone": pd_zone}

def compute_dynamic_params(prob, momentum):
    atr_mult = 1.7 + (min(abs(momentum), 10) / 100) 
    rr = 2.5 + ((prob - 50) / 40 * 4.5)
    return round(atr_mult, 2), round(rr, 2)

def calc_levels(price, atr, trend, rr):
    atr_mult = 1.75 
    sl_dist = atr * atr_mult
    if trend == "bullish":
        sl = price - sl_dist
        tp = price + (sl_dist * rr)
        return {"entry": price, "sl": sl, "tp": tp, "rr": rr, "side": "buy"}
    else:
        sl = price + sl_dist
        tp = price - (sl_dist * rr)
        return {"entry": price, "sl": sl, "tp": tp, "rr": rr, "side": "sell"}

async def analyze_market(symbol: str):
    # تصحيح المسار لطلب بيانات الفيوتشر fapi
    k4h = await call_api(f"{BINANCE_REST_URL}/fapi/v1/klines", {"symbol": symbol, "interval": "4h", "limit": 100})
    k1h = await call_api(f"{BINANCE_REST_URL}/fapi/v1/klines", {"symbol": symbol, "interval": "1h", "limit": 100})
    
    checks_4h = get_institutional_checks(k4h)
    checks_1h = get_institutional_checks(k1h)
    
    prob = 50
    reasons = []
    
    if checks_4h["mss"] == checks_1h["mss"] and checks_4h["mss"]:
        prob += 20
        reasons.append("توافق هيكل السوق (MSS) 4H+1H")
    
    if checks_1h["fvg"] == checks_1h["mss"]:
        prob += 15
        reasons.append("وجود فجوة سعرية (FVG) تدعم الاتجاه")

    cvd = symbol_cvd.get(symbol, 0)
    if (cvd > 0 and checks_1h["mss"] == "bullish") or (cvd < 0 and checks_1h["mss"] == "bearish"):
        prob += 15
        reasons.append("تدفق سيولة مؤسسية (CVD)")

    if (checks_1h["mss"] == "bullish" and checks_1h["pd_zone"] == "discount") or \
       (checks_1h["mss"] == "bearish" and checks_1h["pd_zone"] == "premium"):
        prob += 10
        reasons.append("السعر في منطقة دخول مثالية (PD Array)")

    highs = [float(k[2]) for k in k1h]
    lows = [float(k[3]) for k in k1h]
    atr = (sum(highs[-14:]) - sum(lows[-14:])) / 14
    
    return {
        "symbol": symbol, "prob": min(prob, 95), "trend": checks_1h["mss"] or "neutral", 
        "price": float(k1h[-1][4]), "atr": atr, "reasons": " + ".join(reasons), "momentum": cvd
    }

def build_trade_output(res, is_auto=False):
    atr_mult, rr = compute_dynamic_params(res["prob"], res["momentum"])
    lvl = calc_levels(res["price"], res["atr"], res["trend"], rr)
    deviation = abs(res["price"] - lvl["entry"]) / lvl["entry"]
    
    if deviation <= 0.005:
        trade_type = "فوري"
        footer = ""
    else:
        trade_type = "معلق"
        footer = "\n\nسيتم إرسال رسالة عند وصول السعر إلى منطقة الدخول المقترحة."
    
    side_icon = "🟢 Long" if lvl["side"] == "buy" else "🔴 Short"
    header = "⏰ فحص تلقائي — فرصة جديدة" if is_auto else f"🥇 {res['symbol']} — {side_icon[0]}"
    
    msg = (f"{header} ({trade_type})\n\n"
           f"🎯 {res['symbol']} — {side_icon}\n"
           f"Entry: {lvl['entry']:.4f}\n"
           f"SL: {lvl['sl']:.4f}\n"
           f"TP: {lvl['tp']:.4f}\n"
           f"R:R = 1:{lvl['rr']:.2f}\n"
           f"نسبة النجاح المتوقعة: {res['prob']}%\n\n"
           f"📌 توضيح: {res['reasons']}{footer}")
    return wrap_rtl(msg), lvl

async def auto_scan_loop():
    while True:
        await asyncio.sleep(300) 
        if not AUTO_SCAN_CHAT_ID: continue
        for s in SYMBOLS:
            if time.time() - last_auto_trade.get(s, 0) < AUTO_TRADE_COOLDOWN: continue
            try:
                res = await analyze_market(s)
                if res["prob"] >= 75 and res["trend"] != "neutral":
                    msg, lvl = build_trade_output(res, is_auto=True)
                    await send_msg(AUTO_SCAN_CHAT_ID, msg)
                    last_auto_trade[s] = time.time()
                    open_trades[s] = {"tp": lvl["tp"], "entry": lvl["entry"], "side": lvl["side"], "chat_id": AUTO_SCAN_CHAT_ID}
                    break
            except: continue

async def price_tracker():
    streams = "/".join(f"{s.lower()}@aggTrade" for s in SYMBOLS)
    
    async with websockets.connect(f"{BINANCE_WS_URL}/{streams}") as ws:
        while True:
            try:
                msg = await ws.recv()
                data = json.loads(msg)
                
                price_info = data.get('data', data) 
                symbol = data.get('stream', '').split('@')[0].upper() if 'stream' in data else data.get('s', '').upper()
                
                if 'p' in price_info:
                    price = float(price_info['p'])
                    quantity = float(price_info['q'])
                    is_buyer_maker = price_info['m']
                    
                    # تصحيح المسافات للـ CVD
                    symbol_cvd[symbol] += -quantity if is_buyer_maker else quantity
                    
                    if symbol in open_trades:
                        t = open_trades[symbol]
                        if abs(price - t['entry']) / t['entry'] < 0.002 and not pending_notified.get(symbol):
                            await send_msg(t['chat_id'], wrap_rtl(f"السعر الآن في منطقة الدخول لعملة #{symbol}"))
                            pending_notified[symbol] = True
                        
                        if (t['side'] == "buy" and price >= t['tp']) or (t['side'] == "sell" and price <= t['tp']):
                            await send_msg(t['chat_id'], wrap_rtl(f"🎯 تم الوصول للهدف في عملة #{symbol}"))
                            del open_trades[symbol]
            except:
                await asyncio.sleep(1)
                continue

@app.get("/")
async def root(): return {"status": "online"}

@app.post("/webhook")
async def webhook(req: Request):
    global AUTO_SCAN_CHAT_ID
    data = await req.json()
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        AUTO_SCAN_CHAT_ID = chat_id
        if text == "صفقات":
            results = []
            for s in SYMBOLS:
                try: results.append(await analyze_market(s))
                except: continue
            results.sort(key=lambda x: x["prob"], reverse=True)
            for res in results[:2]:
                if res["trend"] != "neutral":
                    msg, lvl = build_trade_output(res, is_auto=False)
                    await send_msg(chat_id, msg)
                    open_trades[res['symbol']] = {"tp": lvl["tp"], "entry": lvl["entry"], "side": lvl["side"], "chat_id": chat_id}
    return {"ok": True}

@app.on_event("startup")
async def startup():
    asyncio.create_task(price_tracker())
    asyncio.create_task(auto_scan_loop())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
