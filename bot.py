import asyncio
import html
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import uvicorn
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI, Request, Response
from openai import AsyncOpenAI

from config import get_settings
from database import (
    add_message_log,
    add_reviewed_patient,
    create_session,
    delete_reviewed_phone,
    get_active_session_by_phone,
    get_all_reviewed_phones,
    get_session_by_id,
    get_session_messages,
    has_patient_already_reviewed,
    init_db,
    normalize_phone,
    update_session,
)
from wazzup_client import WazzupClient

settings = get_settings()
router = Router()
app = FastAPI(title="CityClinic Feedback Bot + Wazzup Webhook")
telegram_bot: Bot | None = None


class AdminStates(StatesGroup):
    waiting_patient_data = State()


def get_wazzup_client() -> WazzupClient:
    if not settings.wazzup_api_token:
        raise RuntimeError("Не задан WAZZUP_API_TOKEN в Railway Variables")
    return WazzupClient(settings.wazzup_api_token, settings.wazzup_api_base_url)


def parse_patient_data(text: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in text.split(",", maxsplit=1)]
    if len(parts) != 2:
        return None
    patient_name, phone = parts
    if not patient_name or not phone:
        return None
    return patient_name, phone


def admin_help_text() -> str:
    return (
        "Команды администратора:\n\n"
        "/new — начать отправку пациенту в WhatsApp через Wazzup\n"
        "/id — показать ID текущего Telegram-чата\n"
        "/reviewed — показать номера, которые уже оставляли отзыв\n"
        "/delete_reviewed +79991234567 — удалить номер из базы оставивших отзыв\n"
        "/wazzup_channels — показать каналы Wazzup и их channelId\n"
        "/setup_wazzup_webhook — подключить webhook Wazzup\n"
        "/wazzup_webhook — показать текущую настройку webhook Wazzup\n"
        "/health — проверить, что сервис жив"
    )


def yandex_instruction() -> str:
    return (
        "Нажмите на текст отзыва и удерживайте - он выделится; "
        "затем выберите «Скопировать» сверху в меню.\n"
        "После этого откройте Яндекс.Карты по ссылке внизу.\n\n"
        "Пожалуйста, выберите 5 ⭐⭐⭐⭐⭐ ,\n"
        "В текстовом поле для отзыва нажмите «вставить».\n"
        "Опубликуйте отзыв 😊\n\n"
        f"{settings.yandex_maps_review_url}"
    )
def first_whatsapp_message(patient_name: str) -> str:
    return (
        f"{patient_name}, спасибо Вам, что посетили {settings.clinic_name}!\n"
        "Мы надеемся, что Вы остались довольны нашей работой.\n"
        "Оцените, пожалуйста, Ваш визит от 1 до 5.\n\n"
        "Ответьте одной цифрой: 1, 2, 3, 4 или 5."
    )


def positive_review_request() -> str:
    return (
        "Оставьте, пожалуйста, отзыв о нашей работе.\n"
        "Напишите Ваш отзыв следующим сообщением или отправьте голосовое сообщение."
    )


def negative_feedback_request() -> str:
    return (
        "Напишите, пожалуйста, подробнее о Ваших впечатлениях и замечаниях.\n"
        "Можно написать текстом или отправить голосовое сообщение."
    )


def normalize_rating(text: str) -> int | None:
    cleaned = (text or "").strip()
    if cleaned in {"1", "2", "3", "4", "5"}:
        return int(cleaned)
    return None


async def send_to_admin_group(bot: Bot, session_id: int, title: str) -> None:
    session = await get_session_by_id(session_id)
    messages = await get_session_messages(session_id)
    if session:
        patient_block = (
            f"<b>{html.escape(title)}</b>\n\n"
            f"<b>Канал:</b> WhatsApp через Wazzup\n"
            f"<b>Пациент:</b> {html.escape(session['patient_name'])}\n"
            f"<b>Телефон:</b> {html.escape(session['phone'])}\n"
            f"<b>Оценка:</b> {session.get('rating') or 'не указана'}\n"
            f"<b>Wazzup channelId:</b> {html.escape(str(session.get('wazzup_channel_id') or 'не указан'))}\n\n"
        )
    else:
        patient_block = f"<b>{html.escape(title)}</b>\n\n"
    log_lines = []
    for item in messages:
        sender = "Бот" if item["sender"] == "bot" else "Пациент"
        log_lines.append(f"<b>{sender}:</b> {html.escape(item['text'])}")
    text = patient_block + "<b>Переписка:</b>\n" + "\n\n".join(log_lines)
    for i in range(0, len(text), 3900):
        await bot.send_message(settings.admin_group_id, text[i:i + 3900])


async def wazzup_send_to_patient(session: dict, text: str) -> Any:
    if not settings.wazzup_channel_id:
        raise RuntimeError("Не задан WAZZUP_CHANNEL_ID. Выполните /wazzup_channels и добавьте нужный channelId в Railway Variables.")
    return await get_wazzup_client().send_text(
        channel_id=settings.wazzup_channel_id,
        chat_id=normalize_phone(session["phone"]),
        text=text,
        crm_message_id=f"cityclinic-{session['id']}-{uuid4().hex[:10]}",
        clear_unanswered=False,
    )


async def transcribe_audio_url(url: str) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY не задан. Распознавание голоса отключено.")
    tmp_dir = Path("tmp_voice")
    tmp_dir.mkdir(exist_ok=True)
    file_path = tmp_dir / f"wazzup_{uuid4().hex}.ogg"
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(url)
        response.raise_for_status()
        file_path.write_bytes(response.content)
    openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    with file_path.open("rb") as audio_file:
        result = await openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file, language="ru")
    try:
        os.remove(file_path)
    except OSError:
        pass
    return result.text.strip()


async def finish_positive_review(session: dict, review_text: str) -> None:
    session_id = session["id"]
    review_text = review_text.strip()
    await update_session(session_id, review_text=review_text, status="positive_finished")
    await add_message_log(session_id, "patient", review_text)
    await add_reviewed_patient(
        phone=session["phone"],
        patient_name=session["patient_name"],
        session_id=session_id,
        review_text=review_text,
        rating=session.get("rating"),
    )

    message_1 = "Спасибо! Ниже текст Вашего отзыва:"
    await add_message_log(session_id, "bot", message_1)
    await wazzup_send_to_patient(session, message_1)

    message_2 = review_text
    await add_message_log(session_id, "bot", message_2)
    await wazzup_send_to_patient(session, message_2)

    message_3 = yandex_instruction()
    await add_message_log(session_id, "bot", message_3)
    await wazzup_send_to_patient(session, message_3)

    if telegram_bot:
        await send_to_admin_group(telegram_bot, session_id, "Положительный отзыв пациента")


async def finish_negative_feedback(session: dict, feedback_text: str) -> None:
    session_id = session["id"]
    feedback_text = feedback_text.strip()
    await update_session(session_id, review_text=feedback_text, status="negative_finished")
    await add_message_log(session_id, "patient", feedback_text)
    await add_reviewed_patient(
        phone=session["phone"],
        patient_name=session["patient_name"],
        session_id=session_id,
        review_text=feedback_text,
        rating=session.get("rating"),
    )

    final_text = (
        "Спасибо большое за Ваш отзыв! Нам очень важно Ваше мнение, "
        "мы будем стараться сделать нашу работу лучше."
    )
    await add_message_log(session_id, "bot", final_text)
    await wazzup_send_to_patient(session, final_text)

    if telegram_bot:
        await send_to_admin_group(telegram_bot, session_id, "Отзыв пациента с оценкой ниже 5")


async def process_whatsapp_message(session: dict, text: str) -> None:
    status = session.get("status")
    session_id = session["id"]

    if status == "waiting_rating":
        rating = normalize_rating(text)
        if rating is None:
            bot_text = "Пожалуйста, ответьте одной цифрой от 1 до 5."
            await add_message_log(session_id, "patient", text)
            await add_message_log(session_id, "bot", bot_text)
            await wazzup_send_to_patient(session, bot_text)
            return

        await update_session(session_id, rating=rating)
        await add_message_log(session_id, "patient", f"Оценка: {rating}")
        if rating == 5:
            bot_text = positive_review_request()
            await update_session(session_id, status="waiting_positive_review")
        else:
            bot_text = negative_feedback_request()
            await update_session(session_id, status="waiting_negative_feedback")
        await add_message_log(session_id, "bot", bot_text)
        fresh_session = await get_session_by_id(session_id) or session
        await wazzup_send_to_patient(fresh_session, bot_text)
        return

    if status == "waiting_positive_review":
        await finish_positive_review(session, text)
        return

    if status == "waiting_negative_feedback":
        await finish_negative_feedback(session, text)
        return

    bot_text = "Спасибо! Ваш отзыв уже получен."
    await add_message_log(session_id, "patient", text)
    await add_message_log(session_id, "bot", bot_text)
    await wazzup_send_to_patient(session, bot_text)


def extract_message_text(message: dict) -> str | None:
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def extract_message_audio_url(message: dict) -> str | None:
    message_type = str(message.get("type") or "").lower()
    content_uri = message.get("contentUri") or message.get("contentUrl") or message.get("url")
    if content_uri and message_type in {"audio", "voice", "ptt", "document"}:
        return str(content_uri)
    return None


def extract_message_phone(message: dict) -> str | None:
    value = message.get("chatId")
    if isinstance(value, str) and normalize_phone(value):
        return value
    contact = message.get("contact") or {}
    if isinstance(contact, dict):
        for key in ("phone", "name"):
            value = contact.get(key)
            if isinstance(value, str) and normalize_phone(value):
                return value
    return None


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "cityclinic-feedback-wazzup"}


@app.post("/wazzup/webhook")
async def wazzup_webhook(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=200)

    print(f"Wazzup webhook payload: {payload}")

    if payload.get("test") is True:
        return Response(status_code=200)

    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        return Response(status_code=200)

    processed = 0
    for message in messages:
        if not isinstance(message, dict):
            continue

        if message.get("chatType") != "whatsapp":
            continue
        if message.get("isEcho") is True:
            continue
        if message.get("status") not in ("inbound", None):
            continue

        phone = extract_message_phone(message)
        channel_id = message.get("channelId")
        if not phone:
            print(f"Wazzup webhook ignored: no phone in message={message}")
            continue

        session = await get_active_session_by_phone(phone, channel_id=channel_id if isinstance(channel_id, str) else None)
        if not session:
            print(f"Wazzup webhook ignored: no active session for phone={phone}, message={message}")
            continue

        text = extract_message_text(message)
        if not text:
            audio_url = extract_message_audio_url(message)
            if audio_url:
                try:
                    text = await transcribe_audio_url(audio_url)
                except Exception as exc:
                    print(f"Wazzup voice transcription error: {exc}")
                    await wazzup_send_to_patient(session, "Не получилось распознать голосовое сообщение. Пожалуйста, отправьте текстом.")
                    processed += 1
                    continue

        if not text:
            await wazzup_send_to_patient(session, "Пожалуйста, отправьте текстовое или голосовое сообщение.")
            processed += 1
            continue

        await process_whatsapp_message(session, text)
        processed += 1

    return Response(status_code=200)


@router.message(CommandStart())
async def start(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(admin_help_text())


@router.message(Command("new"))
async def new_feedback(message: types.Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_patient_data)
    await message.answer(
        "Отправьте данные пациента в формате:\n\n"
        "<code>Иван Иванович, +79991234567</code>\n\n"
        "После этого сообщение пациенту уйдет в WhatsApp через Wazzup."
    )


@router.message(Command("health"))
async def health_command(message: types.Message) -> None:
    await message.answer("Сервис работает ✅")


@router.message(Command("id"))
async def get_chat_id(message: types.Message) -> None:
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>")


@router.message(Command("reviewed"))
async def show_reviewed(message: types.Message) -> None:
    rows = await get_all_reviewed_phones()
    if not rows:
        await message.answer("В базе пока нет номеров пациентов, уже оставивших отзыв.")
        return
    lines = ["Номера пациентов, уже оставивших отзыв:"]
    for row in rows[:100]:
        lines.append(
            f"• {html.escape(row['original_phone'])} — {html.escape(row.get('patient_name') or 'без имени')} "
            f"— оценка: {row.get('rating') or '—'}"
        )
    if len(rows) > 100:
        lines.append(f"\nПоказаны первые 100 из {len(rows)} записей.")
    await message.answer("\n".join(lines))


@router.message(Command("delete_reviewed"))
async def delete_reviewed(message: types.Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите номер после команды, например:\n<code>/delete_reviewed +79991234567</code>")
        return
    deleted = await delete_reviewed_phone(parts[1])
    if deleted:
        await message.answer(f"Номер удален из базы: <code>{html.escape(parts[1])}</code>")
    else:
        await message.answer("Такого номера в базе не найдено.")


@router.message(Command("wazzup_channels"))
async def wazzup_channels(message: types.Message) -> None:
    try:
        data = await get_wazzup_client().list_channels()
    except Exception as exc:
        await message.answer(f"Ошибка Wazzup API: <code>{html.escape(str(exc))}</code>")
        return
    if not data:
        await message.answer("Каналы Wazzup не найдены.")
        return
    lines = ["Каналы Wazzup:"]
    for item in data:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"• channelId: <code>{html.escape(str(item.get('channelId')))}</code>; "
            f"transport: <code>{html.escape(str(item.get('transport')))}</code>; "
            f"plainId: {html.escape(str(item.get('plainId') or ''))}; "
            f"state: {html.escape(str(item.get('state') or ''))}"
        )
    await message.answer("\n".join(lines[:50]))


@router.message(Command("setup_wazzup_webhook"))
async def setup_wazzup_webhook(message: types.Message) -> None:
    if not settings.public_base_url:
        await message.answer("Сначала задайте PUBLIC_BASE_URL в Railway, например https://your-app.up.railway.app")
        return
    url = f"{settings.public_base_url}/wazzup/webhook"
    try:
        result = await get_wazzup_client().setup_webhook(url)
    except Exception as exc:
        await message.answer(f"Ошибка подключения webhook: <code>{html.escape(str(exc))}</code>")
        return
    await message.answer(f"Webhook Wazzup подключен:\n<code>{html.escape(str(result))}</code>\n\nURL:\n<code>{html.escape(url)}</code>")


@router.message(Command("wazzup_webhook"))
async def wazzup_webhook_info(message: types.Message) -> None:
    try:
        result = await get_wazzup_client().get_webhooks()
    except Exception as exc:
        await message.answer(f"Ошибка получения webhook: <code>{html.escape(str(exc))}</code>")
        return
    await message.answer(f"Webhook Wazzup:\n<code>{html.escape(str(result))}</code>")


@router.message(AdminStates.waiting_patient_data, F.text)
async def receive_patient_data(message: types.Message, state: FSMContext) -> None:
    parsed = parse_patient_data(message.text)
    if not parsed:
        await message.answer(
            "Не получилось разобрать данные. Отправьте, пожалуйста, так:\n\n"
            "<code>Иван Иванович, +79991234567</code>"
        )
        return

    patient_name, phone = parsed
    phone_normalized = normalize_phone(phone)

    if not phone_normalized:
        await message.answer("Не получилось разобрать телефон. Укажите номер в формате +79991234567.")
        return

    if await has_patient_already_reviewed(phone):
        await message.answer("Пациент с таким телефоном уже оставлял отзыв. Сообщение в WhatsApp не отправлено.")
        await state.clear()
        return

    if not settings.wazzup_channel_id:
        await message.answer("Не задан WAZZUP_CHANNEL_ID. Выполните /wazzup_channels и добавьте нужный channelId в Railway Variables.")
        return

    session_id = await create_session(
        message.from_user.id,
        patient_name,
        phone,
        channel="whatsapp",
        wazzup_channel_id=settings.wazzup_channel_id,
    )
    session = await get_session_by_id(session_id)
    bot_text = first_whatsapp_message(patient_name)
    await add_message_log(session_id, "bot", bot_text)

    try:
        await wazzup_send_to_patient(session, bot_text)
    except Exception as exc:
        await message.answer(f"Не удалось отправить сообщение в WhatsApp через Wazzup:\n<code>{html.escape(str(exc))}</code>")
        return

    await state.clear()
    await message.answer(
        "Сообщение пациенту отправлено в WhatsApp через Wazzup.\n"
        f"Пациент: <b>{html.escape(patient_name)}</b>\n"
        f"Телефон: <code>{html.escape(phone)}</code>"
    )


@router.message()
async def fallback(message: types.Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нажмите /new, чтобы начать сбор обратной связи через WhatsApp.")
    else:
        await message.answer("Пожалуйста, следуйте текущему шагу сценария.")


async def run_fastapi() -> None:
    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def run_telegram() -> None:
    global telegram_bot
    telegram_bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    print("CityClinic Telegram admin bot started")
    await dp.start_polling(telegram_bot)


async def main() -> None:
    await init_db()
    print("CityClinic feedback service started with Wazzup")
    await asyncio.gather(run_fastapi(), run_telegram())


if __name__ == "__main__":
    asyncio.run(main())
