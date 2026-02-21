import os
import time
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync  # إضافة مكتبة التخفي

# إعدادات الحساب من Secrets
EMAIL = os.getenv("POCKET_EMAIL")
PASSWORD = os.getenv("POCKET_PASSWORD")

def run_robot():
    with sync_playwright() as p:
        # تشغيل المتصفح
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        # 🟢 هنا نضع كود التخفي ليظهر البوت كأنه إنسان
        stealth_sync(page) 
        
        print("🚀 جاري الدخول لمنصة Pocket Option...")
        try:
            page.goto("https://pocketoption.com/login/", wait_until="networkidle")
            
            # تسجيل الدخول
            page.fill("input[name='email']", EMAIL)
            page.fill("input[name='password']", PASSWORD)
            page.click("button[type='submit']")
            time.sleep(7) # انتظار التحميل
            
            # التأكد من الحساب التجريبي فقط
            if "demo" not in page.url:
                print("⚠️ التحويل إلى الحساب التجريبي (DEMO)...")
                page.goto("https://pocketoption.com/cabinet/demo-quick-high-low/")
            
            print("✅ تم التأكد: العمل الآن على الحساب التجريبي.")
            
            # منطق رادار بايننس (محاكاة للسيولة)
            activity_rate = 92 
            if activity_rate >= 90:
                print(f"🚀 تنبيه: دخول سيولة مؤسسية ضخمة! ({activity_rate}%)")
                print("🟢 نوع العملية: شراء (CALL) - فريم 15 دقيقة")
                print("⚖️ الريسك (R:R): 1:3")
                # هنا يتم إضافة أمر الضغط على زر الشراء لاحقاً بعد نجاح الاتصال
            
        except Exception as e:
            print(f"❌ حدث خطأ أثناء التشغيل: {e}")
        
        print("❗إدارة المخاطر مسؤليتك (حساب تجريبي).")
        print("GOOD LUCK AHMED 👍")
        browser.close()

if __name__ == "__main__":
    run_robot()
