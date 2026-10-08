import asyncio
import logging
import os
import re
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
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


def save_market_request(payload: dict[str, Any]) -> dict[str, Any]:
    rows = supabase.table("market_requests").insert(payload).execute().data or []
    if not rows:
        raise RuntimeError("market request was not saved")
    return rows[0]


def save_buyer_as_lead(request_row: dict[str, Any], message: Message) -> dict[str, Any] | None:
    contact = request_row.get("contact_phone") or request_row.get("contact_telegram") or "не указан"
    budget = request_row.get("budget")
    requirements = request_row.get("requirements") or "без дополнительных требований"
    lead_text = (
        f"Куплю авто: {request_row.get('vehicle')}. "
        f"Город: {request_row.get('city')}. "
        f"Бюджет до {int(budget):,} руб. ".replace(",", " ")
        + f"Требования: {requirements}. Контакт: {contact}"
    )
    payload = {
        "telegram_message_date": message.date.isoformat() if message.date else None,
        "chat_id": int(message.chat.id),
        "chat_name": "Куплю авто | Продам авто",
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
            "contact_phone": request_row.get("contact_phone"),
            "contact_telegram": request_row.get("contact_telegram"),
        },
    }
    rows = supabase.table("leads").insert(payload).execute().data or []
    return rows[0] if rows else None


async def notify_admin_seller(row: dict[str, Any]) -> None:
    if ADMIN_CHAT_ID is None:
        return
    contact = row.get("contact_phone") or row.get("contact_telegram") or "не указан"
    price = f"{int(row['asking_price']):,}".replace(",", " ")
    mileage = f"{int(row['mileage_km']):,}".replace(",", " ")
    text = (
        "💰 <b>Новая заявка на продажу авто</b>\n\n"
        f"<b>Город:</b> {row.get('city') or '—'}\n"
        f"<b>Автомобиль:</b> {row.get('vehicle') or '—'}\n"
        f"<b>Год:</b> {row.get('vehicle_year') or '—'}\n"
        f"<b>Цена:</b> {price} ₽\n"
        f"<b>Пробег:</b> {mileage} км\n"
        f"<b>Контакт:</b> {contact}\n"
        f"<b>Заявка:</b> #{row.get('id')}"
    )
    try:
        await bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML")
    except Exception:
        log.exception("Could not notify admin about seller request %s", row.get("id"))


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
    await state.set_state(BuyerForm.requirements)
    await message.answer(
        "Есть дополнительные требования? Год, пробег, привод, цвет и т.д.\nЕсли нет — нажмите «Пропустить».",
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
        "requirements": data.get("requirements"),
        "contact_phone": phone,
        "contact_telegram": telegram or ident["contact_telegram"],
        "status": "new",
        "source": "telegram_public_bot",
        "raw_data": {"bot_username": PUBLIC_BOT_USERNAME},
    }
    try:
        row = await asyncio.to_thread(save_market_request, payload)
        await asyncio.to_thread(save_buyer_as_lead, row, message)
    except Exception:
        log.exception("Failed to save buyer request")
        await message.answer("Не удалось сохранить заявку. Попробуйте ещё раз позже.", reply_markup=MAIN_KB)
        await state.clear()
        return
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
        await notify_admin_seller(row)
    except Exception:
        log.exception("Failed to save seller request")
        await message.answer("Не удалось сохранить заявку. Попробуйте ещё раз позже.", reply_markup=MAIN_KB)
        await state.clear()
        return
    await state.clear()
    await message.answer(
        f"✅ Заявка #{row['id']} принята.\n\n"
        "Данные автомобиля сохранены. Мы сможем сопоставлять его с запросами покупателей.",
        reply_markup=MAIN_KB,
    )


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer("Выберите действие кнопкой ниже:", reply_markup=MAIN_KB)


async def main() -> None:
    log.info("Starting public auto bot @%s", PUBLIC_BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
