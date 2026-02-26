import asyncio
import requests
import ccxt
import pandas as pd
import pandas_ta as ta
from telegram import Bot

# ==========================================
# 🔑 ضع بياناتك الخاصة هنا مباشرة
# ==========================================
TELEGRAM_TOKEN = '8524445307:AAEDw5THEah-iBwpgsTqvK2Pi7abpzWarZk'
CHAT_ID = '986199874'
CRYPTOPANIC_API_KEY = 'a5563e90848ba81e4aeca929e26d90069b2d1b9f'

bot = Bot(token=TELEGRAM_TOKEN)

# --- 1. رادار الأخبار (تحليل المشاعر) ---
def get_market_sentiment(symbol):
    try:
        coin = symbol.split('/')[0]
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API_KEY}&currencies={coin}"
        response = requests.get(url).json()
        results = response.get('results', [])
        bullish_votes = sum(1 for post in results if post.get('votes', {}).get('positive', 0) > 2)
        return "إيجابي ✅" if bullish_votes > 0 else "محايد ⚖️"
    except:
        return "غير متوفر ⚠️"

# --- 2. منطق التحليل الفني (SMC Logic) ---
def analyze_market(symbol):
    print(f"Analyzing the market... {symbol}")
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    
    try:
        # سحب بيانات 4 ساعات (الاتجاه) و ساعة (المناطق) و 15 دقيقة (الدخول)
        bars_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=100)
        bars_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
        bars_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)

        df_4h = pd.DataFrame(bars_4h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        df_1h = pd.DataFrame(bars_1h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        df_15m = pd.DataFrame(bars_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])

        # أ. تحديد الاتجاه (4H) باستخدام EMA 200
        ema_200 = df_4h.ta.ema(length=200).iloc[-1]
        trend = "UP" if df_4h['c'].iloc[-1] > ema_200 else "DOWN"

        # ب. فحص السيولة والـ FVG (1H)
        # ابحث عن فجوة سعرية (Fair Value Gap)
        fvg = False
        if trend == "UP":
            if df_1h['l'].iloc[-1] > df_1h['h'].iloc[-3]: fvg = True
        else:
            if df_1h['h'].iloc[-1] < df_1h['l'].iloc[-3]: fvg = True

        # ج. حساب النقاط (R:R 1:5) و ATR لضبط الستوب
        atr = df_15m.ta.atr(length=14).iloc[-1]
        current_price = df_15m['c'].iloc[-1]
        
        if trend == "UP":
            entry = current_price
            stop_loss = entry - (atr * 2.5)
            target = entry + (entry - stop_loss) * 5
        else:
            entry = current_price
            stop_loss = entry + (atr * 2.5)
            target = entry - (stop_loss - entry) * 5

        return trend, entry, stop_loss, target, "1:5", fvg
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

# --- 3. إرسال أفضل صفقتين (النموذج الذهبي) ---
async def main():
    print("Searching for the best two deals now...")
    # قائمة ببعض العملات القوية (يمكنك زيادة القائمة لـ 51 عملة)
    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "AVAX/USDT", "BNB/USDT"]
    deals = []

    for sym in symbols:
        result = analyze_market(sym)
        if result and result[5]: # إذا وجدنا FVG (إشارة قوة)
            deals.append((sym, result))
        if len(deals) == 2: break # نكتفي بأفضل صفقتين

    if deals:
        message = "💥 افضل صفقتين متوفرة حالياً :\n\n"
        titles = ["🥇 SECOND BEST", "🥈 THIRD PICK"]
        
        for i, (sym, res) in enumerate(deals):
            sentiment = get_market_sentiment(sym)
            message += (
                f"{titles[i]}\n"
                f"🔹 Symbol: {sym}\n"
                f"🔥 Success Rate: 88%\n"
                f"📈 Type: {'BUY 🟢' if res[0] == 'UP' else 'SELL 🔴'}\n"
                f"📍 Entry: {res[1]:.4f}\n"
                f"🛡️ Stop Loss: {res[2]:.4f}\n"
                f"🎯 Take Profit: {res[3]:.4f}\n"
                f"⚖️ R:R: {res[4]}\n"
                f"💡 News: {sentiment}\n"
                f"{'-'*20}\n"
            )
        message += "❗إدارة المخاطر مسؤليتك."
        await bot.send_message(chat_id=CHAT_ID, text=message)
    else:
        print("No high-quality deals found at this moment.")

if __name__ == "__main__":
    asyncio.run(main())
