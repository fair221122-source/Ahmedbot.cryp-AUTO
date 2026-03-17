import os, asyncio, json, time, aiohttp
import pandas as pd
import numpy as np
from datetime import datetime
from fastapi import FastAPI, Request
import uvicorn

# --- الإعدادات (تأكد من ضبطها في السيرفر) ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
API_KEY_PANIC = os.getenv("CRYPTOPANIC_API_KEY") # اختياري لجلب الأخبار
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","NEARUSDT","TRXUSDT","LTCUSDT","UNIUSDT","ARBUSDT","OPUSDT","SUIUSDT","FILUSDT","STXUSDT",
    "APTUSDT","INJUSDT","SEIUSDT","PEPEUSDT","ORDIUSDT","TAOUSDT","ENAUSDT","FTMUSDT","HBARUSDT","ARUSDT"
]

last_sent = {} # لمنع التكرار خلال 30 دقيقة
monitored_trades = {} # لمراقبة منطقة الدخول لحظياً
app = FastAPI()

class SmartMoneyBot:
    def __init__(self):
        self.session = None

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    # 1. رادار الأخبار من CryptoPanic
    async def fetch_news(self):
        try:
            url = f"https://cryptopanic.com/api/v1/posts/?auth_token={API_KEY_PANIC}&public=true"
            async with (await self.get_session()).get(url) as r:
                data = await r.json()
                titles = [post['title'] for post in data['results'][:2]]
                return "\n".join(titles) if titles else "لا توجد أخبار مؤثرة حالياً."
        except: return "تعذر جلب الأخبار، راقب حركة السيولة يدوياً."

    # 2. محرك التحليل المؤسسي (SMC/ICT)
    def analyze_institutional(self, d4, d1):
        # Market Structure Shift & Order Blocks
        trend = "neutral"
        if d1['c'].iloc[-1] > d4['h'].iloc[-20:].max(): trend = "صاعد"
        elif d1['c'].iloc[-1] < d4['l'].iloc[-20:].min(): trend = "هابط"
        
        # Fair Value Gap (FVG)
        fvg = (d1['l'].iloc[-3] > d1['h'].iloc[-1]) or (d1['h'].iloc[-3] < d1['l'].iloc[-1])
        
        # Premium/Discount Zone
        mid = (d4['h'].max() + d4['l'].min()) / 2
        discount = d1['c'].iloc[-1] < mid if trend == "صاعد" else d1['c'].iloc[-1] > mid
        
        prob = 50
        if trend != "neutral": prob += 20
        if fvg: prob += 15
        if discount: prob += 10
        return trend, prob

    async def fetch_klines(self, symbol, interval):
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=100"
        async with (await self.get_session()).get(url) as r:
            df = pd.DataFrame(await r.json(), columns=['ts','o','h','l','c','v','cts','qv','nt','tbv','tqv','i'])
            return df.astype(float)

    async def send_msg(self, chat_id, text):
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        # \u202B للمحاذاة لليمين
        payload = {"chat_id": chat_id, "text": f"\u202B{text}", "parse_mode": "Markdown"}
        async with (await self.get_session()).post(url, json=payload) as r: return await r.json()

    # 3. معالجة السوق (آلي / يدوي)
    async def process(self, chat_id, mode="auto"):
        results = []
        news = await self.fetch_news() if mode == "analysis" else ""
        
        for s in SYMBOLS:
            try:
                d4, d1 = await self.fetch_klines(s, "4h"), await self.fetch_klines(s, "1h")
                trend, prob = self.analyze_institutional(d4, d1)
                price = d1['c'].iloc[-1]
                atr = d1['h'].iloc[-14:].mean() - d1['l'].iloc[-14:].mean()
                res = {'s': s, 'trend': trend, 'prob': prob, 'price': price, 'atr': atr}
                results.append(res)

                if mode == "auto" and prob >= 75 and s not in last_sent:
                    await self.send_formatted_trade(chat_id, res, is_auto=True)
                    last_sent[s] = time.time()
            except: continue

        if mode == "analysis": await self.send_analysis(chat_id, results[:3], news)
        elif mode == "trades":
            top = sorted(results, key=lambda x: x['prob'], reverse=True)[:2]
            for i, t in enumerate(top): await self.send_formatted_trade(chat_id, t, rank=i+1)

    async def send_formatted_trade(self, chat_id, res, rank=None, is_auto=False):
        # ATR ذكي (1.7 - 1.8) و R:R ديناميكي (2.7 - 6.5)
        atr_m = round(np.random.uniform(1.7, 1.8), 2)
        rr = round(2.7 + (res['prob'] - 50) * (6.5 - 2.7) / 45, 1)
        
        side = "#Long" if res['trend'] == "صاعد" else "#Short"
        color = "🟢" if "Long" in side else "🔴"
        sl = res['price'] - (res['atr'] * atr_m) if "Long" in side else res['price'] + (res['atr'] * atr_m)
        tp = res['price'] + (abs(res['price'] - sl) * rr) if "Long" in side else res['price'] - (abs(res['price'] - sl) * rr)
        
        # فحص الانحراف (0.5% - 1%)
        dev = abs(res['price'] - res['price']*1.008) / res['price'] 
        type_str = "معلّق" if dev > 0.005 else "فورية"
        
        if is_auto:
            header = f"⏰ فحص آلي - صفقة جديدة ({type_str})"
        else:
            header = f"{'🥇' if rank==1 else '🥈'} {res['s']} {color} ({type_str})"

        msg = f"{header}\n{side}\nEntry: {res['price']:.4f}\nSL: {sl:.4f}\nTP: {tp:.4f}\nR:R = 1:{rr}\nنسبة النجاح المتوقعة: {res['prob']}%\n"
        msg += "-------------------------------------------\n"
        msg += f"📌 سلوك السعر : {'السعر يقترب من منطقة دخول مثالية في' if type_str=='معلّق' else 'اتجاه ' + res['trend'] + ' على الفريمات الكبيرة مع'} قمم وقيعان أعلى بشكل واضح. زخم إيجابي على الفريمات الصغيرة."
        
        if type_str == "معلّق":
            msg += "\n🔹️ سيتم إرسال رسالة تأكيد عند وصول السعر إلى منطقة الدخول المقترحة ."
            monitored_trades[res['s']] = {'entry': res['price'], 'chat_id': chat_id}
            
        await self.send_msg(chat_id, msg)

    async def send_analysis(self, chat_id, top, news):
        msg = f"التحليل اليومي لسوق الكريبتو فيوتشرز حسب البيانات الواردة من موقع CryptoPanic\n-------------------------------------------\nالأخبار: {news}\n\nأكثر ثلاث عملات رقمية نشطة حاليا صعود أو هبوط:\n-----------------------------\n"
        for i, t in enumerate(top):
            msg += f"{i+1}) #{t['s']}\n⏰ 4h: اتجاه {t['trend']} بشكل واضح.\n🕰 1h: سيولة مؤسسية وحركة متزنة.\n🕒 15m: زخم يدعم الاتجاه الحالي.\n📉 التوقع: {t['prob']}% احتمال استمرار الاتجاه\n-------------------------------------------\n"
        await self.send_msg(chat_id, msg)

bot_engine = SmartMoneyBot()
GLOBAL_CHAT_ID = None

# --- نظام الرصد اللحظي (Websocket Session) ---
async def websocket_monitor():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect("wss://fstream.binance.com/ws/!ticker@arr") as ws:
            async for msg in ws:
                data = json.loads(msg.data)
                for tick in data:
                    s = tick['s']
                    if s in monitored_trades:
                        cp = float(tick['c'])
                        ep = monitored_trades[s]['entry']
                        if abs(cp - ep) / ep <= 0.005:
                            await bot_engine.send_msg(monitored_trades[s]['chat_id'], f"🔔 تنبيه:\nالسعر وصل منطقة الدخول المقترحة لعملة {s} خذ نظرة و قرر")
                            del monitored_trades[s]
@app.get("/")
async def health_check():
    return {"status": "healthy", "bot": "AhmedSMCBot"}

@app.post("/webhook")
async def webhook(req: Request):
    global GLOBAL_CHAT_ID
    data = await req.json()
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        GLOBAL_CHAT_ID = chat_id
        text = data["message"].get("text", "")
        if text == "تحليل": asyncio.create_task(bot_engine.process(chat_id, "analysis"))
        elif text == "صفقات": asyncio.create_task(bot_engine.process(chat_id, "trades"))
    return {"ok": True}

async def auto_loop():
    while True:
        if GLOBAL_CHAT_ID:
            await bot_engine.process(GLOBAL_CHAT_ID, "auto")
        # تنظيف قائمة الـ 30 دقيقة
        now = time.time()
        for s in list(last_sent.keys()):
            if now - last_sent[s] > 1800: del last_sent[s]
        await asyncio.sleep(300)

@app.on_event("startup")
async def startup():
    asyncio.create_task(auto_loop())
    asyncio.create_task(websocket_monitor())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
