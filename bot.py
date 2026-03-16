# bot.py
import os
import time
import math
import json
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional

import httpx
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# =========================
# إعدادات عامة من البيئة
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

BINANCE_REST_URL = "https://api.binance.com"
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"

CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY")

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","NEARUSDT","TRXUSDT","LTCUSDT","UNIUSDT",
    "ARBUSDT","OPUSDT","SUIUSDT","FILUSDT","STXUSDT",
    "APTUSDT","INJUSDT","SEIUSDT","PEPEUSDT","ORDIUSDT",
    "TAOUSDT","ENAUSDT","FTMUSDT","HBARUSDT","ARUSDT"
]

AUTO_TRADE_COOLDOWN_SECONDS = 60 * 60  # ساعة

app = FastAPI()

# =========================
# حالة البوت في الذاكرة
# =========================

last_auto_trade: Dict[str, float] = {}          # آخر صفقة آلية لكل عملة
pending_alert_sent: Dict[str, bool] = {}        # هل أُرسلت رسالة تذكير للصفقة المعلقة
symbol_orderflow_state: Dict[str, Dict[str, float]] = {
    s: {"cvd": 0.0, "last_price": 0.0} for s in SYMBOLS
}

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


async def fetch_24h_ticker(symbol: str) -> Dict[str, Any]:
    params = {"symbol": symbol}
    return await http_get(f"{BINANCE_REST_URL}/api/v3/ticker/24hr", params=params)


# =========================
# CryptoPanic أخبار بالعربية
# =========================

async def fetch_cryptopanic_news_ar() -> str:
    if not CRYPTOPANIC_API_KEY:
        return "لا توجد أخبار متاحة حالياً (لم يتم إعداد مفتاح CryptoPanic)."

    url = "https://cryptopanic.com/api/v1/posts/"
    params = {
        "auth_token": CRYPTOPANIC_API_KEY,
        "kind": "news",
        "public": "true",
        "filter": "hot"
    }
    try:
        data = await http_get(url, params=params)
        posts = data.get("results", [])[:3]
        if not posts:
            return "لا توجد أخبار مؤثرة حالياً، السوق يتحرك بشكل طبيعي مع ترقب حركة أوضح خلال الساعات القادمة."

        titles = [p.get("title", "") for p in posts]
        joined = " | ".join(titles)
        text_lower = joined.lower()

        if any(w in text_lower for w in ["rally", "surge", "bull", "up", "gain", "positive"]):
            outlook = "المزاج العام يميل للإيجابية مع احتمالية استمرار الزخم الصاعد خلال الساعات القادمة إذا لم تظهر أخبار سلبية مفاجئة."
        elif any(w in text_lower for w in ["dump", "crash", "bear", "down", "loss", "negative"]):
            outlook = "المزاج العام يميل للسلبية مع احتمالية استمرار الضغط البيعي خلال الساعات القادمة ما لم تظهر سيولة شرائية قوية."
        else:
            outlook = "الصورة الحالية محايدة نسبيًا مع ترقب حركة أوضح خلال الساعات القادمة حسب تفاعل السيولة مع الأخبار."

        summary = f"أهم ما في السوق حاليًا:\n- {titles[0]}\n"
        if len(titles) > 1:
            summary += f"- {titles[1]}\n"
        summary += f"\nالتوقع العام:\n{outlook}"
        return summary.strip()
    except Exception:
        return "حدث خطأ أثناء جلب الأخبار من CryptoPanic، سيتم الاعتماد على حركة السعر فقط في هذه الفترة."


# =========================
# أدوات تحليل: ATR + FVG + هيكل + زخم
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

        # FVG صاعد
        if low2 > high1 and low2 > high3:
            fvgs.append({
                "type": "bullish",
                "upper": low2,
                "lower": max(high1, high3)
            })
        # FVG هابط
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


# =========================
# اختيار أنشط العملات
# =========================

async def get_top_active_symbols(symbols: List[str], top_n: int = 3) -> List[str]:
    tickers = []
    for s in symbols:
        try:
            t = await fetch_24h_ticker(s)
            vol = float(t.get("quoteVolume", 0.0))
            tickers.append((s, vol))
        except Exception:
            continue
    tickers.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in tickers[:top_n]]


# =========================
# SL/TP + ATR ذكي
# =========================

def compute_institutional_levels(
    symbol: str,
    entry_price: float,
    atr: float,
    trend: str,
    momentum_score: float
) -> Dict[str, Any]:
    # ATR بين 1.7 و 1.8
    atr_mult = 1.7 if abs(momentum_score) < 5 else 1.8
    sl_distance = atr * atr_mult

    if trend == "bullish":
        sl = entry_price - sl_distance
    elif trend == "bearish":
        sl = entry_price + sl_distance
    else:
        sl = entry_price - sl_distance if atr > 0 else entry_price * 0.99

    # R:R بين 2.5 و 7 حسب قوة الاتجاه/الزخم
    base_rr = 2.5
    if abs(momentum_score) > 10:
        base_rr = 3.5
    if abs(momentum_score) > 20:
        base_rr = 4.5
    if abs(momentum_score) > 30:
        base_rr = 6.0
    rr = max(2.5, min(7.0, base_rr))

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
# منع تكرار نفس العملة خلال ساعة
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
# بناء الرسائل بالشكل المطلوب
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
    type_text = "معلّق" if is_pending else "فوري"

    msg = ""
    if rank == 1:
        msg += "أفضل صفقتين في السوق حاليا:\n"
    msg += "-------------------------------------------\n"
    msg += f"{medal} {symbol} — {color} ({type_text})\n"
    msg += f"{side_tag}\n"
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
    type_text = "معلّق" if is_pending else "فوري"

    msg = "⏰ فحص آلي — فرصة جديدة\n"
    msg += "------------------------------------------\n"
    msg += f"🎯 {symbol} — {color} ({type_text})\n"
    msg += f"{side_tag}\n"
    msg += f"Entry: {levels['entry']:.4f}\n"
    msg += f"SL: {levels['sl']:.4f}\n"
    msg += f"TP: {levels['tp']:.4f}\n"
    msg += f"R:R = 1:{levels['rr']:.2f}\n"
    msg += f"نسبة النجاح المتوقعة: {prob:.0f}%\n"
    msg += "------------------------------------------------\n"
    msg += f"📌 السبب: {reason_text}\n"
    return msg


def build_pending_reminder_message(symbol: str, mode: str = "آلي") -> str:
    return (
        f"تأكيد الصفقة المعلقة ({mode})\n"
        f"#{symbol}\n\n"
        "السعر وصل منطقة الدخول المقترحة، خذ نظرة و قرر."
    )


def build_analysis_message_full(
    news: str,
    analyses: List[Dict[str, Any]]
) -> str:
    msg = "التحليل اليومي لسوق الكريبتو حسب بيانات السوق والأخبار الواردة من موقع CryptoPanic\n"
    msg += "-------------------------------------------\n"
    if "لا توجد أخبار" in news or "خطأ" in news:
        msg += "لا توجد أخبار مؤثرة حالياً.\n"
    else:
        msg += news + "\n"
    msg += "أكثر ثلاث عملات رقمية نشطة حاليا صعود أو هبوط حسب اتجاه السوق:\n"
    msg += "-----------------------------\n"

    for idx, r in enumerate(analyses, start=1):
        symbol = r["symbol"]
        trend = r["trend"]
        prob = r["prob"]
        momentum = r["momentum_score"]
        fvg_bias = r["fvg_bias"]

        if trend == "bullish":
            t4h = "اتجاه صاعد بشكل واضح مع قمم وقيعان أعلى على فريم 4 ساعات."
        elif trend == "bearish":
            t4h = "اتجاه هابط بشكل واضح مع قمم وقيعان أدنى على فريم 4 ساعات."
        else:
            t4h = "حركة متذبذبة على فريم 4 ساعات بدون اتجاه واضح."

        if fvg_bias == "bullish":
            t1h = "اتجاه صاعد بإتجاه فجوة سعرية صاعدة (FVG) على فريم الساعة قد تُستهدف قريباً."
        elif fvg_bias == "bearish":
            t1h = "اتجاه هابط بإتجاه فجوة سعرية هابطة (FVG) على فريم الساعة قد تُستهدف قريباً."
        else:
            t1h = "حركة متوازنة على فريم الساعة مع مراقبة لمناطق فجوات سعرية محتملة."

        if momentum > 0:
            t15m = "اتجاه صاعد على فريم 15 دقيقة بعد دخول سيولة شرائية ملحوظة (زخم إيجابي)."
        elif momentum < 0:
            t15m = "اتجاه هابط على فريم 15 دقيقة مع ضغط بيعي واضح (زخم سلبي)."
        else:
            t15m = "زخم متوازن على فريم 15 دقيقة بدون اندفاع واضح."

        msg += f"{idx}) #{symbol}\n"
        msg += f"⏰ 4h: {t4h}\n"
        msg += f"🕰 1h: {t1h}\n"
        msg += f"🕒 15m: {t15m}\n"
        msg += f"📉 التوقع: {prob:.0f}% باتجاه السيناريو الغالب خلال الساعات القادمة\n"
        msg += "-------------------------------------------\n"

    return msg


# =========================
# تحليل عملة واحدة
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

    return {
        "symbol": symbol,
        "trend": trend,
        "fvg_bias": fvg_bias,
        "momentum_score": momentum_score,
        "prob": prob,
        "last_price": last_close,
        "atr_1h": atr_1h
    }


# =========================
# WebSocket: CVD مبسط
# =========================

async def run_binance_ws():
    """
    WebSocket واحد يجمع AggTrades لكل العملات ويحدّث CVD مبسط.
    """
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

                    # إذا المشتري هو الـ maker → الصفقة بيعية (ضغط بيع)
                    # إذا المشتري ليس maker → الصفقة شرائية (ضغط شراء)
                    delta = qty if not is_buyer_maker else -qty

                    symbol_orderflow_state[s]["cvd"] += delta
                    symbol_orderflow_state[s]["last_price"] = price
        except Exception:
            await asyncio.sleep(5)


# =========================
# منطق زر "تحليل"
# =========================

async def handle_analysis(chat_id: int):
    news = await fetch_cryptopanic_news_ar()
    top_symbols = await get_top_active_symbols(SYMBOLS, top_n=3)

    analyses = []
    for s in top_symbols:
        try:
            res = await analyze_symbol(s)
            analyses.append(res)
        except Exception:
            continue

    if not analyses:
        await send_telegram_message(chat_id, "لم أتمكن من إجراء تحليل موثوق حالياً، يرجى المحاولة لاحقاً.", reply_markup=main_menu_keyboard())
        return

    msg = build_analysis_message_full(news, analyses)
    await send_telegram_message(chat_id, msg, reply_markup=main_menu_keyboard())


# =========================
# منطق زر "صفقات"
# =========================

async def handle_trades(chat_id: int):
    news = await fetch_cryptopanic_news_ar()
    top_symbols = await get_top_active_symbols(SYMBOLS, top_n=8)

    analyses = []
    for s in top_symbols:
        try:
            res = await analyze_symbol(s)
            analyses.append(res)
        except Exception:
            continue

    if not analyses:
        await send_telegram_message(chat_id, "لا توجد حالياً بيانات كافية لاستخراج صفقات موثوقة.", reply_markup=main_menu_keyboard())
        return

    # ترتيب حسب قوة الاحتمال (بعيد عن 50)
    analyses.sort(key=lambda x: abs(x["prob"] - 50), reverse=True)

    manual_msgs = []
    auto_msgs = []
    used_symbols = set()
    manual_count = 0
    auto_count = 0

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

        # منطق الصفقة المعلقة (انحراف 1%)
        is_pending = should_place_pending_order(
            current_price=entry_price,
            target_price=entry_price,
            max_deviation=0.01
        )

        # سبب الدخول (مختصر وواضح)
        reason_parts = []
        if trend == "bullish":
            reason_parts.append("اتجاه صاعد على الفريمات الكبيرة مع قمم وقيعان أعلى بشكل واضح.")
        else:
            reason_parts.append("اتجاه هابط على الفريمات الكبيرة مع قمم وقيعان أدنى بشكل واضح.")

        if r["fvg_bias"] == "bullish" and trend == "bullish":
            reason_parts.append("وجود فجوات سعرية صاعدة (FVG) على فريم 4 ساعات والساعة تدعم استمرار الحركة.")
        if r["fvg_bias"] == "bearish" and trend == "bearish":
            reason_parts.append("وجود فجوات سعرية هابطة (FVG) على فريم 4 ساعات والساعة تدعم استمرار الحركة.")

        if r["momentum_score"] > 0 and trend == "bullish":
            reason_parts.append("زخم إيجابي على فريم 15 دقيقة مع شموع اندفاعية في اتجاه الصفقة.")
        if r["momentum_score"] < 0 and trend == "bearish":
            reason_parts.append("زخم سلبي على فريم 15 دقيقة مع ضغط بيعي واضح في اتجاه الصفقة.")

        cvd_val = symbol_orderflow_state.get(symbol, {}).get("cvd", 0.0)
        if cvd_val > 0 and trend == "bullish":
            reason_parts.append("تدفق السيولة (CVD) يميل للشراء مما يدعم سيناريو الاستمرار.")
        if cvd_val < 0 and trend == "bearish":
            reason_parts.append("تدفق السيولة (CVD) يميل للبيع مما يدعم سيناريو الاستمرار.")

        reason_text = " ".join(reason_parts) if reason_parts else "توافق منطقي بين الاتجاه العام والزخم الحالي ومناطق السعر المهمة."

        # صفقتان يدويتان (أفضل صفقتين في السوق حالياً)
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
            continue

        # صفقة آلية واحدة فقط لكل عملة خلال ساعة
        if auto_count < 2 and can_open_auto_trade(symbol):
            auto_msg = build_auto_trade_message(
                symbol=symbol,
                side=side,
                levels=levels,
                prob=r["prob"],
                reason_text=reason_text,
                is_pending=is_pending
            )
            auto_msgs.append(auto_msg)
            mark_auto_trade(symbol)
            auto_count += 1
            used_symbols.add(symbol)

    if not manual_msgs and not auto_msgs:
        await send_telegram_message(chat_id, "لا توجد حالياً صفقات تلقائية تتوافق مع شروط الدخول المؤسسية بدون مبالغة أو تضارب.", reply_markup=main_menu_keyboard())
        return

    # إرسال الصفقات اليدوية (أفضل صفقتين)
    if manual_msgs:
        full_manual = "\n".join(manual_msgs)
        await send_telegram_message(chat_id, full_manual, reply_markup=main_menu_keyboard())

    # إرسال صفقة/صفقتين آليتين (بصيغة الفحص الآلي)
    for am in auto_msgs:
        await send_telegram_message(chat_id, am, reply_markup=main_menu_keyboard())


# =========================
# Webhook تليجرام
# =========================

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    if "message" not in data:
        return JSONResponse({"ok": True})

    message = data["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if text == "/start":
        await send_telegram_message(
            chat_id,
            "مرحباً، هذا البوت مبني على تحليل مؤسسي حقيقي.\n\nاختر من الأزرار:\n- تحليل\n- صفقات",
            reply_markup=main_menu_keyboard()
        )
    elif text == "تحليل":
        await handle_analysis(chat_id)
    elif text == "صفقات":
        await handle_trades(chat_id)
    else:
        await send_telegram_message(
            chat_id,
            "اختر من الأزرار:\n- تحليل\n- صفقات",
            reply_markup=main_menu_keyboard()
        )

    return JSONResponse({"ok": True})


# =========================
# تشغيل WebSocket في الخلفية
# =========================

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_binance_ws())


# =========================
# نقطة فحص بسيطة
# =========================

@app.get("/")
async def root():
    return {"status": "ok", "message": "Institutional Telegram bot running with webhook + websockets."}

# تشغيل FastAPI على البورت الصحيح من fly.io
if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8080))
    )
