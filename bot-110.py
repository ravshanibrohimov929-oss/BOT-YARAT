import os
import json
import asyncio
import logging
import uuid
import hmac
import hashlib
import re
import base64
from urllib.parse import parse_qsl, quote
import httpx
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command
from io import BytesIO
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, MenuButtonWebApp, MenuButtonCommands, BotCommand, WebAppInfo, LabeledPrice, PreCheckoutQuery, FSInputFile, BufferedInputFile
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.dispatcher.event.bases import SkipHandler
from html import escape as html_escape
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

MAIN_BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # bosh administrator (siz)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")  # masalan: ravshan_uzz (@ belgisiz)

# Click.uz Merchant (Shop API) — avtomatik to'lov uchun. merchant.click.uz'da ro'yxatdan
# o'tgach shu 3 ta qiymatni Railway Variables'ga qo'shing: CLICK_SERVICE_ID, CLICK_MERCHANT_ID, CLICK_SECRET_KEY
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "")
CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "")
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY", "")


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


# ---------- ✨ Animatsiyali (custom) emojilar — Bot Creator inline tugmalari uchun ----------
def _anim_norm(s: str) -> str:
    """Emoji variatsiya belgisini (U+FE0F) olib tashlaydi — solishtirish uchun."""
    return (s or "").replace("\ufe0f", "")


def _anim_strip_lead(text: str, key: str):
    """text `key` emoji bilan boshlansa, emojisiz qolgan matnni qaytaradi, aks holda None."""
    nk = _anim_norm(key)
    if not nk:
        return None
    i = j = 0
    while j < len(nk) and i < len(text):
        if text[i] == "\ufe0f":
            i += 1
            continue
        if text[i] == nk[j]:
            i += 1
            j += 1
        else:
            return None
    if j < len(nk):
        return None
    while i < len(text) and text[i] == "\ufe0f":
        i += 1
    return text[i:].strip()


def anim_active() -> bool:
    return bool(data.get("anim_enabled")) and bool(data.get("anim_emojis"))


def anim_transform_markup(markup):
    """Inline tugma matnining boshidagi oddiy emojini animatsiyali (custom) emoji ikonkasiga almashtiradi.
    O'zgarish bo'lmasa None qaytaradi."""
    emojis = data.get("anim_emojis") or {}
    keys = sorted(emojis, key=len, reverse=True)
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            nb = b
            if not getattr(b, "icon_custom_emoji_id", None):
                for k in keys:
                    rest = _anim_strip_lead(b.text or "", k)
                    if rest is not None:
                        if rest:
                            nb = b.model_copy(update={"text": rest, "icon_custom_emoji_id": emojis[k]})
                            changed = True
                        break
            new_row.append(nb)
        rows.append(new_row)
    return markup.model_copy(update={"inline_keyboard": rows}) if changed else None


async def anim_fetch_flags(bot, ids: list) -> dict:
    """{custom_emoji_id: animatsiyali_mi} — Telegram'dan so'raydi. Aniqlab bo'lmaganlar lug'atda bo'lmaydi."""
    flags = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 200):
        try:
            res = await bot.get_custom_emoji_stickers(custom_emoji_ids=ids[i:i + 200])
        except Exception as e:
            logging.info(f"Emoji turini aniqlashda xato: {e}")
            continue
        for st in res:
            if st.custom_emoji_id:
                flags[st.custom_emoji_id] = bool(getattr(st, "is_animated", False) or getattr(st, "is_video", False))
    return flags


_ANIM_SKIP_TAGS = ("tg-emoji", "code", "pre", "a")


def anim_transform_text(text: str):
    """HTML matndagi saqlangan oddiy emojilarni <tg-emoji> (animatsiyali) bilan almashtiradi.
    <code>, <pre>, <a> va mavjud <tg-emoji> ichiga tegmaydi. O'zgarish bo'lmasa None."""
    emojis = data.get("anim_emojis") or {}
    if not text or not emojis:
        return None
    keys = sorted(emojis, key=len, reverse=True)
    pat = re.compile("(" + "|".join(re.escape(k) for k in keys) + ")\ufe0f?")
    changed = 0
    skip = 0
    out = []

    def sub(m):
        nonlocal changed
        if changed >= 90:          # Telegram chegarasi — xabarda 100 tagacha custom emoji
            return m.group(0)
        changed += 1
        return f'<tg-emoji emoji-id="{emojis[m.group(1)]}">{m.group(0)}</tg-emoji>'

    for part in re.split(r"(<[^>]*>)", text):
        if len(part) > 2 and part.startswith("<") and part.endswith(">"):
            m = re.match(r"</?\s*([A-Za-z0-9\-]+)", part)
            if m and m.group(1).lower() in _ANIM_SKIP_TAGS:
                if part.startswith("</"):
                    skip = max(0, skip - 1)
                elif not part.endswith("/>"):
                    skip += 1
            out.append(part)
        elif skip or not part:
            out.append(part)
        else:
            out.append(pat.sub(sub, part))
    return "".join(out) if changed else None


def _btn_key(text: str) -> str:
    return _anim_norm(text or "").strip()


def btn_hash(text: str) -> str:
    return hashlib.md5(_btn_key(text).encode("utf-8")).hexdigest()[:10]


_BTN_NUM_RE = re.compile(r"\d+(?:[ ,.\u00a0]\d+)*")


def _btn_norm(text: str) -> str:
    """Narx/son o'zgarsa ham rang saqlansin: raqamlar '#' bilan almashtiriladi."""
    return _BTN_NUM_RE.sub("#", _btn_key(text))


_btn_last_save = 0.0


def _btn_seen_add(key: str, seen: list):
    """Tugmani 'ko'rilgan' ro'yxatiga qo'shadi (har 2 daqiqada ko'pi bilan bir marta bazaga yoziladi)."""
    global _btn_last_save
    if not key or key in seen:
        return
    seen.insert(0, key)
    now = datetime.now().timestamp()
    if now - _btn_last_save > 120:
        _btn_last_save = now
        try:
            save_data()
        except Exception as e:
            logging.info(f"btn_seen saqlanmadi: {e}")


def _btn_style_for(key: str, colors: dict):
    if not key or not colors:
        return None
    style = colors.get(key)
    if style:
        return style
    nk = _btn_norm(key)
    for ck, cv in colors.items():
        if _btn_norm(ck) == nk:
            return cv
    return None


def btn_record_and_style(markup):
    """Bot yuborgan inline tugmalarni ro'yxatga oladi va sozlangan ranglarni (style) qo'llaydi.
    O'zgarish bo'lmasa None qaytaradi."""
    colors = data.get("btn_colors") or {}
    seen = data.setdefault("btn_seen", [])
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            nb = b
            key = _btn_key(b.text)
            cbd = str(getattr(b, "callback_data", "") or "")
            if key and not cbd.startswith(("clr_", "txt_")):
                _btn_seen_add(key, seen)
            style = _btn_style_for(key, colors)
            if style and not getattr(b, "style", None):
                nb = b.model_copy(update={"style": style})
                changed = True
            new_row.append(nb)
        rows.append(new_row)
    del seen[300:]
    return markup.model_copy(update={"inline_keyboard": rows}) if changed else None


def btn_record_and_style_reply(markup):
    """Pastdagi doimiy (reply) klaviatura tugmalarini ham ro'yxatga oladi va ranglaydi."""
    colors = data.get("btn_colors") or {}
    seen = data.setdefault("btn_seen", [])
    changed = False
    rows = []
    for row in markup.keyboard:
        new_row = []
        for b in row:
            nb = b
            text = getattr(b, "text", None)
            if text:
                key = _btn_key(text)
                _btn_seen_add(key, seen)
                style = _btn_style_for(key, colors)
                if style and not getattr(b, "style", None):
                    nb = b.model_copy(update={"style": style})
                    changed = True
            new_row.append(nb)
        rows.append(new_row)
    del seen[300:]
    return markup.model_copy(update={"keyboard": rows}) if changed else None


_anim_off_until = 0.0     # animatsiyali emoji rad etilsa, shu vaqtgacha (timestamp) faqat ranglar ishlatiladi


async def anim_request_middleware(make_request, bot, method):
    """Bot Creator xabarlariga tugma ranglari (🎨) va animatsiyali emoji (✨) qo'shadi.
    Telegram rad etsa, bosqichma-bosqich soddalashtiradi: (ranglar + emoji) → (faqat ranglar) → asl xabar.
    Shunda animatsiyali emoji ishlamasa (masalan, bot egasida Premium yo'q), ranglar baribir ishlaydi."""
    global _anim_off_until
    full = style_only = None
    try:
        mk = getattr(method, "reply_markup", None)
        if isinstance(mk, InlineKeyboardMarkup):
            styled_mk = btn_record_and_style(mk)
        elif isinstance(mk, ReplyKeyboardMarkup):
            styled_mk = btn_record_and_style_reply(mk)
        else:
            styled_mk = None
        base_mk = styled_mk if styled_mk is not None else mk
        style_updates = {"reply_markup": styled_mk} if styled_mk is not None else {}
        if style_updates:
            style_only = method.model_copy(update=style_updates)
        anim_updates = {}
        if anim_active() and datetime.now().timestamp() >= _anim_off_until:
            if isinstance(base_mk, InlineKeyboardMarkup):
                am = anim_transform_markup(base_mk)
                if am is not None:
                    anim_updates["reply_markup"] = am
            if hasattr(method, "parse_mode") and not getattr(method, "entities", None) and not getattr(method, "caption_entities", None):
                pm = getattr(method, "parse_mode", None)
                if (isinstance(pm, str) and pm.upper() == "HTML") or (pm is not None and not isinstance(pm, str)):
                    for field in ("text", "caption"):
                        val = getattr(method, field, None)
                        if isinstance(val, str):
                            nv = anim_transform_text(val)
                            if nv is not None:
                                anim_updates[field] = nv
        if anim_updates:
            full = method.model_copy(update={**style_updates, **anim_updates})
    except Exception as e:
        logging.info(f"Tugma/emoji bezashda xato (e'tiborsiz): {e}")
        full = style_only = None
    for m in (full, style_only):
        if m is None:
            continue
        try:
            res = await make_request(bot, m)
            if m is style_only and full is not None:
                _anim_off_until = datetime.now().timestamp() + 600
                logging.warning("Animatsiyali emoji qabul qilinmadi (bot egasida Premium bo'lmasligi mumkin) — 10 daqiqa faqat ranglar ishlatiladi")
            return res
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                raise
            logging.warning(f"Bezatilgan xabar qabul qilinmadi, soddaroq ko'rinishda yuborilmoqda: {e}")
    return await make_request(bot, method)


main_bot.session.middleware(anim_request_middleware)

BOT_TYPES = {
    "kino_pro": "🎬 Kino BOT",
    "shop": "🛒 Savdo bot",
    "ai": "🤖 AI-yordamchi bot",
    "money": "💱 Pul (valyuta) bot",
    "translate": "🌐 Tarjimon bot",
    "taxi": "🚕 Taksi bot",
    "stars": "⭐ Stars sotish bot",
}

DEFAULT_PRICES = {
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
data.setdefault("pro_tariffs", {tid: dict(t) for tid, t in DEFAULT_TARIFFS.items()})

# Kino'dan boshqa barcha bot turlari uchun yagona oylik narx (tarifsiz, cheksiz foydalanuvchi)
data.setdefault("other_bot_price", DEFAULT_OTHER_BOT_PRICE)

# Kino'dan boshqa har bir bot turi endi o'zining alohida (lekin bot turi ichida hammaga bir xil) narxiga ega
data.setdefault("type_prices", {})
for _bt in BOT_TYPES:
    if _bt in ("kino_pro",):
        continue
    data["type_prices"].setdefault(_bt, data["other_bot_price"])

# Har bir bot turi uchun: bepul sinov muddati bormi yoki darhol pullikmi (admin sozlaydi)
# {bot_type: {"enabled": bool, "days": int}}
data.setdefault("bot_type_trial", {})
for _bt in BOT_TYPES:
    data["bot_type_trial"].setdefault(_bt, {"enabled": True, "days": TRIAL_DAYS})


def get_trial_config(bot_type: str) -> dict:
    return data["bot_type_trial"].get(bot_type, {"enabled": True, "days": TRIAL_DAYS})

# RAVSHAN BUILDER BOTning to'liq nusxalari (klonlari) shu yerda ro'yxatga olinadi.
# Har biri: {"token": "...", "username": "...", "created_at": "..."}
data.setdefault("platform_clones", [])

# ✨ Animatsiyali emojilar kutubxonasi: {oddiy_emoji: custom_emoji_id} va tugmalarda yoqilganlik holati
data.setdefault("anim_emojis", {})
data.setdefault("anim_enabled", False)

# 🎨 Tugma ranglari: {tugma_matni: "primary"|"success"|"danger"} va bot yuborgan tugmalar ro'yxati
data.setdefault("btn_colors", {})
data.setdefault("btn_seen", [])

# 📝 Matnlar: Bot Creator xabar matnlari. Tahrirlanganlari BAZADA saqlanadi ({kalit: matn}) —
# kodga yangi qism qo'shilganda yoki kod yangilanganda ham o'chib ketmaydi.
data.setdefault("custom_texts", {})


def _som(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)


TEXT_REGISTRY = {
    "start_welcome": {
        "title": "👋 Bosh menyu (/start)",
        "vars": {
            "tariff_lines": "Kino BOT tariflari ro'yxati (avtomatik)",
            "other_lines": "Boshqa bot turlari narxlari (avtomatik)",
            "trial_days": "Bepul sinov kunlari soni",
        },
        "sample": {"tariff_lines": "💠 Standart — 77 770 so'm/oy", "other_lines": "💠 🛒 Savdo bot — 50 000 so'm/oy", "trial_days": 7},
        "text": """🤖 <b>Bot Creator</b> — Telegram botlar yaratish uchun qulay platforma

Bu platforma orqali siz hech qanday kod yozmasdan o'z Telegram botlaringizni tez va oson yaratishingiz, ularni tahrirlashingiz hamda boshqarishingiz mumkin.

⚡ <b>Nega aynan Bot Creator?</b>
• Botlar muntazam yangilanib boriladi
• Barqaror va mukammal ishlaydigan tizim
• To'liq o'zbek tilidagi qulay interfeys
• Doimiy va tezkor qo'llab-quvvatlash xizmati
• Barcha jarayonlar avtomatik va tushunarli

💳 <b>🎬 Kino BOT tariflari:</b>
{tariff_lines}

💳 <b>Boshqa bot turlari:</b>
{other_lines}

🎁 Har bir bot uchun {trial_days} kunlik BEPUL sinov muddati bor!

Pastdagi menyudan foydalaning 👇""",
    },
    "guide": {
        "title": "📖 Qo'llanma",
        "vars": {"trial_days": "Bepul sinov kunlari soni"},
        "sample": {"trial_days": 7},
        "text": """📖 <b>Qo'llanma</b>

1️⃣ "🤖 Bot yaratish" tugmasini bosing
2️⃣ @BotFather orqali yangi bot yarating va tokenini shu yerga yuboring
3️⃣ Bot turini tanlang (Kino, Savdo, Taksi va h.k.)
4️⃣ {trial_days} kunlik bepul sinovdan foydalaning
5️⃣ Sinov tugagach, "💰 Hisob to'ldirish" orqali balansingizni to'ldirib, botingizni faollashtiring

❓ Savollaringiz bo'lsa — "📩 Murojaat" tugmasini bosing.""",
    },
    "choose_type": {
        "title": "🤖 Bot turini tanlash",
        "vars": {},
        "sample": {},
        "text": "🤖 Quyidagi bot turlaridan birini tanlang:",
    },
    "token_prompt": {
        "title": "🔑 Token so'rash",
        "vars": {"bot_type": "Tanlangan bot turi nomi"},
        "sample": {"bot_type": "🎬 Kino BOT"},
        "text": """{bot_type}

Yangi bot tokenini yuboring.
(@BotFather orqali /newbot bilan yaratib, tokenni shu yerga joylashtiring)""",
    },
    "low_balance": {
        "title": "⚠️ Hisobda mablag' yetarli emas",
        "vars": {
            "template": "Tanlangan shablon nomi",
            "price": "Yaratish narxi (so'm)",
            "balance": "Foydalanuvchi balansi (so'm)",
            "missing": "Yetishmayotgan summa (so'm)",
        },
        "sample": {"template": "🎬 Kino BOT", "price": "7 999", "balance": "200", "missing": "7 799"},
        "text": """⚠️ <b>Hisobingizda mablag' yetarli emas!</b>
<blockquote>📦 Shablon: {template}
💳 Yaratish narxi: <b>{price} so'm</b>
💰 Balansingiz: <b>{balance} so'm</b>
📉 Yetishmayotgan summa: <b>{missing} so'm</b></blockquote>
Ushbu botni yaratish uchun avval balansingizni to'ldiring 👇""",
    },
    "topup_prompt": {
        "title": "💰 Hisob to'ldirish (summa so'rash)",
        "vars": {"min": "Minimal summa", "max": "Maksimal summa"},
        "sample": {"min": "10,000", "max": "5,000,000"},
        "text": """💰 Hisobni to'ldirish uchun summani kiriting.

Minimal: {min} so'm
Maksimal: {max} so'm""",
    },
    "referral": {
        "title": "🎁 Referal dasturi",
        "vars": {"count": "Taklif qilganlar soni", "link": "Shaxsiy havola"},
        "sample": {"count": 3, "link": "https://t.me/bot?start=ref_1"},
        "text": """🎁 <b>Referal dasturi</b>

Do'stingizni shu havola orqali taklif qiling — u botga kirishi bilanoq siz xabardor bo'lasiz.

👥 Jami taklif qilganlaringiz: <b>{count}</b> kishi

🔗 Sizning shaxsiy havolangiz:
<code>{link}</code>""",
    },
    "contact": {
        "title": "📩 Murojaat",
        "vars": {},
        "sample": {},
        "text": "Administrator bilan bog'lanish uchun quyidagi tugmani bosing 👇",
    },
    "site_open": {
        "title": "🌐 Saytga kirish",
        "vars": {},
        "sample": {},
        "text": "Botlaringiz va balansingizni ko'rish uchun quyidagi tugmani bosing 👇",
    },
    "site_missing": {
        "title": "🌐 Sayt sozlanmagan",
        "vars": {},
        "sample": {},
        "text": "🌐 Sayt hozircha sozlanmagan. Birozdan so'ng qayta urinib ko'ring.",
    },
    "phone_request": {
        "title": "📱 Telefon raqam so'rash",
        "vars": {},
        "sample": {},
        "text": "📱 Botdan foydalanish uchun telefon raqamingizni ulashing:",
    },
    "blocked_user": {
        "title": "🚫 Bloklangan foydalanuvchi",
        "vars": {},
        "sample": {},
        "text": """🚫 Siz Bot Creator'dan foydalanishdan bloklangansiz.

Admin bilan bog'lanish uchun murojaat qiling.""",
    },
}

_TEXT_VAR_RE = re.compile(r"\{(\w+)\}")
_TG_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9\-]*)(?:\s[^<>]*)?>")
_TG_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a",
                    "blockquote", "tg-spoiler", "tg-emoji", "span"}


def _html_problem(text: str):
    """Telegram HTML uchun oddiy tekshiruv: teglar ruxsat etilganmi va to'g'ri yopilganmi. Muammo bo'lsa matn qaytaradi."""
    stack = []
    for m in _TG_TAG_RE.finditer(text):
        closing, name = m.group(1) == "/", m.group(2).lower()
        if name not in _TG_ALLOWED_TAGS:
            return f"Ruxsat etilmagan HTML teg: <{name}>"
        if closing:
            if not stack or stack[-1] != name:
                return f"</{name}> tegi noto'g'ri joyda yoki ochilmagan."
            stack.pop()
        else:
            stack.append(name)
    if stack:
        return f"<{stack[-1]}> tegi yopilmagan."
    rest = _TG_TAG_RE.sub("", text)
    if "<" in rest or ">" in rest:
        return "Matnda ortiqcha '<' yoki '>' belgisi bor. Oddiy belgi sifatida &lt; va &gt; yozing."
    return None


def text_is_edited(key: str) -> bool:
    v = (data.get("custom_texts") or {}).get(key)
    return isinstance(v, str) and bool(v.strip())


def text_template(key: str) -> str:
    if text_is_edited(key):
        return data["custom_texts"][key]
    return TEXT_REGISTRY[key]["text"]


def _render_text(tpl: str, kw: dict) -> str:
    return _TEXT_VAR_RE.sub(lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0), tpl)


def T(key: str, **kw) -> str:
    """Bot Creator matnini qaytaradi: admin tahrirlagan bo'lsa o'sha, bo'lmasa koddagi standart matn."""
    default = TEXT_REGISTRY[key]["text"]
    if text_is_edited(key):
        try:
            out = _render_text(data["custom_texts"][key], kw)
            if _html_problem(out) is None:
                return out
            logging.warning(f"Tahrirlangan matn ({key}) HTML xatosi tufayli standart matn ishlatildi")
        except Exception as e:
            logging.warning(f"Tahrirlangan matn ({key}) ishlamadi: {e}")
    return _render_text(default, kw)


def text_validate(key: str, text: str):
    """(ok, xato) qaytaradi."""
    if key not in TEXT_REGISTRY:
        return False, "Noma'lum matn."
    if not text or not text.strip():
        return False, "Matn bo'sh bo'lishi mumkin emas."
    if len(text) > 3500:
        return False, "Matn juda uzun (3500 belgidan oshmasin)."
    allowed = set(TEXT_REGISTRY[key]["vars"])
    bad = sorted({v for v in _TEXT_VAR_RE.findall(text) if v not in allowed})
    if bad:
        names = ", ".join("{" + b + "}" for b in bad)
        have = ", ".join("{" + a + "}" for a in allowed) or "yo'q"
        return False, f"Noma'lum o'zgaruvchi: {names}. Mavjud o'zgaruvchilar: {have}"
    prob = _html_problem(text)
    if prob:
        return False, prob
    return True, ""


def text_save(key: str, text: str):
    data.setdefault("custom_texts", {})[key] = text
    save_data()


def text_reset(key: str):
    data.setdefault("custom_texts", {}).pop(key, None)
    save_data()

# Bot Creator platformasining o'ziga /start bosgan barcha foydalanuvchilar
data.setdefault("platform_users", [])

# Bot Creator platformasining referal tizimi
data.setdefault("platform_referrals", {})    # {str(referrer_uid): [referred_uid, ...]}
data.setdefault("platform_referred_by", {})  # {str(referred_uid): referrer_uid}
data.setdefault("platform_user_info", {})    # {str(uid): {"username": str, "phone": str|None}}
data.setdefault("platform_phone_asked", [])  # kimlardan telefon so'ralgani (qayta so'ramaslik uchun)

# Bot Creator'ning o'ziga majburiy obuna kanallari — {chat_id yoki social_xxxx: {...}}
data.setdefault("platform_channels", {})

# Admin tomonidan bloklangan foydalanuvchilar (bot yaratish/Bot Creator'dan foydalanish ta'qiqlanadi)
data.setdefault("blocked_users", [])

# Bosh admin (ADMIN_ID) tomonidan qo'shilgan, bosh admin bilan BARAVAR huquqqa ega
# to'liq adminlar (Bot Creator'ning butun admin panelidan foydalana oladi)
data.setdefault("full_admins", [])


def is_full_admin(uid: int) -> bool:
    return uid == ADMIN_ID or uid in data["full_admins"]


# QOIDA: botga rasm/video to'g'ridan-to'g'ri yuklab bo'lmaydi — faqat FORWARD (uzatib) qilingan xabar qabul qilinadi.
FORWARD_ONLY_TEXT = (
    "🚫 Bu botga rasm yoki videoni to'g'ridan-to'g'ri yuklab bo'lmaydi.\n"
    "Faqat <b>forward (uzatib)</b> qilingan xabarni yuboring: kanal yoki chatdagi videoni "
    "tanlab «Forward / Uzatish» tugmasini bosing va shu botni tanlang."
)


def is_forwarded_message(message) -> bool:
    """Xabar boshqa joydan uzatilgan (forward) bo'lsagina True. Yangi yuklangan media uchun False."""
    for attr in ("forward_origin", "forward_from_chat", "forward_from", "forward_sender_name", "forward_date"):
        try:
            if getattr(message, attr, None):
                return True
        except Exception:
            continue
    return False

# Endi barcha botlar narxi hamma uchun bir xil (tarifga/other_bot_price'ga qarab) —
# ilgari qo'yilgan har qanday "maxsus narx"larni tozalaymiz.
for _b in data["bots"].values():
    _b.pop("custom_price", None)

running_platform_clones = {}  # token -> asyncio task


def get_price(bot_type: str) -> int:
    return data["prices"].get(bot_type, DEFAULT_PRICES.get(bot_type, 0))


def get_monthly_rate() -> float:
    return data.get("monthly_rate", DEFAULT_MONTHLY_RATE)


def tariffs_for(bot_type: str = "kino_pro") -> dict:
    return data["pro_tariffs"]


def cheapest_tariff_price(bot_type: str = "kino_pro") -> int:
    pool = tariffs_for(bot_type)
    if not pool:
        return 0
    return min(t["price"] for t in pool.values())


def get_tariff(tariff_id: str, bot_type: str = "kino_pro") -> dict:
    pool = tariffs_for(bot_type)
    return pool.get(tariff_id, DEFAULT_TARIFFS.get(tariff_id, DEFAULT_TARIFFS["2"]))


OTHER_BOT_TARIFF_NAME = "Standart"


def get_bot_tariff(info: dict) -> dict:
    if info.get("type") in ("kino_pro",):
        return get_tariff(info.get("tariff", "2"), info.get("type"))
    price = data["type_prices"].get(info.get("type"), data.get("other_bot_price", DEFAULT_OTHER_BOT_PRICE))
    return {"name": OTHER_BOT_TARIFF_NAME, "price": price, "daily_limit": None}


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
    "kino_pro": (
        "<i>Kino botning barcha imkoniyatlari + VIP tizimi bilan.</i>\n\n"
        "🔒 VIP-maxsus kino qo'shish, faqat Premium foydalanuvchilarga ko'rinadigan yopiq "
        "kontent, 💎 VIP kinolar katalogi — bularning barchasi faqat Kino BOTda mavjud.\n\n"
        "🏷 Kategoriyalar, ⭐ tavsiyalar, 📈 TOP reyting, baholash, 🗓 rejalashtirilgan "
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
    if is_full_admin(info.get("admin_id")):
        return True  # Platforma egasi/to'liq adminlar yaratgan botlar — umrbod, hech qachon to'lov so'ralmaydi
    paid_until = info.get("paid_until")
    if paid_until and datetime.now() < datetime.fromisoformat(paid_until):
        return True
    if info.get("skip_trial"):
        return False  # Bu foydalanuvchining birinchi bepul boti emas — darhol to'lov talab qilinadi
    trial_cfg = get_trial_config(info.get("type"))
    if not trial_cfg.get("enabled", True):
        return False  # Bu bot turi uchun sinov o'chirilgan — darhol to'lov talab qilinadi
    created = datetime.fromisoformat(info["created_at"])
    return datetime.now() < created + timedelta(days=trial_cfg.get("days", TRIAL_DAYS))


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
    waiting_title = State()
    waiting_desc = State()
    waiting_price = State()
    waiting_video = State()


class KinoSearch(StatesGroup):
    waiting_query = State()


class AddMoviesBulk(StatesGroup):
    waiting_start = State()
    waiting_video = State()


class MovieAccessFlow(StatesGroup):
    waiting_title = State()
    waiting_price = State()


class MoviePurchase(StatesGroup):
    waiting_check = State()


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


class PlatformChannel(StatesGroup):
    choosing_type = State()
    waiting_username = State()
    waiting_title = State()
    waiting_link = State()


class AdminUserSearch(StatesGroup):
    waiting_query = State()


class AdminBlockUser(StatesGroup):
    waiting_id = State()


class AdminDeleteBot(StatesGroup):
    waiting_id = State()


class AdminBroadcast(StatesGroup):
    waiting_text = State()
    waiting_confirm = State()


class FullAdminManage(StatesGroup):
    waiting_id = State()


class PaymentSystemAdd(StatesGroup):
    waiting_name = State()
    waiting_number = State()
    waiting_owner = State()


class AutoPayAdd(StatesGroup):
    waiting_value = State()


class AnimEmojiAdd(StatesGroup):
    waiting_text = State()


class ColorTextAdd(StatesGroup):
    waiting_text = State()


class TextEdit(StatesGroup):
    waiting = State()


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


class AdminSubtractBalance(StatesGroup):
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
    @dp.message(F.text == "👮 Adminlar")
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
    @dp.message(F.text == "🔐 Kanallar")
    async def channels_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("📡 Majburiy obuna boshqaruvi:", reply_markup=channels_admin_kb())


# ---------- Bosh (creator) bot — XALQ UCHUN OMMAVIY ----------
def types_kb():
    # "stars" endi platformada mavjud emas — kino_pro yagona kino turi.
    # (Eski oddiy kino botlar startup vaqtida avtomatik kino_pro ga ko'chiriladi.)
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"type_{key}")]
        for key, name in BOT_TYPES.items() if key != "stars"
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def tariff_kb(only_ids=None, bot_type: str = "kino_pro"):
    items = tariffs_for(bot_type).items()
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

    def platform_info() -> dict:
        # require_subscription/get_missing_channels/subscribe_kb funksiyalari kutgan
        # shaklga moslashtirilgan "soxta" bot-info — chunki Bot Creator data["bots"]da emas.
        return {"admin_id": ADMIN_ID, "admin_ids": [ADMIN_ID], "channels": data["platform_channels"], "premium_enabled": False}

    def platform_channel_type_kb():
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📢 Telegram kanal", callback_data="pchtype_telegram"),
                InlineKeyboardButton(text="📸 Instagram", callback_data="pchtype_instagram"),
            ],
            [
                InlineKeyboardButton(text="🎵 TikTok", callback_data="pchtype_tiktok"),
                InlineKeyboardButton(text="▶️ YouTube", callback_data="pchtype_youtube"),
            ],
            [InlineKeyboardButton(text="🌐 Boshqa havola", callback_data="pchtype_other")],
        ])

    def platform_channels_admin_kb():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="pch_add")],
            [InlineKeyboardButton(text="📋 Kanallar ro'yxati", callback_data="pch_list")],
            [InlineKeyboardButton(text="➖ Kanal o'chirish", callback_data="pch_del")],
        ])

    @dp.message(F.text == "📢 Majburiy obuna")
    async def platform_channels_panel(message: Message):
        if not is_full_admin(message.from_user.id):
            return
        await message.answer("📢 Bot Creator uchun majburiy obuna boshqaruvi:", reply_markup=platform_channels_admin_kb())

    @dp.callback_query(F.data == "pch_add")
    async def pch_add_cb(callback: CallbackQuery, state: FSMContext):
        if not is_full_admin(callback.from_user.id):
            return
        await callback.message.answer("Kanal turini tanlang:", reply_markup=platform_channel_type_kb())
        await state.set_state(PlatformChannel.choosing_type)
        await callback.answer()

    @dp.callback_query(PlatformChannel.choosing_type, F.data.startswith("pchtype_"))
    async def pch_type_chosen_cb(callback: CallbackQuery, state: FSMContext):
        if not is_full_admin(callback.from_user.id):
            return
        ctype = callback.data.split("_", 1)[1]
        if ctype == "telegram":
            await callback.message.answer(
                "Kanal usernameni yuboring (masalan: @mening_kanalim).\n"
                "⚠️ Bot o'sha kanalda ADMIN bo'lishi shart!"
            )
            await state.set_state(PlatformChannel.waiting_username)
        else:
            await state.update_data(ch_type=ctype)
            await callback.message.answer("Kanal/sahifa nomini yuboring (masalan: Ravshan Media):")
            await state.set_state(PlatformChannel.waiting_title)
        await callback.answer()

    @dp.message(PlatformChannel.waiting_username)
    async def pch_username_process(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        username = message.text.strip()
        try:
            chat = await message.bot.get_chat(username)
            data["platform_channels"][str(chat.id)] = {"type": "telegram", "username": username, "title": chat.title}
            save_data()
            await message.answer(f"✅ Qo'shildi: {chat.title}")
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

    @dp.message(PlatformChannel.waiting_title)
    async def pch_title_process(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.update_data(ch_title=message.text.strip())
        await state.set_state(PlatformChannel.waiting_link)
        await message.answer("Endi havolani (linkni) yuboring (masalan: https://instagram.com/...):")

    @dp.message(PlatformChannel.waiting_link)
    async def pch_link_process(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        url = message.text.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url
        fsm_data = await state.get_data()
        ctype = fsm_data.get("ch_type", "other")
        title = fsm_data.get("ch_title", "Havola")
        key = f"social_{uuid.uuid4().hex[:8]}"
        data["platform_channels"][key] = {"type": ctype, "title": title, "url": url}
        save_data()
        await message.answer(f"✅ Qo'shildi: {title}")
        await state.clear()

    @dp.callback_query(F.data == "pch_list")
    async def pch_list_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        if not data["platform_channels"]:
            await callback.message.answer("Hozircha majburiy kanallar yo'q.")
        else:
            lines = [f"{SOCIAL_EMOJI.get(c.get('type', 'telegram'), '📢')} {c['title']}" for c in data["platform_channels"].values()]
            await callback.message.answer("📋 <b>Majburiy kanallar:</b>\n\n" + "\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "pch_del")
    async def pch_del_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        if not data["platform_channels"]:
            await callback.answer("Hozircha kanallar yo'q.", show_alert=True)
            return
        buttons = [[InlineKeyboardButton(text=c["title"], callback_data=f"pchdel_{cid}")] for cid, c in data["platform_channels"].items()]
        await callback.message.answer("O'chirmoqchi bo'lgan kanalni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("pchdel_"))
    async def pch_del_pick_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        cid = callback.data.split("_", 1)[1]
        removed = data["platform_channels"].pop(cid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['title']}")
        await callback.answer()

    @dp.callback_query(F.data == "check_sub")
    async def platform_check_sub_cb(callback: CallbackQuery):
        missing = await get_missing_channels(callback.bot, data["platform_channels"], callback.from_user.id)
        if missing:
            await callback.answer("Hali barcha kanallarga obuna bo'lmagansiz ❌", show_alert=True)
        else:
            await callback.message.edit_text("✅ Obuna tasdiqlandi! Endi /start bosing.")
            await callback.answer()

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
        if is_full_admin(uid):
            _aurl = get_adminapp_url()
            if _aurl:
                pass   # Admin panel endi klaviatura yonidagi Menu tugmasida («Admin»)
            else:
                # Mini app URL yo'q bo'lsa, admin panelsiz qolmasligi uchun eski tugmalar saqlanadi
                keyboard.append([KeyboardButton(text="📊 Statistika"), KeyboardButton(text="➕ Hisob qo'shish")])
                keyboard.append([KeyboardButton(text="➖ Hisob ayirish")])
                keyboard.append([KeyboardButton(text="💵 Tariflar"), KeyboardButton(text="💳 To'lov tizimlar")])
                keyboard.append([KeyboardButton(text="⭐ Stars kursi"), KeyboardButton(text="👥 Hamkor-adminlar")])
                keyboard.append([KeyboardButton(text="🎁 Sinov/Pullik")])
                keyboard.append([KeyboardButton(text="🚀 Ultra statistika"), KeyboardButton(text="🏆 Top referal")])
                keyboard.append([KeyboardButton(text="🔝 TOP faol")])
                keyboard.append([KeyboardButton(text="📢 Majburiy obuna")])
                keyboard.append([KeyboardButton(text="🔍 Foydalanuvchi qidirish"), KeyboardButton(text="🚫 Bloklash")])
                keyboard.append([KeyboardButton(text="🗑 Botni o'chirish"), KeyboardButton(text="📅 Tugayotgan botlar")])
                keyboard.append([KeyboardButton(text="📢 Barchaga xabar"), KeyboardButton(text="📤 Zaxira nusxa")])
                if uid == ADMIN_ID:
                    keyboard.append([KeyboardButton(text="👑 To'liq admin qo'shish/olib tashlash")])
            keyboard.append([KeyboardButton(text="✨ Animatsiya"), KeyboardButton(text="🎨 Ranglar")])
            keyboard.append([KeyboardButton(text="📝 Matnlar")])
        elif str(uid) in data["sub_admins"]:
            keyboard.append([KeyboardButton(text="➕ Hisob qo'shish"), KeyboardButton(text="💼 Mening daromadim")])
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

    # 🎨 Ranglar ro'yxatiga asosiy menyu tugmalari oldindan qo'shiladi (hech narsa yuborilmasa ham ko'rinadi)
    try:
        _seed_seen = data.setdefault("btn_seen", [])
        for _u in (ADMIN_ID, 0):
            for _row in main_menu_kb(_u).keyboard:
                for _b in _row:
                    _btn_seen_add(_btn_key(_b.text), _seed_seen)
        for _row in types_kb().inline_keyboard:
            for _b in _row:
                _btn_seen_add(_btn_key(_b.text), _seed_seen)
        del _seed_seen[300:]
    except Exception as _e:
        logging.info(f"Tugmalar katalogini to'ldirishda xato (e'tiborsiz): {_e}")

    @dp.message(Command("start"))
    async def main_start(message: Message):
        uid = message.from_user.id
        if uid in data["blocked_users"] and not is_full_admin(uid):
            await message.answer(T("blocked_user"), reply_markup=contact_admin_kb())
            return
        if is_full_admin(uid):
            await set_admin_menu_button(message.bot, uid)
        args = message.text.split(maxsplit=1)
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        data["platform_user_info"].setdefault(str(uid), {"username": uname, "phone": None})
        data["platform_user_info"][str(uid)]["username"] = uname
        is_new = uid not in data["platform_users"]
        if is_new:
            data["platform_users"].append(uid)
            if len(args) > 1 and args[1].startswith("ref_"):
                try:
                    ref_uid = int(args[1].split("_", 1)[1])
                except ValueError:
                    ref_uid = None
                if ref_uid and ref_uid != uid and str(uid) not in data["platform_referred_by"]:
                    data["platform_referred_by"][str(uid)] = ref_uid
                    refs = data["platform_referrals"].setdefault(str(ref_uid), [])
                    if uid not in refs:
                        refs.append(uid)
                    try:
                        await message.bot.send_message(
                            ref_uid,
                            f"🎉 <b>Taklif qilingan yangi foydalanuvchi!</b>\n\n"
                            f"👤 {uname} sizning havolangiz orqali botga qo'shildi.\n"
                            f"🎁 Jami takliflaringiz: {len(refs)} kishi",
                        )
                    except Exception:
                        pass
            save_data()
        else:
            save_data()
        if not await require_subscription(message, platform_info(), ADMIN_ID):
            return
        if not is_full_admin(uid) and not data["platform_user_info"].get(str(uid), {}).get("phone"):
            phone_kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="📱 Raqamni ulashish", request_contact=True)]],
                resize_keyboard=True,
            )
            await message.answer(
                T("phone_request"),
                reply_markup=phone_kb,
            )
            return
        if len(args) > 1 and args[1] == "newbot":
            await message.answer(T("choose_type"), reply_markup=types_kb())
            return
        await show_platform_main_menu(message)

    async def show_platform_main_menu(message: Message):
        pro_tariff_lines = "\n".join(
            f"💠 {t['name']} — {t['price']:,} so'm/oy ({tariff_limit_text(t)})"
            for t in data["pro_tariffs"].values()
        )
        other_lines = "\n".join(
            f"💠 {BOT_TYPES[bt]} — {price:,} so'm/oy"
            for bt, price in data["type_prices"].items()
        )
        text = T("start_welcome", tariff_lines=pro_tariff_lines, other_lines=other_lines, trial_days=TRIAL_DAYS)
        await message.answer(text, reply_markup=main_menu_kb(message.from_user.id))

    @dp.message(F.text == "📖 Qo'llanma")
    async def guide_handler(message: Message):
        await message.answer(T("guide", trial_days=TRIAL_DAYS))

    @dp.message(F.text == "📩 Murojaat")
    async def murojaat_handler(message: Message):
        await message.answer(T("contact"), reply_markup=contact_admin_kb())

    @dp.message(F.text == "🌐 Saytga kirish")
    async def website_handler(message: Message):
        miniapp_url = get_miniapp_url()
        if not miniapp_url:
            await message.answer(T("site_missing"))
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Saytni ochish", web_app=WebAppInfo(url=miniapp_url))]
        ])
        await message.answer(T("site_open"), reply_markup=kb)

    @dp.message(F.text == "🎁 Referal")
    async def referral_handler(message: Message):
        uid = message.from_user.id
        me = await message.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{uid}"
        count = len(data["platform_referrals"].get(str(uid), []))
        share_text = f"🤖 Bot Creator — hech qanday kod yozmasdan o'z Telegram botingizni yarating!\n{link}"
        share_url = f"https://t.me/share/url?url={quote(link)}&text={quote(share_text)}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Do'stlarga yuborish", url=share_url)]
        ])
        await message.answer(T("referral", count=count, link=link), reply_markup=kb)

    @dp.message(F.contact)
    async def platform_contact_received(message: Message):
        uid = message.from_user.id
        if message.contact.user_id != uid:
            return
        data["platform_user_info"].setdefault(str(uid), {"username": None, "phone": None})
        data["platform_user_info"][str(uid)]["phone"] = message.contact.phone_number
        save_data()
        await message.answer("✅ Rahmat!")
        await show_platform_main_menu(message)

    async def send_platform_paginated(message: Message, header: str, lines: list, chunk_size: int = 30):
        if not lines:
            await message.answer(header + "\n\nHali foydalanuvchilar yo'q.")
            return
        for i in range(0, len(lines), chunk_size):
            chunk = lines[i:i + chunk_size]
            prefix = f"{header} ({i + 1}-{min(i + chunk_size, len(lines))} / {len(lines)})\n\n" if len(lines) > chunk_size else header + "\n\n"
            await message.answer(prefix + "\n".join(chunk))

    @dp.message(F.text == "🚀 Ultra statistika")
    async def platform_ultra_statistika(message: Message):
        if not is_full_admin(message.from_user.id):
            return
        rows = []
        for uid in data["platform_users"]:
            uinfo = data["platform_user_info"].get(str(uid), {})
            uname = uinfo.get("username") or "—"
            refcount = len(data["platform_referrals"].get(str(uid), []))
            rows.append((uid, uname, refcount))
        rows.sort(key=lambda r: r[2], reverse=True)
        lines = [f"👤 {uname} | ID: <code>{uid}</code> | 🎁 {refcount} ta taklif" for uid, uname, refcount in rows]
        await send_platform_paginated(message, f"🚀 <b>Ultra statistika</b> — jami {len(rows)} foydalanuvchi", lines)

    @dp.message(F.text == "🏆 Top referal")
    async def platform_top_referal(message: Message):
        if not is_full_admin(message.from_user.id):
            return
        rows = []
        for uid in data["platform_users"]:
            uinfo = data["platform_user_info"].get(str(uid), {})
            uname = uinfo.get("username") or "—"
            phone = uinfo.get("phone") or "—"
            refcount = len(data["platform_referrals"].get(str(uid), []))
            rows.append((uid, uname, phone, refcount))
        rows.sort(key=lambda r: r[3], reverse=True)
        lines = [
            f"{i + 1}. 👤 {uname} | 📞 {phone} | ID: <code>{uid}</code> | 🎁 {refcount} ta taklif"
            for i, (uid, uname, phone, refcount) in enumerate(rows)
        ]
        await send_platform_paginated(message, f"🏆 <b>Top referal</b> — jami {len(rows)} foydalanuvchi", lines)

    @dp.message(F.text == "🔝 TOP faol")
    async def platform_top_active(message: Message):
        if not is_full_admin(message.from_user.id):
            return
        rows = []
        for uid in data["platform_users"]:
            refcount = len(data["platform_referrals"].get(str(uid), []))
            if refcount <= 0:
                continue
            uinfo = data["platform_user_info"].get(str(uid), {})
            rows.append((uid, uinfo.get("username") or "—", refcount))
        rows.sort(key=lambda r: r[2], reverse=True)
        top3 = rows[:3]
        if not top3:
            await message.answer("🔝 <b>TOP faol</b>\n\nHozircha hech kim taklif qilmagan.")
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = "\n".join(f"{medals[i]} {uname} — <code>{uid}</code> — {cnt} ta taklif" for i, (uid, uname, cnt) in enumerate(top3))
        await message.answer(f"🔝 <b>TOP faol — eng ko'p taklif qilgan 3 kishi</b>\n\n{lines}")

    # ---------- 🔍 Foydalanuvchi qidirish ----------
    @dp.message(F.text == "🔍 Foydalanuvchi qidirish")
    async def admin_user_search_prompt(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await message.answer("🔍 Foydalanuvchi ID yoki username'ini kiriting:")
        await state.set_state(AdminUserSearch.waiting_query)

    @dp.message(AdminUserSearch.waiting_query)
    async def admin_user_search_run(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.clear()
        query = message.text.strip().lstrip("@").lower()
        found_uid = None
        if query.isdigit():
            found_uid = int(query)
        else:
            for uid_str, uinfo in data["platform_user_info"].items():
                uname = (uinfo.get("username") or "").lstrip("@").lower()
                if uname == query:
                    found_uid = int(uid_str)
                    break
        if found_uid is None or found_uid not in data["platform_users"]:
            await message.answer("❌ Bunday foydalanuvchi topilmadi.")
            return
        uinfo = data["platform_user_info"].get(str(found_uid), {})
        balance = data["user_balances"].get(str(found_uid), 0)
        own_bots = [b for b in data["bots"].values() if found_uid in b.get("admin_ids", [b["admin_id"]])]
        refcount = len(data["platform_referrals"].get(str(found_uid), []))
        blocked = "🚫 Ha" if found_uid in data["blocked_users"] else "✅ Yo'q"
        bots_lines = "\n".join(f"   • {b['name']} ({BOT_TYPES.get(b['type'], b['type'])})" for b in own_bots) or "   • yo'q"
        await message.answer(
            f"👤 <b>{uinfo.get('username') or '—'}</b>\n"
            f"🆔 ID: <code>{found_uid}</code>\n"
            f"📞 Telefon: {uinfo.get('phone') or '—'}\n"
            f"💰 Balans: {balance:,} so'm\n"
            f"🎁 Takliflar: {refcount} kishi\n"
            f"🚫 Bloklangan: {blocked}\n\n"
            f"🤖 <b>Botlari ({len(own_bots)}):</b>\n{bots_lines}"
        )

    # ---------- 🚫 Bloklash ----------
    @dp.message(F.text == "🚫 Bloklash")
    async def admin_block_prompt(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await message.answer("🚫 Bloklash/blokdan chiqarish uchun foydalanuvchi ID'sini kiriting:")
        await state.set_state(AdminBlockUser.waiting_id)

    @dp.message(AdminBlockUser.waiting_id)
    async def admin_block_run(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.clear()
        try:
            target_uid = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Noto'g'ri ID.")
            return
        if is_full_admin(target_uid):
            await message.answer("❌ Bosh admin yoki boshqa to'liq adminni bloklab bo'lmaydi.")
            return
        if target_uid in data["blocked_users"]:
            data["blocked_users"].remove(target_uid)
            save_data()
            await message.answer(f"✅ <code>{target_uid}</code> blokdan chiqarildi.")
        else:
            data["blocked_users"].append(target_uid)
            save_data()
            await message.answer(f"🚫 <code>{target_uid}</code> bloklandi.")

    # ---------- 🗑 Botni o'chirish ----------
    @dp.message(F.text == "🗑 Botni o'chirish")
    async def admin_delete_bot_prompt(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await message.answer("🗑 O'chirmoqchi bo'lgan botning ID raqamini kiriting (Botlar narxi ro'yxatida ko'rinadi):")
        await state.set_state(AdminDeleteBot.waiting_id)

    @dp.message(AdminDeleteBot.waiting_id)
    async def admin_delete_bot_run(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.clear()
        try:
            bot_id = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Noto'g'ri ID.")
            return
        target_token = None
        target_info = None
        for token, b in data["bots"].items():
            if b["id"] == bot_id:
                target_token, target_info = token, b
                break
        if not target_info:
            await message.answer("❌ Bunday ID'li bot topilmadi.")
            return
        task = running_bots.pop(target_token, None)
        if task:
            task.cancel()
        del data["bots"][target_token]
        save_data()
        await message.answer(f"🗑 <b>{target_info['name']}</b> o'chirildi va to'xtatildi.")

    # ---------- 📅 Tugayotgan botlar ----------
    @dp.message(F.text == "📅 Tugayotgan botlar")
    async def admin_expiring_bots(message: Message):
        if not is_full_admin(message.from_user.id):
            return
        now = datetime.now()
        soon = []
        for b in data["bots"].values():
            if is_full_admin(b.get("admin_id")):
                continue
            paid_until = b.get("paid_until")
            if paid_until:
                expiry = datetime.fromisoformat(paid_until)
            else:
                trial_cfg = get_trial_config(b.get("type"))
                if not trial_cfg.get("enabled", True):
                    continue
                expiry = datetime.fromisoformat(b["created_at"]) + timedelta(days=trial_cfg.get("days", TRIAL_DAYS))
            remaining = expiry - now
            if timedelta(0) <= remaining <= timedelta(days=2):
                soon.append((b, expiry, remaining))
        if not soon:
            await message.answer("✅ Yaqin 2 kun ichida muddati tugaydigan bot yo'q.")
            return
        soon.sort(key=lambda x: x[2])
        lines = [
            f"⏳ <b>{b['name']}</b> ({BOT_TYPES.get(b['type'], b['type'])}) — {format_remaining(rem)} qoldi (egasi: <code>{b['admin_id']}</code>)"
            for b, exp, rem in soon
        ]
        await send_platform_paginated(message, f"📅 <b>Yaqin muddatda tugaydigan botlar</b> — {len(soon)} ta", lines)

    # ---------- 📢 Barchaga xabar (broadcast) ----------
    @dp.message(F.text == "📢 Barchaga xabar")
    async def admin_broadcast_prompt(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await message.answer("📢 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabar matnini kiriting:")
        await state.set_state(AdminBroadcast.waiting_text)

    @dp.message(AdminBroadcast.waiting_text)
    async def admin_broadcast_preview(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.update_data(broadcast_text=message.text)
        await state.set_state(AdminBroadcast.waiting_confirm)
        await message.answer(
            f"📢 <b>Quyidagi xabar {len(data['platform_users'])} ta foydalanuvchiga yuboriladi:</b>\n\n{message.text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Yuborish", callback_data="bc_send"), InlineKeyboardButton(text="❌ Bekor qilish", callback_data="bc_cancel")]
            ]),
        )

    @dp.callback_query(AdminBroadcast.waiting_confirm, F.data == "bc_cancel")
    async def admin_broadcast_cancel_cb(callback: CallbackQuery, state: FSMContext):
        if not is_full_admin(callback.from_user.id):
            return
        await state.clear()
        await callback.message.edit_text("❌ Bekor qilindi.")
        await callback.answer()

    @dp.callback_query(AdminBroadcast.waiting_confirm, F.data == "bc_send")
    async def admin_broadcast_send_cb(callback: CallbackQuery, state: FSMContext):
        if not is_full_admin(callback.from_user.id):
            return
        fsm_data = await state.get_data()
        text = fsm_data.get("broadcast_text", "")
        await state.clear()
        await callback.message.edit_text("⏳ Yuborilmoqda...")
        sent, failed = 0, 0
        for uid in data["platform_users"]:
            try:
                await callback.bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await callback.message.answer(f"✅ Yuborildi: {sent} ta\n❌ Yuborilmadi: {failed} ta")
        await callback.answer()

    # ---------- 📤 Zaxira nusxa ----------
    @dp.message(F.text == "📤 Zaxira nusxa")
    async def admin_backup(message: Message):
        if not is_full_admin(message.from_user.id):
            return
        save_data()
        try:
            await message.answer_document(FSInputFile(DATA_FILE), caption=f"📤 Zaxira nusxa — {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
        except Exception as e:
            await message.answer(f"❌ Xatolik: {e}")

    @dp.message(F.text == "👑 To'liq admin qo'shish/olib tashlash")
    async def full_admin_prompt(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        lines = "\n".join(f"👑 <code>{uid}</code>" for uid in data["full_admins"]) or "   • hozircha yo'q"
        await message.answer(
            f"👑 <b>To'liq adminlar</b> (bosh admin bilan bir xil huquqqa ega):\n{lines}\n\n"
            "Qo'shish yoki olib tashlash uchun foydalanuvchi ID raqamini kiriting:"
        )
        await state.set_state(FullAdminManage.waiting_id)

    @dp.message(FullAdminManage.waiting_id)
    async def full_admin_toggle(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        await state.clear()
        try:
            target_uid = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Noto'g'ri ID.")
            return
        if target_uid == ADMIN_ID:
            await message.answer("❌ Siz allaqachon bosh adminsiz.")
            return
        if target_uid in data["full_admins"]:
            data["full_admins"].remove(target_uid)
            save_data()
            await message.answer(f"➖ <code>{target_uid}</code> endi to'liq admin emas.")
        else:
            data["full_admins"].append(target_uid)
            save_data()
            await message.answer(f"👑 <code>{target_uid}</code> endi bosh admin bilan BARAVAR huquqqa ega to'liq admin!")
            try:
                await message.bot.send_message(
                    target_uid,
                    "👑 Sizga Bot Creator'da <b>to'liq admin</b> huquqi berildi — endi bosh admin bilan bir xil barcha imkoniyatlardan foydalana olasiz.\n\n/start bosing.",
                )
            except Exception:
                pass

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
        if not is_full_admin(message.from_user.id):
            return
        if not data["payment_systems"]:
            await message.answer("⚠️ To'lov tizimlari mavjud emas.", reply_markup=platform_payment_systems_kb())
        else:
            await message.answer("💳 To'lov tizimlari boshqaruvi:", reply_markup=platform_payment_systems_kb())

    @dp.callback_query(F.data == "pps_add")
    async def pps_add_cb(callback: CallbackQuery, state: FSMContext):
        if not is_full_admin(callback.from_user.id):
            return
        await callback.message.answer("Iltimos, to'lov tizimi nomini kiriting:\n\n(Masalan: Click, Payme, Humo, Uzcard...)")
        await state.set_state(PaymentSystemAdd.waiting_name)
        await callback.answer()

    @dp.message(PaymentSystemAdd.waiting_name)
    async def pps_name_process(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.update_data(ps_name=message.text.strip())
        await message.answer("Iltimos, to'lov tizimi raqamini kiriting:\n\n(Masalan: karta yoki hisob raqami)")
        await state.set_state(PaymentSystemAdd.waiting_number)

    @dp.message(PaymentSystemAdd.waiting_number)
    async def pps_number_process(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.update_data(ps_number=message.text.strip())
        await message.answer("Hisob raqami egasining to'liq ismini kiriting:\n\n(Masalan: Ism Familiya)")
        await state.set_state(PaymentSystemAdd.waiting_owner)

    @dp.message(PaymentSystemAdd.waiting_owner)
    async def pps_owner_process(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
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
        if not is_full_admin(callback.from_user.id):
            return
        if not data["payment_systems"]:
            await callback.message.answer("To'lov tizimlari mavjud emas.")
        else:
            lines = [f"• {p['name']} — {p['number']} ({p['owner']})" for p in data["payment_systems"].values()]
            await callback.message.answer("💳 To'lov tizimlari:\n\n" + "\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "pps_del")
    async def pps_del_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
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
        if not is_full_admin(callback.from_user.id):
            return
        pid = callback.data.split("_", 1)[1]
        removed = data["payment_systems"].pop(pid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['name']}")
        await callback.answer()

    @dp.message(F.text == "💰 Hisob to'ldirish")
    async def topup_start(message: Message, state: FSMContext):
        await message.answer(T("topup_prompt", min=f"{MIN_TOPUP:,}", max=f"{MAX_TOPUP:,}"))
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
        if not is_full_admin(callback.from_user.id):
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
        if not is_full_admin(callback.from_user.id):
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
        if not is_full_admin(message.from_user.id):
            return
        total_bots = len(data["bots"])
        active_bots = sum(1 for i in data["bots"].values() if is_active(i))
        total_balance = sum(data["user_balances"].values())
        type_counts = {}
        for b in data["bots"].values():
            type_counts[b["type"]] = type_counts.get(b["type"], 0) + 1
        type_lines = "\n".join(
            f"   • {BOT_TYPES.get(bt, bt)}: {cnt}"
            for bt, cnt in sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
        ) or "   • hali botlar yo'q"
        await message.answer(
            "📊 <b>Platforma statistikasi</b>\n\n"
            f"👤 Bot Creator'ga kirgan odamlar: {len(data['platform_users']):,}\n\n"
            f"🤖 Jami botlar: {total_bots}\n"
            f"🟢 Faol botlar: {active_bots}\n\n"
            f"📁 <b>Turlari bo'yicha:</b>\n{type_lines}\n\n"
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
        if not is_full_admin(uid) and key not in data["user_referring_admin"]:
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

    @dp.message(F.text == "➖ Hisob ayirish")
    async def admin_sub_balance_start(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await message.answer("Foydalanuvchi ID raqamini kiriting:")
        await state.set_state(AdminSubtractBalance.waiting_user_id)

    @dp.message(AdminSubtractBalance.waiting_user_id)
    async def admin_sub_balance_uid(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        try:
            target_uid = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqamli ID kiriting.")
            return
        await state.update_data(target_uid=target_uid)
        current = data["user_balances"].get(str(target_uid), 0)
        await message.answer(f"Joriy balans: {current:,} so'm.\nAyiriladigan summani kiriting (so'mda):")
        await state.set_state(AdminSubtractBalance.waiting_amount)

    @dp.message(AdminSubtractBalance.waiting_amount)
    async def admin_sub_balance_amount(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
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
        data["user_balances"][key] = data["user_balances"].get(key, 0) - amount
        save_data()
        await message.answer(f"✅ {target_uid} ID'li foydalanuvchidan {amount:,} so'm ayirildi.\n💰 Yangi balans: {data['user_balances'][key]:,} so'm")
        try:
            await message.bot.send_message(
                chat_id=target_uid,
                text=f"⚠️ Hisobingizdan administrator tomonidan {amount:,} so'm ayirildi.\n💰 Joriy balans: {data['user_balances'][key]:,} so'm",
            )
        except Exception as e:
            logging.error(f"Foydalanuvchiga xabar yuborishda xato: {e}")
        await state.clear()
    @dp.message(F.text == "👥 Hamkor-adminlar")
    async def sub_admins_panel(message: Message):
        if not is_full_admin(message.from_user.id):
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
        if not is_full_admin(callback.from_user.id):
            return
        await callback.message.answer("Hamkor-admin qilmoqchi bo'lgan foydalanuvchi ID raqamini kiriting:")
        await state.set_state(SubAdminAdd.waiting_id)
        await callback.answer()

    @dp.message(SubAdminAdd.waiting_id)
    async def subadmin_add_save(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
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
        if not is_full_admin(callback.from_user.id):
            return
        if not data["sub_admins"]:
            await callback.message.answer("Hamkor-adminlar yo'q.")
        else:
            lines = [f"• ID {uid} — daromad: {s['earnings']:,} so'm" for uid, s in data["sub_admins"].items()]
            await callback.message.answer("👥 Hamkor-adminlar:\n\n" + "\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "subadmin_del")
    async def subadmin_del_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        if not data["sub_admins"]:
            await callback.answer("Hamkor-adminlar yo'q.", show_alert=True)
            return
        buttons = [[InlineKeyboardButton(text=uid, callback_data=f"subadmindel_{uid}")] for uid in data["sub_admins"]]
        await callback.message.answer("O'chirmoqchi bo'lgan hamkor-adminni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("subadmindel_"))
    async def subadmin_delid_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
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
        if bot_type in ("kino_pro",):
            price_line = "💰 Oylik to'lov: tarifga qarab belgilanadi"
        else:
            price = data["type_prices"].get(bot_type, data.get("other_bot_price", DEFAULT_OTHER_BOT_PRICE))
            price_line = f"💰 Oylik to'lov: {price:,} so'm/oy"
        trial_cfg = get_trial_config(bot_type)
        if trial_cfg.get("enabled", True):
            trial_line = f"🎁 Bepul sinov muddati: {trial_cfg.get('days', TRIAL_DAYS)} kun"
        else:
            trial_line = "💰 Bepul sinov yo'q — bot yaratilgach darhol to'lov talab qilinadi"
        if bot_type == "kino_pro":
            create_price_line = f"💵 Yaratish narxi: {cheapest_tariff_price():,} so'm"
        else:
            create_price_line = "💵 Yaratish narxi: 0 so'm"
        return (
            f"{BOT_TYPES[bot_type]}\n\n"
            f"<blockquote expandable>{desc}</blockquote>\n\n"
            f"{create_price_line}\n"
            f"{price_line}\n"
            f"{trial_line}"
        )

    def type_detail_kb(bot_type: str):
        buttons = []
        if bot_type in ("kino_pro",):
            buttons.append([InlineKeyboardButton(text="💳 Tariflar ro'yxati", callback_data=f"tariffpreview_{bot_type}")])
        if bot_type == "kino_pro":
            buttons.append([InlineKeyboardButton(text=f"✅ Bot yaratish — {cheapest_tariff_price():,} so'm", callback_data=f"createbot_{bot_type}")])
        else:
            buttons.append([InlineKeyboardButton(text="✅ Bot yaratish — Bepul", callback_data=f"createbot_{bot_type}")])
        buttons.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="backtotypes")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def tariff_preview_text(bot_type: str) -> str:
        cards = "\n\n".join(tariff_card_text(tid, t) for tid, t in tariffs_for(bot_type).items())
        return f"{BOT_TYPES[bot_type]} — Tariflar\n\n{cards}"

    def tariff_preview_kb(bot_type: str):
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Orqaga", callback_data=f"backtotype_{bot_type}")]])

    @dp.message(Command("newbot"))
    @dp.message(F.text == "🤖 Bot yaratish")
    async def newbot_start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(T("choose_type"), reply_markup=types_kb())

    @dp.callback_query(F.data == "backtotypes")
    async def back_to_types_cb(callback: CallbackQuery):
        await callback.message.edit_text(T("choose_type"), reply_markup=types_kb())
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

    def low_balance_kb(bot_type: str):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Hisobni to'ldirish", callback_data="lowbal_topup")],
            [InlineKeyboardButton(text="💳 Tariflar", callback_data=f"tariffpreview_{bot_type}")],
            [InlineKeyboardButton(text="⬅️ Shablonlar", callback_data="backtotypes")],
        ])

    async def show_low_balance(callback: CallbackQuery, bot_type: str, price: int, balance: int):
        """Bot yaratayotganda pul yetmasa — «Hisobingizda mablag' yetarli emas» xabari + tugmalar."""
        text = T(
            "low_balance",
            template=BOT_TYPES.get(bot_type, bot_type),
            price=_som(price),
            balance=_som(balance),
            missing=_som(max(0, price - balance)),
        )
        kb = low_balance_kb(bot_type)
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=kb)
        await callback.answer()

    @dp.callback_query(F.data == "lowbal_topup")
    async def lowbal_topup_cb(callback: CallbackQuery, state: FSMContext):
        await callback.message.answer(T("topup_prompt", min=f"{MIN_TOPUP:,}", max=f"{MAX_TOPUP:,}"))
        await state.set_state(TopUpFlow.waiting_amount)
        await callback.answer()

    @dp.callback_query(F.data.startswith("createbot_"))
    async def createbot_cb(callback: CallbackQuery, state: FSMContext):
        bot_type = callback.data.split("_", 1)[1]
        if bot_type == "kino_pro" and not is_full_admin(callback.from_user.id):
            price = cheapest_tariff_price(bot_type)
            balance = data["user_balances"].get(str(callback.from_user.id), 0)
            if balance < price:
                await state.clear()
                await show_low_balance(callback, bot_type, price, balance)
                return
        await state.update_data(bot_type=bot_type)
        await state.set_state(NewBotFlow.waiting_token)
        await callback.message.edit_text(T("token_prompt", bot_type=BOT_TYPES[bot_type]))
        await callback.answer()

    async def finalize_bot_creation(token: str, bot_name: str, bot_type: str, uid: int, tariff_id):
        return await create_bot_record(token, bot_name, bot_type, uid, tariff_id)

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

        if bot_type not in ("kino_pro",):
            # Kino'dan boshqa botlar — tarifsiz, yagona narx bilan yaratiladi
            info = await finalize_bot_creation(token, me.first_name, bot_type, message.from_user.id, None)
            tariff = get_bot_tariff(info)
            price_note = f"💰 Oylik narx: {tariff['price']:,} so'm/oy\n"
            trial_cfg = get_trial_config(bot_type)
            if info.get("skip_trial"):
                trial_note = "💰 Bu sizning birinchi bepul botingiz emas — foydalanish uchun darhol to'lov qiling.\n"
            elif trial_cfg.get("enabled", True):
                trial_note = f"🎁 {trial_cfg.get('days', TRIAL_DAYS)} kunlik bepul sinov boshlandi!\n"
            else:
                trial_note = "💰 Bu bot turi uchun sinov yo'q — foydalanish uchun darhol to'lov qiling.\n"
            go_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤖 Botga o'tish", url=f"https://t.me/{me.username}")]]) if me.username else None
            await message.answer(
                f"✅ {BOT_TYPES[bot_type]} ishga tushdi: <b>{me.first_name}</b>\n\n"
                f"{price_note}"
                f"{trial_note}"
                "Majburiy obuna qo'shish uchun o'sha botga /channels yozing.",
                reply_markup=go_kb,
            )
            await state.clear()
            return

        await state.update_data(token=token, bot_name=me.first_name, bot_username=me.username)
        await state.set_state(NewBotFlow.waiting_tariff)
        await message.answer(
            f"✅ Bot topildi: <b>{me.first_name}</b>\n\n{BOT_TYPES[bot_type]} uchun tarifni tanlang:",
            reply_markup=tariff_kb(bot_type=bot_type),
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

        creation_charge = 0
        if bot_type == "kino_pro" and not is_full_admin(callback.from_user.id):
            creation_charge = cheapest_tariff_price()
            balance = data["user_balances"].get(str(callback.from_user.id), 0)
            if balance < creation_charge:
                await state.clear()
                await show_low_balance(callback, bot_type, creation_charge, balance)
                return
            data["user_balances"][str(callback.from_user.id)] = balance - creation_charge

        info = await finalize_bot_creation(token, bot_name, bot_type, callback.from_user.id, tariff_id)

        tariff = get_tariff(tariff_id, bot_type)
        trial_cfg = get_trial_config(bot_type)
        if info.get("skip_trial"):
            trial_note = "💰 Bu sizning birinchi bepul botingiz emas — foydalanish uchun darhol to'lov qiling.\n"
        elif trial_cfg.get("enabled", True):
            trial_note = f"🎁 {trial_cfg.get('days', TRIAL_DAYS)} kunlik bepul sinov boshlandi!\n"
        else:
            trial_note = "💰 Bu bot turi uchun sinov yo'q — foydalanish uchun darhol to'lov qiling.\n"
        go_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤖 Botga o'tish", url=f"https://t.me/{bot_username}")]]) if bot_username else None
        charge_note = f"💵 Yaratish uchun {creation_charge:,} so'm balansingizdan yechildi.\n" if creation_charge else ""
        await callback.message.edit_text(
            f"✅ {BOT_TYPES[bot_type]} ishga tushdi: <b>{bot_name}</b>\n\n"
            f"{charge_note}"
            f"💠 Tarif: {tariff['name']} — {tariff['price']:,} so'm/oy ({tariff_limit_text(tariff)})\n"
            f"{trial_note}"
            "Majburiy obuna qo'shish uchun o'sha botga /channels yozing.",
            reply_markup=go_kb,
        )
        await state.clear()
        await callback.answer()

    @dp.message(Command("prices"))
    @dp.message(F.text == "💵 Tariflar")
    async def prices_panel(message: Message):
        if not is_full_admin(message.from_user.id):
            return
        buttons = [
            [InlineKeyboardButton(
                text=f"💎 {t['name']} — {t['price']:,} so'm/oy ({tariff_limit_text(t)})",
                callback_data=f"edittariff_p_{tid}",
            )]
            for tid, t in data["pro_tariffs"].items()
        ]
        buttons.append([InlineKeyboardButton(text="➕ Pro tarif qo'shish", callback_data="addtariff_p")])
        buttons.append([InlineKeyboardButton(text="➖ Pro tarif o'chirish", callback_data="deltariff_p")])
        for bt, bt_name in BOT_TYPES.items():
            if bt in ("kino_pro",):
                continue
            price = data["type_prices"].get(bt, data.get("other_bot_price", DEFAULT_OTHER_BOT_PRICE))
            buttons.append([InlineKeyboardButton(
                text=f"{bt_name} — {price:,} so'm/oy",
                callback_data=f"edittypeprice_{bt}",
            )])
        await message.answer(
            "💰 <b>Tariflarni boshqarish</b>\n\n"
            "Narxini o'zgartirish uchun tanlang:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @dp.callback_query(F.data.startswith("addtariff_"))
    async def addtariff_cb(callback: CallbackQuery, state: FSMContext):
        if not is_full_admin(callback.from_user.id):
            return
        pool = callback.data.split("_", 1)[1]
        await state.update_data(tariff_pool=pool)
        pool_label = "🎬 Kino BOT"
        await callback.message.answer(f"{pool_label} uchun yangi tarif nomini kiriting (masalan: 🚀 Mega):")
        await state.set_state(NewTariffAdd.waiting_name)
        await callback.answer()

    @dp.message(NewTariffAdd.waiting_name)
    async def addtariff_name(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.update_data(new_tariff_name=message.text.strip())
        await message.answer("Oylik narxini kiriting (so'm, faqat raqam):")
        await state.set_state(NewTariffAdd.waiting_price)

    @dp.message(NewTariffAdd.waiting_price)
    async def addtariff_price(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
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
        if not is_full_admin(message.from_user.id):
            return
        try:
            limit = int(message.text.strip().replace(" ", ""))
            if limit < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ 0 yoki musbat butun raqam kiriting.")
            return
        fsm_data = await state.get_data()
        pool_key = "pro_tariffs"
        pool = data[pool_key]
        new_id = str(max((int(k) for k in pool.keys() if k.isdigit()), default=0) + 1)
        pool[new_id] = {
            "name": fsm_data["new_tariff_name"],
            "price": fsm_data["new_tariff_price"],
            "daily_limit": None if limit == 0 else limit,
        }
        save_data()
        await message.answer(f"✅ Yangi tarif qo'shildi: {fsm_data['new_tariff_name']} — {fsm_data['new_tariff_price']:,} so'm/oy")
        await state.clear()

    @dp.callback_query(F.data.startswith("deltariff_"))
    async def deltariff_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        pool_code = callback.data.split("_", 1)[1]
        pool_key = "pro_tariffs"
        pool = data[pool_key]
        if len(pool) <= 1:
            await callback.answer("Kamida bitta tarif qolishi kerak.", show_alert=True)
            return
        buttons = [
            [InlineKeyboardButton(text=f"{t['name']} — {t['price']:,} so'm/oy", callback_data=f"deltariffid_{pool_code}_{tid}")]
            for tid, t in pool.items()
        ]
        await callback.message.answer("O'chirmoqchi bo'lgan tarifni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("deltariffid_"))
    async def deltariffid_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        _, pool_code, tid = callback.data.split("_", 2)
        pool_key = "pro_tariffs"
        pool = data[pool_key]
        if len(pool) <= 1:
            await callback.answer("Kamida bitta tarif qolishi kerak.", show_alert=True)
            return
        removed = pool.pop(tid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['name']}")
        await callback.answer()

    @dp.callback_query(F.data.startswith("edittypeprice_"))
    async def edittypeprice_cb(callback: CallbackQuery, state: FSMContext):
        if not is_full_admin(callback.from_user.id):
            return
        bt = callback.data.split("_", 1)[1]
        current = data["type_prices"].get(bt, data.get("other_bot_price", DEFAULT_OTHER_BOT_PRICE))
        await callback.message.answer(
            f"{BOT_TYPES.get(bt, bt)} uchun yangi oylik narxni kiriting (so'm, faqat raqam):\n\n"
            f"Joriy narx: {current:,} so'm/oy"
        )
        await state.set_state(EditPrice.waiting_amount)
        await state.update_data(edit_tariff_id=None, edit_type_price=bt)
        await callback.answer()

    @dp.message(F.text == "⭐ Stars kursi")
    async def stars_rate_start(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        rate = data.get("stars_rate", 250)
        await message.answer(
            f"⭐ <b>Stars kursi</b>\n\nHozirgi kurs: 1 ⭐ = {rate:,} so'm\n\n"
            "Yangi kursni kiriting (so'mda, faqat raqam):"
        )
        await state.set_state(EditStarsRate.waiting_rate)

    @dp.message(EditStarsRate.waiting_rate)
    async def stars_rate_save(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
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
        if not is_full_admin(callback.from_user.id):
            return
        _, pool_code, tid = callback.data.split("_", 2)
        pool_key = "pro_tariffs"
        t = data[pool_key][tid]
        await state.update_data(edit_tariff_id=tid, edit_tariff_pool=pool_key)
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
        if state_data.get("edit_type_price"):
            bt = state_data["edit_type_price"]
            data["type_prices"][bt] = amount
            save_data()
            await message.answer(f"✅ {BOT_TYPES.get(bt, bt)} narxi endi {amount:,} so'm/oy.")
            await state.clear()
            return
        tid = state_data.get("edit_tariff_id")
        pool_key = "pro_tariffs"
        if tid and tid in data[pool_key]:
            data[pool_key][tid]["price"] = amount
            save_data()
            await message.answer(f"✅ {data[pool_key][tid]['name']} tarifi endi {amount:,} so'm/oy.")
        await state.clear()

    @dp.message(Command("globalbuttons"))
    async def global_buttons_panel(message: Message):
        if not is_full_admin(message.from_user.id):
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
        if not is_full_admin(callback.from_user.id):
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
        if not is_full_admin(callback.from_user.id):
            return
        if not data["global_buttons"]:
            await callback.message.answer("Hozircha global tugmalar yo'q.")
        else:
            text = "📋 <b>Global tugmalar:</b>\n\n" + "\n".join(f"• {b['label']}" for b in data["global_buttons"])
            await callback.message.answer(text)
        await callback.answer()

    @dp.callback_query(F.data == "gb_del")
    async def gb_del_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
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
        if not is_full_admin(callback.from_user.id):
            return
        idx = int(callback.data.split("_", 1)[1])
        if 0 <= idx < len(data["global_buttons"]):
            removed = data["global_buttons"].pop(idx)
            save_data()
            await callback.message.answer(f"🗑 O'chirildi: {removed['label']}")
        await callback.answer()

    def format_remaining(td: timedelta) -> str:
        total = int(td.total_seconds())
        if total < 0:
            total = 0
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days} kun")
        if hours or days:
            parts.append(f"{hours} soat")
        if minutes or hours or days:
            parts.append(f"{minutes} daqiqa")
        parts.append(f"{seconds} soniya")
        return " ".join(parts)

    def bot_remaining_text(info: dict) -> str:
        """Bot muddatidan qancha qolgani (qisqa matn)."""
        now = datetime.now()
        if is_full_admin(info.get("admin_id")):
            return "♾ Umrbod"
        paid_until = info.get("paid_until")
        if paid_until:
            dt = datetime.fromisoformat(paid_until)
            return format_remaining(dt - now) if dt > now else "Muddati tugagan"
        trial_cfg = get_trial_config(info.get("type"))
        if info.get("skip_trial") or not trial_cfg.get("enabled", True):
            return "To'lov talab qilinadi"
        dt = datetime.fromisoformat(info["created_at"]) + timedelta(days=trial_cfg.get("days", TRIAL_DAYS))
        return ("Sinov: " + format_remaining(dt - now)) if dt > now else "Sinov tugagan"

    def mybots_list_view(uid: int):
        """Botlarim ro'yxati: (matn, tugmalar)."""
        items = [i for i in data["bots"].values() if uid in i.get("admin_ids", [i["admin_id"]])]
        if not items:
            text = "🤖 <b>Botlarim</b>\n\nHali botlaringiz yo'q.\nPastdagi tugma orqali birinchi botingizni yarating 👇"
        else:
            need = sum(1 for i in items if not is_active(i))
            text = f"🤖 <b>Botlarim ({len(items)})</b>\n\n"
            if need:
                text += f"⚠️ {need} ta bot e'tibor talab qiladi. Tanlab ko'ring:"
            else:
                text += "✅ Hammasi joyida. Boshqarish uchun botni tanlang:"
        rows = [
            [InlineKeyboardButton(text=f"{'🟢' if is_active(i) else '🔴'} {i['name']}", callback_data=f"mb_{i['id']}")]
            for i in items
        ]
        rows.append([InlineKeyboardButton(text="➕ Yangi bot yaratish", callback_data="mb_new")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    def mybot_detail_view(info: dict):
        """Bitta bot kartasi: (matn, tugmalar)."""
        tariff = get_bot_tariff(info)
        active = is_active(info)
        owner_full = is_full_admin(info.get("admin_id"))
        limit = tariff.get("daily_limit")
        today = datetime.now().strftime("%Y-%m-%d")
        usage = info.get("daily_usage", {})
        used = len(usage.get("users", [])) if usage.get("date") == today else 0
        if limit:
            pct = min(100, used * 100 // limit)
            dot = "🟢" if pct < 70 else ("🟡" if pct < 90 else "🔴")
            usage_line = f"📊 Bugun: {used:,} / {limit:,} {dot} {pct}%"
        else:
            usage_line = f"📊 Bugun: {used:,} / ♾"
        price_line = "💰 Narxi: umrbod bepul" if owner_full else f"💰 Narxi: {tariff['price']:,} so'm/oy"
        users_n = len(info.get("users", []))
        s2 = "✅" if users_n >= 1 else "⬜"
        s3 = "✅" if users_n >= 10 else "⬜"
        done = 1 + (users_n >= 1) + (users_n >= 10)
        text = (
            f"{BOT_TYPES.get(info['type'], info['type'])}\n"
            f"🤖 <b>{html_escape(info['name'])}</b>\n"
            f"{'🟢 Holati: <b>Faol</b>' if active else '🔴 Holati: <b>To' + chr(39) + 'xtatilgan</b>'}\n\n"
            f"<blockquote>🔷 Tarif: {html_escape(tariff['name'])}\n"
            f"{usage_line}\n"
            f"{price_line}\n"
            f"⏳ Qoldi: {bot_remaining_text(info)}</blockquote>\n\n"
            f"🚀 <b>Ishga tushirish ({done}/3)</b>\n"
            f"✅ 1. Bot yaratildi va ulandi\n"
            f"{s2} 2. Botga kirib /start bosing — siz uning admini bo'lasiz\n"
            f"{s3} 3. Botni ulashing — 10 ta foydalanuvchi ({min(users_n, 10)}/10)"
        )
        rows = []
        if info["type"] in ("kino_pro",):
            purl = get_kinopanel_url(info.get("id"))
            if purl:
                rows.append([InlineKeyboardButton(text="🌐 Boshqaruv (Web Studio)", web_app=WebAppInfo(url=purl))])
        surl = get_botstats_url(info.get("id"))
        if surl:
            rows.append([InlineKeyboardButton(text="📈 Veb statistika", web_app=WebAppInfo(url=surl))])
        if not owner_full:
            row = []
            if info["type"] in ("kino_pro",):
                row.append(InlineKeyboardButton(text="🔄 Tarif", callback_data=f"changetariff_{info['id']}"))
            row.append(InlineKeyboardButton(text="💰 To'lov", callback_data=f"paynow_{info['id']}"))
            rows.append(row)
        rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="mb_list")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def mb_edit(callback: CallbackQuery, text: str, kb, toast: str = None):
        """Yangi xabar tashlamaydi — bitta xabarning o'zini tahrirlaydi."""
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        await callback.answer(toast) if toast else await callback.answer()

    @dp.message(Command("mybots"))
    @dp.message(F.text == "📁 Botlarim")
    async def mybots(message: Message):
        text, kb = mybots_list_view(message.from_user.id)
        await message.answer(text, reply_markup=kb)

    @dp.callback_query(F.data == "mb_list")
    async def mb_list_cb(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        text, kb = mybots_list_view(callback.from_user.id)
        await mb_edit(callback, text, kb)

    @dp.callback_query(F.data == "mb_new")
    async def mb_new_cb(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        rows = list(types_kb().inline_keyboard) + [[InlineKeyboardButton(text="◀️ Orqaga", callback_data="mb_list")]]
        await mb_edit(callback, T("choose_type"), InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.callback_query(F.data.regexp(r"^mb_\d+$"))
    async def mb_detail_cb(callback: CallbackQuery):
        bot_id = int(callback.data.split("_", 1)[1])
        token, info = find_bot_by_id(bot_id)
        if not info or callback.from_user.id not in info.get("admin_ids", [info["admin_id"]]):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        text, kb = mybot_detail_view(info)
        await mb_edit(callback, text, kb)

    # ---------- ✨ Animatsiya (animatsiyali emojilar kutubxonasi) ----------
    def anim_panel_text() -> str:
        on = bool(data.get("anim_enabled"))
        return (
            "✨ <b>Animatsiya</b>\n\n"
            f"📚 Saqlangan emojilar: <b>{len(data.get('anim_emojis', {}))}</b>\n"
            f"🔘 Tugma va matnlarda: {'✅ yoqilgan' if on else '❌ o`chiq'}\n\n"
            "➕ Animatsiyali (Premium) emojilar bor istalgan matnni yoki emoji to'plami havolasini "
            "yuboring — bot har bir emojini alohida chiqaradi va saqlaydi.\n\n"
            "Yoqilganda Bot Creator'dagi xabar matnlari va inline tugmalardagi oddiy emoji shu animatsiyali emoji bilan almashadi."
        ).replace("`", "'")

    def anim_panel_kb():
        on = bool(data.get("anim_enabled"))
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Emoji qo'shish", callback_data="anim_add")],
            [InlineKeyboardButton(text="📋 Emojilar ro'yxati", callback_data="anim_list")],
            [InlineKeyboardButton(text="🔴 O'chirish" if on else "🟢 Yoqish", callback_data="anim_toggle")],
            [InlineKeyboardButton(text="🧹 Animatsiyasizlarini olib tashlash", callback_data="anim_prune")],
            [InlineKeyboardButton(text="🗑 Hammasini tozalash", callback_data="anim_clear")],
        ])

    async def anim_send_items(target: Message, items: list):
        """Har bir emojini alohida qatorda ko'rsatadi: (emoji, id[, izoh])."""
        for i in range(0, len(items), 40):
            part = items[i:i + 40]
            rich = "\n".join(
                f'<tg-emoji emoji-id="{it[1]}">{it[0]}</tg-emoji>  <code>{it[1]}</code>' + (f"  {it[2]}" if len(it) > 2 else "")
                for it in part)
            plain = "\n".join(
                f"{it[0]}  <code>{it[1]}</code>" + (f"  {it[2]}" if len(it) > 2 else "")
                for it in part)
            try:
                await target.answer(rich)
            except TelegramBadRequest:
                await target.answer(plain)

    @dp.message(F.text == "✨ Animatsiya")
    async def anim_panel(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.clear()
        await message.answer(anim_panel_text(), reply_markup=anim_panel_kb())

    @dp.callback_query(F.data == "anim_add")
    async def anim_add_cb(callback: CallbackQuery, state: FSMContext):
        if not is_full_admin(callback.from_user.id):
            return
        await state.set_state(AnimEmojiAdd.waiting_text)
        await callback.message.answer(
            "✨ <b>Animatsiyali</b> (harakatlanuvchi) Premium emojilar bor istalgan matnni yuboring.\n\n"
            "Yoki emoji to'plami havolasini yuboring: <code>t.me/addemoji/...</code>\n\n"
            "ℹ️ Bot har bir emojining animatsiyali yoki oddiy ekanini tekshiradi. "
            "Oddiy (harakatsiz) emojilar saqlanmaydi.\n\n"
            "Bekor qilish: /bekor"
        )
        await callback.answer()

    @dp.message(AnimEmojiAdd.waiting_text)
    async def anim_add_process(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        text = message.text or message.caption or ""
        if text.strip().lower() in ("/bekor", "/cancel"):
            await state.clear()
            await message.answer("❌ Bekor qilindi.")
            return
        cands = []      # (base, id, override)
        flags = {}
        for ent in (message.entities or message.caption_entities or []):
            if ent.type == "custom_emoji" and ent.custom_emoji_id:
                base = _anim_norm(ent.extract_from(text))
                if base:
                    cands.append((base, ent.custom_emoji_id, True))
        for name in re.findall(r"(?:t\.me|telegram\.me)/addemoji/([A-Za-z0-9_]+)", text):
            try:
                st = await message.bot.get_sticker_set(name)
            except Exception:
                await message.answer(f"❌ To'plam topilmadi: {html_escape(name)}")
                continue
            for sk in st.stickers:
                base = _anim_norm(sk.emoji or "")
                if sk.custom_emoji_id and base:
                    cands.append((base, sk.custom_emoji_id, False))
                    flags[sk.custom_emoji_id] = bool(getattr(sk, "is_animated", False) or getattr(sk, "is_video", False))
        if not cands:
            await message.answer(
                "❌ Premium emoji topilmadi.\n\n"
                "Premium emoji bor matn yuboring yoki <code>t.me/addemoji/...</code> havolasini kiriting. "
                "Bekor qilish: /bekor"
            )
            return
        unknown = [i for _, i, _o in cands if i not in flags]
        flags.update(await anim_fetch_flags(message.bot, unknown))
        lib = data.setdefault("anim_emojis", {})
        saved, skipped = [], []
        for base, eid, override in cands:
            anim = flags.get(eid)          # True / False / None (aniqlanmadi)
            if anim is False:
                skipped.append((base, eid, "🖼 oddiy"))
                continue
            if override:
                lib[base] = eid
            else:
                lib.setdefault(base, eid)
            saved.append((base, eid, "🎞 animatsiyali" if anim else "❓ turi noma'lum"))
        if saved:
            save_data()
        await state.clear()
        if saved:
            await message.answer(f"✅ Saqlandi: <b>{len(saved)}</b> ta emoji. Har biri alohida:")
            await anim_send_items(message, saved[:80])
        if skipped:
            await message.answer(
                f"⚠️ <b>{len(skipped)}</b> ta emoji animatsiyasiz (oddiy, harakatsiz) — saqlanmadi:")
            await anim_send_items(message, skipped[:40])
            if not saved:
                await message.answer(
                    "Animatsiyali emoji topish uchun Telegramda emoji paneli → Emoji bo'limidan "
                    "harakatlanuvchi to'plamni tanlang va uning havolasini (<code>t.me/addemoji/...</code>) yuboring."
                )
        await message.answer(anim_panel_text(), reply_markup=anim_panel_kb())

    @dp.callback_query(F.data == "anim_list")
    async def anim_list_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        lib = data.get("anim_emojis", {})
        if not lib:
            await callback.answer("Hali emoji qo'shilmagan.", show_alert=True)
            return
        flags = await anim_fetch_flags(callback.bot, list(lib.values()))
        label = lambda eid: "🎞 animatsiyali" if flags.get(eid) else ("🖼 oddiy" if eid in flags else "❓")
        await callback.message.answer(f"📚 Saqlangan emojilar: <b>{len(lib)}</b>")
        await anim_send_items(callback.message, [(b, i, label(i)) for b, i in lib.items()])
        await callback.answer()

    @dp.callback_query(F.data == "anim_prune")
    async def anim_prune_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        lib = data.get("anim_emojis", {})
        if not lib:
            await callback.answer("Emoji yo'q.", show_alert=True)
            return
        flags = await anim_fetch_flags(callback.bot, list(lib.values()))
        removed = [b for b, i in list(lib.items()) if flags.get(i) is False]
        for b in removed:
            lib.pop(b, None)
        if not lib:
            data["anim_enabled"] = False
        save_data()
        await callback.message.edit_text(anim_panel_text(), reply_markup=anim_panel_kb())
        await callback.answer(f"🧹 Olib tashlandi: {len(removed)} ta oddiy emoji.", show_alert=True)

    # ---------- 🎨 Ranglar (inline tugmalarga rang berish) ----------
    CLR_EMOJI = {"primary": "🔵", "success": "🟢", "danger": "🔴"}
    CLR_CODES = {"p": "primary", "s": "success", "d": "danger"}
    CLR_PER_PAGE = 8

    def clr_find(h: str):
        for t in list(data.get("btn_colors", {})) + list(data.get("btn_seen", [])):
            if btn_hash(t) == h:
                return t
        return None

    def clr_panel_text() -> str:
        return (
            "🎨 <b>Tugma ranglari</b>\n\n"
            f"Rang berilgan tugmalar: <b>{len(data.get('btn_colors', {}))}</b>\n\n"
            "Bot Creator'dagi tugmalarni ko'k 🔵, yashil 🟢 yoki qizil 🔴 qilishingiz mumkin.\n"
            "Tugmalar ro'yxatidan tanlang yoki tugma matnini yozib qo'shing.\n\n"
            "ℹ️ Rang xabar ostidagi (inline) tugmalarda ham, pastdagi doimiy menyu tugmalarida ham ko'rinadi "
            "(Telegram ilovasi yangi bo'lishi kerak). Pastdagi menyu rangi /start bosilganda yangilanadi.\n"
            "💡 Narx yozilgan tugmalarda (masalan «Bot yaratish — 77,770 so'm») narx o'zgarsa ham rang saqlanadi."
        )

    def clr_panel_kb():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Tugmalar ro'yxati", callback_data="clr_pg_0")],
            [InlineKeyboardButton(text="✍️ Matn bo'yicha qo'shish", callback_data="clr_text")],
            [InlineKeyboardButton(text="🎨 Rang berilganlar", callback_data="clr_cfg_0")],
            [InlineKeyboardButton(text="♻️ Hammasini standartga qaytarish", callback_data="clr_reset")],
        ])

    def clr_list_kb(texts: list, page: int, prefix: str):
        pages = max(1, (len(texts) + CLR_PER_PAGE - 1) // CLR_PER_PAGE)
        page = max(0, min(page, pages - 1))
        colors = data.get("btn_colors", {})
        rows = [
            [InlineKeyboardButton(
                text=f"{CLR_EMOJI.get(colors.get(t), '⚪')} {t[:40]}", callback_data=f"clr_pick_{btn_hash(t)}")]
            for t in texts[page * CLR_PER_PAGE:(page + 1) * CLR_PER_PAGE]
        ]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="clr_noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}{page + 1}"))
        rows.append(nav)
        rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="clr_root")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def clr_pick_text(t: str) -> str:
        cur = data.get("btn_colors", {}).get(_btn_key(t))
        cur_txt = {"primary": "🔵 Ko'k", "success": "🟢 Yashil", "danger": "🔴 Qizil"}.get(cur, "⚪ Standart")
        return f"🎨 Tugma: <b>{html_escape(t)}</b>\n\nHozirgi rang: {cur_txt}\n\nYangi rangni tanlang:"

    def clr_pick_kb(t: str):
        h = btn_hash(t)
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔵 Ko'k", callback_data=f"clr_set_{h}_p"),
             InlineKeyboardButton(text="🟢 Yashil", callback_data=f"clr_set_{h}_s")],
            [InlineKeyboardButton(text="🔴 Qizil", callback_data=f"clr_set_{h}_d"),
             InlineKeyboardButton(text="⚪ Standart", callback_data=f"clr_set_{h}_n")],
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="clr_root")],
        ])

    @dp.message(Command("ranglar"))
    @dp.message(F.text == "🎨 Ranglar")
    async def clr_panel(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.clear()
        await message.answer(clr_panel_text(), reply_markup=clr_panel_kb())

    @dp.callback_query(F.data == "clr_root")
    async def clr_root_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        try:
            await callback.message.edit_text(clr_panel_text(), reply_markup=clr_panel_kb())
        except TelegramBadRequest:
            pass
        await callback.answer()

    @dp.callback_query(F.data == "clr_noop")
    async def clr_noop_cb(callback: CallbackQuery):
        await callback.answer()

    @dp.callback_query(F.data.startswith("clr_pg_"))
    async def clr_pg_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        texts = list(data.get("btn_seen", []))
        if not texts:
            await callback.answer("Hali tugma ro'yxati yo'q — botni biroz ishlatib ko'ring yoki matn bo'yicha qo'shing.", show_alert=True)
            return
        page = int(callback.data.rsplit("_", 1)[1])
        try:
            await callback.message.edit_text("📋 Tugmani tanlang:", reply_markup=clr_list_kb(texts, page, "clr_pg_"))
        except TelegramBadRequest:
            pass
        await callback.answer()

    @dp.callback_query(F.data.startswith("clr_cfg_"))
    async def clr_cfg_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        texts = list(data.get("btn_colors", {}))
        if not texts:
            await callback.answer("Hali hech qaysi tugmaga rang berilmagan.", show_alert=True)
            return
        page = int(callback.data.rsplit("_", 1)[1])
        try:
            await callback.message.edit_text("🎨 Rang berilgan tugmalar:", reply_markup=clr_list_kb(texts, page, "clr_cfg_"))
        except TelegramBadRequest:
            pass
        await callback.answer()

    @dp.callback_query(F.data.startswith("clr_pick_"))
    async def clr_pick_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        t = clr_find(callback.data.split("_", 2)[2])
        if not t:
            await callback.answer("Tugma topilmadi.", show_alert=True)
            return
        try:
            await callback.message.edit_text(clr_pick_text(t), reply_markup=clr_pick_kb(t))
        except TelegramBadRequest:
            pass
        await callback.answer()

    @dp.callback_query(F.data.startswith("clr_set_"))
    async def clr_set_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        parts = callback.data.split("_")
        if len(parts) != 4:
            await callback.answer()
            return
        t = clr_find(parts[2])
        if not t:
            await callback.answer("Tugma topilmadi.", show_alert=True)
            return
        colors = data.setdefault("btn_colors", {})
        key = _btn_key(t)
        if parts[3] == "n":
            colors.pop(key, None)
        elif parts[3] in CLR_CODES:
            colors[key] = CLR_CODES[parts[3]]
        else:
            await callback.answer()
            return
        save_data()
        try:
            await callback.message.edit_text(clr_pick_text(t), reply_markup=clr_pick_kb(t))
        except TelegramBadRequest:
            pass
        await callback.answer("Saqlandi!")

    @dp.callback_query(F.data == "clr_reset")
    async def clr_reset_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        data["btn_colors"] = {}
        save_data()
        try:
            await callback.message.edit_text(clr_panel_text(), reply_markup=clr_panel_kb())
        except TelegramBadRequest:
            pass
        await callback.answer("♻️ Hammasi standartga qaytarildi.")

    @dp.callback_query(F.data == "clr_text")
    async def clr_text_cb(callback: CallbackQuery, state: FSMContext):
        if not is_full_admin(callback.from_user.id):
            return
        await state.set_state(ColorTextAdd.waiting_text)
        await callback.message.answer(
            "✍️ Rang bermoqchi bo'lgan tugmaning matnini yuboring — aynan tugmada yozilganidek "
            "(masalan: <code>💎 Tariflar</code>).\n\nBekor qilish: /bekor"
        )
        await callback.answer()

    @dp.message(ColorTextAdd.waiting_text)
    async def clr_text_process(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        t = _btn_key(message.text or "")
        if t.lower() in ("/bekor", "/cancel"):
            await state.clear()
            await message.answer("❌ Bekor qilindi.")
            return
        if not t or len(t) > 60:
            await message.answer("❌ Tugma matni 1–60 belgi bo'lishi kerak. Qaytadan yuboring yoki /bekor")
            return
        seen = data.setdefault("btn_seen", [])
        if t not in seen:
            seen.insert(0, t)
        await state.clear()
        await message.answer(clr_pick_text(t), reply_markup=clr_pick_kb(t))

    # ---------- 📝 Matnlar (Bot Creator xabar matnlarini tahrirlash) ----------
    TXT_PER_PAGE = 8

    def txt_root_text() -> str:
        edited = sum(1 for k in TEXT_REGISTRY if text_is_edited(k))
        return (
            "📝 <b>Matnlar</b>\n\n"
            f"Jami: <b>{len(TEXT_REGISTRY)}</b> ta matn · tahrirlangan: <b>{edited}</b> ta\n\n"
            "Bot Creator xabarlarini shu yerdan o'zgartirishingiz mumkin. Matnni tanlang 👇\n\n"
            "💾 Tahrirlangan matnlar bazada saqlanadi — kodga yangi qism qo'shilganda ham o'chib ketmaydi.\n"
            "🔒 Faqat adminlar tahrirlay oladi. O'zgarishlar faqat Bot Creator'ga tegishli."
        )

    def txt_list_kb(page: int):
        keys = list(TEXT_REGISTRY)
        pages = max(1, (len(keys) + TXT_PER_PAGE - 1) // TXT_PER_PAGE)
        page = max(0, min(page, pages - 1))
        rows = [
            [InlineKeyboardButton(
                text=("✏️ " if text_is_edited(k) else "📄 ") + TEXT_REGISTRY[k]["title"][:50],
                callback_data=f"txt_o_{k}")]
            for k in keys[page * TXT_PER_PAGE:(page + 1) * TXT_PER_PAGE]
        ]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"txt_pg_{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="txt_noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"txt_pg_{page + 1}"))
        rows.append(nav)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def txt_vars_lines(key: str) -> str:
        vs = TEXT_REGISTRY[key]["vars"]
        if not vs:
            return "—"
        return "\n".join(f"• <code>{{{n}}}</code> — {html_escape(d)}" for n, d in vs.items())

    def txt_detail_text(key: str) -> str:
        e = TEXT_REGISTRY[key]
        cur = html_escape(text_template(key))
        if len(cur) > 2500:
            cur = cur[:2500] + "…"
        status = "✏️ tahrirlangan" if text_is_edited(key) else "📄 standart"
        return (
            f"📝 <b>{html_escape(e['title'])}</b>  ({status})\n\n"
            f"<b>O'zgaruvchilar:</b>\n{txt_vars_lines(key)}\n\n"
            f"<b>Joriy matn:</b>\n<pre>{cur}</pre>"
        )

    def txt_detail_kb(key: str):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Tahrirlash", callback_data=f"txt_e_{key}"),
             InlineKeyboardButton(text="👁 Ko'rish", callback_data=f"txt_v_{key}")],
            [InlineKeyboardButton(text="♻️ Asl holiga qaytarish", callback_data=f"txt_r_{key}")],
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="txt_pg_0")],
        ])

    async def txt_guard(callback: CallbackQuery, prefix: str):
        """Faqat adminlar. Kalit to'g'ri bo'lsa qaytaradi, aks holda None."""
        if not is_full_admin(callback.from_user.id):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return None
        key = callback.data[len(prefix):]
        if key not in TEXT_REGISTRY:
            await callback.answer("Matn topilmadi.", show_alert=True)
            return None
        return key

    @dp.message(Command("texts"))
    @dp.message(F.text == "📝 Matnlar")
    async def txt_panel(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        await state.clear()
        await message.answer(txt_root_text(), reply_markup=txt_list_kb(0))

    @dp.callback_query(F.data == "txt_noop")
    async def txt_noop_cb(callback: CallbackQuery):
        await callback.answer()

    @dp.callback_query(F.data.startswith("txt_pg_"))
    async def txt_pg_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        try:
            page = int(callback.data.rsplit("_", 1)[1])
        except ValueError:
            page = 0
        try:
            await callback.message.edit_text(txt_root_text(), reply_markup=txt_list_kb(page))
        except TelegramBadRequest:
            pass
        await callback.answer()

    @dp.callback_query(F.data.startswith("txt_o_"))
    async def txt_open_cb(callback: CallbackQuery):
        key = await txt_guard(callback, "txt_o_")
        if key is None:
            return
        try:
            await callback.message.edit_text(txt_detail_text(key), reply_markup=txt_detail_kb(key))
        except TelegramBadRequest:
            pass
        await callback.answer()

    @dp.callback_query(F.data.startswith("txt_v_"))
    async def txt_preview_cb(callback: CallbackQuery):
        key = await txt_guard(callback, "txt_v_")
        if key is None:
            return
        sample = TEXT_REGISTRY[key].get("sample", {})
        try:
            await callback.message.answer("👁 <b>Ko'rinishi:</b>\n\n" + T(key, **sample))
        except TelegramBadRequest as e:
            await callback.message.answer(f"❌ Matnni yuborib bo'lmadi: {html_escape(str(e))}")
        await callback.answer()

    @dp.callback_query(F.data.startswith("txt_r_"))
    async def txt_reset_cb(callback: CallbackQuery):
        key = await txt_guard(callback, "txt_r_")
        if key is None:
            return
        text_reset(key)
        try:
            await callback.message.edit_text(txt_detail_text(key), reply_markup=txt_detail_kb(key))
        except TelegramBadRequest:
            pass
        await callback.answer("♻️ Asl holiga qaytarildi.")

    @dp.callback_query(F.data.startswith("txt_e_"))
    async def txt_edit_cb(callback: CallbackQuery, state: FSMContext):
        key = await txt_guard(callback, "txt_e_")
        if key is None:
            return
        await state.set_state(TextEdit.waiting)
        await state.update_data(txt_key=key)
        await callback.message.answer(
            f"✍️ <b>{html_escape(TEXT_REGISTRY[key]['title'])}</b> uchun yangi matnni yuboring.\n\n"
            f"<b>O'zgaruvchilar</b> (matn ichida shunday yozing):\n{txt_vars_lines(key)}\n\n"
            "🎨 Format: Telegram'ning o'zidagi qalin/kursiv/kod belgilash yoki HTML teglar "
            "(<code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, <code>&lt;code&gt;</code>, "
            "<code>&lt;blockquote&gt;</code>) ishlaydi.\n\n"
            "Bekor qilish: /bekor"
        )
        await callback.answer()

    @dp.message(TextEdit.waiting)
    async def txt_edit_process(message: Message, state: FSMContext):
        if not is_full_admin(message.from_user.id):
            return
        raw = (message.text or "").strip()
        if raw.lower() in ("/bekor", "/cancel"):
            await state.clear()
            await message.answer("❌ Bekor qilindi.")
            return
        if not message.text:
            await message.answer("❌ Faqat matn yuboring. Bekor qilish: /bekor")
            return
        st = await state.get_data()
        key = st.get("txt_key")
        if key not in TEXT_REGISTRY:
            await state.clear()
            await message.answer("❌ Matn topilmadi. Qaytadan /texts bosing.")
            return
        new_text = message.html_text if message.entities else message.text
        ok, err = text_validate(key, new_text)
        if not ok:
            await message.answer(f"❌ {html_escape(err)}\n\nQaytadan yuboring yoki /bekor")
            return
        text_save(key, new_text)
        await state.clear()
        await message.answer(
            "✅ <b>Saqlandi!</b> Matn darhol ishlaydi va kod yangilanganda ham o'chmaydi.\n\n" + txt_detail_text(key),
            reply_markup=txt_detail_kb(key),
        )

    @dp.callback_query(F.data == "anim_toggle")
    async def anim_toggle_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        if not data.get("anim_enabled") and not data.get("anim_emojis"):
            await callback.answer("Avval emoji qo'shing.", show_alert=True)
            return
        data["anim_enabled"] = not data.get("anim_enabled")
        save_data()
        await callback.message.edit_text(anim_panel_text(), reply_markup=anim_panel_kb())
        await callback.answer("Saqlandi!")

    @dp.callback_query(F.data == "anim_clear")
    async def anim_clear_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        data["anim_emojis"] = {}
        data["anim_enabled"] = False
        save_data()
        await callback.message.edit_text(anim_panel_text(), reply_markup=anim_panel_kb())
        await callback.answer("🗑 Tozalandi.")

    # ---------- Sinov / Pullik sozlamalari (har bir bot turi uchun) ----------
    def trial_settings_kb():
        buttons = []
        for bt, name in BOT_TYPES.items():
            cfg = get_trial_config(bt)
            status = (
                f"🎁 Bepul ({cfg.get('days', TRIAL_DAYS)} kun)"
                if cfg.get("enabled", True)
                else "💰 Pullik (sinovsiz)"
            )
            buttons.append([InlineKeyboardButton(text=f"{name}: {status}", callback_data=f"trialtoggle_{bt}")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @dp.message(F.text == "🎁 Sinov/Pullik")
    async def trial_settings_panel(message: Message):
        if not is_full_admin(message.from_user.id):
            return
        await message.answer(
            "🎁💰 <b>Sinov / Pullik sozlamalari</b>\n\n"
            "Har bir bot turi uchun holatni tanlang:\n"
            "🎁 Bepul — yangi bot shu turdan yaratilganda avval bepul sinov muddati beriladi.\n"
            "💰 Pullik — sinov yo'q, bot yaratilgach darhol to'lov talab qilinadi (narxi shu turning joriy narxi).\n\n"
            "Almashtirish uchun tugmani bosing:",
            reply_markup=trial_settings_kb(),
        )

    @dp.callback_query(F.data.startswith("trialtoggle_"))
    async def trial_toggle_cb(callback: CallbackQuery):
        if not is_full_admin(callback.from_user.id):
            return
        bt = callback.data.split("_", 1)[1]
        cfg = data["bot_type_trial"].setdefault(bt, {"enabled": True, "days": TRIAL_DAYS})
        cfg["enabled"] = not cfg.get("enabled", True)
        save_data()
        await callback.message.edit_reply_markup(reply_markup=trial_settings_kb())
        new_status = "🎁 Bepul sinov" if cfg["enabled"] else "💰 Pullik (sinovsiz)"
        await callback.answer(f"✅ {BOT_TYPES.get(bt, bt)}: {new_status}")

    def find_bot_by_id(bot_id: int):
        for token, info in data["bots"].items():
            if info.get("id") == bot_id:
                return token, info
        return None, None

    @dp.callback_query(F.data.startswith("changetariff_"))
    async def changetariff_cb(callback: CallbackQuery):
        bot_id = int(callback.data.split("_", 1)[1])
        token, target = find_bot_by_id(bot_id)
        if not target or callback.from_user.id not in target.get("admin_ids", [target["admin_id"]]):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        if target["type"] not in ("kino_pro",):
            await callback.answer("Bu bot turi uchun tarif tanlash mavjud emas — narx doim bir xil.", show_alert=True)
            return
        current_tariff = target.get("tariff", "2")
        buttons = [
            [InlineKeyboardButton(
                text=("✅ " if tid == current_tariff else "") + f"{t['name']} — {t['price']:,} so'm/oy ({tariff_limit_text(t)})",
                callback_data=f"settariff_{bot_id}_{tid}",
            )]
            for tid, t in tariffs_for(target["type"]).items()
        ]
        buttons.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data=f"mb_{bot_id}")])
        await mb_edit(
            callback,
            f"🔄 <b>{html_escape(target['name'])}</b> uchun yangi tarifni tanlang:",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

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
        tariff = get_tariff(tariff_id, target["type"])
        text, kb = mybot_detail_view(target)
        await mb_edit(
            callback,
            f"✅ Tarif o'zgartirildi: <b>{tariff['name']}</b> — {tariff['price']:,} so'm/oy\n"
            "Kunlik limit darhol qo'llanadi, keyingi to'lovda yangi narx hisoblanadi.\n\n" + text,
            kb,
        )

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
        text, kb = mybot_detail_view(target)
        await mb_edit(
            callback,
            f"✅ <b>To'lov muvaffaqiyatli!</b> 💰 {amount:,} so'm yechildi, bot {new_date} gacha faol.\n"
            f"💳 Qolgan balans: {data['user_balances'][key]:,} so'm\n\n" + text,
            kb,
        )

    @dp.callback_query(F.data.startswith("activate_"))
    async def activate_cb(callback: CallbackQuery, state: FSMContext):
        if not is_full_admin(callback.from_user.id):
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
        if not is_full_admin(callback.from_user.id):
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
        if not is_full_admin(message.from_user.id):
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
        if not is_full_admin(message.from_user.id):
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
    try:
        await clone_bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        logging.error(f"delete_webhook xatosi (klon {token[:10]}...): {e}")
    clone_dp = Dispatcher(storage=MemoryStorage())
    setup_platform_bot(clone_dp)
    await setup_public_menu(clone_bot)
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


# ======================================================================
#  AVTOMATIK TO'LOV TIZIMLARI (Click / Payme / Octo / Telegram Pay)
#  Har bir bot admini o'z kalitlarini kiritadi — to'lov avtomatik tasdiqlanadi.
# ======================================================================
AUTO_PROVIDERS = {
    "click": {
        "title": "Click",
        "live": True,
        "fields": [
            ("service_id", "🔢 Click <b>SERVICE ID</b> ni yuboring:\n(merchant.click.uz → Xizmat → Service ID)"),
            ("merchant_id", "🔢 Click <b>MERCHANT ID</b> ni yuboring:\n(merchant.click.uz → Merchant ID)"),
            ("secret_key", "🔑 Click <b>SECRET KEY</b> (API kalit) ni yuboring:"),
        ],
    },
    "payme": {
        "title": "Payme",
        "live": True,
        "fields": [
            ("merchant_id", "🔢 Payme <b>Merchant ID</b> (kassa ID) ni yuboring:\n(business.payme.uz → Kassa)"),
            ("key", "🔑 Payme <b>KEY</b> (API kalit / parol) ni yuboring:"),
        ],
    },
    "octo": {
        "title": "Octo",
        "live": True,
        "fields": [
            ("octo_shop_id", "🔢 Octo <b>Shop ID</b> ni yuboring:\n(Octo shaxsiy kabinetidagi do'kon raqami)"),
            ("octo_secret", "🔑 Octo <b>Secret key</b> ni yuboring:\n(Octo shaxsiy kabinetidagi maxfiy kalit)"),
        ],
    },
    "tgpay": {
        "title": "Telegram Pay",
        "live": True,
        "fields": [
            ("provider_token", "🔑 <b>Provider token</b> ni yuboring:\n(@BotFather → botingiz → Payments → Click yoki Payme ni tanlang → token)"),
        ],
    },
}

AUTO_FULFILL = {}          # (bot_token, kind) -> async fn(bot, order)
AUTO_ORDERS_KEEP = 500
PAYME_TIMEOUT_MS = 12 * 60 * 60 * 1000


def public_base_url() -> str:
    explicit = os.getenv("MINIAPP_URL")
    if explicit:
        return explicit.rstrip("/")
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    return f"https://{domain}" if domain else ""


def auto_webhook_url(info: dict, provider: str) -> str:
    base = public_base_url()
    return f"{base}/pay/{provider}/{info.get('id')}" if base else ""


def auto_provider_ready(info: dict, provider: str) -> bool:
    spec = AUTO_PROVIDERS.get(provider)
    cfg = (info.get("auto_payments") or {}).get(provider)
    if not spec or not cfg or not spec["live"] or not cfg.get("enabled", True):
        return False
    return all(cfg.get(k) for k, _ in spec["fields"])


def auto_pay_buttons(info: dict, kind: str, ref: str) -> list:
    """Mijozga ko'rsatiladigan avtomatik to'lov tugmalari (faqat ulangan va ishlaydigan tizimlar)."""
    rows = []
    for p, spec in AUTO_PROVIDERS.items():
        if auto_provider_ready(info, p):
            rows.append([InlineKeyboardButton(
                text=f"⚡ {spec['title']} (Avto)", callback_data=f"apay|{kind}|{ref}|{p}")])
    return rows


def auto_has_ready(info: dict) -> bool:
    return any(auto_provider_ready(info, p) for p in AUTO_PROVIDERS)


def auto_create_order(info: dict, uid: int, kind: str, ref: str, amount: int, title: str, provider: str) -> str:
    orders = info.setdefault("auto_orders", {})
    order_id = uuid.uuid4().hex[:12]
    orders[order_id] = {
        "uid": int(uid), "kind": kind, "ref": str(ref), "amount": int(amount),
        "title": title, "provider": provider, "status": "new",
        "prepare_id": int(uuid.uuid4().int % 10**9) + 1,
        "created": datetime.now().isoformat(),
    }
    if len(orders) > AUTO_ORDERS_KEEP:
        # eng eskilarini o'chiramiz, lekin to'langan/kutilayotganlarni emas
        for oid in sorted(orders, key=lambda k: orders[k].get("created", ""))[:len(orders) - AUTO_ORDERS_KEEP]:
            if orders[oid].get("status") in ("new", "cancelled"):
                orders.pop(oid, None)
    save_data()
    return order_id


def auto_pay_link(info: dict, provider: str, order_id: str, amount: int) -> str:
    cfg = info["auto_payments"][provider]
    if provider == "click":
        url = (
            "https://my.click.uz/services/pay"
            f"?service_id={quote(str(cfg['service_id']))}"
            f"&merchant_id={quote(str(cfg['merchant_id']))}"
            f"&amount={int(amount)}.00"
            f"&transaction_param={order_id}"
        )
        uname = info.get("username") or info.get("bot_username")
        if uname:
            url += f"&return_url={quote('https://t.me/' + str(uname).lstrip('@'))}"
        return url
    if provider == "payme":
        raw = f"m={cfg['merchant_id']};ac.order_id={order_id};a={int(amount) * 100}"
        return "https://checkout.paycom.uz/" + base64.b64encode(raw.encode()).decode()
    return ""


def auto_find_bot(bot_id):
    try:
        bid = int(bot_id)
    except Exception:
        return None, None
    for token, info in data["bots"].items():
        if info.get("id") == bid:
            return token, info
    return None, None


# ---------------- Click (Shop API: Prepare / Complete) ----------------
def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def click_handle(info: dict, form: dict):
    """(javob, to'langan_order_id yoki None) qaytaradi."""
    cfg = (info.get("auto_payments") or {}).get("click") or {}
    g = lambda k: str(form.get(k, "") if form.get(k) is not None else "")
    resp = {
        "click_trans_id": g("click_trans_id"),
        "merchant_trans_id": g("merchant_trans_id"),
        "error": 0, "error_note": "Success",
    }

    def fail(code, note):
        resp["error"], resp["error_note"] = code, note
        return resp, None

    if not cfg.get("secret_key"):
        return fail(-8, "Click sozlanmagan")
    action = g("action")
    if action not in ("0", "1"):
        return fail(-3, "Action not found")
    if g("service_id") != str(cfg.get("service_id")):
        return fail(-8, "Service id mismatch")

    sign_src = f"{g('click_trans_id')}{g('service_id')}{cfg['secret_key']}{g('merchant_trans_id')}"
    if action == "1":
        sign_src += g("merchant_prepare_id")
    sign_src += f"{g('amount')}{action}{g('sign_time')}"
    if not hmac.compare_digest(_md5(sign_src), g("sign_string").lower()):
        return fail(-1, "SIGN CHECK FAILED!")

    order = (info.get("auto_orders") or {}).get(g("merchant_trans_id"))
    if not order or order.get("provider") != "click":
        return fail(-5, "Order not found")
    try:
        if abs(float(g("amount")) - float(order["amount"])) > 0.01:
            return fail(-2, "Incorrect parameter amount")
    except ValueError:
        return fail(-2, "Incorrect parameter amount")

    if action == "0":
        if order["status"] == "paid":
            return fail(-4, "Already paid")
        if order["status"] == "cancelled":
            return fail(-9, "Transaction cancelled")
        order["status"] = "prepared"
        order["click_trans_id"] = g("click_trans_id")
        save_data()
        resp["merchant_prepare_id"] = order["prepare_id"]
        return resp, None

    # action == "1" (Complete)
    resp["merchant_prepare_id"] = order["prepare_id"]
    if order["status"] == "paid":
        return fail(-4, "Already paid")
    if g("merchant_prepare_id") != str(order["prepare_id"]) or order.get("click_trans_id") != g("click_trans_id"):
        return fail(-6, "Transaction does not exist")
    try:
        click_err = int(g("error") or 0)
    except ValueError:
        click_err = 0
    if click_err < 0 or order["status"] == "cancelled":
        order["status"] = "cancelled"
        save_data()
        return fail(-9, "Transaction cancelled")
    order["status"] = "paid"
    order["paid_at"] = datetime.now().isoformat()
    save_data()
    resp["merchant_confirm_id"] = order["prepare_id"]
    return resp, g("merchant_trans_id")


# ---------------- Payme (Merchant API, JSON-RPC) ----------------
def _payme_err(rid, code, msg, field=None):
    err = {"code": code, "message": {"uz": msg, "ru": msg, "en": msg}}
    if field:
        err["data"] = field
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _payme_tx_view(order):
    p = order["payme"]
    return {
        "create_time": p["create_time"], "perform_time": p["perform_time"],
        "cancel_time": p["cancel_time"], "transaction": order["id"],
        "state": p["state"], "reason": p["reason"],
    }


def _payme_find_tx(info, tid):
    for oid, o in (info.get("auto_orders") or {}).items():
        if o.get("payme", {}).get("id") == tid:
            o["id"] = oid
            return o
    return None


def _payme_check_order(info, params):
    """(order, xato_javobi_kodi_va_matni) — CheckPerform/Create uchun umumiy tekshiruv."""
    oid = str((params.get("account") or {}).get("order_id", ""))
    order = (info.get("auto_orders") or {}).get(oid)
    if not order or order.get("provider") != "payme":
        return None, (-31050, "Buyurtma topilmadi", "order_id")
    order["id"] = oid
    if params.get("amount") != int(order["amount"]) * 100:
        return None, (-31001, "Noto'g'ri summa", None)
    if order["status"] in ("paid", "cancelled"):
        return None, (-31051, "Buyurtma holati to'lov uchun mos emas", "order_id")
    return order, None


def payme_handle(info: dict, auth_header: str, body: dict):
    """(javob, to'langan_order_id yoki None) qaytaradi. Payme har doim HTTP 200 kutadi."""
    rid = body.get("id") if isinstance(body, dict) else None
    cfg = (info.get("auto_payments") or {}).get("payme") or {}
    try:
        scheme, _, b64 = (auth_header or "").partition(" ")
        login, _, key = base64.b64decode(b64).decode("utf-8").partition(":")
        ok = scheme == "Basic" and login == "Paycom" and cfg.get("key") and hmac.compare_digest(key, str(cfg["key"]))
    except Exception:
        ok = False
    if not ok:
        return _payme_err(rid, -32504, "Insufficient privilege to perform this method"), None
    if not isinstance(body, dict) or "method" not in body:
        return _payme_err(rid, -32600, "Invalid request"), None

    method, params = body["method"], body.get("params") or {}
    now = int(datetime.now().timestamp() * 1000)

    if method == "CheckPerformTransaction":
        order, err = _payme_check_order(info, params)
        if err:
            return _payme_err(rid, *err), None
        return {"jsonrpc": "2.0", "id": rid, "result": {"allow": True}}, None

    if method == "CreateTransaction":
        tid = params.get("id")
        existing = _payme_find_tx(info, tid)
        if existing:
            p = existing["payme"]
            if p["state"] != 1:
                return _payme_err(rid, -31008, "Tranzaksiyani bajarib bo'lmaydi"), None
            if now - p["create_time"] > PAYME_TIMEOUT_MS:
                p.update(state=-1, cancel_time=now, reason=4)
                existing["status"] = "cancelled"
                save_data()
                return _payme_err(rid, -31008, "Tranzaksiya muddati tugagan"), None
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "create_time": p["create_time"], "transaction": existing["id"], "state": 1}}, None
        order, err = _payme_check_order(info, params)
        if err:
            return _payme_err(rid, *err), None
        pending = order.get("payme")
        if pending and pending.get("state") == 1:
            return _payme_err(rid, -31050, "Buyurtma band", "order_id"), None
        order["payme"] = {"id": tid, "time": params.get("time"), "create_time": now,
                          "perform_time": 0, "cancel_time": 0, "state": 1, "reason": None}
        order["status"] = "prepared"
        save_data()
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "create_time": now, "transaction": order["id"], "state": 1}}, None

    if method in ("PerformTransaction", "CancelTransaction", "CheckTransaction"):
        order = _payme_find_tx(info, params.get("id"))
        if not order:
            return _payme_err(rid, -31003, "Tranzaksiya topilmadi"), None
        p = order["payme"]

        if method == "CheckTransaction":
            return {"jsonrpc": "2.0", "id": rid, "result": _payme_tx_view(order)}, None

        if method == "PerformTransaction":
            if p["state"] == 2:
                return {"jsonrpc": "2.0", "id": rid, "result": {
                    "transaction": order["id"], "perform_time": p["perform_time"], "state": 2}}, None
            if p["state"] != 1:
                return _payme_err(rid, -31008, "Tranzaksiyani bajarib bo'lmaydi"), None
            if now - p["create_time"] > PAYME_TIMEOUT_MS:
                p.update(state=-1, cancel_time=now, reason=4)
                order["status"] = "cancelled"
                save_data()
                return _payme_err(rid, -31008, "Tranzaksiya muddati tugagan"), None
            p.update(state=2, perform_time=now)
            order["status"] = "paid"
            order["paid_at"] = datetime.now().isoformat()
            save_data()
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "transaction": order["id"], "perform_time": now, "state": 2}}, order["id"]

        # CancelTransaction
        if p["state"] == 1:
            p.update(state=-1, cancel_time=now, reason=params.get("reason"))
            order["status"] = "cancelled"
            save_data()
        elif p["state"] == 2:
            # xizmat allaqachon berilgan — avtomatik qaytarishni qo'llab-quvvatlamaymiz
            return _payme_err(rid, -31007, "Buyurtma bajarilgan, bekor qilib bo'lmaydi"), None
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "transaction": order["id"], "cancel_time": p["cancel_time"], "state": p["state"]}}, None

    if method == "GetStatement":
        frm, to = params.get("from", 0), params.get("to", 0)
        txs = []
        for oid, o in (info.get("auto_orders") or {}).items():
            p = o.get("payme")
            if o.get("provider") == "payme" and p and frm <= p["create_time"] <= to:
                o["id"] = oid
                txs.append({"id": p["id"], "time": p.get("time"), "amount": int(o["amount"]) * 100,
                            "account": {"order_id": oid}, **_payme_tx_view(o)})
        return {"jsonrpc": "2.0", "id": rid, "result": {"transactions": txs}}, None

    return _payme_err(rid, -32601, "Method not found"), None


# ---------------- To'lovdan keyin xizmatni berish ----------------
async def auto_fulfill(token: str, info: dict, order_id: str):
    order = (info.get("auto_orders") or {}).get(order_id)
    if not order or order.get("fulfilled"):
        return
    order["fulfilled"] = True      # ikki marta berilmasligi uchun avval belgilaymiz
    save_data()
    spec = AUTO_PROVIDERS.get(order["provider"], {"title": order["provider"]})
    bot = _new_bot(token)
    try:
        fn = AUTO_FULFILL.get((token, order["kind"]))
        failed = None
        if fn:
            try:
                await fn(bot, order)
            except Exception as e:
                logging.exception(f"Avto-to'lovni bajarishda xato ({order_id})")
                failed = str(e)
        else:
            failed = "bajaruvchi topilmadi"
        text = (
            f"💳 <b>Avtomatik to'lov ({spec['title']})</b>\n\n"
            f"🧾 {html_escape(order.get('title', '-'))}\n"
            f"💰 {int(order['amount']):,} so'm\n"
            f"👤 Foydalanuvchi ID: <code>{order['uid']}</code>"
        )
        if failed:
            text += f"\n\n⚠️ To'lov keldi, lekin xizmat avtomatik berilmadi ({html_escape(failed)}). Qo'lda tekshiring! Buyurtma: <code>{order_id}</code>"
        for aid in info.get("admin_ids", [info.get("admin_id")]):
            if not aid:
                continue
            try:
                await bot.send_message(aid, text)
            except Exception as e:
                logging.info(f"Adminga avto-to'lov xabari yuborilmadi ({aid}): {e}")
    finally:
        try:
            await _close_bot(bot)
        except Exception:
            pass


async def pay_webhook_click(request):
    token, info = auto_find_bot(request.match_info.get("bot_id"))
    if not info:
        return web.json_response({"error": -8, "error_note": "Bot not found"})
    try:
        form = dict(await request.post())
    except Exception:
        return web.json_response({"error": -8, "error_note": "Error in request from click"})
    resp, paid_oid = click_handle(info, form)
    if paid_oid:
        asyncio.create_task(auto_fulfill(token, info, paid_oid))
    return web.json_response(resp)


async def pay_webhook_payme(request):
    token, info = auto_find_bot(request.match_info.get("bot_id"))
    if not info:
        return web.json_response(_payme_err(None, -32504, "Bot not found"))
    try:
        body = await request.json()
    except Exception:
        return web.json_response(_payme_err(None, -32700, "Parse error"))
    resp, paid_oid = payme_handle(info, request.headers.get("Authorization", ""), body)
    if paid_oid:
        asyncio.create_task(auto_fulfill(token, info, paid_oid))
    return web.json_response(resp)


# ---------------- Octo (prepare_payment + status tekshiruvi) ----------------
OCTO_HOST = "https://secure.octo.uz"
OCTO_INFLIGHT = set()


async def _octo_post(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{OCTO_HOST}/prepare_payment", json=payload)
        return r.json()


def _octo_creds(cfg: dict) -> dict:
    sid = str(cfg.get("octo_shop_id", "")).strip()
    return {"octo_shop_id": int(sid) if sid.isdigit() else sid, "octo_secret": cfg.get("octo_secret", "")}


async def octo_create_payment(info: dict, order_id: str, amount: int, title: str, uid: int) -> str:
    """Octo to'lov havolasini yaratadi. Xato bo'lsa bo'sh satr qaytaradi."""
    cfg = (info.get("auto_payments") or {}).get("octo") or {}
    notify = auto_webhook_url(info, "octo")
    if not cfg or not notify:
        return ""
    uname = info.get("username") or info.get("bot_username")
    payload = {
        **_octo_creds(cfg),
        "shop_transaction_id": order_id,
        "auto_capture": True,
        "init_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_data": {"user_id": str(uid)},
        "total_sum": float(amount),
        "currency": "UZS",
        "description": (title or "To'lov")[:200],
        "return_url": f"https://t.me/{str(uname).lstrip('@')}" if uname else "https://t.me",
        "notify_url": notify,
        "language": "uz",
        "ttl": 30,
    }
    try:
        res = await _octo_post(payload)
    except Exception as e:
        logging.warning(f"Octo to'lov yaratishda xato: {e}")
        return ""
    url = ((res or {}).get("data") or {}).get("octo_pay_url")
    if (res or {}).get("error") == 0 and url:
        return url
    logging.warning(f"Octo to'lov yaratilmadi: {res}")
    return ""


async def octo_verify_paid(info: dict, order: dict, order_id: str) -> bool:
    """Webhookka ishonmaymiz: to'lov holatini Octo'dan o'z kalitimiz bilan so'raymiz."""
    cfg = (info.get("auto_payments") or {}).get("octo") or {}
    if not cfg:
        return False
    try:
        res = await _octo_post({**_octo_creds(cfg), "shop_transaction_id": order_id})
    except Exception as e:
        logging.warning(f"Octo holatini tekshirishda xato: {e}")
        return False
    d = (res or {}).get("data") or {}
    if (res or {}).get("error") != 0 or d.get("status") != "succeeded":
        return False
    if d.get("shop_transaction_id") not in (None, order_id):
        return False
    if d.get("total_sum") is not None:
        try:
            if abs(float(d["total_sum"]) - float(order["amount"])) > 0.01:
                logging.warning(f"Octo summa mos emas: {d.get('total_sum')} != {order['amount']} ({order_id})")
                return False
        except (TypeError, ValueError):
            return False
    return True


async def pay_webhook_octo(request):
    token, info = auto_find_bot(request.match_info.get("bot_id"))
    if not info:
        return web.json_response({"error": "bot not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    oid = str((body or {}).get("shop_transaction_id", ""))
    order = (info.get("auto_orders") or {}).get(oid)
    if not order or order.get("provider") != "octo":
        return web.json_response({"error": "order not found"}, status=404)
    if order.get("status") == "paid" or oid in OCTO_INFLIGHT:
        return web.json_response({"status": "ok"})
    OCTO_INFLIGHT.add(oid)
    try:
        if await octo_verify_paid(info, order, oid):
            order["status"] = "paid"
            order["paid_at"] = datetime.now().isoformat()
            save_data()
            asyncio.create_task(auto_fulfill(token, info, oid))
    finally:
        OCTO_INFLIGHT.discard(oid)
    return web.json_response({"status": "ok"})


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

    info.setdefault("auto_payments", {})
    info.setdefault("auto_orders", {})

    def pay_root_kb():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Avtomatik to‘lov tizimlari", callback_data="pay_auto")],
            [InlineKeyboardButton(text="📝 Oddiy to‘lov tizimlari", callback_data="pay_manual")],
        ])

    def pay_auto_kb():
        def label(p):
            cfg = info["auto_payments"].get(p)
            mark = ""
            if cfg:
                mark = "✅ " if cfg.get("enabled", True) else "⏸ "
            return f"{mark}{AUTO_PROVIDERS[p]['title']} (Avto)"
        btns = [InlineKeyboardButton(text=label(p), callback_data=f"autoprov_{p}") for p in AUTO_PROVIDERS]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="pay_root")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @dp.message(F.text == "💳 To'lov tizimlar")
    async def payment_systems_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("⚙️ To'lov tizim sozlamalaridasiz:", reply_markup=pay_root_kb())

    @dp.callback_query(F.data == "pay_root")
    async def pay_root_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        await callback.message.edit_text("⚙️ To'lov tizim sozlamalaridasiz:", reply_markup=pay_root_kb())
        await callback.answer()

    @dp.callback_query(F.data == "pay_manual")
    async def pay_manual_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["payment_systems"]:
            await callback.message.answer("⚠️ To'lov tizimlari mavjud emas.", reply_markup=payment_systems_kb())
        else:
            await callback.message.answer("💳 To'lov tizimlari boshqaruvi:", reply_markup=payment_systems_kb())
        await callback.answer()

    @dp.callback_query(F.data == "pay_auto")
    async def pay_auto_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        await callback.message.edit_text("⚡ Avtomatik to'lov tizimini tanlang:", reply_markup=pay_auto_kb())
        await callback.answer()

    def auto_status_text(p: str) -> str:
        spec, cfg = AUTO_PROVIDERS[p], info["auto_payments"][p]
        state_txt = "✅ Yoqilgan" if cfg.get("enabled", True) else "⏸ O'chirilgan"
        url = auto_webhook_url(info, p)
        txt = f"⚡ <b>{spec['title']} (Avto)</b>\n\nHolati: {state_txt}\n"
        if not spec["live"]:
            txt += "\n⚠️ Kalit saqlangan, lekin bu tizim bilan avtomatik ulanish hali ishga tushirilmagan — mijozlarga ko'rinmaydi.\n"
        elif p == "tgpay":
            txt += "\nℹ️ Qo'shimcha sozlash shart emas — to'lov Telegram ichida, tayyor hisob (invoice) orqali bo'ladi.\n"
        elif p == "octo":
            txt += "\n✅ Webhook manzili har bir to'lov bilan avtomatik yuboriladi — Octo kabinetiga hech narsa kiritish shart emas.\n"
            if not url:
                txt += "\n⚠️ Server manzili topilmadi (MINIAPP_URL yoki RAILWAY_PUBLIC_DOMAIN) — Octo to'lovi ishlamaydi.\n"
        elif url:
            what = "Prepare URL va Complete URL" if p == "click" else "Endpoint URL"
            txt += f"\n🔗 {what} (kabinetga kiriting):\n<code>{url}</code>\n"
        else:
            txt += "\n⚠️ Server manzili topilmadi (MINIAPP_URL yoki RAILWAY_PUBLIC_DOMAIN).\n"
        return txt

    async def auto_ask_field(message: Message, p: str, idx: int):
        await message.answer(AUTO_PROVIDERS[p]["fields"][idx][1])

    @dp.callback_query(F.data.startswith("autoprov_"))
    async def auto_provider_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        p = callback.data.split("_", 1)[1]
        if p not in AUTO_PROVIDERS:
            await callback.answer()
            return
        cfg = info["auto_payments"].get(p)
        if cfg:
            toggle = "⏸ O'chirib turish" if cfg.get("enabled", True) else "▶️ Yoqish"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Kalitni almashtirish", callback_data=f"autoset_{p}")],
                [InlineKeyboardButton(text=toggle, callback_data=f"autotoggle_{p}")],
                [InlineKeyboardButton(text="🗑 Olib tashlash", callback_data=f"autodel_{p}")],
                [InlineKeyboardButton(text="◀️ Orqaga", callback_data="pay_auto")],
            ])
            await callback.message.edit_text(auto_status_text(p), reply_markup=kb)
            await callback.answer()
            return
        await state.set_state(AutoPayAdd.waiting_value)
        await state.update_data(ap_provider=p, ap_idx=0, ap_vals={})
        await auto_ask_field(callback.message, p, 0)
        await callback.answer()

    @dp.callback_query(F.data.startswith("autoset_"))
    async def auto_set_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        p = callback.data.split("_", 1)[1]
        if p not in AUTO_PROVIDERS:
            await callback.answer()
            return
        await state.set_state(AutoPayAdd.waiting_value)
        await state.update_data(ap_provider=p, ap_idx=0, ap_vals={})
        await auto_ask_field(callback.message, p, 0)
        await callback.answer()

    @dp.callback_query(F.data.startswith("autotoggle_"))
    async def auto_toggle_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        p = callback.data.split("_", 1)[1]
        cfg = info["auto_payments"].get(p)
        if not cfg:
            await callback.answer()
            return
        cfg["enabled"] = not cfg.get("enabled", True)
        save_data()
        await callback.answer("Saqlandi!")
        await callback.message.edit_text(auto_status_text(p), reply_markup=callback.message.reply_markup)

    @dp.callback_query(F.data.startswith("autodel_"))
    async def auto_del_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        p = callback.data.split("_", 1)[1]
        if info["auto_payments"].pop(p, None) is not None:
            save_data()
        await callback.message.edit_text("🗑 Olib tashlandi.\n\n⚡ Avtomatik to'lov tizimini tanlang:", reply_markup=pay_auto_kb())
        await callback.answer()

    @dp.message(AutoPayAdd.waiting_value)
    async def auto_value_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        value = (message.text or "").strip()
        sd = await state.get_data()
        p, idx, vals = sd.get("ap_provider"), sd.get("ap_idx", 0), dict(sd.get("ap_vals", {}))
        if p not in AUTO_PROVIDERS:
            await state.clear()
            return
        if not value or len(value) > 300:
            await message.answer("❌ Noto'g'ri qiymat. Qaytadan yuboring:")
            return
        try:
            await message.delete()      # kalit chatda qolib ketmasin
        except Exception:
            pass
        fields = AUTO_PROVIDERS[p]["fields"]
        fname = fields[idx][0]
        if fname == "octo_shop_id" and not value.isdigit():
            await message.answer("❌ Shop ID faqat raqamlardan iborat bo'lishi kerak. Qaytadan yuboring:")
            return
        if fname == "provider_token" and ":" not in value:
            await message.answer("❌ Bu provider token'ga o'xshamaydi (ichida «:» bo'ladi). Qaytadan yuboring:")
            return
        vals[fname] = value
        if idx + 1 < len(fields):
            await state.update_data(ap_idx=idx + 1, ap_vals=vals)
            await auto_ask_field(message, p, idx + 1)
            return
        old = info["auto_payments"].get(p, {})
        info["auto_payments"][p] = {**vals, "enabled": old.get("enabled", True)}
        save_data()
        await state.clear()
        title = AUTO_PROVIDERS[p]["title"]
        if AUTO_PROVIDERS[p]["live"]:
            head = f"✅ <b>{title} (Avto)</b> ulandi — avtomatik to'lov tayyor!\n\n"
        else:
            head = f"✅ <b>{title} (Avto)</b> kaliti saqlandi.\n\n"
        await message.answer(head + auto_status_text(p).split("\n\n", 1)[1])

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
    @dp.message(F.text == "⚙️ Premium")
    async def premium_admin_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            # Oddiy foydalanuvchi "💎 Premium" bossa, keyingi (mijoz) handlerga o'tsin
            raise SkipHandler
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
        buttons += auto_pay_buttons(info, "premium", tid)
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
        log_kino_payment(info, target_uid, tariff, "chek")
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

    # ---------- Avtomatik to'lov (mijoz tanlovi) ----------
    @dp.callback_query(F.data.startswith("apay|"))
    async def auto_pay_chosen_cb(callback: CallbackQuery):
        try:
            _, kind, ref, prov = callback.data.split("|", 3)
        except ValueError:
            await callback.answer("❌ Xatolik.", show_alert=True)
            return
        if not auto_provider_ready(info, prov):
            await callback.answer("❌ Bu to'lov usuli hozir mavjud emas.", show_alert=True)
            return
        uid = callback.from_user.id
        amount, title = 0, ""
        if kind == "premium":
            t = info["premium_tariffs"].get(ref)
            if t:
                amount, title = int(t["price"]), f"Premium — {t['name']}"
        elif kind == "kmovie":
            e = info.get("movies", {}).get(ref)
            if e and e.get("access") == "paid" and e.get("price"):
                amount, title = int(e["price"]), f"Kino #{ref} — {e.get('title') or ''}".strip(" —")
        elif kind == "stars":
            try:
                stars_n, price_n = (int(x) for x in ref.split("-", 1))
                if stars_n > 0 and price_n > 0:
                    amount, title = price_n, f"⭐ {stars_n:,} Stars (hisobga yuborish kerak)"
            except ValueError:
                pass
        elif kind == "shoporder":
            o = info.get("shop_orders", {}).get(ref)
            if o and o.get("user_id") == uid and not o.get("paid"):
                amount, title = int(o["total"]), f"Buyurtma #{ref}"
        if amount <= 0:
            await callback.answer("❌ Ma'lumot topilmadi, qaytadan urinib ko'ring.", show_alert=True)
            return
        order_id = auto_create_order(info, uid, kind, ref, amount, title, prov)
        pname = AUTO_PROVIDERS[prov]["title"]
        if prov == "tgpay":
            try:
                await callback.message.answer_invoice(
                    title=(title or "To'lov")[:32],
                    description=f"{title} — {amount:,} so'm"[:250],
                    payload=f"apay_{order_id}",
                    provider_token=info["auto_payments"]["tgpay"]["provider_token"],
                    currency="UZS",
                    prices=[LabeledPrice(label=(title or "To'lov")[:60], amount=int(amount) * 100)],
                )
            except Exception as e:
                logging.warning(f"Telegram Pay hisobi yuborilmadi: {e}")
                info["auto_orders"][order_id]["status"] = "cancelled"
                save_data()
                await callback.answer("❌ To'lovni boshlab bo'lmadi. Administratorga murojaat qiling.", show_alert=True)
                return
            await callback.answer()
            return
        if prov == "octo":
            link = await octo_create_payment(info, order_id, amount, title, uid)
        else:
            link = auto_pay_link(info, prov, order_id, amount)
        if not link:
            info["auto_orders"][order_id]["status"] = "cancelled"
            save_data()
            await callback.answer("❌ To'lov havolasini yaratib bo'lmadi.", show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"💳 {pname} orqali to'lash — {amount:,} so'm", url=link)]])
        await callback.message.answer(
            f"⚡ <b>{pname} (Avto)</b>\n\n🧾 {html_escape(title)}\n💰 {amount:,} so'm\n\n"
            "Pastdagi tugma orqali to'lang — to'lov avtomatik tasdiqlanadi va xizmat darhol beriladi.",
            reply_markup=kb,
        )
        await callback.answer()

    async def _auto_fulfill_premium(bot, order):
        tariff = info["premium_tariffs"].get(order["ref"])
        if not tariff:
            raise RuntimeError("tarif topilmadi")
        uid = int(order["uid"])
        until = datetime.now() + timedelta(days=tariff["days"])
        info["premium_users"][str(uid)] = {"until": until.isoformat()}
        stat = info["premium_stats"].setdefault(order["ref"], {"count": 0, "revenue": 0})
        stat["count"] += 1
        stat["revenue"] += tariff["price"]
        log_kino_payment(info, uid, tariff, AUTO_PROVIDERS[order["provider"]]["title"])
        save_data()
        await bot.send_message(uid, (
            "✅ <b>To'lov muvaffaqiyatli qabul qilindi!</b>\n\n"
            f"Sizga {tariff['days']} kunlik Premium obuna berildi. "
            f"Amal qilish muddati: {until.strftime('%d.%m.%Y')} gacha.\n\n"
            "Endi cheklovlarsiz foydalanishingiz mumkin! 🎉"))

    AUTO_FULFILL[(token, "premium")] = _auto_fulfill_premium

    # ---------- Telegram Stars orqali to'lov ----------
    @dp.pre_checkout_query()
    async def stars_pre_checkout(pre_checkout_query: PreCheckoutQuery):
        pl = pre_checkout_query.invoice_payload or ""
        if pl.startswith("apay_"):
            order = (info.get("auto_orders") or {}).get(pl.split("_", 1)[1])
            if (not order or order.get("provider") != "tgpay" or order.get("status") == "paid"
                    or order.get("uid") != pre_checkout_query.from_user.id
                    or pre_checkout_query.total_amount != int(order["amount"]) * 100):
                await pre_checkout_query.answer(ok=False, error_message="Buyurtma topilmadi yoki allaqachon to'langan.")
                return
        await pre_checkout_query.answer(ok=True)

    @dp.message(F.successful_payment.invoice_payload.startswith("apay_"))
    async def tgpay_payment_success(message: Message):
        sp = message.successful_payment
        oid = sp.invoice_payload.split("_", 1)[1]
        order = (info.get("auto_orders") or {}).get(oid)
        if (not order or order.get("provider") != "tgpay" or order.get("uid") != message.from_user.id
                or sp.currency != "UZS" or sp.total_amount != int(order["amount"]) * 100):
            logging.warning(f"Telegram Pay: mos kelmaydigan to'lov ({oid})")
            await message.answer("⚠️ To'lov qabul qilindi, lekin buyurtma tasdiqlanmadi. Administratorga murojaat qiling.")
            return
        if order.get("status") != "paid":
            order["status"] = "paid"
            order["paid_at"] = datetime.now().isoformat()
            order["tg_charge_id"] = sp.provider_payment_charge_id
            save_data()
            await auto_fulfill(token, info, oid)

    @dp.message(F.successful_payment.invoice_payload.startswith("premium_"))
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
        log_kino_payment(info, message.from_user.id, tariff, "Stars")
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
            [KeyboardButton(text="📊 Statistika")],
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
        "👥 Foydalanuvchilar", "📢 Xabar va reklama", "👮 Moderatorlar",
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
        await callback.message.answer("Rasmni <b>forward (uzatib)</b> yuboring:")
        await state.set_state(ProductPhoto.waiting_photo)
        await callback.answer()

    @dp.message(ProductPhoto.waiting_photo, F.photo)
    async def pphoto_save(message: Message, state: FSMContext):
        if not is_forwarded_message(message):
            await message.answer(FORWARD_ONLY_TEXT)
            return
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
        _auto_rows = auto_pay_buttons(info, "shoporder", oid) if total > 0 else []
        if _auto_rows:
            await message.answer("⚡ Hoziroq onlayn to'lashingiz mumkin:", reply_markup=InlineKeyboardMarkup(inline_keyboard=_auto_rows))
        active_ads = [a for a in info["ads"].values() if a.get("active")]
        if active_ads:
            import random
            ad = random.choice(active_ads)
            await message.answer(f"📣 {ad['text']}")
        await state.clear()

    async def _auto_fulfill_shop(bot, order):
        o = info["shop_orders"].get(order["ref"])
        if not o:
            raise RuntimeError("buyurtma topilmadi")
        o["paid"] = True
        o["paid_via"] = AUTO_PROVIDERS[order["provider"]]["title"]
        save_data()
        await bot.send_message(int(order["uid"]), f"✅ <b>To'lov qabul qilindi!</b> Buyurtma #{order['ref']} to'langan.")

    AUTO_FULFILL[(token, "shoporder")] = _auto_fulfill_shop

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
        if not info["payment_systems"] and not auto_has_ready(info):
            await target_message.answer("Hozircha to'lov tizimlari mavjud emas. Administratorga murojaat qiling.")
            return
        buttons = [[InlineKeyboardButton(text=p["name"], callback_data=f"starpay_{pid}")] for pid, p in info["payment_systems"].items()]
        buttons += auto_pay_buttons(info, "stars", f"{stars}-{price}")
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

    async def _auto_fulfill_stars(bot, order):
        stars_n, price_n = (int(x) for x in order["ref"].split("-", 1))
        uid = int(order["uid"])
        order_id = uuid.uuid4().hex[:8]
        info["star_orders"][order_id] = {
            "user_id": uid, "stars": stars_n, "price": price_n,
            "status": "bajarildi", "created_at": datetime.now().isoformat(),
        }
        info["stats"]["orders"] += 1
        info["stats"]["stars_sold"] += stars_n
        info["stats"]["revenue"] += price_n
        save_data()
        await bot.send_message(uid, "✅ <b>To'lovingiz tasdiqlandi!</b>\n\n⭐ Stars tez orada hisobingizga yuboriladi. Rahmat!")

    AUTO_FULFILL[(token, "stars")] = _auto_fulfill_stars

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
    info.setdefault("referrals", {})               # {referrer_uid_str: [uid,...]}
    info.setdefault("new_content_notify", False)
    info.setdefault("maintenance_mode", False)
    info.setdefault("welcome_text", "🎬 Film kodini yuboring, men uni topib beraman.")
    info.setdefault("help_text", "Savol va takliflar uchun admin bilan bog'laning.")
    info.setdefault("ratings", {})                 # {code: {"total": int, "count": int, "by_user": {uid: stars}}}
    info.setdefault("vip_codes", [])               # [code, ...] — faqat Premium foydalanuvchilar uchun
    info.setdefault("series_subscribers", {})      # {code: [uid, ...]}
    info.setdefault("user_usernames", {})          # {str(uid): "username yoki F.I.Sh"}
    info.setdefault("user_phones", {})              # {str(uid): "+998901234567"}
    info.setdefault("phone_asked", [])              # kimlardan telefon so'ralgani (qayta so'ramaslik uchun)
    info.setdefault("moderators", [])              # faqat kontent qo'sha oladigan cheklangan adminlar
    info.setdefault("user_activity", {})           # {str(uid): count}
    info.setdefault("auto_report_enabled", False)
    info.setdefault("auto_report_hour", 9)
    info.setdefault("last_report_date", "")
    info.setdefault("vip_system", True)
    info.setdefault("protect_content", False)
    info.setdefault("movie_request_enabled", True)
    info.setdefault("ref_bonus_enabled", False)
    info.setdefault("ref_bonus_amount", 500)
    info.setdefault("weekly_top_enabled", False)
    info.setdefault("autopost_channels", [])
    info.setdefault("week_counts", {})
    info.setdefault("movie_requests", [])
    info.setdefault("bot_about", "")
    info.pop("start_photo", None)   # /start banner olib tashlangan — eski qiymat tozalanadi
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
    def customer_menu_kb():
        # Obunachi (mijoz) menyusi
        rows = [
            [KeyboardButton(text="🆕 Yangi kinolar"), KeyboardButton(text="🔥 TOP kinolar")],
            [KeyboardButton(text="🏷 Kategoriyalar"), KeyboardButton(text="🔍 Nom bo'yicha qidirish")],
        ]
        if is_pro():
            rows.append([KeyboardButton(text="💎 Premium"), KeyboardButton(text="👤 Profilim")])
            rows.append([KeyboardButton(text="💎 VIP kinolar")])
        else:
            rows.append([KeyboardButton(text="👤 Profilim")])
        rows.append([KeyboardButton(text="🔗 Do'st taklif qilish"), KeyboardButton(text="ℹ️ Yordam")])
        return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

    def ultra_admin_kb():
        keyboard = [
            [KeyboardButton(text="📊 Statistika"), KeyboardButton(text="👥 Foydalanuvchilar")],
            [KeyboardButton(text="🎬 Kinolar"), KeyboardButton(text="📮 Postlar")],
            [KeyboardButton(text="📩 Xabar yuborish"), KeyboardButton(text="📣 Reklama")],
            [KeyboardButton(text="🔐 Kanallar"), KeyboardButton(text="📥 So'rovlar")],
        ]
        if is_pro():
            # To'lov tizimlari va Premium faqat 🎬 Kino BOT uchun kerak
            keyboard.append([KeyboardButton(text="💳 To'lov tizimlar"), KeyboardButton(text="⚙️ Premium")])
        keyboard.append([KeyboardButton(text="📝 Matnlar"), KeyboardButton(text="🔗 Referal")])
        keyboard.append([KeyboardButton(text="👮 Adminlar"), KeyboardButton(text="↗️ Ulashish")])
        if get_kinopanel_url(info.get("id")):
            keyboard.append([KeyboardButton(text="🌐 Web Panel")])
        keyboard.append([KeyboardButton(text="⏪ Orqaga"), KeyboardButton(text="🎨 Dizayn")])
        return ReplyKeyboardMarkup(keyboard=keyboard + get_global_button_rows(), resize_keyboard=True)

    def code_pick_kb(prefix: str):
        # Kodlar tugmalari: qatorda 4 tadan, Telegram limiti sababli oxirgi 96 ta
        codes = list(info["movies"].keys())[-96:]
        rows, row = [], []
        for c in codes:
            row.append(InlineKeyboardButton(text=c, callback_data=f"{prefix}{c}"))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def content_menu_kb():
        # "🎬 Kinolar" bo'limining asosiy menyusi
        keyboard = [
            [KeyboardButton(text="🎬 Film qo'shish"), KeyboardButton(text="📺 Serial qo'shish")],
            [KeyboardButton(text="➕ Seriallarga qism qo'shish"), KeyboardButton(text="📋 Filmlar ro'yxati")],
            [KeyboardButton(text="🔍 Kod bo'yicha qidirish"), KeyboardButton(text="✏️ Tavsifni tahrirlash")],
            [KeyboardButton(text="🗑 Film o'chirish")],
        ]
        if is_pro():
            keyboard.append([KeyboardButton(text="🔒 VIP kino qo'shish"), KeyboardButton(text="🔒 VIP qilib belgilash")])
        keyboard.append([KeyboardButton(text="💰 Pullik kino qo'shish"), KeyboardButton(text="💰 Pullik qilib belgilash")])
        keyboard.append([KeyboardButton(text="🗓 Chiqish sanasini belgilash")])
        keyboard.append([KeyboardButton(text="🏷 Kategoriyalar"), KeyboardButton(text="⭐ Tavsiyalar")])
        keyboard.append([KeyboardButton(text="📈 TOP reyting"), KeyboardButton(text="📤 Eksport")])
        keyboard.append([KeyboardButton(text="◀️ Orqaga")])
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

    def content_extra_kb():
        # Eski "Qo'shimcha amallar" ham endi xuddi shu menyu
        return content_menu_kb()

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
            [KeyboardButton(text="👮 Moderatorlar")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def ads_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📣 Reklama joylash"), KeyboardButton(text="📋 Reklamalar ro'yxati")],
            [KeyboardButton(text="➖ Reklamani o'chirish")],
            [KeyboardButton(text="◀️ Orqaga")],
        ], resize_keyboard=True)

    def broadcast_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📢 Ommaviy xabar yuborish")],
            [KeyboardButton(text="📣 Reklama joylash"), KeyboardButton(text="📋 Reklamalar ro'yxati")],
            [KeyboardButton(text="➖ Reklamani o'chirish")],
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
        "👥 Foydalanuvchilar", "📢 Xabar va reklama", "⚙️ Sozlamalar",
        "👮 Moderatorlar",
    }

    @dp.message(F.text == "◀️ Orqaga")
    @dp.message(F.text == "⬅️ Orqaga")
    async def ultra_back(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await state.clear()
        await message.answer("⭐ <b>Boshqaruv paneli</b>", reply_markup=ultra_admin_kb())

    @dp.message(F.text == "🗄 Boshqaruv")
    async def ultra_back_boshqaruv(message: Message, state: FSMContext):
        if not is_moderator(message.from_user.id):
            return
        await state.clear()
        if is_admin(info, message.from_user.id):
            await message.answer("⭐ <b>Boshqaruv paneli</b>", reply_markup=ultra_admin_kb())
        else:
            await message.answer("🎬 <b>Kinolar bo'limidasiz:</b>", reply_markup=content_menu_kb())

    # ---------- /start ----------
    @dp.message(Command("start"))
    async def kstart(message: Message):
        uid = message.from_user.id
        args = message.text.split(maxsplit=1)
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        info["user_usernames"][str(uid)] = uname
        is_new_user = uid not in info["users"]
        if is_new_user:
            info["users"].append(uid)
            info.setdefault("join_log", {})[str(uid)] = datetime.now().strftime("%Y-%m-%d")
            save_data()
        else:
            save_data()
        # Referal: /start ref_<taklif qilgan odam ID si>  (faqat yangi foydalanuvchi uchun)
        if is_new_user and len(args) > 1 and args[1].strip().startswith("ref_"):
            try:
                ref_uid = int(args[1].strip().split("_", 1)[1])
            except (ValueError, IndexError):
                ref_uid = 0
            if ref_uid and ref_uid != uid and ref_uid in info["users"]:
                info.setdefault("ref_pending", {})[str(uid)] = ref_uid
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
        await settle_referral(message.bot, uid)
        if str(uid) not in info["user_phones"] and uid not in info["phone_asked"]:
            info["phone_asked"].append(uid)
            save_data()
            phone_kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="📱 Raqamni ulashish", request_contact=True)], [KeyboardButton(text="⏭ O'tkazib yuborish")]],
                resize_keyboard=True, one_time_keyboard=True,
            )
            await message.answer("📱 Botdan qulay foydalanish uchun telefon raqamingizni ulashing (ixtiyoriy):", reply_markup=phone_kb)
            return
        await send_kino_home(message, uid)

    async def send_kino_home(message: Message, uid: int):
        customer_kb = customer_menu_kb()
        welcome = info.get("welcome_text", "🎬 Film kodini yuboring, men uni topib beraman.")
        await message.answer(welcome, reply_markup=customer_kb)
        info.setdefault("menu_v2", {})[str(uid)] = 1
        save_data()
        if info.get("featured"):
            lines = []
            for code in info["featured"]:
                m = info["movies"].get(code)
                if m:
                    title = m.get("title") or (m.get("desc", "-")[:30])
                    lines.append(f"• Kod {code} — {title}")
            if lines:
                await message.answer("⭐ <b>Tavsiya etilgan kontent:</b>\n\n" + "\n".join(lines))

    @dp.message(F.contact)
    async def kino_contact_received(message: Message):
        uid = message.from_user.id
        if message.contact.user_id == uid:
            info["user_phones"][str(uid)] = message.contact.phone_number
            save_data()
        await message.answer("✅ Rahmat!")
        await send_kino_home(message, uid)

    @dp.message(F.text == "⏭ O'tkazib yuborish")
    async def kino_phone_skip(message: Message):
        await send_kino_home(message, message.from_user.id)

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

    # ---------- Web panel ----------
    @dp.message(F.text == "🌐 Web panel")
    @dp.message(F.text == "🌐 Web Panel")
    async def kino_web_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        panel_url = get_kinopanel_url(info.get("id"))
        if not panel_url:
            await message.answer("🌐 Web panel hozircha sozlanmagan. Birozdan so'ng qayta urinib ko'ring.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Panelni ochish", web_app=WebAppInfo(url=panel_url))]
        ])
        await message.answer("🎬 Kino bot boshqaruv paneli 👇", reply_markup=kb)

    def panel_inline_kb(text: str):
        panel_url = get_kinopanel_url(info.get("id"))
        if not panel_url:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=panel_url))]])

    # ---------- 📮 Postlar (kanallarga avto-joylash) ----------
    @dp.message(F.text == "📮 Postlar")
    async def kino_posts_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        chans = info.get("autopost_channels", [])
        lines = ["📮 <b>Postlar — kanallarga avto-joylash</b>\n",
                 "Yangi kino yoki qism qo'shilganda u shu kanallarga o'zi joylanadi.\n"]
        if not kino_autopost_allowed(info):
            lines.append("🔒 Bu imkoniyat <b>Pro</b>, <b>Turbo</b> va <b>Unlimited</b> tariflarida ishlaydi.")
        elif not chans:
            lines.append("Hali kanal ulanmagan. Kanalni ulash uchun botni kanalga admin qiling va Web panelda «Sozlamalar → Auto-post» bo'limidan qo'shing.")
        else:
            for c in chans:
                name = c.get("title") or (("@" + c["username"]) if c.get("username") else str(c.get("chat_id")))
                lines.append("• " + html_escape(str(name)))
        await message.answer("\n".join(lines), reply_markup=panel_inline_kb("🌐 Panelda sozlash"))

    # ---------- 📣 Reklama ----------
    @dp.message(F.text == "📣 Reklama")
    async def kino_ads_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("📣 <b>Reklama</b>", reply_markup=ads_menu_kb())

    # ---------- 📥 So'rovlar (foydalanuvchilarning kino so'rovlari) ----------
    @dp.message(F.text == "📥 So'rovlar")
    async def kino_requests_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        reqs = info.get("movie_requests", [])
        if not reqs:
            await message.answer("📥 Hozircha kino so'rovlari yo'q.")
            return
        lines = [f"📥 <b>Kino so'rovlari</b> (so'nggi {min(len(reqs), 20)} / jami {len(reqs)})\n"]
        for r in reversed(reqs[-20:]):
            who = info.get("user_usernames", {}).get(str(r.get("uid")), "ID %s" % r.get("uid"))
            title = (info.get("movies", {}).get(r.get("code"), {}) or {}).get("title", "")
            lines.append("• <b>%s</b>%s — %s · %s" % (
                html_escape(str(r.get("code", ""))), (" (%s)" % html_escape(title[:30])) if title else "",
                html_escape(str(who)), str(r.get("date", ""))[:10]))
        await message.answer("\n".join(lines))

    # ---------- 🔗 Referal ----------
    def referral_text() -> str:
        on = bool(info.get("ref_bonus_enabled"))
        return ("🔗 <b>Referal</b>\n\nDo'st taklif qilish bonusi: <b>%s</b>\nHar bir do'st uchun: <b>%s so'm</b>\n\n"
                "Bonus do'st majburiy kanalga a'zo bo'lgach beriladi. Summani Web panelda o'zgartirasiz." % (
                    "✅ Yoqilgan" if on else "⛔ O'chirilgan", "{:,}".format(int(info.get("ref_bonus_amount", 0))).replace(",", " ")))

    def referral_kb():
        on = bool(info.get("ref_bonus_enabled"))
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="⛔ O'chirish" if on else "✅ Yoqish", callback_data="kino_ref_toggle")]])

    @dp.message(F.text == "🔗 Referal")
    async def kino_referral_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(referral_text(), reply_markup=referral_kb())

    @dp.callback_query(F.data == "kino_ref_toggle")
    async def kino_referral_toggle(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        info["ref_bonus_enabled"] = not info.get("ref_bonus_enabled")
        save_data()
        try:
            await callback.message.edit_text(referral_text(), reply_markup=referral_kb())
        except Exception:
            pass
        await callback.answer("Saqlandi")

    # ---------- ↗️ Ulashish (kinoni boshqa chatga yuborishni taqiqlash) ----------
    def share_text() -> str:
        locked = bool(info.get("protect_content"))
        return ("↗️ <b>Ulashish</b>\n\nKinoni boshqa chatga uzatish va saqlash: <b>%s</b>\n\n"
                "Taqiqlansa, foydalanuvchilar kinoni boshqa chatga yubora olmaydi (adminlarga tegmaydi)." % (
                    "🔒 Taqiqlangan" if locked else "✅ Ruxsat berilgan"))

    def share_kb():
        locked = bool(info.get("protect_content"))
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="✅ Ruxsat berish" if locked else "🔒 Taqiqlash", callback_data="kino_share_toggle")]])

    @dp.message(F.text == "↗️ Ulashish")
    async def kino_share_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(share_text(), reply_markup=share_kb())

    @dp.callback_query(F.data == "kino_share_toggle")
    async def kino_share_toggle(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            await callback.answer("Ruxsat yo'q.", show_alert=True)
            return
        info["protect_content"] = not info.get("protect_content")
        save_data()
        try:
            await callback.message.edit_text(share_text(), reply_markup=share_kb())
        except Exception:
            pass
        await callback.answer("Saqlandi")

    # ---------- 🎨 Dizayn ----------
    @dp.message(F.text == "🎨 Dizayn")
    async def kino_design_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        kb = panel_inline_kb("🎨 Panelni ochish")
        await message.answer(
            "🎨 <b>Dizayn</b>\n\nBot menyusi rangi va ko'rinishini Web panelda sozlaysiz:\n"
            "<b>Sozlamalar → Tugmalar</b> (menyu, rang) va <b>Ko'rinish</b> (yorug' / qorong'i).",
            reply_markup=kb)

    # ---------- ⏪ Orqaga (mijoz ko'rinishiga) ----------
    @dp.message(F.text == "⏪ Orqaga")
    async def kino_admin_back(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("👤 Mijoz ko'rinishi. Admin menyuga qaytish uchun /start bosing.")
        await send_kino_home(message, message.from_user.id)

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
    @dp.message(F.text == "🎬 Kinolar")
    async def content_panel(message: Message):
        if not is_moderator(message.from_user.id):
            return
        await message.answer(
            "🎬 <b>Kinolar bo'limidasiz:</b>\n\nQuyidagi amallardan birini tanlang:",
            reply_markup=content_menu_kb(),
        )

    @dp.callback_query(F.data == "kcontent_more")
    async def content_more_cb(callback: CallbackQuery):
        if not is_moderator(callback.from_user.id):
            await callback.answer()
            return
        await callback.message.answer("🧰 <b>Qo'shimcha amallar</b>", reply_markup=content_extra_kb())
        await callback.answer()

    @dp.message(Command("addmovie"))
    @dp.message(F.text == "🎬 Film qo'shish")
    @dp.message(F.text == "📥 Kino yuklash")
    async def addmovie_cmd(message: Message, state: FSMContext):
        if not is_moderator(message.from_user.id):
            return
        await state.update_data(is_vip=False, is_paid=False)
        await message.answer(
            "Kino kodini yuboring (faqat raqam, masalan: 40):",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🗄 Boshqaruv")]], resize_keyboard=True),
        )
        await state.set_state(AddMovie.waiting_code)

    @dp.message(F.text == "💰 Pullik kino qo'shish")
    async def addpaidmovie_cmd(message: Message, state: FSMContext):
        if not is_moderator(message.from_user.id):
            return
        await state.update_data(is_vip=False, is_paid=True)
        await message.answer(
            "💰 <b>Pullik kino qo'shish</b>\n\nBu kinoni foydalanuvchilar alohida sotib oladi "
            "(masalan, 1 ta kino — 10 000 so'm). To'lagan odam kinoni doimiy ko'ra oladi.\n\n"
            "Kino kodini yuboring (faqat raqam, masalan: 40):",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🗄 Boshqaruv")]], resize_keyboard=True),
        )
        await state.set_state(AddMovie.waiting_code)

    @dp.message(F.text == "🔒 VIP kino qo'shish")
    async def addvipmovie_cmd(message: Message, state: FSMContext):
        if not is_moderator(message.from_user.id) or not is_pro():
            return
        await state.update_data(is_vip=True, is_paid=False)
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
            "Endi 1-qism videosini <b>forward (uzatib)</b> yuboring. Har bir videoni ketma-ket uzataverasiz "
            f"(avtomatik 1, 2, 3... deb raqamlanadi) — {count}-qism yuborilgach, bot avtomatik saqlaydi.\n"
            "Xohlasangiz, tugatish uchun /done ham yozishingiz mumkin."
        )
        await state.set_state(AddSeries.waiting_episode)

    async def finalize_series(message: Message, state: FSMContext, state_data: dict):
        episodes = state_data.get("episodes", {})
        code = state_data["code"]
        info["movies"].pop(code, None)   # almashtirilsa, "yangi" ro'yxatda oxiriga o'tadi
        info["movies"][code] = {
            "type": "series",
            "title": state_data["title"],
            "desc": state_data["desc"],
            "episodes": episodes,
            "added_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_data()
        if info.get("new_content_notify"):
            await notify_new_content(message.bot, f"📺 Yangi serial qo'shildi: {state_data['title']} (Kod: {code})")
        await message.answer(f"✅ Serial saqlandi: <b>{state_data['title']}</b> ({len(episodes)} qism), Kod: {code}")
        await state.clear()

    @dp.message(AddSeries.waiting_episode, F.video)
    async def addseries_episode(message: Message, state: FSMContext):
        if not is_forwarded_message(message):
            await message.answer(FORWARD_ONLY_TEXT)
            return
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
        await message.answer("❌ Forward qilingan video yuboring yoki barcha qismlar tugagan bo'lsa /done deb yozing.")

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
        protect = bool(info.get("protect_content")) and not (uid is not None and is_admin(info, uid))
        await send_func(file_id, caption=caption, reply_markup=kb, protect_content=protect)

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
        await message.answer(
            "🏷 Kino <b>nomini</b> yozing (masalan: Titanik).\n"
            "Foydalanuvchilar kinoni shu nom orqali ham qidirib topadi:"
        )
        await state.set_state(AddMovie.waiting_title)

    @dp.message(AddMovie.waiting_title, F.text)
    async def addmovie_title(message: Message, state: FSMContext):
        title = (message.text or "").strip()[:100]
        if not title or title.startswith("/"):
            await message.answer("❌ Nomni matn ko'rinishida yozing:")
            return
        await state.update_data(title=title)
        await message.answer("Endi kino haqida qisqacha tavsif yozing (janr, yil, davlat, til, sifat va h.k.):")
        await state.set_state(AddMovie.waiting_desc)

    @dp.message(AddMovie.waiting_desc)
    async def addmovie_desc(message: Message, state: FSMContext):
        await state.update_data(desc=message.text.strip())
        if (await state.get_data()).get("is_paid"):
            await message.answer("💰 Kino narxini so'mda yozing (masalan: 10000):")
            await state.set_state(AddMovie.waiting_price)
            return
        await message.answer("Endi filmni (videoni) <b>forward (uzatib)</b> yuboring:")
        await state.set_state(AddMovie.waiting_video)

    @dp.message(AddMovie.waiting_price, F.text)
    async def addmovie_price(message: Message, state: FSMContext):
        digits = re.sub(r"\D", "", message.text or "")
        if not digits or int(digits) < 1000:
            await message.answer("❌ Narxni raqamda yozing (kamida 1000 so'm), masalan: 10000")
            return
        await state.update_data(price=int(digits))
        await message.answer(
            f"✅ Narx: <b>{fmt_som(int(digits))} so'm</b>\n\nEndi filmni (videoni) <b>forward (uzatib)</b> yuboring:"
        )
        await state.set_state(AddMovie.waiting_video)

    @dp.message(AddMovie.waiting_video, F.photo | F.document | F.animation | F.video_note | F.audio | F.voice)
    async def addmovie_wrong_media(message: Message):
        await message.answer(FORWARD_ONLY_TEXT)

    async def notify_new_content(bot, text):
        for uid in info["users"]:
            try:
                await bot.send_message(uid, text)
            except Exception:
                pass

    @dp.message(AddMovie.waiting_video, F.video)
    async def addmovie_video(message: Message, state: FSMContext):
        if not is_forwarded_message(message):
            await message.answer(FORWARD_ONLY_TEXT)
            return
        state_data = await state.get_data()
        code = state_data.get("code")
        desc = state_data.get("desc", "")
        is_vip = state_data.get("is_vip", False)
        title = (state_data.get("title") or "").strip()
        info["movies"].pop(code, None)   # almashtirilsa, "yangi" ro'yxatda oxiriga o'tadi
        info["movies"][code] = {
            "file_id": message.video.file_id, "desc": desc,
            "added_at": datetime.now().isoformat(timespec="seconds"),
        }
        if title:
            info["movies"][code]["title"] = title
        if is_vip and code not in info["vip_codes"]:
            info["vip_codes"].append(code)
        is_paid = bool(state_data.get("is_paid")) and int(state_data.get("price") or 0) > 0
        if is_paid:
            # Pullik kino: narx allaqachon belgilangan — darhol e'lon qilinadi
            info["movies"][code]["access"] = "paid"
            info["movies"][code]["price"] = int(state_data["price"])
            if code in info["vip_codes"]:
                info["vip_codes"].remove(code)
        elif not is_vip:
            # Hammaga / Premium / Pullik tanlanmaguncha e'lon (autopost, bildirishnoma) kechiktiriladi
            info["movies"][code]["pending_announce"] = True
        save_data()
        await state.clear()
        if is_paid:
            if info.get("autopost_channels"):
                asyncio.create_task(kino_autopost_movie(token, code))
            if info.get("new_content_notify"):
                await notify_new_content(message.bot, f"💰 Yangi pullik film qo'shildi: Kod {code}")
            await message.answer(
                f"✅ Kod <b>{code}</b> bilan film saqlandi.\n"
                f"💰 Pullik kino — narxi <b>{fmt_som(info['movies'][code]['price'])} so'm</b>. "
                "Foydalanuvchilar uni alohida sotib oladi."
            )
            await send_movie_menu(message, code)
        elif is_vip:
            if info.get("autopost_channels"):
                asyncio.create_task(kino_autopost_movie(token, code))
            if info.get("new_content_notify"):
                await notify_new_content(message.bot, f"🔒 Yangi VIP film qo'shildi: Kod {code}")
            await message.answer(
                f"✅ Kod <b>{code}</b> bilan film saqlandi.\n🔒 Bu film VIP-maxsus (faqat Premium foydalanuvchilar ko'radi)."
            )
        else:
            await send_movie_menu(message, code)

    # ---------- Ko'p kino yuklash ----------
    bulk_lock = asyncio.Lock()

    @dp.message(F.text == "📦 Ko'p kino yuklash")
    async def bulk_movies_start(message: Message, state: FSMContext):
        if not is_moderator(message.from_user.id):
            return
        await message.answer(
            "📦 <b>Ko'p kino yuklash</b>\n\n"
            "Boshlang'ich kodni yuboring (faqat raqam, masalan: 40).\n\n"
            "Keyin kinolarni ketma-ket <b>forward (uzatib)</b> yuboring — kodlar 40, 41, 42... deb "
            "avtomatik beriladi (band kodlar o'tkazib yuboriladi). Tavsif sifatida videoning izohi olinadi.\n\n"
            "Tugatish uchun /done yozing."
        )
        await state.set_state(AddMoviesBulk.waiting_start)

    @dp.message(AddMoviesBulk.waiting_start)
    async def bulk_movies_start_code(message: Message, state: FSMContext):
        code = (message.text or "").strip()
        if not code.isdigit():
            await message.answer("❌ Kod faqat raqamlardan iborat bo'lishi kerak. Qaytadan yuboring:")
            return
        await state.update_data(next_code=int(code), saved=[])
        await message.answer(
            f"✅ Boshlang'ich kod: <b>{code}</b>.\nEndi kinolarni ketma-ket forward qiling. Tugatish: /done"
        )
        await state.set_state(AddMoviesBulk.waiting_video)

    @dp.message(AddMoviesBulk.waiting_video, Command("done"))
    async def bulk_movies_done(message: Message, state: FSMContext):
        async with bulk_lock:
            sd = await state.get_data()
            await state.clear()
        saved = sd.get("saved", [])
        if not saved:
            await message.answer("Hech qanday kino saqlanmadi.", reply_markup=content_menu_kb())
            return
        if info.get("new_content_notify"):
            await notify_new_content(message.bot, f"🎬 Yangi filmlar qo'shildi: {len(saved)} ta (Kod {saved[0]} — {saved[-1]})")
        await message.answer(
            f"✅ Jami <b>{len(saved)}</b> ta kino saqlandi (kodlar: {saved[0]} — {saved[-1]}).",
            reply_markup=content_menu_kb(),
        )

    @dp.message(AddMoviesBulk.waiting_video, F.video)
    async def bulk_movies_video(message: Message, state: FSMContext):
        if not is_forwarded_message(message):
            await message.answer(FORWARD_ONLY_TEXT)
            return
        async with bulk_lock:
            sd = await state.get_data()
            n = int(sd.get("next_code", 1))
            while str(n) in info["movies"]:
                n += 1
            code = str(n)
            desc = (message.caption or "").strip() or f"Kino {code}"
            info["movies"][code] = {
                "file_id": message.video.file_id, "desc": desc,
                "added_at": datetime.now().isoformat(timespec="seconds"),
            }
            first_line = next((ln.strip() for ln in (message.caption or "").splitlines() if ln.strip()), "")
            if first_line:
                info["movies"][code]["title"] = first_line[:100]   # nom orqali qidiruv uchun
            saved = list(sd.get("saved", []))
            saved.append(code)
            await state.update_data(next_code=n + 1, saved=saved)
            save_data()
        if info.get("autopost_channels"):
            asyncio.create_task(kino_autopost_movie(token, code))
        await message.answer(f"✅ Kod <b>{code}</b> saqlandi ({len(saved)}-kino). Keyingisini yuboring yoki /done.")

    @dp.message(AddMoviesBulk.waiting_video)
    async def bulk_movies_wrong(message: Message):
        await message.answer("❌ Iltimos, video forward qiling yoki tugatish uchun /done yozing.")

    # ---------- Kino yuklash menyusi: Hammaga / Premium / Pullik ----------
    def fmt_som(n) -> str:
        return f"{int(n):,}".replace(",", " ")

    def access_of(code: str) -> str:
        e = info["movies"].get(code, {})
        if e.get("access") == "paid" and e.get("price"):
            return "paid"
        return "premium" if code in info["vip_codes"] else "free"

    def has_purchased(uid: int, code: str) -> bool:
        return code in info.get("movie_purchases", {}).get(str(uid), [])

    def movie_menu_caption(code: str) -> str:
        e = info["movies"][code]
        pending = bool(e.get("pending_announce"))
        lines = ["🎞 <b>Kino yuklash jarayoni</b>" if pending else "📝 <b>Kino sozlamalari</b>", "",
                 f"🔎 <b>Kino kodi:</b> {html_escape(str(code))}"]
        if e.get("title"):
            lines.append(f"🏷 <b>Nomi:</b> {html_escape(e['title'])}")
        if not pending:
            a = access_of(code)
            cur = {
                "free": "🔓 Hammaga (bepul)",
                "premium": "💎 Faqat Premium",
                "paid": f"💰 Pullik — {fmt_som(e.get('price', 0))} so'm",
            }[a]
            lines.append(f"📌 <b>Holati:</b> {cur}")
        q = ["🔓 <b>Hammaga yuklash</b> — Kinoni barcha foydalanuvchilar ko'ra oladi."]
        if is_pro():
            q.append("💎 <b>Faqat Premium</b> — Kinoni faqat premium obunasiga ega foydalanuvchilar ko'ra oladi.")
        q.append("💰 <b>Pullik</b> — Kinoni faqat sotib olgan foydalanuvchilar ko'ra oladi (narxni siz belgilaysiz).")
        lines += ["", "<blockquote>" + "\n".join(q) + "</blockquote>"]
        return "\n".join(lines)

    def movie_menu_kb(code: str) -> InlineKeyboardMarkup:
        e = info["movies"][code]
        rows = [
            [InlineKeyboardButton(text=("✏️ Nomni o'zgartirish" if e.get("title") else "➕ Nom kiritish"), callback_data=f"kmv_name_{code}")],
            [InlineKeyboardButton(text="📥 Yuklash: 🔓 Hammaga", callback_data=f"kmv_free_{code}")],
        ]
        if is_pro():
            rows.append([InlineKeyboardButton(text="📥 Yuklash: 💎 Premium", callback_data=f"kmv_prem_{code}")])
        rows.append([InlineKeyboardButton(text="📥 Yuklash: 💰 Pullik", callback_data=f"kmv_paid_{code}")])
        if not e.get("pending_announce"):
            rows.append([InlineKeyboardButton(text="📝 Tavsifni tahrirlash", callback_data=f"kmv_desc_{code}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def send_movie_menu(target: Message, code: str):
        e = info["movies"][code]
        await target.answer_video(e["file_id"], caption=movie_menu_caption(code), reply_markup=movie_menu_kb(code))

    async def refresh_movie_menu(bot, chat_id, msg_id, code: str):
        if not msg_id or code not in info["movies"]:
            return
        try:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=msg_id,
                caption=movie_menu_caption(code), reply_markup=movie_menu_kb(code),
            )
        except Exception:
            pass

    async def apply_movie_access(bot, code: str, access: str, price: int = 0) -> bool:
        e = info["movies"].get(code)
        if not e:
            return False
        if access == "paid":
            e["access"] = "paid"
            e["price"] = int(price)
            if code in info["vip_codes"]:
                info["vip_codes"].remove(code)
        else:
            e.pop("access", None)
            e.pop("price", None)
            if access == "premium":
                if code not in info["vip_codes"]:
                    info["vip_codes"].append(code)
            elif code in info["vip_codes"]:
                info["vip_codes"].remove(code)
        announce = e.pop("pending_announce", False)
        save_data()
        if announce:
            if info.get("autopost_channels"):
                asyncio.create_task(kino_autopost_movie(token, code))
            if info.get("new_content_notify"):
                label = {"free": "🎬 Yangi film", "premium": "🔒 Yangi VIP film", "paid": "💰 Yangi pullik film"}[access]
                await notify_new_content(bot, f"{label} qo'shildi: Kod {code}")
        return True

    @dp.callback_query(F.data.startswith("kmv_"))
    async def movie_menu_cb(callback: CallbackQuery, state: FSMContext):
        if not is_moderator(callback.from_user.id):
            await callback.answer()
            return
        _, action, code = callback.data.split("_", 2)
        e = info["movies"].get(code)
        if not e:
            await callback.answer("❌ Kino topilmadi.", show_alert=True)
            return
        chat_id = callback.message.chat.id
        msg_id = callback.message.message_id
        if action == "name":
            await state.update_data(mv_code=code, mv_msg=msg_id)
            await state.set_state(MovieAccessFlow.waiting_title)
            await callback.message.answer("🏷 Kino nomini yozing:")
        elif action in ("free", "prem"):
            if action == "prem" and not is_pro():
                await callback.answer("💎 Premium faqat Kino BOT uchun mavjud.", show_alert=True)
                return
            access = "premium" if action == "prem" else "free"
            await apply_movie_access(callback.bot, code, access)
            await refresh_movie_menu(callback.bot, chat_id, msg_id, code)
            if access == "premium":
                await callback.message.answer(f"✅ Kod <b>{code}</b> yuklandi: 💎 faqat Premium foydalanuvchilar uchun.")
            else:
                await callback.message.answer(f"✅ Kod <b>{code}</b> yuklandi: 🔓 hammaga (bepul).")
        elif action == "paid":
            await state.update_data(mv_code=code, mv_msg=msg_id)
            await state.set_state(MovieAccessFlow.waiting_price)
            await callback.message.answer("💰 Kino narxini so'mda yozing (masalan: 15000):")
        elif action == "desc":
            await state.update_data(edit_code=code)
            await callback.message.answer("Yangi tavsifni kiriting:")
            await state.set_state(StarPackageEdit.waiting_price)
        await callback.answer()

    @dp.message(MovieAccessFlow.waiting_title, F.text)
    async def movie_title_save(message: Message, state: FSMContext):
        sd = await state.get_data()
        code = sd.get("mv_code")
        await state.clear()
        e = info["movies"].get(code)
        if not e:
            await message.answer("❌ Kino topilmadi.")
            return
        title = message.text.strip()[:100]
        e["title"] = title
        save_data()
        await message.answer(f"✅ Nom saqlandi: <b>{html_escape(title)}</b>")
        await refresh_movie_menu(message.bot, message.chat.id, sd.get("mv_msg"), code)

    @dp.message(MovieAccessFlow.waiting_price, F.text)
    async def movie_price_save(message: Message, state: FSMContext):
        digits = re.sub(r"\D", "", message.text or "")
        if not digits or int(digits) < 1000:
            await message.answer("❌ Narxni raqamda yozing (kamida 1000 so'm), masalan: 15000")
            return
        price = int(digits)
        sd = await state.get_data()
        code = sd.get("mv_code")
        await state.clear()
        if not await apply_movie_access(message.bot, code, "paid", price):
            await message.answer("❌ Kino topilmadi.")
            return
        await message.answer(f"✅ Kod <b>{code}</b> yuklandi: 💰 pullik — <b>{fmt_som(price)} so'm</b>.")
        await refresh_movie_menu(message.bot, message.chat.id, sd.get("mv_msg"), code)

    # ---------- Pullik kinoni sotib olish (mijoz) ----------
    def grant_movie(uid: int, code: str, price: int, src: str):
        lst = info.setdefault("movie_purchases", {}).setdefault(str(uid), [])
        if code not in lst:
            lst.append(code)
        stat = info.setdefault("movie_sales", {}).setdefault(code, {"count": 0, "revenue": 0})
        stat["count"] += 1
        stat["revenue"] += price
        log_kino_payment(info, uid, {"name": f"Kino #{code}", "price": price, "days": 0}, src)
        save_data()

    async def deliver_purchased_movie(bot, uid: int, code: str):
        e = info["movies"].get(code)
        if not e or not e.get("file_id"):
            return
        caption = f"🎬 Kod: {code}"
        if e.get("desc"):
            caption += f"\n\n{e['desc']}"
        try:
            await bot.send_video(uid, e["file_id"], caption=caption, protect_content=bool(info.get("protect_content")))
        except Exception as ex:
            logging.error(f"Sotib olingan kinoni yuborishda xato: {ex}")

    async def _auto_fulfill_movie(bot, order):
        code = order["ref"]
        e = info["movies"].get(code)
        if not e:
            raise RuntimeError("kino topilmadi")
        uid = int(order["uid"])
        grant_movie(uid, code, int(order["amount"]), AUTO_PROVIDERS[order["provider"]]["title"])
        await bot.send_message(uid, "✅ <b>To'lov qabul qilindi!</b> Kino ochildi 🎉")
        await deliver_purchased_movie(bot, uid, code)

    AUTO_FULFILL[(token, "kmovie")] = _auto_fulfill_movie

    async def send_paid_offer(message: Message, code: str, entry: dict):
        price = int(entry.get("price", 0))
        title = entry.get("title") or f"Kod {code}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"💳 Sotib olish — {fmt_som(price)} so'm", callback_data=f"kbuy_{code}")
        ]])
        await message.answer(
            "💰 <b>Pullik kino</b>\n\n"
            f"🎬 {html_escape(title)}\n"
            f"💵 Narxi: <b>{fmt_som(price)} so'm</b> (~{somz_to_stars(price)} ⭐)\n\n"
            "To'lovdan so'ng kino sizga ochiladi.",
            reply_markup=kb,
        )

    @dp.callback_query(F.data.startswith("kbuy_"))
    async def movie_buy_cb(callback: CallbackQuery):
        code = callback.data[5:]
        e = info["movies"].get(code)
        if not e or e.get("access") != "paid" or not e.get("price"):
            await callback.answer("❌ Bu kino endi pullik emas yoki topilmadi.", show_alert=True)
            return
        if has_purchased(callback.from_user.id, code):
            await callback.answer("✅ Siz bu kinoni allaqachon sotib olgansiz.", show_alert=True)
            return
        price = int(e["price"])
        stars = somz_to_stars(price)
        rows = []
        _bal = int(info.get("ref_balance", {}).get(str(callback.from_user.id), 0) or 0)
        if _bal >= price:
            rows.append([InlineKeyboardButton(
                text=f"🎁 Bonus balansdan to'lash ({fmt_som(price)} so'm)", callback_data=f"kbbonus_{code}")])
        rows.append([InlineKeyboardButton(text=f"⭐ {stars} Stars orqali to'lash", callback_data=f"kbstars_{code}")])
        rows += [
            [InlineKeyboardButton(text=pm["name"], callback_data=f"kbpay_{code}_{pid}")]
            for pid, pm in info["payment_systems"].items()
        ]
        rows += auto_pay_buttons(info, "kmovie", code)
        await callback.message.answer(
            "💳 <b>To'lov tizimini tanlang</b>\n\n"
            f"🎬 {html_escape(e.get('title') or 'Kod ' + code)}\n"
            f"💰 Narx: {fmt_som(price)} so'm (~{stars} ⭐)",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("kbstars_"))
    async def movie_stars_cb(callback: CallbackQuery):
        code = callback.data.split("_", 1)[1]
        e = info["movies"].get(code)
        if not e or e.get("access") != "paid" or not e.get("price"):
            await callback.answer("❌ Kino topilmadi.", show_alert=True)
            return
        stars = somz_to_stars(int(e["price"]))
        title = (e.get("title") or f"Kino {code}")[:30]
        await callback.bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"🎬 {title}",
            description=f"Pullik kino (kod {code}) — doimiy ochiladi",
            payload=f"kmovie_{code}",
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=stars)],
        )
        await callback.answer()

    @dp.message(F.successful_payment.invoice_payload.startswith("kmovie_"))
    async def movie_stars_success(message: Message):
        code = message.successful_payment.invoice_payload.split("_", 1)[1]
        e = info["movies"].get(code)
        if not e:
            await message.answer("❌ Xatolik: kino topilmadi. Administratorga murojaat qiling.")
            return
        grant_movie(message.from_user.id, code, int(e.get("price", 0)), "Stars")
        await message.answer("✅ <b>To'lov qabul qilindi!</b> Kino ochildi 🎉")
        await deliver_purchased_movie(message.bot, message.from_user.id, code)

    @dp.callback_query(F.data.startswith("kbpay_"))
    async def movie_pay_chosen_cb(callback: CallbackQuery, state: FSMContext):
        _, code, pid = callback.data.split("_", 2)
        e = info["movies"].get(code)
        psys = info["payment_systems"].get(pid)
        if not e or not psys or not e.get("price"):
            await callback.answer("❌ Ma'lumot topilmadi, qaytadan urinib ko'ring.", show_alert=True)
            return
        await state.update_data(kb_code=code, kb_pid=pid)
        await state.set_state(MoviePurchase.waiting_check)
        await callback.message.answer(
            f"💳 <b>{psys['name']}</b>\n\n"
            f"🔢 Raqami: <code>{psys['number']}</code>\n"
            f"👤 Egasi: {psys['owner']}\n\n"
            f"💰 To'lov summasi: {fmt_som(e['price'])} so'm\n\n"
            "To'lovni amalga oshirgach, to'lov chekini (skrinshot) shu yerga yuboring.\n"
            "Bekor qilish: /bekor"
        )
        await callback.answer()

    @dp.message(MoviePurchase.waiting_check, Command("bekor"))
    async def movie_check_cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("❌ Bekor qilindi.")

    @dp.message(MoviePurchase.waiting_check, F.photo)
    async def movie_check_received(message: Message, state: FSMContext):
        sd = await state.get_data()
        code, pid = sd.get("kb_code"), sd.get("kb_pid")
        e = info["movies"].get(code)
        psys = info["payment_systems"].get(pid)
        if not e or not psys or not e.get("price"):
            await message.answer("❌ Ma'lumot topilmadi, qaytadan urinib ko'ring.")
            await state.clear()
            return
        uid = message.from_user.id
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        caption = (
            "🧾 <b>Pullik kino to'lovi</b>\n\n"
            f"🎬 Kod: {code} — {html_escape(e.get('title') or e.get('desc', '-')[:30])}\n"
            f"💰 Narx: {fmt_som(e['price'])} so'm\n"
            f"💳 To'lov tizimi: {psys['name']}\n"
            f"👤 Foydalanuvchi: {html_escape(uname)} (ID: <code>{uid}</code>)"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"kbok_{uid}_{code}"),
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"kbno_{uid}"),
        ]])
        for aid in info.get("admin_ids", [admin_id]):
            try:
                await message.bot.send_photo(chat_id=aid, photo=message.photo[-1].file_id, caption=caption, reply_markup=kb)
            except Exception as ex:
                logging.error(f"Adminga chek yuborishda xato ({aid}): {ex}")
        await message.answer(
            "✅ Chekingiz qabul qilindi!\n\nAdmin tekshirgach, kino sizga ochiladi."
        )
        await state.clear()

    @dp.message(MoviePurchase.waiting_check)
    async def movie_check_wrong(message: Message):
        await message.answer("❌ Iltimos, to'lov chekining rasmini (skrinshot) yuboring. Bekor qilish: /bekor")

    @dp.callback_query(F.data.startswith("kbok_"))
    async def movie_approve_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        _, uid_s, code = callback.data.split("_", 2)
        uid = int(uid_s)
        e = info["movies"].get(code)
        if not e:
            await callback.answer("❌ Kino topilmadi.", show_alert=True)
            return
        if has_purchased(uid, code):
            await callback.answer("Bu to'lov allaqachon tasdiqlangan.", show_alert=True)
            return
        grant_movie(uid, code, int(e.get("price", 0)), "chek")
        try:
            await callback.bot.send_message(uid, "✅ <b>To'lovingiz tasdiqlandi!</b> Kino ochildi 🎉")
        except Exception as ex:
            logging.error(f"Foydalanuvchiga xabar yuborishda xato: {ex}")
        await deliver_purchased_movie(callback.bot, uid, code)
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ <b>TASDIQLANDI</b>")
        await callback.answer()

    @dp.callback_query(F.data.startswith("kbno_"))
    async def movie_reject_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        uid = int(callback.data.split("_", 1)[1])
        try:
            await callback.bot.send_message(
                uid, "❌ <b>To'lovingiz admin tomonidan bekor qilindi.</b>\n\nSavollar bo'lsa, administrator bilan bog'laning."
            )
        except Exception as ex:
            logging.error(f"Foydalanuvchiga rad javobini yuborishda xato: {ex}")
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ <b>BEKOR QILINDI</b>")
        await callback.answer()

    @dp.message(AddMovie.waiting_video)
    async def addmovie_wrong(message: Message):
        await message.answer("❌ Iltimos, video fayl yuboring (forward qilingan bo'lsa ham bo'ladi).")

    @dp.message(F.text == "📋 Filmlar ro'yxati")
    @dp.message(F.text == "📋 Kinolar ro'yxati")
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
        chunks, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) + 1 > 3500:
                chunks.append(cur)
                cur = ""
            cur += ln + "\n"
        if cur:
            chunks.append(cur)
        for i, ch in enumerate(chunks):
            head = f"📋 <b>Kinolar ({len(lines)} ta):</b>\n\n" if i == 0 else ""
            await message.answer(head + ch)

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
    @dp.message(F.text == "📝 Kino tahrirlash")
    async def edit_desc_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"]:
            await message.answer("Kontent yo'q.")
            return
        await message.answer("Tahrirlamoqchi bo'lgan kino kodini tanlang:", reply_markup=code_pick_kb("editdesc_"))

    @dp.callback_query(F.data.startswith("editdesc_"))
    async def edit_desc_pick(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        code = callback.data.split("_", 1)[1]
        entry = info["movies"].get(code)
        if entry and entry.get("type") != "series" and entry.get("file_id"):
            await send_movie_menu(callback.message, code)
            await callback.answer()
            return
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
    @dp.message(F.text == "🗑 Kino o'chirish")
    async def del_movie_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"]:
            await message.answer("O'chirish uchun film yo'q.")
            return
        await message.answer("O'chirmoqchi bo'lgan kino kodini tanlang:", reply_markup=code_pick_kb("delmovie_"))

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
        await callback.message.answer("Yangi qism videosini <b>forward (uzatib)</b> yuboring:")
        await state.set_state(AppendEpisode.waiting_video)
        await callback.answer()

    @dp.message(AppendEpisode.waiting_video, F.video)
    async def append_episode_video(message: Message, state: FSMContext):
        if not is_forwarded_message(message):
            await message.answer(FORWARD_ONLY_TEXT)
            return
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
        await message.answer("❌ Forward qilingan video yuboring.")

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

    # ---------- Pullik kontent ----------
    @dp.message(F.text == "💰 Pullik qilib belgilash")
    async def paid_mark_start(message: Message):
        if not is_moderator(message.from_user.id):
            return
        items = [(c, m) for c, m in info["movies"].items() if m.get("file_id")]
        if not items:
            await message.answer("Film yo'q.")
            return
        buttons = []
        for code, m in items[-60:][::-1]:
            paid = m.get("access") == "paid" and m.get("price")
            mark = f"💰 {fmt_som(m['price'])}" if paid else "🆓"
            name = m.get("title") or m.get("desc", "-")[:25]
            buttons.append([InlineKeyboardButton(text=f"{mark} | Kod {code} — {name}", callback_data=f"kpmark_{code}")])
        await message.answer(
            "💰 Pullik qilish uchun filmni tanlang (narx so'raladi).\n"
            "Allaqachon pullik filmni bossangiz, u yana bepul bo'ladi:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @dp.callback_query(F.data.startswith("kpmark_"))
    async def paid_mark_toggle(callback: CallbackQuery, state: FSMContext):
        if not is_moderator(callback.from_user.id):
            await callback.answer()
            return
        code = callback.data.split("_", 1)[1]
        e = info["movies"].get(code)
        if not e or not e.get("file_id"):
            await callback.answer("❌ Kino topilmadi.", show_alert=True)
            return
        if e.get("access") == "paid":
            await apply_movie_access(callback.bot, code, "free")
            await callback.answer(f"Kod {code}: 🔓 endi bepul", show_alert=True)
            return
        await state.update_data(mv_code=code, mv_msg=None)
        await state.set_state(MovieAccessFlow.waiting_price)
        await callback.message.answer(f"💰 Kod <b>{code}</b> uchun narxni so'mda yozing (masalan: 10000):")
        await callback.answer()

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
            # Oddiy foydalanuvchi "🏷 Kategoriyalar" bossa, keyingi (mijoz) handlerga o'tsin
            raise SkipHandler
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
    @dp.message(F.text == "📩 Xabar yuborish")
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

    # ---------- Sozlamalar ----------
    @dp.message(F.text == "⚙️ Sozlamalar")
    @dp.message(F.text == "📝 Matnlar")
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

    # ---------- Mijoz: kino so'rash ----------
    @dp.callback_query(F.data.startswith("kreq_"))
    async def kino_request_cb(callback: CallbackQuery):
        if not info.get("movie_request_enabled", True):
            await callback.answer("Kino so'rash o'chirilgan.", show_alert=True)
            return
        code = callback.data[5:]
        uid_r = callback.from_user.id
        reqs = info.setdefault("movie_requests", [])
        if any(r.get("uid") == uid_r and r.get("code") == code for r in reqs):
            await callback.answer("✅ Bu so'rov allaqachon yuborilgan.", show_alert=True)
            return
        reqs.append({"uid": uid_r, "code": code, "date": datetime.now().isoformat()})
        if len(reqs) > 300:
            del reqs[:-300]
        save_data()
        who = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
        for aid in info.get("admin_ids", [admin_id]):
            try:
                await callback.bot.send_message(
                    aid, f"📩 <b>Kino so'rovi</b>\n\nKod/nom: <b>{html_escape(code)}</b>\nFoydalanuvchi: {html_escape(who)} (ID {uid_r})"
                )
            except Exception:
                pass
        await callback.answer("✅ So'rovingiz adminga yuborildi.", show_alert=True)

    # =====================================================================
    #  Mijoz menyusi: Yangi kinolar / TOP / Kategoriyalar / Qidiruv / Premium /
    #  Profilim / Do'st taklif qilish / Yordam
    # =====================================================================
    NEW_FREE_COUNT = 5        # 🆕 Yangi kinolar: hamma uchun BEPUL ko'rinadigan oxirgi kinolar/seriallar soni
    NEW_PREMIUM_LIMIT = 30    # Premium foydalanuvchiga ko'rinadigan oxirgi kinolar soni
    TOP_COUNT = 5             # 🔥 TOP kinolar soni
    CAT_LIST_COUNT = 20       # 🏷 Kategoriya ichida ko'rinadigan oxirgi kinolar soni
    SEARCH_LIMIT = 10         # 🔍 Qidiruv natijalari soni
    _bot_username = {}

    async def get_bot_username(bot) -> str:
        if not _bot_username.get("u"):
            me = await bot.get_me()
            _bot_username["u"] = me.username or ""
        return _bot_username["u"]

    def visible_codes() -> list:
        """Mijozga ko'rinadigan kontent kodlari (eskidan yangiga). E'lon qilinmagan va hali chiqmaganlar kirmaydi."""
        now = datetime.now()
        out = []
        for code, m in info["movies"].items():
            if not isinstance(m, dict) or m.get("pending_announce"):
                continue
            ra = m.get("release_at")
            if ra:
                try:
                    if datetime.fromisoformat(ra) > now:
                        continue
                except Exception:
                    pass
            out.append(code)
        return out

    def movie_title_of(code: str) -> str:
        m = info["movies"].get(code) or {}
        title = (m.get("title") or "").strip()
        if not title:
            desc = (m.get("desc") or "").strip()
            title = desc.splitlines()[0][:30] if desc else ""
        return title or f"Kod {code}"

    def access_icon(uid: int, code: str) -> str:
        e = info["movies"].get(code) or {}
        if e.get("access") == "paid" and e.get("price"):
            return "✅" if (has_purchased(uid, code) or is_moderator(uid)) else "💰"
        if is_pro() and info.get("vip_system", True) and code in info["vip_codes"]:
            return "💎" if is_premium_user(uid) else "🔒"
        return ""

    def movies_list_kb(uid: int, codes: list, free_codes=(), extra_rows=None) -> InlineKeyboardMarkup:
        """Kinolar ro'yxati tugmalari. free_codes dagi kinolar bepul ochiladi (knew_), qolganlari odatdagi tekshiruv bilan (kopen_)."""
        rows = []
        free_set = set(free_codes)
        for code in codes:
            e = info["movies"].get(code) or {}
            kind = "📺" if e.get("type") == "series" else "🎬"
            if code in free_set:
                label, prefix = f"{kind} {movie_title_of(code)}", "knew_"
            else:
                icon = access_icon(uid, code)
                label, prefix = f"{kind} {(icon + ' ') if icon else ''}{movie_title_of(code)}", "kopen_"
            rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"{prefix}{code}")])
        rows += extra_rows or []
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def norm_text(s: str) -> str:
        s = (s or "").lower()
        for ch in ("ʻ", "ʼ", "’", "‘", "`", "´"):
            s = s.replace(ch, "'")
        return " ".join(s.split())

    def search_titles(q: str) -> list:
        """Nom bo'yicha qidirish: avval nom, keyin tavsif mosligi. Yangilari oldinda."""
        tokens = norm_text(q).split()
        if not tokens or len(norm_text(q)) < 2:
            return []
        title_hits, desc_hits = [], []
        for code in visible_codes():
            m = info["movies"][code]
            title = norm_text(m.get("title") or "")
            if title and all(t in title for t in tokens):
                title_hits.append(code)
                continue
            if all(t in (title + " " + norm_text(m.get("desc") or "")) for t in tokens):
                desc_hits.append(code)
        return (list(reversed(title_hits)) + list(reversed(desc_hits)))[:SEARCH_LIMIT]

    # ---------- Referal: havola orqali kelgan do'st majburiy obunadan o'tgach hisoblanadi ----------
    async def settle_referral(bot, uid: int):
        pend = info.get("ref_pending") or {}
        ref_uid = pend.pop(str(uid), None)
        if not ref_uid:
            return
        lst = info.setdefault("referrals", {}).setdefault(str(ref_uid), [])
        if uid in lst:
            save_data()
            return
        lst.append(uid)
        bonus = 0
        if info.get("ref_bonus_enabled"):
            try:
                bonus = max(0, int(info.get("ref_bonus_amount", 0) or 0))
            except Exception:
                bonus = 0
            if bonus:
                bal = info.setdefault("ref_balance", {})
                bal[str(ref_uid)] = int(bal.get(str(ref_uid), 0) or 0) + bonus
        save_data()
        text = "🎉 <b>Yangi do'st!</b> Sizning havolangiz orqali botga yangi foydalanuvchi qo'shildi."
        if bonus:
            text += f"\n🎁 Bonus: <b>+{fmt_som(bonus)} so'm</b>"
        try:
            await bot.send_message(ref_uid, text)
        except Exception:
            pass

    # ---------- Umumiy tekshiruv (xabar va callback uchun) ----------
    async def kino_guard(message: Message) -> bool:
        uid = message.from_user.id
        if not await check_active(message, info, admin_id):
            return False
        if is_blocked(uid):
            await message.answer("🚫 Siz ushbu botdan foydalanish huquqidan mahrum qilingansiz.")
            return False
        if info.get("maintenance_mode") and not is_admin(info, uid):
            await message.answer("🛠 Bot hozircha texnik tanaffusda. Birozdan so'ng qayta urinib ko'ring.")
            return False
        if not await require_subscription(message, info, admin_id):
            return False
        await settle_referral(message.bot, uid)
        return True

    async def kino_guard_cb(callback: CallbackQuery) -> bool:
        uid = callback.from_user.id
        if not await check_active(callback, info, admin_id):
            return False
        if is_blocked(uid):
            await callback.answer("🚫 Siz ushbu botdan foydalanish huquqidan mahrum qilingansiz.", show_alert=True)
            return False
        if info.get("maintenance_mode") and not is_admin(info, uid):
            await callback.answer("🛠 Bot hozircha texnik tanaffusda.", show_alert=True)
            return False
        if not await require_subscription(callback, info, admin_id):
            return False
        await settle_referral(callback.bot, uid)
        return True

    # ---------- Kinoni yuborish (kod yuborilganda ham, tugma bosilganda ham shu ishlaydi) ----------
    async def deliver_entry(target: Message, uid: int, code: str, entry: dict):
        info["stats"]["requests"] += 1
        info["request_counts"][code] = info["request_counts"].get(code, 0) + 1
        _kino_count_view(info)
        wc = info.setdefault("week_counts", {})
        wc[code] = wc.get(code, 0) + 1
        info["user_activity"][str(uid)] = info["user_activity"].get(str(uid), 0) + 1
        save_data()

        if entry.get("type") == "series":
            await send_series_episode(target.answer_video, entry, code, 1, uid=uid)
        else:
            caption = f"🎬 Kod: {code}"
            if entry.get("desc"):
                caption += f"\n\n{entry['desc']}"
            rate_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=str(n), callback_data=f"rate_{code}_{n}") for n in range(1, 6)
            ]])
            await target.answer_video(
                entry["file_id"], caption=caption, reply_markup=rate_kb,
                protect_content=bool(info.get("protect_content")) and not is_admin(info, uid),
            )
        active_ads = [a for a in info["ads"].values() if a.get("active")]
        if active_ads:
            import random
            ad = random.choice(active_ads)
            await target.answer(f"📣 {ad['text']}")

    async def open_movie(target: Message, uid: int, code: str, free_pass: bool = False):
        """Ro'yxatdagi tugma bosilganda kinoni ochadi. free_pass=True — VIP/pullik tekshiruvisiz (🆕 Yangi kinolar)."""
        entry = info["movies"].get(code)
        if not entry or entry.get("pending_announce"):
            await target.answer("❌ Bunday kontent topilmadi.")
            return
        if not free_pass:
            if is_pro() and info.get("vip_system", True) and code in info["vip_codes"] and not is_premium_user(uid):
                vip_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="💎 Premium sotib olish", callback_data="buy_premium")]])
                await target.answer("🔒 Bu kontent faqat <b>VIP (Premium)</b> foydalanuvchilar uchun.", reply_markup=vip_kb)
                return
            if (entry.get("access") == "paid" and entry.get("price")
                    and not is_moderator(uid) and not has_purchased(uid, code)):
                await send_paid_offer(target, code, entry)
                return
        release_at = entry.get("release_at")
        if release_at and not is_admin(info, uid):
            try:
                dt = datetime.fromisoformat(release_at)
                if dt > datetime.now():
                    await target.answer(f"🗓 Bu kontent hali chiqmagan. Chiqish sanasi: {dt.strftime('%d.%m.%Y %H:%M')}")
                    return
            except Exception:
                pass
        await deliver_entry(target, uid, code, entry)

    async def ensure_menu(message: Message, uid: int):
        """Eski foydalanuvchilarga yangi menyuni bir marta yuboradi (ular /start bosmagan bo'lsa ham)."""
        seen = info.setdefault("menu_v2", {})
        if str(uid) in seen or is_admin(info, uid):
            return
        seen[str(uid)] = 1
        save_data()
        try:
            await message.answer("📋 Bot menyusi yangilandi — pastdagi tugmalardan foydalaning 👇", reply_markup=customer_menu_kb())
        except Exception:
            pass

    # ---------- 🆕 Yangi kinolar ----------
    @dp.message(F.text == "🆕 Yangi kinolar")
    async def kino_new_list(message: Message, state: FSMContext):
        await state.clear()
        if not await kino_guard(message):
            return
        uid = message.from_user.id
        codes = visible_codes()
        if not codes:
            await message.answer("🆕 Hozircha kontent yo'q.")
            return
        free_codes = list(reversed(codes[-NEW_FREE_COUNT:]))
        extra = []
        if is_premium_user(uid):
            shown = list(reversed(codes[-NEW_PREMIUM_LIMIT:]))
            text = (f"🆕 <b>Yangi kinolar</b> — so'nggi {len(shown)} ta\n\n"
                    f"💎 Siz Premium foydalanuvchisiz — barcha yangi kinolar ro'yxati ochiq.")
        else:
            shown = free_codes
            text = (f"🆕 <b>Yangi kinolar</b> — oxirgi {len(shown)} ta\n\n"
                    "🆓 Bu kinolarni hamma <b>bepul</b> ko'ra oladi.")
            if is_pro():
                text += "\n💎 Barcha yangi kinolar ro'yxati Premium obuna bilan ochiladi."
                extra.append([InlineKeyboardButton(text="💎 Premium sotib olish", callback_data="buy_premium")])
        await message.answer(text, reply_markup=movies_list_kb(uid, shown, free_codes=free_codes, extra_rows=extra))

    # ---------- 🔥 TOP kinolar ----------
    @dp.message(F.text == "🔥 TOP kinolar")
    async def kino_top_list(message: Message, state: FSMContext):
        await state.clear()
        if not await kino_guard(message):
            return
        uid = message.from_user.id
        vis = set(visible_codes())
        ranked = sorted(
            ((c, n) for c, n in info["request_counts"].items() if c in vis and n > 0),
            key=lambda x: x[1], reverse=True,
        )[:TOP_COUNT]
        if not ranked:
            await message.answer("🔥 Hozircha TOP shakllanmagan — kinolar ko'rila boshlagach shu yerda chiqadi.")
            return
        lines = [f"{i}. {html_escape(movie_title_of(c))} — 👁 {n}" for i, (c, n) in enumerate(ranked, 1)]
        await message.answer(
            f"🔥 <b>TOP {len(ranked)} kino</b>\n\n" + "\n".join(lines) + "\n\nOchish uchun tanlang 👇",
            reply_markup=movies_list_kb(uid, [c for c, _ in ranked]),
        )

    # ---------- 🏷 Kategoriyalar ----------
    def cat_menu_kb() -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text=f"🏷 {name}"[:60], callback_data=f"kcat_{cid}")]
                for cid, name in info["categories"].items()]
        rows.append([InlineKeyboardButton(text=f"📚 Oxirgi {CAT_LIST_COUNT} ta kino", callback_data="kcat_all")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def cat_list_content(uid: int, cid: str):
        """(matn, klaviatura) — kategoriya (yoki hammasi) bo'yicha oxirgi CAT_LIST_COUNT ta kino."""
        if cid == "all":
            title = f"📚 Oxirgi {CAT_LIST_COUNT} ta kino"
            codes = visible_codes()
        else:
            name = info["categories"].get(cid)
            if name is None:
                return None, None
            title = f"🏷 {name}"
            codes = [c for c in visible_codes() if info["movies"][c].get("category") == cid]
        shown = list(reversed(codes[-CAT_LIST_COUNT:]))
        if shown:
            text = f"<b>{html_escape(title)}</b> — oxirgi {len(shown)} ta\n\nOchish uchun tanlang 👇"
        else:
            text = f"<b>{html_escape(title)}</b>\n\nBu bo'limda hozircha kino yo'q."
        back = [[InlineKeyboardButton(text="◀️ Kategoriyalar", callback_data="kcatmenu")]] if info["categories"] else []
        return text, movies_list_kb(uid, shown, extra_rows=back)

    @dp.message(F.text == "🏷 Kategoriyalar")
    async def kino_categories(message: Message, state: FSMContext):
        await state.clear()
        if not await kino_guard(message):
            return
        uid = message.from_user.id
        if not info["categories"]:
            text, kb = cat_list_content(uid, "all")
            await message.answer(text, reply_markup=kb)
            return
        await message.answer("🏷 <b>Kategoriyalar</b>\n\nKategoriyani tanlang 👇", reply_markup=cat_menu_kb())

    @dp.callback_query(F.data == "kcatmenu")
    async def kino_cat_menu_cb(callback: CallbackQuery):
        if not await kino_guard_cb(callback):
            return
        try:
            await callback.message.edit_text("🏷 <b>Kategoriyalar</b>\n\nKategoriyani tanlang 👇", reply_markup=cat_menu_kb())
        except TelegramBadRequest:
            pass
        await callback.answer()

    @dp.callback_query(F.data.startswith("kcat_"))
    async def kino_cat_cb(callback: CallbackQuery):
        if not await kino_guard_cb(callback):
            return
        text, kb = cat_list_content(callback.from_user.id, callback.data[5:])
        if text is None:
            await callback.answer("Kategoriya topilmadi.", show_alert=True)
            return
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await callback.answer()

    # ---------- Ro'yxatdagi kinoni ochish ----------
    @dp.callback_query(F.data.startswith("kopen_"))
    @dp.callback_query(F.data.startswith("knew_"))
    async def kino_open_cb(callback: CallbackQuery):
        if not await kino_guard_cb(callback):
            return
        free = callback.data.startswith("knew_")
        code = callback.data.split("_", 1)[1]
        await callback.answer()
        await open_movie(callback.message, callback.from_user.id, code, free_pass=free)

    # ---------- 🔍 Nom bo'yicha qidirish ----------
    async def send_search_results(message: Message, q: str):
        uid = message.from_user.id
        res = search_titles(q)
        if not res:
            kb = None
            if (info.get("movie_request_enabled", True) and q and not q.startswith("/")
                    and len(("kreq_" + q).encode("utf-8")) <= 64):
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📩 Shu kinoni so'rash", callback_data=f"kreq_{q}")]])
            await message.answer("❌ Bunday nomli kino topilmadi.", reply_markup=kb)
            return
        await message.answer(
            f"🔍 <b>«{html_escape(q[:40])}»</b> bo'yicha {len(res)} ta natija:",
            reply_markup=movies_list_kb(uid, res),
        )

    @dp.message(F.text == "🔍 Nom bo'yicha qidirish")
    async def kino_search_start(message: Message, state: FSMContext):
        await state.clear()
        if not await kino_guard(message):
            return
        await state.set_state(KinoSearch.waiting_query)
        await message.answer("🔍 Kino nomini yozing (masalan: Titanik).\nKino kodi ham bo'lishi mumkin.")

    @dp.message(KinoSearch.waiting_query, F.text)
    async def kino_search_query(message: Message, state: FSMContext):
        q = (message.text or "").strip()
        if q.startswith("/"):
            await state.clear()
            return
        if not await kino_guard(message):
            return
        if len(norm_text(q)) < 2 and not q.isdigit():
            await message.answer("✍️ Kamida 2 ta belgi yozing:")
            return
        await state.clear()
        if q.isdigit() and q in info["movies"]:
            await open_movie(message, message.from_user.id, q)
            return
        await send_search_results(message, q)

    # ---------- 💎 Premium ----------
    @dp.message(F.text == "💎 Premium")
    async def kino_premium_info(message: Message, state: FSMContext):
        await state.clear()
        if not is_pro():
            return
        if not await kino_guard(message):
            return
        uid = message.from_user.id
        can_buy = bool(info.get("premium_enabled")) and bool(info.get("premium_tariffs"))
        rows = []
        if is_admin(info, uid):
            text = "👑 Siz adminsiz — barcha kontent siz uchun ochiq."
        elif is_premium_active(info, uid):
            until = datetime.fromisoformat(info["premium_users"][str(uid)]["until"])
            days = int((until - datetime.now()).total_seconds() // 86400) + 1
            text = (f"💎 <b>Premium faol</b>\n\n📅 Muddati: <b>{until.strftime('%d.%m.%Y %H:%M')}</b> gacha "
                    f"({days} kun qoldi)\n\n"
                    "Sizga ochiq: 🔒 VIP kinolar, 🆕 barcha yangi kinolar ro'yxati va majburiy obunasiz foydalanish.")
            if can_buy:
                rows.append([InlineKeyboardButton(text="🔄 Muddatni uzaytirish", callback_data="buy_premium")])
        else:
            text = ("💎 <b>Premium obuna</b>\n\n"
                    "Premium orqali siz:\n"
                    "• 🔒 VIP kinolarni ko'rasiz\n"
                    "• 🆕 Barcha yangi kinolar ro'yxatiga ega bo'lasiz\n"
                    "• Majburiy obunasiz foydalanasiz")
            if can_buy:
                rows.append([InlineKeyboardButton(text="💎 Premium sotib olish", callback_data="buy_premium")])
            else:
                text += "\n\nHozircha Premium tariflar mavjud emas."
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

    # ---------- 👤 Profilim ----------
    @dp.message(F.text == "👤 Profilim")
    async def kino_profile(message: Message, state: FSMContext):
        await state.clear()
        if not await kino_guard(message):
            return
        uid = message.from_user.id
        lines = ["👤 <b>Profilim</b>", "", f"🆔 ID: <code>{uid}</code>"]
        if is_pro():
            if is_admin(info, uid):
                lines.append("💎 Premium: 👑 admin (cheklovsiz)")
            elif is_premium_active(info, uid):
                until = datetime.fromisoformat(info["premium_users"][str(uid)]["until"])
                days = int((until - datetime.now()).total_seconds() // 86400) + 1
                lines.append(f"💎 Premium: <b>{until.strftime('%d.%m.%Y')}</b> gacha ({days} kun qoldi)")
            else:
                lines.append("💎 Premium: yo'q")
        purchased = [c for c in info.get("movie_purchases", {}).get(str(uid), []) if c in info["movies"]]
        if purchased:
            lines.append(f"🛒 Sotib olingan pullik kinolar: <b>{len(purchased)}</b> ta")
            for c in purchased[-10:]:
                lines.append(f"• {html_escape(movie_title_of(c))} (Kod {c})")
            if len(purchased) > 10:
                lines.append(f"… va yana {len(purchased) - 10} ta")
        else:
            lines.append("🛒 Sotib olingan pullik kinolar: yo'q")
        friends = len(info.get("referrals", {}).get(str(uid), []))
        lines.append(f"👥 Taklif qilgan do'stlar: <b>{friends}</b> ta")
        bal = int(info.get("ref_balance", {}).get(str(uid), 0) or 0)
        if bal or info.get("ref_bonus_enabled"):
            lines.append(f"🎁 Bonus balans: <b>{fmt_som(bal)} so'm</b>")
        kb = movies_list_kb(uid, purchased[-10:][::-1]) if purchased else None
        await message.answer("\n".join(lines), reply_markup=kb)

    # ---------- 🔗 Do'st taklif qilish ----------
    @dp.message(F.text == "🔗 Do'st taklif qilish")
    async def kino_invite(message: Message, state: FSMContext):
        await state.clear()
        if not await kino_guard(message):
            return
        uid = message.from_user.id
        username = await get_bot_username(message.bot)
        if not username:
            await message.answer("❌ Havolani tayyorlab bo'lmadi, birozdan so'ng qayta urinib ko'ring.")
            return
        link = f"https://t.me/{username}?start=ref_{uid}"
        friends = len(info.get("referrals", {}).get(str(uid), []))
        lines = ["🔗 <b>Do'st taklif qilish</b>", "", "Sizning shaxsiy havolangiz:", f"<code>{link}</code>", "",
                 f"👥 Taklif qilgan do'stlaringiz: <b>{friends}</b> ta"]
        try:
            amount = int(info.get("ref_bonus_amount", 0) or 0)
        except Exception:
            amount = 0
        if info.get("ref_bonus_enabled") and amount > 0:
            lines += ["", f"🎁 Har bir do'st uchun <b>+{fmt_som(amount)} so'm</b> bonus beriladi "
                          "(do'stingiz majburiy kanallarga a'zo bo'lgach). Bonusni pullik kinolarni sotib olishda ishlatasiz."]
        share = "https://t.me/share/url?url=" + quote(link) + "&text=" + quote("🎬 Kinolarni shu botda ko'ring!")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Do'stlarga yuborish", url=share)]])
        await message.answer("\n".join(lines), reply_markup=kb)

    # ---------- ℹ️ Yordam ----------
    @dp.message(F.text == "ℹ️ Yordam")
    async def kino_help(message: Message, state: FSMContext):
        await state.clear()
        if not await kino_guard(message):
            return
        text = (info.get("help_text") or "").strip() or "Savol va takliflar uchun admin bilan bog'laning."
        await message.answer("ℹ️ <b>Yordam</b>\n\n" + html_escape(text))

    # ---------- Pullik kinoni bonus balansdan sotib olish ----------
    @dp.callback_query(F.data.startswith("kbbonus_"))
    async def movie_bonus_pay_cb(callback: CallbackQuery):
        code = callback.data.split("_", 1)[1]
        uid = callback.from_user.id
        e = info["movies"].get(code)
        if not e or e.get("access") != "paid" or not e.get("price"):
            await callback.answer("❌ Bu kino endi pullik emas yoki topilmadi.", show_alert=True)
            return
        if has_purchased(uid, code):
            await callback.answer("✅ Siz bu kinoni allaqachon sotib olgansiz.", show_alert=True)
            return
        price = int(e["price"])
        bal = info.setdefault("ref_balance", {})
        cur = int(bal.get(str(uid), 0) or 0)
        if cur < price:
            await callback.answer("❌ Bonus balans yetarli emas.", show_alert=True)
            return
        bal[str(uid)] = cur - price
        lst = info.setdefault("movie_purchases", {}).setdefault(str(uid), [])
        if code not in lst:
            lst.append(code)
        save_data()
        await callback.message.answer("✅ <b>Bonus balansdan to'landi!</b> Kino ochildi 🎉")
        await deliver_purchased_movie(callback.bot, uid, code)
        await callback.answer()

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
        await settle_referral(message.bot, uid)
        code = message.text.strip()
        entry = info["movies"].get(code)
        if not entry:
            # Kod emas — nom bo'yicha qidirib ko'ramiz
            if code and not code.isdigit() and not code.startswith("/") and len(code) >= 2:
                found = search_titles(code)
                if found:
                    await message.answer(
                        f"🔍 <b>«{html_escape(code[:40])}»</b> bo'yicha {len(found)} ta natija:",
                        reply_markup=movies_list_kb(uid, found),
                    )
                    return
            not_found_kb = None
            if (info.get("movie_request_enabled", True) and code and not code.startswith("/")
                    and len(("kreq_" + code).encode("utf-8")) <= 64):
                not_found_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📩 Shu kinoni so'rash", callback_data=f"kreq_{code}")
                ]])
            await message.answer("❌ Bunday kodli film topilmadi.", reply_markup=not_found_kb)
            return

        if is_pro() and info.get("vip_system", True) and code in info["vip_codes"] and not is_premium_user(uid):
            vip_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Premium sotib olish", callback_data="buy_premium")]])
            await message.answer(
                "🔒 Bu kontent faqat <b>VIP (Premium)</b> foydalanuvchilar uchun.",
                reply_markup=vip_kb,
            )
            return

        if (entry.get("access") == "paid" and entry.get("price")
                and not is_moderator(uid) and not has_purchased(uid, code)):
            await send_paid_offer(message, code, entry)
            return

        release_at = entry.get("release_at")
        if release_at and datetime.fromisoformat(release_at) > datetime.now() and not is_admin(info, uid):
            dt = datetime.fromisoformat(release_at)
            await message.answer(f"🗓 Bu kontent hali chiqmagan. Chiqish sanasi: {dt.strftime('%d.%m.%Y %H:%M')}")
            return

        await deliver_entry(message, uid, code, entry)
        await ensure_menu(message, uid)



SETUP_FUNCTIONS = {
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
    try:
        await child_bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        logging.error(f"delete_webhook xatosi ({token[-6:]}): {e}")
    child_dp = Dispatcher(storage=MemoryStorage())
    child_dp.update.outer_middleware(BotMetricsMiddleware(token))
    SETUP_FUNCTIONS[bot_type](child_dp, token)
    if bot_type in ("kino_pro",):
        _kinfo = data["bots"].get(token, {})
        _kurl = get_kinopanel_url(_kinfo.get("id"))
        if _kurl:
            for _aid in _kinfo.get("admin_ids", [_kinfo.get("admin_id")]):
                try:
                    await child_bot.set_chat_menu_button(
                        chat_id=_aid,
                        menu_button=MenuButtonWebApp(text="Panel", web_app=WebAppInfo(url=_kurl)),
                    )
                except Exception as e:
                    logging.info(f"Kino panel menyu tugmasi ({token[-6:]}, admin {_aid}): {e}")
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
                    trial_cfg = get_trial_config(info.get("type"))
                    if not trial_cfg.get("enabled", True):
                        continue  # Bu turdagi bot uchun sinov yo'q — ogohlantirish shart emas
                    expiry = datetime.fromisoformat(info["created_at"]) + timedelta(days=trial_cfg.get("days", TRIAL_DAYS))
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

                if info.get("type") in KINO_TYPES and info.get("auto_report_enabled"):
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
        f"💎 Faol VIP: {active_vip}\n"
        f"👁 Bugungi ko'rishlar: {info.get('daily_views', {}).get(today, 0)}\n"
        f"💰 Bugungi tushum: {sum(int(p.get('amount', 0) or 0) for p in info.get('payment_log', []) if str(p.get('date', '')).startswith(today)):,} so'm"
    )




# ---------- Telegram Mini App (BotFather'dagi kabi "Botlarim" veb-sahifasi) ----------
# ---------- Dizayn (rang) tizimi: 6 ta rang × 5 xil ohang ----------
THEME_PALETTES = {
    "green":  {"name": "Yashil",     "shades": ["#047857", "#0e9f6e", "#10b981", "#15803d", "#16a34a"]},
    "blue":   {"name": "Ko'k",       "shades": ["#1e40af", "#1d4ed8", "#2563eb", "#3b82f6", "#0284c7"]},
    "purple": {"name": "Binafsha",   "shades": ["#5b21b6", "#6d28d9", "#7c3aed", "#8b5cf6", "#9333ea"]},
    "red":    {"name": "Qizil",      "shades": ["#b91c1c", "#dc2626", "#e5484d", "#ef4444", "#be123c"]},
    "orange": {"name": "To'q sariq", "shades": ["#c2410c", "#ea580c", "#f97316", "#d97706", "#b45309"]},
    "pink":   {"name": "Pushti",     "shades": ["#9d174d", "#be185d", "#db2777", "#ec4899", "#c026d3"]},
}


def theme_color(key):
    """'blue-3' -> '#2563eb'. Noto'g'ri kalit bo'lsa None."""
    try:
        fam, idx = str(key).rsplit("-", 1)
        i = int(idx)
        if i < 1:
            return None
        return THEME_PALETTES[fam]["shades"][i - 1]
    except Exception:
        return None


def theme_palettes_json() -> str:
    return json.dumps(THEME_PALETTES, ensure_ascii=False)


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


def validate_platform_init_data(init_data: str):
    """Asosiy bot tokeni bilan tekshiradi, mos kelmasa har bir klon tokeni bilan ham sinaydi —
    chunki Mini App istalgan klon ("Bot Creator" nusxasi) orqali ham ochilishi mumkin."""
    parsed = validate_webapp_init_data(init_data, MAIN_BOT_TOKEN)
    if parsed:
        return parsed
    for clone in data.get("platform_clones", []):
        parsed = validate_webapp_init_data(init_data, clone.get("token", ""))
        if parsed:
            return parsed
    return None


MINIAPP_HTML = """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Bot Creator</title>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #0f1117);
    --bg2: var(--tg-theme-secondary-bg-color, #1a1d27);
    --text: var(--tg-theme-text-color, #ffffff);
    --hint: var(--tg-theme-hint-color, #8e93a3);
    --accent: var(--tg-theme-button-color, #3b82f6);
    --accent-text: var(--tg-theme-button-text-color, #ffffff);
    --line: color-mix(in srgb, var(--hint) 18%, transparent);
    --card-radius: 18px;
  }
  :root[data-theme="light"] {
    --bg: #ffffff; --bg2: #f2f3f7; --text: #14161c; --hint: #7d8290;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .mono { font-family: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace; }
  .wrap { max-width: 480px; margin: 0 auto; padding: 14px 14px 32px; }

  .topbar {
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 20px;
    opacity: 0; animation: rise .45s ease forwards;
  }
  .icon-btn {
    width: 36px; height: 36px; border-radius: 11px; flex-shrink: 0;
    background: var(--bg2); border: none; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    font-size: 15px; color: var(--text);
  }
  .icon-btn:active { transform: scale(0.92); }
  .brand {
    display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0;
  }
  .brand .mark {
    width: 32px; height: 32px; border-radius: 10px; flex-shrink: 0;
    background: linear-gradient(145deg, var(--accent), color-mix(in srgb, var(--accent) 55%, #7b2ff7));
    display: flex; align-items: center; justify-content: center; font-size: 15px;
  }
  .brand .bname { font-size: 15px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bell-wrap { position: relative; }
  .bell-badge {
    position: absolute; top: -3px; right: -3px;
    background: #ff3b30; color: #fff; font-size: 9px; font-weight: 700;
    min-width: 14px; height: 14px; border-radius: 999px;
    display: flex; align-items: center; justify-content: center; padding: 0 3px;
  }
  .avatar {
    width: 36px; height: 36px; border-radius: 50%; flex-shrink: 0;
    background: color-mix(in srgb, var(--accent) 20%, transparent);
    color: var(--accent);
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; font-weight: 700; position: relative; cursor: pointer; border: none;
  }
  .avatar .dot {
    position: absolute; bottom: -1px; right: -1px;
    width: 10px; height: 10px; border-radius: 50%;
    background: #34c759; border: 2px solid var(--bg);
  }

  .greet {
    margin-bottom: 18px;
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    opacity: 0; animation: rise .45s ease .05s forwards;
  }
  .greet h1 { font-size: 20px; font-weight: 700; margin: 0 0 2px; letter-spacing: -0.01em; }
  .greet p { font-size: 13px; color: var(--hint); margin: 0; }
  .newbot-btn {
    flex-shrink: 0;
    background: var(--accent); color: var(--accent-text);
    border: none; border-radius: 12px;
    padding: 10px 14px; font-size: 13.5px; font-weight: 600;
    text-decoration: none; white-space: nowrap;
    display: flex; align-items: center; gap: 5px; cursor: pointer;
  }
  .newbot-btn:active { opacity: .85; }

  .stat-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
    margin-bottom: 22px;
    opacity: 0; animation: rise .45s ease .1s forwards;
  }
  .stat-card { background: var(--bg2); border-radius: var(--card-radius); padding: 14px; }
  .stat-card .chip {
    width: 30px; height: 30px; border-radius: 9px;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; margin-bottom: 10px;
  }
  .stat-card .num { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
  .stat-card .lbl { font-size: 12px; color: var(--hint); margin-top: 2px; line-height: 1.3; }

  .section-head { display: flex; align-items: baseline; justify-content: space-between; margin: 0 2px 8px; }
  .section-head .t { font-size: 13px; font-weight: 700; color: var(--hint); text-transform: uppercase; letter-spacing: .04em; }
  .section-head .c { font-size: 13px; color: var(--accent); font-weight: 600; background: none; border: none; cursor: pointer; }

  .bot-list { background: var(--bg2); border-radius: var(--card-radius); overflow: hidden; margin-bottom: 22px; opacity: 0; animation: rise .45s ease .16s forwards; }
  .bot-row { display: flex; align-items: center; gap: 12px; padding: 13px 14px; border-bottom: 1px solid var(--line); }
  .bot-row:last-child { border-bottom: none; }
  .avatar-sq { flex-shrink: 0; width: 40px; height: 40px; border-radius: 11px; display: flex; align-items: center; justify-content: center; font-size: 17px; color: #fff; font-weight: 700; }
  .bot-meta { flex: 1; min-width: 0; }
  .bot-name { font-size: 14.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bot-sub { font-size: 12px; color: var(--hint); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pill { flex-shrink: 0; font-size: 10.5px; font-weight: 700; padding: 4px 9px; border-radius: 999px; white-space: nowrap; }
  .pill.on { background: color-mix(in srgb, #34c759 18%, transparent); color: #34c759; }
  .pill.off { background: color-mix(in srgb, #ff3b30 16%, transparent); color: #ff453a; }

  .empty { text-align: center; padding: 40px 20px; color: var(--hint); font-size: 14px; line-height: 1.5; }
  .empty .emoji { font-size: 32px; margin-bottom: 10px; display: block; }
  .state-msg { text-align: center; padding: 60px 20px; color: var(--hint); font-size: 14px; }

  /* ---- Profile full-screen overlay ---- */
  .profile-overlay {
    position: fixed; inset: 0; background: var(--bg);
    transform: translateX(100%); transition: transform .28s ease;
    overflow-y: auto; z-index: 20;
  }
  .profile-overlay.open { transform: translateX(0); }
  .profile-header { display: flex; align-items: center; gap: 10px; padding: 14px; }
  .profile-header .back { width: 34px; height: 34px; border-radius: 10px; background: var(--bg2); border: none; color: var(--text); font-size: 16px; display: flex; align-items: center; justify-content: center; }
  .profile-header .ttl { font-size: 16px; font-weight: 700; }
  .profile-hero {
    margin: 6px 14px 18px;
    background: linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 55%, #7b2ff7));
    border-radius: 20px; padding: 22px 18px; color: #fff; text-align: center;
  }
  .profile-hero .av { width: 60px; height: 60px; border-radius: 50%; background: rgba(255,255,255,.18); display: flex; align-items: center; justify-content: center; font-size: 24px; font-weight: 700; margin: 0 auto 10px; }
  .profile-hero .nm { font-size: 18px; font-weight: 700; }
  .profile-hero .un { font-size: 12.5px; opacity: .85; margin-top: 2px; }
  .profile-hero .row { display: flex; justify-content: center; gap: 26px; margin-top: 16px; }
  .profile-hero .row .v { font-size: 16px; font-weight: 700; }
  .profile-hero .row .l { font-size: 10.5px; opacity: .8; text-transform: uppercase; letter-spacing: .04em; margin-top: 2px; }
  .field-section { margin: 0 14px 18px; }
  .field-label { font-size: 12px; font-weight: 700; color: var(--hint); text-transform: uppercase; letter-spacing: .04em; margin: 0 2px 8px; }
  .field-box { background: var(--bg2); border-radius: 14px; padding: 4px; }
  .field { padding: 10px 12px; }
  .field + .field { border-top: 1px solid var(--line); }
  .field .fl { font-size: 11px; color: var(--hint); margin-bottom: 3px; }
  .field .fv { font-size: 14.5px; }
  .link-row {
    display: flex; align-items: center; gap: 10px;
    background: var(--bg2); border-radius: 14px; padding: 13px 14px;
    text-decoration: none; color: var(--text); font-size: 14px; font-weight: 500;
    margin-bottom: 8px; border: none; width: 100%; text-align: left; cursor: pointer;
  }


  /* ---- Yangi bot yaratish oynasi (bottom sheet) ---- */
  .sheet-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 30;
    opacity: 0; pointer-events: none; transition: opacity .25s ease;
  }
  .sheet-overlay.open { opacity: 1; pointer-events: auto; }
  .sheet {
    position: absolute; left: 0; right: 0; bottom: 0; max-width: 480px; margin: 0 auto;
    max-height: 90vh; display: flex; flex-direction: column;
    background: var(--bg); border-radius: 24px 24px 0 0;
    transform: translateY(100%); transition: transform .3s ease;
  }
  .sheet-overlay.open .sheet { transform: translateY(0); }
  .sheet-head { display: flex; align-items: center; justify-content: space-between; padding: 18px 16px 6px; }
  .sheet-head h2 { font-size: 19px; margin: 0; font-weight: 700; }
  .sheet-body { overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 2px 16px 14px; flex: 1; }
  .sheet-foot { padding: 10px 16px calc(14px + env(safe-area-inset-bottom, 0px)); border-top: 1px solid var(--line); background: var(--bg); }
  .step-label { font-size: 12.5px; font-weight: 700; color: var(--hint); text-transform: uppercase; letter-spacing: .05em; margin: 18px 2px 8px; }
  .type-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .type-card {
    background: var(--bg2); border: 2px solid transparent; border-radius: 16px;
    padding: 12px; text-align: left; color: var(--text); font: inherit; cursor: pointer;
  }
  .type-card.sel { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, var(--bg2)); }
  .type-card .tn { font-size: 14px; font-weight: 700; margin-bottom: 4px; }
  .type-card.sel .tn { color: var(--accent); }
  .type-card .td { font-size: 11.5px; color: var(--hint); line-height: 1.35; }
  .text-input {
    width: 100%; background: var(--bg2); border: 1px solid var(--line); border-radius: 14px;
    padding: 13px 14px; font-size: 15px; color: var(--text); outline: none;
  }
  .text-input:focus { border-color: var(--accent); }
  .ghost-btn {
    width: 100%; background: var(--bg2); color: var(--text); border: 1px solid var(--line);
    border-radius: 14px; padding: 12px; font-size: 14px; font-weight: 600; margin-top: 10px; cursor: pointer;
  }
  .tariff-card {
    background: var(--bg2); border: 2px solid transparent; border-radius: 16px;
    padding: 13px 14px; margin-bottom: 10px; display: flex; gap: 10px; align-items: center;
    width: 100%; text-align: left; color: var(--text); font: inherit; cursor: pointer;
  }
  .tariff-card.sel { border-color: var(--accent); }
  .tariff-card .tl { flex: 1; min-width: 0; }
  .tariff-card .tt { font-size: 14.5px; font-weight: 700; }
  .tariff-card .ts { font-size: 12px; color: var(--hint); margin-top: 3px; line-height: 1.45; }
  .tariff-card .tp { font-size: 15px; font-weight: 700; color: var(--accent); white-space: nowrap; text-align: right; }
  .tariff-card .tp small { display: block; font-size: 11px; color: var(--hint); font-weight: 500; }
  .note { font-size: 12.5px; color: var(--hint); line-height: 1.45; margin: 0 2px 10px; }
  .note.warn { color: #ff453a; }
  .err-box { background: color-mix(in srgb, #ff3b30 14%, transparent); color: #ff453a; border-radius: 12px; padding: 10px 12px; font-size: 13px; margin-bottom: 10px; }
  .primary-btn {
    width: 100%; background: var(--accent); color: var(--accent-text); border: none;
    border-radius: 15px; padding: 15px; font-size: 15.5px; font-weight: 700; cursor: pointer;
  }
  .primary-btn:disabled { opacity: .45; cursor: not-allowed; }
  .success-wrap { text-align: center; padding: 22px 8px 10px; }
  .success-wrap .big { font-size: 46px; }

  @keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  @media (prefers-reduced-motion: reduce) {
    .topbar, .greet, .stat-grid, .bot-list { animation: none !important; opacity: 1 !important; }
  }

  /* ---- Suzuvchi pastki menyu (barcha panellar uchun bir xil) ---- */
  .wrap { padding-bottom: calc(130px + env(safe-area-inset-bottom, 0px)); }
  .fnav {
    position: fixed; left: 18px; right: 18px; z-index: 25;
    bottom: calc(34px + env(safe-area-inset-bottom, 0px));
    max-width: 444px; margin: 0 auto; display: flex; padding: 10px 6px;
    background: color-mix(in srgb, var(--bg2) 92%, transparent);
    -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
    border: 1px solid var(--line); border-radius: 26px;
    box-shadow: 0 8px 26px rgba(0,0,0,.22);
  }
  .fnav button {
    flex: 1; border: 0; background: none; color: var(--hint); font: inherit;
    font-size: 12.5px; font-weight: 600; padding: 4px 0; cursor: pointer;
  }
  .fnav button i { display: block; font-style: normal; font-size: 23px; margin-bottom: 2px; }
  .fnav button.on { color: var(--accent); }
  .fnav button:active { transform: scale(.94); }
  .profile-overlay { padding-bottom: calc(140px + env(safe-area-inset-bottom, 0px)); }
  .ffoot {
    position: fixed; left: 0; right: 0; z-index: 24;
    bottom: calc(8px + env(safe-area-inset-bottom, 0px));
    text-align: center; color: var(--hint); font-size: 14px; font-weight: 600;
    pointer-events: none;
  }
</style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <button class="icon-btn" id="menuBtn">☰</button>
      <div class="brand">
        <div class="mark">⚡</div>
        <div class="bname">Bot Creator</div>
      </div>
      <button class="icon-btn" id="themeBtn">🌙</button>
      <button class="icon-btn" id="refreshBtn">↻</button>
      <div class="bell-wrap">
        <button class="icon-btn" id="bellBtn">🔔</button>
        <div class="bell-badge" id="bellBadge" style="display:none">0</div>
      </div>
      <button class="avatar" id="avatarBtn"><span id="avatarInit">?</span><span class="dot"></span></button>
    </div>
    <div id="content">
      <div class="state-msg">Yuklanmoqda…</div>
    </div>
  </div>

  <div class="fnav" id="fnav">
    <button data-nav="home" class="on"><i><svg viewBox="0 0 24 24"><path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10.5V20h13v-9.5"/><path d="M10 20v-5h4v5"/></svg></i>Bosh sahifa</button>
    <button data-nav="bots"><i><svg viewBox="0 0 24 24"><rect x="4" y="8" width="16" height="11" rx="3"/><path d="M12 8V4.5"/><circle cx="12" cy="3.6" r="1"/><circle cx="9" cy="13.5" r="1.1"/><circle cx="15" cy="13.5" r="1.1"/></svg></i>Botlarim</button>
    <button data-nav="new"><i><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 8v8M8 12h8"/></svg></i>Bot yaratish</button>
    <button data-nav="me"><i><svg viewBox="0 0 24 24"><circle cx="12" cy="8.5" r="3.6"/><path d="M4.5 20c.9-3.6 3.8-5.4 7.5-5.4s6.6 1.8 7.5 5.4"/></svg></i>Profil</button>
  </div>
  <div class="ffoot" id="ffoot"></div>

  <div class="profile-overlay" id="profileOverlay">
    <div class="profile-header">
      <button class="back" id="profileBack">←</button>
      <div class="ttl">Profil</div>
    </div>
    <div id="profileBody"></div>
  </div>

  <div class="sheet-overlay" id="sheetOverlay">
    <div class="sheet" id="sheet">
      <div class="sheet-head">
        <h2 id="sheetTitle">✨ Yangi Bot Yaratish</h2>
        <button class="icon-btn" id="sheetClose">✕</button>
      </div>
      <div class="sheet-body" id="sheetBody"></div>
      <div class="sheet-foot" id="sheetFoot"></div>
    </div>
  </div>

<script>
  window.addEventListener("error", function (e) {
    const content = document.getElementById("content");
    if (content) {
      content.innerHTML = '<div class="state-msg">Sahifada xatolik: ' + escapeHtmlSafe(String(e.message || e)) + '</div>';
    }
  });

  window.addEventListener("unhandledrejection", function (e) {
    const content = document.getElementById("content");
    if (content) {
      const msg = (e.reason && (e.reason.message || String(e.reason))) || "noma'lum xato";
      content.innerHTML = '<div class="state-msg">Sahifada xatolik (async): ' + escapeHtmlSafe(msg) + '</div>';
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
    "kino_pro": "#c026d3", "shop": "#0ea5e9", "ai": "#10b981",
    "money": "#f59e0b", "translate": "#6366f1", "taxi": "#eab308", "stars": "#f97316",
  };

  function fmt(n) {
    return Number(n || 0).toLocaleString("ru-RU").replace(/,/g, " ");
  }

  function initials(name) {
    return (name || "?").trim().slice(0, 1).toUpperCase();
  }

  function fetchWithTimeout(url, ms, options) {
    return new Promise((resolve, reject) => {
      const controller = new AbortController();
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        controller.abort();
        reject(new Error("timeout"));
      }, ms);
      fetch(url, Object.assign({}, options || {}, { signal: controller.signal }))
        .then((res) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve(res);
        })
        .catch((err) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          reject(err);
        });
    });
  }

  setTimeout(function () {
    const content = document.getElementById("content");
    if (content && content.textContent.indexOf("Yuklanmoqda") !== -1) {
      content.innerHTML = '<div class="state-msg">Yuklashda muammo yuz berdi.<br>Sahifani yopib, qayta urinib ko\\'ring.</div>';
    }
  }, 15000);

  let cachedInitData = "";
  let lastData = null;

  function renderDashboard(data) {
    lastData = data;
    const content = document.getElementById("content");
    const profile = data.profile || {};
    const stats = data.stats || {};

    document.getElementById("avatarInit").textContent = initials(profile.name);

    let html = '';
    const nm = escapeHtml(profile.name || "do'stim");
    html += '<div class="hero">' +
      '<div class="hero-top"><div class="hero-txt"><div class="hero-hi">Xush kelibsiz</div>' +
      '<h1>Salom, ' + nm + '!</h1>' +
      '<p>' + fmt(stats.total_bots) + ' ta bot sizning nazoratingizda</p></div>' +
      '<button class="newbot-btn" id="newBotBtn">＋ Yangi bot</button></div>' +
      '<div class="hero-bal"><span class="hb-l">Umumiy balans</span>' +
      '<span class="hb-v">' + fmt(data.balance) + '<small>' + "so'm" + '</small></span></div></div>';

    const icBot = '<svg viewBox="0 0 24 24"><rect x="4" y="8" width="16" height="11" rx="3"/><path d="M12 8V4.5"/><circle cx="9" cy="13.5" r="1"/><circle cx="15" cy="13.5" r="1"/></svg>';
    const icPulse = '<svg viewBox="0 0 24 24"><path d="M3 12h4l2.5-6 4 12 2.5-6H21"/></svg>';
    const icGift = '<svg viewBox="0 0 24 24"><rect x="4" y="9" width="16" height="11" rx="2"/><path d="M12 9v11M3 9h18M12 9c-2.5 0-4-1-4-2.5S9.3 4.5 10.8 5.6 12 9 12 9zm0 0c2.5 0 4-1 4-2.5s-1.3-2-2.8-.9S12 9 12 9z"/></svg>';
    html += '<div class="stat-grid s3">';
    html += statCard(icBot, 'color-mix(in srgb, #7c5cff 22%, transparent);color:#a99bff', stats.total_bots, 'Jami botlar');
    html += statCard(icPulse, 'color-mix(in srgb, #22c55e 20%, transparent);color:#4ade80', stats.active_bots, 'Faol botlar');
    html += statCard(icGift, 'color-mix(in srgb, #f2cd7a 22%, transparent);color:#f2cd7a', stats.referrals, 'Takliflar');
    html += '</div>';

    html += '<div class="section-head"><span class="t">Botlarim<em class="cnt">' + fmt(data.bots.length) + '</em></span><button class="c" id="seeAllBtn">Barchasi ›</button></div>';
    if (data.bots.length === 0) {
      html += '<div class="bot-list"><div class="empty"><span class="emoji">📭</span>' + "Hali botingiz yo'q." + '<br>Boshlash uchun yuqoridagi «＋ Yangi bot» tugmasini bosing.</div></div>';
    } else {
      html += '<div class="bot-list">';
      for (const b of data.bots) {
        const color = TYPE_COLORS[b.type_key] || "#8e8e93";
        const pillClass = b.active ? "on" : "off";
        const pillText = b.active ? "Faol" : "To'xtatilgan";
        html += '<div class="bot-row' + (b.active ? '' : ' off') + '"' + (b.kino ? ' data-kid="' + b.id + '"' : '') +
          ' style="--tc:' + color + ';' + (b.kino ? 'cursor:pointer' : '') + '">' +
          '<div class="avatar-sq" style="background:linear-gradient(145deg,' + color + ',color-mix(in srgb,' + color + ' 55%,#000))">' + initials(b.name) + '</div>' +
          '<div class="bot-meta">' +
            '<div class="bot-name">' + escapeHtml(b.name) + '</div>' +
            '<div class="bot-sub"><span class="ch">' + escapeHtml(b.type) + '</span><span class="ch gold">' + escapeHtml(b.tariff) + '</span>' +
              (b.kino ? '<span class="ch acc">🌐 Panel ›</span>' : '') + '</div>' +
          '</div>' +
          '<div class="pill ' + pillClass + '">' + pillText + '</div>' +
        '</div>';
      }
      html += '</div>';
    }

    content.innerHTML = html;

    document.getElementById("newBotBtn").addEventListener("click", function () {
      openCreateSheet();
    });
    document.getElementById("seeAllBtn").addEventListener("click", function () {
      openBotDeepLink("");
    });
    document.querySelectorAll(".bot-row[data-kid]").forEach(function (el) {
      el.addEventListener("click", function () {
        location.href = "/kinopanel?b=" + el.getAttribute("data-kid") + "&from=creator#" + location.hash.slice(1);
      });
    });
  }

  function getBotUsername() {
    return new URLSearchParams(location.search).get("bot") || "";
  }

  function openBotDeepLink(payload) {
    const tg = window.Telegram && window.Telegram.WebApp;
    const username = getBotUsername();
    if (tg && username && tg.openTelegramLink) {
      const link = "https://t.me/" + username + (payload ? "?start=" + payload : "");
      tg.openTelegramLink(link);
      if (tg.close) tg.close();
    } else if (tg && tg.close) {
      tg.close();
    }
  }

  function statCard(emoji, chipBg, value, label) {
    return '<div class="stat-card">' +
      '<div class="chip" style="background:' + chipBg + '">' + emoji + '</div>' +
      '<div class="num">' + value + '</div>' +
      '<div class="lbl">' + label + '</div>' +
    '</div>';
  }

  function renderProfile() {
    const body = document.getElementById("profileBody");
    if (!lastData) { body.innerHTML = ''; return; }
    const p = lastData.profile || {};
    const s = lastData.stats || {};
    let html = '';
    html += '<div class="profile-hero">' +
      '<div class="av">' + initials(p.name) + '</div>' +
      '<div class="nm">' + escapeHtml(p.name || "") + '</div>' +
      (p.username ? '<div class="un">' + escapeHtml(p.username) + '</div>' : '') +
      '<div class="row">' +
        '<div><div class="v">' + fmt(s.total_bots) + '</div><div class="l">Botlar</div></div>' +
        '<div><div class="v">' + fmt(lastData.balance) + '</div><div class="l">Balans</div></div>' +
        '<div><div class="v" style="color:#8fffb0">Faol</div><div class="l">Holat</div></div>' +
      '</div>' +
    '</div>';
    html += '<div class="field-section"><div class="field-label">Shaxsiy ma\\'lumotlar</div><div class="field-box">' +
      '<div class="field"><div class="fl">Ism</div><div class="fv">' + escapeHtml(p.name || "") + '</div></div>' +
      '<div class="field"><div class="fl">Telegram ID</div><div class="fv mono">' + escapeHtml(String(p.id || "")) + '</div></div>' +
      '<div class="field"><div class="fl">Referal kodim</div><div class="fv mono">' + escapeHtml(String(p.id || "")) + '</div></div>' +
    '</div></div>';
    html += '<div class="field-section">' +
      '<div class="field-label">Yordam</div>' +
      '<button class="link-row" id="helpBtn">💬 Murojaat / Yordam</button>' +
      '<button class="link-row" id="guideBtn">📖 Qo\\'llanma</button>' +
    '</div>';
    body.innerHTML = html;

    document.getElementById("helpBtn").addEventListener("click", function () { sendToBot("📩 Murojaat"); });
    document.getElementById("guideBtn").addEventListener("click", function () { sendToBot("📖 Qo'llanma"); });
  }

  function sendToBot(text) {
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.close) { tg.close(); }
  }

  async function fetchAndRender() {
    const content = document.getElementById("content");
    try {
      const res = await fetchWithTimeout("/api/mybots?" + new URLSearchParams({ initData: cachedInitData }), 8000);
      if (!res.ok) {
        const errBody = await res.text().catch(() => "");
        throw new Error("Server javobi: " + res.status + " " + errBody.slice(0, 120));
      }
      const data = await res.json();
      renderDashboard(data);
    } catch (e) {
      const reason = e && e.name === "AbortError" ? "Server 8 soniyada javob bermadi (timeout)." : (e.message || String(e));
      content.innerHTML = '<div class="state-msg">Ma\\'lumotlarni yuklab bo\\'lmadi.<br><span style="font-size:12px">' + escapeHtmlSafe(reason) + '</span></div>';
    }
  }

  async function load() {
    const content = document.getElementById("content");
    try {
      await loadTelegramSdk(4000);
      const tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
      if (tg) {
        try { tg.ready(); } catch (e) {}
        try { tg.expand(); } catch (e) {}
      }

      if (!tg) {
        content.innerHTML = '<div class="state-msg">Telegram SDK yuklanmadi. Internet aloqasini tekshirib, sahifani qayta oching.</div>';
        return;
      }

      const initData = tg.initData || "";

      if (!initData) {
        content.innerHTML = '<div class="state-msg">Bu sahifa faqat Telegram ichida ishlaydi.</div>';
        return;
      }

      cachedInitData = initData;
      await fetchAndRender();
    } catch (e) {
      const reason = e.message || String(e);
      content.innerHTML = '<div class="state-msg">Ma\\'lumotlarni yuklab bo\\'lmadi.<br><span style="font-size:12px">' + escapeHtmlSafe(reason) + '</span></div>';
    }
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  // ---- Top bar interactions ----
  document.getElementById("refreshBtn").addEventListener("click", function () {
    if (!cachedInitData) return;
    document.getElementById("content").innerHTML = '<div class="state-msg">Yangilanmoqda…</div>';
    fetchAndRender();
  });

  document.getElementById("themeBtn").addEventListener("click", function () {
    const root = document.documentElement;
    const isLight = root.getAttribute("data-theme") === "light";
    if (isLight) {
      root.removeAttribute("data-theme");
      this.textContent = "🌙";
    } else {
      root.setAttribute("data-theme", "light");
      this.textContent = "☀️";
    }
  });

  document.getElementById("menuBtn").addEventListener("click", function () {
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.close) { tg.close(); }
  });

  document.getElementById("bellBtn").addEventListener("click", function () {
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.showAlert) { tg.showAlert("Hozircha yangi bildirishnoma yo'q."); }
  });

  document.getElementById("avatarBtn").addEventListener("click", function () {
    renderProfile();
    document.getElementById("profileOverlay").classList.add("open");
  });
  document.getElementById("profileBack").addEventListener("click", function () {
    document.getElementById("profileOverlay").classList.remove("open");
  });

  // ================= YANGI BOT YARATISH OYNASI =================
  let createState = null;

  function moneyFmt(n) { return fmt(n) + " so'm"; }

  function escapeAttr(s) {
    return escapeHtml(String(s == null ? "" : s)).replace(/"/g, "&quot;");
  }

  function findType(st, key) {
    if (!st || !st.info) return null;
    for (const t of st.info.types) { if (t.key === key) return t; }
    return null;
  }

  function needsTariff(t) { return !!t && !!t.tariffs && t.tariffs.length > 0; }

  function cheapestTariffId(t) {
    let best = null;
    for (const tf of t.tariffs) { if (best === null || tf.price < best.price) best = tf; }
    return best ? best.id : null;
  }

  function selectType(st, key) {
    st.type = key;
    const t = findType(st, key);
    st.tariff = needsTariff(t) ? cheapestTariffId(t) : null;
  }

  function computeCanSubmit(st) {
    if (!st || !st.info) return { ok: false, reason: "" };
    if (st.info.blocked) return { ok: false, reason: "Siz bloklangansiz." };
    const t = findType(st, st.type);
    if (!t) return { ok: false, reason: "Shablon tanlang." };
    if (!st.token || st.token.trim().length < 20) return { ok: false, reason: "Bot tokenini kiriting." };
    if (needsTariff(t) && !st.tariff) return { ok: false, reason: "Tarif tanlang." };
    if (t.creation_price > 0 && st.info.balance < t.creation_price) return { ok: false, reason: "Hisobda yetarli mablag' yo'q." };
    return { ok: true, reason: "" };
  }

  function buildStep4Html(st, t) {
    let html = "";
    if (!t) return html;
    const info = st.info;
    if (needsTariff(t)) {
      for (const tf of t.tariffs) {
        html += '<button type="button" class="tariff-card' + (tf.id === st.tariff ? " sel" : "") + '" data-tariff="' + escapeAttr(tf.id) + '">' +
          '<div class="tl"><div class="tt">' + escapeHtml(tf.name) + '</div>' +
          '<div class="ts">👥 ' + escapeHtml(tf.limit) + (tf.speed ? "<br>⚡ Javob tezligi: " + escapeHtml(tf.speed) : "") + '</div></div>' +
          '<div class="tp">' + fmt(tf.price) + ' so\\'m<small>oyiga</small></div></button>';
      }
    } else {
      html += '<div class="tariff-card sel" style="cursor:default"><div class="tl"><div class="tt">Standart tarif</div>' +
        '<div class="ts">👥 cheksiz foydalanuvchi</div></div>' +
        '<div class="tp">' + fmt(t.monthly_price) + ' so\\'m<small>oyiga</small></div></div>';
    }
    if (t.creation_price > 0) {
      html += '<div class="note">💵 Yaratish uchun <b>' + moneyFmt(t.creation_price) + '</b> balansingizdan yechiladi. Joriy balans: ' + moneyFmt(info.balance) + '.</div>';
      if (info.balance < t.creation_price) {
        html += '<div class="note warn">Hisobda yetarli mablag\\' yo\\'q. Avval botda "💰 Hisob to\\'ldirish" orqali balansni to\\'ldiring.</div>';
      }
    }
    if (info.has_bot) {
      html += '<div class="note warn">Bu sizning birinchi bepul botingiz emas — foydalanish uchun darhol to\\'lov qilishingiz kerak (sinov berilmaydi).</div>';
    } else if (t.trial_enabled) {
      html += '<div class="note">🎁 ' + t.trial_days + ' kun bepul sinov beriladi, so\\'ng oylik to\\'lov kerak bo\\'ladi.</div>';
    } else {
      html += '<div class="note warn">Bu bot turi uchun bepul sinov yo\\'q — darhol to\\'lov talab qilinadi.</div>';
    }
    return html;
  }

  function buildSuccessHtml(b) {
    let html = '<div class="success-wrap"><div class="big">✅</div>' +
      '<h3 style="margin:8px 0 4px;font-size:18px">Bot ishga tushdi!</h3>' +
      '<div style="font-weight:700">' + escapeHtml(b.name) + '</div>' +
      '<div class="note" style="margin-top:2px">' + escapeHtml(b.type) + '</div></div>';
    if (b.charge > 0) {
      html += '<div class="note">💵 Yaratish uchun ' + moneyFmt(b.charge) + ' balansingizdan yechildi.</div>';
    }
    if (b.skip_trial) {
      html += '<div class="note warn">Bu sizning birinchi bepul botingiz emas — foydalanish uchun darhol to\\'lov qiling ("📁 Botlarim" → "💰 Hozir to\\'lov qilish").</div>';
    } else if (b.trial_enabled) {
      html += '<div class="note">🎁 ' + b.trial_days + ' kunlik bepul sinov boshlandi!</div>';
    } else {
      html += '<div class="note warn">Bu bot turi uchun sinov yo\\'q — foydalanish uchun darhol to\\'lov qiling.</div>';
    }
    html += '<div class="note">💠 Oylik narx: ' + moneyFmt(b.monthly_price) + '</div>';
    if (b.username) {
      html += '<button type="button" class="ghost-btn" id="gotoBotBtn" data-username="' + escapeAttr(b.username) + '">🤖 Botga o\\'tish (@' + escapeHtml(b.username) + ')</button>';
    }
    html += '<div class="note" style="margin-top:12px">Majburiy obuna qo\\'shish uchun o\\'sha botga /channels yozing.</div>';
    return html;
  }

  function buildSheetBodyHtml(st) {
    if (!st) return "";
    if (st.done) return buildSuccessHtml(st.done);
    if (!st.info) {
      if (st.loadError) {
        return '<div class="state-msg">Ma\\'lumotlarni yuklab bo\\'lmadi.<br><span style="font-size:12px">' + escapeHtml(st.loadError) + '</span></div>';
      }
      return '<div class="state-msg">Yuklanmoqda…</div>';
    }
    const info = st.info;
    const t = findType(st, st.type);
    let html = "";
    if (info.blocked) {
      html += '<div class="note warn">🚫 Siz Bot Creator\\'dan foydalanishdan bloklangansiz.</div>';
    }
    html += '<div class="step-label">1. Shablon tanlang</div><div class="type-grid">';
    for (const it of info.types) {
      html += '<button type="button" class="type-card' + (it.key === st.type ? " sel" : "") + '" data-type="' + escapeAttr(it.key) + '">' +
        '<div class="tn">' + escapeHtml(it.name) + '</div>' +
        '<div class="td">' + escapeHtml(it.short) + '</div></button>';
    }
    html += '</div>';
    html += '<div class="step-label">2. Bot nomi <span style="text-transform:none;font-weight:500">(ixtiyoriy)</span></div>';
    html += '<input class="text-input" id="botNameInput" type="text" maxlength="60" placeholder="Masalan: Mening Kino Botim" value="' + escapeAttr(st.name) + '">';
    html += '<div class="step-label">3. @BotFather tokeni</div>';
    html += '<input class="text-input mono" id="botTokenInput" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="123456789:AAHxxxxxxxxxxxxx" value="' + escapeAttr(st.token) + '">';
    html += '<button type="button" class="ghost-btn" id="openBotFatherBtn">🤖 @BotFather\\'ni ochish</button>';
    html += '<div class="step-label">4. ' + (needsTariff(t) ? "Tarif tanlang" : "Narx") + '</div>';
    html += buildStep4Html(st, t);
    return html;
  }

  function buildSheetFootHtml(st) {
    if (!st) return "";
    if (st.done) return '<button type="button" class="primary-btn" id="sheetDoneBtn">Tayyor</button>';
    const c = computeCanSubmit(st);
    const t = findType(st, st.type);
    let label = "🚀 Botni ishga tushirish";
    if (t && t.creation_price > 0) label += " — " + moneyFmt(t.creation_price);
    if (st.submitting) label = "Yaratilmoqda…";
    let html = "";
    if (st.error) html += '<div class="err-box" id="errBox">' + escapeHtml(st.error) + '</div>';
    html += '<button type="button" class="primary-btn" id="createBtn"' + ((!c.ok || st.submitting) ? " disabled" : "") + '>' + label + '</button>';
    if (!c.ok && c.reason && st.info) {
      html += '<div class="note" style="text-align:center;margin:8px 0 0">' + escapeHtml(c.reason) + '</div>';
    }
    return html;
  }

  function renderSheet() {
    const body = document.getElementById("sheetBody");
    const foot = document.getElementById("sheetFoot");
    const prev = body.scrollTop;
    document.getElementById("sheetTitle").textContent = (createState && createState.done) ? "🎉 Tayyor!" : "✨ Yangi Bot Yaratish";
    body.innerHTML = buildSheetBodyHtml(createState);
    foot.innerHTML = buildSheetFootHtml(createState);
    body.scrollTop = prev;
  }

  function updateFooter() {
    document.getElementById("sheetFoot").innerHTML = buildSheetFootHtml(createState);
  }

  function openTelegramUrl(url) {
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.openTelegramLink) { tg.openTelegramLink(url); }
    else { window.open(url, "_blank"); }
  }

  async function loadCreateInfo(st) {
    try {
      const res = await fetchWithTimeout("/api/create_info?" + new URLSearchParams({ initData: cachedInitData }), 10000);
      if (!res.ok) { throw new Error("Server javobi: " + res.status); }
      const info = await res.json();
      st.info = info;
      if (info.types.length) { selectType(st, info.types[0].key); }
    } catch (e) {
      st.loadError = (e && e.message) ? e.message : String(e);
    }
    if (createState === st) { renderSheet(); }
  }

  function openCreateSheet() {
    createState = { info: null, loadError: "", type: null, tariff: null, name: "", token: "", submitting: false, error: "", done: null };
    document.getElementById("sheetOverlay").classList.add("open");
    renderSheet();
    loadCreateInfo(createState);
  }

  function closeCreateSheet() {
    document.getElementById("sheetOverlay").classList.remove("open");
    const wasDone = !!(createState && createState.done);
    createState = null;
    if (wasDone && cachedInitData) { fetchAndRender(); }
  }

  async function submitCreate() {
    const st = createState;
    if (!st || st.submitting) return;
    if (!computeCanSubmit(st).ok) return;
    st.submitting = true;
    st.error = "";
    updateFooter();
    try {
      const res = await fetchWithTimeout("/api/create_bot", 30000, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          initData: cachedInitData,
          bot_type: st.type,
          token: st.token.trim(),
          bot_name: st.name.trim(),
          tariff_id: st.tariff,
        }),
      });
      let payload = null;
      try { payload = await res.json(); } catch (e) { payload = null; }
      if (!res.ok || !payload || !payload.ok) {
        throw new Error((payload && payload.message) ? payload.message : ("Server xatosi: " + res.status));
      }
      st.submitting = false;
      st.done = payload.bot;
      if (createState === st) { renderSheet(); } else if (cachedInitData) { fetchAndRender(); }
    } catch (e) {
      st.submitting = false;
      st.error = (e && e.message === "timeout")
        ? "Server javob bermadi. Bot yaratilgan bo'lishi mumkin — ro'yxatni yangilab tekshiring."
        : ((e && e.message) ? e.message : String(e));
      if (createState === st) { updateFooter(); }
    }
  }

  document.getElementById("sheetBody").addEventListener("click", function (e) {
    const goto = e.target.closest("#gotoBotBtn");
    if (goto) { openTelegramUrl("https://t.me/" + goto.getAttribute("data-username")); return; }
    if (!createState || createState.done) return;
    const typeEl = e.target.closest("[data-type]");
    if (typeEl) { selectType(createState, typeEl.getAttribute("data-type")); createState.error = ""; renderSheet(); return; }
    const tfEl = e.target.closest("[data-tariff]");
    if (tfEl) { createState.tariff = tfEl.getAttribute("data-tariff"); renderSheet(); return; }
    if (e.target.closest("#openBotFatherBtn")) { openTelegramUrl("https://t.me/BotFather"); }
  });

  document.getElementById("sheetBody").addEventListener("input", function (e) {
    if (!createState || createState.done) return;
    if (e.target.id === "botNameInput") { createState.name = e.target.value; }
    else if (e.target.id === "botTokenInput") { createState.token = e.target.value; createState.error = ""; updateFooter(); }
  });

  document.getElementById("sheetFoot").addEventListener("click", function (e) {
    if (e.target.closest("#sheetDoneBtn")) { closeCreateSheet(); return; }
    if (e.target.closest("#createBtn")) { submitCreate(); }
  });

  document.getElementById("sheetClose").addEventListener("click", closeCreateSheet);
  document.getElementById("sheetOverlay").addEventListener("click", function (e) {
    if (e.target === this) { closeCreateSheet(); }
  });

  load();
</script>
<script>
(function () {
  var nav = document.getElementById("fnav"), foot = document.getElementById("ffoot");
  if (!nav) return;
  try {
    var u = new URLSearchParams(location.search).get("bot") || "";
    foot.textContent = u ? "@" + u : "";
  } catch (e) {}
  function mark(k) {
    nav.querySelectorAll("button").forEach(function (b) { b.classList.toggle("on", b.getAttribute("data-nav") === k); });
  }
  function closeOverlays() {
    var po = document.getElementById("profileOverlay");
    if (po) po.classList.remove("open");
  }
  nav.addEventListener("click", function (e) {
    var b = e.target.closest("[data-nav]"); if (!b) return;
    var k = b.getAttribute("data-nav");
    try {
      if (k === "home") { closeOverlays(); window.scrollTo({top: 0, behavior: "smooth"}); mark("home"); }
      else if (k === "bots") {
        closeOverlays(); mark("bots");
        var h = document.querySelector(".section-head");
        if (h) h.scrollIntoView({behavior: "smooth", block: "start"});
      }
      else if (k === "new") { closeOverlays(); openCreateSheet(); mark("new"); }
      else if (k === "me") { renderProfile(); document.getElementById("profileOverlay").classList.add("open"); mark("me"); }
    } catch (err) {}
  });
  var pb = document.getElementById("profileBack");
  if (pb) pb.addEventListener("click", function () { mark("home"); });
  var sc = document.getElementById("sheetClose");
  if (sc) sc.addEventListener("click", function () { mark("home"); });
  var so = document.getElementById("sheetOverlay");
  if (so) so.addEventListener("click", function (e) { if (e.target === so) mark("home"); });
  var ab = document.getElementById("avatarBtn");
  if (ab) ab.addEventListener("click", function () { mark("me"); });
  var nb = document.getElementById("content");
  if (nb) nb.addEventListener("click", function (e) { if (e.target.closest("#newBotBtn")) mark("new"); });
})();
</script>
<style>
  .bot-row { border: 1px solid color-mix(in srgb, var(--accent) 24%, transparent); box-shadow: 0 8px 22px color-mix(in srgb, var(--accent) 14%, transparent); border-radius: 18px; background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 9%, var(--bg2)), var(--bg2)); }
  .bot-row .avatar-sq { box-shadow: 0 4px 12px rgba(0,0,0,.25); }
</style>
<style>
  /* ===== Tiniq va chiroyli dizayn ===== */
  html, body { -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
  .wrap { max-width: 480px; padding: 12px 16px calc(140px + env(safe-area-inset-bottom, 0px)); }

  /* Yuqori panel: sodda va bo'sh */
  #menuBtn { display: none; }
  .topbar { gap: 8px; margin-bottom: 18px; }
  .icon-btn { width: 40px; height: 40px; border-radius: 13px; background: var(--bg2); border: 1px solid var(--line); font-size: 16px; }
  .brand .mark { width: 36px; height: 36px; border-radius: 12px; font-size: 17px; box-shadow: 0 4px 12px color-mix(in srgb, var(--accent) 35%, transparent); }
  .brand .bname { font-size: 16px; font-weight: 800; letter-spacing: -.01em; }
  .avatar { width: 40px; height: 40px; font-size: 15px; border: 2px solid color-mix(in srgb, var(--accent) 35%, transparent); }

  /* Salomlashish */
  .greet h1 { font-size: 24px; font-weight: 800; letter-spacing: -.02em; }
  .greet p { font-size: 13.5px; }
  .newbot-btn { border-radius: 14px; padding: 12px 16px; font-size: 14px; font-weight: 700;
    box-shadow: 0 6px 16px color-mix(in srgb, var(--accent) 38%, transparent); }

  /* Statistika kartalari */
  .stat-grid { gap: 12px; margin-bottom: 26px; }
  .stat-card { border-radius: 20px; padding: 16px; background: var(--bg2); border: 1px solid var(--line);
    box-shadow: 0 2px 10px rgba(0,0,0,.05); }
  .stat-card .chip { width: 36px; height: 36px; border-radius: 12px; font-size: 17px; margin-bottom: 12px; }
  .stat-card .num { font-size: 26px; font-weight: 800; line-height: 1.1; }
  .stat-card .num small { font-size: 13px; font-weight: 600; opacity: .7; margin-left: 2px; }
  .stat-card .lbl { font-size: 12.5px; margin-top: 4px; }
  .stat-card:nth-child(4) { grid-column: 1 / -1; display: grid; grid-template-columns: auto 1fr; column-gap: 14px; align-items: center;
    background: linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 55%, #7b2ff7)); color: #fff; border: none;
    box-shadow: 0 10px 24px color-mix(in srgb, var(--accent) 35%, transparent); }
  .stat-card:nth-child(4) .chip { grid-row: 1 / 3; margin: 0; background: rgba(255,255,255,.22) !important; width: 44px; height: 44px; font-size: 20px; }
  .stat-card:nth-child(4) .lbl { color: rgba(255,255,255,.85); margin-top: 2px; }

  /* Bo'lim sarlavhasi */
  .section-head { margin: 0 4px 12px; }
  .section-head .t { font-size: 12.5px; letter-spacing: .08em; }
  .section-head .c { font-size: 13.5px; font-weight: 700; }

  /* Botlar ro'yxati: har bir bot alohida toza karta */
  .bot-list { background: transparent; overflow: visible; display: flex; flex-direction: column; gap: 12px; border-radius: 0; }
  .bot-row { background: var(--bg2); border: 1px solid var(--line); border-radius: 20px; padding: 14px;
    box-shadow: 0 2px 10px rgba(0,0,0,.06); gap: 14px; transition: transform .15s ease; }
  .bot-row[data-kid]:active { transform: scale(.985); }
  .bot-row:last-child { border-bottom: 1px solid var(--line); }
  .bot-row .avatar-sq { width: 48px; height: 48px; border-radius: 15px; font-size: 20px; font-weight: 800;
    box-shadow: 0 6px 14px rgba(0,0,0,.18); }
  .bot-name { font-size: 16px; font-weight: 700; }
  .bot-sub, .bot-sub.mono { font-family: inherit; font-size: 12.5px; line-height: 1.45; white-space: normal; margin-top: 3px; }
  .pill { font-size: 11.5px; padding: 5px 11px; }
  .pill.on { background: color-mix(in srgb, #22c55e 16%, transparent); color: #16a34a; }
  .empty { padding: 36px 20px; }
  .bot-list .empty { background: var(--bg2); border: 1px dashed var(--line); border-radius: 20px; }

  /* Pastki menyu: to'liq zich fon, hech narsa ko'rinib turmaydi */
  body::after { content: ""; position: fixed; left: 0; right: 0; bottom: 0; z-index: 13; pointer-events: none;
    height: calc(122px + env(safe-area-inset-bottom, 0px));
    background: linear-gradient(to top, var(--bg) 64%, color-mix(in srgb, var(--bg) 0%, transparent)); }
  .fnav { background: var(--bg2); -webkit-backdrop-filter: none; backdrop-filter: none;
    border: 1px solid var(--line); border-radius: 28px; padding: 8px 6px;
    box-shadow: 0 10px 30px rgba(0,0,0,.20), 0 2px 6px rgba(0,0,0,.08); }
  .fnav button { display: flex; flex-direction: column; align-items: center; gap: 3px; font-size: 11.5px; font-weight: 700;
    padding: 7px 0; border-radius: 20px; margin: 0 2px; transition: background .2s ease, color .2s ease; }
  .fnav button i { margin: 0; display: flex; }
  .fnav button { white-space: nowrap; }
  .fnav button svg { width: 25px; height: 25px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
  .fnav button.on { color: var(--accent); background: color-mix(in srgb, var(--accent) 13%, transparent); }
  .fnav button.on svg { stroke-width: 2.1; }
  .ffoot { font-size: 13px; font-weight: 700; letter-spacing: .01em; opacity: .9; }
</style>
<style>
  /* ===================== PREMIUM: Bot Creator ===================== */
  :root { --bg: #070b16; --bg2: #0f1626; --text: #f3f5fa; --hint: #8b95aa; --line: rgba(255,255,255,.08);
          --gold: #f2cd7a; --gold2: #d9a441; --glass: rgba(255,255,255,.045); }
  :root[data-theme="light"] { --bg: #f5f3ec; --bg2: #ffffff; --text: #141a2c; --hint: #737b8e; --line: rgba(20,26,44,.09);
          --gold: #a86f12; --gold2: #8a5a0c; --glass: #ffffff; }
  html { background: var(--bg); }
  body {
    min-height: 100vh; background-attachment: fixed;
    background:
      radial-gradient(900px 420px at 12% -8%, color-mix(in srgb, var(--accent) 24%, transparent), transparent 70%),
      radial-gradient(700px 380px at 108% 10%, rgba(242,205,122,.10), transparent 70%),
      var(--bg);
  }
  :root[data-theme="light"] body {
    background:
      radial-gradient(800px 360px at 10% -8%, color-mix(in srgb, var(--accent) 12%, transparent), transparent 70%),
      radial-gradient(600px 320px at 108% 8%, rgba(217,164,65,.14), transparent 70%),
      var(--bg);
  }

  /* Yuqori panel: shisha effekti */
  .topbar { position: sticky; top: 0; z-index: 12; margin: -12px -16px 18px; padding: 12px 16px;
    background: color-mix(in srgb, var(--bg) 78%, transparent);
    -webkit-backdrop-filter: blur(16px); backdrop-filter: blur(16px); border-bottom: 1px solid var(--line); }
  .icon-btn { background: var(--glass); border: 1px solid var(--line); }
  .brand .mark { background: linear-gradient(145deg, var(--gold), var(--gold2)); color: #1b1204;
    box-shadow: 0 6px 16px rgba(217,164,65,.35); }
  .brand .bname { letter-spacing: -.01em; }

  /* Katta salom kartasi */
  .hero { position: relative; overflow: hidden; border-radius: 28px; padding: 20px 18px 16px; margin-bottom: 16px;
    border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--line));
    background:
      radial-gradient(420px 220px at 100% 0%, color-mix(in srgb, var(--accent) 34%, transparent), transparent 70%),
      radial-gradient(360px 200px at 0% 100%, rgba(242,205,122,.14), transparent 70%),
      linear-gradient(160deg, color-mix(in srgb, var(--bg2) 90%, var(--accent)), var(--bg2));
    box-shadow: 0 18px 40px rgba(0,0,0,.28); opacity: 0; animation: rise .45s ease .05s forwards; }
  :root[data-theme="light"] .hero { box-shadow: 0 14px 34px rgba(60,50,20,.14); }
  .hero-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  .hero-txt { min-width: 0; }
  .hero-hi { font-size: 11px; letter-spacing: .16em; text-transform: uppercase; color: var(--gold); font-weight: 800; margin-bottom: 7px; }
  .hero h1 { font-size: 25px; font-weight: 800; letter-spacing: -.02em; margin: 0 0 4px; overflow-wrap: anywhere; }
  .hero p { margin: 0; font-size: 13px; color: var(--hint); line-height: 1.4; }
  .hero .newbot-btn { background: linear-gradient(135deg, var(--gold), var(--gold2)); color: #1b1204; font-weight: 800;
    box-shadow: 0 8px 20px rgba(217,164,65,.35); }
  .hero-bal { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-top: 18px; padding-top: 14px;
    border-top: 1px dashed var(--line); }
  .hb-l { font-size: 11.5px; letter-spacing: .1em; text-transform: uppercase; color: var(--hint); font-weight: 700; }
  .hb-v { font-size: 28px; font-weight: 800; letter-spacing: -.02em; font-variant-numeric: tabular-nums; white-space: nowrap;
    background: linear-gradient(135deg, var(--gold), #fff3d0 55%, var(--gold2)); -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent; color: transparent; }
  .hb-v small { font-size: 13px; font-weight: 700; margin-left: 5px; -webkit-text-fill-color: var(--gold); }
  :root[data-theme="light"] .hb-v { background: linear-gradient(135deg, #b7791f, #8a5a0c); -webkit-background-clip: text; background-clip: text; }

  /* Statistika: 3 ta ixcham shisha karta */
  .stat-grid, .stat-grid.s3 { grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 24px; }
  .stat-card { grid-column: auto; display: block; border-radius: 20px; padding: 13px 12px; background: var(--glass);
    color: var(--text); border: 1px solid var(--line); box-shadow: none; }
  .stat-card .chip { width: 32px; height: 32px; border-radius: 11px; margin: 0 0 10px; grid-row: auto; }
  .stat-card .chip svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
  .stat-card .num { font-size: 24px; }
  .stat-card .lbl { font-size: 11.5px; color: var(--hint); margin-top: 3px; }

  /* Sarlavha */
  .section-head .t { display: flex; align-items: center; }
  .section-head .cnt { font-style: normal; margin-left: 8px; font-size: 11px; padding: 2px 9px; border-radius: 999px; letter-spacing: 0;
    font-weight: 800; background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent); }

  /* Bot kartalari */
  .bot-row { position: relative; overflow: hidden; background: var(--glass); border: 1px solid var(--line); border-radius: 22px;
    padding: 14px 14px 14px 20px; gap: 13px; box-shadow: 0 8px 22px rgba(0,0,0,.16); }
  :root[data-theme="light"] .bot-row { box-shadow: 0 8px 22px rgba(60,50,20,.08); }
  .bot-row::before { content: ""; position: absolute; left: 0; top: 16px; bottom: 16px; width: 4px; border-radius: 0 4px 4px 0;
    background: var(--tc, var(--accent)); }
  .bot-row.off { opacity: .75; }
  .bot-row.off::before { background: #ef4444; }
  .bot-sub { margin-top: 6px; white-space: normal; }
  .ch { display: inline-block; font-size: 11.5px; font-weight: 600; padding: 3px 9px; border-radius: 999px; margin: 0 5px 4px 0;
    white-space: nowrap; background: color-mix(in srgb, var(--text) 7%, transparent); color: var(--hint); }
  .ch.gold { color: var(--gold); background: color-mix(in srgb, var(--gold) 13%, transparent); }
  .ch.acc { color: var(--accent); background: color-mix(in srgb, var(--accent) 15%, transparent); }

  /* Profil va tugmalar */
  .profile-hero { border-radius: 26px; box-shadow: 0 14px 34px rgba(0,0,0,.25); }
  .profile-hero .av { box-shadow: 0 0 0 3px rgba(242,205,122,.6); }
  .field-box, .link-row { background: var(--glass); border: 1px solid var(--line); }
  .primary-btn { background: linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 55%, #6d4aff));
    box-shadow: 0 10px 24px color-mix(in srgb, var(--accent) 35%, transparent); }
  .type-card, .tariff-card { background: var(--glass); }
  .sheet { border-top: 1px solid var(--line); }
  @media (prefers-reduced-motion: reduce) { .hero { animation: none !important; opacity: 1 !important; } }
</style>
<script>
(function () {
  fetch("/api/theme", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (j) {
    if (!j || !j.color) return;
    var s = document.documentElement.style;
    s.setProperty("--accent", j.color);
    s.setProperty("--accent-text", "#ffffff");
  }).catch(function () {});
})();
</script>
</body>
</html>
"""


# ---------- Bot Creator ADMIN mini app (/adminapp) ----------

def get_adminapp_url() -> str:
    explicit = os.getenv("MINIAPP_URL")
    if explicit:
        return explicit.rstrip("/") + "/adminapp"
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    if domain:
        return f"https://{domain}/adminapp"
    return ""


PUBLIC_COMMANDS = [
    ("start", "🏠 Bosh menyu"),
    ("newbot", "🤖 Yangi bot yaratish"),
    ("mybots", "📁 Botlarim"),
    ("cancel", "❌ Jarayonni bekor qilish"),
]


async def setup_public_menu(bot):
    """Har bir obunachi uchun klaviatura yonidagi tugma «Botlarim» veb paneli o'rniga buyruqlar menyusi
    (/start, /newbot ...) bo'ladi. Adminlarning shaxsiy «Admin» tugmasiga tegmaydi."""
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in PUBLIC_COMMANDS])
    except Exception as e:
        logging.error(f"Buyruqlar ro'yxatini o'rnatishda xato: {e}")
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        logging.error(f"Menyu tugmasini (buyruqlar) o'rnatishda xato: {e}")


async def set_admin_menu_button(bot, uid: int) -> bool:
    """Berilgan admin chatida klaviatura yonidagi Menu tugmasini «Admin» panelga o'rnatadi (boshqalarga tegmaydi)."""
    aurl = get_adminapp_url()
    if not aurl or not uid:
        return False
    try:
        me = await bot.get_me()
        sep = "&" if "?" in aurl else "?"
        await bot.set_chat_menu_button(
            chat_id=uid,
            menu_button=MenuButtonWebApp(text="Admin", web_app=WebAppInfo(url=f"{aurl}{sep}bot={me.username}")),
        )
        return True
    except Exception as e:
        logging.info(f"Admin menyu tugmasi o'rnatilmadi ({uid}): {e}")
        return False


ADMIN_APP_HTML = """<!DOCTYPE html>
<html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Bot Creator — Admin</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:var(--tg-theme-bg-color,#fff);--tx:var(--tg-theme-text-color,#111);--hint:var(--tg-theme-hint-color,#888);--bt:var(--tg-theme-button-color,#2481cc);--bx:var(--tg-theme-button-text-color,#fff);--card:var(--tg-theme-secondary-bg-color,#f1f3f5)}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--tx);padding:12px 12px 80px}
h1{font-size:18px;margin:4px 0 10px}.tabs{display:flex;gap:6px;overflow-x:auto;padding-bottom:8px}
.tabs button{flex:none;border:0;border-radius:16px;padding:8px 14px;background:var(--card);color:var(--tx);font-size:14px}
.tabs button.on{background:var(--bt);color:var(--bx)}
.card{background:var(--card);border-radius:12px;padding:12px;margin:8px 0}.row{display:flex;justify-content:space-between;gap:8px;align-items:center}
.k{color:var(--hint);font-size:13px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.big{font-size:22px;font-weight:700}
input,textarea{width:100%;padding:10px;border-radius:10px;border:1px solid var(--hint);background:var(--bg);color:var(--tx);font-size:15px}
.btn{border:0;border-radius:10px;padding:9px 14px;background:var(--bt);color:var(--bx);font-size:14px}.btn.red{background:#d33}
.err{color:#d33;padding:20px;text-align:center}
body{padding-bottom:calc(140px + env(safe-area-inset-bottom,0px))}
.tabs{display:none;position:fixed;left:18px;right:18px;max-width:444px;margin:0 auto;z-index:16;flex-wrap:wrap;gap:8px;padding:14px;overflow-y:auto;max-height:52vh;
 bottom:calc(34px + env(safe-area-inset-bottom,0px) + 86px);background:var(--card);border-radius:24px;box-shadow:0 10px 30px rgba(0,0,0,.25)}
.tabs.open{display:flex}
.fnav{position:fixed;left:18px;right:18px;max-width:444px;margin:0 auto;z-index:15;bottom:calc(34px + env(safe-area-inset-bottom,0px));display:flex;padding:10px 6px;
 background:var(--card);border-radius:26px;box-shadow:0 8px 26px rgba(0,0,0,.2)}
.fnav button{flex:1;border:0;background:none;color:var(--hint);font:inherit;font-size:12.5px;font-weight:600;padding:4px 0}
.fnav button i{display:block;font-style:normal;font-size:23px;margin-bottom:2px}
.fnav button.on{color:var(--bt)}
.ffoot{position:fixed;left:0;right:0;z-index:14;bottom:calc(8px + env(safe-area-inset-bottom,0px));text-align:center;color:var(--hint);font-size:14px;font-weight:600;pointer-events:none}
</style></head><body>
<h1>🛠 Bot Creator — Admin panel</h1>
<div class="tabs" id="tabs"></div><div id="v"></div>
<div class="fnav" id="fnav">
<button data-k="stats"><i>📊</i>Statistika</button>
<button data-k="users"><i>👥</i>Odamlar</button>
<button data-k="bots"><i>🤖</i>Botlar</button>
<button data-k="bal"><i>💰</i>Hisob</button>
<button data-k="more"><i>⋯</i>Yana</button>
</div>
<div class="ffoot" id="ffoot">Bot Creator</div>
<script>
const tg=window.Telegram&&Telegram.WebApp;if(tg){tg.ready();tg.expand();}
const ID=(tg&&tg.initData)||"";const V=document.getElementById("v");
const TABS={stats:"📊 Statistika",users:"👥 Foydalanuvchilar",bots:"🤖 Botlar",expiring:"📅 Tugayotgan",tariffs:"💵 Tariflar",bc:"📢 Xabar"};
Object.assign(TABS,{bal:"💰 Hisob",blocked:"🚫 Bloklanganlar",top:"🏆 Top",pay:"💳 To‘lov tizimlar",stars:"⭐ Stars kursi",partners:"🤝 Hamkorlar",trial:"🎁 Sinov/Pullik",subs:"📢 Majburiy obuna",sys:"⚙️ Tizim",design:"🎨 Dizayn",texts:"📝 Matnlar"});
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const n=x=>Number(x||0).toLocaleString("ru-RU").replace(/\\u00a0/g," ");
async function why403(){try{const w=await (await fetch("/api/admin/whoami?"+new URLSearchParams({initData:ID}))).json();
if(w.state=="no_init")return "Telegram ma'lumot yubormadi. Panelni klaviatura yonidagi «Admin» menyu tugmasi orqali qayta oching (botda /start bosing).";
if(w.state=="bad_sig")return "Telegram imzosi mos kelmadi. Panel boshqa botdan ochilgan — asosiy botdagi tugmadan oching.";
if(w.state=="not_admin")return "Sizning ID: "+w.id+" — admin ro'yxatida yo'q. Botga /myid yozib ID ni tekshiring, Railway'dagi ADMIN_ID shu bo'lishi kerak.";
}catch(e){}return "Ruxsat yo'q (faqat admin)";}
async function get(p,q={}){const r=await fetch(p+"?"+new URLSearchParams({...q,initData:ID}));if(!r.ok)throw new Error(r.status==403?await why403():"Xato "+r.status);return r.json();}
async function post(p,b){const r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...b,initData:ID})});if(!r.ok)throw new Error("Xato "+r.status);return r.json();}
const T=document.getElementById("tabs");
for(const k in TABS){const b=document.createElement("button");b.textContent=TABS[k];b.id="t_"+k;b.onclick=()=>show(k);T.appendChild(b);}
async function show(k){
  for(const x in TABS)document.getElementById("t_"+x).className=x==k?"on":"";
  V.innerHTML='<div class="k">Yuklanmoqda…</div>';
  try{await VIEWS[k]();}catch(e){V.innerHTML='<div class="err">'+esc(e.message)+'</div>';}
}
const VIEWS={
async stats(){const s=await get("/api/admin/stats");
 V.innerHTML='<div class="grid"><div class="card"><div class="k">Foydalanuvchilar</div><div class="big">'+n(s.platform_users)+'</div></div>'+
 '<div class="card"><div class="k">Botlar</div><div class="big">'+n(s.total_bots)+'</div></div>'+
 '<div class="card"><div class="k">Faol botlar</div><div class="big">'+n(s.active_bots)+'</div></div>'+
 '<div class="card"><div class="k">Jami balans</div><div class="big">'+n(s.total_balance)+'</div></div></div>'+
 '<div class="card"><b>Bot turlari</b>'+s.type_counts.map(t=>'<div class="row"><span>'+esc(t.type)+'</span><b>'+t.count+'</b></div>').join("")+'</div>';},
async users(){V.innerHTML='<input id="q" placeholder="@username yoki ID qidirish"><div id="l"></div>';
 const q=document.getElementById("q");let tm;q.oninput=()=>{clearTimeout(tm);tm=setTimeout(load,300)};
 async function load(){const r=await get("/api/admin/users",{q:q.value});
  document.getElementById("l").innerHTML=r.users.map(u=>'<div class="card"><div class="row"><b>'+esc(u.username)+'</b><span class="k">ID '+u.id+'</span></div>'+
  '<div class="k">📞 '+esc(u.phone)+' · 💰 '+n(u.balance)+' · 🎁 '+u.referrals+'</div>'+
  (u.bots.length?'<div class="k">🤖 '+u.bots.map(b=>esc(b.name)).join(", ")+'</div>':'')+
  '<div style="margin-top:8px"><button class="btn'+(u.blocked?'':' red')+'" data-id="'+u.id+'">'+(u.blocked?'✅ Blokdan chiqarish':'🚫 Bloklash')+'</button></div></div>').join("")||'<div class="k">Topilmadi</div>';
  document.querySelectorAll("[data-id]").forEach(b=>b.onclick=async()=>{await post("/api/admin/block",{uid:+b.dataset.id});load();});}
 load();},
async bots(){V.innerHTML='<input id="q" placeholder="Bot nomi bo\\'yicha qidirish"><div id="l"></div>';
 const q=document.getElementById("q");let tm;q.oninput=()=>{clearTimeout(tm);tm=setTimeout(load,300)};
 async function load(){const r=await get("/api/admin/bots",{q:q.value});
  document.getElementById("l").innerHTML=r.bots.map(b=>'<div class="card"><div class="row"><b>'+esc(b.name)+'</b><span>'+(b.active?'🟢':'🔴')+'</span></div>'+
  '<div class="k">'+esc(b.type)+' · egasi '+b.owner+' · '+n(b.price)+' so\\'m</div>'+
  '<div style="margin-top:8px"><button class="btn red" data-d="'+b.id+'">🗑 O\\'chirish</button></div></div>').join("")||'<div class="k">Topilmadi</div>';
  document.querySelectorAll("[data-d]").forEach(b=>b.onclick=async()=>{if(!confirm("Bot o'chirilsinmi?"))return;await post("/api/admin/bots/delete",{id:+b.dataset.d});load();});}
 load();},
async expiring(){const r=await get("/api/admin/expiring");
 V.innerHTML=r.bots.map(b=>{const h=Math.floor(b.seconds_left/3600);return '<div class="card"><div class="row"><b>'+esc(b.name)+'</b><span class="k">'+h+' soat</span></div><div class="k">'+esc(b.type)+' · egasi '+b.owner+'</div></div>'}).join("")||'<div class="k">Tugayotgan botlar yo\\'q</div>';},
async tariffs(){const r=await get("/api/admin/tariffs");
 const L=(t,a)=>'<div class="card"><b>'+t+'</b>'+a.map(x=>'<div class="row"><span>'+esc(x.name)+'</span><b>'+n(x.price)+'</b></div>').join("")+'</div>';
 V.innerHTML=L("Kino BOT tariflari",r.pro_tariffs)+'<div class="card"><b>Bot turi narxlari</b>'+r.type_prices.map(x=>'<div class="row"><span>'+esc(x.name)+'</span><b>'+n(x.price)+'</b></div>').join("")+'</div>';},
async bc(){V.innerHTML='<textarea id="t" rows="6" placeholder="Barcha foydalanuvchilarga xabar…"></textarea><p><button class="btn" id="s">📢 Yuborish</button></p><div id="o" class="k"></div>';
 document.getElementById("s").onclick=async()=>{const t=document.getElementById("t").value.trim();if(!t||!confirm("Barchaga yuborilsinmi?"))return;
  document.getElementById("o").textContent="Yuborilmoqda…";const r=await post("/api/admin/broadcast",{text:t});document.getElementById("o").textContent="✅ Yuborildi: "+r.sent+", xato: "+r.failed;};}
};
const note=m=>{try{tg.showAlert(String(m));}catch(e){alert(m);}};
const val=id=>document.getElementById(id).value.trim();
const say=(id,t)=>{const e=document.getElementById(id);if(e)e.textContent=t;};
const wrap=f=>async()=>{try{await f();}catch(e){note(e.message);}};
const SEL='style="width:100%;padding:10px;border-radius:10px;margin-top:6px"';
Object.assign(VIEWS,{
async tariffs(){const r=await get("/api/admin/tariffs");
 const card=(pool,x)=>`<div class="card"><input id="n_${pool}_${x.id}" value="${esc(x.name)}"><div class="row" style="margin-top:6px"><input id="p_${pool}_${x.id}" type="number" value="${x.price}"><button class="btn" data-sv="${pool}|${x.id}">💾</button><button class="btn red" data-dl="${pool}|${x.id}">🗑</button></div></div>`;
 V.innerHTML="<b>Kino BOT tariflari</b>"+r.pro_tariffs.map(x=>card("pro",x)).join("")+
 `<div class="card"><b>➕ Yangi tarif</b><input id="nn" placeholder="Nomi" style="margin-top:6px"><input id="nv" type="number" placeholder="Narx (so‘m/oy)" style="margin-top:6px"><input id="nl" type="number" placeholder="Kunlik limit (ixtiyoriy)" style="margin-top:6px"><p><button class="btn" id="nadd">Qo‘shish</button></p></div>`+
 "<b>Bot turi narxlari</b>"+r.type_prices.map(x=>`<div class="card"><div class="k">${esc(x.name)}</div><div class="row" style="margin-top:6px"><input id="tp_${x.type}" type="number" value="${x.price}"><button class="btn" data-tp="${x.type}">💾</button></div></div>`).join("");
 document.querySelectorAll("[data-sv]").forEach(b=>b.onclick=wrap(async()=>{const [pool,id]=b.dataset.sv.split("|");await post("/api/admin/tariff/save",{pool,id,name:val("n_"+pool+"_"+id),price:+val("p_"+pool+"_"+id)});note("✅ Saqlandi");}));
 document.querySelectorAll("[data-dl]").forEach(b=>b.onclick=wrap(async()=>{if(!confirm("Tarif o‘chirilsinmi?"))return;const [pool,id]=b.dataset.dl.split("|");await post("/api/admin/tariff/delete",{pool,id});VIEWS.tariffs();}));
 document.querySelectorAll("[data-tp]").forEach(b=>b.onclick=wrap(async()=>{await post("/api/admin/typeprice/save",{type:b.dataset.tp,price:+val("tp_"+b.dataset.tp)});note("✅ Saqlandi");}));
 document.getElementById("nadd").onclick=wrap(async()=>{await post("/api/admin/tariff/save",{pool:"pro",name:val("nn"),price:+val("nv"),daily_limit:+val("nl")||null});VIEWS.tariffs();});},
async bal(){
 V.innerHTML=`<div class="card"><b>💰 Hisob qo‘shish / ayirish</b><input id="bu" type="number" placeholder="Foydalanuvchi ID" style="margin-top:8px"><input id="ba" type="number" placeholder="Summa (so‘m)" style="margin-top:8px"><div class="row" style="margin-top:8px"><button class="btn" id="badd">➕ Qo‘shish</button><button class="btn red" id="bsub">➖ Ayirish</button></div><div id="bo" class="k" style="margin-top:8px"></div></div>`;
 const go=mode=>wrap(async()=>{const uid=+val("bu"),amount=+val("ba");if(!uid||!(amount>0))throw new Error("ID va summani to‘g‘ri kiriting");const r=await post("/api/admin/balance",{uid,amount,mode});say("bo","✅ Yangi balans: "+n(r.balance)+" so‘m");});
 document.getElementById("badd").onclick=go("add");document.getElementById("bsub").onclick=go("sub");},
async top(){const r=await get("/api/admin/top");const med=["🥇","🥈","🥉"];
 const t3=r.users.filter(u=>u.referrals>0).slice(0,3);
 V.innerHTML='<div class="card"><b>🔝 TOP faol</b>'+(t3.map((u,i)=>'<div class="row"><span>'+med[i]+" "+esc(u.username)+'</span><b>'+u.referrals+" ta</b></div>").join("")||'<div class="k">Hozircha hech kim taklif qilmagan</div>')+'</div>'+
 '<div class="k">🚀 Ultra statistika / 🏆 Top referal — jami '+r.total+' foydalanuvchi</div>'+
 r.users.map((u,i)=>'<div class="card"><div class="row"><b>'+(i+1)+". "+esc(u.username)+'</b><span>🎁 '+u.referrals+'</span></div><div class="k">ID '+u.id+" · 📞 "+esc(u.phone)+'</div></div>').join("");},
async pay(){const r=await get("/api/admin/paysys");
 V.innerHTML=(r.items.map(p=>'<div class="card"><div class="row"><b>'+esc(p.name)+'</b><button class="btn red" data-d="'+esc(p.id)+'">🗑</button></div><div class="k">'+esc(p.number)+" · "+esc(p.owner)+'</div></div>').join("")||'<div class="k">To‘lov tizimlari mavjud emas</div>')+
 `<div class="card"><b>➕ To‘lov tizimi qo‘shish</b><input id="pn" placeholder="Nomi (Click, Payme, Humo...)" style="margin-top:6px"><input id="pr" placeholder="Karta / hisob raqami" style="margin-top:6px"><input id="po" placeholder="Egasining ismi" style="margin-top:6px"><p><button class="btn" id="pa">Qo‘shish</button></p></div>`;
 document.querySelectorAll("[data-d]").forEach(b=>b.onclick=wrap(async()=>{if(!confirm("O‘chirilsinmi?"))return;await post("/api/admin/paysys/delete",{id:b.dataset.d});VIEWS.pay();}));
 document.getElementById("pa").onclick=wrap(async()=>{await post("/api/admin/paysys/add",{name:val("pn"),number:val("pr"),owner:val("po")});VIEWS.pay();});},
async stars(){const r=await get("/api/admin/stars");
 V.innerHTML=`<div class="card"><b>⭐ Stars kursi</b><div class="k">Hozirgi kurs: 1 ⭐ = ${n(r.rate)} so‘m</div><input id="sr" type="number" value="${r.rate}" style="margin-top:8px"><p><button class="btn" id="ss">💾 Saqlash</button></p></div>`;
 document.getElementById("ss").onclick=wrap(async()=>{await post("/api/admin/stars",{rate:+val("sr")});note("✅ Saqlandi");VIEWS.stars();});},
async partners(){const r=await get("/api/admin/partners");
 V.innerHTML='<div class="k">Hamkor-adminlar foydalanuvchilarga balans qo‘sha oladi. Ular qo‘shgan foydalanuvchining birinchi oylik to‘lovi hamkor daromadiga yoziladi.</div>'+
 (r.items.map(p=>'<div class="card"><div class="row"><b>ID '+p.id+'</b><button class="btn red" data-d="'+p.id+'">🗑 Olib tashlash</button></div><div class="k">💼 Daromad: '+n(p.earnings)+' so‘m</div><input id="e_'+p.id+'" type="number" placeholder="Summa (so‘m)" style="margin-top:8px"><div class="row" style="margin-top:8px"><button class="btn" data-ea="'+p.id+'">➕ Daromad qo‘shish</button><button class="btn red" data-es="'+p.id+'">➖ Daromad minus</button></div></div>').join("")||'<div class="k">Hamkor-adminlar yo‘q</div>')+
 `<div class="card"><b>➕ Hamkor qo‘shish</b><input id="hu" type="number" placeholder="Foydalanuvchi ID" style="margin-top:6px"><p><button class="btn" id="ha">Qo‘shish</button></p></div>`;
 document.querySelectorAll("[data-d]").forEach(b=>b.onclick=wrap(async()=>{if(!confirm("Hamkorlikdan olib tashlansinmi?"))return;await post("/api/admin/partners/delete",{uid:b.dataset.d});VIEWS.partners();}));
 const eg=(attr,mode)=>document.querySelectorAll("["+attr+"]").forEach(b=>b.onclick=wrap(async()=>{const id=b.getAttribute(attr),amount=+val("e_"+id);if(!(amount>0))throw new Error("Summani kiriting");if(mode=="sub"&&!confirm(n(amount)+" so‘m daromaddan ayirilsinmi?"))return;await post("/api/admin/partners/earnings",{uid:id,amount,mode});VIEWS.partners();}));
 eg("data-ea","add");eg("data-es","sub");
 document.getElementById("ha").onclick=wrap(async()=>{await post("/api/admin/partners/add",{uid:+val("hu")});VIEWS.partners();});},
async blocked(){const r=await get("/api/admin/blocked");
 V.innerHTML=`<div class="card"><b>🚫 Bloklash</b><input id="bi" type="number" placeholder="Foydalanuvchi ID" style="margin-top:8px"><p><button class="btn red" id="bb">Bloklash</button></p></div><b>Bloklanganlar (${r.items.length})</b>`+
 (r.items.map(u=>'<div class="card"><div class="row"><b>'+esc(u.username)+'</b><span class="k">ID '+u.id+'</span></div><div class="k">📞 '+esc(u.phone)+'</div><div style="margin-top:8px"><button class="btn" data-u="'+u.id+'">✅ Blokdan chiqarish</button></div></div>').join("")||'<div class="k">Bloklangan foydalanuvchilar yo‘q</div>');
 document.querySelectorAll("[data-u]").forEach(b=>b.onclick=wrap(async()=>{await post("/api/admin/blocked/set",{uid:+b.dataset.u,blocked:false});VIEWS.blocked();}));
 document.getElementById("bb").onclick=wrap(async()=>{const uid=+val("bi");if(!uid)throw new Error("ID kiriting");await post("/api/admin/blocked/set",{uid,blocked:true});VIEWS.blocked();});},
async trial(){const r=await get("/api/admin/trial");
 V.innerHTML='<div class="k">🎁 Bepul — avval sinov muddati beriladi. 💰 Pullik — bot yaratilgach darhol to‘lov talab qilinadi.</div>'+
 r.items.map(x=>'<div class="card"><div class="row"><span>'+esc(x.name)+'</span><button class="btn'+(x.enabled?"":" red")+'" data-t="'+esc(x.type)+'">'+(x.enabled?"🎁 Bepul":"💰 Pullik")+'</button></div></div>').join("");
 document.querySelectorAll("[data-t]").forEach(b=>b.onclick=wrap(async()=>{await post("/api/admin/trial/toggle",{type:b.dataset.t});VIEWS.trial();}));},
async subs(){const r=await get("/api/admin/channels");
 V.innerHTML=(r.items.map(c=>'<div class="card"><div class="row"><b>'+esc(c.emoji+" "+c.title)+'</b><button class="btn red" data-d="'+esc(c.id)+'">🗑</button></div><div class="k">'+esc(c.link)+'</div></div>').join("")||'<div class="k">Majburiy kanallar yo‘q</div>')+
 `<div class="card"><b>➕ Qo‘shish</b><select id="ct" ${SEL}><option value="telegram">📢 Telegram kanal</option><option value="instagram">📸 Instagram</option><option value="tiktok">🎵 TikTok</option><option value="youtube">▶️ YouTube</option><option value="other">🌐 Boshqa havola</option></select><input id="cu" placeholder="Telegram uchun: @kanal_username" style="margin-top:6px"><input id="cti" placeholder="Nomi (ijtimoiy tarmoq uchun)" style="margin-top:6px"><input id="cl" placeholder="Havola (ijtimoiy tarmoq uchun)" style="margin-top:6px"><p><button class="btn" id="ca">Qo‘shish</button></p><div class="k">Telegram kanalda bot ADMIN bo‘lishi shart.</div></div>`;
 document.querySelectorAll("[data-d]").forEach(b=>b.onclick=wrap(async()=>{if(!confirm("Kanal o‘chirilsinmi?"))return;await post("/api/admin/channels/delete",{id:b.dataset.d});VIEWS.subs();}));
 document.getElementById("ca").onclick=wrap(async()=>{const r=await post("/api/admin/channels/add",{ctype:val("ct"),username:val("cu"),title:val("cti"),url:val("cl")});if(r.warning)note("⚠️ "+r.warning);VIEWS.subs();});},
async sys(){const m=await get("/api/admin/me");
 let h=`<div class="card"><b>📤 Zaxira nusxa</b><div class="k">Ma‘lumotlar fayli Telegramda o‘zingizga yuboriladi</div><p><button class="btn" id="bk">Yuborish</button></p></div>`;
 let fa=[];if(m.owner){fa=(await get("/api/admin/fulladmins")).admins;
  h+=`<div class="card"><b>👑 To‘liq adminlar</b>`+(fa.map(a=>'<div class="row" style="margin-top:6px"><span>ID '+a+'</span><button class="btn red" data-d="'+a+'">➖</button></div>').join("")||'<div class="k">Hozircha yo‘q</div>')+`<input id="fu" type="number" placeholder="Foydalanuvchi ID" style="margin-top:8px"><p><button class="btn" id="fadd">➕ Qo‘shish</button></p></div>`;}
 V.innerHTML=h;
 document.getElementById("bk").onclick=wrap(async()=>{await post("/api/admin/backup",{});note("✅ Zaxira nusxa Telegramga yuborildi");});
 if(m.owner){const t=wrap(async()=>{});
  document.querySelectorAll("[data-d]").forEach(b=>b.onclick=wrap(async()=>{if(!confirm("Olib tashlansinmi?"))return;await post("/api/admin/fulladmins/toggle",{uid:+b.dataset.d});VIEWS.sys();}));
  document.getElementById("fadd").onclick=wrap(async()=>{await post("/api/admin/fulladmins/toggle",{uid:+val("fu")});VIEWS.sys();});}}
});
Object.assign(VIEWS,{
async design(){const r=await get("/api/admin/theme");
 const sw=(f,i,c)=>'<button data-th="'+f+'-'+i+'" style="width:46px;height:46px;border-radius:14px;margin:4px 6px 4px 0;background:'+c+';border:'+(r.theme===f+'-'+i?'3px solid var(--tx)':'3px solid transparent')+'"></button>';
 V.innerHTML='<div class="k">Bot Creator mini app (Botlarim) rangi. Har bir rangdan 5 xil ohang tanlang: barcha tugmalar va urg‘u rangi o‘zgaradi.</div>'+
 Object.keys(r.palettes).map(f=>'<div class="card"><b>'+esc(r.palettes[f].name)+'</b><div style="margin-top:6px">'+r.palettes[f].shades.map((c,i)=>sw(f,i+1,c)).join("")+'</div></div>').join("")+
 '<p><button class="btn red" id="thr">↩️ Standart (Telegram rangi)</button></p>';
 const apply=k=>wrap(async()=>{await post("/api/admin/theme",{theme:k});const c=k?r.palettes[k.split("-")[0]].shades[+k.split("-")[1]-1]:"";document.documentElement.style.setProperty("--bt",c);note("✅ Saqlandi");VIEWS.design();});
 document.querySelectorAll("[data-th]").forEach(b=>b.onclick=apply(b.dataset.th));
 document.getElementById("thr").onclick=apply("");}
});
Object.assign(VIEWS,{
async texts(){const r=await get("/api/admin/texts");
 const tpost=async b=>{const x=await fetch("/api/admin/texts",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...b,initData:ID})});const j=await x.json().catch(()=>({}));if(!x.ok)throw new Error(j.error||("Xato "+x.status));return j;};
 V.innerHTML='<div class="k">Bot Creator xabar matnlari. Tahrirlangan matn bazada saqlanadi — kod yangilanganda ham o‘chmaydi. Faqat Bot Creator uchun. Qalin: &lt;b&gt;matn&lt;/b&gt;. O‘zgaruvchi: {nom}</div>'+
 r.items.map(x=>'<div class="card"><b>'+esc(x.title)+'</b>'+(x.edited?' <span class="k">✏️ tahrirlangan</span>':'')+
  '<div class="k" style="margin-top:4px">'+(x.vars.length?'O‘zgaruvchilar: '+x.vars.map(v=>'{'+esc(v.name)+'} — '+esc(v.desc)).join('; '):'O‘zgaruvchi yo‘q')+'</div>'+
  '<textarea id="tx_'+x.key+'" rows="8" style="width:100%;margin-top:6px;padding:10px;border-radius:10px">'+esc(x.text)+'</textarea>'+
  '<div class="row" style="margin-top:6px"><button class="btn" data-sv="'+x.key+'">💾 Saqlash</button><button class="btn red" data-rs="'+x.key+'">♻️ Asl holiga</button></div></div>').join("");
 document.querySelectorAll("[data-sv]").forEach(b=>b.onclick=wrap(async()=>{await tpost({action:"save",key:b.dataset.sv,text:document.getElementById("tx_"+b.dataset.sv).value});note("✅ Saqlandi");VIEWS.texts();}));
 document.querySelectorAll("[data-rs]").forEach(b=>b.onclick=wrap(async()=>{if(!confirm("Asl matnga qaytarilsinmi?"))return;await tpost({action:"reset",key:b.dataset.rs});note("♻️ Asl holiga qaytarildi");VIEWS.texts();}));
}
});
fetch("/api/theme").then(r=>r.json()).then(j=>{if(j&&j.color)document.documentElement.style.setProperty("--bt",j.color);}).catch(()=>{});
(function(){
  const MAIN=["stats","users","bots","bal"];
  const nav=document.getElementById("fnav"),tabs=document.getElementById("tabs");
  const base=show;
  function mark(k){
    nav.querySelectorAll("button").forEach(b=>{
      const bk=b.getAttribute("data-k");
      b.classList.toggle("on",bk===k||(bk==="more"&&!MAIN.includes(k)&&k!=="more"));
    });
  }
  show=function(k){tabs.classList.remove("open");mark(k);return base(k);};
  nav.addEventListener("click",function(e){
    const b=e.target.closest("[data-k]");if(!b)return;
    const k=b.getAttribute("data-k");
    if(k==="more"){tabs.classList.toggle("open");return;}
    show(k);
  });
  document.addEventListener("click",function(e){
    if(!tabs.classList.contains("open"))return;
    if(e.target.closest("#tabs")||e.target.closest("#fnav"))return;
    tabs.classList.remove("open");
  });
  const u=new URLSearchParams(location.search).get("bot");
  if(u)document.getElementById("ffoot").textContent="@"+u;
})();
show("stats");
</script></body></html>
"""


async def adminapp_page(request):
    return web.Response(
        text=ADMIN_APP_HTML,
        content_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
    )


async def miniapp_page(request):
    return web.Response(
        text=MINIAPP_HTML,
        content_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
    )


async def create_bot_record(token: str, bot_name: str, bot_type: str, uid: int, tariff_id):
    """Yangi bot yozuvini yaratadi va ishga tushiradi (chat va Mini App uchun umumiy)."""
    bot_id = data["next_bot_id"]
    data["next_bot_id"] += 1

    already_has_bot = not is_full_admin(uid) and any(
        uid in b.get("admin_ids", [b["admin_id"]]) for b in data["bots"].values()
    )

    today = datetime.now().strftime("%Y-%m-%d")
    data["bots"][token] = {
        "id": bot_id,
        "type": bot_type,
        "name": bot_name,
        "admin_id": uid,
        "admin_ids": [uid],
        "created_at": datetime.now().isoformat(),
        "paid_until": None,
        "skip_trial": already_has_bot,
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


def uid_from_init_data(init_data: str):
    """initData imzosi to'g'ri bo'lsa foydalanuvchi ID'sini qaytaradi, aks holda None."""
    parsed = validate_platform_init_data(init_data or "")
    if not parsed:
        return None
    try:
        return json.loads(parsed.get("user", "{}")).get("id")
    except Exception:
        return None


def short_description(bot_type: str) -> str:
    raw = BOT_DESCRIPTIONS.get(bot_type, "")
    first = raw.split("\n\n")[0]
    text = re.sub(r"<[^>]+>", "", first).strip()
    return text[:150]


def user_has_bot(uid: int) -> bool:
    return any(uid in b.get("admin_ids", [b["admin_id"]]) for b in data["bots"].values())


# Mini app orqali yaratish ro'yxatidan yashiriladigan bot turlari
MINIAPP_HIDDEN_TYPES = ("stars",)


async def api_create_info(request):
    uid = uid_from_init_data(request.query.get("initData", ""))
    if uid is None:
        return web.json_response({"error": "invalid_init_data"}, status=401, headers={"Cache-Control": "no-store"})
    full_admin = is_full_admin(uid)
    types = []
    for key, name in BOT_TYPES.items():
        if key in MINIAPP_HIDDEN_TYPES:
            continue
        trial = get_trial_config(key)
        item = {
            "key": key,
            "name": name,
            "short": short_description(key),
            "trial_enabled": bool(trial.get("enabled", True)),
            "trial_days": trial.get("days", TRIAL_DAYS),
            "creation_price": cheapest_tariff_price("kino_pro") if (key == "kino_pro" and not full_admin) else 0,
        }
        if key in ("kino_pro",):
            item["monthly_price"] = None
            item["tariffs"] = [
                {"id": tid, "name": t["name"], "price": t["price"], "limit": tariff_limit_text(t), "speed": t.get("speed", "")}
                for tid, t in tariffs_for(key).items()
            ]
        else:
            item["monthly_price"] = data["type_prices"].get(key, data.get("other_bot_price", DEFAULT_OTHER_BOT_PRICE))
            item["tariffs"] = []
        types.append(item)
    return web.json_response({
        "balance": data["user_balances"].get(str(uid), 0),
        "has_bot": (not full_admin) and user_has_bot(uid),
        "blocked": (uid in data["blocked_users"]) and not full_admin,
        "types": types,
    }, headers={"Cache-Control": "no-store"})


def _create_error(code: str, message: str, status: int = 400, **extra):
    payload = {"error": code, "message": message}
    payload.update(extra)
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


async def api_create_bot(request):
    try:
        body = await request.json()
    except Exception:
        return _create_error("bad_request", "So'rov noto'g'ri.")
    uid = uid_from_init_data(body.get("initData", ""))
    if uid is None:
        return _create_error("invalid_init_data", "Sessiya eskirgan. Mini App'ni qayta oching.", 401)
    full_admin = is_full_admin(uid)
    if uid in data["blocked_users"] and not full_admin:
        return _create_error("blocked", "Siz Bot Creator'dan foydalanishdan bloklangansiz.", 403)

    bot_type = body.get("bot_type")
    if bot_type not in BOT_TYPES or bot_type in MINIAPP_HIDDEN_TYPES:
        return _create_error("bad_type", "Bot shabloni tanlanmagan.")

    token = (body.get("token") or "").strip()
    if ":" not in token or len(token) < 20 or " " in token:
        return _create_error("bad_token", "Token noto'g'ri. @BotFather bergan tokenni to'liq kiriting.")
    if token in data["bots"]:
        return _create_error("duplicate", "Bu bot allaqachon platformada mavjud.")

    tariff_id = None
    if bot_type in ("kino_pro",):
        tariff_id = str(body.get("tariff_id") or "")
        if tariff_id not in tariffs_for(bot_type):
            return _create_error("bad_tariff", "Tarif tanlanmagan.")

    charge = 0
    if bot_type == "kino_pro" and not full_admin:
        charge = cheapest_tariff_price("kino_pro")
        balance = data["user_balances"].get(str(uid), 0)
        if balance < charge:
            return _create_error(
                "insufficient",
                f"Balansingizda yetarli mablag' yo'q. Kerak: {charge:,} so'm, joriy balans: {balance:,} so'm.",
                needed=charge, balance=balance,
            )

    test_bot = None
    try:
        test_bot = Bot(token=token)
        me = await asyncio.wait_for(test_bot.get_me(), timeout=12)
    except Exception:
        return _create_error("bad_token", "Token noto'g'ri yoki Telegram javob bermadi. Tokenni tekshirib qayta urinib ko'ring.")
    finally:
        if test_bot is not None:
            try:
                await test_bot.session.close()
            except Exception:
                pass

    custom_name = (body.get("bot_name") or "").strip()[:60]
    bot_name = custom_name or me.first_name

    try:
        info = await create_bot_record(token, bot_name, bot_type, uid, tariff_id)
    except Exception as e:
        logging.error(f"Mini App orqali bot yaratishda xato: {e}")
        data["bots"].pop(token, None)
        save_data()
        return _create_error("server_error", "Botni ishga tushirib bo'lmadi. Birozdan so'ng qayta urinib ko'ring.", 500)

    if charge:
        data["user_balances"][str(uid)] = data["user_balances"].get(str(uid), 0) - charge
        save_data()

    trial = get_trial_config(bot_type)
    return web.json_response({
        "ok": True,
        "bot": {
            "name": info["name"],
            "username": me.username or "",
            "type": BOT_TYPES.get(bot_type, bot_type),
            "skip_trial": bool(info.get("skip_trial")),
            "trial_enabled": bool(trial.get("enabled", True)),
            "trial_days": trial.get("days", TRIAL_DAYS),
            "monthly_price": get_bot_tariff(info)["price"],
            "charge": charge,
        },
    }, headers={"Cache-Control": "no-store"})


async def api_mybots(request):
    init_data = request.query.get("initData", "")
    parsed = validate_platform_init_data(init_data)
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
                "id": info.get("id"),
                "kino": info.get("type") in ("kino_pro",),
            })
    balance = data["user_balances"].get(str(uid), 0)
    active_count = sum(1 for b in bots_list if b["active"])
    refcount = len(data["platform_referrals"].get(str(uid), []))
    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")
    full_name = (first_name + " " + last_name).strip() or "Foydalanuvchi"
    username = f"@{user['username']}" if user.get("username") else ""
    return web.json_response({
        "bots": bots_list,
        "balance": balance,
        "is_admin": is_full_admin(uid),
        "profile": {"name": full_name, "username": username, "id": uid},
        "stats": {"total_bots": len(bots_list), "active_bots": active_count, "referrals": refcount},
    }, headers={"Cache-Control": "no-store"})


def is_admin_init_data(init_data: str) -> bool:
    """initData imzosini tekshiradi va faqat ADMIN_ID bo'lsa True qaytaradi."""
    parsed = validate_platform_init_data(init_data or "")
    if not parsed:
        return False
    try:
        user = json.loads(parsed.get("user", "{}"))
        uid = user.get("id")
    except Exception:
        return False
    return is_full_admin(uid)


async def api_admin_stats(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    type_counts = {}
    for b in data["bots"].values():
        type_counts[b["type"]] = type_counts.get(b["type"], 0) + 1
    return web.json_response({
        "platform_users": len(data["platform_users"]),
        "total_bots": len(data["bots"]),
        "active_bots": sum(1 for i in data["bots"].values() if is_active(i)),
        "type_counts": [{"type": BOT_TYPES.get(bt, bt), "count": c} for bt, c in sorted(type_counts.items(), key=lambda x: x[1], reverse=True)],
        "users_with_balance": len(data["user_balances"]),
        "total_balance": sum(data["user_balances"].values()),
    }, headers={"Cache-Control": "no-store"})


async def api_admin_users(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    query = request.query.get("q", "").strip().lower().lstrip("@")
    results = []
    for uid_str, uinfo in data["platform_user_info"].items():
        uname = (uinfo.get("username") or "").lstrip("@").lower()
        if query and query not in uname and query != uid_str:
            continue
        uid = int(uid_str)
        results.append({
            "id": uid,
            "username": uinfo.get("username") or "—",
            "phone": uinfo.get("phone") or "—",
            "balance": data["user_balances"].get(uid_str, 0),
            "referrals": len(data["platform_referrals"].get(uid_str, [])),
            "blocked": uid in data["blocked_users"],
            "bots": [{"name": b["name"], "type": BOT_TYPES.get(b["type"], b["type"])} for b in data["bots"].values() if uid in b.get("admin_ids", [b["admin_id"]])],
        })
        if len(results) >= 50:
            break
    return web.json_response({"users": results}, headers={"Cache-Control": "no-store"})


async def api_admin_block(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    uid = int(body.get("uid", 0))
    if is_full_admin(uid) or not uid:
        return web.json_response({"error": "invalid"}, status=400)
    if uid in data["blocked_users"]:
        data["blocked_users"].remove(uid)
        blocked = False
    else:
        data["blocked_users"].append(uid)
        blocked = True
    save_data()
    return web.json_response({"blocked": blocked})


async def api_admin_tariffs(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    pro_tariffs = [{"id": tid, "name": t["name"], "price": t["price"], "daily_limit": t.get("daily_limit")} for tid, t in data["pro_tariffs"].items()]
    type_prices = [{"type": bt, "name": BOT_TYPES.get(bt, bt), "price": price} for bt, price in data["type_prices"].items()]
    return web.json_response({"pro_tariffs": pro_tariffs, "type_prices": type_prices}, headers={"Cache-Control": "no-store"})


async def api_admin_tariff_save(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    pool = data["pro_tariffs"]
    tid = body.get("id")
    try:
        price = int(body.get("price", 0))
        if price <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid_price"}, status=400)
    if tid and tid in pool:
        pool[tid]["price"] = price
        if body.get("name"):
            pool[tid]["name"] = body["name"]
    else:
        name = body.get("name") or "Yangi tarif"
        limit = body.get("daily_limit")
        new_id = str(max((int(k) for k in pool.keys() if k.isdigit()), default=0) + 1)
        pool[new_id] = {"name": name, "price": price, "daily_limit": limit if limit else None}
    save_data()
    return web.json_response({"ok": True})


async def api_admin_tariff_delete(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    pool = data["pro_tariffs"]
    tid = body.get("id")
    pool.pop(tid, None)
    save_data()
    return web.json_response({"ok": True})


async def api_admin_texts(request):
    """Bot Creator matnlari: GET — ro'yxat, POST {action: save|reset, key, text}. Faqat to'liq adminlar."""
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)
        if not is_admin_init_data(body.get("initData", "")):
            return web.json_response({"error": "forbidden"}, status=403)
        key = body.get("key")
        if key not in TEXT_REGISTRY:
            return web.json_response({"error": "Noma'lum matn"}, status=400)
        action = body.get("action")
        if action == "reset":
            text_reset(key)
            return web.json_response({"ok": True})
        if action == "save":
            text = body.get("text")
            if not isinstance(text, str):
                return web.json_response({"error": "Matn yuborilmadi"}, status=400)
            ok, err = text_validate(key, text)
            if not ok:
                return web.json_response({"error": err}, status=400)
            text_save(key, text)
            return web.json_response({"ok": True})
        return web.json_response({"error": "Noma'lum amal"}, status=400)
    if not is_admin_init_data(request.query.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    items = [
        {
            "key": k,
            "title": e["title"],
            "vars": [{"name": n, "desc": d} for n, d in e["vars"].items()],
            "text": text_template(k),
            "edited": text_is_edited(k),
        }
        for k, e in TEXT_REGISTRY.items()
    ]
    return web.json_response({"items": items}, headers={"Cache-Control": "no-store"})


async def api_admin_typeprice_save(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    bt = body.get("type")
    try:
        price = int(body.get("price", 0))
        if price <= 0 or bt not in data["type_prices"]:
            raise ValueError
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid"}, status=400)
    data["type_prices"][bt] = price
    save_data()
    return web.json_response({"ok": True})


async def api_admin_expiring(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    now = datetime.now()
    soon = []
    for b in data["bots"].values():
        if is_full_admin(b.get("admin_id")):
            continue
        paid_until = b.get("paid_until")
        if paid_until:
            expiry = datetime.fromisoformat(paid_until)
        else:
            trial_cfg = get_trial_config(b.get("type"))
            if not trial_cfg.get("enabled", True):
                continue
            expiry = datetime.fromisoformat(b["created_at"]) + timedelta(days=trial_cfg.get("days", TRIAL_DAYS))
        remaining = (expiry - now).total_seconds()
        if 0 <= remaining <= 2 * 86400:
            soon.append({"id": b["id"], "name": b["name"], "type": BOT_TYPES.get(b["type"], b["type"]), "owner": b["admin_id"], "seconds_left": int(remaining)})
    soon.sort(key=lambda x: x["seconds_left"])
    return web.json_response({"bots": soon}, headers={"Cache-Control": "no-store"})


async def api_admin_bots(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    query = request.query.get("q", "").strip().lower()
    results = []
    for b in data["bots"].values():
        if query and query not in b["name"].lower():
            continue
        tariff = get_bot_tariff(b)
        results.append({
            "id": b["id"],
            "name": b["name"],
            "type": BOT_TYPES.get(b["type"], b["type"]),
            "owner": b["admin_id"],
            "active": is_active(b),
            "price": tariff["price"],
        })
        if len(results) >= 50:
            break
    return web.json_response({"bots": results}, headers={"Cache-Control": "no-store"})


async def api_admin_bot_delete(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    bot_id = int(body.get("id", 0))
    target_token = None
    for token, b in data["bots"].items():
        if b["id"] == bot_id:
            target_token = token
            break
    if not target_token:
        return web.json_response({"error": "not_found"}, status=404)
    task = running_bots.pop(target_token, None)
    if task:
        task.cancel()
    del data["bots"][target_token]
    save_data()
    return web.json_response({"ok": True})


async def api_admin_broadcast(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty"}, status=400)
    sent, failed = 0, 0
    for uid in data["platform_users"]:
        try:
            await main_bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    return web.json_response({"sent": sent, "failed": failed})


# ---------- ADMIN mini app: qo'shimcha API (botdagi admin paneldan ko'chirilgan bo'limlar) ----------

def _forbidden():
    return web.json_response({"error": "forbidden"}, status=403)


def _admin_uid_from_init(init_data: str):
    parsed = validate_platform_init_data(init_data or "")
    if not parsed:
        return None
    try:
        uid = json.loads(parsed.get("user", "{}")).get("id")
    except Exception:
        return None
    return uid if uid and is_full_admin(uid) else None


async def api_admin_me(request):
    uid = _admin_uid_from_init(request.query.get("initData", ""))
    if not uid:
        return _forbidden()
    return web.json_response({"id": uid, "owner": uid == ADMIN_ID}, headers=_NO_STORE_ADMIN)


_NO_STORE_ADMIN = {"Cache-Control": "no-store"}


async def api_admin_whoami(request):
    """Admin panel 403 bersa, sababini ko'rsatadi (ADMIN_ID ni oshkor qilmaydi)."""
    init_data = request.query.get("initData", "")
    if not init_data:
        return web.json_response({"state": "no_init"}, headers=_NO_STORE_ADMIN)
    parsed = validate_platform_init_data(init_data)
    if not parsed:
        return web.json_response({"state": "bad_sig"}, headers=_NO_STORE_ADMIN)
    try:
        uid = json.loads(parsed.get("user", "{}")).get("id")
    except Exception:
        uid = None
    ok = bool(uid) and is_full_admin(uid)
    return web.json_response({"state": "ok" if ok else "not_admin", "id": uid}, headers=_NO_STORE_ADMIN)


async def api_admin_balance(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    try:
        target = int(body.get("uid"))
        amount = int(body.get("amount"))
        if amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid"}, status=400)
    key = str(target)
    sub = body.get("mode") == "sub"
    data["user_balances"][key] = data["user_balances"].get(key, 0) + (-amount if sub else amount)
    save_data()
    bal = data["user_balances"][key]
    try:
        if sub:
            txt = f"⚠️ Hisobingizdan administrator tomonidan {amount:,} so'm ayirildi.\n💰 Joriy balans: {bal:,} so'm"
        else:
            txt = f"✅ Hisobingizga administrator tomonidan {amount:,} so'm qo'shildi.\n💰 Joriy balans: {bal:,} so'm"
        await main_bot.send_message(chat_id=target, text=txt)
    except Exception as e:
        logging.error(f"Foydalanuvchiga xabar yuborishda xato: {e}")
    return web.json_response({"balance": bal})


async def api_admin_top(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return _forbidden()
    rows = []
    for uid in data["platform_users"]:
        uinfo = data["platform_user_info"].get(str(uid), {})
        rows.append({
            "id": uid,
            "username": uinfo.get("username") or "—",
            "phone": uinfo.get("phone") or "—",
            "referrals": len(data["platform_referrals"].get(str(uid), [])),
        })
    rows.sort(key=lambda r: r["referrals"], reverse=True)
    return web.json_response({"total": len(rows), "users": rows[:200]}, headers=_NO_STORE_ADMIN)


async def api_admin_paysys(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return _forbidden()
    items = [{"id": k, "name": p.get("name", ""), "number": p.get("number", ""), "owner": p.get("owner", "")} for k, p in data["payment_systems"].items()]
    return web.json_response({"items": items}, headers=_NO_STORE_ADMIN)


async def api_admin_paysys_add(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    name = (body.get("name") or "").strip()
    number = (body.get("number") or "").strip()
    owner = (body.get("owner") or "").strip()
    if not (name and number and owner):
        return web.json_response({"error": "empty"}, status=400)
    data["payment_systems"][uuid.uuid4().hex[:8]] = {"name": name, "number": number, "owner": owner}
    save_data()
    return web.json_response({"ok": True})


async def api_admin_paysys_delete(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    data["payment_systems"].pop(str(body.get("id")), None)
    save_data()
    return web.json_response({"ok": True})


async def api_admin_stars(request):
    if request.method == "GET":
        if not is_admin_init_data(request.query.get("initData", "")):
            return _forbidden()
        return web.json_response({"rate": data.get("stars_rate", 250)}, headers=_NO_STORE_ADMIN)
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    try:
        rate = int(body.get("rate"))
        if rate <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid"}, status=400)
    data["stars_rate"] = rate
    save_data()
    return web.json_response({"ok": True, "rate": rate})


async def api_admin_partners(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return _forbidden()
    items = [{"id": uid, "earnings": s.get("earnings", 0)} for uid, s in data["sub_admins"].items()]
    return web.json_response({"items": items}, headers=_NO_STORE_ADMIN)


async def api_admin_partner_add(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    try:
        target = int(body.get("uid"))
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid"}, status=400)
    data["sub_admins"].setdefault(str(target), {"earnings": 0})
    save_data()
    try:
        await main_bot.send_message(
            target,
            "🎉 Sizga Bot Creator platformasida hamkor-admin huquqi berildi!\n\n"
            "Endi \"➕ Hisob qo'shish\" tugmasi orqali foydalanuvchilarga balans qo'sha olasiz, "
            "va ular to'lagan birinchi oylik to'lov sizning daromadingizga yoziladi.",
        )
    except Exception:
        pass
    return web.json_response({"ok": True})


async def api_admin_partner_delete(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    data["sub_admins"].pop(str(body.get("uid")), None)
    save_data()
    return web.json_response({"ok": True})


async def api_admin_trial(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return _forbidden()
    items = []
    for bt, name in BOT_TYPES.items():
        cfg = get_trial_config(bt)
        items.append({"type": bt, "name": name, "enabled": bool(cfg.get("enabled", True)), "days": cfg.get("days", TRIAL_DAYS)})
    return web.json_response({"items": items}, headers=_NO_STORE_ADMIN)


async def api_admin_trial_toggle(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    bt = body.get("type")
    if bt not in BOT_TYPES:
        return web.json_response({"error": "invalid"}, status=400)
    cfg = data["bot_type_trial"].setdefault(bt, {"enabled": True, "days": TRIAL_DAYS})
    cfg["enabled"] = not cfg.get("enabled", True)
    save_data()
    return web.json_response({"enabled": cfg["enabled"]})


async def api_admin_channels(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return _forbidden()
    items = []
    for cid, c in data["platform_channels"].items():
        ctype = c.get("type", "telegram")
        link = c.get("url") or c.get("username") or ""
        items.append({"id": cid, "title": c.get("title", ""), "emoji": SOCIAL_EMOJI.get(ctype, "📢"), "link": link})
    return web.json_response({"items": items}, headers=_NO_STORE_ADMIN)


async def api_admin_channel_add(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    ctype = body.get("ctype") or "telegram"
    if ctype == "telegram":
        username = (body.get("username") or "").strip()
        if not username:
            return web.json_response({"error": "Kanal usernameni kiriting"}, status=400)
        try:
            chat = await main_bot.get_chat(username)
        except Exception as e:
            return web.json_response({"error": f"Kanal topilmadi: {e}"}, status=400)
        data["platform_channels"][str(chat.id)] = {"type": "telegram", "username": username, "title": chat.title}
        save_data()
        warning = ""
        try:
            me = await main_bot.get_me()
            member = await main_bot.get_chat_member(chat_id=chat.id, user_id=me.id)
            if member.status not in ("administrator", "creator"):
                warning = f"Bot \"{chat.title}\" kanalida ADMIN emas. Obuna tekshiruvi ishlashi uchun botni kanalga ADMIN qiling!"
        except Exception:
            warning = f"Bot \"{chat.title}\" kanalida ADMIN ekanligini tekshira olmadim. Botni kanalga ADMIN qiling!"
        return web.json_response({"ok": True, "warning": warning})
    title = (body.get("title") or "").strip()
    url = (body.get("url") or "").strip()
    if not (title and url):
        return web.json_response({"error": "Nom va havolani kiriting"}, status=400)
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    data["platform_channels"][f"social_{uuid.uuid4().hex[:8]}"] = {"type": ctype, "title": title, "url": url}
    save_data()
    return web.json_response({"ok": True, "warning": ""})


async def api_admin_channel_delete(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    data["platform_channels"].pop(str(body.get("id")), None)
    save_data()
    return web.json_response({"ok": True})


async def api_admin_backup(request):
    body = await request.json()
    uid = _admin_uid_from_init(body.get("initData", ""))
    if not uid:
        return _forbidden()
    from aiogram.types import BufferedInputFile
    save_data()
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        await main_bot.send_document(
            uid,
            BufferedInputFile(raw, filename="bots_data.json"),
            caption=f"📤 Zaxira nusxa — {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
        )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True})


async def api_admin_fulladmins(request):
    uid = _admin_uid_from_init(request.query.get("initData", ""))
    if uid != ADMIN_ID:
        return _forbidden()
    return web.json_response({"admins": list(data["full_admins"])}, headers=_NO_STORE_ADMIN)


async def api_admin_fulladmin_toggle(request):
    body = await request.json()
    uid = _admin_uid_from_init(body.get("initData", ""))
    if uid != ADMIN_ID:
        return _forbidden()
    try:
        target = int(body.get("uid"))
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid"}, status=400)
    if target == ADMIN_ID:
        return web.json_response({"error": "Siz allaqachon bosh adminsiz"}, status=400)
    if target in data["full_admins"]:
        data["full_admins"].remove(target)
        added = False
    else:
        data["full_admins"].append(target)
        added = True
        try:
            await main_bot.send_message(
                target,
                "👑 Sizga Bot Creator'da <b>to'liq admin</b> huquqi berildi — endi bosh admin bilan bir xil "
                "barcha imkoniyatlardan foydalana olasiz.\n\n/start bosing.",
            )
        except Exception:
            pass
    save_data()
    return web.json_response({"added": added})



async def api_admin_partner_earnings(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    key = str(body.get("uid"))
    sub = data["sub_admins"].get(key)
    if sub is None:
        return web.json_response({"error": "Hamkor topilmadi"}, status=404)
    try:
        amount = int(body.get("amount"))
        if amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return web.json_response({"error": "Musbat butun summa kiriting"}, status=400)
    if body.get("mode") == "sub":
        if amount > sub.get("earnings", 0):
            return web.json_response({"error": "Daromaddan ko'p ayirib bo'lmaydi (joriy: %s so'm)" % f"{sub.get('earnings', 0):,}"}, status=400)
        sub["earnings"] = sub.get("earnings", 0) - amount
        txt = f"⚠️ Daromadingizdan {amount:,} so'm ayirildi.\n💼 Joriy daromad: {sub['earnings']:,} so'm"
    else:
        sub["earnings"] = sub.get("earnings", 0) + amount
        txt = f"✅ Daromadingizga {amount:,} so'm qo'shildi.\n💼 Joriy daromad: {sub['earnings']:,} so'm"
    save_data()
    try:
        await main_bot.send_message(int(key), txt)
    except Exception:
        pass
    return web.json_response({"earnings": sub["earnings"]})


async def api_admin_blocked(request):
    if not is_admin_init_data(request.query.get("initData", "")):
        return _forbidden()
    items = []
    for uid in data["blocked_users"]:
        uinfo = data["platform_user_info"].get(str(uid), {})
        items.append({"id": uid, "username": uinfo.get("username") or "—", "phone": uinfo.get("phone") or "—"})
    return web.json_response({"items": items}, headers=_NO_STORE_ADMIN)


async def api_admin_blocked_set(request):
    body = await request.json()
    if not is_admin_init_data(body.get("initData", "")):
        return _forbidden()
    try:
        uid = int(body.get("uid"))
    except (ValueError, TypeError):
        return web.json_response({"error": "Noto'g'ri ID"}, status=400)
    if is_full_admin(uid):
        return web.json_response({"error": "Bosh admin yoki to'liq adminni bloklab bo'lmaydi"}, status=400)
    if body.get("blocked"):
        if uid not in data["blocked_users"]:
            data["blocked_users"].append(uid)
    else:
        if uid in data["blocked_users"]:
            data["blocked_users"].remove(uid)
            try:
                await main_bot.send_message(uid, "✅ Siz Bot Creator'da blokdan chiqarildingiz. /start bosing.")
            except Exception:
                pass
    save_data()
    return web.json_response({"ok": True})



async def api_theme(request):
    """Bot Creator mini app rangi (hamma uchun ochiq, faqat rang qaytaradi)."""
    key = data.get("miniapp_theme", "")
    return web.json_response({"theme": key, "color": theme_color(key)}, headers={"Cache-Control": "no-store"})


async def api_admin_theme(request):
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not is_admin_init_data(body.get("initData", "")):
            return _forbidden()
        key = str(body.get("theme", ""))
        if key != "" and not theme_color(key):
            return web.json_response({"error": "bad_theme"}, status=400)
        data["miniapp_theme"] = key
        save_data()
    elif not is_admin_init_data(request.query.get("initData", "")):
        return _forbidden()
    return web.json_response(
        {"theme": data.get("miniapp_theme", ""), "palettes": THEME_PALETTES},
        headers={"Cache-Control": "no-store"},
    )


async def start_web_server():
    app = web.Application(client_max_size=1 * 1024 * 1024)   # rasm yuklash olib tashlangan — xotirani tejaymiz
    app.router.add_get("/miniapp", miniapp_page)
    app.router.add_get("/botstats", botstats_page)
    app.router.add_get("/api/botstats", api_botstats)
    app.router.add_get("/adminapp", adminapp_page)
    app.router.add_get("/kinopanel", kinopanel_page)
    app.router.add_get("/api/theme", api_theme)
    app.router.add_get("/api/admin/theme", api_admin_theme)
    app.router.add_post("/api/admin/theme", api_admin_theme)
    app.router.add_get("/api/kino/overview", kino_api_overview)
    app.router.add_get("/api/kino/movies", kino_api_movies)
    app.router.add_post("/api/kino/movie/delete", kino_api_movie_delete)
    app.router.add_post("/api/kino/movie/vip", kino_api_movie_vip)
    app.router.add_get("/api/kino/users", kino_api_users)
    app.router.add_post("/api/kino/user/block", kino_api_user_block)
    app.router.add_get("/api/kino/payments", kino_api_payments)
    app.router.add_get("/api/kino/settings", kino_api_settings)
    app.router.add_post("/api/kino/settings", kino_api_settings)
    app.router.add_post("/api/kino/profile", kino_api_profile)
    app.router.add_get("/api/kino/channels", kino_api_channels)
    app.router.add_post("/api/kino/channels", kino_api_channels)
    app.router.add_get("/api/kino/admins", kino_api_admins)
    app.router.add_post("/api/kino/admins", kino_api_admins)
    app.router.add_get("/api/kino/ads", kino_api_ads)
    app.router.add_post("/api/kino/ads", kino_api_ads)
    app.router.add_post("/api/kino/autopost", kino_api_autopost)
    app.router.add_get("/api/mybots", api_mybots)
    app.router.add_get("/api/create_info", api_create_info)
    app.router.add_post("/api/create_bot", api_create_bot)
    app.router.add_get("/api/admin/stats", api_admin_stats)
    app.router.add_get("/api/admin/users", api_admin_users)
    app.router.add_post("/api/admin/block", api_admin_block)
    app.router.add_get("/api/admin/tariffs", api_admin_tariffs)
    app.router.add_post("/api/admin/tariff/save", api_admin_tariff_save)
    app.router.add_post("/api/admin/tariff/delete", api_admin_tariff_delete)
    app.router.add_post("/api/admin/typeprice/save", api_admin_typeprice_save)
    app.router.add_get("/api/admin/expiring", api_admin_expiring)
    app.router.add_get("/api/admin/bots", api_admin_bots)
    app.router.add_post("/api/admin/bots/delete", api_admin_bot_delete)
    app.router.add_post("/api/admin/broadcast", api_admin_broadcast)
    app.router.add_get("/api/admin/me", api_admin_me)
    app.router.add_get("/api/admin/texts", api_admin_texts)
    app.router.add_post("/api/admin/texts", api_admin_texts)
    app.router.add_get("/api/admin/whoami", api_admin_whoami)
    app.router.add_post("/api/admin/balance", api_admin_balance)
    app.router.add_get("/api/admin/top", api_admin_top)
    app.router.add_get("/api/admin/paysys", api_admin_paysys)
    app.router.add_post("/api/admin/paysys/add", api_admin_paysys_add)
    app.router.add_post("/api/admin/paysys/delete", api_admin_paysys_delete)
    app.router.add_post("/pay/click/{bot_id}", pay_webhook_click)
    app.router.add_post("/pay/payme/{bot_id}", pay_webhook_payme)
    app.router.add_post("/pay/octo/{bot_id}", pay_webhook_octo)
    app.router.add_get("/api/admin/stars", api_admin_stars)
    app.router.add_post("/api/admin/stars", api_admin_stars)
    app.router.add_get("/api/admin/partners", api_admin_partners)
    app.router.add_post("/api/admin/partners/add", api_admin_partner_add)
    app.router.add_post("/api/admin/partners/delete", api_admin_partner_delete)
    app.router.add_post("/api/admin/partners/earnings", api_admin_partner_earnings)
    app.router.add_get("/api/admin/blocked", api_admin_blocked)
    app.router.add_post("/api/admin/blocked/set", api_admin_blocked_set)
    app.router.add_get("/api/admin/trial", api_admin_trial)
    app.router.add_post("/api/admin/trial/toggle", api_admin_trial_toggle)
    app.router.add_get("/api/admin/channels", api_admin_channels)
    app.router.add_post("/api/admin/channels/add", api_admin_channel_add)
    app.router.add_post("/api/admin/channels/delete", api_admin_channel_delete)
    app.router.add_post("/api/admin/backup", api_admin_backup)
    app.router.add_get("/api/admin/fulladmins", api_admin_fulladmins)
    app.router.add_post("/api/admin/fulladmins/toggle", api_admin_fulladmin_toggle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Mini App veb-server {port}-portda ishga tushdi")


# ---------- Kino bot Web paneli (har bir kino bot uchun alohida Mini App) ----------
KINO_TYPES = ("kino_pro",)
_NO_STORE = {"Cache-Control": "no-store"}


def get_kinopanel_url(bot_id) -> str:
    """Kino bot paneli URL'i: https://<domen>/kinopanel?b=<bot_id>"""
    if bot_id is None:
        return ""
    explicit = os.getenv("MINIAPP_URL")
    if explicit:
        base = explicit.rstrip("/")
    else:
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
        if not domain:
            return ""
        base = f"https://{domain}"
    return f"{base}/kinopanel?b={bot_id}"


def _kino_today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _kino_count_view(info: dict):
    """Kunlik ko'rishlar hisobini yuritadi (panel grafigi uchun)."""
    dv = info.setdefault("daily_views", {})
    today = _kino_today()
    dv[today] = dv.get(today, 0) + 1
    if len(dv) > 60:
        for k in sorted(dv.keys())[:-45]:
            dv.pop(k, None)


def log_kino_payment(info: dict, uid, tariff: dict, src: str):
    """Premium to'lovini panel uchun jurnalga yozadi."""
    log = info.setdefault("payment_log", [])
    log.append({
        "uid": uid,
        "tariff": tariff.get("name", "-"),
        "days": tariff.get("days", 0),
        "amount": tariff.get("price", 0),
        "date": datetime.now().isoformat(),
        "src": src,
    })
    if len(log) > 500:
        del log[:-500]


def kino_find_bot(bot_id):
    try:
        bid = int(bot_id)
    except Exception:
        return None, None
    for token, info in data["bots"].items():
        if info.get("id") == bid and info.get("type") in KINO_TYPES:
            return token, info
    return None, None


def kino_auth(request):
    """(token, info, uid, xato_javobi) qaytaradi. Faqat shu botning adminlari kira oladi."""
    token, info = kino_find_bot(request.query.get("b"))
    if not info:
        return None, None, None, web.json_response({"error": "bot_not_found"}, status=404, headers=_NO_STORE)
    init_data = request.query.get("initData", "")
    # Panel kino botning o'zidan ham, Bot Creator paneli ichidan ham ochilishi mumkin —
    # shuning uchun initData imzosi ikkala token bilan tekshiriladi.
    parsed = validate_webapp_init_data(init_data, token) or validate_platform_init_data(init_data)
    if not parsed:
        return None, None, None, web.json_response({"error": "invalid_init_data"}, status=401, headers=_NO_STORE)
    try:
        uid = json.loads(parsed.get("user", "{}")).get("id")
    except Exception:
        uid = None
    if not uid:
        return None, None, None, web.json_response({"error": "no_user"}, status=400, headers=_NO_STORE)
    if not (is_admin(info, uid) or is_full_admin(uid)):
        return None, None, None, web.json_response({"error": "forbidden"}, status=403, headers=_NO_STORE)
    return token, info, uid, None


def kino_title(code: str, m: dict) -> str:
    return m.get("title") or (m.get("desc") or "").strip()[:40] or f"Kino #{code}"


def kino_episodes(m: dict) -> int:
    return len(m.get("episodes", {})) if m.get("type") == "series" else 0


def kino_days_left(info: dict) -> int:
    """Tarif/sinov muddatidan necha kun qolgani. -1 = cheksiz."""
    if is_full_admin(info.get("admin_id")):
        return -1
    now = datetime.now()
    end = None
    paid_until = info.get("paid_until")
    try:
        if paid_until:
            end = datetime.fromisoformat(paid_until)
        elif not info.get("skip_trial"):
            cfg = get_trial_config(info.get("type"))
            if cfg.get("enabled", True):
                end = datetime.fromisoformat(info["created_at"]) + timedelta(days=cfg.get("days", TRIAL_DAYS))
    except Exception:
        end = None
    if not end or end <= now:
        return 0
    return int((end - now).total_seconds() // 86400) + 1


async def kino_api_overview(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    now = datetime.now()
    today = _kino_today()
    month = now.strftime("%Y-%m")
    movies = info.get("movies", {})
    daily_views = info.get("daily_views", {})
    join_log = info.get("join_log", {})
    usage = info.get("daily_usage", {})
    active_today = len(usage.get("users", [])) if usage.get("date") == today else 0
    vip_active = 0
    for u in info.get("premium_users", {}).values():
        try:
            if datetime.fromisoformat(u["until"]) > now:
                vip_active += 1
        except Exception:
            pass
    log = info.get("payment_log", [])
    rev_today = sum(p.get("amount", 0) for p in log if p.get("date", "")[:10] == today)
    rev_month = sum(p.get("amount", 0) for p in log if p.get("date", "")[:7] == month)
    rev_total = sum(s.get("revenue", 0) for s in info.get("premium_stats", {}).values())
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    return web.json_response({
        "bot": {"id": info.get("id"), "name": info.get("name", ""), "username": info.get("username", ""), "is_pro": info.get("type") == "kino_pro"},
        "plan": {"name": get_bot_tariff(info)["name"], "days_left": kino_days_left(info)},
        "theme": info.get("theme", ""),
        "views": {"total": info.get("stats", {}).get("requests", 0), "today": daily_views.get(today, 0)},
        "movies": {
            "count": len(movies),
            "episodes": sum(kino_episodes(m) for m in movies.values()),
            "vip": len([c for c in info.get("vip_codes", []) if c in movies]),
        },
        "users": {
            "total": len(info.get("users", [])),
            "new_today": sum(1 for d in join_log.values() if d == today),
            "new_month": sum(1 for d in join_log.values() if d[:7] == month),
            "active_today": active_today,
            "blocked": len(info.get("blocked_users", [])),
            "vip_active": vip_active,
        },
        "revenue": {"total": rev_total, "today": rev_today, "month": rev_month, "count": len(log)},
        "chart": {
            "labels": [d[8:10] + "." + d[5:7] for d in days],
            "views": [daily_views.get(d, 0) for d in days],
            "new_users": [sum(1 for x in join_log.values() if x == d) for d in days],
        },
    }, headers=_NO_STORE)


async def kino_api_movies(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    vip = set(info.get("vip_codes", []))
    counts = info.get("request_counts", {})
    items = []
    for code, m in info.get("movies", {}).items():
        items.append({
            "code": code,
            "title": kino_title(code, m),
            "series": m.get("type") == "series",
            "episodes": kino_episodes(m),
            "views": counts.get(code, 0),
            "vip": code in vip,
        })
    items.sort(key=lambda x: (0, -int(x["code"])) if x["code"].isdigit() else (1, 0))
    return web.json_response({"movies": items}, headers=_NO_STORE)


async def kino_api_movie_delete(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    code = str(body.get("code", ""))
    if code not in info.get("movies", {}):
        return web.json_response({"error": "not_found"}, status=404, headers=_NO_STORE)
    info["movies"].pop(code, None)
    for key in ("vip_codes", "featured"):
        if code in info.get(key, []):
            info[key].remove(code)
    for key in ("request_counts", "ratings", "series_subscribers"):
        info.get(key, {}).pop(code, None)
    save_data()
    return web.json_response({"ok": True}, headers=_NO_STORE)


async def kino_api_movie_vip(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    if info.get("type") != "kino_pro":
        return web.json_response({"error": "pro_only"}, status=400, headers=_NO_STORE)
    try:
        body = await request.json()
    except Exception:
        body = {}
    code = str(body.get("code", ""))
    if code not in info.get("movies", {}):
        return web.json_response({"error": "not_found"}, status=404, headers=_NO_STORE)
    vip_codes = info.setdefault("vip_codes", [])
    if body.get("vip"):
        if code not in vip_codes:
            vip_codes.append(code)
    elif code in vip_codes:
        vip_codes.remove(code)
    save_data()
    return web.json_response({"ok": True, "vip": code in vip_codes}, headers=_NO_STORE)


async def kino_api_users(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    q = (request.query.get("q") or "").strip().lower()
    now = datetime.now()
    blocked = set(info.get("blocked_users", []))
    names = info.get("user_usernames", {})
    phones = info.get("user_phones", {})
    activity = info.get("user_activity", {})
    premium = info.get("premium_users", {})
    items = []
    for u in reversed(info.get("users", [])):
        name = names.get(str(u), "")
        phone = phones.get(str(u), "")
        if q and q not in str(u) and q not in name.lower() and q not in phone:
            continue
        is_vip = False
        rec = premium.get(str(u))
        if rec:
            try:
                is_vip = datetime.fromisoformat(rec["until"]) > now
            except Exception:
                is_vip = False
        items.append({
            "id": u, "name": name, "phone": phone,
            "requests": activity.get(str(u), 0),
            "vip": is_vip, "blocked": u in blocked,
            "admin": is_admin(info, u),
        })
        if len(items) >= 100:
            break
    return web.json_response({"users": items, "total": len(info.get("users", []))}, headers=_NO_STORE)


async def kino_api_user_block(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    try:
        body = await request.json()
        target = int(body.get("uid"))
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400, headers=_NO_STORE)
    if is_admin(info, target):
        return web.json_response({"error": "cannot_block_admin"}, status=400, headers=_NO_STORE)
    blocked = info.setdefault("blocked_users", [])
    if body.get("block"):
        if target not in blocked:
            blocked.append(target)
    elif target in blocked:
        blocked.remove(target)
    save_data()
    return web.json_response({"ok": True, "blocked": target in blocked}, headers=_NO_STORE)


async def kino_api_payments(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    stats = info.get("premium_stats", {})
    tariffs = []
    for tid, t in info.get("premium_tariffs", {}).items():
        st = stats.get(tid, {})
        tariffs.append({
            "name": t.get("name", "-"), "days": t.get("days", 0), "price": t.get("price", 0),
            "count": st.get("count", 0), "revenue": st.get("revenue", 0),
        })
    names = info.get("user_usernames", {})
    log = []
    for p in reversed(info.get("payment_log", [])[-50:]):
        log.append({
            "user": names.get(str(p.get("uid")), str(p.get("uid"))),
            "tariff": p.get("tariff", "-"), "amount": p.get("amount", 0),
            "date": p.get("date", "")[:16].replace("T", " "), "src": p.get("src", ""),
        })
    cards = []
    for psid, ps in (info.get("payment_systems") or {}).items():
        if not isinstance(ps, dict):
            continue
        cards.append({
            "name": str(ps.get("name", "-")), "number": str(ps.get("number", "-")),
            "owner": str(ps.get("owner", "")),
        })
    vips = []
    now_dt = datetime.now()
    for vid, rec in (info.get("premium_users") or {}).items():
        try:
            until = datetime.fromisoformat(rec["until"])
        except Exception:
            continue
        if until <= now_dt:
            continue
        vips.append({
            "id": str(vid), "name": names.get(str(vid), "") or f"ID {vid}",
            "until": until.strftime("%Y-%m-%d %H:%M"),
            "days_left": int((until - now_dt).total_seconds() // 86400) + 1,
        })
    vips.sort(key=lambda x: x["until"])
    return web.json_response({
        "tariffs": tariffs, "log": log, "cards": cards, "vips": vips[:200],
        "total": sum(s.get("revenue", 0) for s in stats.values()),
        "premium_enabled": bool(info.get("premium_enabled")),
    }, headers=_NO_STORE)


KINO_SETTING_TOGGLES = (
    "new_content_notify", "maintenance_mode", "auto_report_enabled", "vip_system", "protect_content",
    "movie_request_enabled", "ref_bonus_enabled", "weekly_top_enabled",
)
KINO_SETTING_DEFAULTS = {"vip_system": True, "movie_request_enabled": True}
KINO_SETTING_TEXTS = ("welcome_text", "help_text")


def kino_settings_out(info: dict) -> dict:
    out = {k: bool(info.get(k, KINO_SETTING_DEFAULTS.get(k, False))) for k in KINO_SETTING_TOGGLES}
    out.update({k: info.get(k, "") for k in KINO_SETTING_TEXTS})
    out["theme"] = info.get("theme", "")
    out["name"] = info.get("name", "")
    out["about"] = info.get("bot_about", "")
    try:
        out["ref_bonus_amount"] = int(info.get("ref_bonus_amount", 500) or 0)
    except Exception:
        out["ref_bonus_amount"] = 0
    try:
        out["report_hour"] = int(info.get("auto_report_hour", 9))
    except Exception:
        out["report_hour"] = 9
    out["is_pro"] = info.get("type") == "kino_pro"
    out["autopost_locked"] = not kino_autopost_allowed(info)
    out["autopost"] = [
        {"key": str(c.get("chat_id")), "title": c.get("title", ""), "username": c.get("username", "")}
        for c in info.get("autopost_channels", [])
    ]
    return out


# ---------- Kino panel: qo'shimcha funksiyalar (profil, kanallar, adminlar, reklama, auto-post) ----------
KINO_AUTOPOST_TARIFFS = ("3", "4", "5")   # Pro, Turbo, Unlimited
KINO_AUTOPOST_MAX = 3


def kino_autopost_allowed(info: dict) -> bool:
    """Auto-post faqat yuqori tariflarda (yoki platforma egasida) ishlaydi."""
    return bool(is_full_admin(info.get("admin_id"))) or str(info.get("tariff", "2")) in KINO_AUTOPOST_TARIFFS


def _new_bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def _close_bot(b) -> None:
    try:
        await b.session.close()
    except Exception:
        pass


async def kino_resolve_chat(token: str, ref):
    """Kanalni topadi va bot u yerda admin ekanini tekshiradi. (chat, xato_kodi) qaytaradi."""
    ref = str(ref or "").strip()
    if ref.startswith("https://t.me/") or ref.startswith("http://t.me/"):
        ref = "@" + ref.rstrip("/").rsplit("/", 1)[-1]
    if ref and not ref.startswith("@") and not ref.lstrip("-").isdigit():
        ref = "@" + ref
    if len(ref) < 3:
        return None, "bad_ref"
    target = int(ref) if ref.lstrip("-").isdigit() else ref
    b = _new_bot(token)
    try:
        chat = await b.get_chat(target)
        me = await b.get_me()
        member = await b.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            return None, "bot_not_admin"
        return chat, None
    except Exception as e:
        logging.info(f"Kanal topilmadi ({ref}): {e}")
        return None, "chat_not_found"
    finally:
        await _close_bot(b)


async def kino_autopost_text(token: str, info: dict, text: str) -> int:
    """Matndagi {bot} o'rniga bot username'ini qo'yib, barcha auto-post kanallarga yuboradi."""
    chans = info.get("autopost_channels", [])
    if not chans:
        return 0
    b = _new_bot(token)
    sent = 0
    try:
        try:
            me = await b.get_me()
            uname = f"@{me.username}" if me.username else ""
        except Exception:
            uname = ""
        for ch in chans:
            try:
                await b.send_message(ch["chat_id"], text.replace("{bot}", uname))
                sent += 1
            except Exception as e:
                logging.info(f"Auto-post yuborilmadi ({ch.get('chat_id')}): {e}")
    finally:
        await _close_bot(b)
    return sent


async def kino_autopost_movie(token: str, code: str) -> None:
    """Yangi kino qo'shilganda auto-post kanallarga xabar yuboradi."""
    try:
        info = data["bots"].get(token)
        if not info or not info.get("autopost_channels") or not kino_autopost_allowed(info):
            return
        m = info.get("movies", {}).get(code, {})
        text = (
            f"🎬 <b>Yangi kino:</b> {html_escape(kino_title(code, m))}\n"
            f"🆔 Kod: <b>{html_escape(str(code))}</b>\n\n"
            "👉 Ko'rish: {bot}"
        )
        await kino_autopost_text(token, info, text)
    except Exception as e:
        logging.error(f"kino_autopost_movie xatosi: {e}")


async def kino_scheduler_loop():
    """Har 5 daqiqada: kechki hisobot va haftalik top (yakshanba 20:00)."""
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            wkey = now.strftime("%G-W%V")
            for token, info in list(data["bots"].items()):
                if info.get("type") not in KINO_TYPES:
                    continue
                try:
                    if (info.get("auto_report_enabled") and now.hour >= int(info.get("auto_report_hour", 9))
                            and info.get("last_report_date") != today):
                        info["last_report_date"] = today
                        save_data()
                        try:
                            await main_bot.send_message(info["admin_id"], build_kino_report(info))
                        except Exception as e:
                            logging.error(f"Kechki hisobot yuborishda xato ({token[-6:]}): {e}")
                    if (info.get("weekly_top_enabled") and now.weekday() == 6 and now.hour >= 20
                            and info.get("last_weekly") != wkey and info.get("autopost_channels")
                            and kino_autopost_allowed(info)):
                        info["last_weekly"] = wkey
                        movies = info.get("movies", {})
                        top = sorted(
                            ((c, n) for c, n in info.get("week_counts", {}).items() if c in movies),
                            key=lambda x: -x[1],
                        )[:5]
                        info["week_counts"] = {}
                        save_data()
                        if top:
                            lines = [
                                f"{i}. {html_escape(kino_title(c, movies[c]))} — kod <b>{html_escape(str(c))}</b> ({n} ta ko'rish)"
                                for i, (c, n) in enumerate(top, 1)
                            ]
                            text = "📊 <b>Haftalik top kinolar</b>\n\n" + "\n".join(lines) + "\n\n👉 {bot}"
                            await kino_autopost_text(token, info, text)
                except Exception as e:
                    logging.error(f"kino_scheduler_loop ({token[-6:]}): {e}")
        except Exception as e:
            logging.error(f"kino_scheduler_loop xatosi: {e}")
        await asyncio.sleep(300)


def _kino_err(code: str, status: int = 400):
    return web.json_response({"error": code}, status=status, headers=_NO_STORE)


async def _kino_body(request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


async def kino_api_profile(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    body = await _kino_body(request)
    name = str(body.get("name", "")).strip()[:64]
    about = str(body.get("about", "")).strip()[:120]
    if not name:
        return _kino_err("name_required")
    info["name"] = name
    info["bot_about"] = about
    save_data()
    warn = ""
    b = _new_bot(token)
    try:
        try:
            await b.set_my_name(name=name)
        except Exception as e:
            warn = "name"
            logging.info(f"set_my_name xatosi ({token[-6:]}): {e}")
        try:
            await b.set_my_short_description(short_description=about)
        except Exception as e:
            warn = "about"
            logging.info(f"set_my_short_description xatosi ({token[-6:]}): {e}")
    finally:
        await _close_bot(b)
    return web.json_response({
        "name": info["name"], "about": info["bot_about"], "warn": warn,
    }, headers=_NO_STORE)


async def kino_api_channels(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    if request.method == "POST":
        body = await _kino_body(request)
        op = body.get("op")
        if op == "add":
            chat, e = await kino_resolve_chat(token, body.get("ref"))
            if e:
                return _kino_err(e)
            if not chat.username:
                return _kino_err("need_public")
            info.setdefault("channels", {})[str(chat.id)] = {
                "type": "telegram", "username": "@" + chat.username, "title": chat.title or chat.username,
            }
            save_data()
        elif op == "del":
            info.setdefault("channels", {}).pop(str(body.get("key", "")), None)
            save_data()
    rows = []
    for key, c in info.get("channels", {}).items():
        rows.append({"key": key, "title": c.get("title", "-"), "type": c.get("type", ""),
                     "username": c.get("username") or c.get("url", "")})
    return web.json_response({"channels": rows}, headers=_NO_STORE)


def kino_admins_out(info: dict, uid) -> dict:
    owner = info.get("admin_id")
    ids = list(dict.fromkeys(info.get("admin_ids", [owner])))
    names = info.get("user_usernames", {})
    return {
        "can_manage": uid == owner or bool(is_full_admin(uid)),
        "admins": [{"id": i, "name": names.get(str(i), ""), "owner": i == owner} for i in ids],
    }


async def kino_api_admins(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    if request.method == "POST":
        if not (uid == info.get("admin_id") or is_full_admin(uid)):
            return _kino_err("forbidden_owner", 403)
        body = await _kino_body(request)
        op = body.get("op")
        try:
            target = int(body.get("uid"))
        except Exception:
            return _kino_err("bad_id")
        ids = info.setdefault("admin_ids", [info.get("admin_id")])
        if op == "add":
            if target <= 0:
                return _kino_err("bad_id")
            if target in ids:
                return _kino_err("exists")
            ids.append(target)
        elif op == "del":
            if target == info.get("admin_id"):
                return _kino_err("forbidden_owner", 403)
            if target in ids:
                ids.remove(target)
        save_data()
    return web.json_response(kino_admins_out(info, uid), headers=_NO_STORE)


async def kino_api_ads(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    ads = info.setdefault("ads", {})
    if request.method == "POST":
        body = await _kino_body(request)
        op = body.get("op")
        if op == "add":
            text = str(body.get("text", "")).strip()[:500]
            if not text:
                return _kino_err("empty")
            if len(ads) >= 30:
                return _kino_err("limit")
            ads[uuid.uuid4().hex[:6]] = {"text": text, "active": True}
        elif op == "del":
            ads.pop(str(body.get("id", "")), None)
        elif op == "toggle":
            a = ads.get(str(body.get("id", "")))
            if a:
                a["active"] = not a.get("active", True)
        save_data()
    rows = [{"id": k, "text": a.get("text", ""), "active": bool(a.get("active", True))} for k, a in ads.items()]
    return web.json_response({"ads": rows}, headers=_NO_STORE)


async def kino_api_autopost(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    if not kino_autopost_allowed(info):
        return _kino_err("locked", 403)
    body = await _kino_body(request)
    op = body.get("op")
    chans = info.setdefault("autopost_channels", [])
    if op == "add":
        if len(chans) >= KINO_AUTOPOST_MAX:
            return _kino_err("limit")
        chat, e = await kino_resolve_chat(token, body.get("ref"))
        if e:
            return _kino_err(e)
        if any(str(c.get("chat_id")) == str(chat.id) for c in chans):
            return _kino_err("exists")
        chans.append({"chat_id": chat.id, "title": chat.title or chat.username or str(chat.id),
                      "username": ("@" + chat.username) if chat.username else ""})
    elif op == "del":
        key = str(body.get("key", ""))
        info["autopost_channels"] = [c for c in chans if str(c.get("chat_id")) != key]
    save_data()
    return web.json_response(kino_settings_out(info), headers=_NO_STORE)


# ---------- Statistika (metrikalar) — barcha bot turlari uchun ----------
METRIC_KEYS = ("users", "msgs", "reqs", "errs", "undel")


def bot_metric(info: dict, key: str, n: int = 1, err_type: str = None) -> None:
    """Soatlab hisoblagich: yangi user, xabar, so'rov, xato, yetkazilmagan."""
    try:
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        m = info.setdefault("metrics", {})
        d = m.get(day)
        if d is None:
            d = m[day] = {k: [0] * 24 for k in METRIC_KEYS}
            d["err_types"] = {}
            if len(m) > 40:
                for old in sorted(m.keys())[:-35]:
                    m.pop(old, None)
        d[key][now.hour] += n
        if err_type:
            d["err_types"][err_type] = d["err_types"].get(err_type, 0) + 1
    except Exception:
        pass


class BotMetricsMiddleware(BaseMiddleware):
    """Har bir bolalar botiga ulanadi va statistika yig'adi (handlerlarga tegmaydi)."""

    def __init__(self, token: str):
        self.token = token

    async def __call__(self, handler, event, payload):
        info = data["bots"].get(self.token)
        if info is None:
            return await handler(event, payload)
        before = len(info.get("users", []))
        bot_metric(info, "reqs")
        if getattr(event, "message", None) is not None:
            bot_metric(info, "msgs")
        try:
            result = await handler(event, payload)
        except TelegramForbiddenError:
            bot_metric(info, "undel")
            raise
        except Exception as e:
            bot_metric(info, "errs", err_type=type(e).__name__)
            raise
        grown = len(info.get("users", [])) - before
        if grown > 0:
            bot_metric(info, "users", n=grown)
        return result


_BOT_EMOJI = {"kino_pro": "🎬", "shop": "🛍", "ai": "🤖", "taxi": "🚕"}
_creator_username_cache = {"v": ""}


def get_botstats_url(bot_id) -> str:
    if bot_id is None:
        return ""
    explicit = os.getenv("MINIAPP_URL")
    if explicit:
        base = explicit.rstrip("/")
    else:
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
        if not domain:
            return ""
        base = f"https://{domain}"
    return f"{base}/botstats?b={bot_id}"


def _sum_day(d, key):
    return sum(d[key]) if d else 0


async def api_botstats(request):
    parsed = validate_platform_init_data(request.query.get("initData", ""))
    if not parsed:
        return web.json_response({"error": "invalid_init_data"}, status=401, headers=_NO_STORE)
    try:
        uid = json.loads(parsed.get("user", "{}")).get("id")
    except Exception:
        uid = None
    if not uid:
        return web.json_response({"error": "no_user"}, status=400, headers=_NO_STORE)
    token, info = None, None
    for t, i in data["bots"].items():
        if str(i.get("id")) == str(request.query.get("b", "")):
            token, info = t, i
            break
    if not info:
        return web.json_response({"error": "bot_not_found"}, status=404, headers=_NO_STORE)
    if not (uid in info.get("admin_ids", [info.get("admin_id")]) or is_full_admin(uid)):
        return web.json_response({"error": "forbidden"}, status=403, headers=_NO_STORE)

    period = request.query.get("period", "today")
    days = {"today": 1, "week": 7, "month": 30}.get(period, 1)
    metrics = info.get("metrics", {})
    today_d = datetime.now().date()
    day_list = [today_d - timedelta(days=i) for i in range(days - 1, -1, -1)]
    day_data = [metrics.get(d.strftime("%Y-%m-%d")) for d in day_list]

    cards = {k: sum(_sum_day(d, k) for d in day_data) for k in METRIC_KEYS}
    tdata = metrics.get(today_d.strftime("%Y-%m-%d"))
    today = {k: _sum_day(tdata, k) for k in METRIC_KEYS}

    chart = {"labels": [], "users": [], "msgs": [], "reqs": [], "errs": []}
    if days == 1:
        for h in range(0, 24, 2):
            chart["labels"].append(f"{h:02d}:00")
            for k in ("users", "msgs", "reqs", "errs"):
                chart[k].append((tdata[k][h] + tdata[k][h + 1]) if tdata else 0)
    else:
        for d, dd in zip(day_list, day_data):
            chart["labels"].append(d.strftime("%d.%m"))
            for k in ("users", "msgs", "reqs", "errs"):
                chart[k].append(sum(dd[k]) if dd else 0)

    err_types = {}
    for dd in day_data:
        if dd:
            for name, cnt in dd.get("err_types", {}).items():
                err_types[name] = err_types.get(name, 0) + cnt
    err_list = [{"name": n, "count": c} for n, c in sorted(err_types.items(), key=lambda x: -x[1])[:8]]

    task = running_bots.get(token)
    alive = bool(task) and not task.done()
    uname = info.get("username", "")
    if not uname:
        b = _new_bot(token)
        try:
            me = await b.get_me()
            uname = me.username or ""
            if uname:
                info["username"] = uname
                save_data()
        except Exception:
            uname = ""
        finally:
            await _close_bot(b)
    if not _creator_username_cache["v"]:
        try:
            _creator_username_cache["v"] = (await main_bot.get_me()).username or ""
        except Exception:
            pass

    return web.json_response({
        "name": info.get("name", ""),
        "username": uname,
        "emoji": _BOT_EMOJI.get(info.get("type"), "🤖"),
        "type_label": BOT_TYPES.get(info.get("type"), info.get("type", "")),
        "tariff": get_bot_tariff(info).get("name", ""),
        "active": bool(is_active(info) and alive),
        "period": period,
        "cards": {**cards, "uptime": 100 if alive else 0},
        "today": today,
        "chart": chart,
        "err_types": err_list,
        "updated": datetime.now().strftime("%H:%M:%S"),
        "creator": _creator_username_cache["v"],
    }, headers=_NO_STORE)


async def kino_api_settings(request):
    token, info, uid, err = kino_auth(request)
    if err:
        return err
    if request.method == "POST":
        body = await _kino_body(request)
        if body.get("weekly_top_enabled") and not kino_autopost_allowed(info):
            return _kino_err("locked", 403)
        for key in KINO_SETTING_TOGGLES:
            if key in body:
                val = bool(body[key])
                if key == "auto_report_enabled" and val and not info.get("auto_report_enabled"):
                    info["auto_report_hour"] = 21
                info[key] = val
        for key in KINO_SETTING_TEXTS:
            if key in body and isinstance(body[key], str) and body[key].strip():
                info[key] = body[key].strip()[:1500]
        if "ref_bonus_amount" in body:
            try:
                info["ref_bonus_amount"] = max(0, min(10_000_000, int(body["ref_bonus_amount"])))
            except Exception:
                pass
        if "theme" in body:
            t = str(body["theme"])
            if t == "" or theme_color(t):
                info["theme"] = t
        save_data()
    return web.json_response(kino_settings_out(info), headers=_NO_STORE)


KINOPANEL_HTML = r"""<!DOCTYPE html>
<html lang="uz" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<title>Kino panel</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#f3f5f9;--card:#fff;--tx:#0f1729;--mu:#8590a2;--bd:#e7ebf2;--ac:#0e9f6e;--acs:#e3f5ee;--rd:#e5484d;--sh:0 2px 10px rgba(20,30,60,.06);--pg1:#3b5bdb;--pg2:#7048e8}
[data-theme=dark]{--bg:#0d1220;--card:#161d2f;--tx:#eef2fa;--mu:#8f9bb3;--bd:#232c43;--ac:#2dd4a0;--acs:#12332b;--rd:#ff6b70;--sh:none}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--tx);font-family:-apple-system,"Segoe UI",Roboto,sans-serif;padding-bottom:120px}
.top{display:flex;align-items:center;justify-content:space-between;padding:18px 16px 12px}
.top h1{margin:0;font-size:20px;font-weight:800}.top p{margin:2px 0 0;color:var(--mu);font-size:11px}
.ib{width:44px;height:44px;border-radius:50%;border:0;background:var(--card);box-shadow:var(--sh);font-size:13px;color:var(--tx);margin-left:8px}
.wrap{padding:0 16px}
.card{background:var(--card);border-radius:22px;padding:18px;margin-bottom:14px;box-shadow:var(--sh);position:relative;overflow:hidden}
.plan{background:linear-gradient(90deg,var(--pg1),var(--pg2));color:#fff;border-radius:18px;padding:16px 18px;margin-bottom:14px;display:flex;justify-content:space-between;align-items:center}
.plan b{font-size:13px;display:block}.plan small{opacity:.9;font-size:11px}
.pbtn{background:#fff;color:var(--pg1);border:0;border-radius:14px;padding:11px 16px;font-weight:700;font-size:11px}
.lbl{font-size:10px;letter-spacing:.09em;color:var(--mu);font-weight:700;text-transform:uppercase}
.big{font-size:37px;font-weight:800;margin:6px 0 10px}.big small{font-size:12px;color:var(--mu);font-weight:500;margin-left:6px}
.pill{position:absolute;top:14px;right:14px;background:var(--acs);color:var(--ac);border-radius:14px;padding:8px 12px;font-weight:700;font-size:10px}
.kv{display:flex;justify-content:space-between;padding:9px 0;font-size:12px;border-top:1px solid var(--bd)}
.kv span{color:var(--mu)}.kv b{font-weight:700}.kv b.g{color:var(--ac)}
h3{margin:22px 0 10px;font-size:10px;letter-spacing:.09em;color:var(--mu)}
.tabs2{display:flex;gap:10px;margin-bottom:8px}.tabs2 div{flex:1;border:2px solid var(--bd);border-radius:16px;padding:12px}
.tabs2 div.on{border-color:var(--ac);background:var(--acs)}.tabs2 b{display:block;font-size:19px;margin-top:4px}
.grid3{display:flex;text-align:center}.grid3>div{flex:1;padding:6px}.grid3 b{display:block;font-size:22px;color:var(--ac)}.grid3 span{color:var(--mu);font-size:11px}
input.s,textarea.t{width:100%;background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:15px;font-size:12px;color:var(--tx);margin-bottom:12px;font-family:inherit}
textarea.t{min-height:90px;resize:vertical}
.chips{display:flex;gap:8px;overflow-x:auto;margin-bottom:14px;padding-bottom:4px}
.chip{flex:none;padding:11px 18px;border-radius:14px;background:var(--card);border:1px solid var(--bd);font-weight:600;font-size:12px;color:var(--mu)}
.chip.on{border-color:var(--ac);background:var(--acs);color:var(--ac)}
.item{display:flex;align-items:center;gap:14px;background:var(--card);border-radius:20px;padding:14px;margin-bottom:12px;box-shadow:var(--sh)}
.code{width:58px;height:58px;border-radius:16px;background:var(--acs);color:var(--ac);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;font-family:ui-monospace,monospace;flex:none}
.av{border-radius:50%}.meta{flex:1;min-width:0}.meta b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta span{color:var(--mu);font-size:10px}.bdg{display:inline-block;font-size:10px;font-weight:700;border-radius:8px;padding:2px 7px;margin-left:6px;background:var(--acs);color:var(--ac)}
.bdg.r{background:#fde8e9;color:var(--rd)}
.sb{width:44px;height:44px;border-radius:14px;border:1px solid var(--bd);background:var(--bg);font-size:12px;margin-left:4px;color:var(--tx)}
.tb{border:1px solid var(--bd);background:var(--bg);color:var(--tx);border-radius:12px;padding:9px 12px;font-weight:600;font-size:10px}
.note{color:var(--mu);font-size:11px;line-height:1.5;margin:6px 4px 14px}
.tg{display:flex;justify-content:space-between;align-items:center;padding:14px 0;border-top:1px solid var(--bd);font-size:12px}
.sw{width:52px;height:30px;border-radius:15px;background:var(--bd);position:relative;flex:none;transition:.2s}.sw i{position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:50%;background:#fff;transition:.2s}
.sw.on{background:var(--ac)}.sw.on i{left:25px}
.btn{width:100%;padding:16px;border:0;border-radius:16px;background:var(--ac);color:#fff;font-weight:700;font-size:12px}
.nav{position:fixed;left:18px;right:18px;max-width:444px;margin:0 auto;bottom:calc(34px + env(safe-area-inset-bottom,0px));background:var(--card);border-radius:26px;box-shadow:0 8px 26px rgba(20,30,60,.16);display:flex;padding:10px 6px;z-index:5}
.nav{background:color-mix(in srgb,var(--card) 78%,transparent);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);border:1px solid var(--bd);padding:12px 6px 10px}
.nav div{flex:1;text-align:center;color:var(--mu);font-size:10px;font-weight:500;padding:2px 0;letter-spacing:.01em}
.nav div i{display:flex;justify-content:center;font-style:normal;margin-bottom:4px}
.nav div svg{width:27px;height:27px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.nav div.on{color:var(--ac);font-weight:700}.nav div.on svg{stroke-width:2}
.foot{position:fixed;bottom:calc(8px + env(safe-area-inset-bottom,0px));left:0;right:0;text-align:center;color:var(--mu);font-size:10px;font-weight:600;pointer-events:none}
.empty{text-align:center;color:var(--mu);padding:30px 10px}
.chl{display:flex;justify-content:space-between;color:var(--mu);font-size:10px;margin-top:6px}
.swc{width:46px;height:46px;border-radius:14px;border:3px solid transparent;margin:4px 6px 4px 0;box-shadow:var(--sh)}
.swc.on{border-color:var(--tx)}
.fl{font-size:11px;font-weight:700;margin:14px 0 8px}
.sg{display:flex;gap:10px;margin-bottom:8px}
.sc{flex:1;min-width:0;border:2px solid transparent;background:var(--card);border-radius:20px;padding:14px 12px;box-shadow:var(--sh)}
.sc b{display:block;font-size:12px}.sc span{display:block;color:var(--mu);font-size:10px;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sc.on{border-color:var(--ac);background:var(--acs)}
.sc.mc{text-align:center}.sc.mc i{display:block;font-style:normal;font-size:19px;margin-bottom:6px}
.row2{padding:14px 0;border-top:1px solid var(--bd)}.lbl+.row2{border-top:0}
.rt{font-size:14px;font-weight:800}.rd{color:var(--mu);font-size:11px;margin:4px 0 10px;line-height:1.4}
.yn{display:flex;gap:10px}.yn button{flex:1;padding:15px 8px;border-radius:16px;border:0;background:var(--bg);color:var(--tx);font-weight:700;font-size:12px}
.yn .y.on{background:#0a8f5c;color:#fff}.yn .n.on{background:#c0143c;color:#fff}
.gb{background:var(--ac);color:#fff;border:0;border-radius:14px;padding:13px 14px;font-weight:700;font-size:11px;flex:none}
.lock{display:flex;align-items:center;justify-content:space-between;gap:10px;background:#efe6dc;border-radius:18px;padding:14px;margin:8px 0 12px}
[data-theme=dark] .lock{background:#3a2f22}
.addrow{display:flex;gap:10px;margin-top:10px}.addrow input{margin:0}.addrow .tb{flex:none;padding:0 18px;font-size:12px}
</style>
</head>
<body>
<div class="top"><div><h1 id="ttl">Boshqaruv</h1><p id="sub">Umumiy ko'rinish</p></div>
<div><button class="ib" id="rf">&#8635;</button><button class="ib" id="th">&#9728;&#65039;</button></div></div>
<div class="wrap" id="app"><div class="empty">Yuklanmoqda...</div></div>
<div class="nav" id="nav"></div>
<div class="foot" id="foot"></div>
<script>
var tg = window.Telegram && window.Telegram.WebApp;
if (tg) { try { tg.ready(); tg.expand(); } catch (e) {} }
var Q = new URLSearchParams(location.search);
var B = Q.get("b") || "";
var FROM = Q.get("from") || "";
var INIT = (tg && tg.initData) || "";
var S = {tab: "dash", ov: null, movies: null, users: null, pays: null, sets: null, filter: "all", q: "", uq: "", chart: "views", err: "", sub: "bot", psub: "log", chans: null, adms: null, ads: null};
var MODE = "auto";
var PAL = __PALETTES__;
function themeHex(k) { try { var p = String(k).split("-"); return PAL[p[0]].shades[Number(p[1]) - 1] || ""; } catch (e) { return ""; } }
function applyTheme(k) {
  var st = document.documentElement.style, c = themeHex(k);
  ["--ac", "--acs", "--pg1", "--pg2"].forEach(function (v) { st.removeProperty(v); });
  if (!c) return;
  st.setProperty("--ac", c);
  st.setProperty("--acs", "color-mix(in srgb," + c + " 15%,transparent)");
  st.setProperty("--pg1", c);
  st.setProperty("--pg2", "color-mix(in srgb," + c + " 60%,#000)");
}

function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
function fmt(n) { return String(n || 0).replace(/\B(?=(\d{3})+(?!\d))/g, " "); }
function url(p, extra) { return p + "?b=" + encodeURIComponent(B) + "&initData=" + encodeURIComponent(INIT) + (extra || ""); }
async function api(p, body, extra) {
  var opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  var r = await fetch(url(p, extra), opt);
  var j = await r.json().catch(function () { return {}; });
  if (!r.ok) { var e = new Error(j.error || "xato"); e.status = r.status; throw e; }
  return j;
}
function ask(msg, cb) { if (tg && tg.showConfirm) tg.showConfirm(msg, function (ok) { if (ok) cb(); }); else if (confirm(msg)) cb(); }
function toast(msg) { if (tg && tg.showAlert) tg.showAlert(msg); else alert(msg); }
function fail(e) {
  if (e.status === 401) S.err = "Panelni Telegram ichidan oching.";
  else if (e.status === 403) S.err = "Ruxsat yo'q. Panelga faqat bot adminlari kira oladi.";
  else if (e.status === 404) S.err = "Bot topilmadi.";
  else S.err = "Xatolik yuz berdi. Qayta urinib ko'ring.";
  render();
}

var TABS = [["dash", "&#9638;", "Dashbord"], ["movies", "&#127902;", "Kinolar"], ["users", "&#128101;", "Mijozlar"], ["pays", "&#128179;", "To'lovlar"], ["sets", "&#9881;&#65039;", "Sozlama"]];
var TITLES = {dash: ["Boshqaruv", "Umumiy ko'rinish"], movies: ["Kinolar", "Kodlar bo'yicha"], users: ["Mijozlar", "Foydalanuvchilar"], pays: ["To'lovlar", "Premium sotuvlari"], sets: ["Sozlamalar", "Bot va panel"]};

function chartSvg(vals) {
  var W = 320, H = 110, P = 6, mx = Math.max.apply(null, vals.concat([1]));
  var pts = vals.map(function (v, i) { return [P + i * (W - 2 * P) / (vals.length - 1), H - P - (v / mx) * (H - 2 * P - 10)]; });
  var line = pts.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ");
  var area = P + "," + (H - P) + " " + line + " " + (W - P) + "," + (H - P);
  var dots = pts.map(function (p) { return '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="3.5" fill="var(--ac)"/>'; }).join("");
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%"><polygon points="' + area + '" fill="var(--acs)"/><polyline points="' + line + '" fill="none" stroke="var(--ac)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>' + dots + '</svg>';
}

function vDash() {
  var o = S.ov; if (!o) return '<div class="empty">Yuklanmoqda...</div>';
  var h = '<div class="plan"><div><b>' + esc(o.plan.name) + '</b><small>Muddat: ' + (o.plan.days_left < 0 ? "cheksiz" : o.plan.days_left + " kun qoldi") + '</small></div>' + (FROM === "creator" ? '<button class="pbtn" id="backBtn">Botlarim</button>' : '') + '</div>';
  h += '<div class="card"><div class="lbl">Kino ko\'rishlari</div><div class="pill">Bugun: ' + fmt(o.views.today) + '</div>' +
    '<div class="big">' + fmt(o.views.total) + '<small>marta</small></div>' +
    '<div class="kv"><span>Bugun ko\'rilgan:</span><b>' + fmt(o.views.today) + '</b></div>' +
    '<div class="kv"><span>Kinolar:</span><b>' + fmt(o.movies.count) + ' ta (' + fmt(o.movies.episodes) + ' qism)</b></div>' +
    '<div class="kv"><span>Mijozlar:</span><b>' + fmt(o.users.total) + '</b></div>' +
    '<div class="kv"><span>Bugun qo\'shilgan:</span><b>' + fmt(o.users.new_today) + '</b></div>' +
    '<div class="kv"><span>Bugun faol:</span><b>' + fmt(o.users.active_today) + '</b></div>';
  if (o.bot.is_pro) {
    h += '<div style="margin-top:14px;font-size:16px;font-weight:800">&#128176; Tushum</div>' + (o.revenue.count ? '' : '<div class="note" style="margin:2px 0">Hali to\'lov yo\'q</div>') +
      '<div class="kv"><span>Jami:</span><b class="g">' + fmt(o.revenue.total) + ' so\'m</b></div>' +
      '<div class="kv"><span>Bugun:</span><b>' + fmt(o.revenue.today) + ' so\'m</b></div>' +
      '<div class="kv"><span>Shu oy:</span><b>' + fmt(o.revenue.month) + ' so\'m</b></div>';
  }
  h += '</div>';
  var vals = S.chart === "views" ? o.chart.views : o.chart.new_users;
  var tot = vals.reduce(function (a, b) { return a + b; }, 0);
  h += '<div class="card"><div class="tabs2"><div id="c1" class="' + (S.chart === "views" ? "on" : "") + '">&#127916; Ko\'rishlar<b>' + fmt(o.chart.views.reduce(function (a, b) { return a + b; }, 0)) + '</b></div>' +
    '<div id="c2" class="' + (S.chart === "new" ? "on" : "") + '">&#128101; Yangi mijozlar<b>' + fmt(o.chart.new_users.reduce(function (a, b) { return a + b; }, 0)) + '</b></div></div>' +
    chartSvg(vals) + '<div class="chl">' + o.chart.labels.map(function (l) { return '<span>' + l + '</span>'; }).join("") + '</div>' +
    '<div class="note" style="margin-bottom:0">Oxirgi 7 kun' + (tot ? '' : ' — hali ma\'lumot yo\'q') + '.</div></div>';
  h += '<h3>MIJOZLAR</h3><div class="card"><div class="grid3">' +
    (o.bot.is_pro ? '<div><b>' + fmt(o.users.vip_active) + '</b><span>VIP a\'zolar</span></div>' : '') +
    '<div><b>' + fmt(o.users.new_month) + '</b><span>Shu oy yangi</span></div>' +
    '<div><b>' + fmt(o.users.blocked) + '</b><span>Bloklangan</span></div></div></div>';
  return h;
}

function vMovies() {
  var o = S.ov, list = S.movies;
  var h = '<div class="card"><div class="lbl">Kinolar bazasi</div><div class="big">' + (o ? fmt(o.movies.count) : 0) + '<small>ta</small></div>' +
    '<div class="kv"><span>Jami qism:</span><b>' + (o ? fmt(o.movies.episodes) : 0) + '</b></div>' +
    (o && o.bot.is_pro ? '<div class="kv"><span>Premium:</span><b>' + fmt(o.movies.vip) + '</b></div>' : '') + '</div>';
  h += '<input class="s" id="mq" placeholder="&#128269; Kino nomi yoki kodi..." value="' + esc(S.q) + '">';
  var fl = [["all", "Barchasi"]]; if (o && o.bot.is_pro) fl.push(["vip", "Premium"], ["free", "Bepul"]); fl.push(["series", "Serial"]);
  h += '<div class="chips">' + fl.map(function (f) { return '<div class="chip ' + (S.filter === f[0] ? "on" : "") + '" data-f="' + f[0] + '">' + f[1] + '</div>'; }).join("") + '</div>';
  if (!list) return h + '<div class="empty">Yuklanmoqda...</div>';
  var q = S.q.toLowerCase();
  var rows = list.filter(function (m) {
    if (q && m.code.toLowerCase().indexOf(q) < 0 && m.title.toLowerCase().indexOf(q) < 0) return false;
    if (S.filter === "vip") return m.vip; if (S.filter === "free") return !m.vip; if (S.filter === "series") return m.series; return true;
  });
  if (!rows.length) h += '<div class="empty">Kino topilmadi</div>';
  rows.forEach(function (m) {
    h += '<div class="item"><div class="code">' + esc(m.code) + '</div><div class="meta"><b>' + esc(m.title) + (m.vip ? '<span class="bdg">VIP</span>' : '') + '</b><span>' + (m.episodes ? m.episodes + ' qism &middot; ' : '') + '&#128065; ' + fmt(m.views) + '</span></div>' +
      (o && o.bot.is_pro ? '<button class="sb" data-vip="' + esc(m.code) + '">&#9881;&#65039;</button>' : '') +
      '<button class="sb" data-del="' + esc(m.code) + '">&#128465;</button></div>';
  });
  h += '<div class="note">Yangi kino qo\'shish botda: <b>&#127916; Kontent</b>. Yuklash bot ichida amalga oshiriladi.</div>';
  return h;
}

function vUsers() {
  var h = '<input class="s" id="uq" placeholder="&#128269; ID, ism yoki telefon..." value="' + esc(S.uq) + '">';
  if (!S.users) return h + '<div class="empty">Yuklanmoqda...</div>';
  if (!S.users.users.length) h += '<div class="empty">Mijoz topilmadi</div>';
  S.users.users.forEach(function (u) {
    var nm = u.name || ("ID " + u.id);
    h += '<div class="item"><div class="code av">' + esc(nm.replace("@", "").charAt(0).toUpperCase() || "?") + '</div><div class="meta"><b>' + esc(nm) + (u.vip ? '<span class="bdg">VIP</span>' : '') + (u.blocked ? '<span class="bdg r">Blok</span>' : '') + '</b><span>ID ' + u.id + (u.phone ? ' &middot; ' + esc(u.phone) : '') + ' &middot; &#128065; ' + fmt(u.requests) + '</span></div>' +
      (u.admin ? '<span class="bdg">Admin</span>' : '<button class="tb" data-blk="' + u.id + '" data-to="' + (u.blocked ? 0 : 1) + '">' + (u.blocked ? "Ochish" : "Bloklash") + '</button>') + '</div>';
  });
  h += '<div class="note">Jami mijozlar: ' + fmt(S.users.total) + '. Oxirgi 100 tasi ko\'rsatiladi.</div>';
  return h;
}

var PSUBS = [["log", "To'lovlar"], ["tar", "Tariflar"], ["card", "Kartalar"], ["vip", "VIP a'zolar"]];
function vPays() {
  var p = S.pays; if (!p) return '<div class="empty">Yuklanmoqda...</div>';
  var h = '<div class="chips">' + PSUBS.map(function (x) {
    return '<div class="chip ' + (S.psub === x[0] ? "on" : "") + '" data-ps="' + x[0] + '">' + x[1] + '</div>';
  }).join("") + '</div>';
  if (S.psub === "log") {
    h += '<div class="card"><div class="lbl">Jami tushum</div><div class="big">' + fmt(p.total) + '<small>so\'m</small></div></div>';
    if (!p.log.length) h += '<div class="empty"><div style="font-size:32px">&#128179;</div>Hozircha to\'lovlar yo\'q.</div>';
    p.log.forEach(function (l) {
      h += '<div class="item"><div class="meta"><b>' + esc(l.user) + '</b><span>' + esc(l.tariff) + ' &middot; ' + esc(l.date) + '</span></div><b style="color:var(--ac)">+' + fmt(l.amount) + '</b></div>';
    });
  } else if (S.psub === "tar") {
    if (!p.tariffs.length) h += '<div class="empty">Premium tarif qo\'shilmagan.<br>Botda: &#128142; Premium bo\'limi.</div>';
    p.tariffs.forEach(function (t) {
      h += '<div class="item"><div class="meta"><b>' + esc(t.name) + '</b><span>' + t.days + ' kun &middot; ' + fmt(t.price) + ' so\'m</span></div><div style="text-align:right"><b style="color:var(--ac)">' + fmt(t.revenue) + '</b><br><span style="color:var(--mu);font-size:10px">' + t.count + ' ta sotuv</span></div></div>';
    });
  } else if (S.psub === "card") {
    if (!p.cards.length) h += '<div class="empty">To\'lov kartalari yo\'q.<br>Botda: &#128179; To\'lov tizimlar bo\'limi.</div>';
    p.cards.forEach(function (c) {
      h += '<div class="item"><div class="code">&#128179;</div><div class="meta"><b>' + esc(c.name) + '</b><span>' + esc(c.number) + (c.owner ? ' &middot; ' + esc(c.owner) : '') + '</span></div></div>';
    });
  } else {
    if (!p.vips.length) h += '<div class="empty">Hozircha VIP a\'zolar yo\'q.</div>';
    p.vips.forEach(function (v) {
      h += '<div class="item"><div class="code av">' + esc(String(v.name).replace("@", "").charAt(0).toUpperCase() || "?") + '</div><div class="meta"><b>' + esc(v.name) + '<span class="bdg">VIP</span></b><span>ID ' + esc(v.id) + ' &middot; ' + esc(v.until) + '</span></div><b style="color:var(--ac)">' + v.days_left + ' kun</b></div>';
    });
  }
  return h;
}

var SUBS = [["bot", "&#129302;", "Bot", "Nomi, tavsif"], ["btn", "&#128306;", "Tugmalar", "Menyu, rang"], ["txt", "&#9997;&#65039;", "Matnlar", "Bot xabarlari"]];
var SUBS2 = [["chan", "&#128226;", "Kanallar", "Majburiy obuna"], ["adm", "&#128101;", "Adminlar", "Kim boshqaradi"], ["ads", "&#128250;", "Reklama", "Kino ostida"]];
var ERRS = {name_required: "Bot nomini kiriting.", 
  bot_not_admin: "Bot kanalda admin emas. Avval botni kanalga admin qiling.", chat_not_found: "Kanal topilmadi. Username to\'g\'riligini tekshiring.", bad_ref: "Kanal username\'ini kiriting.",
  need_public: "Kanal ochiq (username li) bo\'lishi kerak.", limit: "Limitga yetdingiz.", locked: "Bu funksiya joriy tarifingizda mavjud emas.", bad_id: "ID noto\'g\'ri.",
  exists: "Allaqachon qo\'shilgan.", forbidden_owner: "Bu amal faqat bot egasiga ruxsat etilgan.", empty: "Matn bo\'sh bo\'lmasin."};

function soft(e) {
  if (e && (e.status === 401 || e.status === 403 && e.message === "forbidden" || e.status === 404 && e.message === "bot_not_found")) return fail(e);
  toast(ERRS[e && e.message] || "Xatolik yuz berdi. Qayta urinib ko\'ring.");
}
function yn(key, label, desc, on) {
  return '<div class="row2"><div class="rt">' + label + '</div>' + (desc ? '<div class="rd">' + desc + '</div>' : '') +
    '<div class="yn"><button class="y' + (on ? ' on' : '') + '" data-yn="' + key + '" data-v="1">&#9989; Yoqilgan</button><button class="n' + (on ? '' : ' on') + '" data-yn="' + key + '" data-v="0">&#9940; O\'chirilgan</button></div></div>';
}
function subCards(list) {
  return '<div class="sg">' + list.map(function (x) {
    return '<div class="sc' + (S.sub === x[0] ? ' on' : '') + '" data-sub="' + x[0] + '"><b>' + x[1] + ' ' + x[2] + '</b><span>' + x[3] + '</span></div>';
  }).join("") + '</div>';
}
function modeCards() {
  var m = [["light", "&#9728;&#65039;", "Yorug\'"], ["dark", "&#127769;", "Qorong\'i"], ["auto", "&#9680;", "Avto"]];
  return '<h3>KO\'RINISH</h3><div class="sg">' + m.map(function (x) {
    return '<div class="sc mc' + (MODE === x[0] ? ' on' : '') + '" data-mode="' + x[0] + '"><i>' + x[1] + '</i><b>' + x[2] + '</b></div>';
  }).join("") + '</div><div class="note">Avto — Telegram yoki tizim mavzusiga ergashadi.</div>';
}

function sBot(s) {
  var h = '<div class="card"><div class="lbl">&#129302; Bot profili</div><div style="height:12px"></div>' +
    '<div class="fl">Bot nomi*</div><input class="s" id="pn" maxlength="64" value="' + esc(s.name) + '">' +
    '<div class="fl">Bot haqida (about / tavsif)</div><textarea class="t" id="pa" maxlength="120" placeholder="Bot vazifasi haqida qisqacha ma\'lumot...">' + esc(s.about) + '</textarea>' +
    '<div class="note">Botning Telegram profilida ko\'rinadi (/start xabari emas — u Sozlama &rarr; Matnlar\'da).</div>' +
    '</div>';
  h += '<div class="card"><div class="lbl">&#128279; Do\'st taklif qilish</div>' + yn("ref_bonus_enabled", "Taklif bonusi", "Do\'st kanalga a\'zo bo\'lgach beriladi", s.ref_bonus_enabled) +
    '<div class="fl" style="margin-top:14px">Har bir do\'st uchun (so\'m)</div><input class="s" id="rb" type="number" inputmode="numeric" min="0" value="' + esc(s.ref_bonus_amount) + '"></div>';
  h += '<div class="card"><div class="lbl">&#9881;&#65039; Asosiy sozlamalar</div>' +
    yn("new_content_notify", "&#128276; Yangi kino xabari", "Kino qo\'shilganda obunachilarga xabar boradi", s.new_content_notify) +
    (s.is_pro ? yn("vip_system", "&#128142; VIP tizimi", "O\'chirilsa, VIP kinolarni ham hamma ko\'radi", s.vip_system) : '') +
    yn("protect_content", "&#128274; Ulashishni taqiqlash", "Kinoni boshqa chatga yuborib bo\'lmaydi", s.protect_content) +
    yn("auto_report_enabled", "&#127769; Kechki hisobot", "Har kuni " + esc(s.report_hour) + ":00 da ko\'rish, mijoz va tushum haqida bitta xabar", s.auto_report_enabled) +
    yn("movie_request_enabled", "&#128233; Kino so\'rash", "Kino topilmasa, foydalanuvchi «Shu kinoni so\'rash» tugmasini bosa oladi", s.movie_request_enabled) +
    yn("maintenance_mode", "&#128736; Texnik tanaffus", "Yoqilsa, mijozlar botdan vaqtincha foydalana olmaydi", s.maintenance_mode) +
    '<div class="note" style="margin:10px 0 0">Bu tugmalar bosilishi bilan saqlanadi.</div></div>';
  var a = s.autopost || [];
  h += '<div class="card"><div class="lbl">&#128226; Auto-post kanallari (' + a.length + '/3)</div>' +
    yn("weekly_top_enabled", "&#128202; Haftalik top", "Har yakshanba 20:00 da haftaning 5 ta eng ko\'p ko\'rilgan kinosi kanalga joylanadi", s.weekly_top_enabled);
  if (s.autopost_locked) h += '<div class="lock"><span>&#128274; <b>Pro</b>, <b>Turbo</b> va <b>Unlimited</b> tariflarida ishlaydi.</span><button class="gb" id="tarBtn">&#11088; Tariflar</button></div>';
  h += '<div class="note"><b>Yangi kino shu kanallarga o\'zi joylanadi</b></div>';
  a.forEach(function (c) {
    h += '<div class="item" style="box-shadow:none;padding:6px 0;margin:0"><div class="meta"><b>' + esc(c.title) + '</b><span>' + esc(c.username) + '</span></div><button class="sb" data-apdel="' + esc(c.key) + '">&#128465;&#65039;</button></div>';
  });
  h += '<div class="addrow"><input class="s" id="apc" placeholder="@kanal_username yoki -100..."><button class="tb" id="apAdd">Ulash</button></div>' +
    '<div class="note">Botni kanalga admin qiling, keyin shu yerdan ulang. 3 tagacha kanal.</div></div>';
  h += '<button class="btn" id="saveProf">&#128190; Profilni saqlash</button>';
  return h;
}
function sBtn(s) {
  return '<div class="card"><div class="lbl">Panel va tugmalar rangi</div><div class="note" style="margin:6px 0 4px">Barcha tugmalar va urg\'u ranglari o\'zgaradi. Har bir rangdan 5 xil ohang tanlang.</div>' +
    Object.keys(PAL).map(function (f) {
      return '<div class="note" style="margin:10px 0 2px"><b>' + esc(PAL[f].name) + '</b></div><div>' + PAL[f].shades.map(function (c, i) {
        var key = f + "-" + (i + 1);
        return '<button class="swc' + (s.theme === key ? " on" : "") + '" data-th="' + key + '" style="background:' + c + '"></button>';
      }).join("") + '</div>';
    }).join("") + '<button class="tb" style="margin-top:12px" data-th="">&#8617;&#65039; Standart rang</button></div>';
}
function sTxt(s) {
  return '<div class="card"><div class="lbl">Bot xabarlari</div><div style="height:10px"></div>' +
    '<div class="note" style="margin:0 0 6px">Salomlashuv matni (/start)</div><textarea class="t" id="wt">' + esc(s.welcome_text) + '</textarea>' +
    '<div class="note" style="margin:0 0 6px">Yordam matni</div><textarea class="t" id="ht">' + esc(s.help_text) + '</textarea>' +
    '<button class="btn" id="saveTx">Saqlash</button></div>';
}
function sChan(s) {
  var h = '<div class="card"><div class="lbl">&#128226; Majburiy obuna kanallari</div><div style="height:8px"></div>';
  if (!S.chans) return h + '<div class="empty">Yuklanmoqda...</div></div>';
  if (!S.chans.length) h += '<div class="note">Hozircha majburiy kanallar yo\'q.</div>';
  S.chans.forEach(function (c) {
    h += '<div class="item" style="box-shadow:none;padding:6px 0;margin:0"><div class="meta"><b>' + esc(c.title) + '</b><span>' + esc(c.username || c.type) + '</span></div><button class="sb" data-chdel="' + esc(c.key) + '">&#128465;&#65039;</button></div>';
  });
  return h + '<div class="addrow"><input class="s" id="chn" placeholder="@kanal_username"><button class="tb" id="chAdd">Qo\'shish</button></div><div class="note">Bot o\'sha kanalda ADMIN bo\'lishi shart.</div></div>';
}
function sAdm(s) {
  var h = '<div class="card"><div class="lbl">&#128101; Adminlar</div><div style="height:8px"></div>';
  if (!S.adms) return h + '<div class="empty">Yuklanmoqda...</div></div>';
  S.adms.admins.forEach(function (a) {
    h += '<div class="item" style="box-shadow:none;padding:6px 0;margin:0"><div class="meta"><b>' + esc(a.name || ("ID " + a.id)) + (a.owner ? '<span class="bdg">Egasi</span>' : '') + '</b><span>ID ' + a.id + '</span></div>' +
      (S.adms.can_manage && !a.owner ? '<button class="sb" data-admdel="' + a.id + '">&#128465;&#65039;</button>' : '') + '</div>';
  });
  if (S.adms.can_manage) h += '<div class="addrow"><input class="s" id="adn" type="number" inputmode="numeric" placeholder="Telegram ID"><button class="tb" id="admAdd">Qo\'shish</button></div><div class="note">Yangi admin botga kirib /start bosgan bo\'lishi kerak.</div>';
  else h += '<div class="note">Adminlarni faqat bot egasi boshqara oladi.</div>';
  return h + '</div>';
}
function sAds(s) {
  var h = '<div class="card"><div class="lbl">&#128250; Reklama (kino ostida)</div><div style="height:8px"></div>';
  if (!S.ads) return h + '<div class="empty">Yuklanmoqda...</div></div>';
  if (!S.ads.length) h += '<div class="note">Hozircha reklama yo\'q.</div>';
  S.ads.forEach(function (a) {
    h += '<div class="item" style="box-shadow:none;padding:6px 0;margin:0"><div class="meta"><b style="white-space:normal">' + esc(a.text.slice(0, 80)) + '</b><span>' + (a.active ? "&#128994; Faol" : "&#9898; O\'chiq") + '</span></div>' +
      '<button class="sb" data-adtg="' + esc(a.id) + '">' + (a.active ? "&#9208;&#65039;" : "&#9654;&#65039;") + '</button><button class="sb" data-addel="' + esc(a.id) + '">&#128465;&#65039;</button></div>';
  });
  return h + '<textarea class="t" id="adt" placeholder="Reklama matni (har bir kinodan keyin ko\'rsatiladi)"></textarea><button class="btn" id="adAdd">&#10133; Reklama qo\'shish</button></div>';
}

function vSets() {
  var s = S.sets; if (!s) return '<div class="empty">Yuklanmoqda...</div>';
  return modeCards() + '<h3>&#127912; BOT KO\'RINISHI</h3>' + subCards(SUBS) + '<h3>&#9881;&#65039; BOSHQARUV</h3>' + subCards(SUBS2) +
    ({bot: sBot, btn: sBtn, txt: sTxt, chan: sChan, adm: sAdm, ads: sAds})[S.sub](s);
}

function applyMode(m) {
  MODE = m;
  var eff = m;
  if (m === "auto") eff = (tg && tg.colorScheme) ? tg.colorScheme : ((window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", eff === "dark" ? "dark" : "light");
  document.getElementById("th").innerHTML = eff === "dark" ? "&#127769;" : "&#9728;&#65039;";
  try { localStorage.setItem("kp_mode", m); } catch (e) {}
}
async function loadSub() {
  try {
    if (S.sub === "chan") S.chans = (await api("/api/kino/channels")).channels;
    if (S.sub === "adm") S.adms = await api("/api/kino/admins");
    if (S.sub === "ads") S.ads = (await api("/api/kino/ads")).ads;
  } catch (e) { soft(e); }
}
function bindSets() {
  var $ = function (i) { return document.getElementById(i); };
  var on = function (sel, fn) { document.querySelectorAll(sel).forEach(function (el) { el.onclick = function () { fn(el); }; }); };
  on("[data-mode]", function (el) { applyMode(el.getAttribute("data-mode")); render(); });
  on("[data-sub]", function (el) { S.sub = el.getAttribute("data-sub"); render(); loadSub().then(render); });
  on("[data-yn]", function (el) {
    var b = {}; b[el.getAttribute("data-yn")] = el.getAttribute("data-v") === "1";
    api("/api/kino/settings", b).then(function (j) { S.sets = j; render(); }).catch(soft);
  });
  if ($("rb")) $("rb").onchange = function (e) { api("/api/kino/settings", {ref_bonus_amount: Number(e.target.value) || 0}).then(function (j) { S.sets = j; render(); }).catch(soft); };
  if ($("saveProf")) $("saveProf").onclick = function () {
    api("/api/kino/profile", {name: $("pn").value, about: $("pa").value}).then(function (j) {
      S.sets.name = j.name; S.sets.about = j.about;
      toast(j.warn ? "Saqlandi, lekin Telegramdagi nom/tavsif hozircha yangilanmadi. Keyinroq qayta urinib ko\'ring." : "Profil saqlandi."); render();
    }).catch(soft);
  };
  if ($("tarBtn")) $("tarBtn").onclick = function () { toast("Tarifni o\'zgartirish: bot ichida «Botlarim» → botingiz → «🔄 Tarif»."); };
  if ($("apAdd")) $("apAdd").onclick = function () { api("/api/kino/autopost", {op: "add", ref: $("apc").value}).then(function (j) { S.sets = j; render(); }).catch(soft); };
  on("[data-apdel]", function (el) { api("/api/kino/autopost", {op: "del", key: el.getAttribute("data-apdel")}).then(function (j) { S.sets = j; render(); }).catch(soft); });
  if ($("chAdd")) $("chAdd").onclick = function () { api("/api/kino/channels", {op: "add", ref: $("chn").value}).then(function (j) { S.chans = j.channels; render(); }).catch(soft); };
  on("[data-chdel]", function (el) { ask("Kanal olib tashlansinmi?", function () { api("/api/kino/channels", {op: "del", key: el.getAttribute("data-chdel")}).then(function (j) { S.chans = j.channels; render(); }).catch(soft); }); });
  if ($("admAdd")) $("admAdd").onclick = function () { api("/api/kino/admins", {op: "add", uid: $("adn").value}).then(function (j) { S.adms = j; render(); }).catch(soft); };
  on("[data-admdel]", function (el) { ask("Admin olib tashlansinmi?", function () { api("/api/kino/admins", {op: "del", uid: el.getAttribute("data-admdel")}).then(function (j) { S.adms = j; render(); }).catch(soft); }); });
  if ($("adAdd")) $("adAdd").onclick = function () { api("/api/kino/ads", {op: "add", text: $("adt").value}).then(function (j) { S.ads = j.ads; render(); }).catch(soft); };
  on("[data-adtg]", function (el) { api("/api/kino/ads", {op: "toggle", id: el.getAttribute("data-adtg")}).then(function (j) { S.ads = j.ads; render(); }).catch(soft); });
  on("[data-addel]", function (el) { ask("Reklama o\'chirilsinmi?", function () { api("/api/kino/ads", {op: "del", id: el.getAttribute("data-addel")}).then(function (j) { S.ads = j.ads; render(); }).catch(soft); }); });
}

var NAVICON = {
  dash: '<svg viewBox="0 0 24 24"><rect x="3.5" y="3.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.6"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.6"/></svg>',
  movies: '<svg viewBox="0 0 24 24"><rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M4 8.5h16M4 15.5h16M9 3.5v17M15 3.5v17"/></svg>',
  users: '<svg viewBox="0 0 24 24"><circle cx="9" cy="8.5" r="3.3"/><path d="M2.8 19.5c.6-3.3 3-5 6.2-5s5.6 1.7 6.2 5"/><path d="M15.6 5.4a3.2 3.2 0 0 1 0 6.2M18 14.9c1.9.6 3 2.2 3.4 4.6"/></svg>',
  pays: '<svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="13" rx="2.6"/><path d="M3 9.5V6.8A2.3 2.3 0 0 1 5.3 4.5H17"/><path d="M15.5 12.5h5.5v3.6h-5.5a1.8 1.8 0 0 1 0-3.6z"/></svg>',
  sets: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 13.5a1.6 1.6 0 0 0 .3 1.8l.1.1a1.9 1.9 0 1 1-2.7 2.7l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5v.3a1.9 1.9 0 1 1-3.8 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a1.9 1.9 0 1 1-2.7-2.7l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1h-.3a1.9 1.9 0 1 1 0-3.8h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1A1.9 1.9 0 1 1 7.1 4.3l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5v-.3a1.9 1.9 0 1 1 3.8 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a1.9 1.9 0 1 1 2.7 2.7l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1h.3a1.9 1.9 0 1 1 0 3.8h-.1a1.6 1.6 0 0 0-1.5 1z"/></svg>'
};

function render() {
  var app = document.getElementById("app");
  var pro = S.ov && S.ov.bot.is_pro;
  var tabs = TABS.filter(function (t) { return t[0] !== "pays" || pro; });
  document.getElementById("nav").innerHTML = tabs.map(function (t) { return '<div data-t="' + t[0] + '" class="' + (S.tab === t[0] ? "on" : "") + '"><i>' + (NAVICON[t[0]] || t[1]) + '</i>' + t[2] + '</div>'; }).join("");
  document.getElementById("ttl").textContent = TITLES[S.tab][0];
  document.getElementById("sub").textContent = TITLES[S.tab][1];
  document.getElementById("foot").textContent = S.ov ? (S.ov.bot.username ? "@" + S.ov.bot.username : S.ov.bot.name) : "";
  if (S.err) { app.innerHTML = '<div class="empty">' + esc(S.err) + '</div>'; return; }
  var mq = document.activeElement && document.activeElement.id;
  app.innerHTML = ({dash: vDash, movies: vMovies, users: vUsers, pays: vPays, sets: vSets})[S.tab]();
  bind();
  if (mq === "mq" || mq === "uq") { var el = document.getElementById(mq); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }
}

function bind() {
  var $ = function (i) { return document.getElementById(i); };
  if ($("backBtn")) $("backBtn").onclick = function () { history.back(); };
  if ($("c1")) { $("c1").onclick = function () { S.chart = "views"; render(); }; $("c2").onclick = function () { S.chart = "new"; render(); }; }
  if ($("mq")) $("mq").oninput = function (e) { S.q = e.target.value; render(); };
  if ($("uq")) $("uq").onchange = function (e) { S.uq = e.target.value; load("users"); };
  document.querySelectorAll("[data-f]").forEach(function (el) { el.onclick = function () { S.filter = el.getAttribute("data-f"); render(); }; });
  document.querySelectorAll("[data-ps]").forEach(function (el) { el.onclick = function () { S.psub = el.getAttribute("data-ps"); render(); }; });
  document.querySelectorAll("[data-del]").forEach(function (el) { el.onclick = function () {
    var c = el.getAttribute("data-del");
    ask("Kino #" + c + " o'chirilsinmi?", function () { api("/api/kino/movie/delete", {code: c}).then(function () { load("movies"); load("dash", true); }).catch(fail); });
  }; });
  document.querySelectorAll("[data-vip]").forEach(function (el) { el.onclick = function () {
    var c = el.getAttribute("data-vip"), m = S.movies.filter(function (x) { return x.code === c; })[0];
    ask(m.vip ? "Kino #" + c + " VIP'dan chiqarilsinmi?" : "Kino #" + c + " VIP (Premium) qilinsinmi?", function () { api("/api/kino/movie/vip", {code: c, vip: !m.vip}).then(function () { load("movies"); load("dash", true); }).catch(fail); });
  }; });
  document.querySelectorAll("[data-blk]").forEach(function (el) { el.onclick = function () {
    var id = Number(el.getAttribute("data-blk")), to = el.getAttribute("data-to") === "1";
    ask(to ? id + " bloklansinmi?" : id + " blokdan chiqarilsinmi?", function () { api("/api/kino/user/block", {uid: id, block: to}).then(function () { load("users"); load("dash", true); }).catch(fail); });
  }; });
  document.querySelectorAll("[data-tg]").forEach(function (el) { el.onclick = function () {
    var k = el.getAttribute("data-tg"), b = {}; b[k] = !S.sets[k];
    api("/api/kino/settings", b).then(function (j) { S.sets = j; render(); }).catch(fail);
  }; });
  document.querySelectorAll("[data-th]").forEach(function (el) { el.onclick = function () {
    api("/api/kino/settings", {theme: el.getAttribute("data-th")}).then(function (j) { S.sets = j; applyTheme(j.theme); render(); }).catch(fail);
  }; });
  if ($("saveTx")) $("saveTx").onclick = function () {
    api("/api/kino/settings", {welcome_text: $("wt").value, help_text: $("ht").value}).then(function (j) { S.sets = j; toast("Saqlandi"); render(); }).catch(soft);
  };
  bindSets();
}

async function load(tab, silent) {
  try {
    if (tab === "dash") { S.ov = await api("/api/kino/overview"); applyTheme(S.ov.theme); }
    if (tab === "movies") S.movies = (await api("/api/kino/movies")).movies;
    if (tab === "users") S.users = await api("/api/kino/users", undefined, "&q=" + encodeURIComponent(S.uq));
    if (tab === "pays") S.pays = await api("/api/kino/payments");
    if (tab === "sets") { S.sets = await api("/api/kino/settings"); applyTheme(S.sets.theme); await loadSub(); }
    S.err = "";
    if (!silent) render(); else if (tab === "dash") render();
  } catch (e) { fail(e); }
}

function go(t) { S.tab = t; render(); load(t); if (t === "movies" && !S.ov) load("dash", true); }
document.getElementById("nav").onclick = function (e) { var d = e.target.closest("[data-t]"); if (d) go(d.getAttribute("data-t")); };
document.getElementById("rf").onclick = function () { load(S.tab); if (S.tab !== "dash") load("dash", true); };
document.getElementById("th").onclick = function () {
  applyMode(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
  if (S.tab === "sets") render();
};
(function () {
  var t = null; try { t = localStorage.getItem("kp_mode") || localStorage.getItem("kp_theme"); } catch (e) {}
  applyMode(t === "dark" || t === "light" ? t : "auto");
  if (FROM === "creator" && tg && tg.BackButton) { tg.BackButton.show(); tg.BackButton.onClick(function () { history.back(); }); }
  if (!INIT) { S.err = "Panelni Telegram ichidan oching."; render(); } else { render(); load("dash"); }
})();
</script>
</body>
</html>
"""


BOTSTATS_HTML = r"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<title>Bot statistikasi</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#04070f;--card:#0a1020;--bd:#1a2540;--tx:#eef2fa;--mu:#6f7d9c;--gr:#22c07a;--bl:#38a3e8;--or:#e8a317;--rd:#e5484d;--pu:#8b5cf6}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--tx);font-family:-apple-system,"Segoe UI",Roboto,sans-serif}
body{background:radial-gradient(120% 40% at 50% 0%,#0b1f3a 0%,var(--bg) 60%);min-height:100vh;padding:18px 14px calc(30px + env(safe-area-inset-bottom,0px))}
.hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.hd h1{margin:0;font-size:28px;font-weight:800}
.hr{display:flex;align-items:center;gap:10px}
.rf{width:46px;height:46px;border-radius:14px;border:1px solid var(--bd);background:#0b1326;font-size:20px;color:var(--tx)}
.st{display:flex;align-items:center;gap:8px;border:1px solid #14634a;background:#0b2a22;color:var(--gr);border-radius:18px;padding:12px 16px;font-weight:700;font-size:17px}
.st i{width:10px;height:10px;border-radius:50%;background:var(--gr);display:inline-block}
.st.off{border-color:#6b2226;background:#2a0f12;color:var(--rd)}.st.off i{background:var(--rd)}
.card{background:var(--card);border:1px solid var(--bd);border-radius:26px;padding:18px;margin-bottom:14px}
.pf{display:flex;align-items:center;gap:16px}
.av{width:72px;height:72px;border-radius:20px;background:#0f2038;border:1px solid var(--bd);display:flex;align-items:center;justify-content:center;font-size:36px;flex:none}
.pf b{font-size:22px;display:block}.pf .un{color:var(--bl);font-size:18px;margin:2px 0 8px;display:block;word-break:break-all}
.chip{display:inline-block;border:1px solid var(--bd);background:#101a30;border-radius:10px;padding:6px 12px;font-size:15px;font-weight:600;margin-right:8px}
.chip.b{border-color:#1d4d8a;background:#0e2a52;color:#5db4ff}
.tabs{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.seg{display:flex;background:#0a1122;border:1px solid var(--bd);border-radius:20px;padding:6px;flex:none}
.seg div{padding:12px 20px;border-radius:15px;font-weight:700;font-size:17px;color:var(--mu)}
.seg div.on{background:#1e3fb0;color:#fff}
.upd{color:var(--mu);font-size:15px;line-height:1.3}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
.grid .card{margin:0;padding:16px}
.ic{width:52px;height:52px;border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:26px;margin-bottom:12px}
.n{font-size:44px;font-weight:800;line-height:1}
.l{color:var(--mu);font-size:17px;margin:6px 0 14px}
.bar{height:6px;border-radius:3px;background:#141d33;overflow:hidden}.bar i{display:block;height:100%;border-radius:3px}
h2{margin:0 0 4px;font-size:24px}.sub{color:var(--mu);font-size:17px;margin-bottom:12px}
.leg{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:17px;color:#b5c0d8;margin-bottom:10px}
.leg span i{display:inline-block;width:18px;height:4px;border-radius:2px;margin-right:8px;vertical-align:middle}
.row{display:flex;align-items:center;justify-content:space-between;padding:15px 0;border-top:1px solid #131c33;font-size:19px}
.row:first-of-type{border-top:0}
.row span{color:#b5c0d8}.row span:before{content:"";display:inline-block;width:11px;height:11px;border-radius:50%;background:var(--c);margin-right:14px}
.row b{font-size:26px;color:var(--c)}
.ok{border:1.5px dashed #14634a;background:#0a1f1c;color:var(--gr);border-radius:20px;padding:22px;text-align:center;font-weight:700;font-size:19px}
.er{display:flex;justify-content:space-between;padding:12px 0;border-top:1px solid #131c33;font-size:18px}.er b{color:var(--rd)}
.ft{text-align:center;color:#3f4c6b;font-size:18px;font-weight:600;margin-top:22px}
.empty{text-align:center;color:var(--mu);padding:60px 10px;font-size:18px}
</style>
</head>
<body>
<div id="app"><div class="empty">Yuklanmoqda...</div></div>
<script>
var tg = window.Telegram && window.Telegram.WebApp;
if (tg) { try { tg.ready(); tg.expand(); } catch (e) {} try { tg.setHeaderColor("#000000"); tg.setBackgroundColor("#04070f"); } catch (e) {} }
var Q = new URLSearchParams(location.search);
var B = Q.get("b") || "";
var INIT = (tg && tg.initData) || "";
var P = "today", D = null, ERR = "", busy = false;

function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
function fmt(n) { return String(n || 0).replace(/\B(?=(\d{3})+(?!\d))/g, " "); }

function chart(ch) {
  var W = 340, H = 200, L = 34, R = 12, T = 12, Bm = 28;
  var keys = [["users", "#22c07a"], ["msgs", "#38a3e8"], ["reqs", "#e8a317"], ["errs", "#e5484d"]];
  var mx = 0; keys.forEach(function (k) { ch[k[0]].forEach(function (v) { if (v > mx) mx = v; }); });
  var top = mx < 4 ? 4 : Math.ceil(mx / 4) * 4, n = ch.labels.length;
  var px = function (i) { return n > 1 ? L + i * (W - L - R) / (n - 1) : L; };
  var py = function (v) { return T + (H - T - Bm) * (1 - v / top); };
  var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%">';
  for (var g = 0; g <= 4; g++) {
    var v = top * g / 4, y = py(v);
    s += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '" stroke="#1a2540" stroke-dasharray="2 4"/>' +
      '<text x="' + (L - 6) + '" y="' + (y + 4) + '" fill="#4a587a" font-size="11" text-anchor="end">' + v + '</text>';
  }
  var step = n > 12 ? Math.ceil(n / 7) : 1;
  ch.labels.forEach(function (l, i) {
    if (i % step === 0) s += '<text x="' + px(i) + '" y="' + (H - 8) + '" fill="#4a587a" font-size="11" text-anchor="middle">' + l + '</text>';
  });
  keys.slice().reverse().forEach(function (k) {
    var pts = ch[k[0]].map(function (v, i) { return px(i).toFixed(1) + "," + py(v).toFixed(1); });
    s += '<polyline points="' + pts.join(" ") + '" fill="none" stroke="' + k[1] + '" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>';
    if (n <= 14) ch[k[0]].forEach(function (v, i) { s += '<circle cx="' + px(i).toFixed(1) + '" cy="' + py(v).toFixed(1) + '" r="3.5" fill="' + k[1] + '"/>'; });
  });
  return s + '</svg>';
}

function render() {
  var app = document.getElementById("app");
  if (ERR) { app.innerHTML = '<div class="empty">' + esc(ERR) + '</div>'; return; }
  if (!D) { app.innerHTML = '<div class="empty">Yuklanmoqda...</div>'; return; }
  var c = D.cards, t = D.today;
  var mxv = Math.max(1, c.users, c.msgs, c.reqs, c.errs, c.undel);
  var w = function (v) { return Math.max(8, Math.round(v * 100 / mxv)); };
  var cards = [
    ["&#128101;", "#0d2340", "#3b82f6", c.users, "Yangi userlar", w(c.users)],
    ["&#128172;", "#0c2a26", "#22c07a", c.msgs, "Jami xabarlar", w(c.msgs)],
    ["&#9889;", "#2a2410", "#38a3e8", c.reqs, "Jami so'rovlar", w(c.reqs)],
    ["&#10060;", "#2a1013", "#e5484d", c.errs, "Xatoliklar", w(c.errs)],
    ["&#128683;", "#2a1a10", "#e8a317", c.undel, "Yetkazilmagan", w(c.undel)],
    ["&#9201;&#65039;", "#1c1638", "#8b5cf6", c.uptime + "%", "Webhook uptime", Math.max(8, c.uptime)]
  ];
  var h = '<div class="hd"><h1>' + esc(D.name) + '</h1><div class="hr"><button class="rf" id="rf">&#128260;</button><div class="st' + (D.active ? "" : " off") + '"><i></i>' + (D.active ? "Aktiv" : "To'xtagan") + '</div></div></div>';
  h += '<div class="card pf"><div class="av">' + D.emoji + '</div><div><b>' + esc(D.name) + '</b>' + (D.username ? '<span class="un">@' + esc(D.username) + '</span>' : '<span class="un">&nbsp;</span>') +
    '<span class="chip">' + esc(D.type_label) + '</span><span class="chip b">&#127903; ' + esc(D.tariff) + '</span></div></div>';
  h += '<div class="tabs"><div class="seg">' + [["today", "Bugun"], ["week", "Hafta"], ["month", "Oy"]].map(function (p) { return '<div data-p="' + p[0] + '" class="' + (P === p[0] ? "on" : "") + '">' + p[1] + '</div>'; }).join("") + '</div><div class="upd">' + D.updated + ' da<br>yangilangan</div></div>';
  h += '<div class="grid">' + cards.map(function (x) {
    return '<div class="card"><div class="ic" style="background:' + x[1] + '">' + x[0] + '</div><div class="n">' + (typeof x[3] === "number" ? fmt(x[3]) : x[3]) + '</div><div class="l">' + x[4] + '</div><div class="bar"><i style="width:' + x[5] + '%;background:' + x[2] + '"></i></div></div>';
  }).join("") + '</div>';
  h += '<div class="card"><h2>Faollik grafigi</h2><div class="sub">' + (P === "today" ? "Bugun — soatlab" : (P === "week" ? "Oxirgi 7 kun" : "Oxirgi 30 kun")) + '</div>' +
    '<div class="leg"><span><i style="background:#22c07a"></i>Userlar</span><span><i style="background:#38a3e8"></i>Xabar</span><span><i style="background:#e8a317"></i>So\'rov</span><span><i style="background:#e5484d"></i>Xato</span></div>' + chart(D.chart) + '</div>';
  h += '<div class="card"><h2>Bugun</h2><div class="sub">Real vaqt</div>' +
    [["Yangi user", "+" + fmt(t.users), "#22c07a"], ["Xabarlar", fmt(t.msgs), "#38a3e8"], ["So'rovlar", fmt(t.reqs), "#22b8cf"], ["Xatoliklar", fmt(t.errs), "#e5484d"], ["Yetkazilm.", fmt(t.undel), "#e8a317"]].map(function (r) {
      return '<div class="row" style="--c:' + r[2] + '"><span>' + r[0] + '</span><b>' + r[1] + '</b></div>';
    }).join("") + '</div>';
  h += '<div class="card"><h2>Xato turlari</h2><div class="sub">' + (P === "today" ? "Bugun" : (P === "week" ? "Hafta" : "Oy")) + '</div>' +
    (D.err_types.length ? D.err_types.map(function (e) { return '<div class="er"><span>' + esc(e.name) + '</span><b>' + fmt(e.count) + '</b></div>'; }).join("") : '<div class="ok">&#9989; Xatoliklar aniqlanmadi</div>') + '</div>';
  h += '<div class="ft">' + (D.creator ? "@" + esc(D.creator) : "") + '</div>';
  app.innerHTML = h;
  document.getElementById("rf").onclick = function () { load(); };
  document.querySelectorAll("[data-p]").forEach(function (el) { el.onclick = function () { P = el.getAttribute("data-p"); load(); }; });
}

async function load() {
  if (busy) return;
  if (!INIT) { ERR = "Sahifani Telegram ichidan oching."; render(); return; }
  busy = true;
  try {
    var r = await fetch("/api/botstats?b=" + encodeURIComponent(B) + "&period=" + P + "&initData=" + encodeURIComponent(INIT));
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) {
      ERR = r.status === 401 ? "Sahifani Telegram ichidan oching." : (r.status === 403 ? "Ruxsat yo'q." : (r.status === 404 ? "Bot topilmadi." : "Xatolik yuz berdi. Qayta urinib ko'ring."));
    } else { D = j; ERR = ""; }
  } catch (e) { ERR = "Internet bilan aloqa yo'q. Qayta urinib ko'ring."; }
  busy = false;
  render();
}
render();
load();
setInterval(function () { if (!document.hidden) load(); }, 20000);
</script>
</body>
</html>
"""


async def botstats_page(request):
    return web.Response(
        text=BOTSTATS_HTML,
        content_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
    )


async def kinopanel_page(request):
    return web.Response(
        text=KINOPANEL_HTML.replace("__PALETTES__", theme_palettes_json()),
        content_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
    )


def get_miniapp_url() -> str:
    explicit = os.getenv("MINIAPP_URL")
    if explicit:
        return explicit.rstrip("/") + "/miniapp"
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    if domain:
        return f"https://{domain}/miniapp"
    return ""


async def main():
    # Oddiy Kino Bot ("kino" / eski "kino_ultra") endi platformada yo'q — mavjud botlar bepul Kino BOTga o'tkaziladi
    upgraded = 0
    for info in data["bots"].values():
        if info.get("type") in ("kino", "kino_ultra"):
            info["type"] = "kino_pro"
            upgraded += 1
    if upgraded:
        save_data()
        logging.info(f"{upgraded} ta Oddiy Kino Bot avtomatik (bepul) Kino BOTga o'tkazildi.")

    # Stars bot turi butunlay bekor qilindi (hech kim foydalanmagan) — mavjud botlar to'xtatilib o'chiriladi
    stars_tokens = [token for token, info in data["bots"].items() if info.get("type") == "stars"]
    for token in stars_tokens:
        info = data["bots"][token]
        try:
            await main_bot.send_message(
                info["admin_id"],
                f"⚠️ \"{info['name']}\" boti (Stars bot turi) platformadan butunlay olib tashlandi va endi ishlamaydi.",
            )
        except Exception:
            pass
        del data["bots"][token]
    if stars_tokens:
        save_data()
        logging.info(f"{len(stars_tokens)} ta Stars bot platformadan butunlay o'chirildi.")

    # Nakrutka bot turi butunlay olib tashlandi — bazadagi qoldiqlar (botlar va sozlamalar) tozalanadi.
    # (Bir marta deploy qilingach bu blokni o'chirib tashlash mumkin.)
    _gone = [t for t, i in data["bots"].items() if i.get("type") == "smm"]
    for _t in _gone:
        try:
            await main_bot.send_message(
                data["bots"][_t]["admin_id"],
                f"⚠️ \"{data['bots'][_t]['name']}\" boti (Nakrutka bot turi) platformadan olib tashlandi va endi ishlamaydi.",
            )
        except Exception:
            pass
        del data["bots"][_t]
    _cfg_cleaned = False
    for _k in ("type_prices", "bot_type_trial"):
        if "smm" in data.get(_k, {}):
            data[_k].pop("smm", None)
            _cfg_cleaned = True
    if _gone or _cfg_cleaned:
        save_data()
        logging.info(f"Nakrutka bot qoldiqlari tozalandi: {len(_gone)} ta bot.")

    asyncio.create_task(start_web_server())

    # Obunachilar uchun klaviatura yonidagi tugma: buyruqlar menyusi (/start, /newbot, /mybots ...)
    await setup_public_menu(main_bot)

    miniapp_url = get_miniapp_url()
    if miniapp_url:
        logging.info(f"Mini App manzili: {miniapp_url}")
        # To'liq adminlar uchun klaviatura yonidagi Menu tugmasi — «Admin» panel
        for _aid in {ADMIN_ID, *data["full_admins"]}:
            await set_admin_menu_button(main_bot, _aid)
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
    asyncio.create_task(kino_scheduler_loop())
    try:
        await main_bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        logging.error(f"delete_webhook xatosi (asosiy bot): {e}")
    await main_dp.start_polling(main_bot)


if __name__ == "__main__":
    asyncio.run(main())
