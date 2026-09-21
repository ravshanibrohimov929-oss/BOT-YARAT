import os
import json
import asyncio
import logging
import uuid
import hmac
import hashlib
from urllib.parse import parse_qsl
import httpx
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from io import BytesIO
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, MenuButtonWebApp, WebAppInfo, LabeledPrice, PreCheckoutQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

MAIN_BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # bosh administrator (siz)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")  # masalan: ravshan_uzz (@ belgisiz)


def admin_contact_url() -> str:
    if ADMIN_USERNAME:
        return f"https://t.me/{ADMIN_USERNAME}"
    return f"tg://user?id={ADMIN_ID}"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
DATA_FILE = os.getenv("DATA_FILE_PATH", "bots_data.json")  # Railway Volume ulasangiz, masalan: /data/bots_data.json
TRIAL_DAYS = 7

logging.basicConfig(level=logging.INFO)

main_bot = Bot(token=MAIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
main_dp = Dispatcher(storage=MemoryStorage())

BOT_TYPES = {
    "kino": "🎬 Kino bot",
    "kino_pro": "🎬💎 Pro Kino Bot",
    "shop": "🛒 Savdo bot",
    "ai": "🤖 AI-yordamchi bot",
    "money": "💱 Pul (valyuta) bot",
    "translate": "🌐 Tarjimon bot",
    "taxi": "🚕 Taksi bot",
    "stars": "⭐ Stars sotish bot",
}

DEFAULT_PRICES = {
    "kino": 120_000,
    "kino_pro": 120_000,
    "ai": 120_000,
    "shop": 120_000,
    "money": 120_000,
    "translate": 120_000,
    "taxi": 120_000,
    "stars": 120_000,
}
DEFAULT_MONTHLY_RATE = 0.2  # keyingi oylar uchun narxning 20 foizi (standart) — eskirgan, endi ishlatilmaydi

DEFAULT_TARIFFS = {
    "1": {"name": "🚀 Start", "price": 10_000, "daily_limit": 500, "speed": "~0.5s"},
    "2": {"name": "⭐ Standard", "price": 20_000, "daily_limit": 1_000, "speed": "~0.4s"},
    "3": {"name": "💎 Pro 🔥", "price": 35_000, "daily_limit": 3_000, "speed": "~0.3s"},
    "4": {"name": "⚡ Turbo", "price": 50_000, "daily_limit": 7_500, "speed": "~0.2s"},
    "5": {"name": "♾️ Unlimited", "price": 80_000, "daily_limit": None, "speed": "~0s"},
}

DEFAULT_OTHER_BOT_PRICE = 35_000  # kino'dan boshqa barcha bot turlari uchun yagona oylik narx

running_bots = {}


MONGO_URI = os.getenv("MONGO_URI", "")
mongo_collection = None

if MONGO_URI:
    from pymongo import MongoClient
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
        mongo_client.admin.command("ping")  # ulanishni darhol sinab ko'ramiz
        mongo_db = mongo_client["botcreator"]
        mongo_collection = mongo_db["data"]
        logging.info("✅ MongoDB'ga muvaffaqiyatli ulanildi — ma'lumotlar doimiy saqlanadi.")
    except Exception as e:
        logging.error(f"❌ MongoDB'ga ulanib bo'lmadi, oddiy fayl ishlatiladi. Xato: {e}")
        mongo_collection = None
else:
    logging.warning("⚠️ MONGO_URI o'rnatilmagan — ma'lumotlar vaqtinchalik faylda saqlanadi.")


def load_data():
    if mongo_collection is not None:
        try:
            doc = mongo_collection.find_one({"_id": "main"})
            if doc:
                doc.pop("_id", None)
                return doc
            return {"bots": {}, "next_bot_id": 1}
        except Exception as e:
            logging.error(f"MongoDB'dan o'qishda xato: {e}")
            return {"bots": {}, "next_bot_id": 1}
    # Zaxira variant: MongoDB sozlanmagan bo'lsa, oddiy fayl orqali ishlaydi
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"bots": {}, "next_bot_id": 1}


import concurrent.futures

_save_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def save_data():
    if mongo_collection is not None:
        doc = dict(data)
        doc["_id"] = "main"

        def _write():
            try:
                mongo_collection.replace_one({"_id": "main"}, doc, upsert=True)
            except Exception as e:
                logging.error(f"MongoDB'ga yozishda xato: {e}")

        # MongoDB yozuvi orqa fonda (alohida thread'da) bajariladi —
        # shu tufayli asyncio event loop (va Mini App veb-serveri) bloklanmaydi.
        _save_executor.submit(_write)
        return
    # Zaxira variant
    data_dir = os.path.dirname(DATA_FILE)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


data = load_data()
data.setdefault("next_bot_id", 1)
data.setdefault("prices", dict(DEFAULT_PRICES))
data.setdefault("global_buttons", [])  # [{"label": "...", "response": "..."}]
data.setdefault("monthly_rate", DEFAULT_MONTHLY_RATE)
for _key, _val in DEFAULT_PRICES.items():
    data["prices"].setdefault(_key, _val)

# Platformaning Hisob to'ldirish (balans) tizimi
data.setdefault("user_balances", {})     # {str(uid): so'm}
data.setdefault("payment_systems", {})   # {psid: {"name","number","owner"}} — Hisob to'ldirish uchun
data.setdefault("stars_rate", 250)       # 1 ⭐ Stars narxi (so'mda, admin sozlashi mumkin — taxminiy boshlang'ich qiymat)

# Hamkor-adminlar (reseller) tizimi: birinchi oylik to'lov hamkorga, keyingilari platforma egasiga tushadi
data.setdefault("sub_admins", {})           # {str(uid): {"earnings": so'm}}
data.setdefault("user_referring_admin", {})  # {str(user_uid): sub_admin_uid}


def is_sub_admin(uid: int) -> bool:
    return uid == ADMIN_ID or str(uid) in data["sub_admins"]


def somz_to_stars(somz: int) -> int:
    rate = data.get("stars_rate", 250)
    return max(1, round(somz / rate))

# Har bir bot uchun 3 xil oylik tarif (narx + kunlik foydalanuvchi limiti)
# Kino bot uchun 5 xil oylik tarif (narx + kunlik foydalanuvchi limiti)
data.setdefault("tariffs", {tid: dict(t) for tid, t in DEFAULT_TARIFFS.items()})
for _tid, _t in DEFAULT_TARIFFS.items():
    data["tariffs"].setdefault(_tid, dict(_t))

# Kino'dan boshqa barcha bot turlari uchun yagona oylik narx (tarifsiz, cheksiz foydalanuvchi)
data.setdefault("other_bot_price", DEFAULT_OTHER_BOT_PRICE)

# RAVSHAN BUILDER BOTning to'liq nusxalari (klonlari) shu yerda ro'yxatga olinadi.
# Har biri: {"token": "...", "username": "...", "created_at": "..."}
data.setdefault("platform_clones", [])

# Bot Creator platformasining o'ziga /start bosgan barcha foydalanuvchilar
data.setdefault("platform_users", [])

running_platform_clones = {}  # token -> asyncio task


def get_price(bot_type: str) -> int:
    return data["prices"].get(bot_type, DEFAULT_PRICES.get(bot_type, 0))


def get_monthly_rate() -> float:
    return data.get("monthly_rate", DEFAULT_MONTHLY_RATE)


def get_tariff(tariff_id: str) -> dict:
    return data["tariffs"].get(tariff_id, DEFAULT_TARIFFS.get(tariff_id, DEFAULT_TARIFFS["2"]))


OTHER_BOT_TARIFF_NAME = "Standart"


def get_bot_tariff(info: dict) -> dict:
    if info.get("custom_price"):
        return {"name": "Maxsus", "price": info["custom_price"], "daily_limit": None}
    if info.get("type") in ("kino", "kino_pro"):
        return get_tariff(info.get("tariff", "2"))
    return {"name": OTHER_BOT_TARIFF_NAME, "price": data.get("other_bot_price", DEFAULT_OTHER_BOT_PRICE), "daily_limit": None}


def tariff_limit_text(t: dict) -> str:
    if t.get("daily_limit") is None:
        return "cheksiz foydalanuvchi"
    return f"kuniga {t['daily_limit']:,} tagacha foydalanuvchi"


def tariff_card_text(tid: str, t: dict) -> str:
    daily_price = t["price"] // 30
    if t.get("daily_limit") is None:
        users_line = "♾ Cheksiz foydalanuvchi kuniga"
    else:
        users_line = f"👥 {t['daily_limit']:,} ta foydalanuvchi kuniga"
    return (
        f"<b>{t['name']}</b>\n"
        f"┣ 💵 Narxi: {t['price']:,} so'm/oy ({daily_price:,} so'm/kun)\n"
        f"┗ {users_line}"
    )


# Har bir bot turi uchun tavsif (bot yaratish oynasida ko'rsatiladi)
BOT_DESCRIPTIONS = {
    "kino": (
        "<i>Ushbu tizim orqali siz kinolarni botga yuklaysiz va ularga maxsus kod "
        "biriktirasiz. Foydalanuvchilar shu kod orqali kinoni tez va oson yuklab "
        "olishlari mumkin.</i>\n\n"
        "🏷 Kategoriyalar, ⭐ tavsiya etilgan kontent, 📈 TOP reyting, baholash, 🎁 referal, "
        "🗓 rejalashtirilgan chiqarish, 👮 moderatorlar, 📣 reklama, "
        "👥 foydalanuvchilarni bloklash/qidirish, 📊 chuqur statistika va yana ko'p narsa.\n\n"
        "🔒 Tizim majburiy obuna (Telegram/Instagram/TikTok/YouTube/boshqa havola) "
        "va to'lov tizimlari imkoniyatlarini ham taqdim etadi.\n\n"
        "⚙️ Barcha boshqaruv admin panel orqali amalga oshiriladi."
    ),
    "kino_pro": (
        "<i>Kino botning barcha imkoniyatlari + VIP tizimi bilan.</i>\n\n"
        "🔒 VIP-maxsus kino qo'shish, faqat Premium foydalanuvchilarga ko'rinadigan yopiq "
        "kontent, 💎 VIP kinolar katalogi — bularning barchasi faqat Pro Kino Botda mavjud.\n\n"
        "🏷 Kategoriyalar, ⭐ tavsiyalar, 📈 TOP reyting, baholash, 🎁 referal, 🗓 rejalashtirilgan "
        "chiqarish, 👮 moderatorlar, 📣 reklama va yana ko'p narsa ham bor.\n\n"
        "⚙️ Barcha boshqaruv admin panel orqali amalga oshiriladi."
    ),
    "shop": (
        "<i>Ushbu bot orqali siz mahsulotlaringizni ro'yxatga olib, mijozlaringizga "
        "onlayn savdo qilishingiz mumkin.</i>\n\n"
        "🛍 Mijozlar mahsulotlarni ko'rib, savatchaga qo'shib, buyurtma berishlari mumkin.\n\n"
        "📊 Buyurtmalar va statistikani kuzatib borish, majburiy obuna qo'shish imkoniyati bor.\n\n"
        "⚙️ Barcha boshqaruv admin panel orqali amalga oshiriladi."
    ),
    "ai": (
        "<i>Foydalanuvchilar bilan sun'iy intellekt orqali suhbatlashadigan bot. "
        "Har qanday savolga tezkor va aqlli javob beradi.</i>\n\n"
        "🤖 Cheksiz mavzularda savol-javob, matnli yordam va maslahatlar.\n\n"
        "📊 Foydalanuvchilar statistikasi va majburiy obuna imkoniyati mavjud.\n\n"
        "⚙️ Barcha boshqaruv admin panel orqali amalga oshiriladi."
    ),
    "money": (
        "<i>Joriy valyuta kurslarini ko'rsatadigan bot.</i>\n\n"
        "💱 Dollar, Yevro va boshqa valyutalarning kursini bir zumda ko'rsatadi.\n\n"
        "📊 Foydalanuvchilar statistikasi va majburiy obuna imkoniyati mavjud.\n\n"
        "⚙️ Barcha boshqaruv admin panel orqali amalga oshiriladi."
    ),
    "translate": (
        "<i>Matnlarni turli tillarga tarjima qiladigan bot.</i>\n\n"
        "🌐 Foydalanuvchi matn yuboradi — bot kerakli tilga tezkor tarjima qiladi.\n\n"
        "📊 Foydalanuvchilar statistikasi va majburiy obuna imkoniyati mavjud.\n\n"
        "⚙️ Barcha boshqaruv admin panel orqali amalga oshiriladi."
    ),
    "taxi": (
        "<i>Mijozlar manzil va telefon raqamini yuborib, taksi chaqiradigan bot.</i>\n\n"
        "🚕 Buyurtma to'g'ridan-to'g'ri sizga (yoki haydovchilaringizga) yuboriladi.\n\n"
        "📊 Buyurtmalar statistikasi va majburiy obuna imkoniyati mavjud.\n\n"
        "⚙️ Barcha boshqaruv admin panel orqali amalga oshiriladi."
    ),
    "stars": (
        "<i>Mijozlaringiz Telegram Stars sotib olishi uchun mo'ljallangan bot.</i>\n\n"
        "⭐ Tayyor paketlar yoki mijoz o'zi kiritgan miqdorda Stars sotasiz.\n\n"
        "🧾 Buyurtmalar, foydalanuvchilar va statistikani to'liq boshqarish imkoniyati.\n\n"
        "⚙️ Barcha boshqaruv admin panel orqali amalga oshiriladi (20+ funksiya)."
    ),
}

def is_active(info: dict) -> bool:
    if info.get("admin_id") == ADMIN_ID:
        return True  # Platforma egasi yaratgan botlar — umrbod, hech qachon to'lov so'ralmaydi
    paid_until = info.get("paid_until")
    if paid_until and datetime.now() < datetime.fromisoformat(paid_until):
        return True
    created = datetime.fromisoformat(info["created_at"])
    return datetime.now() < created + timedelta(days=TRIAL_DAYS)


def next_payment_amount(info: dict) -> int:
    """Tarif narxi — har oy bir xil summa (chegirmasiz)."""
    return get_bot_tariff(info)["price"]


async def check_daily_limit(event, info: dict) -> bool:
    """True bo'lsa - foydalanish mumkin. False bo'lsa - kunlik limit tugagan."""
    uid = event.from_user.id
    if is_admin(info, uid):
        return True
    tariff = get_bot_tariff(info)
    limit = tariff.get("daily_limit")
    if limit is None:
        return True
    today = datetime.now().strftime("%Y-%m-%d")
    usage = info.setdefault("daily_usage", {"date": today, "users": []})
    if usage["date"] != today:
        usage["date"] = today
        usage["users"] = []
    if uid in usage["users"]:
        return True
    if len(usage["users"]) >= limit:
        text = (
            "🚧 <b>Kunlik foydalanuvchilar limiti tugadi.</b>\n\n"
            "Ertaga qayta urinib ko'ring, yoki bot egasi tarifni oshirsin."
        )
        if isinstance(event, CallbackQuery):
            await event.message.answer(text)
            await event.answer()
        else:
            await event.answer(text)
        return False
    usage["users"].append(uid)
    save_data()
    return True


async def ask_gemini_chat(contents: list) -> str:
    headers = {"x-goog-api-key": GEMINI_API_KEY, "content-type": "application/json"}
    payload = {"contents": contents}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(GEMINI_URL, headers=headers, json=payload)
        resp.raise_for_status()
        result = resp.json()
        return result["candidates"][0]["content"]["parts"][0]["text"]


async def ask_gemini(prompt: str) -> str:
    return await ask_gemini_chat([{"role": "user", "parts": [{"text": prompt}]}])


# ---------- Holatlar (FSM) ----------
class NewBotFlow(StatesGroup):
    waiting_token = State()
    waiting_tariff = State()


class NewPlatformFlow(StatesGroup):
    """RAVSHAN BUILDER BOTning to'liq nusxasini (klon) yaratish uchun — faqat ADMIN_ID."""
    waiting_token = State()


class EditPrice(StatesGroup):
    waiting_amount = State()


class BotCustomPrice(StatesGroup):
    waiting_price = State()


class NewTariffAdd(StatesGroup):
    waiting_name = State()
    waiting_price = State()
    waiting_limit = State()


class EditRate(StatesGroup):
    waiting_percent = State()


class EditStarsRate(StatesGroup):
    waiting_rate = State()


class ActivateFlow(StatesGroup):
    waiting_days = State()


class GlobalButtonAdd(StatesGroup):
    waiting_label = State()
    waiting_response = State()


class AddMovie(StatesGroup):
    waiting_code = State()
    waiting_desc = State()
    waiting_video = State()


class AddSeries(StatesGroup):
    waiting_code = State()
    waiting_title = State()
    waiting_desc = State()
    waiting_count = State()
    waiting_episode = State()


class AddChannel(StatesGroup):
    choosing_type = State()
    waiting_username = State()
    waiting_title = State()
    waiting_link = State()


class PaymentSystemAdd(StatesGroup):
    waiting_name = State()
    waiting_number = State()
    waiting_owner = State()


class PremiumTariffAdd(StatesGroup):
    waiting_name = State()
    waiting_days = State()
    waiting_price = State()


class PremiumPurchase(StatesGroup):
    waiting_check = State()


class PremiumGrant(StatesGroup):
    waiting_user = State()


class AppendEpisode(StatesGroup):
    waiting_video = State()


class ScheduleRelease(StatesGroup):
    waiting_datetime = State()


class ModeratorAdd(StatesGroup):
    waiting_id = State()


class TopUpFlow(StatesGroup):
    waiting_amount = State()
    waiting_check = State()


class AdminAddBalance(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()


class SubAdminAdd(StatesGroup):
    waiting_id = State()


MIN_TOPUP = 10_000
MAX_TOPUP = 150_000


class AddAdmin(StatesGroup):
    waiting_id = State()


class AddProduct(StatesGroup):
    waiting_name = State()
    waiting_price = State()


class EditProduct(StatesGroup):
    waiting_field = State()
    waiting_value = State()


class ProductPhoto(StatesGroup):
    waiting_photo = State()


class ShopCategoryAdd(StatesGroup):
    waiting_name = State()


class PromoCodeAdd(StatesGroup):
    waiting_code = State()
    waiting_percent = State()


class ShopModeratorAdd(StatesGroup):
    waiting_id = State()


class ShopSettingsFlow(StatesGroup):
    waiting_delivery_fee = State()
    waiting_vip_discount = State()
    waiting_referral_bonus = State()


class ShopUserSearch(StatesGroup):
    waiting_query = State()


class ShopBlockUser(StatesGroup):
    waiting_id = State()


class ShopUnblockUser(StatesGroup):
    waiting_id = State()


class Checkout(StatesGroup):
    waiting_address = State()
    waiting_phone = State()
    waiting_payment = State()


class CurrencyAdd(StatesGroup):
    waiting_code = State()
    waiting_rate = State()


class CurrencyUpdate(StatesGroup):
    waiting_rate = State()


class MoneyAmount(StatesGroup):
    waiting_amount = State()


class PostFlow(StatesGroup):
    waiting_text = State()
    waiting_confirm = State()


class TaxiOrder(StatesGroup):
    waiting_from = State()
    waiting_to = State()
    waiting_phone = State()


class StarPackageAdd(StatesGroup):
    waiting_stars = State()
    waiting_price = State()


class StarPackageEdit(StatesGroup):
    waiting_price = State()


class StarOrderCustom(StatesGroup):
    waiting_amount = State()


class StarOrderCheck(StatesGroup):
    waiting_check = State()


class StarSettings(StatesGroup):
    waiting_min = State()
    waiting_max = State()


class StarUserSearch(StatesGroup):
    waiting_query = State()


class StarBlockUser(StatesGroup):
    waiting_id = State()


class StarUnblockUser(StatesGroup):
    waiting_id = State()


class WelcomeFlow(StatesGroup):
    waiting_text = State()


def is_admin(info: dict, uid: int) -> bool:
    return uid in info.get("admin_ids", [info.get("admin_id")])


def admins_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="adm_add")],
        [InlineKeyboardButton(text="📋 Adminlar ro'yxati", callback_data="adm_list")],
        [InlineKeyboardButton(text="➖ Adminni o'chirish", callback_data="adm_del")],
    ])


def setup_admin_management(dp: Dispatcher, token: str):
    info = data["bots"][token]
    info.setdefault("admin_ids", [info.get("admin_id")])
    owner_id = info["admin_id"]

    @dp.message(Command("cancel"))
    async def cancel_cmd(message: Message, state: FSMContext):
        current = await state.get_state()
        if current is None:
            await message.answer("Bekor qilinadigan jarayon yo'q.")
            return
        await state.clear()
        await message.answer("❌ Jarayon bekor qilindi.")

    @dp.message(Command("admins"))
    @dp.message(F.text == "👤 Adminlar")
    async def admins_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("👤 Adminlar boshqaruvi:", reply_markup=admins_kb())

    @dp.callback_query(F.data == "adm_add")
    async def adm_add_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        await callback.message.answer("Yangi admin Telegram ID'ini yuboring (/myid orqali bilib olish mumkin):")
        await state.set_state(AddAdmin.waiting_id)
        await callback.answer()

    @dp.message(AddAdmin.waiting_id)
    async def adm_add_process(message: Message, state: FSMContext):
        try:
            new_id = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        if new_id not in info["admin_ids"]:
            info["admin_ids"].append(new_id)
            save_data()
        await message.answer(f"✅ Admin qo'shildi: {new_id}")
        await state.clear()

    @dp.callback_query(F.data == "adm_list")
    async def adm_list_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        lines = []
        for aid in info["admin_ids"]:
            tag = " (asosiy)" if aid == owner_id else ""
            lines.append(f"• {aid}{tag}")
        await callback.message.answer("👤 Adminlar:\n\n" + "\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "adm_del")
    async def adm_del_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        removable = [aid for aid in info["admin_ids"] if aid != owner_id]
        if not removable:
            await callback.message.answer("O'chirish uchun qo'shimcha admin yo'q.")
            await callback.answer()
            return
        buttons = [[InlineKeyboardButton(text=str(aid), callback_data=f"admdel_{aid}")] for aid in removable]
        await callback.message.answer("O'chirmoqchi bo'lgan adminni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("admdel_"))
    async def adm_delid_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        target_id = int(callback.data.split("_", 1)[1])
        if target_id in info["admin_ids"] and target_id != owner_id:
            info["admin_ids"].remove(target_id)
            save_data()
            await callback.message.answer(f"🗑 Admin o'chirildi: {target_id}")
        await callback.answer()


# ---------- Majburiy obuna (barcha botlar uchun umumiy) ----------
def channels_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="ch_add")],
        [InlineKeyboardButton(text="📋 Kanallar ro'yxati", callback_data="ch_list")],
        [InlineKeyboardButton(text="➖ Kanal o'chirish", callback_data="ch_del")],
    ])


SOCIAL_EMOJI = {
    "instagram": "📸",
    "tiktok": "🎵",
    "youtube": "▶️",
    "other": "🌐",
}


async def get_missing_channels(bot: Bot, channels: dict, user_id: int):
    """Faqat Telegram kanallar uchun haqiqiy obuna tekshiruvi mumkin.
    Instagram/TikTok/YouTube/Boshqa havola turlari Bot API orqali tekshirib bo'lmaydi,
    shuning uchun ular bloklovchi hisoblanmaydi — faqat reklama tugmasi sifatida ko'rsatiladi."""
    missing = []
    for chat_id, info in channels.items():
        if info.get("type", "telegram") != "telegram":
            continue
        try:
            member = await bot.get_chat_member(chat_id=int(chat_id), user_id=user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                missing.append(info)
        except Exception as e:
            logging.error(f"Obuna tekshirishda xato ({chat_id}): {e}")
            # Xatolik bo'lsa ham xavfsiz tomonni tanlaymiz — obuna talab qilinadi
            missing.append(info)
    return missing


def subscribe_kb(missing, channels: dict, show_premium: bool = False):
    buttons = [[InlineKeyboardButton(text=info["title"], url=f"https://t.me/{info['username'].lstrip('@')}")] for info in missing]
    # Instagram/TikTok/YouTube/Boshqa havola — tekshirib bo'lmaydi, shuning uchun har doim reklama sifatida qo'shiladi
    for info in channels.values():
        ctype = info.get("type", "telegram")
        if ctype != "telegram":
            emoji = SOCIAL_EMOJI.get(ctype, "🔗")
            buttons.append([InlineKeyboardButton(text=f"{emoji} {info['title']}", url=info["url"])])
    if show_premium:
        buttons.append([InlineKeyboardButton(text="💎 Premium (cheklovlarsiz foydalaning)", callback_data="buy_premium")])
    buttons.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def is_premium_active(info: dict, uid: int) -> bool:
    rec = info.get("premium_users", {}).get(str(uid))
    if not rec:
        return False
    try:
        return datetime.fromisoformat(rec["until"]) > datetime.now()
    except Exception:
        return False


async def require_subscription(event, info: dict, admin_id: int) -> bool:
    uid = event.from_user.id
    if is_admin(info, uid):
        return True
    if is_premium_active(info, uid):
        return True
    channels = info.get("channels", {})
    premium_required = info.get("premium_enabled", False) and bool(info.get("premium_tariffs"))
    missing = await get_missing_channels(event.bot, channels, uid) if channels else []

    if premium_required:
        # Premium yoqilgan bo'lsa, majburiy obunaga obuna bo'lish botni ochib bermaydi —
        # botdan foydalanish faqat Premium sotib olish orqali mumkin.
        kb = subscribe_kb(missing, channels, show_premium=True)
        if missing:
            text = "Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling, YOKI Premium sotib oling:"
        else:
            text = "💎 Botdan foydalanish uchun Premium sotib oling:"
        if isinstance(event, CallbackQuery):
            await event.message.answer(text, reply_markup=kb)
            await event.answer()
        else:
            await event.answer(text, reply_markup=kb)
        return False

    if missing:
        kb = subscribe_kb(missing, channels, show_premium=False)
        text = "Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling:"
        if isinstance(event, CallbackQuery):
            await event.message.answer(text, reply_markup=kb)
            await event.answer()
        else:
            await event.answer(text, reply_markup=kb)
        return False

    return True


async def check_active(event, info: dict, admin_id: int) -> bool:
    """True bo'lsa - bot ishlaydi. False bo'lsa - sinov tugagan / to'lov kerak."""
    if is_active(info):
        return await check_daily_limit(event, info)
    uid = event.from_user.id
    amount = next_payment_amount(info)
    tariff = get_bot_tariff(info)
    is_renewal = bool(info.get("paid_until"))
    kb = None
    if is_admin(info, uid):
        kb = contact_admin_kb()
        if is_renewal:
            text = (
                f"⏳ <b>Oylik to'lov muddati tugadi.</b>\n\n"
                f"Tarif: {tariff['name']} — <b>{amount:,} so'm/oy</b>.\n\n"
                "To'lovni amalga oshirish uchun administrator bilan bog'laning."
            )
        else:
            text = (
                f"⏳ <b>Bepul sinov muddati tugadi.</b>\n\n"
                f"Ushbu bot ({BOT_TYPES.get(info['type'])}) tarifi: {tariff['name']} — <b>{amount:,} so'm/oy</b>.\n\n"
                "To'lovni amalga oshirish uchun administrator bilan bog'laning."
            )
    else:
        text = "🚧 Bot vaqtincha ishlamayapti."
    if isinstance(event, CallbackQuery):
        await event.message.answer(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)
    return False


def setup_subscription_handlers(dp: Dispatcher, token: str, admin_id: int):
    info = data["bots"][token]
    info.setdefault("channels", {})

    def channel_type_kb():
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📢 Telegram kanal", callback_data="chtype_telegram"),
                InlineKeyboardButton(text="📸 Instagram", callback_data="chtype_instagram"),
            ],
            [
                InlineKeyboardButton(text="🎵 TikTok", callback_data="chtype_tiktok"),
                InlineKeyboardButton(text="▶️ YouTube", callback_data="chtype_youtube"),
            ],
            [InlineKeyboardButton(text="🌐 Boshqa havola", callback_data="chtype_other")],
        ])

    @dp.callback_query(F.data == "ch_add")
    async def ch_add_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        await callback.message.answer("Kanal turini tanlang:", reply_markup=channel_type_kb())
        await state.set_state(AddChannel.choosing_type)
        await callback.answer()

    @dp.callback_query(AddChannel.choosing_type, F.data.startswith("chtype_"))
    async def ch_type_chosen_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        ctype = callback.data.split("_", 1)[1]
        if ctype == "telegram":
            await callback.message.answer(
                "Kanal usernameni yuboring (masalan: @mening_kanalim).\n"
                "⚠️ Bot o'sha kanalda ADMIN bo'lishi shart!"
            )
            await state.set_state(AddChannel.waiting_username)
        else:
            await state.update_data(ch_type=ctype)
            await callback.message.answer("Kanal/sahifa nomini yuboring (masalan: Ravshan Media):")
            await state.set_state(AddChannel.waiting_title)
        await callback.answer()

    @dp.message(AddChannel.waiting_username)
    async def ch_add_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        username = message.text.strip()
        try:
            chat = await message.bot.get_chat(username)
            info["channels"][str(chat.id)] = {"type": "telegram", "username": username, "title": chat.title}
            save_data()
            await message.answer(f"✅ Qo'shildi: {chat.title}")

            # Bot o'sha kanalda ADMIN ekanligini darhol tekshiramiz
            try:
                bot_member = await message.bot.get_chat_member(chat_id=chat.id, user_id=message.bot.id)
                if bot_member.status not in ("administrator", "creator"):
                    await message.answer(
                        f"⚠️ <b>Diqqat!</b> Bot \"{chat.title}\" kanalida ADMIN emas.\n"
                        "Obuna tekshiruvi ishlashi uchun botni o'sha kanalga ADMIN qilib qo'ying!"
                    )
            except Exception:
                await message.answer(
                    f"⚠️ <b>Diqqat!</b> Bot \"{chat.title}\" kanalida ADMIN ekanligini tekshira olmadim.\n"
                    "Iltimos, botni o'sha kanalga ADMIN qilib qo'ying, aks holda obuna tekshiruvi ishlamaydi!"
                )
        except Exception as e:
            await message.answer(f"❌ Xatolik: kanal topilmadi.\n{e}")
        await state.clear()

    @dp.message(AddChannel.waiting_title)
    async def ch_add_title_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        title = message.text.strip()
        await state.update_data(ch_title=title)
        await state.set_state(AddChannel.waiting_link)
        await message.answer("Endi havolani (linkni) yuboring (masalan: https://instagram.com/...):")

    @dp.message(AddChannel.waiting_link)
    async def ch_add_link_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        url = message.text.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url
        fsm_data = await state.get_data()
        ctype = fsm_data.get("ch_type", "other")
        title = fsm_data.get("ch_title", "Havola")
        key = f"social_{uuid.uuid4().hex[:8]}"
        info["channels"][key] = {"type": ctype, "title": title, "url": url}
        save_data()
        await message.answer(f"✅ Qo'shildi: {title}")
        await state.clear()

    @dp.callback_query(F.data == "ch_list")
    async def ch_list_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["channels"]:
            await callback.message.answer("Hozircha majburiy kanallar yo'q.")
        else:
            lines = []
            for c in info["channels"].values():
                ctype = c.get("type", "telegram")
                if ctype == "telegram":
                    lines.append(f"• 📢 {c['title']} ({c['username']})")
                else:
                    emoji = SOCIAL_EMOJI.get(ctype, "🔗")
                    lines.append(f"• {emoji} {c['title']} ({c['url']})")
            await callback.message.answer("📋 Majburiy obuna kanallari:\n\n" + "\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "ch_del")
    async def ch_del_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["channels"]:
            await callback.message.answer("O'chirish uchun kanal yo'q.")
            await callback.answer()
            return
        buttons = [[InlineKeyboardButton(text=c["title"], callback_data=f"chdel_{cid}")] for cid, c in info["channels"].items()]
        await callback.message.answer("O'chirmoqchi bo'lgan kanalni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("chdel_"))
    async def ch_delid_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        cid = callback.data.split("_", 1)[1]
        removed = info["channels"].pop(cid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['title']}")
        await callback.answer()

    @dp.callback_query(F.data == "check_sub")
    async def check_sub_cb(callback: CallbackQuery):
        missing = await get_missing_channels(callback.bot, info["channels"], callback.from_user.id)
        premium_required = info.get("premium_enabled", False) and bool(info.get("premium_tariffs"))
        if missing:
            await callback.answer("Hali barcha kanallarga obuna bo'lmagansiz ❌", show_alert=True)
        elif premium_required and not is_premium_active(info, callback.from_user.id):
            await callback.answer(
                "✅ Kanallarga obuna bo'ldingiz, lekin botdan foydalanish uchun Premium sotib olishingiz kerak.",
                show_alert=True,
            )
        else:
            await callback.message.edit_text("✅ Obuna tasdiqlandi! Endi so'rovingizni qayta yuboring.")
            await callback.answer()

    @dp.message(Command("channels"))
    @dp.message(F.text == "📡 Majburiy obuna")
    async def channels_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("📡 Majburiy obuna boshqaruvi:", reply_markup=channels_admin_kb())


# ---------- Bosh (creator) bot — XALQ UCHUN OMMAVIY ----------
def types_kb():
    buttons = [[InlineKeyboardButton(text=name, callback_data=f"type_{key}")] for key, name in BOT_TYPES.items()]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def tariff_kb(only_ids=None):
    items = data["tariffs"].items()
    if only_ids:
        items = [(tid, t) for tid, t in items if tid in only_ids]
    buttons = [
        [InlineKeyboardButton(
            text=f"{t['name']} — {t['price']:,} so'm/oy ({tariff_limit_text(t)})",
            callback_data=f"tariff_{tid}",
        )]
        for tid, t in items
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def contact_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Admin bilan bog'lanish", url=admin_contact_url())]
    ])


def setup_platform_bot(dp: Dispatcher):
    """
    RAVSHAN BUILDER BOTning to'liq mantig'i shu yerda joylashgan: /start, /newbot,
    /prices, /globalbuttons, /mybots, to'lov tasdiqlash va h.k.

    Bu funksiya bitta Dispatcher (main_dp yoki klon bot dispatcheri)ga qo'llanadi.
    Shu tufayli /newplatform orqali yaratilgan har qanday klon — asl RAVSHAN BUILDER
    BOTning AYNAN o'zi kabi ishlaydi (bir xil narxlar, bir xil botlar bazasi,
    bir xil ADMIN_ID nazorati — chunki hammasi umumiy `data` obyektidan foydalanadi).
    """

    @dp.message(Command("cancel"))
    async def main_cancel(message: Message, state: FSMContext):
        current_state = await state.get_state()
        if current_state is None:
            await message.answer("Bekor qilinadigan jarayon yo'q.")
            return
        await state.clear()
        await message.answer("❌ Jarayon bekor qilindi.")

    @dp.message(Command("myid"))
    async def myid_handler(message: Message):
        await message.answer(f"Sizning Telegram ID'ingiz: <code>{message.from_user.id}</code>")

    def main_menu_kb(uid: int):
        keyboard = [
            [KeyboardButton(text="🤖 Bot yaratish"), KeyboardButton(text="📁 Botlarim")],
            [KeyboardButton(text="👤 Shaxsiy kabinet"), KeyboardButton(text="💰 Hisob to'ldirish")],
            [KeyboardButton(text="🎁 Referal"), KeyboardButton(text="🌐 Saytga kirish")],
            [KeyboardButton(text="📩 Murojaat"), KeyboardButton(text="📖 Qo'llanma")],
        ]
        if uid == ADMIN_ID:
            keyboard.append([KeyboardButton(text="📊 Statistika"), KeyboardButton(text="➕ Hisob qo'shish")])
            keyboard.append([KeyboardButton(text="💵 Tariflar"), KeyboardButton(text="💳 To'lov tizimlar")])
            keyboard.append([KeyboardButton(text="⭐ Stars kursi"), KeyboardButton(text="👥 Hamkor-adminlar")])
            keyboard.append([KeyboardButton(text="🤖 Botlar narxi")])
        elif str(uid) in data["sub_admins"]:
            keyboard.append([KeyboardButton(text="➕ Hisob qo'shish"), KeyboardButton(text="💼 Mening daromadim")])
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

    @dp.message(Command("start"))
    async def main_start(message: Message):
        uid = message.from_user.id
        if uid not in data["platform_users"]:
            data["platform_users"].append(uid)
            save_data()
        tariff_lines = "\n".join(
            f"💠 {t['name']} — {t['price']:,} so'm/oy ({tariff_limit_text(t)})"
            for t in data["tariffs"].values()
        )
        other_price = data.get("other_bot_price", DEFAULT_OTHER_BOT_PRICE)
        text = (
            "🤖 <b>Bot Creator</b> — Telegram botlar yaratish uchun qulay platforma\n\n"
            "Bu platforma orqali siz hech qanday kod yozmasdan o'z Telegram botlaringizni "
            "tez va oson yaratishingiz, ularni tahrirlashingiz hamda boshqarishingiz mumkin.\n\n"
            "⚡ <b>Nega aynan Bot Creator?</b>\n"
            "• Botlar muntazam yangilanib boriladi\n"
            "• Barqaror va mukammal ishlaydigan tizim\n"
            "• To'liq o'zbek tilidagi qulay interfeys\n"
            "• Doimiy va tezkor qo'llab-quvvatlash xizmati\n"
            "• Barcha jarayonlar avtomatik va tushunarli\n\n"
            "💳 <b>🎬 Kino bot tariflari:</b>\n"
            f"{tariff_lines}\n\n"
            f"💳 <b>Boshqa barcha bot turlari:</b> {other_price:,} so'm/oy\n\n"
            f"🎁 Har bir bot uchun {TRIAL_DAYS} kunlik BEPUL sinov muddati bor!\n\n"
            "Pastdagi menyudan foydalaning 👇"
        )
        await message.answer(text, reply_markup=main_menu_kb(message.from_user.id))

    @dp.message(F.text == "📖 Qo'llanma")
    async def guide_handler(message: Message):
        await message.answer(
            "📖 <b>Qo'llanma</b>\n\n"
            "1️⃣ \"🤖 Bot yaratish\" tugmasini bosing\n"
            "2️⃣ @BotFather orqali yangi bot yarating va tokenini shu yerga yuboring\n"
            "3️⃣ Bot turini tanlang (Kino, Savdo, Taksi va h.k.)\n"
            f"4️⃣ {TRIAL_DAYS} kunlik bepul sinovdan foydalaning\n"
            "5️⃣ Sinov tugagach, \"💰 Hisob to'ldirish\" orqali balansingizni to'ldirib, botingizni faollashtiring\n\n"
            "❓ Savollaringiz bo'lsa — \"📩 Murojaat\" tugmasini bosing."
        )

    @dp.message(F.text == "📩 Murojaat")
    async def murojaat_handler(message: Message):
        await message.answer("Administrator bilan bog'lanish uchun quyidagi tugmani bosing 👇", reply_markup=contact_admin_kb())

    @dp.message(F.text == "🌐 Saytga kirish")
    async def website_handler(message: Message):
        miniapp_url = get_miniapp_url()
        if not miniapp_url:
            await message.answer("🌐 Sayt hozircha sozlanmagan. Birozdan so'ng qayta urinib ko'ring.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Saytni ochish", web_app=WebAppInfo(url=miniapp_url))]
        ])
        await message.answer("Botlaringiz va balansingizni ko'rish uchun quyidagi tugmani bosing 👇", reply_markup=kb)

    @dp.message(F.text == "🎁 Referal")
    async def referral_handler(message: Message):
        await message.answer(
            "🎁 <b>Referal tizimi</b>\n\n"
            "Bu funksiya hozircha ishlab chiqilmoqda. Tez orada do'stlaringizni taklif qilib, "
            "bonuslar olish imkoniyati qo'shiladi!"
        )

    @dp.message(F.text == "👤 Shaxsiy kabinet")
    async def cabinet_handler(message: Message):
        uid = message.from_user.id
        balance = data["user_balances"].get(str(uid), 0)
        bot_count = sum(1 for i in data["bots"].values() if uid in i.get("admin_ids", [i["admin_id"]]))
        await message.answer(
            "👤 <b>Shaxsiy kabinet</b>\n\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"💰 Balans: {balance:,} so'm\n"
            f"🤖 Botlaringiz soni: {bot_count}"
        )

    # ---------- Hisob to'ldirish (balans) ----------
    def platform_payment_systems_kb():
        buttons = [[InlineKeyboardButton(text="➕ To'lov tizimi qo'shish", callback_data="pps_add")]]
        if data["payment_systems"]:
            buttons.append([InlineKeyboardButton(text="📋 Ro'yxat", callback_data="pps_list")])
            buttons.append([InlineKeyboardButton(text="➖ O'chirish", callback_data="pps_del")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @dp.message(F.text == "💳 To'lov tizimlar")
    async def platform_payment_systems_panel(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        if not data["payment_systems"]:
            await message.answer("⚠️ To'lov tizimlari mavjud emas.", reply_markup=platform_payment_systems_kb())
        else:
            await message.answer("💳 To'lov tizimlari boshqaruvi:", reply_markup=platform_payment_systems_kb())

    @dp.callback_query(F.data == "pps_add")
    async def pps_add_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        await callback.message.answer("Iltimos, to'lov tizimi nomini kiriting:\n\n(Masalan: Click, Payme, Humo, Uzcard...)")
        await state.set_state(PaymentSystemAdd.waiting_name)
        await callback.answer()

    @dp.message(PaymentSystemAdd.waiting_name)
    async def pps_name_process(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        await state.update_data(ps_name=message.text.strip())
        await message.answer("Iltimos, to'lov tizimi raqamini kiriting:\n\n(Masalan: karta yoki hisob raqami)")
        await state.set_state(PaymentSystemAdd.waiting_number)

    @dp.message(PaymentSystemAdd.waiting_number)
    async def pps_number_process(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        await state.update_data(ps_number=message.text.strip())
        await message.answer("Hisob raqami egasining to'liq ismini kiriting:\n\n(Masalan: Ism Familiya)")
        await state.set_state(PaymentSystemAdd.waiting_owner)

    @dp.message(PaymentSystemAdd.waiting_owner)
    async def pps_owner_process(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        fsm_data = await state.get_data()
        psid = uuid.uuid4().hex[:8]
        data["payment_systems"][psid] = {
            "name": fsm_data.get("ps_name", "-"),
            "number": fsm_data.get("ps_number", "-"),
            "owner": message.text.strip(),
        }
        save_data()
        await message.answer("✅ To'lov tizimi qo'shildi!")
        await state.clear()

    @dp.callback_query(F.data == "pps_list")
    async def pps_list_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        if not data["payment_systems"]:
            await callback.message.answer("To'lov tizimlari mavjud emas.")
        else:
            lines = [f"• {p['name']} — {p['number']} ({p['owner']})" for p in data["payment_systems"].values()]
            await callback.message.answer("💳 To'lov tizimlari:\n\n" + "\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "pps_del")
    async def pps_del_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        if not data["payment_systems"]:
            await callback.message.answer("O'chirish uchun to'lov tizimi yo'q.")
            await callback.answer()
            return
        buttons = [[InlineKeyboardButton(text=p["name"], callback_data=f"ppsdel_{pid}")] for pid, p in data["payment_systems"].items()]
        await callback.message.answer("O'chirmoqchi bo'lgan to'lov tizimini tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("ppsdel_"))
    async def pps_delid_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        pid = callback.data.split("_", 1)[1]
        removed = data["payment_systems"].pop(pid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['name']}")
        await callback.answer()

    @dp.message(F.text == "💰 Hisob to'ldirish")
    async def topup_start(message: Message, state: FSMContext):
        await message.answer(
            f"💰 Hisobni to'ldirish uchun summani kiriting.\n\n"
            f"Minimal: {MIN_TOPUP:,} so'm\n"
            f"Maksimal: {MAX_TOPUP:,} so'm"
        )
        await state.set_state(TopUpFlow.waiting_amount)

    @dp.message(TopUpFlow.waiting_amount)
    async def topup_amount_process(message: Message, state: FSMContext):
        try:
            amount = int(message.text.strip().replace(" ", ""))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        if amount < MIN_TOPUP or amount > MAX_TOPUP:
            await message.answer(f"❌ Summa {MIN_TOPUP:,} so'mdan {MAX_TOPUP:,} so'mgacha bo'lishi kerak.")
            return
        await state.update_data(topup_amount=amount)
        stars = somz_to_stars(amount)
        buttons = [[InlineKeyboardButton(text=f"⭐ {stars} Stars orqali to'lash", callback_data=f"topupstars_{amount}")]]
        buttons += [[InlineKeyboardButton(text=p["name"], callback_data=f"topuppay_{pid}")] for pid, p in data["payment_systems"].items()]
        await message.answer(
            f"💰 Summa: {amount:,} so'm (~{stars} ⭐)\n\n💳 To'lov tizimini tanlang:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @dp.callback_query(F.data.startswith("topupstars_"))
    async def topup_stars_cb(callback: CallbackQuery):
        amount = int(callback.data.split("_", 1)[1])
        stars = somz_to_stars(amount)
        await callback.bot.send_invoice(
            chat_id=callback.from_user.id,
            title="💰 Hisobni to'ldirish",
            description=f"Balansga {amount:,} so'm qo'shish",
            payload=f"topup_{amount}",
            currency="XTR",
            prices=[LabeledPrice(label="Hisob to'ldirish", amount=stars)],
        )
        await callback.answer()

    @dp.pre_checkout_query()
    async def platform_stars_pre_checkout(pre_checkout_query: PreCheckoutQuery):
        await pre_checkout_query.answer(ok=True)

    @dp.message(F.successful_payment)
    async def platform_stars_payment_success(message: Message):
        payload = message.successful_payment.invoice_payload
        if not payload.startswith("topup_"):
            return
        amount = int(payload.split("_", 1)[1])
        key = str(message.from_user.id)
        data["user_balances"][key] = data["user_balances"].get(key, 0) + amount
        save_data()
        await message.answer(
            f"✅ <b>To'lov muvaffaqiyatli qabul qilindi!</b>\n\n"
            f"Hisobingizga {amount:,} so'm qo'shildi.\n"
            f"💰 Joriy balans: {data['user_balances'][key]:,} so'm"
        )

    @dp.callback_query(F.data.startswith("topuppay_"))
    async def topup_payment_chosen_cb(callback: CallbackQuery, state: FSMContext):
        pid = callback.data.split("_", 1)[1]
        psys = data["payment_systems"].get(pid)
        if not psys:
            await callback.answer("❌ Ma'lumot topilmadi, qaytadan urinib ko'ring.", show_alert=True)
            return
        fsm_data = await state.get_data()
        amount = fsm_data.get("topup_amount", 0)
        await state.set_state(TopUpFlow.waiting_check)
        text = (
            f"💳 <b>{psys['name']}</b>\n\n"
            f"🔢 Raqami: <code>{psys['number']}</code>\n"
            f"👤 Egasi: {psys['owner']}\n\n"
            f"💰 To'lov summasi: {amount:,} so'm\n\n"
            "To'lovni amalga oshirgach, to'lov chekini (skrinshot) shu yerga yuboring."
        )
        await callback.message.answer(text)
        await callback.answer()

    @dp.message(TopUpFlow.waiting_check, F.photo)
    async def topup_check_received(message: Message, state: FSMContext):
        fsm_data = await state.get_data()
        amount = fsm_data.get("topup_amount", 0)
        uid = message.from_user.id
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        caption = (
            "🧾 <b>Yangi Hisob to'ldirish so'rovi</b>\n\n"
            f"💰 Summa: {amount:,} so'm\n"
            f"👤 Foydalanuvchi: {uname} (ID: <code>{uid}</code>)"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"topupapprove_{uid}_{amount}"),
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"topupreject_{uid}"),
        ]])
        try:
            await message.bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=caption, reply_markup=kb)
        except Exception as e:
            logging.error(f"Adminga chek yuborishda xato: {e}")
        await message.answer(
            "✅ Chekingiz qabul qilindi!\n\n"
            "Administrator tomonidan tez orada ko'rib chiqiladi. Tasdiqlansa, balansingizga mablag' qo'shiladi."
        )
        await state.clear()

    @dp.callback_query(F.data.startswith("topupapprove_"))
    async def topup_approve_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        _, target_uid, amount = callback.data.split("_", 2)
        amount = int(amount)
        key = str(target_uid)
        data["user_balances"][key] = data["user_balances"].get(key, 0) + amount
        save_data()
        try:
            await callback.bot.send_message(
                chat_id=int(target_uid),
                text=(
                    "✅ <b>Chekingiz qabul qilindi!</b>\n\n"
                    f"Hisobingizga {amount:,} so'm qo'shildi.\n"
                    f"💰 Joriy balans: {data['user_balances'][key]:,} so'm"
                ),
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga xabar yuborishda xato: {e}")
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ <b>TASDIQLANDI</b>")
        await callback.answer()

    @dp.callback_query(F.data.startswith("topupreject_"))
    async def topup_reject_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        target_uid = int(callback.data.split("_", 1)[1])
        try:
            await callback.bot.send_message(
                chat_id=target_uid,
                text="❌ <b>To'lovingiz administrator tomonidan bekor qilindi.</b>\n\nAgar savollaringiz bo'lsa, murojaat qiling.",
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga xabar yuborishda xato: {e}")
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ <b>BEKOR QILINDI</b>")
        await callback.answer()

    # ---------- Admin: Statistika va Hisob qo'shish ----------
    @dp.message(F.text == "📊 Statistika")
    async def platform_stats(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        total_bots = len(data["bots"])
        active_bots = sum(1 for i in data["bots"].values() if is_active(i))
        total_balance = sum(data["user_balances"].values())
        await message.answer(
            "📊 <b>Platforma statistikasi</b>\n\n"
            f"👤 Bot Creator'ga kirgan odamlar: {len(data['platform_users']):,}\n\n"
            f"🤖 Jami botlar: {total_bots}\n"
            f"🟢 Faol botlar: {active_bots}\n"
            f"👥 Balansi bor foydalanuvchilar: {len(data['user_balances'])}\n"
            f"💰 Tizimdagi jami balans: {total_balance:,} so'm"
        )

    @dp.message(F.text == "➕ Hisob qo'shish")
    async def admin_add_balance_start(message: Message, state: FSMContext):
        if not is_sub_admin(message.from_user.id):
            return
        await message.answer("Foydalanuvchi ID raqamini kiriting:")
        await state.set_state(AdminAddBalance.waiting_user_id)

    @dp.message(AdminAddBalance.waiting_user_id)
    async def admin_add_balance_uid(message: Message, state: FSMContext):
        if not is_sub_admin(message.from_user.id):
            return
        try:
            target_uid = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        await state.update_data(target_uid=target_uid)
        await message.answer("Qo'shiladigan summani kiriting (so'mda):")
        await state.set_state(AdminAddBalance.waiting_amount)

    @dp.message(AdminAddBalance.waiting_amount)
    async def admin_add_balance_amount(message: Message, state: FSMContext):
        uid = message.from_user.id
        if not is_sub_admin(uid):
            return
        try:
            amount = int(message.text.strip().replace(" ", ""))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting.")
            return
        fsm_data = await state.get_data()
        target_uid = fsm_data.get("target_uid")
        key = str(target_uid)
        data["user_balances"][key] = data["user_balances"].get(key, 0) + amount
        if uid != ADMIN_ID and key not in data["user_referring_admin"]:
            data["user_referring_admin"][key] = uid
        save_data()
        await message.answer(f"✅ {target_uid} ID'li foydalanuvchiga {amount:,} so'm qo'shildi.\n💰 Yangi balans: {data['user_balances'][key]:,} so'm")
        try:
            await message.bot.send_message(
                chat_id=target_uid,
                text=f"✅ Hisobingizga administrator tomonidan {amount:,} so'm qo'shildi.\n💰 Joriy balans: {data['user_balances'][key]:,} so'm",
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga xabar yuborishda xato: {e}")
        await state.clear()

    # ---------- Hamkor-adminlar (reseller) ----------
    @dp.message(F.text == "👥 Hamkor-adminlar")
    async def sub_admins_panel(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        buttons = [
            [InlineKeyboardButton(text="➕ Hamkor qo'shish", callback_data="subadmin_add")],
            [InlineKeyboardButton(text="📋 Ro'yxat", callback_data="subadmin_list")],
        ]
        if data["sub_admins"]:
            buttons.append([InlineKeyboardButton(text="➖ O'chirish", callback_data="subadmin_del")])
        await message.answer(
            "👥 <b>Hamkor-adminlar</b>\n\n"
            "Hamkor-adminlar foydalanuvchilarga balans qo'sha oladi. Ular qo'shgan foydalanuvchining "
            "birinchi oylik to'lovi hamkor-adminning daromadiga yoziladi, keyingi oylardan boshlab "
            "to'lov sizga (platforma egasiga) tushadi.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @dp.callback_query(F.data == "subadmin_add")
    async def subadmin_add_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        await callback.message.answer("Hamkor-admin qilmoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(SubAdminAdd.waiting_id)
        await callback.answer()

    @dp.message(SubAdminAdd.waiting_id)
    async def subadmin_add_save(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        data["sub_admins"].setdefault(str(target), {"earnings": 0})
        save_data()
        await message.answer(f"✅ {target} hamkor-admin qilib tayinlandi.")
        try:
            await message.bot.send_message(
                target,
                "🎉 Sizga Bot Creator platformasida hamkor-admin huquqi berildi!\n\n"
                "Endi \"➕ Hisob qo'shish\" tugmasi orqali foydalanuvchilarga balans qo'sha olasiz, "
                "va ular to'lagan birinchi oylik to'lov sizning daromadingizga yoziladi.",
            )
        except Exception:
            pass
        await state.clear()

    @dp.callback_query(F.data == "subadmin_list")
    async def subadmin_list_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        if not data["sub_admins"]:
            await callback.message.answer("Hamkor-adminlar yo'q.")
        else:
            lines = [f"• ID {uid} — daromad: {s['earnings']:,} so'm" for uid, s in data["sub_admins"].items()]
            await callback.message.answer("👥 Hamkor-adminlar:\n\n" + "\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "subadmin_del")
    async def subadmin_del_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        if not data["sub_admins"]:
            await callback.answer("Hamkor-adminlar yo'q.", show_alert=True)
            return
        buttons = [[InlineKeyboardButton(text=uid, callback_data=f"subadmindel_{uid}")] for uid in data["sub_admins"]]
        await callback.message.answer("O'chirmoqchi bo'lgan hamkor-adminni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("subadmindel_"))
    async def subadmin_delid_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        target = callback.data.split("_", 1)[1]
        data["sub_admins"].pop(target, None)
        save_data()
        await callback.message.answer(f"➖ {target} hamkor-adminlikdan olib tashlandi.")
        await callback.answer()

    @dp.message(F.text == "💼 Mening daromadim")
    async def my_earnings(message: Message):
        uid = str(message.from_user.id)
        sub = data["sub_admins"].get(uid)
        if not sub:
            return
        await message.answer(f"💼 <b>Mening daromadim</b>\n\n💰 Jami: {sub['earnings']:,} so'm")

    def type_detail_text(bot_type: str) -> str:
        desc = BOT_DESCRIPTIONS.get(bot_type, "")
        if bot_type == "kino":
            price_line = "💰 Oylik to'lov: tarifga qarab belgilanadi"
        else:
            price_line = f"💰 Oylik to'lov: {data.get('other_bot_price', DEFAULT_OTHER_BOT_PRICE):,} so'm/oy"
        return (
            f"{BOT_TYPES[bot_type]}\n\n"
            f"{desc}\n\n"
            f"💵 Yaratish narxi: 0 so'm\n"
            f"{price_line}\n"
            f"🎁 Bepul sinov muddati: {TRIAL_DAYS} kun"
        )

    def type_detail_kb(bot_type: str):
        buttons = []
        if bot_type == "kino":
            buttons.append([InlineKeyboardButton(text="💳 Tariflar ro'yxati", callback_data=f"tariffpreview_{bot_type}")])
        buttons.append([InlineKeyboardButton(text="✅ Bot yaratish — Bepul", callback_data=f"createbot_{bot_type}")])
        buttons.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="backtotypes")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def tariff_preview_text(bot_type: str) -> str:
        cards = "\n\n".join(tariff_card_text(tid, t) for tid, t in data["tariffs"].items())
        return f"{BOT_TYPES[bot_type]} — Tariflar\n\n{cards}"

    def tariff_preview_kb(bot_type: str):
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Orqaga", callback_data=f"backtotype_{bot_type}")]])

    @dp.message(Command("newbot"))
    @dp.message(F.text == "🤖 Bot yaratish")
    async def newbot_start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("🤖 Quyidagi bot turlaridan birini tanlang:", reply_markup=types_kb())

    @dp.callback_query(F.data == "backtotypes")
    async def back_to_types_cb(callback: CallbackQuery):
        await callback.message.edit_text("🤖 Quyidagi bot turlaridan birini tanlang:", reply_markup=types_kb())
        await callback.answer()

    @dp.callback_query(F.data.startswith("type_"))
    async def newbot_type(callback: CallbackQuery):
        bot_type = callback.data.split("_", 1)[1]
        await callback.message.edit_text(type_detail_text(bot_type), reply_markup=type_detail_kb(bot_type))
        await callback.answer()

    @dp.callback_query(F.data.startswith("backtotype_"))
    async def back_to_type_cb(callback: CallbackQuery):
        bot_type = callback.data.split("_", 1)[1]
        await callback.message.edit_text(type_detail_text(bot_type), reply_markup=type_detail_kb(bot_type))
        await callback.answer()

    @dp.callback_query(F.data.startswith("tariffpreview_"))
    async def tariff_preview_cb(callback: CallbackQuery):
        bot_type = callback.data.split("_", 1)[1]
        await callback.message.edit_text(tariff_preview_text(bot_type), reply_markup=tariff_preview_kb(bot_type))
        await callback.answer()

    @dp.callback_query(F.data.startswith("createbot_"))
    async def createbot_cb(callback: CallbackQuery, state: FSMContext):
        bot_type = callback.data.split("_", 1)[1]
        await state.update_data(bot_type=bot_type)
        await state.set_state(NewBotFlow.waiting_token)
        await callback.message.edit_text(
            f"{BOT_TYPES[bot_type]}\n\n"
            "Yangi bot tokenini yuboring.\n"
            "(@BotFather orqali /newbot bilan yaratib, tokenni shu yerga joylashtiring)"
        )
        await callback.answer()

    async def finalize_bot_creation(token: str, bot_name: str, bot_type: str, uid: int, tariff_id):
        bot_id = data["next_bot_id"]
        data["next_bot_id"] += 1

        today = datetime.now().strftime("%Y-%m-%d")
        data["bots"][token] = {
            "id": bot_id,
            "type": bot_type,
            "name": bot_name,
            "admin_id": uid,
            "admin_ids": [uid],
            "created_at": datetime.now().isoformat(),
            "paid_until": None,
            "tariff": tariff_id,
            "daily_usage": {"date": today, "users": []},
            "movies": {},
            "products": {},
            "next_id": 1,
            "carts": {},
            "channels": {},
            "users": [],
            "stats": {},
        }
        save_data()
        await start_child_bot(token, bot_type)
        return data["bots"][token]

    @dp.message(NewBotFlow.waiting_token)
    async def newbot_token(message: Message, state: FSMContext):
        token = message.text.strip()
        try:
            test_bot = Bot(token=token)
            me = await test_bot.get_me()
            await test_bot.session.close()
        except Exception:
            await message.answer("❌ Token noto'g'ri. Qaytadan yuboring.")
            return

        state_data = await state.get_data()
        bot_type = state_data.get("bot_type")
        if not bot_type:
            await message.answer("Xatolik: qaytadan \"🤖 Bot yaratish\" bosing.")
            await state.clear()
            return

        if bot_type not in ("kino", "kino_pro"):
            # Kino'dan boshqa botlar — tarifsiz, yagona narx bilan yaratiladi
            info = await finalize_bot_creation(token, me.first_name, bot_type, message.from_user.id, None)
            tariff = get_bot_tariff(info)
            price_note = f"💰 Oylik narx: {tariff['price']:,} so'm/oy\n"
            go_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤖 Botga o'tish", url=f"https://t.me/{me.username}")]]) if me.username else None
            await message.answer(
                f"✅ {BOT_TYPES[bot_type]} ishga tushdi: <b>{me.first_name}</b>\n\n"
                f"{price_note}"
                f"🎁 {TRIAL_DAYS} kunlik bepul sinov boshlandi!\n"
                "Majburiy obuna qo'shish uchun o'sha botga /channels yozing.",
                reply_markup=go_kb,
            )
            await state.clear()
            return

        await state.update_data(token=token, bot_name=me.first_name, bot_username=me.username)
        await state.set_state(NewBotFlow.waiting_tariff)
        await message.answer(
            f"✅ Bot topildi: <b>{me.first_name}</b>\n\n{BOT_TYPES[bot_type]} uchun tarifni tanlang:",
            reply_markup=tariff_kb(),
        )

    @dp.callback_query(NewBotFlow.waiting_tariff, F.data.startswith("tariff_"))
    async def newbot_tariff(callback: CallbackQuery, state: FSMContext):
        tariff_id = callback.data.split("_", 1)[1]
        state_data = await state.get_data()
        token = state_data.get("token")
        bot_name = state_data.get("bot_name")
        bot_type = state_data.get("bot_type")
        bot_username = state_data.get("bot_username")

        if not token or not bot_type:
            await callback.answer("Xatolik: qaytadan \"🤖 Bot yaratish\" bosing.", show_alert=True)
            return

        info = await finalize_bot_creation(token, bot_name, bot_type, callback.from_user.id, tariff_id)

        tariff = get_tariff(tariff_id)
        go_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤖 Botga o'tish", url=f"https://t.me/{bot_username}")]]) if bot_username else None
        await callback.message.edit_text(
            f"✅ {BOT_TYPES[bot_type]} ishga tushdi: <b>{bot_name}</b>\n\n"
            f"💠 Tarif: {tariff['name']} — {tariff['price']:,} so'm/oy ({tariff_limit_text(tariff)})\n"
            f"🎁 {TRIAL_DAYS} kunlik bepul sinov boshlandi!\n"
            "Majburiy obuna qo'shish uchun o'sha botga /channels yozing.",
            reply_markup=go_kb,
        )
        await state.clear()
        await callback.answer()

    @dp.message(Command("prices"))
    @dp.message(F.text == "💵 Tariflar")
    async def prices_panel(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        buttons = [
            [InlineKeyboardButton(
                text=f"{t['name']} — {t['price']:,} so'm/oy ({tariff_limit_text(t)})",
                callback_data=f"edittariff_{tid}",
            )]
            for tid, t in data["tariffs"].items()
        ]
        buttons.append([InlineKeyboardButton(text="➕ Tarif qo'shish", callback_data="addtariff")])
        buttons.append([InlineKeyboardButton(text="➖ Tarif o'chirish", callback_data="deltariff")])
        buttons.append([InlineKeyboardButton(
            text=f"🤖 Boshqa botlar — {data.get('other_bot_price', DEFAULT_OTHER_BOT_PRICE):,} so'm/oy",
            callback_data="editotherprice",
        )])
        await message.answer(
            "💰 <b>Tariflarni boshqarish</b>\n\n"
            "🎬 Kino bot uchun 5 xil tarif, boshqa barcha bot turlari uchun yagona narx.\n\n"
            "Narxini o'zgartirish uchun tanlang:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @dp.callback_query(F.data == "addtariff")
    async def addtariff_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        await callback.message.answer("Yangi tarif nomini kiriting (masalan: 🚀 Mega):")
        await state.set_state(NewTariffAdd.waiting_name)
        await callback.answer()

    @dp.message(NewTariffAdd.waiting_name)
    async def addtariff_name(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        await state.update_data(new_tariff_name=message.text.strip())
        await message.answer("Oylik narxini kiriting (so'm, faqat raqam):")
        await state.set_state(NewTariffAdd.waiting_price)

    @dp.message(NewTariffAdd.waiting_price)
    async def addtariff_price(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        try:
            price = int(message.text.strip().replace(" ", ""))
            if price <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting.")
            return
        await state.update_data(new_tariff_price=price)
        await message.answer("Kunlik foydalanuvchi limitini kiriting (cheksiz bo'lsa 0 yozing):")
        await state.set_state(NewTariffAdd.waiting_limit)

    @dp.message(NewTariffAdd.waiting_limit)
    async def addtariff_limit(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        try:
            limit = int(message.text.strip().replace(" ", ""))
            if limit < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ 0 yoki musbat butun raqam kiriting.")
            return
        fsm_data = await state.get_data()
        new_id = str(max((int(k) for k in data["tariffs"].keys() if k.isdigit()), default=0) + 1)
        data["tariffs"][new_id] = {
            "name": fsm_data["new_tariff_name"],
            "price": fsm_data["new_tariff_price"],
            "daily_limit": None if limit == 0 else limit,
        }
        save_data()
        await message.answer(f"✅ Yangi tarif qo'shildi: {fsm_data['new_tariff_name']} — {fsm_data['new_tariff_price']:,} so'm/oy")
        await state.clear()

    @dp.callback_query(F.data == "deltariff")
    async def deltariff_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        if len(data["tariffs"]) <= 1:
            await callback.answer("Kamida bitta tarif qolishi kerak.", show_alert=True)
            return
        buttons = [
            [InlineKeyboardButton(text=f"{t['name']} — {t['price']:,} so'm/oy", callback_data=f"deltariffid_{tid}")]
            for tid, t in data["tariffs"].items()
        ]
        await callback.message.answer("O'chirmoqchi bo'lgan tarifni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("deltariffid_"))
    async def deltariffid_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        if len(data["tariffs"]) <= 1:
            await callback.answer("Kamida bitta tarif qolishi kerak.", show_alert=True)
            return
        tid = callback.data.split("_", 1)[1]
        removed = data["tariffs"].pop(tid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['name']}")
        await callback.answer()

    @dp.callback_query(F.data == "editotherprice")
    async def editotherprice_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        await callback.message.answer(
            f"Kino'dan boshqa barcha botlar uchun yangi oylik narxni kiriting (so'm, faqat raqam):\n\n"
            f"Joriy narx: {data.get('other_bot_price', DEFAULT_OTHER_BOT_PRICE):,} so'm/oy"
        )
        await state.set_state(EditPrice.waiting_amount)
        await state.update_data(edit_tariff_id=None, edit_other_price=True)
        await callback.answer()

    @dp.message(F.text == "⭐ Stars kursi")
    async def stars_rate_start(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        rate = data.get("stars_rate", 250)
        await message.answer(
            f"⭐ <b>Stars kursi</b>\n\nHozirgi kurs: 1 ⭐ = {rate:,} so'm\n\n"
            "Yangi kursni kiriting (so'mda, faqat raqam):"
        )
        await state.set_state(EditStarsRate.waiting_rate)

    @dp.message(EditStarsRate.waiting_rate)
    async def stars_rate_save(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        try:
            rate = int(message.text.strip().replace(" ", ""))
            if rate <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting (masalan: 250).")
            return
        data["stars_rate"] = rate
        save_data()
        await message.answer(f"✅ Stars kursi endi: 1 ⭐ = {rate:,} so'm")
        await state.clear()

    @dp.callback_query(F.data.startswith("edittariff_"))
    async def edittariff_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        tid = callback.data.split("_", 1)[1]
        t = data["tariffs"][tid]
        await state.update_data(edit_tariff_id=tid)
        await callback.message.answer(
            f"{t['name']} tarifi uchun yangi narxni kiriting (so'm/oy, faqat raqam):\n\n"
            f"Joriy narx: {t['price']:,} so'm/oy"
        )
        await state.set_state(EditPrice.waiting_amount)
        await callback.answer()

    @dp.message(EditPrice.waiting_amount)
    async def editprice_save(message: Message, state: FSMContext):
        try:
            amount = int(message.text.strip().replace(" ", ""))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Faqat musbat raqam kiriting.")
            return
        state_data = await state.get_data()
        if state_data.get("edit_other_price"):
            data["other_bot_price"] = amount
            save_data()
            await message.answer(f"✅ Boshqa botlar narxi endi {amount:,} so'm/oy.")
            await state.clear()
            return
        tid = state_data.get("edit_tariff_id")
        if tid and tid in data["tariffs"]:
            data["tariffs"][tid]["price"] = amount
            save_data()
            await message.answer(f"✅ {data['tariffs'][tid]['name']} tarifi endi {amount:,} so'm/oy.")
        await state.clear()

    @dp.message(Command("globalbuttons"))
    async def global_buttons_panel(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Tugma qo'shish", callback_data="gb_add")],
            [InlineKeyboardButton(text="📋 Tugmalar ro'yxati", callback_data="gb_list")],
            [InlineKeyboardButton(text="➖ Tugmani o'chirish", callback_data="gb_del")],
        ])
        await message.answer(
            "🧩 <b>Global tugmalar boshqaruvi</b>\n\n"
            "Bu yerda qo'shgan tugma barcha turdagi botning menyusiga avtomatik qo'shiladi.",
            reply_markup=buttons,
        )

    @dp.callback_query(F.data == "gb_add")
    async def gb_add_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        await callback.message.answer("Tugma nomini yozing (masalan: ℹ️ Biz haqimizda):")
        await state.set_state(GlobalButtonAdd.waiting_label)
        await callback.answer()

    @dp.message(GlobalButtonAdd.waiting_label)
    async def gb_add_label(message: Message, state: FSMContext):
        await state.update_data(label=message.text.strip())
        await message.answer("Endi shu tugma bosilganda chiqadigan javob matnini yozing:")
        await state.set_state(GlobalButtonAdd.waiting_response)

    @dp.message(GlobalButtonAdd.waiting_response)
    async def gb_add_response(message: Message, state: FSMContext):
        state_data = await state.get_data()
        label = state_data.get("label")
        data["global_buttons"].append({"label": label, "response": message.text})
        save_data()
        await message.answer(f"✅ Tugma qo'shildi: {label}\n\nEndi barcha botlarda ko'rinadi.")
        await state.clear()

    @dp.callback_query(F.data == "gb_list")
    async def gb_list_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        if not data["global_buttons"]:
            await callback.message.answer("Hozircha global tugmalar yo'q.")
        else:
            text = "📋 <b>Global tugmalar:</b>\n\n" + "\n".join(f"• {b['label']}" for b in data["global_buttons"])
            await callback.message.answer(text)
        await callback.answer()

    @dp.callback_query(F.data == "gb_del")
    async def gb_del_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        if not data["global_buttons"]:
            await callback.message.answer("O'chirish uchun tugma yo'q.")
            await callback.answer()
            return
        buttons = [
            [InlineKeyboardButton(text=b["label"], callback_data=f"gbdel_{i}")]
            for i, b in enumerate(data["global_buttons"])
        ]
        await callback.message.answer("O'chirmoqchi bo'lgan tugmani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("gbdel_"))
    async def gb_delid_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        idx = int(callback.data.split("_", 1)[1])
        if 0 <= idx < len(data["global_buttons"]):
            removed = data["global_buttons"].pop(idx)
            save_data()
            await callback.message.answer(f"🗑 O'chirildi: {removed['label']}")
        await callback.answer()

    @dp.message(Command("mybots"))
    @dp.message(F.text == "📁 Botlarim")
    async def mybots(message: Message):
        uid = message.from_user.id
        items = [(t, i) for t, i in data["bots"].items() if uid in i.get("admin_ids", [i["admin_id"]])]

        if not items:
            await message.answer("Hali botlaringiz yo'q. /newbot orqali yarating.")
            return

        for token, info in items:
            status = "🟢 Faol" if is_active(info) else "🔴 Sinov/to'lov tugagan"
            paid_until = info.get("paid_until")
            if info.get("admin_id") == ADMIN_ID:
                paid_note = " (umrbod)"
            elif paid_until:
                date_str = datetime.fromisoformat(paid_until).strftime("%d.%m.%Y")
                paid_note = f" (to'langan: {date_str} gacha)"
            else:
                paid_note = ""
            tariff = get_bot_tariff(info)
            text = (
                f"{BOT_TYPES.get(info['type'])}: <b>{info['name']}</b>\n{status}{paid_note}\n"
                f"💠 Tarif: {tariff['name']} ({tariff_limit_text(tariff)})"
            )
            buttons = []
            if info.get("admin_id") != ADMIN_ID:
                if info["type"] in ("kino", "kino_pro"):
                    buttons.append([InlineKeyboardButton(text="🔄 Tarifni o'zgartirish", callback_data=f"changetariff_{info['id']}")])
                buttons.append([InlineKeyboardButton(text="💰 Hozir to'lov qilish", callback_data=f"paynow_{info['id']}")])
            kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
            await message.answer(text, reply_markup=kb)

    def find_bot_by_id(bot_id: int):
        for token, info in data["bots"].items():
            if info.get("id") == bot_id:
                return token, info
        return None, None

    # ---------- Botlar narxi (admin har bir botni alohida tahrirlashi) ----------
    @dp.message(F.text == "🤖 Botlar narxi")
    async def bot_prices_panel(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        if not data["bots"]:
            await message.answer("Hozircha botlar yo'q.")
            return
        buttons = []
        for token, b in data["bots"].items():
            tariff = get_bot_tariff(b)
            mark = "⚙️" if b.get("custom_price") else ""
            buttons.append([InlineKeyboardButton(
                text=f"{mark}{b['name']} ({BOT_TYPES.get(b['type'], b['type'])}) — {tariff['price']:,} so'm",
                callback_data=f"botpricepick_{b['id']}",
            )])
        await message.answer("🤖 <b>Botlar narxi</b>\n\n⚙️ — maxsus narx qo'yilgan botlar.\n\nTahrirlamoqchi bo'lgan botni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("botpricepick_"))
    async def bot_price_pick_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        bot_id = int(callback.data.split("_", 1)[1])
        token, target = find_bot_by_id(bot_id)
        if not target:
            await callback.answer("❌ Bot topilmadi.", show_alert=True)
            return
        tariff = get_bot_tariff(target)
        buttons = [[InlineKeyboardButton(text="✏️ Yangi narx belgilash", callback_data=f"botpriceset_{bot_id}")]]
        if target.get("custom_price"):
            buttons.append([InlineKeyboardButton(text="↩️ Standart narxga qaytarish", callback_data=f"botpricereset_{bot_id}")])
        await callback.message.answer(
            f"🤖 <b>{target['name']}</b> ({BOT_TYPES.get(target['type'], target['type'])})\n"
            f"👤 Egasi ID: {target['admin_id']}\n"
            f"💰 Joriy narx: {tariff['price']:,} so'm/oy{' (maxsus)' if target.get('custom_price') else ''}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("botpriceset_"))
    async def bot_price_set_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        bot_id = int(callback.data.split("_", 1)[1])
        await state.update_data(price_bot_id=bot_id)
        await callback.message.answer("Yangi oylik narxni kiriting (so'm, faqat raqam):")
        await state.set_state(BotCustomPrice.waiting_price)
        await callback.answer()

    @dp.message(BotCustomPrice.waiting_price)
    async def bot_price_set_save(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        try:
            price = int(message.text.strip().replace(" ", ""))
            if price <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting.")
            return
        fsm_data = await state.get_data()
        bot_id = fsm_data.get("price_bot_id")
        token, target = find_bot_by_id(bot_id)
        if target:
            target["custom_price"] = price
            save_data()
            await message.answer(f"✅ <b>{target['name']}</b> uchun narx endi: {price:,} so'm/oy (maxsus).")
        await state.clear()

    @dp.callback_query(F.data.startswith("botpricereset_"))
    async def bot_price_reset_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        bot_id = int(callback.data.split("_", 1)[1])
        token, target = find_bot_by_id(bot_id)
        if target:
            target.pop("custom_price", None)
            save_data()
            tariff = get_bot_tariff(target)
            await callback.message.answer(f"↩️ <b>{target['name']}</b> standart narxga qaytarildi: {tariff['price']:,} so'm/oy.")
        await callback.answer()

    @dp.callback_query(F.data.startswith("changetariff_"))
    async def changetariff_cb(callback: CallbackQuery):
        bot_id = int(callback.data.split("_", 1)[1])
        token, target = find_bot_by_id(bot_id)
        if not target or callback.from_user.id not in target.get("admin_ids", [target["admin_id"]]):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        if target["type"] not in ("kino", "kino_pro"):
            await callback.answer("Bu bot turi uchun tarif tanlash mavjud emas — narx doim bir xil.", show_alert=True)
            return
        current_tariff = target.get("tariff", "2")
        buttons = [
            [InlineKeyboardButton(
                text=("✅ " if tid == current_tariff else "") + f"{t['name']} — {t['price']:,} so'm/oy ({tariff_limit_text(t)})",
                callback_data=f"settariff_{bot_id}_{tid}",
            )]
            for tid, t in data["tariffs"].items()
        ]
        await callback.message.answer(
            f"🔄 <b>{target['name']}</b> uchun yangi tarifni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("settariff_"))
    async def settariff_cb(callback: CallbackQuery):
        _, bot_id, tariff_id = callback.data.split("_", 2)
        bot_id = int(bot_id)
        token, target = find_bot_by_id(bot_id)
        if not target or callback.from_user.id not in target.get("admin_ids", [target["admin_id"]]):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        target["tariff"] = tariff_id
        save_data()
        tariff = get_tariff(tariff_id)
        await callback.message.edit_text(
            f"✅ Tarif o'zgartirildi: <b>{tariff['name']}</b> — {tariff['price']:,} so'm/oy ({tariff_limit_text(tariff)})\n\n"
            "Kunlik limit darhol qo'llanadi. Keyingi to'lovda shu yangi narx hisoblanadi."
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("paynow_"))
    async def paynow_cb(callback: CallbackQuery):
        bot_id = int(callback.data.split("_", 1)[1])
        token, target = find_bot_by_id(bot_id)
        if not target or callback.from_user.id not in target.get("admin_ids", [target["admin_id"]]):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        amount = next_payment_amount(target)
        uid = callback.from_user.id
        key = str(uid)
        balance = data["user_balances"].get(key, 0)
        if balance < amount:
            await callback.answer(
                f"❌ Balansingizda yetarli mablag' yo'q.\n\nKerak: {amount:,} so'm\nMavjud: {balance:,} so'm\n\n"
                "\"💰 Hisob to'ldirish\" orqali to'ldiring.",
                show_alert=True,
            )
            return
        is_first_payment = not target.get("paid_until")
        data["user_balances"][key] = balance - amount
        base = datetime.now()
        if target.get("paid_until"):
            existing = datetime.fromisoformat(target["paid_until"])
            if existing > base:
                base = existing
        target["paid_until"] = (base + timedelta(days=30)).isoformat()
        if is_first_payment:
            ref_admin = data["user_referring_admin"].get(key)
            if ref_admin and str(ref_admin) in data["sub_admins"]:
                data["sub_admins"][str(ref_admin)]["earnings"] += amount
                try:
                    await callback.bot.send_message(
                        ref_admin,
                        f"💼 Sizning foydalanuvchingiz birinchi oylik to'lovini amalga oshirdi!\n"
                        f"💰 Daromadingizga {amount:,} so'm qo'shildi.",
                    )
                except Exception:
                    pass
        save_data()
        new_date = datetime.fromisoformat(target["paid_until"]).strftime("%d.%m.%Y")
        await callback.message.edit_text(
            f"✅ <b>To'lov muvaffaqiyatli amalga oshirildi!</b>\n\n"
            f"💰 {amount:,} so'm balansdan yechildi.\n"
            f"📅 Bot {new_date} gacha faol.\n"
            f"💳 Qolgan balans: {data['user_balances'][key]:,} so'm"
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("activate_"))
    async def activate_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        bot_id = int(callback.data.split("_", 1)[1])
        await state.update_data(activate_bot_id=bot_id)
        await callback.message.answer("Necha kunga faollashtirilsin? (masalan: 30):")
        await state.set_state(ActivateFlow.waiting_days)
        await callback.answer()

    @dp.message(ActivateFlow.waiting_days)
    async def activate_days_save(message: Message, state: FSMContext):
        try:
            days = int(message.text.strip())
            if days <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting (masalan: 30).")
            return
        state_data = await state.get_data()
        bot_id = state_data.get("activate_bot_id")
        for token, info in data["bots"].items():
            if info.get("id") == bot_id:
                expiry = datetime.now() + timedelta(days=days)
                info["paid_until"] = expiry.isoformat()
                save_data()
                await message.answer(f"✅ {info['name']} bot {expiry.strftime('%d.%m.%Y')} sanagacha faollashtirildi.")
                break
        await state.clear()

    @dp.callback_query(F.data.startswith("deactivate_"))
    async def deactivate_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        bot_id = int(callback.data.split("_", 1)[1])
        for token, info in data["bots"].items():
            if info.get("id") == bot_id:
                info["paid_until"] = None
                save_data()
                await callback.message.answer(f"❌ {info['name']} bot tasdiqdan chiqarildi (to'lov holati bekor qilindi).")
                break
        await callback.answer()

    # ---- RAVSHAN BUILDER BOTning to'liq nusxasini (klon) yaratish — FAQAT ADMIN_ID ----
    @dp.message(Command("newplatform"))
    async def newplatform_start(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        await message.answer(
            "🏗 <b>RAVSHAN BUILDER BOTning yangi nusxasini yaratish</b>\n\n"
            "@BotFather orqali yangi bot yarating va uning tokenini shu yerga yuboring.\n"
            "Token yuborilishi bilan bu yangi bot — hozirgi bot bilan bir xil, "
            "to'liq ishlaydigan RAVSHAN BUILDER BOT nusxasiga aylanadi "
            "(bir xil botlar bazasi, bir xil narxlar, siz — bir xil admin)."
        )
        await state.set_state(NewPlatformFlow.waiting_token)

    @dp.message(NewPlatformFlow.waiting_token)
    async def newplatform_token(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            await state.clear()
            return
        clone_token = message.text.strip()
        try:
            test_bot = Bot(token=clone_token)
            me = await test_bot.get_me()
            await test_bot.session.close()
        except Exception:
            await message.answer("❌ Token noto'g'ri. Qaytadan yuboring.")
            return

        await start_platform_clone(clone_token, me.username)
        await message.answer(
            f"✅ Tayyor! @{me.username} — bu endi to'liq RAVSHAN BUILDER BOT nusxasi.\n\n"
            "U orqali ham /newbot bilan botlar yaratish, /mybots, /prices, /globalbuttons "
            "— hammasi ishlaydi, xuddi shu botdagidek."
        )
        await state.clear()


async def start_platform_clone(token: str, username: str = None):
    """RAVSHAN BUILDER BOTning to'liq nusxasini berilgan token bilan ishga tushiradi."""
    if token in running_platform_clones:
        return
    clone_bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    clone_dp = Dispatcher(storage=MemoryStorage())
    setup_platform_bot(clone_dp)
    task = asyncio.create_task(clone_dp.start_polling(clone_bot))
    running_platform_clones[token] = task

    if not any(c["token"] == token for c in data["platform_clones"]):
        data["platform_clones"].append({
            "token": token,
            "username": username,
            "created_at": datetime.now().isoformat(),
        })
        save_data()


setup_platform_bot(main_dp)


def setup_premium_system(dp: Dispatcher, token: str, admin_id: int):
    """Barcha bot turlari uchun umumiy: to'lov tizimlari + Premium obuna tizimi.
    Yoqilgan bo'lsa, botdan foydalanish uchun Premium sotib olish talab qilinishi mumkin.
    Bir necha bot turida chaqiriladi, shuning uchun info shu yerda alohida olinadi."""
    info = data["bots"][token]
    info.setdefault("payment_systems", {})
    info.setdefault("premium_tariffs", {})
    info.setdefault("premium_users", {})
    info.setdefault("premium_stats", {})
    info.setdefault("premium_enabled", False)

    # ---------- To'lov tizimlari (admin) ----------
    def payment_systems_kb():
        buttons = [[InlineKeyboardButton(text="➕ To'lov tizimi qo'shish", callback_data="ps_add")]]
        if info["payment_systems"]:
            buttons.append([InlineKeyboardButton(text="📋 Ro'yxat", callback_data="ps_list")])
            buttons.append([InlineKeyboardButton(text="➖ O'chirish", callback_data="ps_del")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @dp.message(F.text == "💳 To'lov tizimlar")
    async def payment_systems_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["payment_systems"]:
            await message.answer("⚠️ To'lov tizimlari mavjud emas.", reply_markup=payment_systems_kb())
        else:
            await message.answer("💳 To'lov tizimlari boshqaruvi:", reply_markup=payment_systems_kb())

    @dp.callback_query(F.data == "ps_add")
    async def ps_add_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        await callback.message.answer("Iltimos, to'lov tizimi nomini kiriting:\n\n(Masalan: Click, Payme, Humo, Uzcard...)")
        await state.set_state(PaymentSystemAdd.waiting_name)
        await callback.answer()

    @dp.message(PaymentSystemAdd.waiting_name)
    async def ps_name_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await state.update_data(ps_name=message.text.strip())
        await message.answer("Iltimos, to'lov tizimi raqamini kiriting:\n\n(Masalan: karta yoki hisob raqami)")
        await state.set_state(PaymentSystemAdd.waiting_number)

    @dp.message(PaymentSystemAdd.waiting_number)
    async def ps_number_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await state.update_data(ps_number=message.text.strip())
        await message.answer("Hisob raqami egasining to'liq ismini kiriting:\n\n(Masalan: Ism Familiya)")
        await state.set_state(PaymentSystemAdd.waiting_owner)

    @dp.message(PaymentSystemAdd.waiting_owner)
    async def ps_owner_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        fsm_data = await state.get_data()
        psid = uuid.uuid4().hex[:8]
        info["payment_systems"][psid] = {
            "name": fsm_data.get("ps_name", "-"),
            "number": fsm_data.get("ps_number", "-"),
            "owner": message.text.strip(),
        }
        save_data()
        await message.answer("✅ To'lov tizimi qo'shildi!")
        await state.clear()

    @dp.callback_query(F.data == "ps_list")
    async def ps_list_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["payment_systems"]:
            await callback.message.answer("To'lov tizimlari mavjud emas.")
        else:
            lines = [f"• {p['name']} — {p['number']} ({p['owner']})" for p in info["payment_systems"].values()]
            await callback.message.answer("💳 To'lov tizimlari:\n\n" + "\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "ps_del")
    async def ps_del_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["payment_systems"]:
            await callback.message.answer("O'chirish uchun to'lov tizimi yo'q.")
            await callback.answer()
            return
        buttons = [[InlineKeyboardButton(text=p["name"], callback_data=f"psdel_{pid}")] for pid, p in info["payment_systems"].items()]
        await callback.message.answer("O'chirmoqchi bo'lgan to'lov tizimini tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("psdel_"))
    async def ps_delid_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        pid = callback.data.split("_", 1)[1]
        removed = info["payment_systems"].pop(pid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['name']}")
        await callback.answer()

    # ---------- Premium tariflar (admin) ----------
    def premium_admin_kb():
        toggle_text = "❌ Premium'ni o'chirish" if info["premium_enabled"] else "✅ Premium'ni yoqish"
        buttons = [
            [InlineKeyboardButton(text=toggle_text, callback_data="premium_toggle")],
            [InlineKeyboardButton(text="➕ Tarif qo'shish", callback_data="pt_add")],
        ]
        if info["premium_tariffs"]:
            buttons.append([InlineKeyboardButton(text="📋 Ro'yxat", callback_data="pt_list")])
            buttons.append([InlineKeyboardButton(text="➖ O'chirish", callback_data="pt_del")])
            buttons.append([InlineKeyboardButton(text="🎁 Premium berish", callback_data="pt_grant")])
        buttons.append([InlineKeyboardButton(text="📊 VIP Statistika", callback_data="pt_vipstats")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @dp.message(F.text == "💎 Premium")
    async def premium_admin_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        status = "✅ Yoqilgan" if info["premium_enabled"] else "❌ O'chirilgan"
        await message.answer(f"💎 Premium tariflar boshqaruvi\n\nHolati: {status}", reply_markup=premium_admin_kb())

    @dp.callback_query(F.data == "premium_toggle")
    async def premium_toggle_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        info["premium_enabled"] = not info["premium_enabled"]
        save_data()
        status = "✅ Yoqilgan" if info["premium_enabled"] else "❌ O'chirilgan"
        await callback.message.edit_text(f"💎 Premium tariflar boshqaruvi\n\nHolati: {status}", reply_markup=premium_admin_kb())
        await callback.answer("Saqlandi!")

    # ---------- Premium berish (admin tomonidan qo'lda, to'lovsiz) ----------
    @dp.callback_query(F.data == "pt_grant")
    async def pt_grant_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["premium_tariffs"]:
            await callback.answer("Avval kamida bitta tarif qo'shing.", show_alert=True)
            return
        buttons = [
            [InlineKeyboardButton(text=f"{t['name']} — {t['days']} kun", callback_data=f"ptgranttariff_{tid}")]
            for tid, t in info["premium_tariffs"].items()
        ]
        await callback.message.answer("Qaysi tarifni bermoqchisiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("ptgranttariff_"))
    async def pt_grant_tariff_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        tid = callback.data.split("_", 1)[1]
        tariff = info["premium_tariffs"].get(tid)
        if not tariff:
            await callback.answer("❌ Tarif topilmadi.", show_alert=True)
            return
        await state.update_data(grant_tariff_id=tid)
        await callback.message.answer(
            f"Tanlangan tarif: {tariff['name']} ({tariff['days']} kun)\n\n"
            "Endi foydalanuvchining ID raqamini yoki @username'ini yuboring:"
        )
        await state.set_state(PremiumGrant.waiting_user)
        await callback.answer()

    @dp.message(PremiumGrant.waiting_user)
    async def pt_grant_user(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        raw = message.text.strip()
        target_uid = None
        if raw.startswith("@"):
            try:
                chat = await message.bot.get_chat(raw)
                target_uid = chat.id
            except Exception:
                await message.answer("❌ Bu username bo'yicha foydalanuvchi topilmadi. ID raqamini yuborib ko'ring.")
                return
        else:
            try:
                target_uid = int(raw)
            except ValueError:
                await message.answer("❌ ID raqam yoki @username ko'rinishida yuboring.")
                return

        fsm_data = await state.get_data()
        tid = fsm_data.get("grant_tariff_id")
        tariff = info["premium_tariffs"].get(tid)
        if not tariff:
            await message.answer("❌ Xatolik: tarif topilmadi.")
            await state.clear()
            return

        until = datetime.now() + timedelta(days=tariff["days"])
        existing = info["premium_users"].get(str(target_uid))
        if existing:
            try:
                existing_until = datetime.fromisoformat(existing["until"])
                if existing_until > datetime.now():
                    until = existing_until + timedelta(days=tariff["days"])
            except Exception:
                pass
        info["premium_users"][str(target_uid)] = {"until": until.isoformat()}
        save_data()

        await message.answer(
            f"✅ Premium berildi!\n\n👤 Foydalanuvchi: <code>{target_uid}</code>\n"
            f"💎 Tarif: {tariff['name']}\n📅 Muddati: {until.strftime('%d.%m.%Y')} gacha"
        )
        try:
            await message.bot.send_message(
                target_uid,
                f"🎁 <b>Sizga Premium obuna berildi!</b>\n\n"
                f"💎 Tarif: {tariff['name']}\n📅 Muddati: {until.strftime('%d.%m.%Y')} gacha\n\n"
                "Endi cheklovlarsiz foydalanishingiz mumkin! 🎉",
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga Premium xabarini yuborishda xato: {e}")
        await state.clear()

    # ---------- VIP Statistika ----------
    @dp.callback_query(F.data == "pt_vipstats")
    async def pt_vipstats_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        now = datetime.now()
        active = 0
        for u in info["premium_users"].values():
            try:
                if datetime.fromisoformat(u["until"]) > now:
                    active += 1
            except Exception:
                pass
        total_revenue = sum(s.get("revenue", 0) for s in info.get("premium_stats", {}).values())
        lines = [
            "💎 <b>VIP Obuna Statistikasi</b>\n",
            f"🔹 Faol VIP foydalanuvchilar: {active} ta",
            f"💰 Jami VIP daromad: {total_revenue:,} so'm\n",
            "📈 <b>Tariflar bo'yicha tushumlar:</b>",
        ]
        if not info["premium_tariffs"]:
            lines.append("(hozircha tarif qo'shilmagan)")
        else:
            for tid, t in info["premium_tariffs"].items():
                s = info.get("premium_stats", {}).get(tid, {"count": 0, "revenue": 0})
                lines.append(f"▪️ {t['name']} ({t['price']:,} so'm): {s['count']} ta ({s['revenue']:,} so'm)")
        await callback.message.answer("\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "pt_add")
    async def pt_add_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        await callback.message.answer("Tarif nomini kiriting:\n\n(Masalan: 1 kunlik obuna)")
        await state.set_state(PremiumTariffAdd.waiting_name)
        await callback.answer()

    @dp.message(PremiumTariffAdd.waiting_name)
    async def pt_name_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await state.update_data(pt_name=message.text.strip())
        await message.answer("Necha kunlik? (faqat raqam, masalan: 1):")
        await state.set_state(PremiumTariffAdd.waiting_days)

    @dp.message(PremiumTariffAdd.waiting_days)
    async def pt_days_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        try:
            days = int(message.text.strip())
            if days <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting (masalan: 1).")
            return
        await state.update_data(pt_days=days)
        await message.answer("Narxini kiriting (so'mda, faqat raqam):")
        await state.set_state(PremiumTariffAdd.waiting_price)

    @dp.message(PremiumTariffAdd.waiting_price)
    async def pt_price_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        try:
            price = int(message.text.strip())
            if price <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting (masalan: 5000).")
            return
        fsm_data = await state.get_data()
        tid = uuid.uuid4().hex[:8]
        info["premium_tariffs"][tid] = {
            "name": fsm_data.get("pt_name", "-"),
            "days": fsm_data.get("pt_days", 1),
            "price": price,
        }
        save_data()
        await message.answer("✅ Tarif qo'shildi!")
        await state.clear()

    @dp.callback_query(F.data == "pt_list")
    async def pt_list_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["premium_tariffs"]:
            await callback.message.answer("Tariflar mavjud emas.")
        else:
            lines = [f"• {t['name']} — {t['days']} kun — {t['price']:,} so'm" for t in info["premium_tariffs"].values()]
            await callback.message.answer("💎 Premium tariflar:\n\n" + "\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "pt_del")
    async def pt_del_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["premium_tariffs"]:
            await callback.message.answer("O'chirish uchun tarif yo'q.")
            await callback.answer()
            return
        buttons = [[InlineKeyboardButton(text=t["name"], callback_data=f"ptdel_{tid}")] for tid, t in info["premium_tariffs"].items()]
        await callback.message.answer("O'chirmoqchi bo'lgan tarifni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("ptdel_"))
    async def pt_delid_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        tid = callback.data.split("_", 1)[1]
        removed = info["premium_tariffs"].pop(tid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['name']}")
        await callback.answer()

    # ---------- Premium sotib olish (mijoz) ----------
    @dp.callback_query(F.data == "buy_premium")
    async def buy_premium_cb(callback: CallbackQuery):
        if not info["premium_enabled"] or not info["premium_tariffs"]:
            await callback.answer("Hozircha Premium tariflar mavjud emas.", show_alert=True)
            return
        buttons = [
            [InlineKeyboardButton(text=f"{t['name']} - {t['price']:,} so'm", callback_data=f"premtariff_{tid}")]
            for tid, t in info["premium_tariffs"].items()
        ]
        text = (
            "💎 <b>Premium obuna</b>\n\n"
            "Premium orqali quyidagilarga ega bo'lasiz:\n"
            "• Kanallarga obuna bo'lmasdan botdan foydalanish\n"
            "• Cheklovlarsiz, tezkor xizmat\n\n"
            "📋 Quyidagi tariflardan birini tanlang:"
        )
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("premtariff_"))
    async def premium_tariff_chosen_cb(callback: CallbackQuery):
        tid = callback.data.split("_", 1)[1]
        tariff = info["premium_tariffs"].get(tid)
        if not tariff:
            await callback.answer("❌ Bu tarif endi mavjud emas.", show_alert=True)
            return
        stars = somz_to_stars(tariff["price"])
        buttons = [[InlineKeyboardButton(text=f"⭐ {stars} Stars orqali to'lash", callback_data=f"premstars_{tid}")]]
        buttons += [
            [InlineKeyboardButton(text=p["name"], callback_data=f"prempay_{tid}_{pid}")]
            for pid, p in info["payment_systems"].items()
        ]
        buttons.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="buy_premium")])
        text = (
            "💳 <b>To'lov tizimini tanlang</b>\n\n"
            f"💎 Tarif: {tariff['name']}\n"
            f"📆 Muddat: {tariff['days']} kun\n"
            f"💰 Narx: {tariff['price']:,} so'm (~{stars} ⭐)"
        )
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("premstars_"))
    async def premium_stars_cb(callback: CallbackQuery):
        tid = callback.data.split("_", 1)[1]
        tariff = info["premium_tariffs"].get(tid)
        if not tariff:
            await callback.answer("❌ Bu tarif endi mavjud emas.", show_alert=True)
            return
        stars = somz_to_stars(tariff["price"])
        await callback.bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"💎 Premium — {tariff['name']}",
            description=f"{tariff['days']} kunlik Premium obuna",
            payload=f"premium_{tid}",
            currency="XTR",
            prices=[LabeledPrice(label=tariff["name"], amount=stars)],
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("prempay_"))
    async def premium_payment_chosen_cb(callback: CallbackQuery, state: FSMContext):
        _, tid, pid = callback.data.split("_", 2)
        tariff = info["premium_tariffs"].get(tid)
        psys = info["payment_systems"].get(pid)
        if not tariff or not psys:
            await callback.answer("❌ Ma'lumot topilmadi, qaytadan urinib ko'ring.", show_alert=True)
            return
        await state.update_data(prem_tariff_id=tid, prem_payment_id=pid)
        await state.set_state(PremiumPurchase.waiting_check)
        text = (
            f"💳 <b>{psys['name']}</b>\n\n"
            f"🔢 Raqami: <code>{psys['number']}</code>\n"
            f"👤 Egasi: {psys['owner']}\n\n"
            f"💰 To'lov summasi: {tariff['price']:,} so'm\n\n"
            "To'lovni amalga oshirgach, to'lov chekini (skrinshot) shu yerga yuboring."
        )
        await callback.message.answer(text)
        await callback.answer()

    @dp.message(PremiumPurchase.waiting_check, F.photo)
    async def premium_check_received(message: Message, state: FSMContext):
        fsm_data = await state.get_data()
        tid = fsm_data.get("prem_tariff_id")
        pid = fsm_data.get("prem_payment_id")
        tariff = info["premium_tariffs"].get(tid)
        psys = info["payment_systems"].get(pid)
        if not tariff or not psys:
            await message.answer("❌ Ma'lumot topilmadi, qaytadan /start bosing.")
            await state.clear()
            return
        uid = message.from_user.id
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        caption = (
            "🧾 <b>Yangi Premium to'lovi</b>\n\n"
            f"💎 Tarif: {tariff['name']} ({tariff['days']} kun)\n"
            f"💰 Narx: {tariff['price']:,} so'm\n"
            f"💳 To'lov tizimi: {psys['name']}\n"
            f"👤 Foydalanuvchi: {uname} (ID: <code>{uid}</code>)"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"premapprove_{uid}_{tid}"),
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"premreject_{uid}"),
        ]])
        for aid in info.get("admin_ids", [admin_id]):
            try:
                await message.bot.send_photo(chat_id=aid, photo=message.photo[-1].file_id, caption=caption, reply_markup=kb)
            except Exception as e:
                logging.error(f"Adminga chek yuborishda xato ({aid}): {e}")
        await message.answer(
            "✅ Chekingiz qabul qilindi!\n\n"
            "Adminlar tomonidan tez orada ko'rib chiqiladi. Agar to'lov muvaffaqiyatli "
            "amalga oshirilgan bo'lsa, sizga premium obunasi beriladi."
        )
        await state.clear()

    @dp.callback_query(F.data.startswith("premapprove_"))
    async def premium_approve_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        _, target_uid, tid = callback.data.split("_", 2)
        target_uid = int(target_uid)
        tariff = info["premium_tariffs"].get(tid)
        if not tariff:
            await callback.answer("❌ Tarif topilmadi.", show_alert=True)
            return
        until = datetime.now() + timedelta(days=tariff["days"])
        info["premium_users"][str(target_uid)] = {"until": until.isoformat()}
        stat = info["premium_stats"].setdefault(tid, {"count": 0, "revenue": 0})
        stat["count"] += 1
        stat["revenue"] += tariff["price"]
        save_data()
        try:
            await callback.bot.send_message(
                chat_id=target_uid,
                text=(
                    "✅ <b>Chekingiz qabul qilindi!</b>\n\n"
                    f"Sizga {tariff['days']} kunlik Premium obuna berildi. "
                    f"Amal qilish muddati: {until.strftime('%d.%m.%Y')} gacha.\n\n"
                    "Endi kanallarga obuna bo'lmasdan botdan to'liq foydalanishingiz mumkin! 🎉"
                ),
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga premium xabarini yuborishda xato: {e}")
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ <b>TASDIQLANDI</b>")
        await callback.answer()

    @dp.callback_query(F.data.startswith("premreject_"))
    async def premium_reject_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        target_uid = int(callback.data.split("_", 1)[1])
        try:
            await callback.bot.send_message(
                chat_id=target_uid,
                text=(
                    "❌ <b>To'lovingiz admin tomonidan bekor qilindi.</b>\n\n"
                    "Agar savollaringiz bo'lsa, administrator bilan bog'laning."
                ),
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga rad javobini yuborishda xato: {e}")
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ <b>BEKOR QILINDI</b>")
        await callback.answer()

    # ---------- Telegram Stars orqali to'lov ----------
    @dp.pre_checkout_query()
    async def stars_pre_checkout(pre_checkout_query: PreCheckoutQuery):
        await pre_checkout_query.answer(ok=True)

    @dp.message(F.successful_payment)
    async def stars_payment_success(message: Message):
        payload = message.successful_payment.invoice_payload
        if not payload.startswith("premium_"):
            return
        tid = payload.split("_", 1)[1]
        tariff = info["premium_tariffs"].get(tid)
        if not tariff:
            await message.answer("❌ Xatolik: tarif topilmadi. Administratorga murojaat qiling.")
            return
        until = datetime.now() + timedelta(days=tariff["days"])
        info["premium_users"][str(message.from_user.id)] = {"until": until.isoformat()}
        stat = info["premium_stats"].setdefault(tid, {"count": 0, "revenue": 0})
        stat["count"] += 1
        stat["revenue"] += tariff["price"]
        save_data()
        await message.answer(
            "✅ <b>To'lov muvaffaqiyatli qabul qilindi!</b>\n\n"
            f"Sizga {tariff['days']} kunlik Premium obuna berildi. "
            f"Amal qilish muddati: {until.strftime('%d.%m.%Y')} gacha.\n\n"
            "Endi cheklovlarsiz foydalanishingiz mumkin! 🎉"
        )




# ---------- Kino bot ----------
def setup_shop_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("orders", 0)
    info["stats"].setdefault("revenue", 0)
    info.setdefault("categories", {})              # {cid: name}
    info.setdefault("promo_codes", {})              # {code: {"percent": int, "active": bool}}
    info.setdefault("shop_orders", {})              # {oid: {...}}
    info.setdefault("blocked_users", [])
    info.setdefault("ads", {})
    info.setdefault("referrals", {})
    info.setdefault("referral_bonus_amount", 0)
    info.setdefault("store_credit", {})             # {str(uid): so'm}
    info.setdefault("moderators", [])
    info.setdefault("delivery_fee", 0)
    info.setdefault("vip_discount_percent", 0)
    info.setdefault("auto_report_enabled", False)
    info.setdefault("auto_report_hour", 9)
    info.setdefault("last_report_date", "")
    info.setdefault("user_purchase_count", {})      # {str(uid): count}
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_premium_system(dp, token, admin_id)
    setup_global_buttons_handler(dp, lambda m, s: sstart(m))

    def is_blocked(uid: int) -> bool:
        return uid in info.get("blocked_users", [])

    def is_moderator(uid: int) -> bool:
        return is_admin(info, uid) or uid in info.get("moderators", [])

    def is_premium_user(uid: int) -> bool:
        return is_admin(info, uid) or is_premium_active(info, uid)

    # ---------- Klaviaturalar ----------
    def shop_admin_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📦 Mahsulotlar"), KeyboardButton(text="🏷 Kategoriyalar")],
            [KeyboardButton(text="🎟 Promo-kodlar"), KeyboardButton(text="🧾 Buyurtmalar")],
            [KeyboardButton(text="👥 Foydalanuvchilar"), KeyboardButton(text="📢 Xabar va reklama")],
            [KeyboardButton(text="🎁 Referal"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
            [KeyboardButton(text="💳 To'lov tizimlar"), KeyboardButton(text="💎 Premium")],
            [KeyboardButton(text="👮 Moderatorlar"), KeyboardButton(text="⚙️ Sozlamalar")],
            [KeyboardButton(text="📤 Eksport")],
        ] + get_global_button_rows(), resize_keyboard=True)

    def products_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Qo'shish"), KeyboardButton(text="📋 Ro'yxat")],
            [KeyboardButton(text="✏️ Tahrirlash"), KeyboardButton(text="➖ O'chirish")],
            [KeyboardButton(text="🖼 Rasm qo'shish"), KeyboardButton(text="🔥 Eng ko'p sotilganlar")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def categories_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Kategoriya qo'shish"), KeyboardButton(text="📋 Kategoriyalar ro'yxati")],
            [KeyboardButton(text="🔗 Mahsulotga biriktirish"), KeyboardButton(text="➖ Kategoriya o'chirish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def promo_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Promo-kod qo'shish"), KeyboardButton(text="📋 Promo-kodlar ro'yxati")],
            [KeyboardButton(text="➖ Promo-kodni o'chirish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def orders_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🕓 Kutilayotgan"), KeyboardButton(text="🚚 Yetkazilmoqda")],
            [KeyboardButton(text="✅ Yakunlangan")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def users_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📋 Ro'yxat"), KeyboardButton(text="🔍 Qidirish")],
            [KeyboardButton(text="🚫 Bloklash"), KeyboardButton(text="✅ Blokdan chiqarish")],
            [KeyboardButton(text="🏆 Eng faol xaridorlar")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def broadcast_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📢 Ommaviy xabar yuborish")],
            [KeyboardButton(text="📣 Reklama joylash"), KeyboardButton(text="📋 Reklamalar ro'yxati")],
            [KeyboardButton(text="➖ Reklamani o'chirish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def referral_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🎯 Bonusni sozlash"), KeyboardButton(text="📊 Referal statistikasi")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def moderators_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Moderator qo'shish"), KeyboardButton(text="📋 Moderatorlar ro'yxati")],
            [KeyboardButton(text="➖ Moderatorni o'chirish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def settings_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🚚 Yetkazib berish narxi"), KeyboardButton(text="💎 VIP chegirma foizi")],
            [KeyboardButton(text="📅 Avtomatik hisobot")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    BACK_BUTTONS = {
        "📦 Mahsulotlar", "🏷 Kategoriyalar", "🎟 Promo-kodlar", "🧾 Buyurtmalar",
        "👥 Foydalanuvchilar", "📢 Xabar va reklama", "🎁 Referal", "👮 Moderatorlar",
        "⚙️ Sozlamalar",
    }

    @dp.message(F.text == "◀️ Orqaga")
    async def shop_back(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("🛒 <b>Boshqaruv paneli</b>", reply_markup=shop_admin_menu_kb())

    def catalog_kb():
        buttons = []
        sorted_products = sorted(info["products"].items(), key=lambda item: item[1]["name"].lower())
        for pid, p in sorted_products:
            if p["qty"] > 0:
                buttons.append([InlineKeyboardButton(text=f"{p['name']} — {p['price']:,} so'm", callback_data=f"buy_{pid}")])
        return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    def main_menu_kb():
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🛍 Mahsulotlar"), KeyboardButton(text="🛒 Savatim")],
                [KeyboardButton(text="📜 Buyurtmalarim")],
            ],
            resize_keyboard=True,
        )

    async def send_cart(user_id: int, send_func):
        uid = str(user_id)
        cart = info["carts"].get(uid, {})
        if not cart:
            await send_func("🛒 Savatingiz bo'sh.")
            return
        lines = []
        total = 0
        for pid, qty in cart.items():
            p = info["products"].get(pid)
            if not p:
                continue
            subtotal = p["price"] * qty
            total += subtotal
            lines.append(f"{p['name']} x{qty} = {subtotal:,} so'm")
        text = "🛒 <b>Savatingiz:</b>\n\n" + "\n".join(lines) + f"\n\n💰 Jami: {total:,} so'm"
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Buyurtma berish", callback_data="checkout")],
            [InlineKeyboardButton(text="🗑 Tozalash", callback_data="cart_clear")],
        ])
        await send_func(text, reply_markup=buttons)

    @dp.message(Command("start"))
    async def sstart(message: Message):
        uid = message.from_user.id
        args = message.text.split(maxsplit=1)
        if uid not in info["users"]:
            info["users"].append(uid)
            if len(args) > 1 and args[1].startswith("ref_"):
                try:
                    ref_uid = int(args[1].split("_", 1)[1])
                    if ref_uid != uid:
                        info["referrals"].setdefault(str(ref_uid), [])
                        if uid not in info["referrals"][str(ref_uid)]:
                            info["referrals"][str(ref_uid)].append(uid)
                            bonus = info.get("referral_bonus_amount", 0)
                            if bonus > 0:
                                key = str(ref_uid)
                                info["store_credit"][key] = info["store_credit"].get(key, 0) + bonus
                                try:
                                    await message.bot.send_message(ref_uid, f"🎁 Do'stingiz botga qo'shildi! Sizga {bonus:,} so'm bonus hisobingizga qo'shildi.")
                                except Exception:
                                    pass
                except Exception:
                    pass
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer("🛒 <b>Savdo bot boshqaruvi</b>", reply_markup=shop_admin_menu_kb())
            return
        if is_blocked(uid):
            await message.answer("🚫 Siz ushbu botdan foydalanish huquqidan mahrum qilingansiz.")
            return
        if not await require_subscription(message, info, admin_id):
            return

        if not info["products"]:
            await message.answer("Hozircha mahsulotlar yo'q.", reply_markup=main_menu_kb())
        else:
            kb = catalog_kb()
            await message.answer("🛍 Mahsulotlar:", reply_markup=kb)
            await message.answer("Pastdagi menyudan foydalaning 👇", reply_markup=main_menu_kb())

    @dp.message(F.text == "🛍 Mahsulotlar")
    async def show_catalog(message: Message):
        if not await check_active(message, info, admin_id):
            return
        if is_blocked(message.from_user.id):
            await message.answer("🚫 Siz ushbu botdan foydalanish huquqidan mahrum qilingansiz.")
            return
        if not await require_subscription(message, info, admin_id):
            return
        kb = catalog_kb()
        if not kb:
            await message.answer("Hozircha mahsulotlar yo'q.")
        else:
            await message.answer("🛍 Mahsulotlar:", reply_markup=kb)

    @dp.message(F.text == "🛒 Savatim")
    async def show_cart_menu(message: Message):
        await send_cart(message.from_user.id, message.answer)

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def shop_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        pending = sum(1 for o in info["shop_orders"].values() if o["status"] == "kutilmoqda")
        await message.answer(
            f"📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"🧾 Buyurtmalar: {info['stats']['orders']}\n"
            f"💰 Jami tushum: {info['stats']['revenue']:,} so'm\n"
            f"🕓 Kutilayotgan buyurtmalar: {pending}\n"
            f"📦 Mahsulotlar soni: {len(info['products'])}\n"
            f"🏷 Kategoriyalar: {len(info['categories'])}\n"
            f"🚫 Bloklanganlar: {len(info['blocked_users'])}"
        )

    @dp.message(F.text == "📤 Eksport")
    async def export_products(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["products"]:
            await message.answer("Eksport qilish uchun mahsulot yo'q.")
            return
        lines = [f"#{pid}\t{p['name']}\t{p['price']:,} so'm\t{p['qty']} dona" for pid, p in info["products"].items()]
        text = "📤 Mahsulotlar ro'yxati:\n\n" + "\n".join(lines)
        if len(text) > 3900:
            text = text[:3900] + "\n\n… (qisqartirildi)"
        await message.answer(text)

    # ---------- Mahsulotlar ----------
    @dp.message(F.text == "📦 Mahsulotlar")
    async def products_panel(message: Message):
        if not is_moderator(message.from_user.id):
            return
        await message.answer("📦 <b>Mahsulotlar boshqaruvi</b>", reply_markup=products_menu_kb())

    @dp.message(F.text == "➕ Qo'shish")
    async def padd_cmd(message: Message, state: FSMContext):
        if not is_moderator(message.from_user.id):
            return
        await message.answer("Mahsulot nomini yozing:")
        await state.set_state(AddProduct.waiting_name)

    @dp.message(AddProduct.waiting_name)
    async def padd_name(message: Message, state: FSMContext):
        name = message.text.strip()
        existing = next((p for p in info["products"].values() if p["name"].lower() == name.lower()), None)
        if existing:
            await message.answer(f"⚠️ \"{name}\" nomli mahsulot allaqachon mavjud. Baribir davom etamiz — yangi mahsulot alohida qo'shiladi.")
        await state.update_data(name=name)
        await message.answer("Narxini yozing (faqat raqam, so'mda):")
        await state.set_state(AddProduct.waiting_price)

    @dp.message(AddProduct.waiting_price)
    async def padd_price(message: Message, state: FSMContext):
        try:
            price = int(message.text.strip().replace(" ", ""))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        state_data = await state.get_data()
        pid = str(info["next_id"])
        info["next_id"] += 1
        info["products"][pid] = {"name": state_data["name"], "price": price, "qty": 999999, "sold": 0}
        save_data()
        await message.answer(f"✅ Qo'shildi: {state_data['name']} — {price:,} so'm")
        await state.clear()

        for uid in info["users"]:
            if is_admin(info, uid):
                continue
            try:
                kb = catalog_kb()
                if kb:
                    await message.bot.send_message(uid, "🛍 Mahsulotlar:", reply_markup=kb)
            except Exception:
                pass

    @dp.message(F.text == "📋 Ro'yxat")
    async def plist_cmd(message: Message):
        if not is_moderator(message.from_user.id):
            return
        if not info["products"]:
            await message.answer("Mahsulotlar yo'q.")
        else:
            sorted_products = sorted(info["products"].items(), key=lambda item: item[1]["name"].lower())
            lines = []
            for pid, p in sorted_products:
                cat = info["categories"].get(p.get("category", ""), "")
                cat_note = f" [{cat}]" if cat else ""
                lines.append(f"#{pid}: {p['name']} — {p['price']:,} so'm ({p['qty']} dona){cat_note}")
            await message.answer("📦 <b>Mahsulotlar (alifbo tartibida):</b>\n\n" + "\n".join(lines))

    @dp.message(F.text == "✏️ Tahrirlash")
    async def pedit_start(message: Message):
        if not is_moderator(message.from_user.id):
            return
        if not info["products"]:
            await message.answer("Mahsulotlar yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=p["name"], callback_data=f"peditpick_{pid}")] for pid, p in info["products"].items()]
        await message.answer("Tahrirlamoqchi bo'lgan mahsulotni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("peditpick_"))
    async def pedit_pick(callback: CallbackQuery, state: FSMContext):
        pid = callback.data.split("_", 1)[1]
        await state.update_data(edit_pid=pid)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Nomi", callback_data="peditfield_name")],
            [InlineKeyboardButton(text="Narxi", callback_data="peditfield_price")],
            [InlineKeyboardButton(text="Miqdori", callback_data="peditfield_qty")],
        ])
        await callback.message.answer("Nimani tahrirlaymiz?", reply_markup=buttons)
        await callback.answer()

    @dp.callback_query(F.data.startswith("peditfield_"))
    async def pedit_field(callback: CallbackQuery, state: FSMContext):
        field = callback.data.split("_", 1)[1]
        await state.update_data(edit_field=field)
        await callback.message.answer("Yangi qiymatni kiriting:")
        await state.set_state(EditProduct.waiting_value)
        await callback.answer()

    @dp.message(EditProduct.waiting_value)
    async def pedit_save(message: Message, state: FSMContext):
        fsm_data = await state.get_data()
        pid = fsm_data.get("edit_pid")
        field = fsm_data.get("edit_field")
        product = info["products"].get(pid)
        if not product:
            await message.answer("❌ Mahsulot topilmadi.")
            await state.clear()
            return
        value = message.text.strip()
        if field == "name":
            product["name"] = value
        elif field in ("price", "qty"):
            try:
                product[field] = int(value.replace(" ", ""))
            except ValueError:
                await message.answer("❌ Faqat raqam kiriting.")
                return
        save_data()
        await message.answer(f"✅ Yangilandi: {product['name']}")
        await state.clear()

    @dp.message(F.text == "🖼 Rasm qo'shish")
    async def pphoto_start(message: Message):
        if not is_moderator(message.from_user.id):
            return
        if not info["products"]:
            await message.answer("Mahsulotlar yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=p["name"], callback_data=f"pphotopick_{pid}")] for pid, p in info["products"].items()]
        await message.answer("Qaysi mahsulotga rasm qo'shamiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("pphotopick_"))
    async def pphoto_pick(callback: CallbackQuery, state: FSMContext):
        pid = callback.data.split("_", 1)[1]
        await state.update_data(photo_pid=pid)
        await callback.message.answer("Rasmni yuboring:")
        await state.set_state(ProductPhoto.waiting_photo)
        await callback.answer()

    @dp.message(ProductPhoto.waiting_photo, F.photo)
    async def pphoto_save(message: Message, state: FSMContext):
        fsm_data = await state.get_data()
        pid = fsm_data.get("photo_pid")
        if pid in info["products"]:
            info["products"][pid]["photo"] = message.photo[-1].file_id
            save_data()
            await message.answer(f"✅ Rasm saqlandi: {info['products'][pid]['name']}")
        await state.clear()

    @dp.message(F.text == "🔥 Eng ko'p sotilganlar")
    async def bestsellers(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        sold = [(pid, p) for pid, p in info["products"].items() if p.get("sold", 0) > 0]
        if not sold:
            await message.answer("Hali sotuv statistikasi yo'q.")
            return
        sold.sort(key=lambda x: x[1]["sold"], reverse=True)
        lines = [f"{i+1}. {p['name']} — {p['sold']} dona sotilgan" for i, (pid, p) in enumerate(sold[:10])]
        await message.answer("🔥 <b>Eng ko'p sotilgan TOP-10:</b>\n\n" + "\n".join(lines))

    @dp.message(F.text == "➖ O'chirish")
    async def pdel_cmd(message: Message):
        if not is_moderator(message.from_user.id):
            return
        if not info["products"]:
            await message.answer("O'chirish uchun mahsulot yo'q.")
            return
        sorted_products = sorted(info["products"].items(), key=lambda item: item[1]["name"].lower())
        buttons = [[InlineKeyboardButton(text=p["name"], callback_data=f"pdelid_{pid}")] for pid, p in sorted_products]
        await message.answer("O'chirmoqchi bo'lgan mahsulotni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("pdelid_"))
    async def pdelid_cb(callback: CallbackQuery):
        if not is_moderator(callback.from_user.id):
            return
        pid = callback.data.split("_", 1)[1]
        removed = info["products"].pop(pid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['name']}")
        await callback.answer()

    # ---------- Kategoriyalar ----------
    @dp.message(F.text == "🏷 Kategoriyalar")
    async def categories_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("🏷 <b>Kategoriyalar boshqaruvi</b>", reply_markup=categories_menu_kb())

    @dp.message(F.text == "➕ Kategoriya qo'shish")
    async def cat_add_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Kategoriya nomini kiriting:")
        await state.set_state(ShopCategoryAdd.waiting_name)

    @dp.message(ShopCategoryAdd.waiting_name)
    async def cat_add_save(message: Message, state: FSMContext):
        cid = uuid.uuid4().hex[:6]
        info["categories"][cid] = message.text.strip()
        save_data()
        await message.answer(f"✅ Kategoriya qo'shildi: {message.text.strip()}")
        await state.clear()

    @dp.message(F.text == "📋 Kategoriyalar ro'yxati")
    async def cat_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["categories"]:
            await message.answer("Kategoriyalar mavjud emas.")
        else:
            await message.answer("🏷 Kategoriyalar:\n\n" + "\n".join(f"• {n}" for n in info["categories"].values()))

    @dp.message(F.text == "🔗 Mahsulotga biriktirish")
    async def cat_assign_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["products"] or not info["categories"]:
            await message.answer("Buning uchun kamida bitta mahsulot va kategoriya bo'lishi kerak.")
            return
        buttons = [[InlineKeyboardButton(text=p["name"], callback_data=f"catassign_{pid}")] for pid, p in info["products"].items()]
        await message.answer("Qaysi mahsulotga kategoriya biriktiramiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("catassign_"))
    async def cat_assign_pick(callback: CallbackQuery, state: FSMContext):
        pid = callback.data.split("_", 1)[1]
        await state.update_data(assign_pid=pid)
        buttons = [[InlineKeyboardButton(text=name, callback_data=f"catset_{cid}")] for cid, name in info["categories"].items()]
        await callback.message.answer("Kategoriyani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("catset_"))
    async def cat_assign_set(callback: CallbackQuery, state: FSMContext):
        cid = callback.data.split("_", 1)[1]
        fsm_data = await state.get_data()
        pid = fsm_data.get("assign_pid")
        if pid in info["products"]:
            info["products"][pid]["category"] = cid
            save_data()
            await callback.message.answer(f"✅ {info['products'][pid]['name']} — {info['categories'].get(cid)} kategoriyasiga biriktirildi.")
        await state.clear()
        await callback.answer()

    @dp.message(F.text == "➖ Kategoriya o'chirish")
    async def cat_del_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["categories"]:
            await message.answer("Kategoriyalar mavjud emas.")
            return
        buttons = [[InlineKeyboardButton(text=name, callback_data=f"catdel_{cid}")] for cid, name in info["categories"].items()]
        await message.answer("O'chirmoqchi bo'lgan kategoriyani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("catdel_"))
    async def cat_del_cb(callback: CallbackQuery):
        cid = callback.data.split("_", 1)[1]
        removed = info["categories"].pop(cid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed}")
        await callback.answer()

    # ---------- Promo-kodlar ----------
    @dp.message(F.text == "🎟 Promo-kodlar")
    async def promo_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("🎟 <b>Promo-kodlar boshqaruvi</b>", reply_markup=promo_menu_kb())

    @dp.message(F.text == "➕ Promo-kod qo'shish")
    async def promo_add_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Promo-kod matnini kiriting (masalan: YANGI10):")
        await state.set_state(PromoCodeAdd.waiting_code)

    @dp.message(PromoCodeAdd.waiting_code)
    async def promo_add_code(message: Message, state: FSMContext):
        await state.update_data(promo_code=message.text.strip().upper())
        await message.answer("Chegirma foizini kiriting (masalan: 10):")
        await state.set_state(PromoCodeAdd.waiting_percent)

    @dp.message(PromoCodeAdd.waiting_percent)
    async def promo_add_percent(message: Message, state: FSMContext):
        try:
            percent = int(message.text.strip())
            if not (0 < percent <= 100):
                raise ValueError
        except ValueError:
            await message.answer("❌ 1-100 oralig'ida raqam kiriting.")
            return
        fsm_data = await state.get_data()
        code = fsm_data["promo_code"]
        info["promo_codes"][code] = {"percent": percent, "active": True}
        save_data()
        await message.answer(f"✅ Promo-kod qo'shildi: {code} — {percent}% chegirma")
        await state.clear()

    @dp.message(F.text == "📋 Promo-kodlar ro'yxati")
    async def promo_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["promo_codes"]:
            await message.answer("Promo-kodlar mavjud emas.")
        else:
            lines = [f"• {'🟢' if p['active'] else '⚪️'} {c} — {p['percent']}%" for c, p in info["promo_codes"].items()]
            await message.answer("🎟 Promo-kodlar:\n\n" + "\n".join(lines))

    @dp.message(F.text == "➖ Promo-kodni o'chirish")
    async def promo_del_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["promo_codes"]:
            await message.answer("Promo-kodlar mavjud emas.")
            return
        buttons = [[InlineKeyboardButton(text=c, callback_data=f"promodel_{c}")] for c in info["promo_codes"]]
        await message.answer("O'chirmoqchi bo'lgan promo-kodni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("promodel_"))
    async def promo_del_cb(callback: CallbackQuery):
        code = callback.data.split("_", 1)[1]
        info["promo_codes"].pop(code, None)
        save_data()
        await callback.message.answer(f"🗑 O'chirildi: {code}")
        await callback.answer()

    # ---------- Foydalanuvchilar ----------
    @dp.message(F.text == "👥 Foydalanuvchilar")
    async def users_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(f"👥 Jami foydalanuvchilar: {len(info['users'])}", reply_markup=users_menu_kb())

    @dp.message(F.text == "📋 Ro'yxat")
    async def users_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        users = info["users"][-30:]
        await message.answer(f"👥 Oxirgi {len(users)} (jami {len(info['users'])}):\n\n" + "\n".join(f"• <code>{u}</code>" for u in users))

    @dp.message(F.text == "🔍 Qidirish")
    async def user_search_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Foydalanuvchi ID raqamini kiriting:")
        await state.set_state(ShopUserSearch.waiting_query)

    @dp.message(ShopUserSearch.waiting_query)
    async def user_search_result(message: Message, state: FSMContext):
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        found = target in info["users"]
        blocked = target in info["blocked_users"]
        purchases = info["user_purchase_count"].get(str(target), 0)
        credit = info["store_credit"].get(str(target), 0)
        await message.answer(
            f"🔍 ID: <code>{target}</code>\n"
            f"{'✅ Bot foydalanuvchisi' if found else '❌ Topilmadi'}\n"
            f"{'🚫 Bloklangan' if blocked else '✅ Bloklanmagan'}\n"
            f"🛍 Xaridlar soni: {purchases}\n"
            f"💰 Bonus hisobi: {credit:,} so'm"
        )
        await state.clear()

    @dp.message(F.text == "🚫 Bloklash")
    async def block_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Bloklamoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(ShopBlockUser.waiting_id)

    @dp.message(ShopBlockUser.waiting_id)
    async def block_save(message: Message, state: FSMContext):
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        if target not in info["blocked_users"]:
            info["blocked_users"].append(target)
            save_data()
        await message.answer(f"🚫 {target} bloklandi.")
        await state.clear()

    @dp.message(F.text == "✅ Blokdan chiqarish")
    async def unblock_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Blokdan chiqarmoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(ShopUnblockUser.waiting_id)

    @dp.message(ShopUnblockUser.waiting_id)
    async def unblock_save(message: Message, state: FSMContext):
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        if target in info["blocked_users"]:
            info["blocked_users"].remove(target)
            save_data()
            await message.answer(f"✅ {target} blokdan chiqarildi.")
        else:
            await message.answer("Bu foydalanuvchi bloklanmagan.")
        await state.clear()

    @dp.message(F.text == "🏆 Eng faol xaridorlar")
    async def top_customers(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["user_purchase_count"]:
            await message.answer("Hali xaridlar yo'q.")
            return
        top = sorted(info["user_purchase_count"].items(), key=lambda x: x[1], reverse=True)[:10]
        lines = [f"{i+1}. ID {uid} — {count} ta xarid" for i, (uid, count) in enumerate(top)]
        await message.answer("🏆 <b>Eng faol xaridorlar TOP-10:</b>\n\n" + "\n".join(lines))

    # ---------- Xabar va reklama ----------
    @dp.message(F.text == "📢 Xabar va reklama")
    async def broadcast_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("📢 <b>Xabar va reklama</b>", reply_markup=broadcast_menu_kb())

    @dp.message(F.text == "📢 Ommaviy xabar yuborish")
    async def broadcast_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("E'lon matnini yuboring:")
        await state.set_state(PostFlow.waiting_text)

    @dp.message(PostFlow.waiting_text)
    async def broadcast_text(message: Message, state: FSMContext):
        await state.update_data(text=message.text)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="shop_post_confirm")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="shop_post_cancel")],
        ])
        await message.answer(f"{len(info['users'])} kishiga yuborilsinmi?\n\n{message.text}", reply_markup=buttons)
        await state.set_state(PostFlow.waiting_confirm)

    @dp.callback_query(F.data == "shop_post_confirm", PostFlow.waiting_confirm)
    async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
        fsm_data = await state.get_data()
        text = fsm_data.get("text", "")
        count = 0
        for uid in info["users"]:
            try:
                await callback.bot.send_message(uid, text)
                count += 1
            except Exception:
                pass
        await callback.message.edit_text(f"✅ {count} ta foydalanuvchiga yuborildi.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "shop_post_cancel", PostFlow.waiting_confirm)
    async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
        await callback.message.edit_text("❌ Bekor qilindi.")
        await state.clear()
        await callback.answer()

    @dp.message(F.text == "📣 Reklama joylash")
    async def ad_add_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Reklama matnini kiriting (har bir xariddan keyin ko'rsatiladi):")
        await state.set_state(StarOrderCustom.waiting_amount)

    @dp.message(StarOrderCustom.waiting_amount)
    async def ad_add_save(message: Message, state: FSMContext):
        aid = uuid.uuid4().hex[:6]
        info["ads"][aid] = {"text": message.text.strip(), "active": True}
        save_data()
        await message.answer("✅ Reklama qo'shildi va faollashtirildi.")
        await state.clear()

    @dp.message(F.text == "📋 Reklamalar ro'yxati")
    async def ad_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["ads"]:
            await message.answer("Reklamalar mavjud emas.")
        else:
            lines = [f"• {'🟢' if a['active'] else '⚪️'} {a['text'][:40]}" for a in info["ads"].values()]
            await message.answer("📣 Reklamalar:\n\n" + "\n".join(lines))

    @dp.message(F.text == "➖ Reklamani o'chirish")
    async def ad_del_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["ads"]:
            await message.answer("Reklamalar mavjud emas.")
            return
        buttons = [[InlineKeyboardButton(text=a["text"][:30], callback_data=f"addel_{aid}")] for aid, a in info["ads"].items()]
        await message.answer("O'chirmoqchi bo'lgan reklamani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("addel_"))
    async def ad_del_cb(callback: CallbackQuery):
        aid = callback.data.split("_", 1)[1]
        info["ads"].pop(aid, None)
        save_data()
        await callback.message.answer("🗑 Reklama o'chirildi.")
        await callback.answer()

    # ---------- Referal ----------
    @dp.message(F.text == "🎁 Referal")
    async def referral_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"🎁 <b>Referal tizimi</b>\n\nHozirgi bonus: {info.get('referral_bonus_amount', 0):,} so'm",
            reply_markup=referral_menu_kb(),
        )

    @dp.message(F.text == "🎯 Bonusni sozlash")
    async def referral_bonus_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Har bir taklif uchun necha so'm bonus berilsin? (0 — o'chirish):")
        await state.set_state(ShopSettingsFlow.waiting_referral_bonus)

    @dp.message(ShopSettingsFlow.waiting_referral_bonus)
    async def referral_bonus_save(message: Message, state: FSMContext):
        try:
            amount = int(message.text.strip().replace(" ", ""))
            if amount < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ 0 yoki musbat butun raqam kiriting.")
            return
        info["referral_bonus_amount"] = amount
        save_data()
        await message.answer(f"✅ Referal bonusi: {amount:,} so'm.")
        await state.clear()

    @dp.message(F.text == "📊 Referal statistikasi")
    async def referral_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        total_refs = sum(len(v) for v in info["referrals"].values())
        top = sorted(info["referrals"].items(), key=lambda x: len(x[1]), reverse=True)[:10]
        lines = [f"{i+1}. ID {uid} — {len(refs)} ta taklif" for i, (uid, refs) in enumerate(top)]
        text = f"📊 Jami takliflar: {total_refs}\n\n" + ("\n".join(lines) if lines else "Hali takliflar yo'q.")
        await message.answer(text)

    # ---------- Moderatorlar ----------
    @dp.message(F.text == "👮 Moderatorlar")
    async def moderators_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"👮 <b>Moderatorlar</b>\n\nModeratorlar faqat mahsulot qo'sha oladi, boshqa sozlamalarga kira olmaydi.\n\nJami: {len(info['moderators'])} ta",
            reply_markup=moderators_menu_kb(),
        )

    @dp.message(F.text == "➕ Moderator qo'shish")
    async def moderator_add_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Moderator qilmoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(ShopModeratorAdd.waiting_id)

    @dp.message(ShopModeratorAdd.waiting_id)
    async def moderator_add_save(message: Message, state: FSMContext):
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        if target not in info["moderators"]:
            info["moderators"].append(target)
            save_data()
        await message.answer(f"✅ {target} moderator qilib tayinlandi.")
        await state.clear()

    @dp.message(F.text == "📋 Moderatorlar ro'yxati")
    async def moderator_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["moderators"]:
            await message.answer("Moderatorlar yo'q.")
        else:
            await message.answer("👮 Moderatorlar:\n\n" + "\n".join(f"• <code>{m}</code>" for m in info["moderators"]))

    @dp.message(F.text == "➖ Moderatorni o'chirish")
    async def moderator_del_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["moderators"]:
            await message.answer("Moderatorlar yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=str(m), callback_data=f"moddel_{m}")] for m in info["moderators"]]
        await message.answer("O'chirmoqchi bo'lgan moderatorni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("moddel_"))
    async def moderator_del_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        target = int(callback.data.split("_", 1)[1])
        if target in info["moderators"]:
            info["moderators"].remove(target)
            save_data()
        await callback.message.answer(f"➖ {target} moderatorlikdan olib tashlandi.")
        await callback.answer()

    # ---------- Sozlamalar ----------
    @dp.message(F.text == "⚙️ Sozlamalar")
    async def settings_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"⚙️ <b>Sozlamalar</b>\n\n"
            f"🚚 Yetkazib berish narxi: {info.get('delivery_fee', 0):,} so'm\n"
            f"💎 VIP chegirma: {info.get('vip_discount_percent', 0)}%",
            reply_markup=settings_menu_kb(),
        )

    @dp.message(F.text == "🚚 Yetkazib berish narxi")
    async def delivery_fee_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Yetkazib berish narxini kiriting (so'm, 0 — bepul):")
        await state.set_state(ShopSettingsFlow.waiting_delivery_fee)

    @dp.message(ShopSettingsFlow.waiting_delivery_fee)
    async def delivery_fee_save(message: Message, state: FSMContext):
        try:
            fee = int(message.text.strip().replace(" ", ""))
            if fee < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ 0 yoki musbat butun raqam kiriting.")
            return
        info["delivery_fee"] = fee
        save_data()
        await message.answer(f"✅ Yetkazib berish narxi: {fee:,} so'm.")
        await state.clear()

    @dp.message(F.text == "💎 VIP chegirma foizi")
    async def vip_discount_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Premium foydalanuvchilar uchun chegirma foizini kiriting (0-100):")
        await state.set_state(ShopSettingsFlow.waiting_vip_discount)

    @dp.message(ShopSettingsFlow.waiting_vip_discount)
    async def vip_discount_save(message: Message, state: FSMContext):
        try:
            percent = int(message.text.strip())
            if not (0 <= percent <= 100):
                raise ValueError
        except ValueError:
            await message.answer("❌ 0-100 oralig'ida raqam kiriting.")
            return
        info["vip_discount_percent"] = percent
        save_data()
        await message.answer(f"✅ VIP chegirma: {percent}%.")
        await state.clear()

    @dp.message(F.text == "📅 Avtomatik hisobot")
    async def auto_report_toggle(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        info["auto_report_enabled"] = not info.get("auto_report_enabled", False)
        save_data()
        status = "✅ Yoqildi" if info["auto_report_enabled"] else "❌ O'chirildi"
        await message.answer(f"📅 Avtomatik kunlik hisobot: {status}")

    # ---------- Buyurtmalar (admin) ----------
    def order_status_list_text(status: str, emoji: str):
        orders = [(oid, o) for oid, o in info["shop_orders"].items() if o["status"] == status]
        if not orders:
            return f"{emoji} Bu holatda buyurtma yo'q."
        lines = [f"#{oid[:6]} — {o['total']:,} so'm — ID:{o['user_id']}" for oid, o in orders[-20:]]
        return f"{emoji} <b>Buyurtmalar ({len(orders)}):</b>\n\n" + "\n".join(lines)

    @dp.message(F.text == "🧾 Buyurtmalar")
    async def orders_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("🧾 <b>Buyurtmalar boshqaruvi</b>", reply_markup=orders_menu_kb())

    @dp.message(F.text == "🕓 Kutilayotgan")
    async def orders_pending(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(order_status_list_text("kutilmoqda", "🕓"))

    @dp.message(F.text == "🚚 Yetkazilmoqda")
    async def orders_delivering(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(order_status_list_text("yetkazilmoqda", "🚚"))

    @dp.message(F.text == "✅ Yakunlangan")
    async def orders_done(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(order_status_list_text("yakunlandi", "✅"))

    @dp.callback_query(F.data.startswith("orderstatus_"))
    async def order_status_update_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        _, oid, new_status = callback.data.split("_", 2)
        order = info["shop_orders"].get(oid)
        if order:
            order["status"] = new_status
            save_data()
            try:
                status_text = {"yetkazilmoqda": "🚚 Buyurtmangiz yetkazilmoqda!", "yakunlandi": "✅ Buyurtmangiz yakunlandi. Xaridingiz uchun rahmat!"}.get(new_status, "")
                if status_text:
                    await callback.bot.send_message(order["user_id"], status_text)
            except Exception:
                pass
        await callback.answer("✅ Holat yangilandi.")

    @dp.callback_query(F.data == "padd")
    async def padd_cb_legacy(callback: CallbackQuery, state: FSMContext):
        if not is_moderator(callback.from_user.id):
            return
        await callback.message.answer("Mahsulot nomini yozing:")
        await state.set_state(AddProduct.waiting_name)
        await callback.answer()

    @dp.callback_query(F.data.startswith("buy_"))
    async def buy_cb(callback: CallbackQuery):
        if not await check_active(callback, info, admin_id):
            return
        if is_blocked(callback.from_user.id):
            await callback.answer("🚫 Siz botdan foydalanish huquqidan mahrum qilingansiz.", show_alert=True)
            return
        if not await require_subscription(callback, info, admin_id):
            return
        pid = callback.data.split("_", 1)[1]
        uid = str(callback.from_user.id)
        product = info["products"].get(pid)
        if not product or product["qty"] <= 0:
            await callback.answer("❌ Mahsulot tugagan.", show_alert=True)
            return
        cart = info["carts"].setdefault(uid, {})
        cart[pid] = cart.get(pid, 0) + 1
        save_data()
        total = sum(info["products"][p]["price"] * q for p, q in cart.items() if p in info["products"])
        await callback.answer(f"✅ Qo'shildi! Savat: {total:,} so'm")

    @dp.callback_query(F.data == "cart")
    async def cart_cb(callback: CallbackQuery):
        await send_cart(callback.from_user.id, callback.message.answer)
        await callback.answer()

    @dp.callback_query(F.data == "cart_clear")
    async def cart_clear_cb(callback: CallbackQuery):
        uid = str(callback.from_user.id)
        info["carts"][uid] = {}
        save_data()
        await callback.message.answer("🗑 Savat tozalandi.")
        await callback.answer()

    def location_kb():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Joylashuvni yuborish", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True,
        )

    def contact_kb():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📞 Raqamni yuborish", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True,
        )

    def promo_prompt_kb():
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭ O'tkazib yuborish", callback_data="skip_promo")]])

    @dp.callback_query(F.data == "checkout")
    async def checkout_cb(callback: CallbackQuery, state: FSMContext):
        uid = str(callback.from_user.id)
        cart = info["carts"].get(uid, {})
        if not cart:
            await callback.answer("Savat bo'sh.", show_alert=True)
            return
        await callback.message.answer(
            "🎟 Promo-kodingiz bo'lsa yuboring, bo'lmasa \"O'tkazib yuborish\" tugmasini bosing:",
            reply_markup=promo_prompt_kb(),
        )
        await state.set_state(Checkout.waiting_payment)
        await callback.answer()

    @dp.callback_query(F.data == "skip_promo", Checkout.waiting_payment)
    async def skip_promo_cb(callback: CallbackQuery, state: FSMContext):
        await ask_address(callback.message, state)
        await callback.answer()

    @dp.message(Checkout.waiting_payment)
    async def promo_code_entered(message: Message, state: FSMContext):
        code = message.text.strip().upper()
        promo = info["promo_codes"].get(code)
        if promo and promo.get("active"):
            await state.update_data(promo_percent=promo["percent"], promo_code=code)
            await message.answer(f"✅ Promo-kod qabul qilindi: {promo['percent']}% chegirma!")
        else:
            await message.answer("❌ Bunday promo-kod topilmadi, chegirmasiz davom etamiz.")
        await ask_address(message, state)

    async def ask_address(message: Message, state: FSMContext):
        await message.answer(
            "📍 Yetkazib berish manzilini yuboring — pastdagi tugma orqali joylashuvingizni ulashing:",
            reply_markup=location_kb(),
        )
        await state.set_state(Checkout.waiting_address)

    @dp.message(Checkout.waiting_address, F.location)
    async def checkout_address_location(message: Message, state: FSMContext):
        lat, lon = message.location.latitude, message.location.longitude
        address = f"https://maps.google.com/?q={lat},{lon}"
        await state.update_data(address=address)
        await message.answer("📞 Endi telefon raqamingizni yuboring:", reply_markup=contact_kb())
        await state.set_state(Checkout.waiting_phone)

    @dp.message(Checkout.waiting_address)
    async def checkout_address_text(message: Message, state: FSMContext):
        await state.update_data(address=message.text.strip())
        await message.answer("📞 Endi telefon raqamingizni yuboring:", reply_markup=contact_kb())
        await state.set_state(Checkout.waiting_phone)

    @dp.message(Checkout.waiting_phone, F.contact)
    async def checkout_phone_contact(message: Message, state: FSMContext):
        await finalize_order(message, state, message.contact.phone_number)

    @dp.message(Checkout.waiting_phone)
    async def checkout_phone_text(message: Message, state: FSMContext):
        await finalize_order(message, state, message.text.strip())

    async def finalize_order(message: Message, state: FSMContext, phone: str):
        state_data = await state.get_data()
        address = state_data.get("address", "-")
        promo_percent = state_data.get("promo_percent", 0)

        uid = str(message.from_user.id)
        cart = info["carts"].get(uid, {})
        lines = []
        subtotal = 0
        for pid, qty in cart.items():
            p = info["products"].get(pid)
            if not p:
                continue
            line_total = p["price"] * qty
            subtotal += line_total
            lines.append(f"{p['name']} x{qty} = {line_total:,} so'm")
            p["qty"] = max(0, p["qty"] - qty)
            p["sold"] = p.get("sold", 0) + qty

        vip_percent = info.get("vip_discount_percent", 0) if is_premium_user(message.from_user.id) else 0
        discount_percent = max(promo_percent, vip_percent)
        discount_amount = subtotal * discount_percent // 100

        credit = info["store_credit"].get(uid, 0)
        credit_used = min(credit, subtotal - discount_amount)
        if credit_used > 0:
            info["store_credit"][uid] = credit - credit_used

        delivery_fee = info.get("delivery_fee", 0)
        total = max(0, subtotal - discount_amount - credit_used) + delivery_fee

        username = message.from_user.username or message.from_user.id
        order_text = (
            f"🛒 <b>Yangi buyurtma!</b>\n"
            f"Xaridor: @{username}\n"
            f"📍 Manzil: {address}\n"
            f"📞 Telefon: {phone}\n\n"
            + "\n".join(lines)
            + (f"\n🎟 Chegirma: -{discount_amount:,} so'm" if discount_amount else "")
            + (f"\n💳 Bonus hisobdan: -{credit_used:,} so'm" if credit_used else "")
            + (f"\n🚚 Yetkazib berish: {delivery_fee:,} so'm" if delivery_fee else "")
            + f"\n\n💰 Jami: {total:,} so'm"
        )
        oid = uuid.uuid4().hex[:8]
        info["shop_orders"][oid] = {
            "user_id": int(uid), "lines": lines, "total": total, "address": address,
            "phone": phone, "status": "kutilmoqda", "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚚 Yetkazilmoqda", callback_data=f"orderstatus_{oid}_yetkazilmoqda"),
            InlineKeyboardButton(text="✅ Yakunlandi", callback_data=f"orderstatus_{oid}_yakunlandi"),
        ]])
        await message.bot.send_message(admin_id, order_text, reply_markup=kb)

        info["carts"][uid] = {}
        info["stats"]["orders"] += 1
        info["stats"]["revenue"] += total
        info["user_purchase_count"][uid] = info["user_purchase_count"].get(uid, 0) + 1
        info.setdefault("order_history", {})
        info["order_history"].setdefault(uid, []).append({
            "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "lines": lines, "total": total, "address": address, "phone": phone,
        })
        save_data()

        await message.answer(f"✅ Buyurtmangiz qabul qilindi! Jami: {total:,} so'm. Tez orada siz bilan bog'lanishadi.", reply_markup=ReplyKeyboardRemove())
        active_ads = [a for a in info["ads"].values() if a.get("active")]
        if active_ads:
            import random
            ad = random.choice(active_ads)
            await message.answer(f"📣 {ad['text']}")
        await state.clear()

    @dp.message(F.text == "📜 Buyurtmalarim")
    async def my_orders(message: Message):
        uid = str(message.from_user.id)
        orders = info.get("order_history", {}).get(uid, [])
        if not orders:
            await message.answer("Sizda hali buyurtmalar yo'q.")
            return
        text = "📜 <b>Buyurtmalarim:</b>\n\n"
        for o in orders[-10:]:
            text += f"🗓 {o['date']}\n" + "\n".join(o["lines"]) + f"\n💰 Jami: {o['total']:,} so'm\n\n"
        await message.answer(text)


def setup_ai_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("questions", 0)
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_premium_system(dp, token, admin_id)
    setup_global_buttons_handler(dp, lambda m, s: astart(m))

    def ai_admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🔄 Yangi suhbat"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📢 Xabar yuborish")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
            [KeyboardButton(text="💳 To'lov tizimlar"), KeyboardButton(text="💎 Premium")],
        ] + get_global_button_rows(), resize_keyboard=True)

    def ai_user_kb():
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔄 Yangi suhbat")]] + get_global_button_rows(), resize_keyboard=True)

    @dp.message(Command("start"))
    async def astart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer(
                "🤖 Salom! Pastdagi menyudan foydalaning 👇\nSavol yozsangiz ham javob beraman.",
                reply_markup=ai_admin_kb(),
            )
        else:
            await message.answer(
                "🤖 Salom! Menga istalgan savolni yozing, sun'iy intellekt sifatida javob beraman.",
                reply_markup=ai_user_kb(),
            )

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def ai_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"❓ Savollar soni: {info['stats']['questions']}"
        )

    @dp.message(F.text == "🔄 Yangi suhbat")
    async def reset_chat(message: Message):
        info.setdefault("ai_history", {})
        info["ai_history"][str(message.from_user.id)] = []
        save_data()
        await message.answer("🔄 Suhbat tarixi tozalandi. Yangi savol yozing.")

    @dp.message(F.text == "📢 Xabar yuborish")
    async def ai_newpost_cb(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("E'lon matnini yuboring:")
        await state.set_state(PostFlow.waiting_text)

    @dp.message(PostFlow.waiting_text)
    async def ai_post_text(message: Message, state: FSMContext):
        await state.update_data(text=message.text)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="ai_post_confirm")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="ai_post_cancel")],
        ])
        await message.answer(
            f"Quyidagi xabar {len(info['users'])} kishiga yuborilsinmi?\n\n{message.text}",
            reply_markup=buttons,
        )
        await state.set_state(PostFlow.waiting_confirm)

    @dp.callback_query(F.data == "ai_post_confirm", PostFlow.waiting_confirm)
    async def ai_post_confirm_cb(callback: CallbackQuery, state: FSMContext):
        state_data = await state.get_data()
        text = state_data.get("text", "")
        count = 0
        for uid in info["users"]:
            try:
                await callback.bot.send_message(uid, text)
                count += 1
            except Exception:
                pass
        await callback.message.edit_text(f"✅ {count} ta foydalanuvchiga yuborildi.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "ai_post_cancel", PostFlow.waiting_confirm)
    async def ai_post_cancel_cb(callback: CallbackQuery, state: FSMContext):
        await callback.message.edit_text("❌ Bekor qilindi.")
        await state.clear()
        await callback.answer()

    @dp.message(F.text)
    async def ai_chat(message: Message):
        if not await check_active(message, info, admin_id):
            return
        if not await require_subscription(message, info, admin_id):
            return
        info["stats"]["questions"] += 1
        info.setdefault("ai_history", {})
        uid = str(message.from_user.id)
        history = info["ai_history"].setdefault(uid, [])

        contents = list(history) + [{"role": "user", "parts": [{"text": message.text}]}]

        await message.bot.send_chat_action(message.chat.id, "typing")
        thinking = await message.answer("💭 O'ylayapman...")
        try:
            answer = await ask_gemini_chat(contents)
            await thinking.edit_text(answer)
            history.append({"role": "user", "parts": [{"text": message.text}]})
            history.append({"role": "model", "parts": [{"text": answer}]})
            info["ai_history"][uid] = history[-12:]  # oxirgi 6 ta savol-javobni saqlaymiz
            save_data()
        except Exception as e:
            logging.error(f"Xatolik: {e}")
            await thinking.edit_text("Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.")


def setup_money_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("conversions", 0)
    info.setdefault("rates", {"USD": 12650, "EUR": 13700, "RUB": 140})
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_premium_system(dp, token, admin_id)
    setup_global_buttons_handler(dp, lambda m, s: mstart(m))

    def admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Valyuta qo'shish"), KeyboardButton(text="✏️ Kursni yangilash")],
            [KeyboardButton(text="🗑 Valyutani o'chirish"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
            [KeyboardButton(text="💳 To'lov tizimlar"), KeyboardButton(text="💎 Premium")],
        ] + get_global_button_rows(), resize_keyboard=True)

    def currency_kb():
        buttons = [[InlineKeyboardButton(text=code, callback_data=f"curr_{code}")] for code in info["rates"]]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @dp.message(Command("start"))
    async def mstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            rates_text = "\n".join(f"{c}: {r:,} so'm" for c, r in info["rates"].items()) or "Hozircha valyuta yo'q."
            await message.answer(f"💱 <b>Pul bot boshqaruvi</b>\n\nJoriy kurslar:\n{rates_text}", reply_markup=admin_kb())
            return
        if not await require_subscription(message, info, admin_id):
            return
        if not info["rates"]:
            await message.answer("Hozircha valyutalar qo'shilmagan.")
            return
        await message.answer("💱 Valyutani tanlang:", reply_markup=currency_kb())

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def money_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"💱 Konvertatsiyalar: {info['stats']['conversions']}\n💰 Valyutalar soni: {len(info['rates'])}"
        )

    @dp.message(F.text == "➕ Valyuta qo'shish")
    async def add_currency_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Valyuta kodini yozing (masalan: GBP, CNY, TRY, KZT):")
        await state.set_state(CurrencyAdd.waiting_code)

    @dp.message(CurrencyAdd.waiting_code)
    async def add_currency_code(message: Message, state: FSMContext):
        code = message.text.strip().upper()
        if not code.isalpha() or len(code) > 6:
            await message.answer("❌ Kodni to'g'ri kiriting (masalan: GBP).")
            return
        await state.update_data(code=code)
        await message.answer(f"1 {code} necha so'm? (faqat raqam):")
        await state.set_state(CurrencyAdd.waiting_rate)

    @dp.message(CurrencyAdd.waiting_rate)
    async def add_currency_rate(message: Message, state: FSMContext):
        try:
            rate = float(message.text.strip().replace(" ", "").replace(",", "."))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        state_data = await state.get_data()
        code = state_data.get("code")
        info["rates"][code] = rate
        save_data()
        await message.answer(f"✅ {code} qo'shildi: 1 {code} = {rate:,} so'm")
        await state.clear()

    @dp.message(F.text == "✏️ Kursni yangilash")
    async def update_rate_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["rates"]:
            await message.answer("Hozircha valyuta yo'q. Avval qo'shing.")
            return
        buttons = [[InlineKeyboardButton(text=f"{c} ({r:,})", callback_data=f"updrate_{c}")] for c, r in info["rates"].items()]
        await message.answer("Qaysi valyuta kursini yangilaymiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("updrate_"))
    async def update_rate_pick(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        code = callback.data.split("_", 1)[1]
        await state.update_data(update_code=code)
        await callback.message.answer(f"1 {code} uchun yangi kursni kiriting (so'm):")
        await state.set_state(CurrencyUpdate.waiting_rate)
        await callback.answer()

    @dp.message(CurrencyUpdate.waiting_rate)
    async def update_rate_save(message: Message, state: FSMContext):
        try:
            rate = float(message.text.strip().replace(" ", "").replace(",", "."))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        state_data = await state.get_data()
        code = state_data.get("update_code")
        if code in info["rates"]:
            info["rates"][code] = rate
            save_data()
            await message.answer(f"✅ {code} kursi yangilandi: {rate:,} so'm")
        await state.clear()

    @dp.message(F.text == "🗑 Valyutani o'chirish")
    async def del_currency_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["rates"]:
            await message.answer("O'chirish uchun valyuta yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=c, callback_data=f"delcurr_{c}")] for c in info["rates"]]
        await message.answer("O'chirmoqchi bo'lgan valyutani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("delcurr_"))
    async def del_currency_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        code = callback.data.split("_", 1)[1]
        removed = info["rates"].pop(code, None)
        save_data()
        if removed is not None:
            await callback.message.answer(f"🗑 {code} o'chirildi.")
        await callback.answer()

    @dp.callback_query(F.data.startswith("curr_"))
    async def pick_currency(callback: CallbackQuery, state: FSMContext):
        currency = callback.data.split("_", 1)[1]
        await state.update_data(currency=currency)
        await callback.message.answer(f"{currency} miqdorini kiriting:")
        await state.set_state(MoneyAmount.waiting_amount)
        await callback.answer()

    @dp.message(MoneyAmount.waiting_amount)
    async def calc_amount(message: Message, state: FSMContext):
        try:
            amount = float(message.text.strip().replace(",", "."))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        state_data = await state.get_data()
        currency = state_data.get("currency", "USD")
        rate = info["rates"].get(currency, 0)
        total = amount * rate
        info["stats"]["conversions"] += 1
        save_data()
        await message.answer(f"💱 {amount:,.2f} {currency} = <b>{total:,.0f} so'm</b>")
        await state.clear()



def setup_translate_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("translations", 0)
    info.setdefault("user_lang", {})
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_premium_system(dp, token, admin_id)
    setup_global_buttons_handler(dp, lambda m, s: tstart(m))

    LANGS = {"uz": "🇺🇿 O'zbek", "en": "🇬🇧 English", "ru": "🇷🇺 Русский", "tr": "🇹🇷 Türkçe", "ar": "🇸🇦 العربية"}

    def lang_kb():
        buttons = [[InlineKeyboardButton(text=name, callback_data=f"lang_{code}")] for code, name in LANGS.items()]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def lang_chosen_kb():
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔄 Tilni o'zgartirish")]] + get_global_button_rows(), resize_keyboard=True)

    def admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📊 Statistika"), KeyboardButton(text="📢 Xabar yuborish")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
            [KeyboardButton(text="💳 To'lov tizimlar"), KeyboardButton(text="💎 Premium")],
        ] + get_global_button_rows(), resize_keyboard=True)

    @dp.message(Command("start"))
    async def tstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer("🌐 <b>Tarjimon bot boshqaruvi</b>", reply_markup=admin_kb())
            return
        if not await require_subscription(message, info, admin_id):
            return
        await message.answer("🌐 Qaysi tilga tarjima qilishni xohlaysiz?", reply_markup=lang_kb())

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def translate_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {len(info['users'])}\n🌐 Tarjimalar: {info['stats']['translations']}"
        )

    @dp.message(F.text == "📢 Xabar yuborish")
    async def t_newpost(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("E'lon matnini yuboring:")
        await state.set_state(PostFlow.waiting_text)

    @dp.message(PostFlow.waiting_text)
    async def t_post_text(message: Message, state: FSMContext):
        await state.update_data(text=message.text)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="t_post_confirm")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="t_post_cancel")],
        ])
        await message.answer(f"{len(info['users'])} kishiga yuborilsinmi?\n\n{message.text}", reply_markup=buttons)
        await state.set_state(PostFlow.waiting_confirm)

    @dp.callback_query(F.data == "t_post_confirm", PostFlow.waiting_confirm)
    async def t_post_confirm(callback: CallbackQuery, state: FSMContext):
        state_data = await state.get_data()
        text = state_data.get("text", "")
        count = 0
        for uid in info["users"]:
            try:
                await callback.bot.send_message(uid, text)
                count += 1
            except Exception:
                pass
        await callback.message.edit_text(f"✅ {count} kishiga yuborildi.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "t_post_cancel", PostFlow.waiting_confirm)
    async def t_post_cancel(callback: CallbackQuery, state: FSMContext):
        await callback.message.edit_text("❌ Bekor qilindi.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data.startswith("lang_"))
    async def pick_lang(callback: CallbackQuery):
        code = callback.data.split("_", 1)[1]
        info.setdefault("user_lang", {})
        info["user_lang"][str(callback.from_user.id)] = code
        save_data()
        await callback.message.answer(
            f"✅ Til tanlandi: {LANGS[code]}\n\nEndi tarjima qilmoqchi bo'lgan matningizni yuboring.",
            reply_markup=lang_chosen_kb(),
        )
        await callback.answer()

    @dp.message(F.text == "🔄 Tilni o'zgartirish")
    async def change_lang(message: Message):
        await message.answer("🌐 Qaysi tilga tarjima qilishni xohlaysiz?", reply_markup=lang_kb())

    @dp.message(F.text)
    async def do_translate(message: Message):
        if not await check_active(message, info, admin_id):
            return
        if not await require_subscription(message, info, admin_id):
            return
        uid = str(message.from_user.id)
        lang = info.get("user_lang", {}).get(uid)
        if not lang:
            await message.answer("Avval tilni tanlang:", reply_markup=lang_kb())
            return
        lang_name = LANGS.get(lang, lang)
        thinking = await message.answer("💭 Tarjima qilinmoqda...")
        try:
            result = await ask_gemini(
                f"Translate the following text to {lang_name}. Respond with ONLY the translation, nothing else:\n\n{message.text}"
            )
            info["stats"]["translations"] += 1
            save_data()
            await thinking.edit_text(result)
        except Exception as e:
            logging.error(f"Xatolik: {e}")
            await thinking.edit_text("Xatolik yuz berdi.")



def setup_taxi_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("orders", 0)
    info.setdefault("orders", {})
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_premium_system(dp, token, admin_id)
    setup_global_buttons_handler(dp, lambda m, s: tstart(m))

    def taxi_admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
            [KeyboardButton(text="💳 To'lov tizimlar"), KeyboardButton(text="💎 Premium")],
        ] + get_global_button_rows(), resize_keyboard=True)

    def taxi_order_kb():
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🚕 Taksi chaqirish")]], resize_keyboard=True)

    def phone_kb():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True,
        )

    @dp.message(Command("start"))
    async def tstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer("🚕 <b>Taksi bot boshqaruvi</b>\n\nPastdagi menyudan foydalaning 👇", reply_markup=taxi_admin_kb())
            return
        if not await require_subscription(message, info, admin_id):
            return
        await message.answer("🚕 Taksi chaqirish uchun quyidagi tugmani bosing 👇", reply_markup=taxi_order_kb())

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def taxi_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"🚕 Buyurtmalar: {info['stats']['orders']}"
        )

    @dp.message(F.text == "🚕 Taksi chaqirish")
    async def taxi_order_start(message: Message, state: FSMContext):
        if not await check_active(message, info, admin_id):
            return
        if not await require_subscription(message, info, admin_id):
            return
        await message.answer("📍 Qayerdan olib ketish kerak? (manzilni yozing)")
        await state.set_state(TaxiOrder.waiting_from)

    @dp.message(TaxiOrder.waiting_from)
    async def taxi_from_process(message: Message, state: FSMContext):
        await state.update_data(taxi_from=message.text.strip())
        await message.answer("📍 Qayerga borasiz? (manzilni yozing)")
        await state.set_state(TaxiOrder.waiting_to)

    @dp.message(TaxiOrder.waiting_to)
    async def taxi_to_process(message: Message, state: FSMContext):
        await state.update_data(taxi_to=message.text.strip())
        await message.answer("📱 Telefon raqamingizni yuboring:", reply_markup=phone_kb())
        await state.set_state(TaxiOrder.waiting_phone)

    async def finalize_taxi_order(message: Message, state: FSMContext, phone: str):
        fsm_data = await state.get_data()
        from_addr = fsm_data.get("taxi_from", "-")
        to_addr = fsm_data.get("taxi_to", "-")
        uid = message.from_user.id
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        order_id = uuid.uuid4().hex[:8]
        info["orders"][order_id] = {
            "user_id": uid, "from": from_addr, "to": to_addr, "phone": phone,
            "status": "kutilmoqda", "created_at": datetime.now().isoformat(),
        }
        info["stats"]["orders"] += 1
        save_data()
        text = (
            "🚕 <b>Yangi taksi buyurtmasi</b>\n\n"
            f"📍 Qayerdan: {from_addr}\n"
            f"📍 Qayerga: {to_addr}\n"
            f"📱 Telefon: {phone}\n"
            f"👤 Mijoz: {uname} (ID: <code>{uid}</code>)"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Qabul qilindi", callback_data=f"taxiaccept_{uid}_{order_id}"),
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"taxicancel_{uid}_{order_id}"),
        ]])
        for aid in info.get("admin_ids", [admin_id]):
            try:
                await message.bot.send_message(chat_id=aid, text=text, reply_markup=kb)
            except Exception as e:
                logging.error(f"Adminga buyurtma yuborishda xato ({aid}): {e}")
        await message.answer(
            "✅ Buyurtmangiz qabul qilindi! Tez orada haydovchi siz bilan bog'lanadi.",
            reply_markup=taxi_order_kb(),
        )
        await state.clear()

    @dp.message(TaxiOrder.waiting_phone, F.contact)
    async def taxi_phone_contact(message: Message, state: FSMContext):
        await finalize_taxi_order(message, state, message.contact.phone_number)

    @dp.message(TaxiOrder.waiting_phone, F.text)
    async def taxi_phone_text(message: Message, state: FSMContext):
        await finalize_taxi_order(message, state, message.text.strip())

    @dp.callback_query(F.data.startswith("taxiaccept_"))
    async def taxi_accept_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        _, target_uid, order_id = callback.data.split("_", 2)
        order = info["orders"].get(order_id)
        if order:
            order["status"] = "qabul qilindi"
            save_data()
        try:
            await callback.bot.send_message(
                chat_id=int(target_uid),
                text="✅ <b>Buyurtmangiz qabul qilindi!</b>\n\nHaydovchi tez orada siz bilan bog'lanadi. 🚕",
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga xabar yuborishda xato: {e}")
        await callback.message.edit_text(callback.message.text + "\n\n✅ <b>QABUL QILINDI</b>")
        await callback.answer()

    @dp.callback_query(F.data.startswith("taxicancel_"))
    async def taxi_cancel_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        _, target_uid, order_id = callback.data.split("_", 2)
        order = info["orders"].get(order_id)
        if order:
            order["status"] = "bekor qilindi"
            save_data()
        try:
            await callback.bot.send_message(
                chat_id=int(target_uid),
                text="❌ <b>Uzr, hozircha buyurtmangizni bajarib bo'lmaydi.</b>\n\nBiroz vaqtdan so'ng qayta urinib ko'ring.",
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga xabar yuborishda xato: {e}")
        await callback.message.edit_text(callback.message.text + "\n\n❌ <b>BEKOR QILINDI</b>")
        await callback.answer()



# ---------- Stars sotish boti ----------
def setup_stars_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("orders", 0)
    info["stats"].setdefault("stars_sold", 0)
    info["stats"].setdefault("revenue", 0)
    info.setdefault("star_packages", {})
    info.setdefault("star_orders", {})
    info.setdefault("blocked_users", [])
    info.setdefault("min_stars", 50)
    info.setdefault("max_stars", 10000)
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_premium_system(dp, token, admin_id)
    setup_global_buttons_handler(dp, lambda m, s: sstart(m))

    # ---------- Klaviaturalar ----------
    def stars_admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📦 Paketlar"), KeyboardButton(text="🧾 Buyurtmalar")],
            [KeyboardButton(text="👥 Foydalanuvchilar"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📢 Xabar yuborish"), KeyboardButton(text="⚙️ Sozlamalar")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
            [KeyboardButton(text="💳 To'lov tizimlar"), KeyboardButton(text="💎 Premium")],
        ] + get_global_button_rows(), resize_keyboard=True)

    def packages_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Paket qo'shish"), KeyboardButton(text="📋 Paketlar ro'yxati")],
            [KeyboardButton(text="✏️ Paketni tahrirlash"), KeyboardButton(text="➖ Paketni o'chirish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def orders_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🕓 Kutilayotgan"), KeyboardButton(text="✅ Bajarilgan")],
            [KeyboardButton(text="❌ Rad etilgan")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def users_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📋 Ro'yxat"), KeyboardButton(text="🔍 Qidirish")],
            [KeyboardButton(text="🚫 Bloklash"), KeyboardButton(text="✅ Blokdan chiqarish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def settings_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🔢 Min/Max miqdor")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def customer_kb():
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⭐ Stars sotib olish")]], resize_keyboard=True)

    def packages_inline_kb():
        buttons = [
            [InlineKeyboardButton(text=f"⭐ {p['stars']:,} — {p['price']:,} so'm", callback_data=f"starpkg_{pid}")]
            for pid, p in info["star_packages"].items()
        ]
        buttons.append([InlineKeyboardButton(text="✏️ Boshqa miqdor kiritish", callback_data="starcustom")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def is_blocked(uid: int) -> bool:
        return uid in info.get("blocked_users", [])

    # ---------- /start ----------
    @dp.message(Command("start"))
    async def sstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer("⭐ <b>Stars sotish boti — boshqaruv</b>", reply_markup=stars_admin_kb())
            return
        if is_blocked(uid):
            await message.answer("🚫 Siz ushbu botdan foydalanish huquqidan mahrum qilingansiz.")
            return
        if not await require_subscription(message, info, admin_id):
            return
        await message.answer(
            "⭐ <b>Stars sotib olish</b>\n\nTayyor paketlardan birini tanlang yoki xohlagan miqdoringizni kiriting 👇",
            reply_markup=packages_inline_kb(),
        )
        await message.answer("Pastdagi menyudan ham foydalanishingiz mumkin 👇", reply_markup=customer_kb())

    @dp.message(F.text == "⭐ Stars sotib olish")
    async def stars_buy_menu(message: Message):
        if not await check_active(message, info, admin_id):
            return
        if is_blocked(message.from_user.id):
            await message.answer("🚫 Siz ushbu botdan foydalanish huquqidan mahrum qilingansiz.")
            return
        if not await require_subscription(message, info, admin_id):
            return
        await message.answer("⭐ Paketni tanlang yoki miqdor kiriting:", reply_markup=packages_inline_kb())

    @dp.message(F.text == "◀️ Orqaga")
    async def stars_back_to_admin(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("⭐ <b>Boshqaruv paneli</b>", reply_markup=stars_admin_kb())

    # ---------- Statistika ----------
    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def stars_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        pending = sum(1 for o in info["star_orders"].values() if o["status"] == "kutilmoqda")
        done = sum(1 for o in info["star_orders"].values() if o["status"] == "bajarildi")
        await message.answer(
            "📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"🧾 Jami buyurtmalar: {info['stats']['orders']}\n"
            f"⭐ Sotilgan Stars: {info['stats']['stars_sold']:,}\n"
            f"💰 Jami tushum: {info['stats']['revenue']:,} so'm\n\n"
            f"🕓 Kutilayotgan: {pending}\n"
            f"✅ Bajarilgan: {done}"
        )

    # ---------- Paketlar boshqaruvi ----------
    @dp.message(F.text == "📦 Paketlar")
    async def packages_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("📦 <b>Paketlar boshqaruvi</b>", reply_markup=packages_menu_kb())

    @dp.message(F.text == "➕ Paket qo'shish")
    async def pkg_add_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Nechta Stars? (faqat raqam, masalan: 100):")
        await state.set_state(StarPackageAdd.waiting_stars)

    @dp.message(StarPackageAdd.waiting_stars)
    async def pkg_add_stars(message: Message, state: FSMContext):
        try:
            stars = int(message.text.strip().replace(" ", ""))
            if stars <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting.")
            return
        await state.update_data(pkg_stars=stars)
        await message.answer("Narxini kiriting (so'mda, faqat raqam):")
        await state.set_state(StarPackageAdd.waiting_price)

    @dp.message(StarPackageAdd.waiting_price)
    async def pkg_add_price(message: Message, state: FSMContext):
        try:
            price = int(message.text.strip().replace(" ", ""))
            if price <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting.")
            return
        fsm_data = await state.get_data()
        pid = uuid.uuid4().hex[:8]
        info["star_packages"][pid] = {"stars": fsm_data["pkg_stars"], "price": price}
        save_data()
        await message.answer(f"✅ Paket qo'shildi: ⭐ {fsm_data['pkg_stars']:,} — {price:,} so'm")
        await state.clear()

    @dp.message(F.text == "📋 Paketlar ro'yxati")
    async def pkg_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["star_packages"]:
            await message.answer("Paketlar mavjud emas.")
        else:
            lines = [f"• ⭐ {p['stars']:,} — {p['price']:,} so'm" for p in info["star_packages"].values()]
            await message.answer("📦 Paketlar:\n\n" + "\n".join(lines))

    @dp.message(F.text == "✏️ Paketni tahrirlash")
    async def pkg_edit_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["star_packages"]:
            await message.answer("Tahrirlash uchun paket yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=f"⭐ {p['stars']:,} — {p['price']:,} so'm", callback_data=f"pkgedit_{pid}")] for pid, p in info["star_packages"].items()]
        await message.answer("Tahrirlamoqchi bo'lgan paketni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("pkgedit_"))
    async def pkg_edit_pick(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        pid = callback.data.split("_", 1)[1]
        await state.update_data(edit_pkg_id=pid)
        await callback.message.answer("Yangi narxni kiriting (so'mda):")
        await state.set_state(StarPackageEdit.waiting_price)
        await callback.answer()

    @dp.message(StarPackageEdit.waiting_price)
    async def pkg_edit_save(message: Message, state: FSMContext):
        try:
            price = int(message.text.strip().replace(" ", ""))
            if price <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting.")
            return
        fsm_data = await state.get_data()
        pid = fsm_data.get("edit_pkg_id")
        if pid in info["star_packages"]:
            info["star_packages"][pid]["price"] = price
            save_data()
            await message.answer(f"✅ Narx yangilandi: {price:,} so'm")
        await state.clear()

    @dp.message(F.text == "➖ Paketni o'chirish")
    async def pkg_del_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["star_packages"]:
            await message.answer("O'chirish uchun paket yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=f"⭐ {p['stars']:,} — {p['price']:,} so'm", callback_data=f"pkgdel_{pid}")] for pid, p in info["star_packages"].items()]
        await message.answer("O'chirmoqchi bo'lgan paketni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("pkgdel_"))
    async def pkg_del_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        pid = callback.data.split("_", 1)[1]
        removed = info["star_packages"].pop(pid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: ⭐ {removed['stars']:,}")
        await callback.answer()

    # ---------- Sozlamalar ----------
    @dp.message(F.text == "⚙️ Sozlamalar")
    async def settings_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"⚙️ <b>Sozlamalar</b>\n\nMin: {info['min_stars']:,} ⭐\nMax: {info['max_stars']:,} ⭐",
            reply_markup=settings_menu_kb(),
        )

    @dp.message(F.text == "🔢 Min/Max miqdor")
    async def minmax_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Minimal Stars miqdorini kiriting:")
        await state.set_state(StarSettings.waiting_min)

    @dp.message(StarSettings.waiting_min)
    async def minmax_min(message: Message, state: FSMContext):
        try:
            val = int(message.text.strip())
            if val <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting.")
            return
        await state.update_data(min_val=val)
        await message.answer("Maksimal Stars miqdorini kiriting:")
        await state.set_state(StarSettings.waiting_max)

    @dp.message(StarSettings.waiting_max)
    async def minmax_max(message: Message, state: FSMContext):
        try:
            val = int(message.text.strip())
            if val <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting.")
            return
        fsm_data = await state.get_data()
        min_val = fsm_data.get("min_val", 50)
        if val < min_val:
            await message.answer("❌ Maksimal miqdor minimaldan kichik bo'lmasligi kerak.")
            return
        info["min_stars"] = min_val
        info["max_stars"] = val
        save_data()
        await message.answer(f"✅ Saqlandi: {min_val:,} – {val:,} ⭐")
        await state.clear()

    # ---------- Foydalanuvchilar ----------
    @dp.message(F.text == "👥 Foydalanuvchilar")
    async def users_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(f"👥 Jami foydalanuvchilar: {len(info['users'])}", reply_markup=users_menu_kb())

    @dp.message(F.text == "📋 Ro'yxat")
    async def users_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        users = info["users"][-30:]
        text = f"👥 Oxirgi {len(users)} foydalanuvchi (jami {len(info['users'])}):\n\n" + "\n".join(f"• <code>{u}</code>" for u in users)
        await message.answer(text)

    @dp.message(F.text == "🔍 Qidirish")
    async def users_search_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Qidirmoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(StarUserSearch.waiting_query)

    @dp.message(StarUserSearch.waiting_query)
    async def users_search_result(message: Message, state: FSMContext):
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        found = target in info["users"]
        blocked = is_blocked(target)
        user_orders = [o for o in info["star_orders"].values() if o["user_id"] == target]
        total_bought = sum(o["stars"] for o in user_orders if o["status"] == "bajarildi")
        await message.answer(
            f"🔍 <b>Natija:</b>\n\n"
            f"🆔 ID: <code>{target}</code>\n"
            f"{'✅ Botda ro‘yxatdan o‘tgan' if found else '❌ Bu bot foydalanuvchisi emas'}\n"
            f"{'🚫 Bloklangan' if blocked else '✅ Bloklanmagan'}\n"
            f"🧾 Buyurtmalar: {len(user_orders)}\n"
            f"⭐ Xarid qilingan Stars: {total_bought:,}"
        )
        await state.clear()

    @dp.message(F.text == "🚫 Bloklash")
    async def block_user_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Bloklamoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(StarBlockUser.waiting_id)

    @dp.message(StarBlockUser.waiting_id)
    async def block_user_save(message: Message, state: FSMContext):
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        if target not in info["blocked_users"]:
            info["blocked_users"].append(target)
            save_data()
        await message.answer(f"🚫 {target} bloklandi.")
        await state.clear()

    @dp.message(F.text == "✅ Blokdan chiqarish")
    async def unblock_user_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Blokdan chiqarmoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(StarUnblockUser.waiting_id)

    @dp.message(StarUnblockUser.waiting_id)
    async def unblock_user_save(message: Message, state: FSMContext):
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        if target in info["blocked_users"]:
            info["blocked_users"].remove(target)
            save_data()
            await message.answer(f"✅ {target} blokdan chiqarildi.")
        else:
            await message.answer("Bu foydalanuvchi bloklanmagan.")
        await state.clear()

    # ---------- Xabar yuborish ----------
    @dp.message(F.text == "📢 Xabar yuborish")
    async def stars_broadcast_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("E'lon matnini yuboring:")
        await state.set_state(PostFlow.waiting_text)

    @dp.message(PostFlow.waiting_text)
    async def stars_broadcast_text(message: Message, state: FSMContext):
        await state.update_data(text=message.text)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="stars_post_confirm")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="stars_post_cancel")],
        ])
        await message.answer(f"{len(info['users'])} kishiga yuborilsinmi?\n\n{message.text}", reply_markup=buttons)
        await state.set_state(PostFlow.waiting_confirm)

    @dp.callback_query(F.data == "stars_post_confirm", PostFlow.waiting_confirm)
    async def stars_broadcast_confirm(callback: CallbackQuery, state: FSMContext):
        fsm_data = await state.get_data()
        text = fsm_data.get("text", "")
        count = 0
        for uid in info["users"]:
            try:
                await callback.bot.send_message(uid, text)
                count += 1
            except Exception:
                pass
        await callback.message.edit_text(f"✅ {count} ta foydalanuvchiga yuborildi.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "stars_post_cancel", PostFlow.waiting_confirm)
    async def stars_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
        await callback.message.edit_text("❌ Bekor qilindi.")
        await state.clear()
        await callback.answer()

    # ---------- Buyurtmalar ro'yxati (admin) ----------
    def order_status_list_text(status: str, emoji: str):
        orders = [(oid, o) for oid, o in info["star_orders"].items() if o["status"] == status]
        if not orders:
            return f"{emoji} Bu holatda buyurtma yo'q."
        lines = [f"#{oid[:6]} — ⭐{o['stars']:,} — {o['price']:,} so'm — ID:{o['user_id']}" for oid, o in orders[-20:]]
        return f"{emoji} <b>Buyurtmalar ({len(orders)}):</b>\n\n" + "\n".join(lines)

    @dp.message(F.text == "🧾 Buyurtmalar")
    async def orders_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("🧾 <b>Buyurtmalar boshqaruvi</b>", reply_markup=orders_menu_kb())

    @dp.message(F.text == "🕓 Kutilayotgan")
    async def orders_pending(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(order_status_list_text("kutilmoqda", "🕓"))

    @dp.message(F.text == "✅ Bajarilgan")
    async def orders_done(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(order_status_list_text("bajarildi", "✅"))

    @dp.message(F.text == "❌ Rad etilgan")
    async def orders_rejected(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(order_status_list_text("bekor qilindi", "❌"))

    # ---------- Mijoz: paket tanlash / miqdor kiritish ----------
    @dp.callback_query(F.data.startswith("starpkg_"))
    async def starpkg_chosen_cb(callback: CallbackQuery, state: FSMContext):
        pid = callback.data.split("_", 1)[1]
        pkg = info["star_packages"].get(pid)
        if not pkg:
            await callback.answer("❌ Bu paket endi mavjud emas.", show_alert=True)
            return
        await start_star_payment(callback.message, callback.from_user.id, state, pkg["stars"], pkg["price"])
        await callback.answer()

    @dp.callback_query(F.data == "starcustom")
    async def starcustom_cb(callback: CallbackQuery, state: FSMContext):
        await callback.message.answer(
            f"Nechta Stars xohlaysiz? ({info['min_stars']:,} – {info['max_stars']:,} oralig'ida):"
        )
        await state.set_state(StarOrderCustom.waiting_amount)
        await callback.answer()

    @dp.message(StarOrderCustom.waiting_amount)
    async def starcustom_amount(message: Message, state: FSMContext):
        try:
            stars = int(message.text.strip().replace(" ", ""))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        if stars < info["min_stars"] or stars > info["max_stars"]:
            await message.answer(f"❌ Miqdor {info['min_stars']:,} – {info['max_stars']:,} oralig'ida bo'lishi kerak.")
            return
        # Narxni eng yaqin paket nisbati asosida yoki oddiy formulaga ko'ra hisoblaymiz
        if info["star_packages"]:
            sample = next(iter(info["star_packages"].values()))
            price_per_star = sample["price"] / sample["stars"]
        else:
            price_per_star = 150
        price = round(stars * price_per_star)
        await start_star_payment(message, message.from_user.id, state, stars, price)

    async def start_star_payment(target_message: Message, uid: int, state: FSMContext, stars: int, price: int):
        await state.update_data(order_stars=stars, order_price=price)
        if not info["payment_systems"]:
            await target_message.answer("Hozircha to'lov tizimlari mavjud emas. Administratorga murojaat qiling.")
            return
        buttons = [[InlineKeyboardButton(text=p["name"], callback_data=f"starpay_{pid}")] for pid, p in info["payment_systems"].items()]
        await target_message.answer(
            f"⭐ Miqdor: {stars:,}\n💰 Narx: {price:,} so'm\n\n💳 To'lov tizimini tanlang:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @dp.callback_query(F.data.startswith("starpay_"))
    async def starpay_chosen_cb(callback: CallbackQuery, state: FSMContext):
        pid = callback.data.split("_", 1)[1]
        psys = info["payment_systems"].get(pid)
        if not psys:
            await callback.answer("❌ Ma'lumot topilmadi, qaytadan urinib ko'ring.", show_alert=True)
            return
        fsm_data = await state.get_data()
        price = fsm_data.get("order_price", 0)
        await state.set_state(StarOrderCheck.waiting_check)
        text = (
            f"💳 <b>{psys['name']}</b>\n\n"
            f"🔢 Raqami: <code>{psys['number']}</code>\n"
            f"👤 Egasi: {psys['owner']}\n\n"
            f"💰 To'lov summasi: {price:,} so'm\n\n"
            "To'lovni amalga oshirgach, to'lov chekini (skrinshot) shu yerga yuboring."
        )
        await callback.message.answer(text)
        await callback.answer()

    @dp.message(StarOrderCheck.waiting_check, F.photo)
    async def star_check_received(message: Message, state: FSMContext):
        fsm_data = await state.get_data()
        stars = fsm_data.get("order_stars", 0)
        price = fsm_data.get("order_price", 0)
        uid = message.from_user.id
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        order_id = uuid.uuid4().hex[:8]
        info["star_orders"][order_id] = {
            "user_id": uid, "stars": stars, "price": price,
            "status": "kutilmoqda", "created_at": datetime.now().isoformat(),
        }
        info["stats"]["orders"] += 1
        save_data()
        caption = (
            "🧾 <b>Yangi Stars buyurtmasi</b>\n\n"
            f"⭐ Miqdor: {stars:,}\n"
            f"💰 Narx: {price:,} so'm\n"
            f"👤 Foydalanuvchi: {uname} (ID: <code>{uid}</code>)"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"starapprove_{uid}_{order_id}"),
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"starreject_{uid}_{order_id}"),
        ]])
        for aid in info.get("admin_ids", [admin_id]):
            try:
                await message.bot.send_photo(chat_id=aid, photo=message.photo[-1].file_id, caption=caption, reply_markup=kb)
            except Exception as e:
                logging.error(f"Adminga chek yuborishda xato ({aid}): {e}")
        await message.answer(
            "✅ Chekingiz qabul qilindi!\n\n"
            "Adminlar tomonidan tez orada ko'rib chiqiladi. Tasdiqlansa, Stars hisobingizga tez orada yuboriladi."
        )
        await state.clear()

    @dp.callback_query(F.data.startswith("starapprove_"))
    async def star_approve_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        _, target_uid, order_id = callback.data.split("_", 2)
        order = info["star_orders"].get(order_id)
        if order:
            order["status"] = "bajarildi"
            info["stats"]["stars_sold"] += order["stars"]
            info["stats"]["revenue"] += order["price"]
            save_data()
        try:
            await callback.bot.send_message(
                chat_id=int(target_uid),
                text="✅ <b>To'lovingiz tasdiqlandi!</b>\n\n⭐ Stars tez orada hisobingizga yuboriladi. Rahmat!",
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga xabar yuborishda xato: {e}")
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ <b>TASDIQLANDI</b>")
        await callback.answer()

    @dp.callback_query(F.data.startswith("starreject_"))
    async def star_reject_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        _, target_uid, order_id = callback.data.split("_", 2)
        order = info["star_orders"].get(order_id)
        if order:
            order["status"] = "bekor qilindi"
            save_data()
        try:
            await callback.bot.send_message(
                chat_id=int(target_uid),
                text="❌ <b>To'lovingiz admin tomonidan bekor qilindi.</b>\n\nAgar savollaringiz bo'lsa, administrator bilan bog'laning.",
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga xabar yuborishda xato: {e}")
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ <b>BEKOR QILINDI</b>")
        await callback.answer()



# ---------- Kino bot ----------
def setup_kino_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("requests", 0)
    info.setdefault("categories", {})            # {cid: name}
    info.setdefault("featured", [])               # [code, ...]
    info.setdefault("request_counts", {})         # {code: count}
    info.setdefault("blocked_users", [])
    info.setdefault("ads", {})                     # {aid: {"text":..., "active":bool}}
    info.setdefault("referral_bonus_days", 0)
    info.setdefault("referrals", {})               # {referrer_uid_str: [uid,...]}
    info.setdefault("new_content_notify", False)
    info.setdefault("maintenance_mode", False)
    info.setdefault("welcome_text", "🎬 Film kodini yuboring, men uni topib beraman.")
    info.setdefault("help_text", "Savol va takliflar uchun admin bilan bog'laning.")
    info.setdefault("ratings", {})                 # {code: {"total": int, "count": int, "by_user": {uid: stars}}}
    info.setdefault("vip_codes", [])               # [code, ...] — faqat Premium foydalanuvchilar uchun
    info.setdefault("series_subscribers", {})      # {code: [uid, ...]}
    info.setdefault("moderators", [])              # faqat kontent qo'sha oladigan cheklangan adminlar
    info.setdefault("user_activity", {})           # {str(uid): count}
    info.setdefault("auto_report_enabled", False)
    info.setdefault("auto_report_hour", 9)
    info.setdefault("last_report_date", "")
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_premium_system(dp, token, admin_id)
    setup_global_buttons_handler(dp, lambda m, s: kstart(m))

    def is_blocked(uid: int) -> bool:
        return uid in info.get("blocked_users", [])

    def is_moderator(uid: int) -> bool:
        return is_admin(info, uid) or uid in info.get("moderators", [])

    def is_pro() -> bool:
        return info.get("type") == "kino_pro"

    def is_premium_user(uid: int) -> bool:
        return is_admin(info, uid) or is_premium_active(info, uid)

    # ---------- Klaviaturalar ----------
    def ultra_admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🎬 Kontent"), KeyboardButton(text="🏷 Kategoriyalar")],
            [KeyboardButton(text="⭐ Tavsiyalar"), KeyboardButton(text="📈 TOP reyting")],
            [KeyboardButton(text="👥 Foydalanuvchilar"), KeyboardButton(text="📢 Xabar va reklama")],
            [KeyboardButton(text="🎁 Referal"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
            [KeyboardButton(text="💳 To'lov tizimlar"), KeyboardButton(text="💎 Premium")],
            [KeyboardButton(text="👮 Moderatorlar"), KeyboardButton(text="⚙️ Sozlamalar")],
            [KeyboardButton(text="📤 Eksport")],
        ] + get_global_button_rows(), resize_keyboard=True)

    def content_menu_kb():
        keyboard = [
            [KeyboardButton(text="🎬 Film qo'shish"), KeyboardButton(text="📺 Serial qo'shish")],
            [KeyboardButton(text="➕ Seriallarga qism qo'shish"), KeyboardButton(text="📋 Filmlar ro'yxati")],
            [KeyboardButton(text="🔍 Kod bo'yicha qidirish"), KeyboardButton(text="✏️ Tavsifni tahrirlash")],
            [KeyboardButton(text="🗑 Film o'chirish")],
        ]
        if is_pro():
            keyboard.append([KeyboardButton(text="🔒 VIP kino qo'shish"), KeyboardButton(text="🔒 VIP qilib belgilash")])
        keyboard.append([KeyboardButton(text="🗓 Chiqish sanasini belgilash")])
        keyboard.append([KeyboardButton(text="◀️ Orqaga")])
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

    def categories_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Kategoriya qo'shish"), KeyboardButton(text="📋 Kategoriyalar ro'yxati")],
            [KeyboardButton(text="🔗 Filmga kategoriya biriktirish"), KeyboardButton(text="➖ Kategoriya o'chirish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def featured_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Tavsiyaga qo'shish"), KeyboardButton(text="📋 Tavsiyalar ro'yxati")],
            [KeyboardButton(text="➖ Tavsiyadan olib tashlash")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def top_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🔥 Eng ko'p so'ralganlar"), KeyboardButton(text="📅 Bugungi faollik")],
            [KeyboardButton(text="⭐ Reytinglar"), KeyboardButton(text="🏆 Faol foydalanuvchilar")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def users_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📋 Ro'yxat"), KeyboardButton(text="🔍 Qidirish")],
            [KeyboardButton(text="🚫 Bloklash"), KeyboardButton(text="✅ Blokdan chiqarish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def broadcast_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📢 Ommaviy xabar yuborish")],
            [KeyboardButton(text="📣 Reklama joylash"), KeyboardButton(text="📋 Reklamalar ro'yxati")],
            [KeyboardButton(text="➖ Reklamani o'chirish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def referral_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🎯 Bonusni sozlash"), KeyboardButton(text="📊 Referal statistikasi")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def moderators_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Moderator qo'shish"), KeyboardButton(text="📋 Moderatorlar ro'yxati")],
            [KeyboardButton(text="➖ Moderatorni o'chirish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def settings_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="✏️ Salomlashuv matni"), KeyboardButton(text="📄 Yordam matni")],
            [KeyboardButton(text="🔔 Yangi kontent bildirishnomasi"), KeyboardButton(text="🛠 Texnik tanaffus")],
            [KeyboardButton(text="📅 Avtomatik hisobot")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    BACK_BUTTONS = {
        "🎬 Kontent", "🏷 Kategoriyalar", "⭐ Tavsiyalar", "📈 TOP reyting",
        "👥 Foydalanuvchilar", "📢 Xabar va reklama", "🎁 Referal", "⚙️ Sozlamalar",
        "👮 Moderatorlar",
    }

    @dp.message(F.text == "◀️ Orqaga")
    async def ultra_back(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("⭐ <b>Boshqaruv paneli</b>", reply_markup=ultra_admin_kb())

    # ---------- /start ----------
    @dp.message(Command("start"))
    async def kstart(message: Message):
        uid = message.from_user.id
        args = message.text.split(maxsplit=1)
        if uid not in info["users"]:
            info["users"].append(uid)
            if len(args) > 1 and args[1].startswith("ref_"):
                try:
                    ref_uid = int(args[1].split("_", 1)[1])
                    if ref_uid != uid:
                        info["referrals"].setdefault(str(ref_uid), [])
                        if uid not in info["referrals"][str(ref_uid)]:
                            info["referrals"][str(ref_uid)].append(uid)
                            bonus_days = info.get("referral_bonus_days", 0)
                            if bonus_days > 0:
                                until = datetime.now() + timedelta(days=bonus_days)
                                existing = info["premium_users"].get(str(ref_uid))
                                if existing and datetime.fromisoformat(existing["until"]) > datetime.now():
                                    until = datetime.fromisoformat(existing["until"]) + timedelta(days=bonus_days)
                                info["premium_users"][str(ref_uid)] = {"until": until.isoformat()}
                                try:
                                    await message.bot.send_message(
                                        ref_uid,
                                        f"🎁 Sizning taklifingiz bilan yangi foydalanuvchi qo'shildi! "
                                        f"Sizga {bonus_days} kunlik Premium bonus berildi."
                                    )
                                except Exception:
                                    pass
                except Exception:
                    pass
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer(
                "🎬 <b>Kino bot — boshqaruv</b>\n\nPastdagi menyudan foydalaning 👇",
                reply_markup=ultra_admin_kb(),
            )
            return
        if is_blocked(uid):
            await message.answer("🚫 Siz ushbu botdan foydalanish huquqidan mahrum qilingansiz.")
            return
        if info.get("maintenance_mode"):
            await message.answer("🛠 Bot hozircha texnik tanaffusda. Birozdan so'ng qayta urinib ko'ring.")
            return
        if not await require_subscription(message, info, admin_id):
            return
        customer_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="💎 VIP kinolar")]], resize_keyboard=True) if is_pro() else None
        await message.answer(info.get("welcome_text", "🎬 Film kodini yuboring, men uni topib beraman."), reply_markup=customer_kb)
        if info.get("featured"):
            lines = []
            for code in info["featured"]:
                m = info["movies"].get(code)
                if m:
                    title = m.get("title") or (m.get("desc", "-")[:30])
                    lines.append(f"• Kod {code} — {title}")
            if lines:
                await message.answer("⭐ <b>Tavsiya etilgan kontent:</b>\n\n" + "\n".join(lines))

    @dp.message(F.text == "💎 VIP kinolar")
    async def vip_catalog(message: Message):
        if not is_pro():
            return
        uid = message.from_user.id
        if not info["vip_codes"]:
            await message.answer("Hozircha VIP kontent mavjud emas.")
            return
        lines = []
        for code in info["vip_codes"]:
            m = info["movies"].get(code)
            if not m:
                continue
            title = m.get("title") or m.get("desc", "-")[:30]
            lines.append(f"• Kod {code} — {title}")
        if not lines:
            await message.answer("Hozircha VIP kontent mavjud emas.")
            return
        text = "🔒 <b>VIP kinolar:</b>\n\n" + "\n".join(lines)
        if not is_premium_user(uid):
            text += "\n\n💎 Bu kontentni ko'rish uchun Premium sotib oling."
        await message.answer(text)

    # ---------- Statistika ----------
    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def kino_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        today_count = info.get("daily_usage", {}).get("date") == today and len(info.get("daily_usage", {}).get("users", []))
        vip_count = len(info["vip_codes"])
        free_count = len(info["movies"]) - vip_count
        await message.answer(
            f"📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"🔍 Jami so'rovlar: {info['stats']['requests']}\n"
            f"🎞 Saqlangan kontent: {len(info['movies'])} (🆓 {free_count} / 🔒 VIP {vip_count})\n"
            f"🏷 Kategoriyalar: {len(info['categories'])}\n"
            f"⭐ Tavsiyalar: {len(info['featured'])}\n"
            f"🚫 Bloklanganlar: {len(info['blocked_users'])}\n"
            f"📅 Bugun faol foydalanuvchi: {today_count or 0}"
        )

    @dp.message(F.text == "📤 Eksport")
    async def export_movies(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"]:
            await message.answer("Eksport qilish uchun kontent yo'q.")
            return
        lines = [f"{code}\t{m.get('title', m.get('desc', '-'))[:50]}" for code, m in info["movies"].items()]
        text = "📤 Filmlar ro'yxati (kod — nomi):\n\n" + "\n".join(lines)
        if len(text) > 3900:
            text = text[:3900] + "\n\n… (ro'yxat uzun, qisqartirildi)"
        await message.answer(text)

    # ---------- Kontent submenu ----------
    @dp.message(F.text == "🎬 Kontent")
    async def content_panel(message: Message):
        if not is_moderator(message.from_user.id):
            return
        await message.answer("🎬 <b>Kontent boshqaruvi</b>", reply_markup=content_menu_kb())

    @dp.message(Command("addmovie"))
    @dp.message(F.text == "🎬 Film qo'shish")
    async def addmovie_cmd(message: Message, state: FSMContext):
        if not is_moderator(message.from_user.id):
            return
        await state.update_data(is_vip=False)
        await message.answer("Kino kodini yuboring (faqat raqam, masalan: 40):")
        await state.set_state(AddMovie.waiting_code)

    @dp.message(F.text == "🔒 VIP kino qo'shish")
    async def addvipmovie_cmd(message: Message, state: FSMContext):
        if not is_moderator(message.from_user.id) or not is_pro():
            return
        await state.update_data(is_vip=True)
        await message.answer(
            "🔒 <b>VIP kino qo'shish</b>\n\nBu kino faqat Premium (VIP) foydalanuvchilarga ko'rinadi.\n\n"
            "Kino kodini yuboring (faqat raqam, masalan: 40):"
        )
        await state.set_state(AddMovie.waiting_code)

    @dp.message(F.text == "📺 Serial qo'shish")
    async def addseries_cmd(message: Message, state: FSMContext):
        if not is_moderator(message.from_user.id):
            return
        await message.answer("Serial kodini yuboring (faqat raqam, masalan: 41):")
        await state.set_state(AddSeries.waiting_code)

    @dp.message(AddSeries.waiting_code)
    async def addseries_code(message: Message, state: FSMContext):
        code = message.text.strip()
        if not code.isdigit():
            await message.answer("❌ Kod faqat raqamlardan iborat bo'lishi kerak. Qaytadan yuboring:")
            return
        existing = info["movies"].get(code)
        if existing:
            name = existing.get("title") or existing.get("desc", "-")[:30]
            await message.answer(f"⚠️ Kod {code} allaqachon band: <b>{name}</b>. Davom etsangiz, u almashtiriladi.")
        await state.update_data(code=code)
        await message.answer("Serial nomini yozing (masalan: Umar ibn Xattob):")
        await state.set_state(AddSeries.waiting_title)

    @dp.message(AddSeries.waiting_title)
    async def addseries_title(message: Message, state: FSMContext):
        await state.update_data(title=message.text.strip())
        await message.answer("Tavsif yozing (sifati, davlati, janri, tili, yili va h.k.):")
        await state.set_state(AddSeries.waiting_desc)

    @dp.message(AddSeries.waiting_desc)
    async def addseries_desc(message: Message, state: FSMContext):
        await state.update_data(desc=message.text.strip(), episodes={})
        await message.answer("Jami nechta qism/serial bor? (faqat raqam, masalan: 10):")
        await state.set_state(AddSeries.waiting_count)

    @dp.message(AddSeries.waiting_count)
    async def addseries_count(message: Message, state: FSMContext):
        try:
            count = int(message.text.strip())
            if count <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting (masalan: 10).")
            return
        await state.update_data(expected_count=count)
        await message.answer(
            f"Jami <b>{count}</b> qism kutilmoqda.\n\n"
            "Endi 1-qism videosini yuboring. Har bir videoni ketma-ket yuboraverasiz "
            f"(avtomatik 1, 2, 3... deb raqamlanadi) — {count}-qism yuborilgach, bot avtomatik saqlaydi.\n"
            "Xohlasangiz, tugatish uchun /done ham yozishingiz mumkin."
        )
        await state.set_state(AddSeries.waiting_episode)

    async def finalize_series(message: Message, state: FSMContext, state_data: dict):
        episodes = state_data.get("episodes", {})
        code = state_data["code"]
        info["movies"][code] = {
            "type": "series",
            "title": state_data["title"],
            "desc": state_data["desc"],
            "episodes": episodes,
        }
        save_data()
        if info.get("new_content_notify"):
            await notify_new_content(message.bot, f"📺 Yangi serial qo'shildi: {state_data['title']} (Kod: {code})")
        await message.answer(f"✅ Serial saqlandi: <b>{state_data['title']}</b> ({len(episodes)} qism), Kod: {code}")
        await state.clear()

    @dp.message(AddSeries.waiting_episode, F.video)
    async def addseries_episode(message: Message, state: FSMContext):
        state_data = await state.get_data()
        episodes = state_data.get("episodes", {})
        next_num = len(episodes) + 1
        episodes[str(next_num)] = message.video.file_id
        await state.update_data(episodes=episodes)
        expected = state_data.get("expected_count")
        if expected and next_num >= expected:
            state_data["episodes"] = episodes
            await finalize_series(message, state, state_data)
            return
        await message.answer(f"✅ {next_num}/{expected or '?'}-qism saqlandi. Davom eting yoki /done deb tugating.")

    @dp.message(AddSeries.waiting_episode, Command("done"))
    async def addseries_done(message: Message, state: FSMContext):
        state_data = await state.get_data()
        if not state_data.get("episodes"):
            await message.answer("❌ Kamida bitta qism yuborishingiz kerak.")
            return
        await finalize_series(message, state, state_data)

    @dp.message(AddSeries.waiting_episode)
    async def addseries_wrong(message: Message):
        await message.answer("❌ Video yuboring yoki barcha qismlar tugagan bo'lsa /done deb yozing.")

    async def send_series_episode(send_func, series: dict, code: str, ep_num: int, uid: int = None):
        episodes = series["episodes"]
        sorted_eps = sorted(int(k) for k in episodes.keys())
        total = len(sorted_eps)
        file_id = episodes.get(str(ep_num))
        caption = (
            f"🎬 <b>{series['title']}</b>\n"
            f"🆔 Kodi: {code}\n"
            f"📁 Qism: {ep_num}/{total}\n\n"
            f"{series.get('desc', '')}"
        )
        buttons = []
        row = []
        for n in sorted_eps:
            label = f"• {n}-qism" if n == ep_num else f"{n}-qism"
            row.append(InlineKeyboardButton(text=label, callback_data=f"ep_{code}_{n}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        next_ep = ep_num + 1
        if next_ep in sorted_eps:
            buttons.append([InlineKeyboardButton(text="Keyingi ▶️", callback_data=f"ep_{code}_{next_ep}")])
        if uid is not None:
            subs = info["series_subscribers"].get(code, [])
            sub_label = "🔕 Obunani bekor qilish" if uid in subs else "🔔 Yangi qismga obuna bo'lish"
            buttons.append([InlineKeyboardButton(text=sub_label, callback_data=f"subep_{code}")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await send_func(file_id, caption=caption, reply_markup=kb)

    @dp.callback_query(F.data.startswith("subep_"))
    async def subscribe_episode_cb(callback: CallbackQuery):
        code = callback.data.split("_", 1)[1]
        uid = callback.from_user.id
        subs = info["series_subscribers"].setdefault(code, [])
        if uid in subs:
            subs.remove(uid)
            await callback.answer("🔕 Obuna bekor qilindi.")
        else:
            subs.append(uid)
            await callback.answer("🔔 Endi yangi qism chiqsa xabar beramiz!")
        save_data()

    @dp.callback_query(F.data.startswith("ep_"))
    async def episode_nav_cb(callback: CallbackQuery):
        _, code, num_str = callback.data.split("_")
        num = int(num_str)
        series = info["movies"].get(code)
        if not series or series.get("type") != "series":
            await callback.answer("Topilmadi.", show_alert=True)
            return
        await send_series_episode(callback.message.answer_video, series, code, num, uid=callback.from_user.id)
        await callback.answer()

    @dp.message(AddMovie.waiting_code)
    async def addmovie_code(message: Message, state: FSMContext):
        code = message.text.strip()
        if not code.isdigit():
            await message.answer("❌ Kod faqat raqamlardan iborat bo'lishi kerak. Qaytadan yuboring:")
            return
        existing = info["movies"].get(code)
        if existing:
            name = existing.get("title") or existing.get("desc", "-")[:30]
            await message.answer(f"⚠️ Kod {code} allaqachon band: <b>{name}</b>. Davom etsangiz, u almashtiriladi.")
        await state.update_data(code=code)
        await message.answer("Endi kino haqida qisqacha tavsif yozing (janr, yil, va h.k.):")
        await state.set_state(AddMovie.waiting_desc)

    @dp.message(AddMovie.waiting_desc)
    async def addmovie_desc(message: Message, state: FSMContext):
        await state.update_data(desc=message.text.strip())
        await message.answer("Endi filmni (videoni) yuboring:")
        await state.set_state(AddMovie.waiting_video)

    async def notify_new_content(bot, text):
        for uid in info["users"]:
            try:
                await bot.send_message(uid, text)
            except Exception:
                pass

    @dp.message(AddMovie.waiting_video, F.video)
    async def addmovie_video(message: Message, state: FSMContext):
        state_data = await state.get_data()
        code = state_data.get("code")
        desc = state_data.get("desc", "")
        is_vip = state_data.get("is_vip", False)
        info["movies"][code] = {"file_id": message.video.file_id, "desc": desc}
        if is_vip and code not in info["vip_codes"]:
            info["vip_codes"].append(code)
        save_data()
        if info.get("new_content_notify"):
            label = "🔒 Yangi VIP film" if is_vip else "🎬 Yangi film"
            await notify_new_content(message.bot, f"{label} qo'shildi: Kod {code}")
        vip_note = "\n🔒 Bu film VIP-maxsus (faqat Premium foydalanuvchilar ko'radi)." if is_vip else ""
        await message.answer(f"✅ Kod <b>{code}</b> bilan film saqlandi.{vip_note}")
        await state.clear()

    @dp.message(AddMovie.waiting_video)
    async def addmovie_wrong(message: Message):
        await message.answer("❌ Iltimos, video fayl yuboring (forward qilingan bo'lsa ham bo'ladi).")

    @dp.message(F.text == "📋 Filmlar ro'yxati")
    async def list_movies(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"]:
            await message.answer("Hozircha filmlar yo'q.")
            return
        lines = []
        for code, m in info["movies"].items():
            cat = info["categories"].get(m.get("category", ""), "")
            cat_note = f" [{cat}]" if cat else ""
            vip_mark = "🔒 VIP" if code in info["vip_codes"] else "🆓"
            if m.get("type") == "series":
                lines.append(f"• Kod {code} {vip_mark} 📺 [Serial] {m.get('title', '-')} ({len(m.get('episodes', {}))} qism){cat_note}")
            else:
                lines.append(f"• Kod {code} {vip_mark} 🎬 {m.get('desc', '-')[:40]}{cat_note}")
        await message.answer("📋 <b>Filmlar:</b>\n\n" + "\n".join(lines))

    @dp.message(F.text == "🔍 Kod bo'yicha qidirish")
    async def search_by_code_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Qidirmoqchi bo'lgan kodni kiriting:")
        await state.set_state(StarUserSearch.waiting_query)

    @dp.message(StarUserSearch.waiting_query)
    async def search_by_code_result(message: Message, state: FSMContext):
        code = message.text.strip()
        m = info["movies"].get(code)
        if not m:
            await message.answer("❌ Bunday kodli kontent topilmadi.")
        else:
            req_count = info["request_counts"].get(code, 0)
            if m.get("type") == "series":
                await message.answer(f"📺 <b>{m.get('title','-')}</b>\nKod: {code}\nQismlar: {len(m.get('episodes', {}))}\nSo'ralgan: {req_count} marta")
            else:
                await message.answer(f"🎬 Kod: {code}\n{m.get('desc','-')}\nSo'ralgan: {req_count} marta")
        await state.clear()

    @dp.message(F.text == "✏️ Tavsifni tahrirlash")
    async def edit_desc_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"]:
            await message.answer("Kontent yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=f"Kod {code}", callback_data=f"editdesc_{code}")] for code in info["movies"]]
        await message.answer("Tavsifini tahrirlamoqchi bo'lgan kontentni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("editdesc_"))
    async def edit_desc_pick(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        code = callback.data.split("_", 1)[1]
        await state.update_data(edit_code=code)
        await callback.message.answer("Yangi tavsifni kiriting:")
        await state.set_state(StarPackageEdit.waiting_price)
        await callback.answer()

    @dp.message(StarPackageEdit.waiting_price)
    async def edit_desc_save(message: Message, state: FSMContext):
        fsm_data = await state.get_data()
        code = fsm_data.get("edit_code")
        if code == "__help__":
            info["help_text"] = message.text.strip()
            save_data()
            await message.answer("✅ Yordam matni yangilandi.")
            await state.clear()
            return
        if code in info["movies"]:
            info["movies"][code]["desc"] = message.text.strip()
            save_data()
            await message.answer(f"✅ Kod {code} tavsifi yangilandi.")
        await state.clear()

    @dp.message(F.text == "🗑 Film o'chirish")
    async def del_movie_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"]:
            await message.answer("O'chirish uchun film yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=f"Kod {code}", callback_data=f"delmovie_{code}")] for code in info["movies"]]
        await message.answer("O'chirmoqchi bo'lgan filmni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("delmovie_"))
    async def del_movie_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        code = callback.data.split("_", 1)[1]
        removed = info["movies"].pop(code, None)
        info["request_counts"].pop(code, None)
        if code in info["featured"]:
            info["featured"].remove(code)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 Kod {code} o'chirildi.")
        await callback.answer()

    # ---------- Seriallarga qism qo'shish ----------
    @dp.message(F.text == "➕ Seriallarga qism qo'shish")
    async def append_episode_start(message: Message):
        if not is_moderator(message.from_user.id):
            return
        series_list = {c: m for c, m in info["movies"].items() if m.get("type") == "series"}
        if not series_list:
            await message.answer("Hozircha seriallar yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=f"{m['title']} (Kod {c})", callback_data=f"appendep_{c}")] for c, m in series_list.items()]
        await message.answer("Qaysi serialga yangi qism qo'shamiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("appendep_"))
    async def append_episode_pick(callback: CallbackQuery, state: FSMContext):
        code = callback.data.split("_", 1)[1]
        await state.update_data(append_code=code)
        await callback.message.answer("Yangi qism videosini yuboring:")
        await state.set_state(AppendEpisode.waiting_video)
        await callback.answer()

    @dp.message(AppendEpisode.waiting_video, F.video)
    async def append_episode_video(message: Message, state: FSMContext):
        fsm_data = await state.get_data()
        code = fsm_data.get("append_code")
        series = info["movies"].get(code)
        if not series:
            await message.answer("❌ Serial topilmadi.")
            await state.clear()
            return
        next_num = len(series["episodes"]) + 1
        series["episodes"][str(next_num)] = message.video.file_id
        save_data()
        await message.answer(f"✅ {next_num}-qism qo'shildi: <b>{series['title']}</b>")
        subs = info["series_subscribers"].get(code, [])
        for uid in subs:
            try:
                await message.bot.send_message(uid, f"🔔 <b>{series['title']}</b> — yangi {next_num}-qism chiqdi! Kod: {code}")
            except Exception:
                pass
        await state.clear()

    @dp.message(AppendEpisode.waiting_video)
    async def append_episode_wrong(message: Message):
        await message.answer("❌ Video yuboring.")

    # ---------- VIP kontent ----------
    @dp.message(F.text == "🔒 VIP qilib belgilash")
    async def vip_mark_start(message: Message):
        if not is_admin(info, message.from_user.id) or not is_pro():
            return
        if not info["movies"]:
            await message.answer("Kontent yo'q.")
            return
        buttons = []
        for code, m in info["movies"].items():
            mark = "🔒" if code in info["vip_codes"] else "🔓"
            name = m.get("title") or m.get("desc", "-")[:25]
            buttons.append([InlineKeyboardButton(text=f"{mark} Kod {code} — {name}", callback_data=f"vipmark_{code}")])
        await message.answer("Bosish orqali VIP holatini yoqing/o'chiring:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("vipmark_"))
    async def vip_mark_toggle(callback: CallbackQuery):
        code = callback.data.split("_", 1)[1]
        if code in info["vip_codes"]:
            info["vip_codes"].remove(code)
            status = "🔓 Ochiq (VIP emas)"
        else:
            info["vip_codes"].append(code)
            status = "🔒 VIP-maxsus"
        save_data()
        await callback.answer(f"Kod {code}: {status}", show_alert=True)

    # ---------- Rejalashtirilgan chiqarish ----------
    @dp.message(F.text == "🗓 Chiqish sanasini belgilash")
    async def schedule_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"]:
            await message.answer("Kontent yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=f"Kod {c}", callback_data=f"schedpick_{c}")] for c in info["movies"]]
        await message.answer("Qaysi kontent uchun chiqish sanasi belgilaymiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("schedpick_"))
    async def schedule_pick(callback: CallbackQuery, state: FSMContext):
        code = callback.data.split("_", 1)[1]
        await state.update_data(sched_code=code)
        await callback.message.answer("Chiqish sanasi va vaqtini kiriting (masalan: 25.12.2026 18:00):")
        await state.set_state(ScheduleRelease.waiting_datetime)
        await callback.answer()

    @dp.message(ScheduleRelease.waiting_datetime)
    async def schedule_save(message: Message, state: FSMContext):
        try:
            dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
        except ValueError:
            await message.answer("❌ Format noto'g'ri. Masalan: 25.12.2026 18:00 ko'rinishida yuboring.")
            return
        fsm_data = await state.get_data()
        code = fsm_data.get("sched_code")
        if code in info["movies"]:
            info["movies"][code]["release_at"] = dt.isoformat()
            save_data()
            await message.answer(f"✅ Kod {code} uchun chiqish sanasi: {dt.strftime('%d.%m.%Y %H:%M')}")
        await state.clear()

    # ---------- Reytinglar ----------
    @dp.callback_query(F.data.startswith("rate_"))
    async def rate_movie_cb(callback: CallbackQuery):
        _, code, stars_str = callback.data.split("_")
        stars = int(stars_str)
        uid = str(callback.from_user.id)
        rating = info["ratings"].setdefault(code, {"total": 0, "count": 0, "by_user": {}})
        prev = rating["by_user"].get(uid)
        if prev is not None:
            rating["total"] -= prev
            rating["count"] -= 1
        rating["by_user"][uid] = stars
        rating["total"] += stars
        rating["count"] += 1
        save_data()
        await callback.answer(f"✅ Bahoyingiz qabul qilindi: {'⭐' * stars}")

    @dp.message(F.text == "⭐ Reytinglar")
    async def ratings_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        rated = [(c, r) for c, r in info["ratings"].items() if r["count"] > 0]
        if not rated:
            await message.answer("Hali baholangan kontent yo'q.")
            return
        rated.sort(key=lambda x: x[1]["total"] / x[1]["count"], reverse=True)
        lines = []
        for code, r in rated[:10]:
            avg = r["total"] / r["count"]
            lines.append(f"Kod {code}: {avg:.1f} ⭐ ({r['count']} ta baho)")
        await message.answer("⭐ <b>Eng yuqori baholangan TOP-10:</b>\n\n" + "\n".join(lines))

    # ---------- Faol foydalanuvchilar ----------
    @dp.message(F.text == "🏆 Faol foydalanuvchilar")
    async def active_users_top(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["user_activity"]:
            await message.answer("Hali statistikaga yetarli ma'lumot yo'q.")
            return
        top = sorted(info["user_activity"].items(), key=lambda x: x[1], reverse=True)[:10]
        lines = [f"{i+1}. ID {uid} — {count} ta so'rov" for i, (uid, count) in enumerate(top)]
        await message.answer("🏆 <b>Eng faol foydalanuvchilar TOP-10:</b>\n\n" + "\n".join(lines))

    # ---------- Moderatorlar ----------
    @dp.message(F.text == "👮 Moderatorlar")
    async def moderators_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"👮 <b>Moderatorlar</b>\n\nModeratorlar faqat kontent qo'sha oladi, boshqa sozlamalarga kira olmaydi.\n\nJami: {len(info['moderators'])} ta",
            reply_markup=moderators_menu_kb(),
        )

    @dp.message(F.text == "➕ Moderator qo'shish")
    async def moderator_add_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Moderator qilmoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(ModeratorAdd.waiting_id)

    @dp.message(ModeratorAdd.waiting_id)
    async def moderator_add_save(message: Message, state: FSMContext):
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        if target not in info["moderators"]:
            info["moderators"].append(target)
            save_data()
        await message.answer(f"✅ {target} moderator qilib tayinlandi.")
        await state.clear()

    @dp.message(F.text == "📋 Moderatorlar ro'yxati")
    async def moderator_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["moderators"]:
            await message.answer("Moderatorlar yo'q.")
        else:
            await message.answer("👮 Moderatorlar:\n\n" + "\n".join(f"• <code>{m}</code>" for m in info["moderators"]))

    @dp.message(F.text == "➖ Moderatorni o'chirish")
    async def moderator_del_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["moderators"]:
            await message.answer("Moderatorlar yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=str(m), callback_data=f"moddel_{m}")] for m in info["moderators"]]
        await message.answer("O'chirmoqchi bo'lgan moderatorni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("moddel_"))
    async def moderator_del_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        target = int(callback.data.split("_", 1)[1])
        if target in info["moderators"]:
            info["moderators"].remove(target)
            save_data()
        await callback.message.answer(f"➖ {target} moderatorlikdan olib tashlandi.")
        await callback.answer()

    # ---------- Avtomatik hisobot ----------
    @dp.message(F.text == "📅 Avtomatik hisobot")
    async def auto_report_toggle(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        info["auto_report_enabled"] = not info.get("auto_report_enabled", False)
        save_data()
        if info["auto_report_enabled"]:
            await message.answer(
                f"✅ Avtomatik kunlik hisobot yoqildi. Har kuni soat {info.get('auto_report_hour', 9)}:00 dan keyin yuboriladi."
            )
        else:
            await message.answer("❌ Avtomatik hisobot o'chirildi.")

    # ---------- Kategoriyalar ----------
    @dp.message(F.text == "🏷 Kategoriyalar")
    async def categories_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("🏷 <b>Kategoriyalar boshqaruvi</b>", reply_markup=categories_menu_kb())

    @dp.message(F.text == "➕ Kategoriya qo'shish")
    async def cat_add_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Kategoriya nomini kiriting (masalan: Jangari, Komediya, Multfilm):")
        await state.set_state(StarPackageAdd.waiting_stars)

    @dp.message(StarPackageAdd.waiting_stars)
    async def cat_add_save(message: Message, state: FSMContext):
        name = message.text.strip()
        cid = uuid.uuid4().hex[:6]
        info["categories"][cid] = name
        save_data()
        await message.answer(f"✅ Kategoriya qo'shildi: {name}")
        await state.clear()

    @dp.message(F.text == "📋 Kategoriyalar ro'yxati")
    async def cat_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["categories"]:
            await message.answer("Kategoriyalar mavjud emas.")
        else:
            lines = [f"• {name}" for name in info["categories"].values()]
            await message.answer("🏷 Kategoriyalar:\n\n" + "\n".join(lines))

    @dp.message(F.text == "🔗 Filmga kategoriya biriktirish")
    async def cat_assign_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"] or not info["categories"]:
            await message.answer("Buning uchun kamida bitta kontent va bitta kategoriya bo'lishi kerak.")
            return
        buttons = [[InlineKeyboardButton(text=f"Kod {code}", callback_data=f"catassign_{code}")] for code in info["movies"]]
        await message.answer("Qaysi kontentga kategoriya biriktiramiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("catassign_"))
    async def cat_assign_pick_movie(callback: CallbackQuery, state: FSMContext):
        code = callback.data.split("_", 1)[1]
        await state.update_data(assign_code=code)
        buttons = [[InlineKeyboardButton(text=name, callback_data=f"catset_{cid}")] for cid, name in info["categories"].items()]
        await callback.message.answer("Kategoriyani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("catset_"))
    async def cat_assign_set(callback: CallbackQuery, state: FSMContext):
        cid = callback.data.split("_", 1)[1]
        fsm_data = await state.get_data()
        code = fsm_data.get("assign_code")
        if code in info["movies"]:
            info["movies"][code]["category"] = cid
            save_data()
            await callback.message.answer(f"✅ Kod {code} — {info['categories'].get(cid)} kategoriyasiga biriktirildi.")
        await state.clear()
        await callback.answer()

    @dp.message(F.text == "➖ Kategoriya o'chirish")
    async def cat_del_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["categories"]:
            await message.answer("Kategoriyalar mavjud emas.")
            return
        buttons = [[InlineKeyboardButton(text=name, callback_data=f"catdel_{cid}")] for cid, name in info["categories"].items()]
        await message.answer("O'chirmoqchi bo'lgan kategoriyani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("catdel_"))
    async def cat_del_cb(callback: CallbackQuery):
        cid = callback.data.split("_", 1)[1]
        removed = info["categories"].pop(cid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed}")
        await callback.answer()

    # ---------- Tavsiyalar ----------
    @dp.message(F.text == "⭐ Tavsiyalar")
    async def featured_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("⭐ <b>Tavsiyalar boshqaruvi</b>", reply_markup=featured_menu_kb())

    @dp.message(F.text == "➕ Tavsiyaga qo'shish")
    async def featured_add_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"]:
            await message.answer("Kontent yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=f"Kod {code}", callback_data=f"featadd_{code}")] for code in info["movies"] if code not in info["featured"]]
        if not buttons:
            await message.answer("Barcha kontent allaqachon tavsiyada.")
            return
        await message.answer("Tavsiyaga qo'shmoqchi bo'lgan kontentni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("featadd_"))
    async def featured_add_cb(callback: CallbackQuery):
        code = callback.data.split("_", 1)[1]
        if code not in info["featured"]:
            info["featured"].append(code)
            save_data()
        await callback.message.answer(f"✅ Kod {code} tavsiyalarga qo'shildi.")
        await callback.answer()

    @dp.message(F.text == "📋 Tavsiyalar ro'yxati")
    async def featured_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["featured"]:
            await message.answer("Tavsiyalar mavjud emas.")
        else:
            await message.answer("⭐ Tavsiyalar:\n\n" + "\n".join(f"• Kod {c}" for c in info["featured"]))

    @dp.message(F.text == "➖ Tavsiyadan olib tashlash")
    async def featured_del_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["featured"]:
            await message.answer("Tavsiyalar mavjud emas.")
            return
        buttons = [[InlineKeyboardButton(text=f"Kod {c}", callback_data=f"featdel_{c}")] for c in info["featured"]]
        await message.answer("Olib tashlamoqchi bo'lgan kodni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("featdel_"))
    async def featured_del_cb(callback: CallbackQuery):
        code = callback.data.split("_", 1)[1]
        if code in info["featured"]:
            info["featured"].remove(code)
            save_data()
        await callback.message.answer(f"➖ Kod {code} tavsiyalardan olib tashlandi.")
        await callback.answer()

    # ---------- TOP reyting ----------
    @dp.message(F.text == "📈 TOP reyting")
    async def top_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("📈 <b>TOP reyting</b>", reply_markup=top_menu_kb())

    @dp.message(F.text == "🔥 Eng ko'p so'ralganlar")
    async def top_requested(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["request_counts"]:
            await message.answer("Hali statistikaga yetarli ma'lumot yo'q.")
            return
        top = sorted(info["request_counts"].items(), key=lambda x: x[1], reverse=True)[:10]
        lines = [f"{i+1}. Kod {code} — {count} marta" for i, (code, count) in enumerate(top)]
        await message.answer("🔥 <b>Eng ko'p so'ralgan TOP-10:</b>\n\n" + "\n".join(lines))

    @dp.message(F.text == "📅 Bugungi faollik")
    async def today_activity(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        usage = info.get("daily_usage", {})
        count = len(usage.get("users", [])) if usage.get("date") == today else 0
        await message.answer(f"📅 Bugun ({today}) faol bo'lgan foydalanuvchilar: {count}")

    # ---------- Foydalanuvchilar ----------
    @dp.message(F.text == "👥 Foydalanuvchilar")
    async def users_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(f"👥 Jami foydalanuvchilar: {len(info['users'])}", reply_markup=users_menu_kb())

    @dp.message(F.text == "📋 Ro'yxat")
    async def users_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        users = info["users"][-30:]
        await message.answer(f"👥 Oxirgi {len(users)} (jami {len(info['users'])}):\n\n" + "\n".join(f"• <code>{u}</code>" for u in users))

    @dp.message(F.text == "🔍 Qidirish")
    async def user_search_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Foydalanuvchi ID raqamini kiriting:")
        await state.set_state(StarBlockUser.waiting_id)

    @dp.message(StarBlockUser.waiting_id)
    async def user_search_result_or_block(message: Message, state: FSMContext):
        fsm_data = await state.get_data()
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        action = fsm_data.get("block_action", "search")
        if action == "block":
            if target not in info["blocked_users"]:
                info["blocked_users"].append(target)
                save_data()
            await message.answer(f"🚫 {target} bloklandi.")
        else:
            found = target in info["users"]
            blocked = target in info["blocked_users"]
            await message.answer(
                f"🔍 ID: <code>{target}</code>\n"
                f"{'✅ Bot foydalanuvchisi' if found else '❌ Topilmadi'}\n"
                f"{'🚫 Bloklangan' if blocked else '✅ Bloklanmagan'}"
            )
        await state.clear()

    @dp.message(F.text == "🚫 Bloklash")
    async def block_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await state.update_data(block_action="block")
        await message.answer("Bloklamoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(StarBlockUser.waiting_id)

    @dp.message(F.text == "✅ Blokdan chiqarish")
    async def unblock_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Blokdan chiqarmoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(StarUnblockUser.waiting_id)

    @dp.message(StarUnblockUser.waiting_id)
    async def unblock_save(message: Message, state: FSMContext):
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        if target in info["blocked_users"]:
            info["blocked_users"].remove(target)
            save_data()
            await message.answer(f"✅ {target} blokdan chiqarildi.")
        else:
            await message.answer("Bu foydalanuvchi bloklanmagan.")
        await state.clear()

    # ---------- Xabar va reklama ----------
    @dp.message(F.text == "📢 Xabar va reklama")
    async def broadcast_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("📢 <b>Xabar va reklama</b>", reply_markup=broadcast_menu_kb())

    @dp.message(F.text == "📢 Ommaviy xabar yuborish")
    async def broadcast_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("E'lon matnini yuboring:")
        await state.set_state(PostFlow.waiting_text)

    @dp.message(PostFlow.waiting_text)
    async def broadcast_text(message: Message, state: FSMContext):
        await state.update_data(text=message.text)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="ultra_post_confirm")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="ultra_post_cancel")],
        ])
        await message.answer(f"{len(info['users'])} kishiga yuborilsinmi?\n\n{message.text}", reply_markup=buttons)
        await state.set_state(PostFlow.waiting_confirm)

    @dp.callback_query(F.data == "ultra_post_confirm", PostFlow.waiting_confirm)
    async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
        fsm_data = await state.get_data()
        text = fsm_data.get("text", "")
        count = 0
        for uid in info["users"]:
            try:
                await callback.bot.send_message(uid, text)
                count += 1
            except Exception:
                pass
        await callback.message.edit_text(f"✅ {count} ta foydalanuvchiga yuborildi.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "ultra_post_cancel", PostFlow.waiting_confirm)
    async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
        await callback.message.edit_text("❌ Bekor qilindi.")
        await state.clear()
        await callback.answer()

    @dp.message(F.text == "📣 Reklama joylash")
    async def ad_add_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Reklama matnini kiriting (har bir kino natijasi ostida ko'rsatiladi):")
        await state.set_state(StarOrderCustom.waiting_amount)

    @dp.message(StarOrderCustom.waiting_amount)
    async def ad_add_save(message: Message, state: FSMContext):
        aid = uuid.uuid4().hex[:6]
        info["ads"][aid] = {"text": message.text.strip(), "active": True}
        save_data()
        await message.answer("✅ Reklama qo'shildi va faollashtirildi.")
        await state.clear()

    @dp.message(F.text == "📋 Reklamalar ro'yxati")
    async def ad_list(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["ads"]:
            await message.answer("Reklamalar mavjud emas.")
        else:
            lines = [f"• {'🟢' if a['active'] else '⚪️'} {a['text'][:40]}" for a in info["ads"].values()]
            await message.answer("📣 Reklamalar:\n\n" + "\n".join(lines))

    @dp.message(F.text == "➖ Reklamani o'chirish")
    async def ad_del_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["ads"]:
            await message.answer("Reklamalar mavjud emas.")
            return
        buttons = [[InlineKeyboardButton(text=a["text"][:30], callback_data=f"addel_{aid}")] for aid, a in info["ads"].items()]
        await message.answer("O'chirmoqchi bo'lgan reklamani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("addel_"))
    async def ad_del_cb(callback: CallbackQuery):
        aid = callback.data.split("_", 1)[1]
        info["ads"].pop(aid, None)
        save_data()
        await callback.message.answer("🗑 Reklama o'chirildi.")
        await callback.answer()

    # ---------- Referal ----------
    @dp.message(F.text == "🎁 Referal")
    async def referral_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"🎁 <b>Referal tizimi</b>\n\nHozirgi bonus: {info.get('referral_bonus_days', 0)} kunlik Premium",
            reply_markup=referral_menu_kb(),
        )

    @dp.message(F.text == "🎯 Bonusni sozlash")
    async def referral_bonus_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Har bir taklif uchun necha kunlik Premium bonus berilsin? (0 — o'chirish):")
        await state.set_state(StarSettings.waiting_min)

    @dp.message(StarSettings.waiting_min)
    async def referral_bonus_save(message: Message, state: FSMContext):
        try:
            days = int(message.text.strip())
            if days < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ 0 yoki musbat butun raqam kiriting.")
            return
        info["referral_bonus_days"] = days
        save_data()
        await message.answer(f"✅ Referal bonusi: {days} kun.")
        await state.clear()

    @dp.message(F.text == "📊 Referal statistikasi")
    async def referral_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        total_refs = sum(len(v) for v in info["referrals"].values())
        top = sorted(info["referrals"].items(), key=lambda x: len(x[1]), reverse=True)[:10]
        lines = [f"{i+1}. ID {uid} — {len(refs)} ta taklif" for i, (uid, refs) in enumerate(top)]
        text = f"📊 Jami takliflar: {total_refs}\n\n" + ("\n".join(lines) if lines else "Hali takliflar yo'q.")
        await message.answer(text)

    # ---------- Sozlamalar ----------
    @dp.message(F.text == "⚙️ Sozlamalar")
    async def settings_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("⚙️ <b>Bot sozlamalari</b>", reply_markup=settings_menu_kb())

    @dp.message(F.text == "✏️ Salomlashuv matni")
    async def welcome_edit_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(f"Joriy matn:\n\n{info.get('welcome_text','-')}\n\nYangi matnni kiriting:")
        await state.set_state(WelcomeFlow.waiting_text)

    @dp.message(WelcomeFlow.waiting_text)
    async def welcome_edit_save(message: Message, state: FSMContext):
        info["welcome_text"] = message.text.strip()
        save_data()
        await message.answer("✅ Salomlashuv matni yangilandi.")
        await state.clear()

    @dp.message(F.text == "📄 Yordam matni")
    async def help_edit_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(f"Joriy matn:\n\n{info.get('help_text','-')}\n\nYangi matnni kiriting:")
        await state.set_state(StarPackageEdit.waiting_price)
        await state.update_data(edit_code="__help__")

    @dp.message(F.text == "🔔 Yangi kontent bildirishnomasi")
    async def toggle_notify(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        info["new_content_notify"] = not info.get("new_content_notify", False)
        save_data()
        status = "✅ Yoqildi" if info["new_content_notify"] else "❌ O'chirildi"
        await message.answer(f"🔔 Yangi kontent bildirishnomasi: {status}")

    @dp.message(F.text == "🛠 Texnik tanaffus")
    async def toggle_maintenance(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        info["maintenance_mode"] = not info.get("maintenance_mode", False)
        save_data()
        status = "✅ Yoqildi (mijozlar botdan foydalana olmaydi)" if info["maintenance_mode"] else "❌ O'chirildi"
        await message.answer(f"🛠 Texnik tanaffus: {status}")

    # ---------- Mijoz: kino kodi ----------
    @dp.message(F.text)
    async def get_movie(message: Message):
        if not await check_active(message, info, admin_id):
            return
        uid = message.from_user.id
        if is_blocked(uid):
            await message.answer("🚫 Siz ushbu botdan foydalanish huquqidan mahrum qilingansiz.")
            return
        if info.get("maintenance_mode") and not is_admin(info, uid):
            await message.answer("🛠 Bot hozircha texnik tanaffusda. Birozdan so'ng qayta urinib ko'ring.")
            return
        if not await require_subscription(message, info, admin_id):
            return
        code = message.text.strip()
        entry = info["movies"].get(code)
        if not entry:
            await message.answer("❌ Bunday kodli film topilmadi.")
            return

        if is_pro() and code in info["vip_codes"] and not is_premium_user(uid):
            vip_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Premium sotib olish", callback_data="buy_premium")]])
            await message.answer(
                "🔒 Bu kontent faqat <b>VIP (Premium)</b> foydalanuvchilar uchun.",
                reply_markup=vip_kb,
            )
            return

        release_at = entry.get("release_at")
        if release_at and datetime.fromisoformat(release_at) > datetime.now() and not is_admin(info, uid):
            dt = datetime.fromisoformat(release_at)
            await message.answer(f"🗓 Bu kontent hali chiqmagan. Chiqish sanasi: {dt.strftime('%d.%m.%Y %H:%M')}")
            return

        info["stats"]["requests"] += 1
        info["request_counts"][code] = info["request_counts"].get(code, 0) + 1
        info["user_activity"][str(uid)] = info["user_activity"].get(str(uid), 0) + 1
        save_data()

        if entry.get("type") == "series":
            await send_series_episode(message.answer_video, entry, code, 1, uid=uid)
        else:
            caption = f"🎬 Kod: {code}"
            if entry.get("desc"):
                caption += f"\n\n{entry['desc']}"
            rate_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=str(n), callback_data=f"rate_{code}_{n}") for n in range(1, 6)
            ]])
            await message.answer_video(entry["file_id"], caption=caption, reply_markup=rate_kb)
        active_ads = [a for a in info["ads"].values() if a.get("active")]
        if active_ads:
            import random
            ad = random.choice(active_ads)
            await message.answer(f"📣 {ad['text']}")



SETUP_FUNCTIONS = {
    "kino": setup_kino_bot,
    "kino_pro": setup_kino_bot,
    "shop": setup_shop_bot,
    "ai": setup_ai_bot,
    "money": setup_money_bot,
    "translate": setup_translate_bot,
    "taxi": setup_taxi_bot,
    "stars": setup_stars_bot,
}


def get_global_button_rows():
    rows = [[KeyboardButton(text="◀️ Orqaga")]]
    rows += [[KeyboardButton(text=b["label"])] for b in data.get("global_buttons", [])]
    return rows


def is_global_button_text(message: Message) -> bool:
    if not message.text:
        return False
    return any(message.text == b["label"] for b in data.get("global_buttons", []))


def setup_global_buttons_handler(dp: Dispatcher, start_func=None):
    @dp.message(F.text == "◀️ Orqaga")
    async def back_button_handler(message: Message, state: FSMContext):
        await state.clear()
        if start_func:
            await start_func(message, state)
        else:
            await message.answer("🏠 Bosh menyuga qaytish uchun /start bosing.")

    @dp.message(is_global_button_text)
    async def global_button_handler(message: Message):
        for b in data.get("global_buttons", []):
            if b["label"] == message.text:
                await message.answer(b["response"])
                return


async def start_child_bot(token: str, bot_type: str):
    if token in running_bots:
        return
    if bot_type not in SETUP_FUNCTIONS:
        logging.error(
            f"'{bot_type}' turidagi bot ishga tushirilmadi (token: ...{token[-6:]}) — "
            "bu bot turi endi platformada mavjud emas."
        )
        return
    child_bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    child_dp = Dispatcher(storage=MemoryStorage())
    SETUP_FUNCTIONS[bot_type](child_dp, token)
    task = asyncio.create_task(child_dp.start_polling(child_bot))
    running_bots[token] = task


async def trial_warning_loop():
    """Har 6 soatda barcha botlarni tekshirib, sinov/to'lov muddati tugashiga 1 kun qolganlarga ogohlantirish yuboradi."""
    while True:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            for token, info in data["bots"].items():
                if info.get("paid_until"):
                    expiry = datetime.fromisoformat(info["paid_until"])
                    kind = "to'lov"
                else:
                    expiry = datetime.fromisoformat(info["created_at"]) + timedelta(days=TRIAL_DAYS)
                    kind = "sinov"

                days_left = (expiry - datetime.now()).total_seconds() / 86400
                if 0 <= days_left <= 1 and info.get("last_warned_date") != today:
                    amount = next_payment_amount(info)
                    try:
                        await main_bot.send_message(
                            info["admin_id"],
                            f"⏳ <b>Ogohlantirish!</b>\n\n"
                            f"{BOT_TYPES.get(info['type'])} (<b>{info['name']}</b>) uchun {kind} muddati "
                            f"taxminan 1 kundan keyin tugaydi.\n\n"
                            f"Davom ettirish uchun to'lov: <b>{amount:,} so'm</b>.\n"
                            "To'lovni amalga oshirish uchun administrator bilan bog'laning.",
                            reply_markup=contact_admin_kb(),
                        )
                    except Exception as e:
                        logging.error(f"Ogohlantirish yuborishda xato ({token}): {e}")
                    info["last_warned_date"] = today
                    save_data()

                if info.get("type") == "kino" and info.get("auto_report_enabled"):
                    now = datetime.now()
                    if now.hour >= info.get("auto_report_hour", 9) and info.get("last_report_date") != today:
                        try:
                            await main_bot.send_message(info["admin_id"], build_kino_report(info))
                        except Exception as e:
                            logging.error(f"Avtomatik hisobot yuborishda xato ({token}): {e}")
                        info["last_report_date"] = today
                        save_data()
        except Exception as e:
            logging.error(f"trial_warning_loop xatosi: {e}")

        await asyncio.sleep(6 * 60 * 60)  # 6 soat


def build_kino_report(info: dict) -> str:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    daily_usage = info.get("daily_usage", {})
    today_count = len(daily_usage.get("users", [])) if daily_usage.get("date") == today else 0
    active_vip = 0
    for u in info.get("premium_users", {}).values():
        try:
            if datetime.fromisoformat(u["until"]) > now:
                active_vip += 1
        except Exception:
            pass
    return (
        f"📅 <b>Kunlik hisobot — {info['name']}</b>\n\n"
        f"👥 Jami foydalanuvchilar: {len(info.get('users', []))}\n"
        f"📊 Bugungi faol foydalanuvchilar: {today_count}\n"
        f"🔍 Jami so'rovlar: {info.get('stats', {}).get('requests', 0)}\n"
        f"🎞 Kontent soni: {len(info.get('movies', {}))}\n"
        f"💎 Faol VIP: {active_vip}"
    )


MINIAPP_HTML = """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Bot Creator</title>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #ffffff);
    --bg2: var(--tg-theme-secondary-bg-color, #f2f2f7);
    --text: var(--tg-theme-text-color, #1c1c1e);
    --hint: var(--tg-theme-hint-color, #8e8e93);
    --accent: var(--tg-theme-button-color, #2481cc);
    --accent-text: var(--tg-theme-button-text-color, #ffffff);
    --line: color-mix(in srgb, var(--hint) 22%, transparent);
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .mono {
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  }
  .wrap {
    max-width: 480px;
    margin: 0 auto;
    padding: 20px 16px 32px;
  }
  .hero {
    text-align: center;
    padding: 12px 0 22px;
    opacity: 0;
    animation: rise .5s ease forwards;
  }
  .hero-badge {
    width: 64px; height: 64px;
    margin: 0 auto 14px;
    border-radius: 18px;
    background: linear-gradient(145deg, var(--accent), color-mix(in srgb, var(--accent) 60%, #7b2ff7));
    display: flex; align-items: center; justify-content: center;
    font-size: 28px;
    box-shadow: 0 8px 20px -8px color-mix(in srgb, var(--accent) 70%, transparent);
  }
  .hero h1 {
    font-size: 21px;
    font-weight: 700;
    margin: 0 0 4px;
    letter-spacing: -0.01em;
  }
  .hero p {
    font-size: 13.5px;
    color: var(--hint);
    margin: 0;
  }
  .balance-card {
    background: linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 55%, #7b2ff7));
    color: var(--accent-text);
    border-radius: 18px;
    padding: 18px 20px;
    margin-bottom: 22px;
    opacity: 0;
    animation: rise .5s ease .08s forwards;
  }
  .balance-card .label {
    font-size: 12.5px;
    opacity: .85;
    text-transform: uppercase;
    letter-spacing: .04em;
    margin-bottom: 6px;
  }
  .balance-card .amount {
    font-size: 28px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
  }
  .section-label {
    font-size: 12.5px;
    font-weight: 600;
    color: var(--hint);
    text-transform: uppercase;
    letter-spacing: .05em;
    margin: 0 4px 8px;
  }
  .bot-list {
    background: var(--bg2);
    border-radius: 16px;
    overflow: hidden;
    opacity: 0;
    animation: rise .5s ease .16s forwards;
  }
  .bot-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 13px 14px;
    border-bottom: 1px solid var(--line);
    text-decoration: none;
    color: inherit;
  }
  .bot-row:last-child { border-bottom: none; }
  .avatar {
    flex-shrink: 0;
    width: 42px; height: 42px;
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
    color: #fff;
    font-weight: 600;
  }
  .bot-meta { flex: 1; min-width: 0; }
  .bot-name {
    font-size: 15px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .bot-sub {
    font-size: 12.5px;
    color: var(--hint);
    margin-top: 1px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .pill {
    flex-shrink: 0;
    font-size: 11px;
    font-weight: 600;
    padding: 4px 9px;
    border-radius: 999px;
    white-space: nowrap;
  }
  .pill.on { background: color-mix(in srgb, #34c759 18%, transparent); color: #248a3d; }
  .pill.off { background: color-mix(in srgb, #ff3b30 16%, transparent); color: #d70015; }
  .empty {
    text-align: center;
    padding: 46px 20px;
    color: var(--hint);
    font-size: 14px;
    line-height: 1.5;
  }
  .empty .emoji { font-size: 34px; margin-bottom: 10px; display: block; }
  .state-msg {
    text-align: center;
    padding: 60px 20px;
    color: var(--hint);
    font-size: 14px;
  }
  @keyframes rise {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @media (prefers-reduced-motion: reduce) {
    .hero, .balance-card, .bot-list { animation: none !important; opacity: 1 !important; }
  }
</style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div class="hero-badge">🤖</div>
      <h1>Bot Creator</h1>
      <p>Botlaringiz va balansingiz — bir joyda</p>
    </div>

    <div id="content">
      <div class="state-msg">Yuklanmoqda…</div>
    </div>
  </div>

<script>
  window.addEventListener("error", function (e) {
    const content = document.getElementById("content");
    if (content) {
      content.innerHTML = '<div class="state-msg">Sahifada xatolik: ' + escapeHtmlSafe(String(e.message || e)) + '</div>';
    }
  });

  function escapeHtmlSafe(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function loadTelegramSdk(timeoutMs) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => { if (!done) { done = true; resolve(); } };
      const timer = setTimeout(finish, timeoutMs);
      const el = document.createElement("script");
      el.src = "https://telegram.org/js/telegram-web-app.js";
      el.onload = () => { clearTimeout(timer); finish(); };
      el.onerror = () => { clearTimeout(timer); finish(); };
      document.head.appendChild(el);
    });
  }

  const TYPE_COLORS = {
    "kino": "#7b5cff",
  };

  function fmt(n) {
    return Number(n || 0).toLocaleString("ru-RU").replace(/,/g, " ");
  }

  function initials(name) {
    return (name || "?").trim().slice(0, 1).toUpperCase();
  }

  function fetchWithTimeout(url, ms) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ms);
    return fetch(url, { signal: controller.signal }).finally(() => clearTimeout(timer));
  }

  async function load() {
    const content = document.getElementById("content");

    await loadTelegramSdk(4000);
    const tg = window.Telegram?.WebApp;
    if (tg) { tg.ready(); tg.expand(); }

    if (!tg) {
      content.innerHTML = '<div class="state-msg">Telegram SDK yuklanmadi. Internet aloqasini tekshirib, sahifani qayta oching.</div>';
      return;
    }

    const initData = tg?.initData || "";

    if (!initData) {
      content.innerHTML = '<div class="state-msg">Bu sahifa faqat Telegram ichida ishlaydi.</div>';
      return;
    }

    try {
      const res = await fetchWithTimeout("/api/mybots?" + new URLSearchParams({ initData }), 8000);
      if (!res.ok) {
        const errBody = await res.text().catch(() => "");
        throw new Error("Server javobi: " + res.status + " " + errBody.slice(0, 120));
      }
      const data = await res.json();

      let html = "";
      html += '<div class="balance-card"><div class="label">Balans</div><div class="amount">' + fmt(data.balance) + " so'm</div></div>";
      html += '<div class="section-label">Botlarim (' + data.bots.length + ')</div>';

      if (data.bots.length === 0) {
        html += '<div class="bot-list"><div class="empty"><span class="emoji">📭</span>Hali botingiz yo\'q.<br>Bot yaratish uchun bosh menyudan "🤖 Bot yaratish" tugmasini bosing.</div></div>';
      } else {
        html += '<div class="bot-list">';
        for (const b of data.bots) {
          const color = TYPE_COLORS[b.type_key] || "#8e8e93";
          const pillClass = b.active ? "on" : "off";
          const pillText = b.active ? "Faol" : "To'xtagan";
          html += '<div class="bot-row">' +
            '<div class="avatar" style="background:' + color + '">' + initials(b.name) + '</div>' +
            '<div class="bot-meta">' +
              '<div class="bot-name">' + escapeHtml(b.name) + '</div>' +
              '<div class="bot-sub mono">' + escapeHtml(b.type) + ' · ' + escapeHtml(b.tariff) + '</div>' +
            '</div>' +
            '<div class="pill ' + pillClass + '">' + pillText + '</div>' +
          '</div>';
        }
        html += '</div>';
      }

      content.innerHTML = html;
    } catch (e) {
      const reason = e && e.name === "AbortError" ? "Server 8 soniyada javob bermadi (timeout)." : (e.message || String(e));
      content.innerHTML = '<div class="state-msg">Ma\'lumotlarni yuklab bo\'lmadi.<br><span style="font-size:12px">' + escapeHtmlSafe(reason) + '</span></div>';
    }
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  load();
</script>
</body>
</html>
"""


# ---------- Telegram Mini App (BotFather'dagi kabi "Botlarim" veb-sahifasi) ----------
def validate_webapp_init_data(init_data: str, bot_token: str):
    """Telegram WebApp initData imzosini tekshiradi. To'g'ri bo'lsa, parslangan dict qaytaradi."""
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None
        return parsed
    except Exception:
        return None


async def miniapp_page(request):
    return web.Response(
        text=MINIAPP_HTML,
        content_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
    )


async def api_mybots(request):
    init_data = request.query.get("initData", "")
    parsed = validate_webapp_init_data(init_data, MAIN_BOT_TOKEN)
    if not parsed:
        return web.json_response({"error": "invalid_init_data"}, status=401, headers={"Cache-Control": "no-store"})
    try:
        user = json.loads(parsed.get("user", "{}"))
        uid = user.get("id")
    except Exception:
        uid = None
    if not uid:
        return web.json_response({"error": "no_user"}, status=400, headers={"Cache-Control": "no-store"})

    bots_list = []
    for token, info in data["bots"].items():
        if uid in info.get("admin_ids", [info["admin_id"]]):
            tariff = get_bot_tariff(info)
            bots_list.append({
                "name": info["name"],
                "type": BOT_TYPES.get(info["type"], info["type"]),
                "type_key": info["type"],
                "active": is_active(info),
                "tariff": tariff["name"],
            })
    balance = data["user_balances"].get(str(uid), 0)
    return web.json_response({"bots": bots_list, "balance": balance}, headers={"Cache-Control": "no-store"})


async def start_web_server():
    app = web.Application()
    app.router.add_get("/miniapp", miniapp_page)
    app.router.add_get("/api/mybots", api_mybots)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Mini App veb-server {port}-portda ishga tushdi")


def get_miniapp_url() -> str:
    explicit = os.getenv("MINIAPP_URL")
    if explicit:
        return explicit.rstrip("/") + "/miniapp"
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    if domain:
        return f"https://{domain}/miniapp"
    return ""


async def main():
    # Eski "kino_ultra" turidagi botlar endi oddiy "kino" botga birlashtirildi
    migrated = 0
    for info in data["bots"].values():
        if info.get("type") == "kino_ultra":
            info["type"] = "kino"
            migrated += 1
    if migrated:
        save_data()
        logging.info(f"{migrated} ta bot 'kino_ultra' turidan 'kino' turiga o'tkazildi.")

    asyncio.create_task(start_web_server())

    miniapp_url = get_miniapp_url()
    if miniapp_url:
        try:
            await main_bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Botlarim", web_app=WebAppInfo(url=miniapp_url))
            )
            logging.info(f"Mini App menyu tugmasi sozlandi: {miniapp_url}")
        except Exception as e:
            logging.error(f"Mini App menyu tugmasini sozlashda xato: {e}")
    else:
        logging.warning(
            "MINIAPP_URL yoki RAILWAY_PUBLIC_DOMAIN topilmadi — Mini App menyu tugmasi sozlanmadi. "
            "Railway'da domen generatsiya qiling yoki MINIAPP_URL o'zgaruvchisini qo'ying."
        )

    for token, info in data["bots"].items():
        info.setdefault("stats", {})
        try:
            await start_child_bot(token, info["type"])
        except Exception as e:
            logging.error(f"Bot ishga tushmadi (token: ...{token[-6:]}, tur: {info.get('type')}): {e}")
    for clone in data.get("platform_clones", []):
        try:
            await start_platform_clone(clone["token"], clone.get("username"))
        except Exception as e:
            logging.error(f"Klon ishga tushmadi: {e}")
    asyncio.create_task(trial_warning_loop())
    await main_dp.start_polling(main_bot)


if __name__ == "__main__":
    asyncio.run(main())
