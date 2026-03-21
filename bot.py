import os
import time
import json
import asyncio
import logging
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
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set")

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
MIN_PROB_AUTO = 75              # حد أدنى للنسبة في الفحص الآلي

app = FastAPI()

last_sent: Dict[str, float] = {}
monitored_trades: Dict[str, Dict[str, Any]] = {}
open_trades: Dict[str, Dict[str, Any]] = {}
GLOBAL_CHAT_ID: Optional[int] = None
LAST_TELEGRAM_SEND = 0.0


# =========================
# محرك التداول المؤسسي (SMC / ICT)
# =========================
class InstitutionalEngine:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def safe_request(self, method: str, url: str, **kwargs):
        """
        أهم دالة في النظام:
        - جلسة HTTP محسّنة
        - Retry تلقائي
        - Logging كامل
        - حماية ضد Flood
        - حماية ضد انقطاع الاتصال
        - حماية ضد Rate Limit
        """
        global LAST_TELEGRAM_SEND

        # حماية ضد Flood في Telegram
        if "api.telegram.org" in url:
            now = time.time()
            if now - LAST_TELEGRAM_SEND < 0.7:
                await asyncio.sleep(0.7)
            LAST_TELEGRAM_SEND = time.time()

        # إنشاء جلسة محسّنة
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(
                total=30,
                connect=12,
                sock_read=12,
                sock_connect=12
            )
            connector = aiohttp.TCPConnector(
                limit=150,
                enable_cleanup_closed=True
            )
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                trust_env=True
            )

        retries = 5
        delay = 1.0

        for attempt in range(retries):
            try:
                if method.lower() == "get":
                    async with self.session.get(url, **kwargs) as r:
                        return await r.json()
                elif method.lower() == "post":
                    async with self.session.post(url, **kwargs) as r:
                        return await r.json()
                else:
                    raise ValueError(f"Unsupported method: {method}")
            except Exception as e:
                logging.error(f"Request failed ({attempt+1}/{retries}) {method} {url}: {e}")
                if attempt == retries - 1:
                    logging.exception(e)
                    raise
                await asyncio.sleep(delay)
                delay *= 1.5

    async def fetch_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        url = f"{FAPI_BASE}/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        data = await self.safe_request("get", url, params=params)
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

        except Exception as e:
            logging.exception(e)
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
        return (7 <= h <= 11) or (12 <= h <= 20)

    # =========================
    # فلتر تذبذب (Low Volatility Filter)
    # =========================
    def is_low_volatility(self, df1h: pd.DataFrame, atr: float) -> bool:
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
        return bool(rel_range < 0.004)

    # =========================
    # فلتر Range / Consolidation
    # =========================
    def is_ranging(self, df1h: pd.DataFrame) -> bool:
        """
        نعتبر السوق في رينج إذا كان السعر يتحرك داخل نطاق ضيق
        مع غياب قمم وقيعان واضحة.
        """
        if len(df1h) < 80:
            return False
        recent = df1h.tail(60)
        high = recent["high"].max()
        low = recent["low"].min()
        price = recent["close"].iloc[-1]
        if price <= 0:
            return False
        total_range = (high - low) / price
        # رينج ضيق أقل من 2.5%
        if total_range < 0.025:
            return True
        return False

    # =========================
    # هيكل جاهز لفلتر أخبار (يمكنك ربطه لاحقاً)
    # =========================
    def is_news_time(self) -> bool:
        """
        هنا يمكن ربط أوقات الأخبار القوية (FOMC, CPI, NFP...)
        حالياً نستخدم منطق بسيط: نتجنب أول 5 دقائق بعد بداية كل ساعة.
        """
        now_utc = datetime.now(timezone.utc)
        if now_utc.minute <= 5:
            return True
        return False

    # =========================
    # هيكل السوق (BOS / CHOCH) + قوة الكسر
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
    # Smart Money Trap (Sweep + Rejection)
    # =========================
    def detect_smart_money_trap(self, df: pd.DataFrame) -> bool:
        """
        نبحث عن Sweep ثم شمعة رفض قوية تعيد السعر داخل النطاق.
        """
        if len(df) < 20:
            return False
        recent = df.tail(10)
        high = recent["high"]
        low = recent["low"]
        close = recent["close"]
        open_ = recent["open"]

        # شمعة أخيرة ذات ذيل طويل ورفض واضح
        last = recent.iloc[-1]
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]
        if rng <= 0:
            return False
        long_wick = (rng - body) / rng > 0.6

        # تحقق من أن الشمعة كسرت قمة/قاع ثم أغلقت داخل النطاق السابق
        prev_high = high.iloc[:-1].max()
        prev_low = low.iloc[:-1].min()

        swept_up = last["high"] > prev_high and last["close"] < prev_high
        swept_down = last["low"] < prev_low and last["close"] > prev_low

        return bool(long_wick and (swept_up or swept_down))

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
    # Volume Imbalance بسيط
    # =========================
    def detect_volume_imbalance(self, df: pd.DataFrame) -> bool:
        if len(df) < 40:
            return False
        vol = df["volume"].tail(30)
        avg = vol.mean()
        last = vol.iloc[-1]
        return bool(last > avg * 2.0)

    # =========================
    # OTE (Optimal Trade Entry) بسيط
    # =========================
    def compute_ote_level(self, df1h: pd.DataFrame, trend: str) -> Optional[float]:
        if len(df1h) < 30:
            return None
        recent = df1h.tail(30)
        swing_high = recent["high"].max()
        swing_low = recent["low"].min()
        if trend == "صاعد":
            # OTE شراء بين 62% و 79% من الحركة الهابطة الأخيرة
            diff = swing_high - swing_low
            level_62 = swing_low + diff * 0.62
            level_79 = swing_low + diff * 0.79
            return (level_62 + level_79) / 2
        elif trend == "هابط":
            diff = swing_high - swing_low
            level_62 = swing_high - diff * 0.62
            level_79 = swing_high - diff * 0.79
            return (level_62 + level_79) / 2
        return None

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
        df15m: pd.DataFrame,
        smart_trap: bool,
        vol_imbalance: bool,
        multi_tf_liq: bool
    ) -> int:
        score = 0

        # هيكل السوق
        if trend == "صاعد" or trend == "هابط":
            score += 18

        # FVG / Imbalance
        if fvg:
            score += 8

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
            score += 12
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

        if smart_trap:
            score += 10

        if vol_imbalance:
            score += 6

        if multi_tf_liq:
            score += 6

        score = max(0, min(100, score))
        return int(score)

    # =========================
    # تصنيف جودة الإشارة (A / B / C)
    # =========================
    def classify_quality(self, prob: int, confluence_count: int) -> str:
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
        entry_type: str,
        smart_trap: bool
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
            rr += 0.5
        if cluster:
            rr += 0.3
        if liq_zone:
            rr += 0.2
        if smart_trap:
            rr += 0.4

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
        mit_block: bool,
        ote_level: Optional[float]
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

        if ote_level is not None:
            entry = (entry + ote_level) / 2

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
        entry_type: str,
        smart_trap: bool
    ) -> Dict[str, Any]:
        if atr <= 0:
            return {}

        atr_mult = 1.6
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
            entry_type,
            smart_trap
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
        quality: str,
        smart_trap: bool,
        vol_imbalance: bool
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
        if smart_trap:
            parts.append("وجود Smart Money Trap (Sweep + رفض قوي) يدعم السيناريو")
        if vol_imbalance:
            parts.append("وجود Volume Imbalance واضح يعكس دخول سيولة قوية")

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
            liq_pools_1h = self.detect_liquidity_pools(df1h)
            liq_zone_1h = self.detect_liquidity_zones(df1h)
            liq_pools_4h = self.detect_liquidity_pools(df4h)
            liq_zone_4h = self.detect_liquidity_zones(df4h)
            multi_tf_liq = (
                (liq_pools_1h["equal_highs"] or liq_pools_1h["equal_lows"] or liq_zone_1h) and
                (liq_pools_4h["equal_highs"] or liq_pools_4h["equal_lows"] or liq_zone_4h)
            )

            fvg = self.detect_fvg(df1h)
            ob = self.detect_orderblock(df1h, trend)
            breaker = self.detect_breaker_block(df1h, trend)
            mit_block = self.detect_mitigation_block(df1h, trend)
            cluster = self.detect_cluster_pressure(df5m)
            smart_trap = self.detect_smart_money_trap(df1h)
            vol_imbalance = self.detect_volume_imbalance(df1h)

            prob = self.score_signal(
                trend,
                fvg,
                ob,
                breaker,
                mit_block,
                liq_pools_1h,
                liq_zone_1h,
                cluster,
                df15m,
                smart_trap,
                vol_imbalance,
                multi_tf_liq
            )

            price = float(df5m["close"].iloc[-1])
            atr = self.calc_atr(df1h)
            ref_price = float(df1h["close"].iloc[-1])
            entry_type = self.classify_type(price, ref_price)

            ote_level = self.compute_ote_level(df1h, trend)
            refined_entry = self.refine_entry(price, df1h, trend, ob, fvg, mit_block, ote_level)

            low_vol = self.is_low_volatility(df1h, atr)
            kill_ok = self.in_kill_zone()
            news_block = self.is_news_time()
            ranging = self.is_ranging(df1h)

            confluence_count = sum([
                trend != "محايد",
                fvg,
                ob,
                breaker,
                mit_block,
                liq_pools_1h["equal_highs"],
                liq_pools_1h["equal_lows"],
                liq_pools_1h["sweep_high"],
                liq_pools_1h["sweep_low"],
                liq_zone_1h,
                cluster,
                smart_trap,
                vol_imbalance,
                multi_tf_liq
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
                liq_pools_1h,
                liq_zone_1h,
                cluster,
                entry_type,
                smart_trap
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
                liq_pools_1h,
                liq_zone_1h,
                cluster,
                prob,
                entry_type,
                quality,
                smart_trap,
                vol_imbalance
            )

            # فلاتر إضافية لرفع جودة الإشارة
            if ranging:
                return None

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
                "liq_pools": liq_pools_1h,
                "liq_zone": liq_zone_1h,
                "cluster": cluster,
                "behavior": behavior,
                "quality": quality,
                "confluence": confluence_count,
                "low_vol": low_vol,
                "kill_ok": kill_ok,
                "news_block": news_block,
                "smart_trap": smart_trap,
                "vol_imbalance": vol_imbalance,
                "multi_tf_liq": multi_tf_liq,
                "ranging": ranging
            }
        except Exception as e:
            logging.exception(e)
            return None

    async def get_top_active_symbols(self, limit: int = 3) -> List[Optional[Dict[str, Any]]]:
        tasks = [self.analyze_symbol(s) for s in SYMBOLS]
        results_raw = await asyncio.gather(*tasks)
        results: List[Dict[str, Any]] = [r for r in results_raw if r]
        results.sort(key=lambda x: x["prob"], reverse=True)
        return results[:limit]

    async def send_msg(self, chat_id: int, text: str):
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": f"\u202B{text}",
            "parse_mode": "Markdown"
        }
        try:
            await self.safe_request("post", url, json=payload)
        except Exception as e:
            logging.exception(e)

    async def send_manual_trades(self, chat_id: int):
        tasks = [self.analyze_symbol(s) for s in SYMBOLS]
        results_raw = await asyncio.gather(*tasks)
        results: List[Dict[str, Any]] = [r for r in results_raw if r]

        results = [r for r in results if r["prob"] >= 70]

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

            extra_flags = []
            if r["low_vol"]:
                extra_flags.append("⚠️ تذبذب ضعيف")
            if not r["kill_ok"]:
                extra_flags.append("⏱ خارج Kill Zones")
            if r["news_block"]:
                extra_flags.append("📰 وقت أخبار قوية")
            if r["ranging"]:
                extra_flags.append("📎 السوق في رينج")
            if r["smart_trap"]:
                extra_flags.append("🎯 Smart Money Trap")
            if r["multi_tf_liq"]:
                extra_flags.append("💧 سيولة متوافقة عبر الفريمات")

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
                f"نسبة النجاح المتوقعة: {r['prob']}%\n"
                f"تصنيف الإشارة: {r['quality']} (Confluence: {r['confluence']})"
                f"{flags_text}\n"
                f"{'-'*35}"
            )
            lines.append(f"📌 ملاحظة : {r['behavior']}")
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
        header = f"⏰ فحص آلي - صفقة جديدة ({res['entry_type']})"

        extra_flags = []
        if res["low_vol"]:
            extra_flags.append("⚠️ تذبذب ضعيف")
        if not res["kill_ok"]:
            extra_flags.append("⏱ خارج Kill Zones")
        if res["news_block"]:
            extra_flags.append("📰 وقت أخبار قوية")
        if res["ranging"]:
            extra_flags.append("📎 السوق في رينج")
        if res["smart_trap"]:
            extra_flags.append("🎯 Smart Money Trap")
        if res["multi_tf_liq"]:
            extra_flags.append("💧 سيولة متوافقة عبر الفريمات")

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
            f"نسبة النجاح المتوقعة: {res['prob']}%\n"
            f"تصنيف الإشارة: {res['quality']} (Confluence: {res['confluence']})"
            f"{flags_text}\n"
            f"{'-'*35}\n"
            f"📌 ملاحظة : {res['behavior']}"
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
            if r["ranging"]:
                extra_flags.append("📎 السوق في رينج")
            if r["smart_trap"]:
                extra_flags.append("🎯 Smart Money Trap")
            if r["multi_tf_liq"]:
                extra_flags.append("💧 سيولة متوافقة عبر الفريمات")

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
    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
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
                                        "🔔 تنبيه :\n"
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
        except Exception as e:
            logging.error(f"WS reconnect: {e}")
            await asyncio.sleep(5)


# =========================
# الفحص الآلي كل 5 دقائق
# =========================
async def auto_loop():
    while True:
        await asyncio.sleep(AUTO_SCAN_INTERVAL)
        if not GLOBAL_CHAT_ID:
            continue

        now = time.time()

        # تنظيف دوري للقواميس
        for k in list(open_trades.keys()):
            tr = open_trades[k]
            # تنظيف بسيط بعد 24 ساعة مثلاً (يمكن تطويره)
            if now - last_sent.get(k, now) > 86400:
                del open_trades[k]

        for k in list(monitored_trades.keys()):
            if now - last_sent.get(k, now) > 86400:
                del monitored_trades[k]

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
            if res["low_vol"]:
                continue
            if not res["kill_ok"]:
                continue
            if res["news_block"]:
                continue
            if res["ranging"]:
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
    return {"status": "healthy", "bot": "InstitutionalSMC_Encyclopedia_Pro"}

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
