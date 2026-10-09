import asyncio
import html
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "").strip()
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()
PUBLIC_BOT_USERNAME = os.getenv("PUBLIC_BOT_USERNAME", "KuplyuProdamAutoBot").strip()

ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW) if ADMIN_CHAT_ID_RAW else None

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not set")
if not SUPABASE_SECRET_KEY:
    raise RuntimeError("SUPABASE_SECRET_KEY is not set")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kuplyu-prodam-auto")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


class BuyerForm(StatesGroup):
    city = State()
    vehicle = State()
    budget = State()
    min_year = State()
    max_year = State()
    max_mileage = State()
    requirements = State()
    contact = State()


class SellerForm(StatesGroup):
    city = State()
    vehicle = State()
    year = State()
    price = State()
    mileage = State()
    contact = State()


MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚗 Хочу купить авто")],
        [KeyboardButton(text="💰 Хочу продать авто")],
    ],
    resize_keyboard=True,
)

CONTACT_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)],
        [KeyboardButton(text="💬 Оставить Telegram")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

SKIP_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Пропустить")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def parse_money(text: str) -> int | None:
    normalized = text.lower().replace("₽", " руб ").replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)\s*млн", normalized)
    if m:
        return int(float(m.group(1)) * 1_000_000)
    m = re.search(r"([\d\s]{2,})\s*тыс", normalized)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        if digits:
            return int(digits) * 1_000
    digits = re.sub(r"\D", "", normalized)
    if digits:
        value = int(digits)
        if value >= 10_000:
            return value
    return None


def parse_int(text: str) -> int | None:
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def user_identity(message: Message) -> dict[str, Any]:
    user = message.from_user
    return {
        "telegram_user_id": user.id if user else 0,
        "username": user.username if user else None,
        "full_name": user.full_name if user else None,
        "contact_telegram": f"@{user.username}" if user and user.username else None,
    }


def contact_from_message(message: Message) -> tuple[str | None, str | None]:
    phone = message.contact.phone_number if message.contact else None
    username = None
    if message.text == "💬 Оставить Telegram" and message.from_user and message.from_user.username:
        username = f"@{message.from_user.username}"
    elif message.text and message.text != "📱 Отправить номер телефона":
        text = message.text.strip()
        if text.startswith("@"):
            username = text[:100]
        elif re.search(r"\d{6,}", text):
            phone = text[:60]
    return phone, username


def _fingerprint_text(value: Any) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    return " ".join(text.split())


def _fingerprint_number(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value).strip()


def request_fingerprint(payload: dict[str, Any]) -> str:
    request_type = str(payload.get("request_type") or "")
    common = [
        request_type,
        _fingerprint_text(payload.get("city")),
        _fingerprint_text(payload.get("vehicle")),
    ]
    if request_type == "buyer":
        parts = common + [
            _fingerprint_number(payload.get("budget")),
            _fingerprint_number(payload.get("min_vehicle_year")),
            _fingerprint_number(payload.get("max_vehicle_year")),
            _fingerprint_number(payload.get("max_mileage_km")),
            _fingerprint_text(payload.get("requirements")),
        ]
    else:
        parts = common + [
            _fingerprint_number(payload.get("vehicle_year")),
            _fingerprint_number(payload.get("asking_price")),
            _fingerprint_number(payload.get("mileage_km")),
        ]
    canonical = "|".join(parts)
    return hashlib.md5(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()


def find_active_duplicate(payload: dict[str, Any], fingerprint: str) -> dict[str, Any] | None:
    rows = (
        supabase.table("market_requests")
        .select("*")
        .eq("telegram_user_id", int(payload.get("telegram_user_id") or 0))
        .eq("request_type", payload.get("request_type"))
        .eq("request_fingerprint", fingerprint)
        .in_("status", ["new", "in_progress"])
        .order("created_at")
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def save_market_request(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    fingerprint = request_fingerprint(payload)
    payload["request_fingerprint"] = fingerprint

    existing = find_active_duplicate(payload, fingerprint)
    if existing:
        row = dict(existing)
        row["_is_duplicate"] = True
        return row

    try:
        rows = supabase.table("market_requests").insert(payload).execute().data or []
    except Exception:
        # The database also has a unique active-request guard. If two identical
        # submissions race each other, return the one that won instead of creating
        # a duplicate or showing a false save error.
        existing = find_active_duplicate(payload, fingerprint)
        if existing:
            row = dict(existing)
            row["_is_duplicate"] = True
            return row
        raise

    if not rows:
        raise RuntimeError("market request was not saved")
    row = dict(rows[0])
    row["_is_duplicate"] = False
    return row


def link_request_to_lead(request_id: int, lead_id: int) -> None:
    (
        supabase.table("market_requests")
        .update({"radar_lead_id": lead_id})
        .eq("id", request_id)
        .execute()
    )


def find_existing_request_lead(request_id: int) -> dict[str, Any] | None:
    rows = (
        supabase.table("leads")
        .select("*")
        .contains("raw_data", {"market_request_id": request_id})
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


MATCH_THRESHOLD = 70

VEHICLE_ALIASES = {
    "бмв": "bmw",
    "тойота": "toyota",
    "мерседес": "mercedes",
    "мерс": "mercedes",
    "фольксваген": "volkswagen",
    "фольцваген": "volkswagen",
    "ауди": "audi",
    "лексус": "lexus",
    "хонда": "honda",
    "мазда": "mazda",
    "ниссан": "nissan",
    "хендай": "hyundai",
    "хундай": "hyundai",
    "киа": "kia",
    "шкода": "skoda",
    "лада": "lada",
    "хавал": "haval",
    "джили": "geely",
    "чери": "chery",
    "омода": "omoda",
    "зикр": "zeekr",
    "камри": "camry",
    "рав4": "rav4",
    "рав": "rav4",
    "икс5": "x5",
    "икс3": "x3",
    "икс6": "x6",
}

VEHICLE_NOISE = {
    "авто", "автомобиль", "автомобиля", "машина", "машину", "продам", "куплю",
    "ищу", "хочу", "год", "года", "г", "руб", "рублей",
}


def normalize_text(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return " ".join(text.split())


def esc(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value))


def normalize_city(value: Any) -> str:
    return normalize_text(value)


def vehicle_tokens(value: Any) -> set[str]:
    tokens: set[str] = set()
    for token in normalize_text(value).split():
        token = VEHICLE_ALIASES.get(token, token)
        if token not in VEHICLE_NOISE and len(token) > 1:
            tokens.add(token)
    return tokens


def vehicle_similarity(buyer_vehicle: Any, seller_vehicle: Any) -> float:
    buyer_tokens = vehicle_tokens(buyer_vehicle)
    seller_tokens = vehicle_tokens(seller_vehicle)
    if not buyer_tokens or not seller_tokens:
        return 0.0
    if buyer_tokens <= seller_tokens or seller_tokens <= buyer_tokens:
        return 1.0
    overlap = len(buyer_tokens & seller_tokens) / len(buyer_tokens | seller_tokens)
    left = " ".join(sorted(buyer_tokens))
    right = " ".join(sorted(seller_tokens))
    sequence = SequenceMatcher(None, left, right).ratio()
    return max(overlap, sequence * 0.9)


def evaluate_match(buyer: dict[str, Any], seller: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    # Never connect a Telegram user with their own buy/sell request.
    buyer_user_id = int(buyer.get("telegram_user_id") or 0)
    seller_user_id = int(seller.get("telegram_user_id") or 0)
    if buyer_user_id and seller_user_id and buyer_user_id == seller_user_id:
        return None

    # Year and mileage are mandatory buyer criteria. Legacy buyer requests without
    # them stay in the database but do not create automatic matches.
    min_year = buyer.get("min_vehicle_year")
    max_year = buyer.get("max_vehicle_year")
    max_mileage = buyer.get("max_mileage_km")
    seller_year = seller.get("vehicle_year")
    seller_mileage = seller.get("mileage_km")
    if min_year is None or max_year is None or max_mileage is None or seller_year is None or seller_mileage is None:
        return None

    min_year = int(min_year)
    max_year = int(max_year)
    max_mileage = int(max_mileage)
    seller_year = int(seller_year)
    seller_mileage = int(seller_mileage)

    # Hard filters: wrong year or excessive mileage means no match.
    if seller_year < min_year or seller_year > max_year:
        return None
    if seller_mileage > max_mileage:
        return None

    similarity = vehicle_similarity(buyer.get("vehicle"), seller.get("vehicle"))
    if similarity < 0.58:
        return None

    score = round(similarity * 50)
    reasons: dict[str, Any] = {
        "vehicle_similarity": round(similarity, 2),
        "vehicle_match": similarity >= 0.82,
        "year_match": True,
        "buyer_min_year": min_year,
        "buyer_max_year": max_year,
        "seller_year": seller_year,
        "mileage_match": True,
        "buyer_max_mileage_km": max_mileage,
        "seller_mileage_km": seller_mileage,
    }
    score += 10
    score += 10

    buyer_city = normalize_city(buyer.get("city"))
    seller_city = normalize_city(seller.get("city"))
    same_city = bool(buyer_city and seller_city and buyer_city == seller_city)
    reasons["same_city"] = same_city
    if same_city:
        score += 10

    budget = buyer.get("budget")
    price = seller.get("asking_price")
    if budget is not None and price is not None:
        budget_value = float(budget)
        price_value = float(price)
        if budget_value <= 0:
            return None
        ratio = price_value / budget_value
        reasons["price_to_budget"] = round(ratio, 3)
        if ratio <= 1.0:
            score += 20
            reasons["within_budget"] = True
        elif ratio <= 1.10:
            score += 10
            reasons["within_budget"] = False
            reasons["slightly_over_budget"] = True
        elif ratio <= 1.20:
            score += 4
            reasons["within_budget"] = False
            reasons["over_budget"] = True
        else:
            return None
    else:
        reasons["within_budget"] = None

    score = max(0, min(100, score))
    if score < MATCH_THRESHOLD:
        return None
    return score, reasons

def insert_match_if_new(buyer: dict[str, Any], seller: dict[str, Any]) -> dict[str, Any] | None:
    buyer_id = int(buyer["id"])
    seller_id = int(seller["id"])
    existing = (
        supabase.table("auto_matches")
        .select("id,status")
        .eq("buyer_request_id", buyer_id)
        .eq("seller_request_id", seller_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        return existing[0]

    evaluated = evaluate_match(buyer, seller)
    if not evaluated:
        return None
    score, reasons = evaluated
    rows = (
        supabase.table("auto_matches")
        .insert({
            "buyer_request_id": buyer_id,
            "seller_request_id": seller_id,
            "match_score": score,
            "match_reasons": reasons,
            "status": "new",
            "notification_status": "pending",
        })
        .execute()
        .data
        or []
    )
    if rows:
        log.info("Auto match %s created: buyer=%s seller=%s score=%s", rows[0].get("id"), buyer_id, seller_id, score)
        return rows[0]
    return None


def create_matches_for_request(request_row: dict[str, Any]) -> int:
    request_type = request_row.get("request_type")
    if request_type not in {"buyer", "seller"}:
        return 0
    opposite_type = "seller" if request_type == "buyer" else "buyer"
    candidates = (
        supabase.table("market_requests")
        .select("*")
        .eq("request_type", opposite_type)
        .in_("status", ["new", "in_progress"])
        .order("created_at", desc=True)
        .limit(200)
        .execute()
        .data
        or []
    )
    created = 0
    for candidate in candidates:
        buyer = request_row if request_type == "buyer" else candidate
        seller = candidate if request_type == "buyer" else request_row
        if insert_match_if_new(buyer, seller):
            created += 1
    return created


def backfill_auto_matches() -> None:
    buyers = (
        supabase.table("market_requests")
        .select("*")
        .eq("request_type", "buyer")
        .in_("status", ["new", "in_progress"])
        .order("created_at")
        .limit(300)
        .execute()
        .data
        or []
    )
    sellers = (
        supabase.table("market_requests")
        .select("*")
        .eq("request_type", "seller")
        .in_("status", ["new", "in_progress"])
        .order("created_at")
        .limit(300)
        .execute()
        .data
        or []
    )
    def signature(row: dict[str, Any]) -> tuple[Any, ...]:
        # Only true duplicate active requests are collapsed during startup backfill.
        # Different year/mileage/requirements remain separate requests.
        return (
            row.get("telegram_user_id"),
            row.get("request_type"),
            row.get("request_fingerprint") or request_fingerprint(row),
        )

    seen_buyers: set[tuple[Any, ...]] = set()
    unique_buyers: list[dict[str, Any]] = []
    for buyer in buyers:
        sig = signature(buyer)
        if sig in seen_buyers:
            continue
        seen_buyers.add(sig)
        unique_buyers.append(buyer)

    seen_sellers: set[tuple[Any, ...]] = set()
    unique_sellers: list[dict[str, Any]] = []
    for seller in sellers:
        sig = signature(seller)
        if sig in seen_sellers:
            continue
        seen_sellers.add(sig)
        unique_sellers.append(seller)

    for buyer in unique_buyers:
        for seller in unique_sellers:
            try:
                insert_match_if_new(buyer, seller)
            except Exception:
                log.exception("Could not backfill match buyer=%s seller=%s", buyer.get("id"), seller.get("id"))


def get_request(request_id: int) -> dict[str, Any] | None:
    rows = (
        supabase.table("market_requests")
        .select("*")
        .eq("id", request_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def get_match(match_id: int) -> dict[str, Any] | None:
    rows = (
        supabase.table("auto_matches")
        .select("*")
        .eq("id", match_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def get_match_bundle(match_id: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    match = get_match(match_id)
    if not match:
        return None
    buyer = get_request(int(match["buyer_request_id"]))
    seller = get_request(int(match["seller_request_id"]))
    if not buyer or not seller:
        return None
    return match, buyer, seller


def contact_text(request_row: dict[str, Any]) -> str:
    return str(request_row.get("contact_phone") or request_row.get("contact_telegram") or "контакт не указан")


def format_rub(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        return f"{int(float(value)):,} ₽".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def offer_keyboard(match_id: int, side: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, интересно", callback_data=f"macc:{match_id}:{side}"),
        InlineKeyboardButton(text="❌ Не интересно", callback_data=f"mdec:{match_id}:{side}"),
    ]])


async def send_match_offer(match: dict[str, Any], buyer: dict[str, Any], seller: dict[str, Any]) -> None:
    match_id = int(match["id"])
    buyer_chat_id = int(buyer["telegram_user_id"])
    seller_chat_id = int(seller["telegram_user_id"])

    buyer_text = (
        f"🔗 <b>Найден подходящий автомобиль · совпадение {int(match.get('match_score') or 0)}%</b>\n\n"
        f"🚙 <b>Автомобиль:</b> {esc(seller.get('vehicle'))}\n"
        f"📍 <b>Город:</b> {esc(seller.get('city'))}\n"
        f"📅 <b>Год:</b> {esc(seller.get('vehicle_year'))}\n"
        f"💰 <b>Цена:</b> {esc(format_rub(seller.get('asking_price')))}\n"
        f"🛣 <b>Пробег:</b> {esc(seller.get('mileage_km'))} км\n\n"
        "Контакт продавца пока скрыт. Если предложение интересно, подтвердите — контакт откроется только после согласия обеих сторон."
    )
    seller_text = (
        f"🔗 <b>Найден покупатель · совпадение {int(match.get('match_score') or 0)}%</b>\n\n"
        f"🚗 <b>Ищет:</b> {esc(buyer.get('vehicle'))}\n"
        f"📍 <b>Город:</b> {esc(buyer.get('city'))}\n"
        f"📅 <b>Год:</b> {esc(buyer.get('min_vehicle_year'))}–{esc(buyer.get('max_vehicle_year'))}\n"
        f"🛣 <b>Пробег:</b> до {esc(buyer.get('max_mileage_km'))} км\n"
        f"💰 <b>Бюджет:</b> {esc(format_rub(buyer.get('budget')))}\n"
        f"📝 <b>Требования:</b> {esc(buyer.get('requirements') or 'без дополнительных требований')}\n\n"
        "Контакт покупателя пока скрыт. Если готовы продолжить, подтвердите — контакт откроется только после согласия обеих сторон."
    )

    buyer_sent_at = match.get("buyer_offer_sent_at")
    seller_sent_at = match.get("seller_offer_sent_at")

    if not buyer_sent_at:
        try:
            await bot.send_message(
                buyer_chat_id,
                buyer_text,
                parse_mode="HTML",
                reply_markup=offer_keyboard(match_id, "buyer"),
            )
            buyer_sent_at = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(
                lambda: supabase.table("auto_matches")
                .update({"buyer_offer_sent_at": buyer_sent_at})
                .eq("id", match_id)
                .execute()
            )
        except Exception:
            log.exception("Could not send match %s offer to buyer", match_id)

    if not seller_sent_at:
        try:
            await bot.send_message(
                seller_chat_id,
                seller_text,
                parse_mode="HTML",
                reply_markup=offer_keyboard(match_id, "seller"),
            )
            seller_sent_at = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(
                lambda: supabase.table("auto_matches")
                .update({"seller_offer_sent_at": seller_sent_at})
                .eq("id", match_id)
                .execute()
            )
        except Exception:
            log.exception("Could not send match %s offer to seller", match_id)

    if buyer_sent_at and seller_sent_at:
        (
            supabase.table("auto_matches")
            .update({"status": "offered", "offered_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", match_id)
            .eq("status", "approved")
            .execute()
        )


async def approved_match_worker() -> None:
    while True:
        try:
            approved = (
                supabase.table("auto_matches")
                .select("*")
                .eq("status", "approved")
                .order("created_at")
                .limit(20)
                .execute()
                .data
                or []
            )
            for match in approved:
                buyer = await asyncio.to_thread(get_request, int(match["buyer_request_id"]))
                seller = await asyncio.to_thread(get_request, int(match["seller_request_id"]))
                if buyer and seller:
                    await send_match_offer(match, buyer, seller)
        except Exception:
            log.exception("Approved match worker error")
        await asyncio.sleep(5)


async def finalize_if_both_accepted(match_id: int) -> bool:
    bundle = await asyncio.to_thread(get_match_bundle, match_id)
    if not bundle:
        return False
    match, buyer, seller = bundle
    if match.get("status") == "connected":
        return True
    if match.get("buyer_decision") != "accepted" or match.get("seller_decision") != "accepted":
        return False

    buyer_chat_id = int(buyer["telegram_user_id"])
    seller_chat_id = int(seller["telegram_user_id"])
    buyer_contact = esc(contact_text(buyer))
    seller_contact = esc(contact_text(seller))

    await bot.send_message(
        buyer_chat_id,
        "🤝 <b>Обе стороны подтвердили интерес.</b>\n\n"
        f"Контакт продавца: <b>{seller_contact}</b>\n"
        f"Автомобиль: {esc(seller.get('vehicle'))} · {esc(format_rub(seller.get('asking_price')))}",
        parse_mode="HTML",
    )
    await bot.send_message(
        seller_chat_id,
        "🤝 <b>Обе стороны подтвердили интерес.</b>\n\n"
        f"Контакт покупателя: <b>{buyer_contact}</b>\n"
        f"Запрос: {esc(buyer.get('vehicle'))} · бюджет {esc(format_rub(buyer.get('budget')))}",
        parse_mode="HTML",
    )
    (
        supabase.table("auto_matches")
        .update({"status": "connected", "connected_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", match_id)
        .neq("status", "connected")
        .execute()
    )
    return True



def save_buyer_as_lead(request_row: dict[str, Any], message: Message) -> dict[str, Any] | None:
    if request_row.get("id"):
        existing = find_existing_request_lead(int(request_row["id"]))
        if existing:
            link_request_to_lead(int(request_row["id"]), int(existing["id"]))
            return existing

    contact = request_row.get("contact_phone") or request_row.get("contact_telegram") or "не указан"
    budget = request_row.get("budget")
    requirements = request_row.get("requirements") or "без дополнительных требований"
    lead_text = (
        f"🟢 ПОКУПАТЕЛЬ | Куплю авто: {request_row.get('vehicle')}. "
        f"Город: {request_row.get('city')}. "
        f"Бюджет до {int(budget):,} руб. ".replace(",", " ")
        + f"Год {request_row.get('min_vehicle_year')}–{request_row.get('max_vehicle_year')}. "
        + f"Пробег до {request_row.get('max_mileage_km')} км. "
        + f"Требования: {requirements}. Контакт: {contact}"
    )
    payload = {
        "telegram_message_date": message.date.isoformat() if message.date else None,
        "chat_id": int(message.chat.id),
        "chat_name": "🟢 ПОКУПАТЕЛЬ | AutoClick Market",
        "message_id": int(message.message_id),
        "sender_id": int(message.from_user.id) if message.from_user else None,
        "username": message.from_user.username if message.from_user else None,
        "sender_name": message.from_user.full_name if message.from_user else None,
        "message_text": lead_text,
        "message_link": f"https://t.me/{PUBLIC_BOT_USERNAME}",
        "category": "auto",
        "city": request_row.get("city"),
        "budget": budget,
        "currency": "RUB",
        "status": "new",
        "source_id": None,
        "raw_data": {
            "ingestion": "public_telegram_bot",
            "market_request_id": request_row.get("id"),
            "request_type": "buyer",
            "min_vehicle_year": request_row.get("min_vehicle_year"),
            "max_vehicle_year": request_row.get("max_vehicle_year"),
            "max_mileage_km": request_row.get("max_mileage_km"),
            "contact_phone": request_row.get("contact_phone"),
            "contact_telegram": request_row.get("contact_telegram"),
        },
    }
    rows = supabase.table("leads").insert(payload).execute().data or []
    if not rows:
        return None
    lead = rows[0]
    if request_row.get("id") and lead.get("id"):
        link_request_to_lead(int(request_row["id"]), int(lead["id"]))
    return lead


def save_seller_as_lead(request_row: dict[str, Any]) -> dict[str, Any]:
    if request_row.get("id"):
        existing = find_existing_request_lead(int(request_row["id"]))
        if existing:
            link_request_to_lead(int(request_row["id"]), int(existing["id"]))
            return existing

    contact = request_row.get("contact_phone") or request_row.get("contact_telegram") or "не указан"
    price = request_row.get("asking_price")
    mileage = request_row.get("mileage_km")
    year = request_row.get("vehicle_year")

    price_text = f"{int(price):,}".replace(",", " ") if price is not None else "—"
    mileage_text = f"{int(mileage):,}".replace(",", " ") if mileage is not None else "—"
    lead_text = (
        f"🔵 ПРОДАВЕЦ | Продам авто: {request_row.get('vehicle')}. "
        f"Город: {request_row.get('city')}. "
        f"Год: {year or '—'}. "
        f"Цена: {price_text} руб. "
        f"Пробег: {mileage_text} км. "
        f"Контакт: {contact}"
    )

    telegram_user_id = int(request_row.get("telegram_user_id") or 0)
    telegram_message_id = int(request_row.get("telegram_message_id") or request_row.get("id") or 0)

    payload = {
        "chat_id": telegram_user_id,
        "chat_name": "🔵 ПРОДАВЕЦ | AutoClick Market",
        "message_id": telegram_message_id,
        "sender_id": telegram_user_id or None,
        "username": request_row.get("username"),
        "sender_name": request_row.get("full_name"),
        "message_text": lead_text,
        "message_link": f"https://t.me/{PUBLIC_BOT_USERNAME}",
        "category": "auto",
        "city": request_row.get("city"),
        "budget": price,
        "currency": "RUB",
        "status": "new",
        "source_id": None,
        "raw_data": {
            "ingestion": "public_telegram_bot",
            "market_request_id": request_row.get("id"),
            "request_type": "seller",
            "vehicle": request_row.get("vehicle"),
            "vehicle_year": year,
            "mileage_km": mileage,
            "asking_price": price,
            "contact_phone": request_row.get("contact_phone"),
            "contact_telegram": request_row.get("contact_telegram"),
        },
    }

    rows = supabase.table("leads").insert(payload).execute().data or []
    if not rows:
        raise RuntimeError("seller lead was not saved")

    lead = rows[0]
    lead_id = int(lead["id"])
    forced = {
        "lead_score": 70,
        "rule_score": 70,
        "lead_quality": "warm",
        "matched_keywords": ["продам авто"],
        "score_reasons": {
            "seller_request": True,
            "has_city": bool(request_row.get("city")),
            "has_price": price is not None,
            "specific_vehicle": bool(request_row.get("vehicle")),
        },
        "notification_status": "pending",
        "notification_claimed_at": None,
        "notification_error": None,
    }
    updated = supabase.table("leads").update(forced).eq("id", lead_id).execute().data or []
    if updated:
        lead = updated[0]

    if request_row.get("id"):
        link_request_to_lead(int(request_row["id"]), lead_id)
    return lead


def backfill_seller_radar_leads(limit: int = 50) -> None:
    rows = (
        supabase.table("market_requests")
        .select("*")
        .eq("request_type", "seller")
        .is_("radar_lead_id", "null")
        .order("created_at")
        .limit(limit)
        .execute()
        .data
        or []
    )
    for row in rows:
        try:
            request_id = int(row["id"])
            existing = find_existing_request_lead(request_id)
            if existing:
                link_request_to_lead(request_id, int(existing["id"]))
                log.info("Repaired seller request %s -> existing Radar lead %s", request_id, existing.get("id"))
                continue
            lead = save_seller_as_lead(row)
            log.info("Backfilled seller request %s to Radar lead %s", request_id, lead.get("id"))
        except Exception:
            log.exception("Could not backfill seller request %s", row.get("id"))


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🚘 <b>Куплю авто | Продам авто</b>\n\n"
        "Здесь можно бесплатно оставить заявку на покупку или продажу автомобиля.\n"
        "Телефон не публикуется в открытом доступе — он используется только для связи по заявке.\n\n"
        "Что вы хотите сделать?",
        parse_mode="HTML",
        reply_markup=MAIN_KB,
    )


@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено. Выберите, что хотите сделать:", reply_markup=MAIN_KB)


@dp.message(F.text == "🚗 Хочу купить авто")
async def buyer_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BuyerForm.city)
    await message.answer("В каком городе ищете автомобиль?", reply_markup=ReplyKeyboardRemove())


@dp.message(BuyerForm.city)
async def buyer_city(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Напишите город текстом.")
        return
    await state.update_data(city=text[:120])
    await state.set_state(BuyerForm.vehicle)
    await message.answer("Какой автомобиль ищете? Например: Toyota Camry, BMW X5 или «семейный кроссовер».")


@dp.message(BuyerForm.vehicle)
async def buyer_vehicle(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Напишите марку/модель или тип автомобиля.")
        return
    await state.update_data(vehicle=text[:200])
    await state.set_state(BuyerForm.budget)
    await message.answer("Какой бюджет? Например: 2,5 млн или 2 500 000 ₽.")


@dp.message(BuyerForm.budget)
async def buyer_budget(message: Message, state: FSMContext) -> None:
    value = parse_money(message.text or "")
    if value is None:
        await message.answer("Не понял бюджет. Напишите, например: 2,5 млн или 2 500 000.")
        return
    await state.update_data(budget=value)
    await state.set_state(BuyerForm.min_year)
    await message.answer("Минимальный год автомобиля? Например: 2020. Машины старше указанного года в совпадение не попадут.")


@dp.message(BuyerForm.min_year)
async def buyer_min_year(message: Message, state: FSMContext) -> None:
    year = parse_int(message.text or "")
    if year is None or year < 1950 or year > 2035:
        await message.answer("Напишите минимальный год четырьмя цифрами, например 2020.")
        return
    await state.update_data(min_vehicle_year=year)
    await state.set_state(BuyerForm.max_year)
    await message.answer("Максимальный год автомобиля? Например: 2026.")


@dp.message(BuyerForm.max_year)
async def buyer_max_year(message: Message, state: FSMContext) -> None:
    year = parse_int(message.text or "")
    data = await state.get_data()
    min_year = int(data.get("min_vehicle_year") or 0)
    if year is None or year < min_year or year > 2035:
        await message.answer(f"Максимальный год должен быть не меньше {min_year}. Например: 2026.")
        return
    await state.update_data(max_vehicle_year=year)
    await state.set_state(BuyerForm.max_mileage)
    await message.answer("Максимальный пробег, который рассматриваете? Например: 100000 км.")


@dp.message(BuyerForm.max_mileage)
async def buyer_max_mileage(message: Message, state: FSMContext) -> None:
    mileage = parse_int(message.text or "")
    if mileage is None or mileage < 0 or mileage > 2_000_000:
        await message.answer("Напишите максимальный пробег цифрами, например 100000.")
        return
    await state.update_data(max_mileage_km=mileage)
    await state.set_state(BuyerForm.requirements)
    await message.answer(
        "Есть дополнительные требования? Привод, цвет, комплектация и т.д.\nЕсли нет — нажмите «Пропустить».",
        reply_markup=SKIP_KB,
    )


@dp.message(BuyerForm.requirements)
async def buyer_requirements(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    requirements = None if text == "Пропустить" else text[:500]
    await state.update_data(requirements=requirements)
    await state.set_state(BuyerForm.contact)
    await message.answer(
        "Как с вами связаться?\nМожно отправить номер телефона или оставить ваш Telegram.",
        reply_markup=CONTACT_KB,
    )


@dp.message(BuyerForm.contact)
async def buyer_contact(message: Message, state: FSMContext) -> None:
    phone, telegram = contact_from_message(message)
    if not phone and not telegram:
        if message.from_user and message.from_user.username:
            telegram = f"@{message.from_user.username}"
        else:
            await message.answer(
                "Нужен контакт. Отправьте номер кнопкой или напишите телефон / @username.",
                reply_markup=CONTACT_KB,
            )
            return
    data = await state.get_data()
    ident = user_identity(message)
    payload = {
        "request_type": "buyer",
        "telegram_user_id": ident["telegram_user_id"],
        "telegram_message_id": message.message_id,
        "username": ident["username"],
        "full_name": ident["full_name"],
        "city": data["city"],
        "vehicle": data["vehicle"],
        "budget": data["budget"],
        "min_vehicle_year": data["min_vehicle_year"],
        "max_vehicle_year": data["max_vehicle_year"],
        "max_mileage_km": data["max_mileage_km"],
        "requirements": data.get("requirements"),
        "contact_phone": phone,
        "contact_telegram": telegram or ident["contact_telegram"],
        "status": "new",
        "source": "telegram_public_bot",
        "raw_data": {"bot_username": PUBLIC_BOT_USERNAME},
    }
    try:
        row = await asyncio.to_thread(save_market_request, payload)
    except Exception:
        log.exception("Failed to save buyer request")
        await message.answer("Не удалось сохранить заявку. Попробуйте ещё раз позже.", reply_markup=MAIN_KB)
        await state.clear()
        return
    if row.get("_is_duplicate"):
        await state.clear()
        await message.answer(
            f"ℹ️ Такая активная заявка уже есть — №{row['id']}. Вторую копию не создаю.",
            reply_markup=MAIN_KB,
        )
        return
    row.pop("_is_duplicate", None)
    try:
        await asyncio.to_thread(save_buyer_as_lead, row, message)
    except Exception:
        log.exception("Buyer request %s saved, but Radar linkage failed; it will be repaired later", row.get("id"))
    try:
        await asyncio.to_thread(create_matches_for_request, row)
    except Exception:
        log.exception("Could not match buyer request %s", row.get("id"))
    await state.clear()
    await message.answer(
        f"✅ Заявка #{row['id']} принята.\n\n"
        "Мы зафиксировали, какой автомобиль вы ищете. Когда появится подходящий вариант, с вами можно будет связаться по указанному контакту.",
        reply_markup=MAIN_KB,
    )


@dp.message(F.text == "💰 Хочу продать авто")
async def seller_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SellerForm.city)
    await message.answer("В каком городе находится автомобиль?", reply_markup=ReplyKeyboardRemove())


@dp.message(SellerForm.city)
async def seller_city(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Напишите город текстом.")
        return
    await state.update_data(city=text[:120])
    await state.set_state(SellerForm.vehicle)
    await message.answer("Марка и модель автомобиля? Например: Toyota Camry 2.5.")


@dp.message(SellerForm.vehicle)
async def seller_vehicle(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Напишите марку и модель.")
        return
    await state.update_data(vehicle=text[:200])
    await state.set_state(SellerForm.year)
    await message.answer("Какого года автомобиль?")


@dp.message(SellerForm.year)
async def seller_year(message: Message, state: FSMContext) -> None:
    year = parse_int(message.text or "")
    if year is None or year < 1950 or year > 2035:
        await message.answer("Напишите год четырьмя цифрами, например 2020.")
        return
    await state.update_data(vehicle_year=year)
    await state.set_state(SellerForm.price)
    await message.answer("За какую цену хотите продать? Например: 2,5 млн или 2 500 000 ₽.")


@dp.message(SellerForm.price)
async def seller_price(message: Message, state: FSMContext) -> None:
    value = parse_money(message.text or "")
    if value is None:
        await message.answer("Не понял цену. Напишите, например: 2,5 млн или 2 500 000.")
        return
    await state.update_data(asking_price=value)
    await state.set_state(SellerForm.mileage)
    await message.answer("Какой пробег автомобиля в километрах?")


@dp.message(SellerForm.mileage)
async def seller_mileage(message: Message, state: FSMContext) -> None:
    value = parse_int(message.text or "")
    if value is None or value > 2_000_000:
        await message.answer("Напишите пробег цифрами, например 85000.")
        return
    await state.update_data(mileage_km=value)
    await state.set_state(SellerForm.contact)
    await message.answer(
        "Как с вами связаться?\nМожно отправить номер телефона или оставить ваш Telegram.",
        reply_markup=CONTACT_KB,
    )


@dp.message(SellerForm.contact)
async def seller_contact(message: Message, state: FSMContext) -> None:
    phone, telegram = contact_from_message(message)
    if not phone and not telegram:
        if message.from_user and message.from_user.username:
            telegram = f"@{message.from_user.username}"
        else:
            await message.answer(
                "Нужен контакт. Отправьте номер кнопкой или напишите телефон / @username.",
                reply_markup=CONTACT_KB,
            )
            return
    data = await state.get_data()
    ident = user_identity(message)
    payload = {
        "request_type": "seller",
        "telegram_user_id": ident["telegram_user_id"],
        "telegram_message_id": message.message_id,
        "username": ident["username"],
        "full_name": ident["full_name"],
        "city": data["city"],
        "vehicle": data["vehicle"],
        "vehicle_year": data["vehicle_year"],
        "asking_price": data["asking_price"],
        "mileage_km": data["mileage_km"],
        "contact_phone": phone,
        "contact_telegram": telegram or ident["contact_telegram"],
        "status": "new",
        "source": "telegram_public_bot",
        "raw_data": {"bot_username": PUBLIC_BOT_USERNAME},
    }
    try:
        row = await asyncio.to_thread(save_market_request, payload)
    except Exception:
        log.exception("Failed to save seller request")
        await message.answer("Не удалось сохранить заявку. Попробуйте ещё раз позже.", reply_markup=MAIN_KB)
        await state.clear()
        return
    if row.get("_is_duplicate"):
        await state.clear()
        await message.answer(
            f"ℹ️ Такая активная заявка уже есть — №{row['id']}. Вторую копию не создаю.",
            reply_markup=MAIN_KB,
        )
        return
    row.pop("_is_duplicate", None)
    try:
        await asyncio.to_thread(save_seller_as_lead, row)
    except Exception:
        log.exception("Seller request %s saved, but Radar linkage failed; it will be repaired later", row.get("id"))
    try:
        await asyncio.to_thread(create_matches_for_request, row)
    except Exception:
        log.exception("Could not match seller request %s", row.get("id"))
    await state.clear()
    await message.answer(
        f"✅ Заявка #{row['id']} принята.\n\n"
        "Данные автомобиля сохранены. Мы сможем сопоставлять его с запросами покупателей.",
        reply_markup=MAIN_KB,
    )


@dp.callback_query(F.data.startswith("macc:"))
async def match_accept_callback(callback: CallbackQuery) -> None:
    try:
        _, match_id_raw, side = callback.data.split(":", 2)
        match_id = int(match_id_raw)
    except Exception:
        await callback.answer("Некорректная команда", show_alert=True)
        return

    bundle = await asyncio.to_thread(get_match_bundle, match_id)
    if not bundle:
        await callback.answer("Совпадение уже недоступно", show_alert=True)
        return
    match, buyer, seller = bundle
    if match.get("status") not in {"approved", "offered"}:
        await callback.answer("Это совпадение уже закрыто", show_alert=True)
        return
    request_row = buyer if side == "buyer" else seller
    if not callback.from_user or int(request_row["telegram_user_id"]) != int(callback.from_user.id):
        await callback.answer("Эта кнопка предназначена другой стороне", show_alert=True)
        return

    field = "buyer_decision" if side == "buyer" else "seller_decision"
    await asyncio.to_thread(
        lambda: supabase.table("auto_matches").update({field: "accepted"}).eq("id", match_id).execute()
    )
    await callback.answer("Интерес подтверждён")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("✅ Ваш интерес подтверждён. Ждём подтверждение второй стороны.")
    try:
        connected = await finalize_if_both_accepted(match_id)
        if connected and callback.message:
            await callback.message.answer("🤝 Совпадение подтверждено обеими сторонами — контакты отправлены.")
    except Exception:
        log.exception("Could not finalize match %s", match_id)


@dp.callback_query(F.data.startswith("mdec:"))
async def match_decline_callback(callback: CallbackQuery) -> None:
    try:
        _, match_id_raw, side = callback.data.split(":", 2)
        match_id = int(match_id_raw)
    except Exception:
        await callback.answer("Некорректная команда", show_alert=True)
        return

    bundle = await asyncio.to_thread(get_match_bundle, match_id)
    if not bundle:
        await callback.answer("Совпадение уже недоступно", show_alert=True)
        return
    match, buyer, seller = bundle
    if match.get("status") not in {"approved", "offered"}:
        await callback.answer("Это совпадение уже закрыто", show_alert=True)
        return
    request_row = buyer if side == "buyer" else seller
    if not callback.from_user or int(request_row["telegram_user_id"]) != int(callback.from_user.id):
        await callback.answer("Эта кнопка предназначена другой стороне", show_alert=True)
        return

    field = "buyer_decision" if side == "buyer" else "seller_decision"
    await asyncio.to_thread(
        lambda: supabase.table("auto_matches")
        .update({field: "declined", "status": "rejected"})
        .eq("id", match_id)
        .execute()
    )
    await callback.answer("Отказ сохранён")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Понятно. Это совпадение закрыто, заявка остаётся активной для других вариантов.")


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer("Выберите действие кнопкой ниже:", reply_markup=MAIN_KB)


async def main() -> None:
    log.info("Starting public auto bot @%s", PUBLIC_BOT_USERNAME)
    await asyncio.to_thread(backfill_seller_radar_leads)
    await asyncio.to_thread(backfill_auto_matches)
    worker = asyncio.create_task(approved_match_worker())
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
