import os
import time
import json
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

import aiohttp
import pandas as pd
import numpy as np
from fastapi import FastAPI, Request
import uvicorn

# =========================
# الإعدادات العامة
# =========================
TOKEN = os.getenv("TELEGRAM_TOKEN")

FAPI_BASE = "https://fapi.binance.com"
FAPI_WS = "wss://fstream.binance.com/ws/!ticker@arr"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "MATICUSDT", "NEARUSDT", "TRXUSDT", "LTCUSDT", "UNIUSDT",
    "ARBUSDT", "SUIUSDT", "FILUSDT", "STXUSDT", "APTUSDT"
]

AUTO_SCAN_INTERVAL = 300        # كل 5 دقائق
COOLDOWN_SECONDS = 1800         # 30 دقيقة
ENTRY_ALERT_TOLERANCE = 0.005   # 0.5% لتنبيه الوصول لمنطقة الدخول
MIN_PROB_AUTO = 65              # حد أدنى للنسبة في الفحص الآلي

app = FastAPI()

last_sent: Dict[str, float] = {}
monitored_trades: Dict[str, Dict[str, Any]] = {}
open_trades: Dict[str, Dict[str, Any]] = {}
GLOBAL_CHAT_ID: Optional[int] = None


# =========================
# محرك التداول المؤسسي (SMC / ICT)
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
            columns=["open_time", "open", "high", "low", "close", "volume",
                     "close_time", "qav", "trades", "tbbav", "tbqav", "ignore"]
        )
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df
        async def fetch_news(self):
    import feedparser
    from deep_translator import GoogleTranslator

    rss_url = "https://www.coindesk.com/arc/outboundfeeds/rss/"

    try:
        feed = feedparser.parse(rss_url)
        items = feed.entries[:5]

        if not items:
            return "لا توجد أخبار متاحة حالياً."

        news_list = []
        for item in items:
            title_en = item.title
            title_ar = GoogleTranslator(source='auto', target='ar').translate(title_en)
            link = item.link
            news_list.append(f"• {title_ar}\n{link}")

        return "\n\n".join(news_list)

    except Exception:
        return "تعذر جلب أخبار RSS حالياً."

    # =========================
    # ATR ذكي
    # =========================
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
    # Kill Zones (لندن + نيويورك)
    # =========================
    def in_kill_zone(self) -> bool:
        """
        نستخدم التوقيت العالمي UTC:
        - لندن تقريباً: 7 إلى 11 UTC
        - نيويورك تقريباً: 12 إلى 20 UTC
        """
        now_utc = datetime.now(timezone.utc)
        h = now_utc.hour
        # لندن أو نيويورك
        return (7 <= h <= 11) or (12 <= h <= 20)

    # =========================
    # فلتر تذبذب (Low Volatility Filter)
    # =========================
    def is_low_volatility(self, df1h: pd.DataFrame, atr: float) -> bool:
        """
        نعتبر السوق ميت إذا كان متوسط المدى السعري ضعيف جداً
        مقارنة بالسعر الحالي (رينج ضيق).
        """
        if len(df1h) < 40:
            return False
        recent = df1h.tail(30)
        high = recent["high"]
        low = recent["low"]
        price = recent["close"].iloc[-1]
        avg_range = (high - low).mean()
        if price <= 0:
            return False
        rel_range = avg_range / price
        # لو الرينج أقل من 0.4% نعتبره تذبذب ضعيف
        return bool(rel_range < 0.004)

    # =========================
    # هيكل جاهز لفلتر أخبار (يمكنك ربطه لاحقاً)
    # =========================
    def is_news_time(self) -> bool:
        """
        هنا يمكنك لاحقاً ربط أوقات الأخبار القوية (FOMC, CPI, NFP...)
        حالياً نعيد False دائماً حتى لا نمنع أي صفقة.
        """
        return False

    # =========================
    # هيكل السوق (BOS / CHOCH)
    # =========================
    def detect_market_structure(self, df4h: pd.DataFrame, df1h: pd.DataFrame) -> str:
        c4 = df4h["close"].tail(120)
        c1 = df1h["close"].tail(120)

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

        def structure_bias(series: pd.Series) -> str:
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
                return "صاعد"
            if lh and ll:
                return "هابط"
            return "محايد"

        t4 = structure_bias(c4)
        t1 = structure_bias(c1)

        if t4 == t1 and t4 != "محايد":
            return t4
        if t1 != "محايد":
            return t1
        return t4

    # =========================
    # السيولة (Liquidity)
    # =========================
    def detect_liquidity_pools(self, df: pd.DataFrame) -> Dict[str, bool]:
        h = df["high"].tail(100)
        l = df["low"].tail(100)

        eq_highs = (h.round(3).value_counts() >= 2).any()
        eq_lows = (l.round(3).value_counts() >= 2).any()

        last_h = h.iloc[-1]
        last_l = l.iloc[-1]
        prev_max = h.iloc[:-1].max()
        prev_min = l.iloc[:-1].min()

        swept_high = last_h > prev_max
        swept_low = last_l < prev_min

        return {
            "equal_highs": bool(eq_highs),
            "equal_lows": bool(eq_lows),
            "sweep_high": bool(swept_high),
            "sweep_low": bool(swept_low)
        }

    def detect_liquidity_zones(self, df: pd.DataFrame) -> bool:
        h = df["high"].tail(120)
        l = df["low"].tail(120)
        high_clusters = ((h.round(3).value_counts() > 3).any())
        low_clusters = ((l.round(3).value_counts() > 3).any())
        return bool(high_clusters or low_clusters)

    # =========================
    # FVG / Imbalance
    # =========================
    def detect_fvg(self, df: pd.DataFrame) -> bool:
        if len(df) < 5:
            return False
        h = df["high"]
        l = df["low"]
        for i in range(2, len(df) - 2):
            if l.iloc[i - 1] > h.iloc[i + 1]:
                return True
            if h.iloc[i - 1] < l.iloc[i + 1]:
                return True
        return False

    # =========================
    # Order Blocks / Breaker / Mitigation
    # =========================
    def detect_orderblock(self, df: pd.DataFrame, trend: str) -> bool:
        body = (df["close"] - df["open"]).abs()
        rng = df["high"] - df["low"]
        small_body = (rng > 0) & (body / rng < 0.3)
        recent = df[small_body].tail(15)
        if recent.empty:
            return False
        if trend == "صاعد":
            return (recent["low"] == recent["low"].min()).any()
        if trend == "هابط":
            return (recent["high"] == recent["high"].max()).any()
        return False

    def detect_breaker_block(self, df: pd.DataFrame, trend: str) -> bool:
        if len(df) < 40 or trend == "محايد":
            return False
        body = (df["close"] - df["open"]).abs()
        rng = df["high"] - df["low"]
        strong = (rng > 0) & (body / rng > 0.6) & (rng > rng.rolling(20).mean())
        recent = df[strong].tail(12)
        if recent.empty:
            return False
        last = df.iloc[-1]
        if trend == "صاعد":
            bears = recent[recent["close"] < recent["open"]]
            if bears.empty:
                return False
            bb = bears.iloc[-1]
            return bool(last["low"] <= bb["high"] <= last["high"])
        if trend == "هابط":
            bulls = recent[recent["close"] > recent["open"]]
            if bulls.empty:
                return False
            bb = bulls.iloc[-1]
            return bool(last["low"] <= bb["low"] <= last["high"])
        return False

    def detect_mitigation_block(self, df: pd.DataFrame, trend: str) -> bool:
        if len(df) < 50 or trend == "محايد":
            return False
        body = (df["close"] - df["open"]).abs()
        rng = df["high"] - df["low"]
        strong = (rng > 0) & (body / rng > 0.5) & (rng > rng.rolling(25).mean())
        zone = df[strong].tail(20)
        if zone.empty:
            return False
        last = df.iloc[-1]
        z_high = zone["high"].max()
        z_low = zone["low"].min()
        return bool(z_low <= last["close"] <= z_high)

    # =========================
    # الزخم (RSI / Volume / Cluster)
    # =========================
    def rsi(self, series: pd.Series, period: int = 14) -> float:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        val = rsi.iloc[-1]
        return float(val) if not np.isnan(val) else 50.0

    def detect_cluster_pressure(self, df5m: pd.DataFrame) -> bool:
        vol = df5m["volume"].tail(40)
        avg = vol.mean()
        last = vol.iloc[-1]
        return bool(last > avg * 1.8)

    # =========================
    # نموذج النسبة (أقرب للواقع)
    # =========================
    def score_signal(
        self,
        trend: str,
        fvg: bool,
        ob: bool,
        breaker: bool,
        mit_block: bool,
        liq_pools: Dict[str, bool],
        liq_zone: bool,
        cluster: bool,
        df15m: pd.DataFrame
    ) -> int:
        score = 0

        # هيكل السوق
        if trend == "صاعد" or trend == "هابط":
            score += 15

        # FVG / Imbalance
        if fvg:
            score += 10

        # Order Block / Breaker / Mitigation
        if ob:
            score += 10
        if breaker:
            score += 8
        if mit_block:
            score += 8

        # السيولة
        if liq_pools["equal_highs"] or liq_pools["equal_lows"]:
            score += 6
        if liq_pools["sweep_high"] or liq_pools["sweep_low"]:
            score += 10
        if liq_zone:
            score += 6

        # الزخم
        rsi_val = self.rsi(df15m["close"].tail(80))
        if 35 < rsi_val < 65:
            score += 8
        if rsi_val > 70 or rsi_val < 30:
            score += 4

        if cluster:
            score += 8

        score = max(0, min(100, score))
        return int(score)

    # =========================
    # تصنيف جودة الإشارة (A / B / C)
    # =========================
    def classify_quality(self, prob: int, confluence_count: int) -> str:
        """
        A: نسبة عالية + تداخل إشارات قوي
        B: نسبة متوسطة + تداخل جيد
        C: أقل من ذلك
        """
        if prob >= 80 and confluence_count >= 6:
            return "A"
        if prob >= 65 and confluence_count >= 4:
            return "B"
        return "C"

    # =========================
    # R:R + ATR + المستويات
    # =========================
    def build_rr(
        self,
        trend: str,
        fvg: bool,
        ob: bool,
        breaker: bool,
        mit_block: bool,
        liq_pools: Dict[str, bool],
        liq_zone: bool,
        cluster: bool,
        prob: int,
        entry_type: str
    ) -> float:
        rr = 2.7

        if trend != "محايد":
            rr += 0.5
        if fvg:
            rr += 0.3
        if ob:
            rr += 0.3
        if breaker or mit_block:
            rr += 0.3
        if liq_pools["sweep_high"] or liq_pools["sweep_low"]:
            rr += 0.4
        if cluster:
            rr += 0.3
        if liq_zone:
            rr += 0.2

        if prob >= 80:
            rr += 0.7
        elif prob >= 70:
            rr += 0.4

        if entry_type == "معلّق":
            rr += 0.3

        rr = max(2.7, min(6.0, rr))
        return round(rr, 1)

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
            h = df1h["high"].iloc[-4:]
            l = df1h["low"].iloc[-4:]
            mid = (h.max() + l.min()) / 2
            entry = (entry + mid) / 2

        if mit_block or ob:
            eq = df1h["close"].tail(6).mean()
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
        breaker: bool,
        mit_block: bool,
        liq_pools: Dict[str, bool],
        liq_zone: bool,
        cluster: bool,
        entry_type: str
    ) -> Dict[str, Any]:
        if atr <= 0:
            return {}

        atr_mult = round(np.random.uniform(1.5, 1.8), 2)
        side = "Long" if trend == "صاعد" else "Short"

        rr = self.build_rr(
            trend,
            fvg,
            ob,
            breaker,
            mit_block,
            liq_pools,
            liq_zone,
            cluster,
            prob,
            entry_type
        )

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

    def classify_type(self, price: float, ref_price: float) -> str:
        dev = abs(price - ref_price) / ref_price
        return "فوري" if dev <= 0.02 else "معلّق"

    def build_behavior(
        self,
        symbol: str,
        trend: str,
        fvg: bool,
        ob: bool,
        breaker: bool,
        mit_block: bool,
        liq_pools: Dict[str, bool],
        liq_zone: bool,
        cluster: bool,
        prob: int,
        entry_type: str,
        quality: str
    ) -> str:
        parts = []

        if trend == "صاعد":
            parts.append("اتجاه صاعد واضح على الفريمات الكبيرة مع BOS صاعد وهيكل HH/HL")
        elif trend == "هابط":
            parts.append("اتجاه هابط واضح على الفريمات الكبيرة مع BOS هابط وهيكل LH/LL")
        else:
            parts.append("هيكل سعري متذبذب مع احتمالية CHOCH أو انتقال في الاتجاه")

        if fvg:
            parts.append("وجود مناطق FVG / Imbalance تدعم حركة السعر")
        if ob:
            parts.append("وجود Order Block مؤسسي قريب من منطقة التسعير الحالية")
        if breaker:
            parts.append("وجود Breaker Block يعكس امتصاص سيولة سابقة")
        if mit_block:
            parts.append("وجود Mitigation Block يعيد اختبار منطقة مؤسسية سابقة")
        if liq_pools["equal_highs"] or liq_pools["equal_lows"]:
            parts.append("تكدس سيولة عند قمم/قيعان متساوية (Liquidity Pools)")
        if liq_pools["sweep_high"] or liq_pools["sweep_low"]:
            parts.append("حدوث Liquidity Sweep على قمم أو قيعان سابقة")
        if liq_zone:
            parts.append("تكدس واضح للسيولة في نطاق سعري ضيق (Liquidity Zone)")
        if cluster:
            parts.append("ضغط واضح في أحجام التداول على الفريمات الصغيرة (Cluster Pressure)")

        if prob >= 80:
            parts.append("توافق قوي بين الهيكل والسيولة والزخم")
        elif prob >= 70:
            parts.append("توافق جيد بين الفريمات مع زخم داعم")

        parts.append(f"تصنيف الإشارة: {quality}")

        base = "، ".join(parts)
        if entry_type == "معلّق":
            return f"السعر يقترب من منطقة دخول مؤسسية في {base}."
        return f"تم اختيار هذه الصفقة بناءً على {base}."

    # =========================
    # التحليل الكامل لزوج واحد
    # =========================
    async def analyze_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            df4h = await self.fetch_klines(symbol, "4h", 300)
            df1h = await self.fetch_klines(symbol, "1h", 300)
            df15m = await self.fetch_klines(symbol, "15m", 300)
            df5m = await self.fetch_klines(symbol, "5m", 300)

            trend = self.detect_market_structure(df4h, df1h)
            liq_pools = self.detect_liquidity_pools(df1h)
            liq_zone = self.detect_liquidity_zones(df1h)
            fvg = self.detect_fvg(df1h)
            ob = self.detect_orderblock(df1h, trend)
            breaker = self.detect_breaker_block(df1h, trend)
            mit_block = self.detect_mitigation_block(df1h, trend)
            cluster = self.detect_cluster_pressure(df5m)

            prob = self.score_signal(
                trend,
                fvg,
                ob,
                breaker,
                mit_block,
                liq_pools,
                liq_zone,
                cluster,
                df15m
            )

            price = float(df5m["close"].iloc[-1])
            atr = self.calc_atr(df1h)
            ref_price = float(df1h["close"].iloc[-1])
            entry_type = self.classify_type(price, ref_price)
            refined_entry = self.refine_entry(price, df1h, trend, ob, fvg, mit_block)

            low_vol = self.is_low_volatility(df1h, atr)
            kill_ok = self.in_kill_zone()
            news_block = self.is_news_time()

            # عدد التوافقات (Confluence Count)
            confluence_count = sum([
                trend != "محايد",
                fvg,
                ob,
                breaker,
                mit_block,
                liq_pools["equal_highs"],
                liq_pools["equal_lows"],
                liq_pools["sweep_high"],
                liq_pools["sweep_low"],
                liq_zone,
                cluster
            ])

            quality = self.classify_quality(prob, confluence_count)

            levels = self.build_levels(
                refined_entry,
                atr,
                trend,
                prob,
                fvg,
                ob,
                breaker,
                mit_block,
                liq_pools,
                liq_zone,
                cluster,
                entry_type
            )
            if not levels:
                return None

            behavior = self.build_behavior(
                symbol,
                trend,
                fvg,
                ob,
                breaker,
                mit_block,
                liq_pools,
                liq_zone,
                cluster,
                prob,
                entry_type,
                quality
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
                "breaker": breaker,
                "mit_block": mit_block,
                "liq_pools": liq_pools,
                "liq_zone": liq_zone,
                "cluster": cluster,
                "behavior": behavior,
                "quality": quality,
                "confluence": confluence_count,
                "low_vol": low_vol,
                "kill_ok": kill_ok,
                "news_block": news_block
            }
        except Exception:
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

        # فلترة الصفقات بحيث تكون ≥ 70%
        results = [r for r in results if r["prob"] >= 65]

        if not results:
            await self.send_msg(chat_id, "لا توجد صفقات مناسبة حالياً.")
            return
        results.sort(key=lambda x: x["prob"], reverse=True)
        top = results[:2]

        lines = ["أفضل صفقتين مؤسسيتين في السوق حالياً:", "-" * 35]
        for i, r in enumerate(top):
            lv = r["levels"]
            side_tag = "#Long" if lv["side"] == "Long" else "#Short"
            color = "🟢" if lv["side"] == "Long" else "🔴"
            medal = "🥇" if i == 0 else "🥈"

            extra_flags = []
            if r["low_vol"]:
                extra_flags.append("⚠️ تذبذب ضعيف")
            if not r["kill_ok"]:
                extra_flags.append("⏱ خارج Kill Zones")
            if r["news_block"]:
                extra_flags.append("📰 وقت أخبار قوية")

            flags_text = ""
            if extra_flags:
                flags_text = "\n" + " | ".join(extra_flags)

            lines.append(
                f"{medal} {r['symbol']} {color} ({r['entry_type']})\n"
                f"{side_tag}\n"
                f"Entry: {lv['entry']:.4f}\n"
                f"SL: {lv['sl']:.4f}\n"
                f"TP: {lv['tp']:.4f}\n"
                f"R:R = 1:{lv['rr']}\n"
                f"نسبة الثقة النموذجية: {r['prob']}%\n"
                f"تصنيف الإشارة: {r['quality']} (Confluence: {r['confluence']})"
                f"{flags_text}\n"
                f"{'-'*35}"
            )
            lines.append(f"📌 سلوك السعر المؤسسي : {r['behavior']}")
            if r["entry_type"] == "معلّق":
                lines.append("🔹️ سيتم إرسال رسالة تأكيد عند وصول السعر إلى منطقة الدخول المقترحة.")
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
        header = f"⏰ فحص آلي - صفقة مؤسسية جديدة ({res['entry_type']})"

        extra_flags = []
        if res["low_vol"]:
            extra_flags.append("⚠️ تذبذب ضعيف")
        if not res["kill_ok"]:
            extra_flags.append("⏱ خارج Kill Zones")
        if res["news_block"]:
            extra_flags.append("📰 وقت أخبار قوية")

        flags_text = ""
        if extra_flags:
            flags_text = "\n" + " | ".join(extra_flags)

        msg = (
            f"{header}\n"
            f"{res['symbol']} {color}\n"
            f"{side_tag}\n"
            f"Entry: {lv['entry']:.4f}\n"
            f"SL: {lv['sl']:.4f}\n"
            f"TP: {lv['tp']:.4f}\n"
            f"R:R = 1:{lv['rr']}\n"
            f"نسبة الثقة النموذجية: {res['prob']}%\n"
            f"تصنيف الإشارة: {res['quality']} (Confluence: {res['confluence']})"
            f"{flags_text}\n"
            f"{'-'*35}\n"
            f"📌 سلوك السعر المؤسسي : {res['behavior']}"
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
            "التحليل اليومي لسوق الكريبتو فيوتشرز حسب البيانات الواردة من CoinDesk",
            "-" * 43,
            f"الأخبار / بيانات السوق:\n{news}",
            "-" * 55,
            "أكثر ثلاث عملات رقمية نشطة حالياً:",
            "-" * 35
        ]

        for i, r in enumerate(focus, start=1):
            if not r:
                continue

            trend_word = (
                "الصاعد" if r["trend"] == "صاعد"
                else "الهابط" if r["trend"] == "هابط"
                else "الحالي"
            )

            extra_flags = []
            if r["low_vol"]:
                extra_flags.append("⚠️ تذبذب ضعيف")
            if not r["kill_ok"]:
                extra_flags.append("⏱ خارج Kill Zones")
            if r["news_block"]:
                extra_flags.append("📰 وقت أخبار قوية")

            flags_text = ""
            if extra_flags:
                flags_text = "\n" + " | ".join(extra_flags)

            lines.append(
                f"{i}) #{r['symbol']}\n"
                f"⏰ 4h: اتجاه {r['trend']}.\n"
                "🕰 1h: قراءة هيكل السوق والسيولة المؤسسية.\n"
                "🕒 15m: زخم يدعم السيناريو الحالي.\n"
                f"📉 التوقع النموذجي: {r['prob']}% لاستمرار الاتجاه {trend_word}\n"
                f"تصنيف الإشارة: {r['quality']} (Confluence: {r['confluence']})"
                f"{flags_text}\n"
                f"{'-'*55}"
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
                                "🔔 تنبيه مؤسسي:\n"
                                f"السعر وصل منطقة الدخول المقترحة لزوج {s}، راجع النموذج وقرر."
                            )
                            await engine.send_msg(chat_id, text)
                            del monitored_trades[s]

                    if s in open_trades:
                        tr = open_trades[s]
                        if tr["side"] == "Long" and price >= tr["tp"]:
                            await engine.send_msg(
                                tr["chat_id"],
                                f"\u202B🎯 تم الوصول للهدف في عملة #{s} وفق النموذج المؤسسي."
                            )
                            del open_trades[s]
                        elif tr["side"] == "Short" and price <= tr["tp"]:
                            await engine.send_msg(
                                tr["chat_id"],
                                f"\u202B🎯 تم الوصول للهدف في عملة #{s} وفق النموذج المؤسسي."
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
            # فلتر تذبذب + Kill Zone + أخبار
            if res["low_vol"]:
                continue
            if not res["kill_ok"]:
                continue
            if res["news_block"]:
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
    return {"status": "healthy", "bot": "InstitutionalSMC_Encyclopedia"}

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
