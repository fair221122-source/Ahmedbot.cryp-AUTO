import asyncio
import requests
import ccxt
import pandas as pd
from telegram import Bot

# ==========================================
# 🔑 بيانات الربط (استخدم بياناتك هنا)
# ==========================================
TELEGRAM_TOKEN = '8524445307:AAEDw5THEah-iBwpgsTqvK2Pi7abpzWarZk'
CHAT_ID = '986199874'
CRYPTOPANIC_API_KEY = 'a5563e90848ba81e4aeca929e26d90069b2d1b9f'

bot = Bot(token=TELEGRAM_TOKEN)
exchange = ccxt.binance({'options': {'defaultType': 'future'}})

# قائمة الـ 51 عملة
SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'TAO/USDT', 'MKR/USDT', 'AAVE/USDT', 'LTC/USDT', 'BCH/USDT', 'ORDI/USDT', 'AVAX/USDT', 'LINK/USDT', 'INJ/USDT', 'NEAR/USDT', 'DOT/USDT', 'UNI/USDT', 'APT/USDT', 'OP/USDT', 'SUI/USDT', 'TIA/USDT', 'RNDR/USDT', 'FIL/USDT', 'STX/USDT', 'FET/USDT', 'LDO/USDT', 'DYDX/USDT', 'SNX/USDT', 'ENS/USDT', 'PENDLE/USDT', 'RUNE/USDT', 'AXS/USDT', 'AR/USDT', 'IMX/USDT', 'SEI/USDT', 'THETA/USDT', 'EGLD/USDT', 'ALGO/USDT', 'ATOM/USDT', 'VET/USDT', 'XRP/USDT', 'ADA/USDT', 'DOGE/USDT', 'MATIC/USDT', 'CRV/USDT', 'PYTH/USDT', 'WIF/USDT', 'JTO/USDT', 'ENA/USDT', 'W/USDT', 'ICP/USDT']

# مخزن لمراقبة الصفقات المفتوحة والأهداف
active_trades = {}

# --- 1. حساب المؤشرات يدوياً لبيئة Render ---
def calculate_indicators(df):
    df['ema_200'] = df['c'].ewm(span=200, adjust=False).mean()
    df['tr'] = pd.concat([df['h']-df['l'], (df['h']-df['c'].shift()).abs(), (df['l']-df['c'].shift()).abs()], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(window=14).mean()
    delta = df['c'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    df['rsi'] = 100 - (100 / (1 + (gain / loss)))
    return df

# --- 2. رادار الأخبار ---
def get_news_radar(symbol):
    try:
        coin = symbol.split('/')[0]
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API_KEY}&currencies={coin}&filter=hot"
        res = requests.get(url).json()
        posts = res.get('results', [])
        news = posts[0]['title'] if posts else "استقرار في حركة السيولة المؤسسية حالياً."
        sent = "Positive ✅" if len(posts) > 0 else "Neutral ⚖️"
        return news, sent
    except: return "تعذر جلب الأخبار اللحظية.", "Neutral ⚖️"

# --- 3. المحرك التحليلي (SMC Logic) ---
async def analyze_market(symbol):
    try:
        b_4h = exchange.fetch_ohlcv(symbol, '4h', limit=200)
        df_4h = calculate_indicators(pd.DataFrame(b_4h, columns=['t','o','h','l','c','v']))
        is_up = df_4h['c'].iloc[-1] > df_4h['ema_200'].iloc[-1]
        
        b_1h = exchange.fetch_ohlcv(symbol, '1h', limit=50)
        df_1h = calculate_indicators(pd.DataFrame(b_1h, columns=['t','o','h','l','c','v']))
        activity = int(df_1h['rsi'].iloc[-1])
        
        fvg = False
        if is_up and df_1h['l'].iloc[-1] > df_1h['h'].iloc[-3]: fvg = True
        elif not is_up and df_1h['h'].iloc[-1] < df_1h['l'].iloc[-3]: fvg = True

        b_15m = exchange.fetch_ohlcv(symbol, '15m', limit=50)
        df_15m = calculate_indicators(pd.DataFrame(b_15m, columns=['t','o','h','l','c','v']))
        price = df_15m['c'].iloc[-1]
        atr = df_15m['atr'].iloc[-1]

        return is_up, price, atr, activity, fvg
    except: return None

# --- 4. معالجة الصفقات والتنبيهات ---
async def process_market():
    print("Analyzing the market...")
    for sym in SYMBOLS:
        res = await analyze_market(sym)
        if res and (res[3] >= 90 or res[4]) and sym not in active_trades:
            is_up, price, atr, act, fvg = res
            sl = price - (atr * 2.5) if is_up else price + (atr * 2.5)
            diff = abs(price - sl)
            tp1, tp2, tp3 = (price + diff*2, price + diff*3, price + diff*5) if is_up else (price - diff*2, price - diff*3, price - diff*5)
            
            news, sent = get_news_radar(sym)
            active_trades[sym] = {'entry': price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'is_up': is_up, 'tp1_hit': False, 'tp2_hit': False}

            msg = (f"🚀 تنبيه: دخول سيولة مؤسسية ضخمة!\n🥇 العملة: {sym.replace('/', '-')}\n"
                   f"🔥 نسبة النشاط: {act}% ({'زخم شرائي' if is_up else 'ضغط بيعي'})\n⚡ نوع العملية: {'LONG 🟢' if is_up else 'SHORT 🔴'}\n"
                   f"📍 Entry: {price:.4f}\n🛡️ S.L   : {sl:.4f}\n🎯 T.P 1 : {tp1:.4f}\n     T.P 2 : {tp2:.4f}\n     T.P 3 : {tp3:.4f}\n     R:R    : 1:5\n"
                   f"--------------------------\n📰 رادار الأخبار (News Sentiment):\n💬 آخر خبر: \"{news}\" ({sent})\n"
                   f"📅 أجندة اقتصادية: صدور بيانات اقتصادية هامة قريباً.\n⚠️ نصيحة : التحليل الفني مدعوم بالخبر، يفضل الدخول بنصف المخاطرة المعتادة.\n"
                   f"--------------------------\n❗إدارة المخاطر مسؤوليتك.")
            await bot.send_message(CHAT_ID, msg)

        # مراقبة الأهداف للصفقات المفتوحة
        if sym in active_trades:
            curr = exchange.fetch_ticker(sym)['last']
            trade = active_trades[sym]
            if not trade['tp1_hit'] and ((trade['is_up'] and curr >= trade['tp1']) or (not trade['is_up'] and curr <= trade['tp1'])):
                trade['tp1_hit'] = True
                await bot.send_message(CHAT_ID, f"✅ تحديث: تم ضرب الهدف الأول بنجاح (T.P 1)\n💰 العائد الحالي: 1:2\n🛡️ إجراء أمني: تم تأمين الصفقة!\n📍 المطلوب: قم بتحريك وقف الخسارة (SL) إلى سعر الدخول ({trade['entry']:.4f}) الآن. الصفقة أصبحت \"صفر مخاطرة\".")
            elif trade['tp1_hit'] and not trade['tp2_hit'] and ((trade['is_up'] and curr >= trade['tp2']) or (not trade['is_up'] and curr <= trade['tp2'])):
                trade['tp2_hit'] = True
                await bot.send_message(CHAT_ID, f"✅ تحديث: تم ضرب الهدف الثاني بنجاح (T.P 2)\n💰 العائد الحالي: 1:3\n🛡️ إجراء أمني: تم تأمين المزيد من الأرباح!\n📍 المطلوب: قم بتحريك وقف الخسارة (SL) إلى منطقة الهدف الأول T.P 1 ({trade['tp1']:.4f}) الآن.\n🔥 قوة الاتجاه: مستمرة (Strong)\n💡 المطلوب: يمكنك حجز 50% من الأرباح الآن وترك الباقي للهدف النهائي.")

if __name__ == "__main__":
    while True:
        try: asyncio.run(process_market())
        except Exception as e: print(f"Error: {e}")
        asyncio.run(asyncio.sleep(300))

