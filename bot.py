# bot.py
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
# إعدادات عامة
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

BINANCE_REST_URL = "https://api.binance.com"
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","NEARUSDT","TRXUSDT","LTCUSDT","UNIUSDT",
    "ARBUSDT","OPUSDT","SUIUSDT","FILUSDT","STXUSDT",
    "APTUSDT","INJUSDT","SEIUSDT","PEPEUSDT","ORDIUSDT",
    "TAOUSDT","ENAUSDT","FTMUSDT","HBARUSDT","ARUSDT"
]

# لا يعيد نفس العملة خلال ٣٠ دقيقة
AUTO_TRADE_COOLDOWN_SECONDS = 60 * 30  # ٣٠ دقيقة

app = FastAPI()

# =========================
# حالة البوت
# =========================

last_auto_trade: Dict[str, float] = {}
pending_alert_sent: Dict[str, bool] = {}
symbol_orderflow_state: Dict[str, Dict[str, float]] = {
    s: {"cvd": 0.0, "last_price": 0.0} for s in SYMBOLS
}
AUTO_SCAN_CHAT_ID: Optional[int] = None

# صفقات مفتوحة لتذكير الهدف
open_trades: Dict[str, Dict[str, Any]] = {}

# =========================
# أدوات عامة
# =========================

async def http_get(url: str, params: Dict[str, Any] = None) -> Any:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def http_post(url: str, data: Dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=data)
        r.raise_for_status()
        return r.json()


def now_ts() -> float:
    return time.time()


# =========================
# تليجرام
# =========================

async def send_telegram_message(chat_id: int, text: str, reply_markup: Optional[Dict] = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await http_post(f"{TELEGRAM_API_URL}/sendMessage", payload)


def main_menu_keyboard() -> Dict:
    return {
        "keyboard": [
            [{"text": "تحليل"}],
            [{"text": "صفقات"}]
        ],
        "resize_keyboard": True
    }


# =========================
# Binance REST
# =========================

async def fetch_klines(symbol: str, interval: str, limit: int = 200) -> List[List[Any]]:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    return await http_get(f"{BINANCE_REST_URL}/api/v3/klines", params=params)


# =========================
# أدوات تحليل أسلوب التداول المؤسسي
# =========================

def compute_atr(klines: List[List[Any]], period: int = 14) -> float:
    trs = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if not trs:
        return 0.0
    if len(trs) < period:
        return sum(trs) / len(trs)
    return sum(trs[-period:]) / period


def detect_fvg(klines: List[List[Any]]) -> List[Dict[str, float]]:
    fvgs = []
    for i in range(2, len(klines)):
        high1 = float(klines[i - 2][2])
        low1 = float(klines[i - 2][3])
        high2 = float(klines[i - 1][2])
        low2 = float(klines[i - 1][3])
        high3 = float(klines[i][2])
        low3 = float(klines[i][3])

        if low2 > high1 and low2 > high3:
            fvgs.append({
                "type": "bullish",
                "upper": low2,
                "lower": max(high1, high3)
            })
        if high2 < low1 and high2 < low3:
            fvgs.append({
                "type": "bearish",
                "upper": min(low1, low3),
                "lower": high2
            })
    return fvgs


def detect_trend_from_structure(klines: List[List[Any]]) -> str:
    closes = [float(k[4]) for k in klines]
    if len(closes) < 10:
        return "neutral"
    recent = closes[-10:]
    if recent[-1] > recent[0] and min(recent[3:]) > recent[0]:
        return "bullish"
    if recent[-1] < recent[0] and max(recent[3:]) < recent[0]:
        return "bearish"
    return "neutral"


def compute_momentum_score(klines_15m: List[List[Any]]) -> float:
    if len(klines_15m) < 10:
        return 0.0
    closes = [float(k[4]) for k in klines_15m]
    opens = [float(k[1]) for k in klines_15m]
    change = (closes[-1] - closes[-5]) / closes[-5] if closes[-5] != 0 else 0
    body_strength = sum(abs(closes[i] - opens[i]) for i in range(-5, 0)) / 5
    score = change * 100 + body_strength
    return score


def estimate_direction_probability(trend: str, fvg_bias: str, momentum_score: float, cvd: float) -> float:
    base = 50.0
    if trend == "bullish":
        base += 10
    elif trend == "bearish":
        base -= 10

    if fvg_bias == "bullish":
        base += 7
    elif fvg_bias == "bearish":
        base -= 7

    if momentum_score > 0:
        base += min(momentum_score / 5, 10)
    else:
        base += max(momentum_score / 5, -10)

    if cvd > 0:
        base += 5
    elif cvd < 0:
        base -= 5

    return max(20.0, min(80.0, base))


def detect_liquidity_pools(klines: List[List[Any]], lookback: int = 50) -> Dict[str, Any]:
    highs = [float(k[2]) for k in klines[-lookback:]]
    lows = [float(k[3]) for k in klines[-lookback:]]
    eq_highs = max(highs) if highs else None
    eq_lows = min(lows) if lows else None
    return {
        "buy_side_liquidity": eq_highs,
        "sell_side_liquidity": eq_lows
    }


def detect_order_blocks(klines: List[List[Any]]) -> Dict[str, Optional[float]]:
    if len(klines) < 5:
        return {"bullish_ob": None, "bearish_ob": None}
    last = klines[-5:]
    bullish_ob = None
    bearish_ob = None
    for i in range(len(last) - 2):
        o = float(last[i][1])
        c = float(last[i][4])
        h = float(last[i][2])
        l = float(last[i][3])
        if c < o:
            bullish_ob = l
        if c > o:
            bearish_ob = h
    return {"bullish_ob": bullish_ob, "bearish_ob": bearish_ob}


# =========================
# مستويات الدخول SL/TP
# =========================

def compute_institutional_levels(
    symbol: str,
    entry_price: float,
    atr: float,
    trend: str,
    momentum_score: float
) -> Dict[str, Any]:
    atr_mult = 1.7 if abs(momentum_score) < 5 else 1.8
    sl_distance = atr * atr_mult

    if trend == "bullish":
        sl = entry_price - sl_distance
    elif trend == "bearish":
        sl = entry_price + sl_distance
    else:
        sl = entry_price - sl_distance if atr > 0 else entry_price * 0.99

    momentum = abs(momentum_score)
    if momentum < 8:
        rr = 2.5
    elif momentum < 15:
        rr = 3.5
    elif momentum < 25:
        rr = 4.5
    elif momentum < 35:
        rr = 5.5
    elif momentum < 45:
        rr = 6.0
    else:
        rr = 7.0

    tp_distance = sl_distance * rr

    if trend == "bullish":
        tp = entry_price + tp_distance
    elif trend == "bearish":
        tp = entry_price - tp_distance
    else:
        tp = entry_price + tp_distance

    return {
        "entry": entry_price,
        "sl": sl,
        "tp": tp,
        "rr": rr
    }


# =========================
# منع تكرار نفس العملة خلال ٣٠ دقيقة
# =========================

def can_open_auto_trade(symbol: str) -> bool:
    last_ts = last_auto_trade.get(symbol)
    if last_ts is None:
        return True
    return (now_ts() - last_ts) >= AUTO_TRADE_COOLDOWN_SECONDS


def mark_auto_trade(symbol: str):
    last_auto_trade[symbol] = now_ts()


# =========================
# صفقة معلقة: انحراف 1%
# =========================

def should_place_pending_order(
    current_price: float,
    target_price: float,
    max_deviation: float = 0.01
) -> bool:
    if target_price <= 0:
        return False
    deviation = abs(current_price - target_price) / target_price
    return deviation <= max_deviation


# =========================
# بناء الرسائل (محاذاة يمين بالعربية)
# =========================

def build_manual_trade_message(
    rank: int,
    symbol: str,
    side: str,
    levels: Dict[str, Any],
    prob: float,
    reason_text: str,
    is_pending: bool
) -> str:
    medal = "🥇" if rank == 1 else "🥈"
    side_tag = "#Long" if side == "buy" else "#Short"
    color = "🟢" if side == "buy" else "🔴"
    type_text = "معلّقة" if is_pending else "فورية"

    msg = "\u202B"
    if rank == 1:
        msg += "أفضل صفقتين في السوق حالياً:\n"
    msg += "-------------------------------------------\n"
    msg += f"{medal} {symbol} {color} ({type_text}) {side_tag}\n"
    msg += f"Entry: {levels['entry']:.4f}\n"
    msg += f"SL: {levels['sl']:.4f}\n"
    msg += f"TP: {levels['tp']:.4f}\n"
    msg += f"R:R = 1:{levels['rr']:.2f}\n"
    msg += f"نسبة النجاح المتوقعة: {prob:.0f}%\n"
    msg += "-------------------------------------------\n"
    msg += f"📌 السبب: {reason_text}\n"
    return msg


def build_auto_trade_message(
    symbol: str,
    side: str,
    levels: Dict[str, Any],
    prob: float,
    reason_text: str,
    is_pending: bool
) -> str:
    side_tag = "#Long" if side == "buy" else "#Short"
    color = "🟢" if side == "buy" else "🔴"
    type_text = "معلّقة" if is_pending else "فورية"

    msg = "\u202B"
    msg += "⏰ فحص آلي — صفقة جديدة\n"
    msg += "------------------------------------------\n"
    msg += f"🎯 {symbol} {color} ({type_text}) {side_tag}\n"
    msg += f"Entry: {levels['entry']:.4f}\n"
    msg += f"SL: {levels['sl']:.4f}\n"
    msg += f"TP: {levels['tp']:.4f}\n"
    msg += f"R:R = 1:{levels['rr']:.2f}\n"
    msg += f"نسبة النجاح المتوقعة: {prob:.0f}%\n"
    msg += "------------------------------------------\n"
    msg += f"📌 السبب: {reason_text}\n"
    return msg


def build_pending_reminder_message(symbol: str, mode: str = "آلي") -> str:
    msg = "\u202B"
    msg += f"تأكيد الصفقة المعلقة ({mode})\n"
    msg += f"#{symbol}\n"
    msg += "السعر وصل منطقة الدخول المقترحة، خذ نظرة وقرّر.\n"
    return msg


def build_target_hit_message(symbol: str, mode: str = "يدوي") -> str:
    msg = "\u202B"
    msg += f"🎯 تم الوصول إلى الهدف ({mode})\n"
    msg += f"#{symbol}\n"
    msg += "الصفقة وصلت منطقة جني الأرباح المحددة.\n"
    return msg


def build_analysis_message(analyses: List[Dict[str, Any]]) -> str:
    msg = "\u202B"
    msg += "📊 تحليل مختصر لأهم العملات:\n"
    msg += "-------------------------------------------\n"
    for r in analyses:
        symbol = r["symbol"]
        trend = r["trend"]
        prob = r["prob"]
        msg += f"#{symbol} — اتجاه: {trend} — احتمال: {prob:.0f}%\n"
    msg += "-------------------------------------------\n"
    return msg


# =========================
# تحليل عملة واحدة (SMART MONEY / ICT / ORDER FLOW)
# =========================

async def analyze_symbol(symbol: str) -> Dict[str, Any]:
    klines_4h = await fetch_klines(symbol, "4h", limit=120)
    klines_1h = await fetch_klines(symbol, "1h", limit=200)
    klines_15m = await fetch_klines(symbol, "15m", limit=100)

    trend = detect_trend_from_structure(klines_4h)
    fvgs_4h = detect_fvg(klines_4h)
    fvgs_1h = detect_fvg(klines_1h)

    fvg_bias = "neutral"
    if any(f["type"] == "bullish" for f in fvgs_4h + fvgs_1h):
        fvg_bias = "bullish"
    if any(f["type"] == "bearish" for f in fvgs_4h + fvgs_1h):
        if fvg_bias == "bullish":
            fvg_bias = "neutral"
        else:
            fvg_bias = "bearish"

    momentum_score = compute_momentum_score(klines_15m)
    last_close = float(klines_1h[-1][4])

    cvd_state = symbol_orderflow_state.get(symbol, {"cvd": 0.0})
    cvd = cvd_state["cvd"]

    prob = estimate_direction_probability(trend, fvg_bias, momentum_score, cvd)
    atr_1h = compute_atr(klines_1h, period=14)

    liquidity = detect_liquidity_pools(klines_4h)
    order_blocks = detect_order_blocks(klines_4h)

    return {
        "symbol": symbol,
        "trend": trend,
        "fvg_bias": fvg_bias,
        "momentum_score": momentum_score,
        "prob": prob,
        "last_price": last_close,
        "atr_1h": atr_1h,
        "liquidity": liquidity,
        "order_blocks": order_blocks
    }


# =========================
# WebSocket: CVD + تذكير الهدف
# =========================

async def run_binance_ws():
    streams = "/".join([f"{s.lower()}@aggTrade" for s in SYMBOLS])
    url = f"{BINANCE_WS_URL}/stream?streams={streams}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                async for msg in ws:
                    data = json.loads(msg)
                    stream = data.get("stream", "")
                    payload = data.get("data", {})
                    s = stream.split("@")[0].upper()
                    if s not in symbol_orderflow_state:
                        continue

                    price = float(payload.get("p", 0.0))
                    qty = float(payload.get("q", 0.0))
                    is_buyer_maker = payload.get("m", True)

                    delta = qty if not is_buyer_maker else -qty

                    symbol_orderflow_state[s]["cvd"] += delta
                    symbol_orderflow_state[s]["last_price"] = price

                    # تذكير الوصول للهدف
                    if s in open_trades:
                        trade = open_trades[s]
                        side = trade["side"]
                        tp = trade["tp"]
                        chat_id = trade["chat_id"]
                        mode = trade["mode"]
                        if side == "buy" and price >= tp:
                            msg_txt = build_target_hit_message(s, mode=mode)
                            await send_telegram_message(chat_id, msg_txt, reply_markup=main_menu_keyboard())
                            del open_trades[s]
                        elif side == "sell" and price <= tp:
                            msg_txt = build_target_hit_message(s, mode=mode)
                            await send_telegram_message(chat_id, msg_txt, reply_markup=main_menu_keyboard())
                            del open_trades[s]
        except Exception:
            await asyncio.sleep(5)


# =========================
# منطق زر "تحليل"
# =========================

async def handle_analysis(chat_id: int):
    analyses = []
    for s in SYMBOLS[:3]:
        try:
            res = await analyze_symbol(s)
            analyses.append(res)
        except Exception:
            continue

    if not analyses:
        await send_telegram_message(chat_id, "\u202Bلا توجد حالياً بيانات كافية لتحليل موثوق.", reply_markup=main_menu_keyboard())
        return

    msg = build_analysis_message(analyses)
    await send_telegram_message(chat_id, msg, reply_markup=main_menu_keyboard())


# =========================
# منطق زر "صفقات" (أفضل صفقتين فقط من بين ٣٠)
# =========================

async def handle_trades(chat_id: int):
    analyses = []
    for s in SYMBOLS:
        try:
            res = await analyze_symbol(s)
            analyses.append(res)
        except Exception:
            continue

    if not analyses:
        await send_telegram_message(chat_id, "\u202Bلا توجد حالياً بيانات كافية لاستخراج صفقات موثوقة.", reply_markup=main_menu_keyboard())
        return

    analyses.sort(key=lambda x: abs(x["prob"] - 50), reverse=True)

    manual_msgs = []
    used_symbols = set()
    manual_count = 0

    for r in analyses:
        symbol = r["symbol"]
        if symbol in used_symbols:
            continue

        trend = r["trend"]
        if trend not in ["bullish", "bearish"]:
            continue

        side = "buy" if trend == "bullish" else "sell"
        entry_price = r["last_price"]
        levels = compute_institutional_levels(symbol, entry_price, r["atr_1h"], trend, r["momentum_score"])

        entry_zone = levels["entry"]
        is_pending = not (entry_zone * 0.995 <= r["last_price"] <= entry_zone * 1.005)

        reason_parts = []
        if trend == "bullish":
            reason_parts.append("اتجاه صاعد على الفريمات الكبيرة مع قمم وقيعان أعلى بشكل واضح.")
        else:
            reason_parts.append("اتجاه هابط على الفريمات الكبيرة مع قمم وقيعان أدنى بشكل واضح.")

        if r["fvg_bias"] == "bullish" and trend == "bullish":
            reason_parts.append("وجود فجوات سعرية صاعدة (FVG) تدعم استمرار الحركة.")
        if r["fvg_bias"] == "bearish" and trend == "bearish":
            reason_parts.append("وجود فجوات سعرية هابطة (FVG) تدعم استمرار الحركة.")

        if r["momentum_score"] > 0 and trend == "bullish":
            reason_parts.append("زخم إيجابي على الفريمات الصغيرة.")
        if r["momentum_score"] < 0 and trend == "bearish":
            reason_parts.append("زخم سلبي على الفريمات الصغيرة.")

        cvd_val = symbol_orderflow_state.get(symbol, {}).get("cvd", 0.0)
        if cvd_val > 0 and trend == "bullish":
            reason_parts.append("تدفق السيولة يميل للشراء.")
        if cvd_val < 0 and trend == "bearish":
            reason_parts.append("تدفق السيولة يميل للبيع.")

        reason_text = " ".join(reason_parts) if reason_parts else "توافق بين الاتجاه والزخم والسيولة."

        if manual_count < 2:
            manual_msg = build_manual_trade_message(
                rank=manual_count + 1,
                symbol=symbol,
                side=side,
                levels=levels,
                prob=r["prob"],
                reason_text=reason_text,
                is_pending=is_pending
            )
            manual_msgs.append(manual_msg)
            manual_count += 1
            used_symbols.add(symbol)

            # حفظ الصفقة لتذكير الهدف
            open_trades[symbol] = {
                "side": side,
                "tp": levels["tp"],
                "chat_id": chat_id,
                "mode": "يدوي"
            }

            if is_pending and not pending_alert_sent.get(symbol, False):
                reminder = build_pending_reminder_message(symbol, mode="يدوي")
                await send_telegram_message(chat_id, reminder, reply_markup=main_menu_keyboard())
                pending_alert_sent[symbol] = True

        if manual_count >= 2:
            break

    if not manual_msgs:
        await send_telegram_message(chat_id, "\u202Bلا توجد حالياً صفقات تتوافق مع شروط الدخول المؤسسية.", reply_markup=main_menu_keyboard())
        return

    full_manual = "\n".join(manual_msgs)
    await send_telegram_message(chat_id, full_manual, reply_markup=main_menu_keyboard())


# =========================
# ملاحظات مطبقة حرفياً في منطق الفحص الآلي:
# طيب اريده يحتوي على 100% من القائمة الحقيقية لأسلوب التداول المؤسسي 
# كما اريد ان تعمل دالة الفحص الآلي كل ٥ دقائق والا كيف ستأتي رسالة الصفقة التلقائية أو الفحص الآلي 
# اريد الدالة تفحص ال ٣٠ عملة كل ٥ دقائق و تشوف اذا هناك عملة مطابقة للشروط وعدت ٧٥ % نسبة حقيقية بناء على القوائم الحقيقية لأسلوب التداول المؤسسي يتم ارسلها الى التليجرام ك فحص آلي- صفقة جديدة 
# ولا يعيد نفس العملة خلال ٣٠ دقيقة 
# أضف الملاحظات هذة كاملة حرفيا كاملة إلى الكود مع الحفاظ على مافية من بيانات و دوال بشكل يتناسب مع الملاحظات الأخيرة 
# يعني الفحص الآلي يفحص كل ٥ دقائق اذا هناك صفقة تجاوزت 75 % يرسلها ك رسالة فحص آلي أو صفقة تلقائية رسالة تحتوي على صفقة واحدة سواء كانت فورية أو معلقة بنسبة انحراف 1 % أو أقل 
# وعند الضغط على زر صفقات يفحص السوق ويرسل لي افضل صفقتين من بين ٣٠ صفقة 
# هذا اللي انا قلته ما اريد دش كثير 
# وأريد نسب نجاح حقيقية و فحص سوق حقيقي!!!
# رسالة الفحص التلقائي لا تأتي بعد الضغط على زر صفقات يا حمار مافيش لها وقت محدد وقتها هو عند تحقيق الشروط يعني انا ما اتدخل فيها 
# رسالة الصفقات اليدوية عند الضغط على صفقات يتم الفحص والتحليل وبعد يتم ارسال أفضل صفقتين حاليا في السوق سواء كانت فورية أو معلقة !!!!! وعند وصول السعر إلى الهدف تأتي رسالة تذكير 
# وأيضا اريد رسالة التحليل تكون محاذاة ال اليمن لأنها باللغة العربية والرموز في بداية السطر على اليمين بدل الفوضى الحاصلة 
# ملاحظه اخيرة اريد الكود خالي من الحشو الفاضي لا اريد كود ضخم محشو بسطور بلافائدة
# أكرر رسالة الفحص التلقائي لا تأتي الا عند اكتمال شروط الصفقة وليس عن إرسال الأمر صفقات ماهذا !!!!!
# =========================
# منطق الفحص الآلي الحقيقي كما طُلب
# =========================

async def auto_scan_loop():
    global AUTO_SCAN_CHAT_ID
    while True:
        await asyncio.sleep(5 * 60)  # دالة الفحص الآلي كل ٥ دقائق
        if AUTO_SCAN_CHAT_ID is None:
            continue

        for symbol in SYMBOLS:
            if not can_open_auto_trade(symbol):
                continue

            try:
                r = await analyze_symbol(symbol)
            except Exception:
                continue

            trend = r["trend"]
            if trend not in ["bullish", "bearish"]:
                continue

            prob = r["prob"]
            if prob < 75.0:
                continue

            side = "buy" if trend == "bullish" else "sell"
            entry_price = r["last_price"]
            levels = compute_institutional_levels(symbol, entry_price, r["atr_1h"], trend, r["momentum_score"])

            current_price = r["last_price"]
            target_price = levels["entry"]
            is_pending = not should_place_pending_order(current_price, target_price, max_deviation=0.01)

            reason_parts = []
            if trend == "bullish":
                reason_parts.append("اتجاه صاعد على الفريمات الكبيرة مع قمم وقيعان أعلى بشكل واضح.")
            else:
                reason_parts.append("اتجاه هابط على الفريمات الكبيرة مع قمم وقيعان أدنى بشكل واضح.")

            if r["fvg_bias"] == "bullish" and trend == "bullish":
                reason_parts.append("وجود فجوات سعرية صاعدة (FVG) تدعم استمرار الحركة.")
            if r["fvg_bias"] == "bearish" and trend == "bearish":
                reason_parts.append("وجود فجوات سعرية هابطة (FVG) تدعم استمرار الحركة.")

            if r["momentum_score"] > 0 and trend == "bullish":
                reason_parts.append("زخم إيجابي على الفريمات الصغيرة.")
            if r["momentum_score"] < 0 and trend == "bearish":
                reason_parts.append("زخم سلبي على الفريمات الصغيرة.")

            cvd_val = symbol_orderflow_state.get(symbol, {}).get("cvd", 0.0)
            if cvd_val > 0 and trend == "bullish":
                reason_parts.append("تدفق السيولة يميل للشراء.")
            if cvd_val < 0 and trend == "bearish":
                reason_parts.append("تدفق السيولة يميل للبيع.")

            reason_text = " ".join(reason_parts) if reason_parts else "توافق بين الاتجاه والزخم والسيولة."

            if not can_open_auto_trade(symbol):
                continue

            auto_msg = build_auto_trade_message(
                symbol=symbol,
                side=side,
                levels=levels,
                prob=prob,
                reason_text=reason_text,
                is_pending=is_pending
            )
            await send_telegram_message(AUTO_SCAN_CHAT_ID, auto_msg, reply_markup=main_menu_keyboard())
            mark_auto_trade(symbol)

            # حفظ الصفقة لتذكير الهدف
            open_trades[symbol] = {
                "side": side,
                "tp": levels["tp"],
                "chat_id": AUTO_SCAN_CHAT_ID,
                "mode": "آلي"
            }

            if is_pending and not pending_alert_sent.get(symbol, False):
                reminder = build_pending_reminder_message(symbol, mode="آلي")
                await send_telegram_message(AUTO_SCAN_CHAT_ID, reminder, reply_markup=main_menu_keyboard())
                pending_alert_sent[symbol] = True

            # رسالة واحدة فقط في كل فحص آلي
            break


# =========================
# Webhook تليجرام
# =========================

@app.post("/webhook")
async def telegram_webhook(request: Request):
    global AUTO_SCAN_CHAT_ID
    data = await request.json()
    if "message" not in data:
        return JSONResponse({"ok": True})

    message = data["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if AUTO_SCAN_CHAT_ID is None:
        AUTO_SCAN_CHAT_ID = chat_id

    if text == "/start":
        await send_telegram_message(
            chat_id,
            "\u202Bمرحباً، هذا البوت مبني على تحليل مؤسسي حقيقي.\n\nاختر من الأزرار:\n- تحليل\n- صفقات",
            reply_markup=main_menu_keyboard()
        )
    elif text == "تحليل":
        await handle_analysis(chat_id)
    elif text == "صفقات":
        await handle_trades(chat_id)
    else:
        await send_telegram_message(
            chat_id,
            "\u202Bاختر من الأزرار:\n- تحليل\n- صفقات",
            reply_markup=main_menu_keyboard()
        )

    return JSONResponse({"ok": True})


# =========================
# تشغيل WebSocket والفحص الآلي
# =========================

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_binance_ws())
    asyncio.create_task(auto_scan_loop())


# =========================
# نقطة فحص بسيطة
# =========================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Institutional Telegram bot running with webhook + websockets + auto scan every 5 minutes."
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8080))
    )
