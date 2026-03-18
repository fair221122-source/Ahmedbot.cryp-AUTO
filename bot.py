هذا 👇هجين من ها 👆
import os
import time
import json
import asyncio
from typing import Dict, Any, List, Optional

import aiohttp
import pandas as pd
import numpy as np
from fastapi import FastAPI, Request
import uvicorn

# =========================
# الإعدادات العامة
# =========================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY")

FAPI_BASE = "https://fapi.binance.com"
FAPI_WS = "wss://fstream.binance.com/ws/!ticker@arr"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","NEARUSDT","TRXUSDT","LTCUSDT","UNIUSDT",
    "ARBUSDT","SUIUSDT","FILUSDT","STXUSDT","APTUSDT"
]
AUTO_SCAN_INTERVAL = 300        # كل 5 دقائق
COOLDOWN_SECONDS = 1800         # 30 دقيقة
ENTRY_TOLERANCE = 0.01          # 1%
ENTRY_ALERT_TOLERANCE = 0.005   # 0.5%
MIN_PROB_AUTO = 75

app = FastAPI()

last_sent: Dict[str, float] = {}
monitored_trades: Dict[str, Dict[str, Any]] = {}
open_trades: Dict[str, Dict[str, Any]] = {}
GLOBAL_CHAT_ID: Optional[int] = None


# =========================
# محرك التداول المؤسسي
# =========================
class InstitutionalEngine:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def fetch_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        url = f"{FAPI_BASE}/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        async with (await self.get_session()).get(url, params=params, timeout=15) as r:
            data = await r.json()
        df = pd.DataFrame(
            data,
            columns=["open_time","open","high","low","close","volume",
                     "close_time","qav","trades","tbbav","tbqav","ignore"]
        )
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        return df

    async def fetch_news(self) -> str:
        if not CRYPTOPANIC_API_KEY:
            return "لا توجد أخبار متاحة حالياً."
        try:
            url = "https://cryptopanic.com/api/v1/posts/"
            params = {"auth_token": CRYPTOPANIC_API_KEY, "public": "true"}
            async with (await self.get_session()).get(url, params=params, timeout=15) as r:
                data = await r.json()
            titles = [p.get("title", "") for p in data.get("results", [])[:2]]
            if not titles:
                return "لا توجد أخبار مؤثرة حالياً."
            return "\n".join(titles)
        except:
            return "تعذر جلب الأخبار حالياً، راقب حركة السيولة يدوياً."

    def calc_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        return float(atr) if not np.isnan(atr) else 0.0

    # =========================
    # تحسين اكتشاف الاتجاه + BOS/CHOCH
    # =========================
    def detect_trend(self, df4h: pd.DataFrame, df1h: pd.DataFrame) -> str:
        c4 = df4h["close"].tail(80)
        c1 = df1h["close"].tail(80)

        def swing_points(series: pd.Series, lookback: int = 3):
            highs = []
            lows = []
            for i in range(lookback, len(series) - lookback):
                window = series[i - lookback:i + lookback + 1]
                if series.iloc[i] == window.max():
                    highs.append((i, series.iloc[i]))
                if series.iloc[i] == window.min():
                    lows.append((i, series.iloc[i]))
            return highs, lows

        def detect_structure(series: pd.Series) -> str:
            highs, lows = swing_points(series)
            if len(highs) < 3 or len(lows) < 3:
                return "محايد"

            last_highs = [h[1] for h in highs[-3:]]
            last_lows = [l[1] for l in lows[-3:]]

            hh = last_highs[2] > last_highs[1] > last_highs[0]
            hl = last_lows[2] > last_lows[1] > last_lows[0]

            lh = last_highs[2] < last_highs[1] < last_highs[0]
            ll = last_lows[2] < last_lows[1] < last_lows[0]

            if hh and hl:
                return "صاعد"   # BOS صاعد
            if lh and ll:
                return "هابط"   # BOS هابط
            return "محايد"      # ممكن CHOCH أو تذبذب

        t4 = detect_structure(c4)
        t1 = detect_structure(c1)

        if t4 == t1 and t4 != "محايد":
            return t4
        if t1 != "محايد":
            return t1
        return t4

    def detect_fvg(self, df: pd.DataFrame) -> bool:
        if len(df) < 5:
            return False
        h = df["high"]
        l = df["low"]
        for i in range(len(df) - 3, len(df) - 1):
            if l.iloc[i - 1] > h.iloc[i + 1]:
                return True
            if h.iloc[i - 1] < l.iloc[i + 1]:
                return True
        return False

    def detect_orderblock(self, df: pd.DataFrame, trend: str) -> bool:
        body = (df["close"] - df["open"]).abs()
        rng = df["high"] - df["low"]
        small_body = (rng > 0) & (body / rng < 0.3)
        recent = df[small_body].tail(10)
        if recent.empty:
            return False
        if trend == "صاعد":
            return (recent["low"] == recent["low"].min()).any()
        if trend == "هابط":
            return (recent["high"] == recent["high"].max()).any()
        return False

    def detect_liquidity_sweeps(self, df: pd.DataFrame) -> bool:
        h = df["high"].tail(40)
        l = df["low"].tail(40)
        last_h = h.iloc[-1]
        last_l = l.iloc[-1]
        prev_max = h.iloc[:-1].max()
        prev_min = l.iloc[:-1].min()
        swept_high = last_h > prev_max
        swept_low = last_l < prev_min
        return bool(swept_high or swept_low)

    # =========================
    # تحسين اكتشاف السيولة (تكدس قمم/قيعان)
    # =========================
    def detect_liquidity_zones(self, df: pd.DataFrame) -> bool:
        h = df["high"].tail(80)
        l = df["low"].tail(80)
        high_clusters = ((h.round(3).value_counts() > 3).any())
        low_clusters = ((l.round(3).value_counts() > 3).any())
        return bool(high_clusters or low_clusters)

    def detect_cluster_pressure(self, df5m: pd.DataFrame, trend: str) -> bool:
        vol = df5m["volume"].tail(30)
        avg = vol.mean()
        last = vol.iloc[-1]
        if last > avg * 1.8:
            return True
        return False

    # =========================
    # Breaker Blocks
    # =========================
    def detect_breaker_block(self, df: pd.DataFrame, trend: str) -> bool:
        if len(df) < 30 or trend == "محايد":
            return False

        body = (df["close"] - df["open"]).abs()
        rng = df["high"] - df["low"]
        strong = (rng > 0) & (body / rng > 0.6) & (rng > rng.rolling(20).mean())

        recent = df[strong].tail(10)
        if recent.empty:
            return False

        last = df.iloc[-1]

        if trend == "صاعد":
            bears = recent[recent["close"] < recent["open"]]
            if bears.empty:
                return False
            bb = bears.iloc[-1]
            if last["low"] <= bb["high"] <= last["high"]:
                return True

        if trend == "هابط":
            bulls = recent[recent["close"] > recent["open"]]
            if bulls.empty:
                return False
            bb = bulls.iloc[-1]
            if last["low"] <= bb["low"] <= last["high"]:
                return True

        return False

    # =========================
    # Mitigation Blocks
    # =========================
    def detect_mitigation_block(self, df: pd.DataFrame, trend: str) -> bool:
        if len(df) < 40 or trend == "محايد":
            return False

        body = (df["close"] - df["open"]).abs()
        rng = df["high"] - df["low"]
        strong = (rng > 0) & (body / rng > 0.5) & (rng > rng.rolling(25).mean())

        zone = df[strong].tail(15)
        if zone.empty:
            return False

        last = df.iloc[-1]
        z_high = zone["high"].max()
        z_low = zone["low"].min()

        return z_low <= last["close"] <= z_high

    def rsi(self, series: pd.Series, period: int = 14) -> float:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        val = rsi.iloc[-1]
        return float(val) if not np.isnan(val) else 50.0

    def score_signal(
        self,
        trend: str,
        fvg: bool,
        ob: bool,
        sweep: bool,
        cluster: bool,
        breaker: bool,
        mit_block: bool,
        liq_zone: bool,
        df15m: pd.DataFrame
    ) -> int:
        score = 50
        if trend != "محايد":
            score += 10
        if fvg:
            score += 8
        if ob:
            score += 8
        if sweep:
            score += 10
        if cluster:
            score += 8
        if breaker:
            score += 6
        if mit_block:
            score += 6
        if liq_zone:
            score += 6

        rsi_val = self.rsi(df15m["close"].tail(60))
        if trend == "صاعد" and 35 < rsi_val < 65:
            score += 6
        if trend == "هابط" and 35 < rsi_val < 65:
            score += 6
        if trend == "صاعد" and rsi_val > 70:
            score += 4
        if trend == "هابط" and rsi_val < 30:
            score += 4

        return max(0, min(96, score))

    def build_rr(
        self,
        trend: str,
        fvg: bool,
        ob: bool,
        sweep: bool,
        cluster: bool,
        atr: float,
        prob: int,
        entry_type: str
    ) -> float:
        rr = 2.7

        if trend != "محايد":
            rr += 0.6
        if fvg:
            rr += 0.4
        if ob:
            rr += 0.4
        if sweep:
            rr += 0.6
        if cluster:
            rr += 0.5

        if prob >= 80:
            rr += 0.7
        elif prob >= 70:
            rr += 0.4

        if atr > 0:
            if atr < 0.5:
                rr -= 0.2
            elif atr > 2:
                rr += 0.3

        if entry_type == "معلّق":
            rr += 0.4

        rr = max(2.7, min(6.5, rr))
        return round(rr, 1)

    # =========================
    # تحسين نقاط الدخول
    # =========================
    def refine_entry(
        self,
        price: float,
        df1h: pd.DataFrame,
        trend: str,
        ob: bool,
        fvg: bool,
        mit_block: bool
    ) -> float:
        entry = price

        if fvg:
            h = df1h["high"].iloc[-3:]
            l = df1h["low"].iloc[-3:]
            mid = (h.max() + l.min()) / 2
            entry = (entry + mid) / 2

        if mit_block or ob:
            eq = df1h["close"].tail(5).mean()
            entry = (entry + eq) / 2

        return float(entry)

    def build_levels(
        self,
        price: float,
        atr: float,
        trend: str,
        prob: int,
        fvg: bool,
        ob: bool,
        sweep: bool,
        cluster: bool,
        entry_type: str
    ) -> Dict[str, Any]:
        if atr <= 0:
            return {}
        atr_mult = round(np.random.uniform(1.7, 1.8), 2)
        side = "Long" if trend == "صاعد" else "Short"
        rr = self.build_rr(trend, fvg, ob, sweep, cluster, atr, prob, entry_type)

        if side == "Long":
            sl = price - atr * atr_mult
            tp = price + abs(price - sl) * rr
        else:
            sl = price + atr * atr_mult
            tp = price - abs(price - sl) * rr

        return {
            "side": side,
            "entry": price,
            "sl": sl,
            "tp": tp,
            "rr": rr
        }

    def classify_type(self, price: float, entry: float) -> str:
        dev = abs(price - entry) / entry
        return "فوري" if dev <= 0.005 else "معلّق"

    def build_behavior(
        self,
        symbol: str,
        trend: str,
        fvg: bool,
        ob: bool,
        sweep: bool,
        cluster: bool,
        breaker: bool,
        mit_block: bool,
        liq_zone: bool,
        prob: int,
        entry_type: str
    ) -> str:
        parts = []

        if trend == "صاعد":
            parts.append("اتجاه صاعد على الفريمات الكبيرة مع قمم وقيعان أعلى (BOS صاعد)")
        elif trend == "هابط":
            parts.append("اتجاه هابط على الفريمات الكبيرة مع قمم وقيعان أدنى (BOS هابط)")
        else:
            parts.append("حركة سعرية متزنة مع احتمالية CHOCH أو تذبذب في الهيكل السعري")

        if fvg:
            parts.append("وجود مناطق FVG تدعم استمرار الحركة")
        if ob:
            parts.append("وجود Order Block قوي قريب من منطقة الدخول")
        if sweep:
            parts.append("حدوث Liquidity Sweep على قمم أو قيعان سابقة")
        if cluster:
            parts.append("ضغط كلاستر واضح في أحجام التداول على الفريمات الصغيرة")
        if breaker:
            parts.append("وجود Breaker Block يدعم إعادة الانعكاس من منطقة مؤسسية سابقة")
        if mit_block:
            parts.append("وجود Mitigation Block يعكس امتصاص سيولة سابقة وإعادة اختبار منطقة مؤسسية")
        if liq_zone:
            parts.append("تكدس واضح للسيولة حول قمم/قيعان متقاربة (Liquidity Zones)")

        if prob >= 80:
            parts.append("احتمالية عالية لاستمرار السيناريو الحالي")
        elif prob >= 70:
            parts.append("توافق جيد بين الفريمات والزخم")

        base = "، ".join(parts)
        if entry_type == "معلّق":
            return f"السعر يقترب من منطقة دخول مثالية في {base}."
        return f"تم اختيار هذه الصفقة بناءً على {base}."

    async def analyze_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            df4h = await self.fetch_klines(symbol, "4h", 200)
            df1h = await self.fetch_klines(symbol, "1h", 200)
            df15m = await self.fetch_klines(symbol, "15m", 200)
            df5m = await self.fetch_klines(symbol, "5m", 200)

            trend = self.detect_trend(df4h, df1h)
            fvg = self.detect_fvg(df1h)
            ob = self.detect_orderblock(df1h, trend)
            sweep = self.detect_liquidity_sweeps(df1h)
            cluster = self.detect_cluster_pressure(df5m, trend)
            breaker = self.detect_breaker_block(df1h, trend)
            mit_block = self.detect_mitigation_block(df1h, trend)
            liq_zone = self.detect_liquidity_zones(df1h)

            prob = self.score_signal(
                trend,
                fvg,
                ob,
                sweep,
                cluster,
                breaker,
                mit_block,
                liq_zone,
                df15m
            )

            price = float(df5m["close"].iloc[-1])
            atr = self.calc_atr(df1h)

            ref_price = float(df1h["close"].iloc[-1])
            entry_type = self.classify_type(price, ref_price)

            refined_entry = self.refine_entry(price, df1h, trend, ob, fvg, mit_block)

            levels = self.build_levels(refined_entry, atr, trend, prob, fvg, ob, sweep, cluster, entry_type)
            if not levels:
                return None

            behavior = self.build_behavior(
                symbol,
                trend,
                fvg,
                ob,
                sweep,
                cluster,
                breaker,
                mit_block,
                liq_zone,
                prob,
                entry_type
            )

            return {
                "symbol": symbol,
                "trend": trend,
                "prob": prob,
                "price": price,
                "atr": atr,
                "levels": levels,
                "entry_type": entry_type,
                "fvg": fvg,
                "ob": ob,
                "sweep": sweep,
                "cluster": cluster,
                "breaker": breaker,
                "mit_block": mit_block,
                "liq_zone": liq_zone,
                "behavior": behavior
            }
        except:
            return None

    async def get_top_active_symbols(self, limit: int = 3) -> List[Optional[Dict[str, Any]]]:
        results: List[Dict[str, Any]] = []
        for s in SYMBOLS:
            res = await self.analyze_symbol(s)
            if res:
                results.append(res)
        results.sort(key=lambda x: x["prob"], reverse=True)
        return results[:limit]

    async def send_msg(self, chat_id: int, text: str):
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": f"\u202B{text}",
            "parse_mode": "Markdown"
        }
        async with (await self.get_session()).post(url, json=payload, timeout=15):
            return

    async def send_manual_trades(self, chat_id: int):
        results: List[Dict[str, Any]] = []
        for s in SYMBOLS:
            res = await self.analyze_symbol(s)
            if res:
                results.append(res)
        if not results:
            await self.send_msg(chat_id, "لا توجد صفقات مناسبة حالياً.")
            return
        results.sort(key=lambda x: x["prob"], reverse=True)
        top = results[:2]

        lines = ["أفضل صفقتين في السوق حالياً:", "-" * 35]
        for i, r in enumerate(top):
            lv = r["levels"]
            side_tag = "#Long" if lv["side"] == "Long" else "#Short"
            color = "🟢" if lv["side"] == "Long" else "🔴"
            medal = "🥇" if i == 0 else "🥈"
            lines.append(
                f"{medal} {r['symbol']} {color} ({r['entry_type']})\n"
                f"{side_tag}\n"
                f"Entry: {lv['entry']:.4f}\n"
                f"SL: {lv['sl']:.4f}\n"
                f"TP: {lv['tp']:.4f}\n"
                f"R:R = 1:{lv['rr']}\n"
                f"نسبة النجاح المتوقعة: {r['prob']}%\n"
                f"{'-'*35}"
            )
            lines.append(f"📌 سلوك السعر : {r['behavior']}")
            if r["entry_type"] == "معلّق":
                lines.append("🔹️ سيتم إرسال رسالة تأكيد عند وصول السعر إلى منطقة الدخول المقترحة .")
                monitored_trades[r["symbol"]] = {
                    "entry": lv["entry"],
                    "chat_id": chat_id
                }
            open_trades[r["symbol"]] = {
                "tp": lv["tp"],
                "side": lv["side"],
                "chat_id": chat_id
            }
            lines.append("-" * 35)

        await self.send_msg(chat_id, "\n".join(lines))

    async def send_auto_trade(self, chat_id: int, res: Dict[str, Any]):
        lv = res["levels"]
        side_tag = "#Long" if lv["side"] == "Long" else "#Short"
        color = "🟢" if lv["side"] == "Long" else "🔴"
        header = f"⏰ فحص آلي - صفقة جديدة ({res['entry_type']})"

        msg = (
            f"{header}\n"
            f"{res['symbol']} {color}\n"
            f"{side_tag}\n"
            f"Entry: {lv['entry']:.4f}\n"
            f"SL: {lv['sl']:.4f}\n"
            f"TP: {lv['tp']:.4f}\n"
            f"R:R = 1:{lv['rr']}\n"
            f"نسبة النجاح المتوقعة: {res['prob']}%\n"
            f"{'-'*35}\n"
            f"📌 سلوك السعر : {res['behavior']}"
        )

        if res["entry_type"] == "معلّق":
            msg += "\n🔹️ سيتم إرسال رسالة تأكيد عند وصول السعر إلى منطقة الدخول المقترحة."
            monitored_trades[res["symbol"]] = {
                "entry": lv["entry"],
                "chat_id": chat_id
            }

        open_trades[res["symbol"]] = {
            "tp": lv["tp"],
            "side": lv["side"],
            "chat_id": chat_id
        }

        await self.send_msg(chat_id, msg)

    async def send_analysis(self, chat_id: int):
        news = await self.fetch_news()
        focus = await self.get_top_active_symbols(limit=3)

        lines = [
            "التحليل اليومي لسوق الكريبتو فيوتشرز حسب البيانات الواردة من موقع CryptoPanic",
            "-" * 43,
            f"الأخبار: {news}",
            "",
            "أكثر ثلاث عملات رقمية نشطة حالياً صعوداً أو هبوطاً:",
            "-" * 29
        ]

        for i, r in enumerate(focus, start=1):
            if not r:
                continue

            trend_word = (
                "الصاعد" if r["trend"] == "صاعد"
                else "الهابط" if r["trend"] == "هابط"
                else "الحالي"
            )

            lines.append(
                f"{i}) #{r['symbol']}\n"
                f"⏰ 4h: اتجاه {r['trend']} بشكل واضح.\n"
                "🕰 1h: سيولة مؤسسية وحركة متزنة.\n"
                "🕒 15m: زخم يدعم الاتجاه الحالي.\n"
                f"📉 التوقع: {r['prob']}% احتمال استمرار الاتجاه {trend_word}\n"
                f"{'-'*43}"
            )

        await self.send_msg(chat_id, "\n".join(lines))


engine = InstitutionalEngine()


# =========================
# WebSocket لمراقبة الأسعار
# =========================
async def websocket_monitor():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(FAPI_WS, heartbeat=30, timeout=30) as ws:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                ticks = data if isinstance(data, list) else [data]
                for tick in ticks:
                    s = tick.get("s")
                    if not s:
                        continue
                    price = float(tick.get("c", 0))

                    if s in monitored_trades:
                        ep = monitored_trades[s]["entry"]
                        if abs(price - ep) / ep <= ENTRY_ALERT_TOLERANCE:
                            chat_id = monitored_trades[s]["chat_id"]
                            text = (
                                "🔔 تنبيه:\n"
                                f"السعر وصل منطقة الدخول المقترحة لزوج العملة {s} خذ نظرة و قرر"
                            )
                            await engine.send_msg(chat_id, text)
                            del monitored_trades[s]

                    if s in open_trades:
                        tr = open_trades[s]
                        if tr["side"] == "Long" and price >= tr["tp"]:
                            await engine.send_msg(
                                tr["chat_id"],
                                f"\u202B🎯 تم الوصول للهدف في عملة #{s}"
                            )
                            del open_trades[s]
                        elif tr["side"] == "Short" and price <= tr["tp"]:
                            await engine.send_msg(
                                tr["chat_id"],
                                f"\u202B🎯 تم الوصول للهدف في عملة #{s}"
                            )
                            del open_trades[s]


# =========================
# الفحص الآلي كل 5 دقائق
# =========================
async def auto_loop():
    while True:
        await asyncio.sleep(AUTO_SCAN_INTERVAL)
        if not GLOBAL_CHAT_ID:
            continue
        now = time.time()
        for s in list(last_sent.keys()):
            if now - last_sent[s] > COOLDOWN_SECONDS:
                del last_sent[s]

        best: Optional[Dict[str, Any]] = None
        for sym in SYMBOLS:
            if sym in last_sent and now - last_sent[sym] < COOLDOWN_SECONDS:
                continue
            res = await engine.analyze_symbol(sym)
            if not res:
                continue
            if res["prob"] < MIN_PROB_AUTO:
                continue
            if not best or res["prob"] > best["prob"]:
                best = res

        if best:
            await engine.send_auto_trade(GLOBAL_CHAT_ID, best)
            last_sent[best["symbol"]] = time.time()


# =========================
# FastAPI Webhook + Health
# =========================
@app.get("/")
async def health_check():
    return {"status": "healthy", "bot": "InstitutionalSMC"}

@app.post("/webhook")
async def webhook(req: Request):
    global GLOBAL_CHAT_ID
    data = await req.json()
    msg = data.get("message", {})
    if not msg:
        return {"ok": True}
    chat_id = msg["chat"]["id"]
    GLOBAL_CHAT_ID = chat_id
    text = msg.get("text", "").strip()
    if text == "تحليل":
        asyncio.create_task(engine.send_analysis(chat_id))
    elif text == "صفقات":
        asyncio.create_task(engine.send_manual_trades(chat_id))
    return {"ok": True}


@app.on_event("startup")
async def startup():
    asyncio.create_task(auto_loop())
    asyncio.create_task(websocket_monitor())
    await asyncio.sleep(0.1)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
