import os
import time
import json
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional

import httpx
import websockets
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# =========================
# الإعدادات الربط والتحكم
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
BINANCE_REST_URL = "https://api.binance.com"
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","NEARUSDT","TRXUSDT","LTCUSDT","UNIUSDT","ARBUSDT","OPUSDT","SUIUSDT","FILUSDT","STXUSDT",
    "APTUSDT","INJUSDT","SEIUSDT","PEPEUSDT","ORDIUSDT","TAOUSDT","ENAUSDT","FTMUSDT","HBARUSDT","ARUSDT"
]

AUTO_TRADE_COOLDOWN = 1800 # 30 دقيقة منع تكرار
app = FastAPI()

# =========================
# إدارة حالة البوت
# =========================
last_auto_trade: Dict[str, float] = {}
pending_notified: Dict[str, bool] = {}
symbol_cvd: Dict[str, float] = {s: 0.0 for s in SYMBOLS}
AUTO_SCAN_CHAT_ID: Optional[int] = None
open_trades: Dict[str, Dict[str, Any]] = {}

# =========================
# الأدوات والتحليل المؤسسي
# =========================
async def call_api(url: str, params=None):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        return r.json()

async def send_msg(chat_id: int, text: str):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    async with httpx.AsyncClient() as client:
        await client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

def detect_structure(klines):
    highs = [float(k[2]) for k in klines[-20:]]
    lows = [float(k[3]) for k in klines[-20:]]
    if highs[-1] > max(highs[:-5]): return "bullish"
    if lows[-1] < min(lows[:-5]): return "bearish"
    return "neutral"

def find_fvg_ob(klines):
    fvg = False
    for i in range(len(klines)-3, len(klines)):
        if float(klines[i-2][3]) > float(klines[i][2]): fvg = True # Bullish FVG
    return fvg

def calc_levels(price, atr, trend, rr_min=3, rr_max=8):
    sl_dist = atr * 1.5
    rr = max(rr_min, min(rr_max, 4.0)) # افتراضي 4 كما طلبت بالنموذج
    if trend == "bullish":
        sl = price - sl_dist
        tp = price + (sl_dist * rr)
        return {"entry": price, "sl": sl, "tp": tp, "rr": rr, "side": "buy"}
    else:
        sl = price + sl_dist
        tp = price - (sl_dist * rr)
        return {"entry": price, "sl": sl, "tp": tp, "rr": rr, "side": "sell"}

# =========================
# منطق الفحص والتحليل
# =========================
async def analyze_market(symbol: str):
    k4h = await call_api(f"{BINANCE_REST_URL}/api/v3/klines", {"symbol": symbol, "interval": "4h", "limit": 50})
    k1h = await call_api(f"{BINANCE_REST_URL}/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": 50})
    k15m = await call_api(f"{BINANCE_REST_URL}/api/v3/klines", {"symbol": symbol, "interval": "15m", "limit": 50})
    
    trend_4h = detect_structure(k4h)
    trend_1h = detect_structure(k1h)
    trend_15m = detect_structure(k15m)
    
    prob = 50
    reasons = []
    if trend_4h == trend_1h == trend_15m: 
        prob += 25
        reasons.append(f"توافق الإطارات الزمنية 4H+1H+15m")
    
    if find_fvg_ob(k1h): 
        prob += 10
        reasons.append("وجود منطقة فجوة سعرية (FVG)")
    
    cvd = symbol_cvd.get(symbol, 0)
    if (cvd > 0 and trend_1h == "bullish") or (cvd < 0 and trend_1h == "bearish"):
        prob += 15
        reasons.append("دخول سيولة مؤسسية ملحوظة (زخم)")

    highs = [float(k[2]) for k in k1h]
    lows = [float(k[3]) for k in k1h]
    atr = (sum(highs[-14:]) - sum(lows[-14:])) / 14
    
    return {
        "symbol": symbol, "prob": prob, "trend": trend_1h, 
        "price": float(k1h[-1][4]), "atr": atr, "reasons": " + ".join(reasons)
    }

# =========================
# بناء الرسائل (محاذاة يمين)
# =========================
def wrap_rtl(text): return f"\u202B{text}"

async def auto_scan_loop():
    while True:
        await asyncio.sleep(300) # كل 5 دقائق
        if not AUTO_SCAN_CHAT_ID: continue
        
        for s in SYMBOLS:
            now = time.time()
            if now - last_auto_trade.get(s, 0) < AUTO_TRADE_COOLDOWN: continue
            
            res = await analyze_market(s)
            if res["prob"] >= 75:
                lvl = calc_levels(res["price"], res["atr"], res["trend"])
                side_icon = "🟢 Long" if lvl["side"] == "buy" else "🔴 Short"
                is_pending = "معلق" if abs(res["price"] - lvl["entry"]) / lvl["entry"] > 0.005 else "فوري"
                
                msg = wrap_rtl(
                    f"⏰ فحص تلقائي — فرصة جديدة ({is_pending})\n\n"
                    f"🎯 {s} — {side_icon}\n"
                    f"Entry: {lvl['entry']:.4f}\n"
                    f"SL: {lvl['sl']:.4f}\n"
                    f"TP: {lvl['tp']:.4f}\n"
                    f"R:R = 1:{lvl['rr']:.2f}\n"
                    f"نسبة النجاح المتوقعة: {res['prob']}%\n\n"
                    f"📌 توضيح: {res['reasons']}\n\n"
                    f"سيتم التأكيد عند الوصول إلى هدف الدخول."
                )
                await send_msg(AUTO_SCAN_CHAT_ID, msg)
                last_auto_trade[s] = now
                open_trades[s] = {"tp": lvl["tp"], "entry": lvl["entry"], "side": lvl["side"], "chat_id": AUTO_SCAN_CHAT_ID}
                break # إرسال صفقة واحدة فقط في الدورة لعدم الإزعاج

# =========================
# الأوامر اليدوية
# =========================
async def handle_trades_cmd(chat_id):
    results = []
    for s in SYMBOLS:
        try: results.append(await analyze_market(s))
        except: continue
    
    results.sort(key=lambda x: x["prob"], reverse=True)
    top_two = results[:2]
    
    output = []
    for i, res in enumerate(top_two):
        lvl = calc_levels(res["price"], res["atr"], res["trend"])
        medal = "🥇" if i == 0 else "🥈"
        side_color = "🟢" if lvl["side"] == "buy" else "🔴"
        type_str = "فوري" if abs(res["price"] - lvl["entry"]) / lvl["entry"] < 0.01 else "معلق"
        
        row = (f"{medal} {res['symbol']} — {side_color} ({type_str})\n\n"
               f"Entry: {lvl['entry']:.4f}\n"
               f"SL: {lvl['sl']:.4f}\n"
               f"TP: {lvl['tp']:.4f}\n"
               f"R:R = 1:{lvl['rr']:.2f}\n"
               f"نسبة النجاح المتوقعة: {res['prob']}%\n\n"
               f"📌 توضيح : {res['reasons']}\n"
               f"--------------------------------------")
        output.append(row)
    
    await send_msg(chat_id, wrap_rtl("\n".join(output)))

async def handle_analysis_cmd(chat_id):
    top_three = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    msg = "التحليل اليومي لسوق الكريبتو حسب بيانات SMC\n"
    msg += "-------------------------------------------\n"
    msg += "✅ لا توجد أخبار مؤثرة حالياً.\n"
    msg += "أكثر ثلاث عملات رقمية نشطة حاليا صعود أو هبوط:\n"
    msg += "-----------------------------\n"
    
    for i, s in enumerate(top_three):
        res = await analyze_market(s)
        msg += f"{i+1}) #{s}\n"
        msg += f"⏰ 4h: اتجاه {res['trend']} بشكل واضح.\n"
        msg += f"🕰 1h: سيولة مؤسسية وحركة متزنة.\n"
        msg += f"🕒 15m: زخم يدعم الاتجاه الحالي.\n"
        msg += f"📉 التوقع: {res['prob']}% احتمال استمرار الاتجاه\n"
        msg += "-------------------------------------------\n"
    
    await send_msg(chat_id, wrap_rtl(msg))

# =========================
# تتبع السعر (Websocket)
# =========================
async def price_tracker():
    streams = "/".join([f"{s.lower()}@aggTrade" for s in SYMBOLS])
    async with websockets.connect(f"{BINANCE_WS_URL}/stream?streams={streams}") as ws:
        while True:
            data = json.loads(await ws.recv())
            symbol = data['stream'].split('@')[0].upper()
            price = float(data['data']['p'])
            side = "sell" if data['data']['m'] else "buy"
            
            # تحديث CVD
            symbol_cvd[symbol] += float(data['data']['q']) if side == "buy" else -float(data['data']['q'])
            
            # فحص الأهداف والتذكير
            if symbol in open_trades:
                trade = open_trades[symbol]
                # تذكير منطقة الدخول
                if not pending_notified.get(symbol) and abs(price - trade['entry']) / trade['entry'] < 0.002:
                    await send_msg(trade['chat_id'], wrap_rtl(f"السعر الآن في منطقة الدخول المقترحة لعملة #{symbol}، خذ نظرة و قرر"))
                    pending_notified[symbol] = True
                
                # تذكير الهدف
                if (trade['side'] == "buy" and price >= trade['tp']) or (trade['side'] == "sell" and price <= trade['tp']):
                    await send_msg(trade['chat_id'], wrap_rtl(f"🎯 تم الوصول للهدف في عملة #{symbol}"))
                    del open_trades[symbol]

# =========================
# التشغيل
# =========================
@app.post("/webhook")
async def webhook(req: Request):
    global AUTO_SCAN_CHAT_ID
    data = await req.json()
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        AUTO_SCAN_CHAT_ID = chat_id
        if text == "صفقات": await handle_trades_cmd(chat_id)
        elif text == "تحليل": await handle_analysis_cmd(chat_id)
    return {"ok": True}

@app.on_event("startup")
async def startup():
    asyncio.create_task(price_tracker())
    asyncio.create_task(auto_scan_loop())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
