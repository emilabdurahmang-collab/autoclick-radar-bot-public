import asyncio
import html
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Mapping

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
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
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


class SupabaseFSMStorage(BaseStorage):
    """Persistent aiogram FSM storage backed by Supabase.

    This keeps unfinished buyer/seller/edit forms across Railway restarts.
    """

    def __init__(self, client: Client) -> None:
        self.client = client

    @staticmethod
    def _storage_key(key: StorageKey) -> str:
        # Keep compatibility with different aiogram 3.x StorageKey versions.
        parts = [
            getattr(key, "bot_id", None),
            getattr(key, "chat_id", None),
            getattr(key, "user_id", None),
            getattr(key, "thread_id", None),
            getattr(key, "business_connection_id", None),
            getattr(key, "destiny", "default"),
        ]
        return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))

    def _set_state_sync(self, storage_key: str, state: str | None) -> None:
        self.client.table("telegram_fsm_storage").upsert(
            {
                "storage_key": storage_key,
                "state": state,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="storage_key",
        ).execute()

    def _get_state_sync(self, storage_key: str) -> str | None:
        rows = (
            self.client.table("telegram_fsm_storage")
            .select("state")
            .eq("storage_key", storage_key)
            .limit(1)
            .execute()
            .data
            or []
        )
        return str(rows[0]["state"]) if rows and rows[0].get("state") else None

    def _set_data_sync(self, storage_key: str, data: dict[str, Any]) -> None:
        self.client.table("telegram_fsm_storage").upsert(
            {
                "storage_key": storage_key,
                "data": data,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="storage_key",
        ).execute()

    def _get_data_sync(self, storage_key: str) -> dict[str, Any]:
        rows = (
            self.client.table("telegram_fsm_storage")
            .select("data")
            .eq("storage_key", storage_key)
            .limit(1)
            .execute()
            .data
            or []
        )
        value = rows[0].get("data") if rows else None
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                return dict(decoded) if isinstance(decoded, dict) else {}
            except Exception:
                return {}
        return {}

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await asyncio.to_thread(self._set_state_sync, self._storage_key(key), value)

    async def get_state(self, key: StorageKey) -> str | None:
        return await asyncio.to_thread(self._get_state_sync, self._storage_key(key))

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await asyncio.to_thread(self._set_data_sync, self._storage_key(key), dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_data_sync, self._storage_key(key))

    async def close(self) -> None:
        return None


dp = Dispatcher(storage=SupabaseFSMStorage(supabase))


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
    trim = State()
    description = State()
    photo = State()
    contact = State()


class EditRequestForm(StatesGroup):
    value = State()


MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚗 Хочу купить авто")],
        [KeyboardButton(text="💰 Хочу продать авто")],
        [KeyboardButton(text="📋 Мои заявки")],
    ],
    resize_keyboard=True,
)


START_TEXT = (
    "Привет! 👋\n"
    "Я <b>AutoClick</b> — помогаю людям покупать и продавать авто в Telegram.\n\n"
    "Выберите, что вас интересует:"
)

START_BANNER_PATH = os.path.join(os.path.dirname(__file__), "autoclick_start.jpg")

START_INLINE_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Купить авто", callback_data="start:buy")],
        [InlineKeyboardButton(text="🚘 Продать авто", callback_data="start:sell")],
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="start:my")],
    ]
)

HELP_TEXT = (
    "❓ <b>Помощь</b>\n"
    "Здесь основная информация о том, как работает AutoClick.\n\n"
    "🚙 <b>Как купить авто?</b>\n"
    "1. Нажмите «Купить авто».\n"
    "2. Заполните город, автомобиль, бюджет, год и пробег.\n"
    "3. Получайте подходящие предложения.\n"
    "4. Нажмите «Интересно», если вариант подходит.\n\n"
    "💰 <b>Как продать авто?</b>\n"
    "1. Нажмите «Продать авто».\n"
    "2. Заполните информацию об автомобиле.\n"
    "3. Добавьте до 5 фотографий.\n"
    "4. Получайте заинтересованных покупателей.\n\n"
    "📋 <b>Мои заявки</b>\n"
    "Здесь можно посмотреть заявки, изменить их, приостановить, возобновить или закрыть.\n\n"
    "🤝 <b>Как происходит контакт?</b>\n"
    "Контакты скрыты. Когда покупатель и продавец оба нажимают «Интересно», AutoClick открывает контактные данные обеим сторонам.\n\n"
    "⏸ Если поиск или продажа временно не нужны — поставьте заявку на паузу. После возобновления она снова участвует в подборе."
)

BOT_COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="buy", description="Купить авто"),
    BotCommand(command="sell", description="Продать авто"),
    BotCommand(command="my", description="Мои заявки"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="cancel", description="Отменить действие"),
]

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

PHOTO_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="✅ Готово")], [KeyboardButton(text="Пропустить")]],
    resize_keyboard=True,
)

MAX_SELLER_PHOTOS = 5
SELLER_ALBUM_WAIT_SECONDS = 0.9
SELLER_ALBUM_BUFFERS: dict[tuple[int, int, str], list[tuple[str, str]]] = {}
SELLER_ALBUM_TASKS: dict[tuple[int, int, str], asyncio.Task[Any]] = {}
SELLER_ALBUM_LOCK = asyncio.Lock()


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

def seller_photo_ids(row: dict[str, Any]) -> list[str]:
    value = row.get("photo_file_ids")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = []
    photos = [str(x) for x in (value or []) if x]
    if not photos and row.get("photo_file_id"):
        photos = [str(row["photo_file_id"])]
    return photos[:MAX_SELLER_PHOTOS]


def seller_photo_count(row: dict[str, Any]) -> int:
    return len(seller_photo_ids(row))


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
        .in_("status", ["new", "in_progress", "paused"])
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
        .select("*")
        .eq("buyer_request_id", buyer_id)
        .eq("seller_request_id", seller_id)
        .limit(1)
        .execute()
        .data
        or []
    )

    if existing and existing[0].get("status") != "expired":
        return existing[0]

    evaluated = evaluate_match(buyer, seller)
    if not evaluated:
        return None
    score, reasons = evaluated

    # If this pair existed before but became stale because a request was edited,
    # reuse the same row instead of creating a duplicate pair.
    if existing:
        match_id = int(existing[0]["id"])
        rows = (
            supabase.table("auto_matches")
            .update({
                "match_score": score,
                "match_reasons": reasons,
                "status": "new",
                "buyer_decision": "pending",
                "seller_decision": "pending",
                "notification_status": "pending",
                "notification_claimed_at": None,
                "notified_at": None,
                "notification_error": None,
                "offered_at": None,
                "buyer_offer_sent_at": None,
                "seller_offer_sent_at": None,
                "connected_at": None,
            })
            .eq("id", match_id)
            .eq("status", "expired")
            .execute()
            .data
            or []
        )
        if rows:
            log.info("Auto match %s refreshed after request edit: buyer=%s seller=%s score=%s", match_id, buyer_id, seller_id, score)
            return rows[0]
        return None

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


def format_number(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        return f"{int(float(value)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


MATCHING_REQUEST_STATUSES = {"new", "in_progress"}
MANAGEABLE_REQUEST_STATUSES = {"new", "in_progress", "paused"}


def get_user_requests(telegram_user_id: int, limit: int = 12) -> list[dict[str, Any]]:
    return (
        supabase.table("market_requests")
        .select("*")
        .eq("telegram_user_id", telegram_user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )


def request_status_text(row: dict[str, Any]) -> str:
    reason = row.get("close_reason")
    if reason == "bought":
        return "✅ Купил автомобиль"
    if reason == "sold":
        return "✅ Автомобиль продан"
    if reason == "cancelled":
        return "⛔ Закрыта"
    return {
        "new": "🟢 Активна",
        "in_progress": "🟡 В работе",
        "paused": "⏸ На паузе",
        "done": "✅ Закрыта",
        "rejected": "⛔ Закрыта",
    }.get(str(row.get("status") or ""), str(row.get("status") or "—"))


def request_card(row: dict[str, Any]) -> str:
    request_id = int(row["id"])
    is_buyer = row.get("request_type") == "buyer"
    body = [
        f"<b>{'🟢 ПОКУПКА' if is_buyer else '🔵 ПРОДАЖА'} · заявка #{request_id}</b>",
        f"Статус: {request_status_text(row)}",
        "",
        f"🚙 Авто: {esc(row.get('vehicle'))}",
        f"📍 Город: {esc(row.get('city'))}",
    ]
    if is_buyer:
        body.extend([
            f"💰 Бюджет: {esc(format_rub(row.get('budget')))}",
            f"📅 Год: {esc(row.get('min_vehicle_year'))}–{esc(row.get('max_vehicle_year'))}",
            f"🛣 Максимальный пробег: {esc(format_number(row.get('max_mileage_km')))} км",
            f"📝 Требования: {esc(row.get('requirements') or 'нет')}",
        ])
    else:
        body.extend([
            f"📅 Год: {esc(row.get('vehicle_year'))}",
            f"💰 Цена: {esc(format_rub(row.get('asking_price')))}",
            f"🛣 Пробег: {esc(format_number(row.get('mileage_km')))} км",
            f"⚙️ Комплектация: {esc(row.get('vehicle_trim') or 'не указана')}",
        ])
        if row.get("seller_description"):
            body.append(f"📝 Описание: {esc(row.get('seller_description'))}")
        body.append(f"📷 Фото: {seller_photo_count(row) if seller_photo_count(row) else 'нет'}")
    return "\n".join(body)


def request_actions_keyboard(row: dict[str, Any]) -> InlineKeyboardMarkup | None:
    status = str(row.get("status") or "")
    if status not in MANAGEABLE_REQUEST_STATUSES:
        return None
    request_id = int(row["id"])
    success_reason = "bought" if row.get("request_type") == "buyer" else "sold"
    success_text = "✅ Купил авто" if success_reason == "bought" else "✅ Продано"
    pause_button = (
        InlineKeyboardButton(text="▶️ Возобновить", callback_data=f"reqstate:{request_id}:resume")
        if status == "paused"
        else InlineKeyboardButton(text="⏸ Приостановить", callback_data=f"reqstate:{request_id}:pause")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [pause_button],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"reqedit:{request_id}")],
        [InlineKeyboardButton(text=success_text, callback_data=f"reqclose:{request_id}:{success_reason}")],
        [InlineKeyboardButton(text="🛑 Закрыть заявку", callback_data=f"reqclose:{request_id}:cancelled")],
    ])


def edit_fields_keyboard(row: dict[str, Any]) -> InlineKeyboardMarkup:
    request_id = int(row["id"])
    if row.get("request_type") == "buyer":
        fields = [
            ("📍 Город", "city"), ("🚙 Авто", "vehicle"),
            ("💰 Бюджет", "budget"), ("📅 Мин. год", "min_vehicle_year"),
            ("📅 Макс. год", "max_vehicle_year"), ("🛣 Пробег до", "max_mileage_km"),
            ("📝 Требования", "requirements"), ("☎️ Контакт", "contact"),
        ]
    else:
        fields = [
            ("📍 Город", "city"), ("🚙 Авто", "vehicle"),
            ("📅 Год", "vehicle_year"), ("💰 Цена", "asking_price"),
            ("🛣 Пробег", "mileage_km"), ("⚙️ Комплектация", "vehicle_trim"),
            ("📝 Описание", "seller_description"), ("📷 Фото", "photo_file_id"),
            ("☎️ Контакт", "contact"),
        ]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(fields), 2):
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"reqfield:{request_id}:{field}")
            for label, field in fields[i:i + 2]
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к заявке", callback_data=f"reqshow:{request_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_field_prompt(field: str) -> str:
    prompts = {
        "city": "Введите новый город.",
        "vehicle": "Введите новую марку и модель автомобиля.",
        "budget": "Введите новый бюджет, например 2,5 млн или 2 500 000 ₽.",
        "min_vehicle_year": "Введите новый минимальный год, например 2020.",
        "max_vehicle_year": "Введите новый максимальный год, например 2026.",
        "max_mileage_km": "Введите новый максимальный пробег, например 100000.",
        "requirements": "Введите новые требования. Если требований нет — напишите «Пропустить».",
        "vehicle_year": "Введите новый год автомобиля, например 2020.",
        "asking_price": "Введите новую цену, например 2,5 млн или 2 500 000 ₽.",
        "mileage_km": "Введите новый пробег автомобиля, например 85000.",
        "vehicle_trim": "Введите комплектацию автомобиля, например M Sport, Elegance или Comfort. Если не хотите указывать — напишите «Пропустить».",
        "seller_description": "Введите краткое описание автомобиля: состояние, владельцы, ДТП, обслуживание и важные особенности. До 1000 символов. Если не хотите указывать — напишите «Пропустить».",
        "photo_file_id": "Отправьте новое главное фото автомобиля. Оно заменит текущую фотогалерею. Чтобы удалить все фото — напишите «Удалить».",
        "contact": "Отправьте новый телефон или @username.",
    }
    return prompts.get(field, "Введите новое значение.")


def expire_matches_for_request(request_id: int) -> None:
    (
        supabase.table("auto_matches")
        .update({"status": "expired"})
        .in_("status", ["new", "approved", "offered"])
        .or_(f"buyer_request_id.eq.{request_id},seller_request_id.eq.{request_id}")
        .execute()
    )


def sync_request_lead(row: dict[str, Any]) -> None:
    lead_id = row.get("radar_lead_id")
    if not lead_id:
        return
    if row.get("request_type") == "buyer":
        text = (
            f"🟢 ПОКУПАТЕЛЬ | Куплю авто: {row.get('vehicle')}. "
            f"Город: {row.get('city')}. Бюджет до {format_rub(row.get('budget'))}. "
            f"Год: {row.get('min_vehicle_year')}–{row.get('max_vehicle_year')}. "
            f"Пробег до {row.get('max_mileage_km')} км. "
            f"Требования: {row.get('requirements') or 'без дополнительных требований'}. "
            f"Контакт: {contact_text(row)}"
        )
        updates = {"city": row.get("city"), "budget": row.get("budget"), "message_text": text}
    else:
        text = (
            f"🔵 ПРОДАВЕЦ | Продам авто: {row.get('vehicle')}. "
            f"Город: {row.get('city')}. Год: {row.get('vehicle_year')}. "
            f"Цена: {format_rub(row.get('asking_price'))}. "
            f"Пробег: {row.get('mileage_km')} км. "
            f"Комплектация: {row.get('vehicle_trim') or 'не указана'}. "
            f"Описание: {row.get('seller_description') or 'нет'}. "
            f"Фото: {seller_photo_count(row)} шт. "
            f"Контакт: {contact_text(row)}"
        )
        updates = {"city": row.get("city"), "budget": row.get("asking_price"), "message_text": text}
    supabase.table("leads").update(updates).eq("id", int(lead_id)).execute()


def update_request_owned(request_id: int, telegram_user_id: int, updates: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    row = get_request(request_id)
    if not row or int(row.get("telegram_user_id") or 0) != telegram_user_id:
        return "not_found", None
    if row.get("status") not in MANAGEABLE_REQUEST_STATUSES:
        return "closed", row

    merged = dict(row)
    merged.update(updates)
    fingerprint = request_fingerprint(merged)
    duplicate = (
        supabase.table("market_requests")
        .select("id")
        .eq("telegram_user_id", telegram_user_id)
        .eq("request_type", row.get("request_type"))
        .eq("request_fingerprint", fingerprint)
        .in_("status", ["new", "in_progress", "paused"])
        .neq("id", request_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if duplicate:
        return "duplicate", {"id": duplicate[0]["id"]}

    payload = dict(updates)
    payload["request_fingerprint"] = fingerprint
    rows = (
        supabase.table("market_requests")
        .update(payload)
        .eq("id", request_id)
        .eq("telegram_user_id", telegram_user_id)
        .in_("status", ["new", "in_progress", "paused"])
        .execute()
        .data
        or []
    )
    if not rows:
        return "not_found", None
    updated = rows[0]
    sync_request_lead(updated)

    # Re-run matching only when a field that actually affects matching changed.
    # Cosmetic/contact edits must not expire existing matches or create duplicates.
    matching_fields = (
        {"city", "vehicle", "budget", "min_vehicle_year", "max_vehicle_year", "max_mileage_km"}
        if row.get("request_type") == "buyer"
        else {"city", "vehicle", "vehicle_year", "asking_price", "mileage_km"}
    )
    matching_changed = any(
        field in updates and updates.get(field) != row.get(field)
        for field in matching_fields
    )
    if updated.get("status") in MATCHING_REQUEST_STATUSES and matching_changed:
        expire_matches_for_request(request_id)
        create_matches_for_request(updated)
    return "ok", updated


def close_request_owned(request_id: int, telegram_user_id: int, reason: str) -> tuple[str, dict[str, Any] | None]:
    row = get_request(request_id)
    if not row or int(row.get("telegram_user_id") or 0) != telegram_user_id:
        return "not_found", None
    if row.get("status") not in MANAGEABLE_REQUEST_STATUSES:
        return "closed", row
    if reason == "bought" and row.get("request_type") != "buyer":
        return "not_found", None
    if reason == "sold" and row.get("request_type") != "seller":
        return "not_found", None
    if reason not in {"bought", "sold", "cancelled"}:
        return "not_found", None

    status = "done" if reason in {"bought", "sold"} else "rejected"
    now = datetime.now(timezone.utc).isoformat()
    rows = (
        supabase.table("market_requests")
        .update({"status": status, "close_reason": reason, "closed_at": now})
        .eq("id", request_id)
        .eq("telegram_user_id", telegram_user_id)
        .in_("status", ["new", "in_progress", "paused"])
        .execute()
        .data
        or []
    )
    if not rows:
        return "closed", row
    updated = rows[0]
    expire_matches_for_request(request_id)
    if updated.get("radar_lead_id"):
        lead_status = "done" if status == "done" else "rejected"
        supabase.table("leads").update({"status": lead_status}).eq("id", int(updated["radar_lead_id"])).execute()
    return "ok", updated



def set_request_paused_owned(request_id: int, telegram_user_id: int, pause: bool) -> tuple[str, dict[str, Any] | None]:
    row = get_request(request_id)
    if not row or int(row.get("telegram_user_id") or 0) != telegram_user_id:
        return "not_found", None

    current = str(row.get("status") or "")
    if pause:
        if current == "paused":
            return "already", row
        if current not in MATCHING_REQUEST_STATUSES:
            return "closed", row
        rows = (
            supabase.table("market_requests")
            .update({"status": "paused"})
            .eq("id", request_id)
            .eq("telegram_user_id", telegram_user_id)
            .in_("status", ["new", "in_progress"])
            .execute()
            .data
            or []
        )
        if not rows:
            return "not_found", None
        updated = rows[0]
        expire_matches_for_request(request_id)
        return "ok", updated

    if current in MATCHING_REQUEST_STATUSES:
        return "already", row
    if current != "paused":
        return "closed", row
    rows = (
        supabase.table("market_requests")
        .update({"status": "new"})
        .eq("id", request_id)
        .eq("telegram_user_id", telegram_user_id)
        .eq("status", "paused")
        .execute()
        .data
        or []
    )
    if not rows:
        return "not_found", None
    updated = rows[0]
    create_matches_for_request(updated)
    return "ok", updated

def offer_keyboard(match_id: int, side: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Интересно", callback_data=f"macc:{match_id}:{side}"),
        InlineKeyboardButton(text="❌ Не подходит", callback_data=f"mdec:{match_id}:{side}"),
    ]])


async def send_match_offer(match: dict[str, Any], buyer: dict[str, Any], seller: dict[str, Any]) -> None:
    match_id = int(match["id"])
    buyer_chat_id = int(buyer["telegram_user_id"])
    seller_chat_id = int(seller["telegram_user_id"])

    description = str(seller.get("seller_description") or "").strip()
    description_line = f"📝 {esc(description[:300])}\n" if description else ""
    buyer_text = (
        f"🚘 <b>{esc(seller.get('vehicle'))} · {esc(seller.get('vehicle_year'))}</b>\n"
        f"💰 <b>{esc(format_rub(seller.get('asking_price')))}</b>\n\n"
        f"🎯 Совпадение с вашим запросом — <b>{int(match.get('match_score') or 0)}%</b>\n"
        f"📍 {esc(seller.get('city'))}\n"
        f"🛣 {esc(format_number(seller.get('mileage_km')))} км\n"
        f"⚙️ {esc(seller.get('vehicle_trim') or 'Комплектация не указана')}\n"
        f"{description_line}\n"
        "🔒 Контакт продавца скрыт. Нажмите «Интересно» — контакт откроется только после взаимного подтверждения."
    )
    seller_text = (
        f"👤 <b>Найден покупатель на {esc(buyer.get('vehicle'))}</b>\n\n"
        f"🎯 Совпадение — <b>{int(match.get('match_score') or 0)}%</b>\n"
        f"📍 {esc(buyer.get('city'))}\n"
        f"📅 Год: {esc(buyer.get('min_vehicle_year'))}–{esc(buyer.get('max_vehicle_year'))}\n"
        f"🛣 Пробег: до {esc(format_number(buyer.get('max_mileage_km')))} км\n"
        f"💰 Бюджет: <b>{esc(format_rub(buyer.get('budget')))}</b>\n"
        f"📝 {esc(buyer.get('requirements') or 'Без дополнительных требований')}\n\n"
        "🔒 Контакт покупателя скрыт. Нажмите «Интересно» — контакт откроется только после взаимного подтверждения."
    )

    buyer_sent_at = match.get("buyer_offer_sent_at")
    seller_sent_at = match.get("seller_offer_sent_at")

    if not buyer_sent_at:
        try:
            photos = seller_photo_ids(seller)
            if len(photos) == 1:
                await bot.send_photo(
                    buyer_chat_id,
                    photo=photos[0],
                    caption=buyer_text,
                    parse_mode="HTML",
                    reply_markup=offer_keyboard(match_id, "buyer"),
                )
            elif len(photos) > 1:
                await bot.send_media_group(
                    buyer_chat_id,
                    media=[InputMediaPhoto(media=photo_id) for photo_id in photos],
                )
                await bot.send_message(
                    buyer_chat_id,
                    buyer_text,
                    parse_mode="HTML",
                    reply_markup=offer_keyboard(match_id, "buyer"),
                )
            else:
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
        f"Комплектация: {request_row.get('vehicle_trim') or 'не указана'}. "
        f"Описание: {request_row.get('seller_description') or 'нет'}. "
        f"Фото: {seller_photo_count(request_row)} шт. "
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
            "vehicle_trim": request_row.get("vehicle_trim"),
            "seller_description": request_row.get("seller_description"),
            "has_photo": bool(seller_photo_count(request_row)),
            "photo_count": seller_photo_count(request_row),
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
    if os.path.exists(START_BANNER_PATH):
        await message.answer_photo(
            FSInputFile(START_BANNER_PATH),
            caption=START_TEXT,
            parse_mode="HTML",
            reply_markup=START_INLINE_KB,
        )
    else:
        await message.answer(START_TEXT, parse_mode="HTML", reply_markup=START_INLINE_KB)


@dp.callback_query(F.data == "start:buy")
async def start_buy_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BuyerForm.city)
    await callback.answer()
    if callback.message:
        await callback.message.answer("В каком городе ищете автомобиль?", reply_markup=ReplyKeyboardRemove())


@dp.callback_query(F.data == "start:sell")
async def start_sell_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SellerForm.city)
    await callback.answer()
    if callback.message:
        await callback.message.answer("В каком городе находится автомобиль?", reply_markup=ReplyKeyboardRemove())


@dp.callback_query(F.data == "start:my")
async def start_my_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not callback.from_user:
        return
    await callback.answer()
    rows = await asyncio.to_thread(get_user_requests, int(callback.from_user.id))
    if not callback.message:
        return
    if not rows:
        await callback.message.answer("У вас пока нет заявок.", reply_markup=MAIN_KB)
        return
    await callback.message.answer("📋 <b>Ваши последние заявки</b>", parse_mode="HTML", reply_markup=MAIN_KB)
    for row in rows:
        await callback.message.answer(
            request_card(row),
            parse_mode="HTML",
            reply_markup=request_actions_keyboard(row),
        )


@dp.message(Command("help"))
async def help_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=START_INLINE_KB)


@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Действие отменено. Выберите следующий шаг:", reply_markup=MAIN_KB)


@dp.message(Command("sell"))
async def sell_command_handler(message: Message, state: FSMContext) -> None:
    # Commands must work even if the user is in the middle of another form.
    await state.clear()
    await state.set_state(SellerForm.city)
    await message.answer("В каком городе находится автомобиль?", reply_markup=ReplyKeyboardRemove())


@dp.message(Command("my"))
async def my_command_handler(message: Message, state: FSMContext) -> None:
    # /my should always interrupt the current form and open saved requests.
    await state.clear()
    if not message.from_user:
        return
    rows = await asyncio.to_thread(get_user_requests, int(message.from_user.id))
    if not rows:
        await message.answer("У вас пока нет заявок.", reply_markup=MAIN_KB)
        return
    await message.answer("📋 <b>Ваши последние заявки</b>", parse_mode="HTML", reply_markup=MAIN_KB)
    for row in rows:
        await message.answer(
            request_card(row),
            parse_mode="HTML",
            reply_markup=request_actions_keyboard(row),
        )


@dp.message(Command("buy"))
@dp.message(F.text == "🚗 Хочу купить авто")
async def buyer_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BuyerForm.city)
    await message.answer("В каком городе ищете автомобиль?", reply_markup=ReplyKeyboardRemove())


@dp.message(BuyerForm.city)
async def buyer_city(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Укажите город, например: Симферополь.")
        return
    await state.update_data(city=text[:120])
    await state.set_state(BuyerForm.vehicle)
    await message.answer("Какой автомобиль ищете? Например: Toyota Camry, BMW X5 или «семейный кроссовер».")


@dp.message(BuyerForm.vehicle)
async def buyer_vehicle(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Укажите автомобиль, например: Toyota Camry.")
        return
    await state.update_data(vehicle=text[:200])
    await state.set_state(BuyerForm.budget)
    await message.answer("Какой бюджет? Например: 2,5 млн или 2 500 000 ₽.")


@dp.message(BuyerForm.budget)
async def buyer_budget(message: Message, state: FSMContext) -> None:
    value = parse_money(message.text or "")
    if value is None:
        await message.answer("Укажите бюджет, например: 2,5 млн или 2 500 000 ₽.")
        return
    await state.update_data(budget=value)
    await state.set_state(BuyerForm.min_year)
    await message.answer("Минимальный год автомобиля? Например: 2020. Машины старше указанного года в совпадение не попадут.")


@dp.message(BuyerForm.min_year)
async def buyer_min_year(message: Message, state: FSMContext) -> None:
    year = parse_int(message.text or "")
    if year is None or year < 1950 or year > 2035:
        await message.answer("Укажите год четырьмя цифрами, например: 2020.")
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
        await message.answer(f"Максимальный год должен быть не меньше {min_year}.")
        return
    await state.update_data(max_vehicle_year=year)
    await state.set_state(BuyerForm.max_mileage)
    await message.answer("Максимальный пробег, который рассматриваете? Например: 100000 км.")


@dp.message(BuyerForm.max_mileage)
async def buyer_max_mileage(message: Message, state: FSMContext) -> None:
    mileage = parse_int(message.text or "")
    if mileage is None or mileage < 0 or mileage > 2_000_000:
        await message.answer("Укажите пробег цифрами, например: 100 000.")
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
                "Укажите контакт: отправьте номер кнопкой или напишите @username.",
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
        await message.answer("⚠️ Не удалось сохранить заявку. Попробуйте ещё раз через минуту.", reply_markup=MAIN_KB)
        await state.clear()
        return
    if row.get("_is_duplicate"):
        await state.clear()
        await message.answer(
            f"ℹ️ Такая активная заявка уже есть: №{row['id']}.",
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


@dp.message(F.text == "📋 Мои заявки")
async def my_requests_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not message.from_user:
        return
    rows = await asyncio.to_thread(get_user_requests, int(message.from_user.id))
    if not rows:
        await message.answer("У вас пока нет заявок.", reply_markup=MAIN_KB)
        return
    await message.answer("📋 <b>Ваши последние заявки</b>", parse_mode="HTML", reply_markup=MAIN_KB)
    for row in rows:
        await message.answer(
            request_card(row),
            parse_mode="HTML",
            reply_markup=request_actions_keyboard(row),
        )


@dp.callback_query(F.data.startswith("reqshow:"))
async def request_show_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not callback.from_user:
        return
    try:
        request_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Эта кнопка уже неактуальна", show_alert=True)
        return
    row = await asyncio.to_thread(get_request, request_id)
    if not row or int(row.get("telegram_user_id") or 0) != int(callback.from_user.id):
        await callback.answer("Заявка не найдена. Откройте «Мои заявки»", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            request_card(row),
            parse_mode="HTML",
            reply_markup=request_actions_keyboard(row),
        )


@dp.callback_query(F.data.startswith("reqedit:"))
async def request_edit_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user:
        return
    try:
        request_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Эта кнопка уже неактуальна", show_alert=True)
        return
    row = await asyncio.to_thread(get_request, request_id)
    if not row or int(row.get("telegram_user_id") or 0) != int(callback.from_user.id):
        await callback.answer("Заявка не найдена. Откройте «Мои заявки»", show_alert=True)
        return
    if row.get("status") not in MANAGEABLE_REQUEST_STATUSES:
        await callback.answer("Заявка уже закрыта", show_alert=True)
        return
    await state.clear()
    await callback.answer("Выберите поле")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=edit_fields_keyboard(row))


@dp.callback_query(F.data.startswith("reqfield:"))
async def request_field_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user:
        return
    try:
        _, request_id_raw, field = callback.data.split(":", 2)
        request_id = int(request_id_raw)
    except Exception:
        await callback.answer("Эта кнопка уже неактуальна", show_alert=True)
        return
    row = await asyncio.to_thread(get_request, request_id)
    if not row or int(row.get("telegram_user_id") or 0) != int(callback.from_user.id):
        await callback.answer("Заявка не найдена. Откройте «Мои заявки»", show_alert=True)
        return
    if row.get("status") not in MANAGEABLE_REQUEST_STATUSES:
        await callback.answer("Заявка уже закрыта", show_alert=True)
        return

    buyer_fields = {"city", "vehicle", "budget", "min_vehicle_year", "max_vehicle_year", "max_mileage_km", "requirements", "contact"}
    seller_fields = {"city", "vehicle", "vehicle_year", "asking_price", "mileage_km", "vehicle_trim", "seller_description", "photo_file_id", "contact"}
    allowed = buyer_fields if row.get("request_type") == "buyer" else seller_fields
    if field not in allowed:
        await callback.answer("Это поле нельзя изменить", show_alert=True)
        return

    await state.set_state(EditRequestForm.value)
    await state.update_data(edit_request_id=request_id, edit_field=field)
    await callback.answer()
    if callback.message:
        if field == "contact":
            reply_markup = CONTACT_KB
        elif field in {"vehicle_trim", "seller_description"}:
            reply_markup = SKIP_KB
        else:
            reply_markup = ReplyKeyboardRemove()
        await callback.message.answer(edit_field_prompt(field), reply_markup=reply_markup)


@dp.message(EditRequestForm.value)
async def edit_request_value_handler(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    data = await state.get_data()
    request_id = int(data.get("edit_request_id") or 0)
    field = str(data.get("edit_field") or "")
    row = await asyncio.to_thread(get_request, request_id)
    if not row or int(row.get("telegram_user_id") or 0) != int(message.from_user.id):
        await state.clear()
        await message.answer("Заявка не найдена. Откройте «Мои заявки».", reply_markup=MAIN_KB)
        return

    text = (message.text or "").strip()
    updates: dict[str, Any] = {}
    if field in {"city", "vehicle"}:
        if len(text) < 2:
            await message.answer("Введите значение полностью.")
            return
        updates[field] = text[:200 if field == "vehicle" else 120]
    elif field in {"budget", "asking_price"}:
        value = parse_money(text)
        if value is None:
            await message.answer("Укажите сумму, например: 2,5 млн или 2 500 000 ₽.")
            return
        updates[field] = value
    elif field in {"min_vehicle_year", "max_vehicle_year", "vehicle_year"}:
        value = parse_int(text)
        if value is None or value < 1950 or value > 2035:
            await message.answer("Укажите год четырьмя цифрами, например: 2020.")
            return
        if field == "min_vehicle_year" and row.get("max_vehicle_year") is not None and value > int(row["max_vehicle_year"]):
            await message.answer(f"Минимальный год не может быть больше {row['max_vehicle_year']}.")
            return
        if field == "max_vehicle_year" and row.get("min_vehicle_year") is not None and value < int(row["min_vehicle_year"]):
            await message.answer(f"Максимальный год не может быть меньше {row['min_vehicle_year']}.")
            return
        updates[field] = value
    elif field in {"max_mileage_km", "mileage_km"}:
        value = parse_int(text)
        if value is None or value < 0 or value > 2_000_000:
            await message.answer("Укажите пробег цифрами, например: 100 000.")
            return
        updates[field] = value
    elif field == "requirements":
        updates[field] = None if text.lower() == "пропустить" else text[:500]
    elif field == "vehicle_trim":
        updates[field] = None if text.lower() == "пропустить" else text[:200]
    elif field == "seller_description":
        updates[field] = None if text.lower() == "пропустить" else text[:1000]
    elif field == "photo_file_id":
        if message.photo:
            photo = message.photo[-1]
            updates["photo_file_id"] = photo.file_id
            updates["photo_unique_id"] = photo.file_unique_id
            updates["photo_file_ids"] = [photo.file_id]
            updates["photo_unique_ids"] = [photo.file_unique_id]
        elif text.lower() in {"удалить", "удалить фото", "пропустить"}:
            updates["photo_file_id"] = None
            updates["photo_unique_id"] = None
            updates["photo_file_ids"] = []
            updates["photo_unique_ids"] = []
        else:
            await message.answer("Отправьте фото автомобиля или напишите «Удалить».")
            return
    elif field == "contact":
        phone, telegram = contact_from_message(message)
        if not phone and not telegram:
            await message.answer("Отправьте номер телефона или напишите @username.", reply_markup=CONTACT_KB)
            return
        if phone:
            updates["contact_phone"] = phone
            updates["contact_telegram"] = None
        if telegram:
            updates["contact_telegram"] = telegram
            updates["contact_phone"] = None
    else:
        await state.clear()
        await message.answer("Не удалось открыть редактирование. Попробуйте ещё раз.", reply_markup=MAIN_KB)
        return

    try:
        result, updated = await asyncio.to_thread(update_request_owned, request_id, int(message.from_user.id), updates)
    except Exception:
        log.exception("Failed to edit market request %s", request_id)
        await message.answer("⚠️ Не удалось изменить заявку. Попробуйте ещё раз через минуту.", reply_markup=MAIN_KB)
        await state.clear()
        return

    if result == "duplicate":
        await message.answer(
            f"ℹ️ После этого изменения заявка станет копией активной заявки №{updated['id']}. Изменение не сохранено.",
            reply_markup=MAIN_KB,
        )
        await state.clear()
        return
    if result != "ok" or not updated:
        await message.answer("Эта заявка уже закрыта.", reply_markup=MAIN_KB)
        await state.clear()
        return

    await state.clear()
    buyer_matching_fields = {"city", "vehicle", "budget", "min_vehicle_year", "max_vehicle_year", "max_mileage_km"}
    seller_matching_fields = {"city", "vehicle", "vehicle_year", "asking_price", "mileage_km"}
    matching_fields = buyer_matching_fields if row.get("request_type") == "buyer" else seller_matching_fields
    if field in matching_fields and row.get("status") in MATCHING_REQUEST_STATUSES:
        success_text = "✅ Заявка обновлена. Совпадения пересчитаны."
    else:
        success_text = "✅ Заявка обновлена. Текущие совпадения сохранены."
    await message.answer(success_text, reply_markup=MAIN_KB)
    await message.answer(request_card(updated), parse_mode="HTML", reply_markup=request_actions_keyboard(updated))



@dp.callback_query(F.data.startswith("reqstate:"))
async def request_state_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user:
        return
    try:
        _, request_id_raw, action = callback.data.split(":", 2)
        request_id = int(request_id_raw)
    except Exception:
        await callback.answer("Эта кнопка уже неактуальна", show_alert=True)
        return
    if action not in {"pause", "resume"}:
        await callback.answer("Эта кнопка уже неактуальна", show_alert=True)
        return
    try:
        result, row = await asyncio.to_thread(
            set_request_paused_owned, request_id, int(callback.from_user.id), action == "pause"
        )
    except Exception:
        log.exception("Failed to change request state %s", request_id)
        await callback.answer("Не удалось изменить статус. Попробуйте ещё раз", show_alert=True)
        return
    if result == "not_found":
        await callback.answer("Заявка не найдена. Откройте «Мои заявки»", show_alert=True)
        return
    if result == "closed":
        await callback.answer("Заявка уже закрыта", show_alert=True)
        return
    if result == "already":
        await callback.answer("Статус уже установлен", show_alert=True)
        return

    await state.clear()
    await callback.answer("Готово")
    if callback.message and row:
        await callback.message.edit_text(
            request_card(row),
            parse_mode="HTML",
            reply_markup=request_actions_keyboard(row),
        )
        if action == "pause":
            await callback.message.answer(
                "⏸ Заявка приостановлена. Она временно не участвует в подборе.",
                reply_markup=MAIN_KB,
            )
        else:
            await callback.message.answer(
                "▶️ Заявка снова активна и участвует в подборе.",
                reply_markup=MAIN_KB,
            )

@dp.callback_query(F.data.startswith("reqclose:"))
async def request_close_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user:
        return
    try:
        _, request_id_raw, reason = callback.data.split(":", 2)
        request_id = int(request_id_raw)
    except Exception:
        await callback.answer("Эта кнопка уже неактуальна", show_alert=True)
        return
    try:
        result, row = await asyncio.to_thread(close_request_owned, request_id, int(callback.from_user.id), reason)
    except Exception:
        log.exception("Failed to close market request %s", request_id)
        await callback.answer("Не удалось закрыть заявку. Попробуйте ещё раз", show_alert=True)
        return
    if result == "not_found":
        await callback.answer("Заявка не найдена. Откройте «Мои заявки»", show_alert=True)
        return
    if result == "closed":
        await callback.answer("Заявка уже закрыта", show_alert=True)
        return

    await state.clear()
    labels = {"bought": "✅ Отмечено: автомобиль куплен.", "sold": "✅ Отмечено: автомобиль продан.", "cancelled": "🛑 Заявка закрыта."}
    await callback.answer("Готово")
    if callback.message and row:
        await callback.message.edit_text(request_card(row), parse_mode="HTML", reply_markup=None)
        await callback.message.answer(labels[reason], reply_markup=MAIN_KB)


@dp.message(F.text == "💰 Хочу продать авто")
async def seller_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SellerForm.city)
    await message.answer("В каком городе находится автомобиль?", reply_markup=ReplyKeyboardRemove())


@dp.message(SellerForm.city)
async def seller_city(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Укажите город, например: Симферополь.")
        return
    await state.update_data(city=text[:120])
    await state.set_state(SellerForm.vehicle)
    await message.answer("Марка и модель автомобиля? Например: Toyota Camry 2.5.")


@dp.message(SellerForm.vehicle)
async def seller_vehicle(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Укажите марку и модель, например: Toyota Camry.")
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
        await message.answer("Укажите цену, например: 2,5 млн или 2 500 000 ₽.")
        return
    await state.update_data(asking_price=value)
    await state.set_state(SellerForm.mileage)
    await message.answer("Какой пробег автомобиля в километрах?")


@dp.message(SellerForm.mileage)
async def seller_mileage(message: Message, state: FSMContext) -> None:
    value = parse_int(message.text or "")
    if value is None or value > 2_000_000:
        await message.answer("Укажите пробег цифрами, например: 85 000.")
        return
    await state.update_data(mileage_km=value)
    await state.set_state(SellerForm.trim)
    await message.answer(
        "Какая комплектация автомобиля? Например: M Sport, Elegance или Comfort.\nЕсли не хотите указывать — нажмите «Пропустить».",
        reply_markup=SKIP_KB,
    )


@dp.message(SellerForm.trim)
async def seller_trim(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    vehicle_trim = None if text == "Пропустить" else text[:200]
    await state.update_data(vehicle_trim=vehicle_trim)
    await state.set_state(SellerForm.description)
    await message.answer(
        "Кратко опишите автомобиль: состояние, владельцы, ДТП, обслуживание и важные особенности.\nЕсли не хотите указывать — нажмите «Пропустить».",
        reply_markup=SKIP_KB,
    )


@dp.message(SellerForm.description)
async def seller_description(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    description = None if text == "Пропустить" else text[:1000]
    await state.update_data(seller_description=description, photo_file_ids=[], photo_unique_ids=[])
    await state.set_state(SellerForm.photo)
    await message.answer(
        "📷 Добавьте до 5 фотографий автомобиля.\n\n"
        "Лучше всего: спереди, сзади, салон, приборная панель и важные детали. "
        "Можно отправить несколько фото сразу одним альбомом или по одному. "
        "После первого фото появится кнопка «✅ Готово».\n\n"
        "Если фото пока нет — нажмите «Пропустить».",
        reply_markup=SKIP_KB,
    )


async def finalize_seller_album(
    key: tuple[int, int, str],
    state: FSMContext,
) -> None:
    await asyncio.sleep(SELLER_ALBUM_WAIT_SECONDS)

    async with SELLER_ALBUM_LOCK:
        incoming = SELLER_ALBUM_BUFFERS.pop(key, [])
        SELLER_ALBUM_TASKS.pop(key, None)

    if not incoming:
        return
    if await state.get_state() != SellerForm.photo.state:
        return

    data = await state.get_data()
    photo_ids = list(data.get("photo_file_ids") or [])
    unique_ids = list(data.get("photo_unique_ids") or [])
    incoming_count = len(incoming)

    for file_id, unique_id in incoming:
        if unique_id in unique_ids:
            continue
        if len(photo_ids) >= MAX_SELLER_PHOTOS:
            break
        photo_ids.append(file_id)
        unique_ids.append(unique_id)

    await state.update_data(
        photo_file_ids=photo_ids,
        photo_unique_ids=unique_ids,
    )
    count = len(photo_ids)

    if count >= MAX_SELLER_PHOTOS:
        await state.update_data(
            photo_file_id=photo_ids[0],
            photo_unique_id=unique_ids[0],
        )
        await state.set_state(SellerForm.contact)
        extra = " Сохранил первые 5." if incoming_count > MAX_SELLER_PHOTOS else ""
        await bot.send_message(
            key[0],
            f"✅ Добавлено 5 фото — отлично.{extra} Теперь укажите контакт для связи.",
            reply_markup=CONTACT_KB,
        )
        return

    await bot.send_message(
        key[0],
        f"✅ Добавлено фото: {count}/{MAX_SELLER_PHOTOS}. Отправьте ещё или нажмите «✅ Готово».",
        reply_markup=PHOTO_KB,
    )


@dp.message(SellerForm.photo)
async def seller_photo(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()

    if message.photo and message.media_group_id:
        photo = message.photo[-1]
        user_id = int(message.from_user.id) if message.from_user else 0
        key = (int(message.chat.id), user_id, str(message.media_group_id))
        async with SELLER_ALBUM_LOCK:
            buffer = SELLER_ALBUM_BUFFERS.setdefault(key, [])
            if all(unique_id != photo.file_unique_id for _, unique_id in buffer):
                buffer.append((photo.file_id, photo.file_unique_id))
            if key not in SELLER_ALBUM_TASKS:
                SELLER_ALBUM_TASKS[key] = asyncio.create_task(finalize_seller_album(key, state))
        return

    data = await state.get_data()
    photo_ids = list(data.get("photo_file_ids") or [])
    unique_ids = list(data.get("photo_unique_ids") or [])

    if message.photo:
        photo = message.photo[-1]
        if photo.file_unique_id not in unique_ids and len(photo_ids) < MAX_SELLER_PHOTOS:
            photo_ids.append(photo.file_id)
            unique_ids.append(photo.file_unique_id)
        await state.update_data(photo_file_ids=photo_ids, photo_unique_ids=unique_ids)

        count = len(photo_ids)
        if count >= MAX_SELLER_PHOTOS:
            await state.update_data(
                photo_file_id=photo_ids[0],
                photo_unique_id=unique_ids[0],
            )
            await state.set_state(SellerForm.contact)
            await message.answer(
                "✅ Добавлено 5 фото — отлично. Теперь укажите контакт для связи.",
                reply_markup=CONTACT_KB,
            )
            return

        await message.answer(
            f"✅ Фото добавлено: {count}/{MAX_SELLER_PHOTOS}. Отправьте ещё или нажмите «✅ Готово».",
            reply_markup=PHOTO_KB,
        )
        return

    if text == "✅ Готово":
        if not photo_ids:
            await message.answer("Добавьте хотя бы одно фото или нажмите «Пропустить».", reply_markup=SKIP_KB)
            return
        await state.update_data(photo_file_id=photo_ids[0], photo_unique_id=unique_ids[0])
    elif text == "Пропустить":
        await state.update_data(
            photo_file_id=None, photo_unique_id=None,
            photo_file_ids=[], photo_unique_ids=[],
        )
    else:
        await message.answer(
            "Отправьте до 5 фотографий автомобиля — можно одним альбомом или по одной. Когда закончите — нажмите «✅ Готово».",
            reply_markup=PHOTO_KB if photo_ids else SKIP_KB,
        )
        return

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
                "Укажите контакт: отправьте номер кнопкой или напишите @username.",
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
        "vehicle_trim": data.get("vehicle_trim"),
        "seller_description": data.get("seller_description"),
        "photo_file_id": data.get("photo_file_id"),
        "photo_unique_id": data.get("photo_unique_id"),
        "photo_file_ids": data.get("photo_file_ids") or [],
        "photo_unique_ids": data.get("photo_unique_ids") or [],
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
        await message.answer("⚠️ Не удалось сохранить заявку. Попробуйте ещё раз через минуту.", reply_markup=MAIN_KB)
        await state.clear()
        return
    if row.get("_is_duplicate"):
        await state.clear()
        await message.answer(
            f"ℹ️ Такая активная заявка уже есть: №{row['id']}.",
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
        "Автомобиль сохранён и участвует в подборе покупателей. Если найдётся подходящий запрос, вы получите предложение здесь.",
        reply_markup=MAIN_KB,
    )


@dp.callback_query(F.data.startswith("macc:"))
async def match_accept_callback(callback: CallbackQuery) -> None:
    try:
        _, match_id_raw, side = callback.data.split(":", 2)
        match_id = int(match_id_raw)
    except Exception:
        await callback.answer("Эта кнопка уже неактуальна", show_alert=True)
        return

    bundle = await asyncio.to_thread(get_match_bundle, match_id)
    if not bundle:
        await callback.answer("Это предложение уже неактуально", show_alert=True)
        return
    match, buyer, seller = bundle
    if match.get("status") not in {"approved", "offered"}:
        await callback.answer("Это предложение уже закрыто", show_alert=True)
        return
    request_row = buyer if side == "buyer" else seller
    if not callback.from_user or int(request_row["telegram_user_id"]) != int(callback.from_user.id):
        await callback.answer("Эта кнопка доступна другому участнику", show_alert=True)
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
        await callback.answer("Эта кнопка уже неактуальна", show_alert=True)
        return

    bundle = await asyncio.to_thread(get_match_bundle, match_id)
    if not bundle:
        await callback.answer("Это предложение уже неактуально", show_alert=True)
        return
    match, buyer, seller = bundle
    if match.get("status") not in {"approved", "offered"}:
        await callback.answer("Это предложение уже закрыто", show_alert=True)
        return
    request_row = buyer if side == "buyer" else seller
    if not callback.from_user or int(request_row["telegram_user_id"]) != int(callback.from_user.id):
        await callback.answer("Эта кнопка доступна другому участнику", show_alert=True)
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
    await message.answer("Выберите действие в меню ниже 👇", reply_markup=MAIN_KB)


async def main() -> None:
    log.info("Starting public auto bot @%s", PUBLIC_BOT_USERNAME)
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        await bot.set_my_short_description(
            "AutoClick Market — покупка и продажа авто через умные совпадения заявок."
        )
        await bot.set_my_description(
            "🚘 AutoClick Market — сервис для покупки и продажи автомобилей через Telegram.\n\n"
            "🔎 Покупатель оставляет запрос: автомобиль, бюджет, год, пробег и пожелания.\n"
            "💰 Продавец добавляет авто, цену, описание и до 5 фотографий.\n\n"
            "AutoClick сопоставляет заявки и показывает подходящие варианты.\n\n"
            "🔒 Контакты открываются только после взаимного подтверждения интереса.\n\n"
            "Один запрос — больше выбора."
        )
    except Exception:
        log.exception("Could not update public bot profile/commands")
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
