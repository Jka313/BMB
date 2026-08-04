import os
import re
import json
import base64
import logging

import requests
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─── الإعدادات ───────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

# Groq API
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_VISION_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_VISION_MODEL = "llama-3.2-90b-vision-preview"

# Google Sheets Scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Google Sheets Setup ─────────────────────────────────────────────────────
_credentials = None
_client = None

def get_credentials():
    global _credentials
    if _credentials is None:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            try:
                decoded = base64.b64decode(creds_json).decode('utf-8')
                creds_dict = json.loads(decoded)
            except:
                creds_dict = json.loads(creds_json)
            _credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            _credentials = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return _credentials

def get_sheet():
    global _client
    if _client is None:
        creds = get_credentials()
        _client = gspread.authorize(creds)
    return _client.open_by_key(GOOGLE_SHEET_ID).sheet1

# ─── Groq AI: استخراج الباركود من النص ──────────────────────────────────────
def extract_barcode_with_ai(text: str) -> str | None:
    system_prompt = (
        "You are a barcode extractor. Your ONLY job is to extract a 13-digit barcode number "
        "from the user's message. Return ONLY the 13-digit number, nothing else. "
        "If no valid 13-digit barcode is found, return exactly: NONE"
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
        "max_tokens": 50,
    }

    try:
        response = requests.post(GROQ_URL, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        barcode = result["choices"][0]["message"]["content"].strip()

        if barcode.upper() == "NONE":
            return None

        digits_only = re.sub(r"\D", "", barcode)
        if len(digits_only) == 13:
            return digits_only
        return None

    except Exception as e:
        logger.error(f"Groq API text error: {e}")
        return None


# ─── Groq Vision: قراءة الباركود من الصورة ─────────────────────────────────
def extract_barcode_from_image(image_bytes: bytes) -> str | None:
    image_b64 = base64.b64encode(image_bytes).decode('utf-8')

    system_prompt = (
        "You are a barcode reader. Look at the image and extract the 13-digit barcode number. "
        "Return ONLY the 13-digit number, nothing else. "
        "If no valid 13-digit barcode is visible, return exactly: NONE"
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": GROQ_VISION_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract the 13-digit barcode from this image:"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}"
                        }
                    }
                ]
            }
        ],
        "temperature": 0.0,
        "max_tokens": 50,
    }

    try:
        response = requests.post(GROQ_VISION_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        barcode = result["choices"][0]["message"]["content"].strip()

        if barcode.upper() == "NONE":
            return None

        digits_only = re.sub(r"\D", "", barcode)
        if len(digits_only) == 13:
            return digits_only
        return None

    except Exception as e:
        logger.error(f"Groq Vision API error: {e}")
        return None


# ─── البحث في Google Sheet ───────────────────────────────────────────────────
def search_product(barcode: str):
    try:
        sheet = get_sheet()
        all_data = sheet.get_all_records()

        for row in all_data:
            scancode = str(row.get("SCANCODE", "")).strip()
            if scancode == barcode:
                return row

        return None

    except Exception as e:
        logger.error(f"Google Sheet error: {e}")
        return None


# ─── تنسيق الرد ──────────────────────────────────────────────────────────────
def format_response(product: dict, barcode: str) -> str:
    conc_name = product.get("Conc Name", "غير متوفر")
    discount_type = product.get("Discount type", "غير متوفر")
    retail_price = product.get("RETAIL_PRICE", "غير متوفر")
    sale_price = product.get("SALE_PRICE", "غير متوفر")

    discount_percent = ""
    savings = ""
    try:
        if retail_price and sale_price:
            retail_str = str(retail_price).replace(",", "").strip()
            sale_str = str(sale_price).replace(",", "").strip()

            if retail_str and sale_str:
                orig = float(retail_str)
                sale = float(sale_str)
                if orig > 0 and sale > 0:
                    if orig != sale:
                        pct = round(((orig - sale) / orig) * 100, 1)
                        discount_percent = f" ({pct}% خصم)"
                        saved = orig - sale
                        savings = f"💵 توفير: <b>{saved:,.0f}</b> د.ع"
                    else:
                        discount_percent = " (بدون خصم)"
    except:
        pass

    response = f"""
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃        📦 تفاصيل المنتج          ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🏷️ <b>الباركود:</b> <code>{barcode}</code>
┃
┃ 📋 <b>الاسم:</b> {conc_name}
┃ 🏷️ <b>نوع الخصم:</b> {discount_type}
┃
┃ 💰 <b>السعر الأصلي:</b> <s>{retail_price} د.ع</s>
┃ 🏷️ <b>سعر البيع:</b> <b>{sale_price} د.ع</b>{discount_percent}
{savings}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
""".strip()
    return response


# ─── أوامر البوت ────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = """
👋 <b>أهلاً وسهلاً بيك في بوت بغداد مول!</b>

🤖 أنا بوت ذكي أقدر أساعدك تستعلم عن أسعار المنتجات بالباركود.

📌 <b>كيفية الاستخدام:</b>
• أرسل رقم الباركود مباشرة (13 رقم)
• أو أرسل رسالة تحتوي على الباركود
• أو <b>أرسل صورة</b> تحتوي على الباركود!

⚡️ راح أرد عليك فوراً بالسعر والخصم!

📊 <b>الأوامر المتوفرة:</b>
/start - بدء المحادثة
/help - المساعدة
/stats - إحصائيات المتجر
"""
    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
❓ <b>كيف تستخدم البوت؟</b>

1️⃣ <b>بالنص:</b> أرسل رقم الباركود (13 رقم)
   مثال: <code>1190000019999</code>

2️⃣ <b>برسالة:</b> أرسل أي رسالة تحتوي على الباركود
   مثال: "شنو سعر هذا 1190000019999"

3️⃣ <b>بالصورة:</b> أرسل صورة الباركود مباشرة!
   📸 البوت يقرأ الرقم من الصورة تلقائياً

💡 <b>نصائح:</b>
• البوت يفهم أي رسالة حتى لو مخربطة
• يستخدم ذكاء اصطناعي لاستخراج الباركود
• البيانات من ملف Google Sheet مباشرة
"""
    await update.message.reply_text(help_text, parse_mode="HTML")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sheet = get_sheet()
        all_data = sheet.get_all_records()
        total_products = len(all_data)

        discount_types = set()
        for row in all_data:
            dt = str(row.get("Discount type", "")).strip()
            if dt:
                discount_types.add(dt)

        discounted_count = 0
        for row in all_data:
            try:
                retail = str(row.get("RETAIL_PRICE", "")).replace(",", "").strip()
                sale = str(row.get("SALE_PRICE", "")).replace(",", "").strip()
                if retail and sale and float(retail) > float(sale):
                    discounted_count += 1
            except:
                pass

        stats_text = f"""
📊 <b>إحصائيات متجر بغداد مول</b>

📦 إجمالي المنتجات: <b>{total_products:,}</b>
🎁 منتجات بخصم: <b>{discounted_count:,}</b>
🏷️ أنواع الخصومات: <b>{len(discount_types)}</b>

📋 أنواع الخصومات المتاحة:
{chr(10).join(f"• {dt}" for dt in sorted(discount_types)[:10])}
"""
        await update.message.reply_text(stats_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Stats error: {e}")
        await update.message.reply_text("⚠️ حدث خطأ في جلب الإحصائيات")


# ─── معالجة الرسائل ──────────────────────────────────────────────────────────
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"

    logger.info(f"Text from {username} ({user_id}): {user_text}")
    await update.message.chat.send_action(action="typing")

    barcode = extract_barcode_with_ai(user_text)

    if not barcode:
        await update.message.reply_text(
            "❌ <b>ما لقيت باركود صحيح بالرسالة.</b>\n\n"
            "📌 تأكد إن الرقم مكون من <b>13 رقم</b>.\n"
            "🔄 جرب ترسله مباشرة بدون أي كلام.\n"
            "📸 أو أرسل <b>صورة</b> الباركود مباشرة!",
            parse_mode="HTML",
        )
        return

    await process_barcode(update, barcode)


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"

    logger.info(f"Photo from {username} ({user_id})")
    await update.message.chat.send_action(action="typing")

    photo = update.message.photo[-1]

    try:
        photo_file = await photo.get_file()
        image_bytes = await photo_file.download_as_bytearray()

        processing_msg = await update.message.reply_text(
            "📸 <b>جاري قراءة الباركود من الصورة...</b>",
            parse_mode="HTML"
        )

        barcode = extract_barcode_from_image(bytes(image_bytes))

        await processing_msg.delete()

        if not barcode:
            await update.message.reply_text(
                "❌ <b>ما قدرت أقرأ باركود واضح من الصورة.</b>\n\n"
                "📌 نصائح لصورة أفضل:\n"
                "• تأكد الباركود واضح ومرئي\n"
                "• خلفية الصورة نظيفة\n"
                "• الإضاءة كافية\n\n"
                "🔄 جرب ترسل صورة ثانية أو اكتب الرقم مباشرة.",
                parse_mode="HTML",
            )
            return

        await process_barcode(update, barcode)

    except Exception as e:
        logger.error(f"Photo processing error: {e}")
        await update.message.reply_text(
            "⚠️ حدث خطأ بمعالجة الصورة. جرب مرة ثانية أو ارسل الرقم نصياً."
        )


async def process_barcode(update: Update, barcode: str):
    product = search_product(barcode)

    if product:
        response = format_response(product, barcode)
        await update.message.reply_text(response, parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"🔍 <b>الباركود:</b> <code>{barcode}</code>\n\n"
            "❌ هذا المنتج <b>غير موجود</b> بقاعدة البيانات حالياً.\n\n"
            "📞 تواصل مع الإدارة لإضافته.",
            parse_mode="HTML",
        )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ حدث خطأ غير متوقع. جرب مرة ثانية أو تواصل مع الدعم."
        )


# ─── التشغيل الرئيسي (Polling - بدون Webhook) ───────────────────────────────
def main():
    required_vars = ["TELEGRAM_BOT_TOKEN", "GROQ_API_KEY", "GOOGLE_SHEET_ID"]
    missing = [v for v in required_vars if not os.environ.get(v)]

    if missing:
        logger.error(f"❌ ناقص متغيرات البيئة: {', '.join(missing)}")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_error_handler(error_handler)

    logger.info("🤖 بوت بغداد مول شغال...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
