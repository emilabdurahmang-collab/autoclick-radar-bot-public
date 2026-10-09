import asyncio
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "").strip()
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "8"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "20"))

ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW) if ADMIN_CHAT_ID_RAW else None

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not set")
if not SUPABASE_SECRET_KEY:
    raise RuntimeError("SUPABASE_SECRET_KEY is not set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("autoclick-radar")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

# Cache include-keywords so every Telegram message does not hit Supabase.
_keyword_cache: tuple[float, list[str]] = (0.0, [])
KEYWORD_CACHE_SECONDS = 300

CRIMEA_CITIES = [
    "Симферополь", "Севастополь", "Ялта", "Евпатория", "Керчь",
    "Феодосия", "Алушта", "Джанкой", "Саки", "Бахчисарай",
    "Белогорск", "Судак", "Армянск", "Красноперекопск",
]

BUY_INTENT_RE = re.compile(
    r"\b(куплю|ищу|хочу\s+купить|готов\s+купить|нужн(?:а|о|ы|ен)?|подбираю|рассматриваю)\b",
    re.IGNORECASE,
)
ADVISORY_INTENT_RE = re.compile(
    r"(что.{0,15}взять|что.{0,15}купить|что\s+посоветуете|посоветуйте|"
    r"какую.{0,25}(?:машин\w*|авто|автомобил\w*).{0,25}(?:взять|купить|выбрать)|"
    r"выбираю\s+между|что\s+лучше\s+взять|какой\s+вариант\s+лучше)",
    re.IGNORECASE,
)
AUTO_HINT_RE = re.compile(
    r"\b(авто|машин\w*|автомобил\w*|кроссовер\w*|седан\w*|внедорожник\w*|"
    r"camry|камри|corolla|королла|bmw|бмв|audi|ауди|mercedes|мерседес|toyota|тойота|"
    r"lada|лада|kia|киа|hyundai|хендай|skoda|шкода|volkswagen|фольксваген|"
    r"lexus|лексус|honda|хонда|mazda|мазда|nissan|ниссан|ford|форд|geely|джили|"
    r"haval|хавал|chery|чери|omoda|омода|exeed|эксид|zeekr|зикр|"
    r"solaris|солярис|rio|рио|sportage|спортаж|tucson|туссан|tiguan|тигуан|"
    r"x5|x3|x6|q3|q5|q7|rav4|рав4|cx5|cx-5|кашкай|qashqai)\b",
    re.IGNORECASE,
)
SELLER_SIGNAL_RE = re.compile(
    r"\b(продам|продаю|продается|продаётся|автосалон|в\s+наличии|выставил\s+на\s+продажу)\b",
    re.IGNORECASE,
)
PARTS_SIGNAL_RE = re.compile(
    r"\b(запчаст\w*|разбор\w*|двигател\w*|кпп|бампер\w*|фар[ау]?|аккумулятор\w*|ремонт\w*)\b",
    re.IGNORECASE,
)
SERVICE_SIGNAL_RE = re.compile(
    r"\b(автоподбор|услуг[аи]|кредит|рассрочк\w*|страховк\w*|подписывайтесь|реклама)\b",
    re.IGNORECASE,
)


def esc(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value))


def format_budget(value: Any, currency: str | None) -> str:
    if value in (None, ""):
        return "не указан"
    try:
        number = float(value)
        text = f"{number:,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        text = str(value)
    return f"{text} {currency or 'RUB'}"


def lead_label(quality: str | None) -> str:
    return {
        "hot": "🔥 Горячий",
        "warm": "🟠 Тёплый",
        "cold": "🔵 Холодный",
        "rejected": "⚪ Не подходит",
    }.get(quality or "", "Новый лид")


def lead_message(lead: dict[str, Any]) -> str:
    reasons = lead.get("score_reasons") or {}
    tags: list[str] = []
    if reasons.get("has_budget"):
        tags.append("бюджет")
    if reasons.get("has_city"):
        tags.append("город")
    if reasons.get("has_year"):
        tags.append("год авто")
    if reasons.get("has_urgency"):
        tags.append("срочность")
    if reasons.get("specific_vehicle"):
        tags.append("конкретная модель/марка")
    if reasons.get("strong_intent"):
        tags.append("явное намерение купить")
    elif reasons.get("advisory_intent"):
        tags.append("выбор автомобиля")

    matched = lead.get("matched_keywords") or []
    source = lead.get("chat_name") or f"chat {lead.get('chat_id')}"
    score = lead.get("lead_score")
    score_text = f"{score}/100" if score is not None else "—"

    if lead.get("username"):
        author = f"@{esc(lead.get('username'))}"
    else:
        author = esc(lead.get("sender_name"))

    vehicle = " ".join(
        part for part in [str(lead.get("vehicle_make") or "").strip(), str(lead.get("vehicle_model") or "").strip()]
        if part
    )

    body = [
        f"<b>{lead_label(lead.get('lead_quality'))} · {esc(score_text)}</b>",
        "",
        f"<b>Источник:</b> {esc(source)}",
        f"<b>Автор:</b> {author}",
    ]
    if vehicle:
        body.append(f"<b>Автомобиль:</b> {esc(vehicle)}")
    body.extend([
        f"<b>Город:</b> {esc(lead.get('city'))}",
        f"<b>Бюджет:</b> {esc(format_budget(lead.get('budget'), lead.get('currency')))}",
    ])

    if matched:
        body.append(f"<b>Совпадения:</b> {esc(', '.join(matched[:5]))}")
    if tags:
        body.append(f"<b>Сигналы:</b> {esc(', '.join(tags))}")
    if lead.get("ai_checked"):
        body.append(
            f"<b>ИИ-анализ:</b> {esc(lead.get('ai_reason') or 'проверено ИИ')} "
            f"({esc(lead.get('ai_confidence'))}%)"
        )

    body.extend([
        "",
        "<b>Сообщение:</b>",
        esc(lead.get("message_text") or ""),
    ])
    return "\n".join(body)


def match_message(match: dict[str, Any], buyer: dict[str, Any], seller: dict[str, Any]) -> str:
    reasons = match.get("match_reasons") or {}
    score = int(match.get("match_score") or 0)
    same_city = "да" if reasons.get("same_city") else "нет"
    budget = format_budget(buyer.get("budget"), "RUB")
    price = format_budget(seller.get("asking_price"), "RUB")
    return "\n".join([
        f"<b>🔗 СОВПАДЕНИЕ · {score}%</b>",
        "",
        "<b>🟢 ПОКУПАТЕЛЬ</b>",
        f"Авто: {esc(buyer.get('vehicle'))}",
        f"Город: {esc(buyer.get('city'))}",
        f"Бюджет: {esc(budget)}",
        f"Требования: {esc(buyer.get('requirements') or 'без дополнительных требований')}",
        "",
        "<b>🔵 ПРОДАВЕЦ</b>",
        f"Авто: {esc(seller.get('vehicle'))}",
        f"Город: {esc(seller.get('city'))}",
        f"Год: {esc(seller.get('vehicle_year'))}",
        f"Цена: {esc(price)}",
        f"Пробег: {esc(seller.get('mileage_km'))} км",
        "",
        f"Совпадает город: {same_city}",
        "Контакты сторонам пока не раскрываются.",
    ])


def match_keyboard(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Соединить", callback_data=f"matchconnect:{match_id}"),
        InlineKeyboardButton(text="❌ Не подходит", callback_data=f"matchreject:{match_id}"),
    ]])


def db_get_market_request(request_id: int) -> dict[str, Any] | None:
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


def db_get_pending_matches() -> list[dict[str, Any]]:
    return (
        supabase.table("auto_matches")
        .select("*")
        .eq("status", "new")
        .eq("notification_status", "pending")
        .order("created_at")
        .limit(10)
        .execute()
        .data
        or []
    )


def db_claim_match(match_id: int) -> bool:
    rows = (
        supabase.table("auto_matches")
        .update({
            "notification_status": "sending",
            "notification_claimed_at": datetime.now(timezone.utc).isoformat(),
            "notification_error": None,
        })
        .eq("id", match_id)
        .eq("notification_status", "pending")
        .eq("status", "new")
        .execute()
        .data
        or []
    )
    return bool(rows)


def db_mark_match_sent(match_id: int) -> None:
    (
        supabase.table("auto_matches")
        .update({
            "notification_status": "sent",
            "notified_at": datetime.now(timezone.utc).isoformat(),
            "notification_error": None,
        })
        .eq("id", match_id)
        .execute()
    )


def db_mark_match_failed(match_id: int, error: str) -> None:
    (
        supabase.table("auto_matches")
        .update({"notification_status": "failed", "notification_error": error[:1000]})
        .eq("id", match_id)
        .execute()
    )


def db_requeue_stale_matches() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    (
        supabase.table("auto_matches")
        .update({"notification_status": "pending", "notification_claimed_at": None})
        .eq("notification_status", "sending")
        .eq("status", "new")
        .lt("notification_claimed_at", cutoff)
        .execute()
    )


def db_set_match_status(match_id: int, status: str) -> None:
    (
        supabase.table("auto_matches")
        .update({"status": status})
        .eq("id", match_id)
        .execute()
    )


def keyboard_for(lead: dict[str, Any]) -> InlineKeyboardMarkup:
    lead_id = lead["id"]
    rows = [[
        InlineKeyboardButton(text="✅ В работу", callback_data=f"work:{lead_id}"),
        InlineKeyboardButton(text="❌ Не подходит", callback_data=f"reject:{lead_id}"),
    ]]
    if lead.get("message_link"):
        rows.append([
            InlineKeyboardButton(text="🔗 Открыть сообщение", url=lead["message_link"])
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def db_get_pending() -> list[dict[str, Any]]:
    response = (
        supabase.table("leads")
        .select("*")
        .eq("notification_status", "pending")
        .in_("lead_quality", ["hot", "warm"])
        .order("created_at")
        .limit(10)
        .execute()
    )
    return response.data or []


def db_claim(lead_id: int) -> bool:
    response = (
        supabase.table("leads")
        .update({
            "notification_status": "sending",
            "notification_claimed_at": datetime.now(timezone.utc).isoformat(),
            "notification_error": None,
        })
        .eq("id", lead_id)
        .eq("notification_status", "pending")
        .execute()
    )
    return bool(response.data)


def db_mark_sent(lead_id: int) -> None:
    (
        supabase.table("leads")
        .update({
            "notification_status": "sent",
            "notified_at": datetime.now(timezone.utc).isoformat(),
            "notification_error": None,
        })
        .eq("id", lead_id)
        .execute()
    )


def db_mark_failed(lead_id: int, error: str) -> None:
    (
        supabase.table("leads")
        .update({
            "notification_status": "failed",
            "notification_error": error[:1000],
        })
        .eq("id", lead_id)
        .execute()
    )


def db_requeue_stale() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    (
        supabase.table("leads")
        .update({
            "notification_status": "pending",
            "notification_claimed_at": None,
        })
        .eq("notification_status", "sending")
        .lt("notification_claimed_at", cutoff)
        .execute()
    )


def db_set_status(lead_id: int, status: str) -> None:
    (
        supabase.table("leads")
        .update({"status": status})
        .eq("id", lead_id)
        .execute()
    )


def db_get_include_keywords() -> list[str]:
    global _keyword_cache
    now = time.monotonic()
    cached_at, phrases = _keyword_cache
    if phrases and now - cached_at < KEYWORD_CACHE_SECONDS:
        return phrases

    response = (
        supabase.table("keywords")
        .select("phrase")
        .eq("category", "auto")
        .eq("kind", "include")
        .eq("enabled", True)
        .execute()
    )
    phrases = [str(row["phrase"]).strip().lower() for row in (response.data or []) if row.get("phrase")]
    _keyword_cache = (now, phrases)
    return phrases


def looks_ai_ambiguous(text: str) -> bool:
    """Cheap gate before asking AI about a message the rules would drop."""
    cleaned = text.strip()
    if len(cleaned) < 8:
        return False

    direct_buy = bool(re.search(r"\b(куплю|хочу\s+купить|готов\s+купить)\b", cleaned, re.IGNORECASE))
    if (PARTS_SIGNAL_RE.search(cleaned) or SERVICE_SIGNAL_RE.search(cleaned)) and not direct_buy:
        return False
    if SELLER_SIGNAL_RE.search(cleaned) and not direct_buy:
        return False

    if AUTO_HINT_RE.search(cleaned):
        return True
    if extract_budget(cleaned) is not None:
        return True
    return bool(re.search(
        r"\b(взять|вариант(?:ы|ов)?|интересует|подскажите|посоветуйте|выбираю|думаю\s+взять|"
        r"есть\s+что|есть\s+вариант|что\s+можно\s+взять|для\s+семьи|семейн\w*)\b",
        cleaned,
        re.IGNORECASE,
    ))


def _response_output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"])
    return ""


def ai_analyze_message(text: str, source_title: str | None) -> dict[str, Any] | None:
    """Classify an ambiguous auto-group message with OpenAI Responses API.

    Called only for ambiguous/borderline messages AND only when the source has
    ai_allowed=true. Clear buyer leads, sellers, parts and services do not call AI.
    Responses are not stored by OpenAI (store=false). If the API is unavailable,
    the bot simply falls back to the deterministic rules.
    """
    if not OPENAI_API_KEY:
        return None

    schema = {
        "type": "object",
        "properties": {
            "is_buyer": {"type": "boolean"},
            "confidence": {"type": "integer"},
            "intent": {
                "type": "string",
                "enum": ["buyer", "advice", "seller", "parts", "service", "other"],
            },
            "quality": {
                "type": "string",
                "enum": ["hot", "warm", "cold", "rejected"],
            },
            "vehicle_make": {"type": "string"},
            "vehicle_model": {"type": "string"},
            "city": {"type": "string"},
            "budget_rub": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": [
            "is_buyer", "confidence", "intent", "quality",
            "vehicle_make", "vehicle_model", "city", "budget_rub", "reason",
        ],
        "additionalProperties": False,
    }

    instructions = (
        "Ты классификатор лидов для сервиса подбора автомобилей. "
        "Определи, является ли сообщение реальным намерением купить автомобиль или просьбой помочь выбрать автомобиль. "
        "Не считай лидом продажу автомобиля, запчасти, ремонт, кредитные/страховые услуги, рекламу и обычную болтовню. "
        "HOT: явная покупка плюс конкретика (модель/бюджет/срок/готовность купить). "
        "WARM: намерение купить/выбрать есть, но конкретики меньше. "
        "COLD: слабый или неуверенный интерес. REJECTED: не покупатель. "
        "confidence — уверенность классификации 0..100. budget_rub=0, city='', vehicle_make='', vehicle_model='' если неизвестно. Не додумывай отсутствующие данные. "
        "Причина должна быть короткой, на русском, без персональных выводов сверх текста."
    )

    body = {
        "model": OPENAI_MODEL,
        "store": False,
        "reasoning": {"effort": "none"},
        "max_output_tokens": 220,
        "instructions": instructions,
        "input": f"Источник: {source_title or 'авто-группа'}\nСообщение: {text[:3000]}",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "lead_analysis",
                "strict": True,
                "schema": schema,
            }
        },
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        raw = _response_output_text(payload)
        if not raw:
            log.warning("AI returned no output text")
            return None
        result = json.loads(raw)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:1000]
        log.error("OpenAI API HTTP %s: %s", exc.code, body_text)
        return None
    except Exception:
        log.exception("OpenAI analysis failed")
        return None

    try:
        confidence = max(0, min(100, int(result.get("confidence", 0))))
        budget = max(0, int(result.get("budget_rub", 0) or 0))
    except (TypeError, ValueError):
        return None

    intent = str(result.get("intent") or "other")
    quality = str(result.get("quality") or "rejected")
    is_buyer = bool(result.get("is_buyer"))

    if intent not in {"buyer", "advice", "seller", "parts", "service", "other"}:
        intent = "other"
    if quality not in {"hot", "warm", "cold", "rejected"}:
        quality = "rejected"

    if not is_buyer or intent in {"seller", "parts", "service", "other"}:
        quality = "rejected"
    elif confidence < 55:
        quality = "cold"
    elif quality == "hot" and confidence < 75:
        quality = "warm"

    return {
        "is_buyer": is_buyer,
        "confidence": confidence,
        "intent": intent,
        "quality": quality,
        "vehicle_make": str(result.get("vehicle_make") or "").strip()[:80],
        "vehicle_model": str(result.get("vehicle_model") or "").strip()[:80],
        "city": str(result.get("city") or "").strip()[:120],
        "budget_rub": budget,
        "reason": str(result.get("reason") or "").strip()[:500],
        "model": OPENAI_MODEL,
    }


def _ai_quality_score(quality: str, confidence: int) -> int:
    if quality == "hot":
        return max(75, confidence)
    if quality == "warm":
        return max(50, min(74, confidence))
    if quality == "cold":
        return max(30, min(49, confidence))
    return min(29, confidence)


def db_apply_ai_result(lead: dict[str, Any], ai_result: dict[str, Any]) -> dict[str, Any]:
    quality = ai_result["quality"]
    confidence = int(ai_result["confidence"])
    score = _ai_quality_score(quality, confidence)
    reasons = dict(lead.get("score_reasons") or {})
    reasons.update({
        "ai_used": True,
        "ai_model": ai_result["model"],
        "ai_confidence": confidence,
        "ai_intent": ai_result["intent"],
        "ai_reason": ai_result["reason"],
    })

    update = {
        "ai_checked": True,
        "ai_confidence": confidence,
        "ai_intent": ai_result["intent"],
        "ai_quality": quality,
        "ai_reason": ai_result["reason"],
        "ai_model": ai_result["model"],
        "ai_checked_at": datetime.now(timezone.utc).isoformat(),
        "vehicle_make": ai_result.get("vehicle_make") or lead.get("vehicle_make"),
        "vehicle_model": ai_result.get("vehicle_model") or lead.get("vehicle_model"),
        "city": lead.get("city") or ai_result.get("city") or None,
        "budget": lead.get("budget") or (ai_result.get("budget_rub") or None),
        "lead_score": score,
        "lead_quality": quality,
        "score_reasons": reasons,
        "notification_status": "pending" if quality in {"hot", "warm"} else "skipped",
        "notification_error": None,
    }
    rows = supabase.table("leads").update(update).eq("id", lead["id"]).execute().data or []
    return rows[0] if rows else {**lead, **update}


def looks_like_candidate(text: str) -> bool:
    """Fast semantic pre-filter before saving a message to Supabase.

    We intentionally do not store every group message. Only plausible buyer
    messages are sent to the database, where the full smart scoring trigger
    assigns HOT / WARM / COLD / REJECTED.
    """
    lowered = text.lower()
    phrases = db_get_include_keywords()

    # Exact high-signal phrases configured in Supabase always qualify.
    if any(phrase in lowered for phrase in phrases):
        return True

    # Avoid obvious sales, services and parts requests before storing them.
    # A direct buyer statement still wins over a generic seller word.
    direct_buy = bool(re.search(r"\b(куплю|хочу\s+купить|готов\s+купить)\b", text, re.IGNORECASE))
    if PARTS_SIGNAL_RE.search(text) or SERVICE_SIGNAL_RE.search(text):
        return False
    if SELLER_SIGNAL_RE.search(text) and not direct_buy:
        return False

    has_auto = bool(AUTO_HINT_RE.search(text))
    has_buy_intent = bool(BUY_INTENT_RE.search(text))
    has_advisory_intent = bool(ADVISORY_INTENT_RE.search(text))
    has_budget = extract_budget(text) is not None or bool(
        re.search(r"\bбюджет\b|\bдо\s*\d|\d+(?:[\.,]\d+)?\s*(?:млн|тыс|руб|₽)", text, re.IGNORECASE)
    )

    # Examples: "куплю Camry", "ищу X5", "нужна машина".
    if has_buy_intent and has_auto:
        return True

    # In an auto group, messages like "что взять за 1.5 млн?" are valuable
    # even when the word "машина" is omitted.
    if has_advisory_intent and (has_auto or has_budget):
        return True

    return False


def extract_city(text: str) -> str | None:
    lowered = text.lower()
    for city in CRIMEA_CITIES:
        if city.lower() in lowered:
            return city
    return None


def extract_budget(text: str) -> float | None:
    normalized = text.lower().replace("₽", " руб ")

    # Examples: 2.5 млн, 2,5 млн
    m = re.search(r"(\d+(?:[\.,]\d+)?)\s*млн", normalized)
    if m:
        return float(m.group(1).replace(",", ".")) * 1_000_000

    # Examples: 2500 тыс, 2 500 тыс
    m = re.search(r"([\d\s]{2,})\s*тыс", normalized)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        if digits:
            return float(digits) * 1_000

    # Examples: 2 500 000 руб / 2500000 руб
    m = re.search(r"([\d][\d\s\.]{4,})\s*(?:руб|р\b)", normalized)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        if digits:
            return float(digits)

    return None


def telegram_message_link(message: Message) -> str | None:
    if message.chat.username:
        return f"https://t.me/{message.chat.username}/{message.message_id}"

    chat_id = str(message.chat.id)
    if message.chat.type == "supergroup" and chat_id.startswith("-100"):
        internal_id = chat_id[4:]
        return f"https://t.me/c/{internal_id}/{message.message_id}"
    return None


def db_get_or_create_source(message: Message) -> dict[str, Any]:
    chat_id = int(message.chat.id)
    existing = (
        supabase.table("sources")
        .select("*")
        .eq("telegram_chat_id", chat_id)
        .limit(1)
        .execute()
    ).data or []

    now = datetime.now(timezone.utc).isoformat()
    title = message.chat.title or str(chat_id)
    username = message.chat.username
    source_type = "supergroup" if message.chat.type == "supergroup" else "group"

    if existing:
        source = existing[0]
        updated = (
            supabase.table("sources")
            .update({
                "title": title,
                "username": username,
                "source_type": source_type,
                "last_seen_at": now,
            })
            .eq("id", source["id"])
            .execute()
        ).data or []
        return updated[0] if updated else source

    created = (
        supabase.table("sources")
        .insert({
            "telegram_chat_id": chat_id,
            "username": username,
            "title": title,
            "source_type": source_type,
            "category": "auto",
            # A group where the bot was explicitly added is treated as an authorized source.
            "enabled": True,
            "ai_allowed": False,
            "last_seen_at": now,
            "notes": "Auto-registered by Telegram bot",
        })
        .execute()
    ).data or []
    if not created:
        raise RuntimeError("Could not register Telegram source")
    return created[0]


def db_insert_group_lead(message: Message, source: dict[str, Any], text: str, ai_result: dict[str, Any] | None = None) -> dict[str, Any] | None:
    sender = message.from_user
    sender_name = sender.full_name if sender else None
    username = sender.username if sender else None
    sender_id = sender.id if sender else None

    payload = {
        "telegram_message_date": message.date.astimezone(timezone.utc).isoformat() if message.date else None,
        "chat_id": int(message.chat.id),
        "chat_name": message.chat.title,
        "message_id": int(message.message_id),
        "sender_id": sender_id,
        "username": username,
        "sender_name": sender_name,
        "message_text": text,
        "message_link": telegram_message_link(message),
        "category": "auto",
        "vehicle_make": ((ai_result or {}).get("vehicle_make") or None),
        "vehicle_model": ((ai_result or {}).get("vehicle_model") or None),
        "city": extract_city(text) or ((ai_result or {}).get("city") or None),
        "budget": extract_budget(text) or (((ai_result or {}).get("budget_rub") or 0) or None),
        "currency": "RUB",
        "ai_checked": bool(ai_result),
        "ai_confidence": (ai_result or {}).get("confidence"),
        "ai_intent": (ai_result or {}).get("intent"),
        "ai_quality": (ai_result or {}).get("quality"),
        "ai_reason": (ai_result or {}).get("reason"),
        "ai_model": (ai_result or {}).get("model"),
        "ai_checked_at": datetime.now(timezone.utc).isoformat() if ai_result else None,
        "status": "new",
        "source_id": source["id"],
        "raw_data": {
            "ingestion": "telegram_bot",
            "chat_type": message.chat.type,
            "ai_candidate": bool(ai_result),
            "ai_vehicle_make": (ai_result or {}).get("vehicle_make") or None,
            "ai_vehicle_model": (ai_result or {}).get("vehicle_model") or None,
        },
    }

    # Avoid duplicates if Telegram redelivers an update.
    existing = (
        supabase.table("leads")
        .select("id")
        .eq("chat_id", int(message.chat.id))
        .eq("message_id", int(message.message_id))
        .limit(1)
        .execute()
    ).data or []
    if existing:
        return None

    created = supabase.table("leads").insert(payload).execute().data or []
    return created[0] if created else None


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    if ADMIN_CHAT_ID is None:
        await message.answer(
            "Бот запущен.\n\n"
            f"Ваш ADMIN_CHAT_ID: <code>{message.chat.id}</code>\n\n"
            "Добавьте это число в переменную ADMIN_CHAT_ID на сервере и перезапустите приложение.",
            parse_mode="HTML",
        )
        return

    if message.chat.type == "private" and message.chat.id != ADMIN_CHAT_ID:
        await message.answer("Доступ закрыт.")
        return

    if message.chat.type == "private":
        await message.answer("✅ AutoClick Radar запущен. Горячие и тёплые лиды будут приходить сюда. ИИ-анализ неоднозначных сообщений: " + ("включён" if OPENAI_API_KEY else "ожидает OPENAI_API_KEY"))


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_message_handler(message: Message) -> None:
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return
    if message.from_user and message.from_user.is_bot:
        return

    try:
        source = await asyncio.to_thread(db_get_or_create_source, message)
        if not source.get("enabled", False):
            log.info("Source %s disabled; message skipped", source.get("id"))
            return

        is_candidate = await asyncio.to_thread(looks_like_candidate, text)
        ai_result: dict[str, Any] | None = None
        ai_decision_used = False

        # If rules are unsure, AI may decide whether this is a buyer lead.
        if not is_candidate and source.get("ai_allowed", False) and OPENAI_API_KEY:
            ai_gate = await asyncio.to_thread(looks_ai_ambiguous, text)
            if ai_gate:
                ai_result = await asyncio.to_thread(ai_analyze_message, text, source.get("title"))
                if ai_result:
                    log.info(
                        "AI checked ambiguous group message %s/%s: intent=%s quality=%s confidence=%s",
                        message.chat.id, message.message_id, ai_result.get("intent"),
                        ai_result.get("quality"), ai_result.get("confidence"),
                    )
                    is_candidate = bool(
                        ai_result.get("is_buyer")
                        and ai_result.get("intent") in {"buyer", "advice"}
                        and int(ai_result.get("confidence", 0)) >= 55
                    )
                    ai_decision_used = is_candidate

        if not is_candidate:
            log.info("Group message %s/%s ignored: no buyer intent", message.chat.id, message.message_id)
            return

        # IMPORTANT: do NOT call AI for messages already accepted by deterministic rules.
        # AI is reserved only for ambiguous/borderline messages that the cheap rules
        # would otherwise drop, and only for sources where ai_allowed=true.

        lead = await asyncio.to_thread(db_insert_group_lead, message, source, text, ai_result)
        if lead and ai_result and ai_decision_used:
            lead = await asyncio.to_thread(db_apply_ai_result, lead, ai_result)

        if lead:
            log.info(
                "Group lead %s saved: quality=%s score=%s chat=%s ai=%s",
                lead.get("id"),
                lead.get("lead_quality"),
                lead.get("lead_score"),
                message.chat.id,
                bool(ai_result),
            )
    except Exception:
        log.exception("Failed to process group message %s/%s", message.chat.id, message.message_id)


@dp.callback_query(F.data.startswith("matchconnect:"))
async def match_connect_callback(callback: CallbackQuery) -> None:
    if ADMIN_CHAT_ID is not None and callback.message and callback.message.chat.id != ADMIN_CHAT_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    match_id = int(callback.data.split(":", 1)[1])
    await asyncio.to_thread(db_set_match_status, match_id, "approved")
    await callback.answer("Обеим сторонам будет отправлено предложение")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"✅ Совпадение #{match_id} одобрено. AutoClick отправит предложение покупателю и продавцу; контакты откроются только после взаимного согласия."
        )


@dp.callback_query(F.data.startswith("matchreject:"))
async def match_reject_callback(callback: CallbackQuery) -> None:
    if ADMIN_CHAT_ID is not None and callback.message and callback.message.chat.id != ADMIN_CHAT_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    match_id = int(callback.data.split(":", 1)[1])
    await asyncio.to_thread(db_set_match_status, match_id, "rejected")
    await callback.answer("Совпадение отклонено")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"❌ Совпадение #{match_id} отклонено. Обе исходные заявки остаются в базе.")


@dp.callback_query(F.data.startswith("work:"))
async def work_callback(callback: CallbackQuery) -> None:
    if ADMIN_CHAT_ID is not None and callback.message and callback.message.chat.id != ADMIN_CHAT_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    lead_id = int(callback.data.split(":", 1)[1])
    await asyncio.to_thread(db_set_status, lead_id, "in_progress")
    await callback.answer("Лид взят в работу")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"✅ Лид #{lead_id} — в работе")


@dp.callback_query(F.data.startswith("reject:"))
async def reject_callback(callback: CallbackQuery) -> None:
    if ADMIN_CHAT_ID is not None and callback.message and callback.message.chat.id != ADMIN_CHAT_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    lead_id = int(callback.data.split(":", 1)[1])
    await asyncio.to_thread(db_set_status, lead_id, "rejected")
    await callback.answer("Лид отмечен как неподходящий")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"❌ Лид #{lead_id} — не подходит")


async def notification_worker() -> None:
    while True:
        try:
            if ADMIN_CHAT_ID is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            await asyncio.to_thread(db_requeue_stale)
            leads = await asyncio.to_thread(db_get_pending)

            for lead in leads:
                lead_id = int(lead["id"])
                claimed = await asyncio.to_thread(db_claim, lead_id)
                if not claimed:
                    continue

                try:
                    await bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=lead_message(lead),
                        parse_mode="HTML",
                        reply_markup=keyboard_for(lead),
                        disable_web_page_preview=True,
                    )
                    await asyncio.to_thread(db_mark_sent, lead_id)
                    log.info("Lead %s sent", lead_id)
                except Exception as exc:
                    log.exception("Failed to send lead %s", lead_id)
                    await asyncio.to_thread(db_mark_failed, lead_id, str(exc))

            await asyncio.to_thread(db_requeue_stale_matches)
            matches = await asyncio.to_thread(db_get_pending_matches)
            for match in matches:
                match_id = int(match["id"])
                claimed = await asyncio.to_thread(db_claim_match, match_id)
                if not claimed:
                    continue
                try:
                    buyer = await asyncio.to_thread(db_get_market_request, int(match["buyer_request_id"]))
                    seller = await asyncio.to_thread(db_get_market_request, int(match["seller_request_id"]))
                    if not buyer or not seller:
                        raise RuntimeError("buyer or seller request missing")
                    await bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=match_message(match, buyer, seller),
                        parse_mode="HTML",
                        reply_markup=match_keyboard(match_id),
                        disable_web_page_preview=True,
                    )
                    await asyncio.to_thread(db_mark_match_sent, match_id)
                    log.info("Match %s sent", match_id)
                except Exception as exc:
                    log.exception("Failed to send match %s", match_id)
                    await asyncio.to_thread(db_mark_match_failed, match_id, str(exc))
        except Exception:
            log.exception("Notification worker error")

        await asyncio.sleep(POLL_INTERVAL)


async def main() -> None:
    worker = asyncio.create_task(notification_worker())
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
