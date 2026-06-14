import asyncio
import html
import json
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
from fastapi.responses import RedirectResponse
from openai import AsyncOpenAI

from config import get_settings
from database import (
    add_message_log,
    add_reviewed_patient,
    add_ai_usage,
    create_session,
    delete_reviewed_phone,
    get_active_session_by_phone,
    get_all_reviewed_phones,
    get_session_by_id,
    get_session_messages,
    get_latest_session_by_phone,
    mark_yandex_clicked,
    has_patient_already_reviewed,
    init_db,
    normalize_phone,
    update_session,
    update_wazzup_delivery,
)
from wazzup_client import WazzupClient

settings = get_settings()
router = Router()
app = FastAPI(title="CityClinic Feedback Bot + Wazzup Webhook")
telegram_bot: Bot | None = None


class AdminStates(StatesGroup):
    choosing_scenario = State()
    waiting_patient_data = State()
    waiting_cashback_email_data = State()


def get_wazzup_client() -> WazzupClient:
    if not settings.wazzup_api_token:
        raise RuntimeError("Не задан WAZZUP_API_TOKEN в Railway Variables")
    return WazzupClient(settings.wazzup_api_token, settings.wazzup_api_base_url)


def parse_patient_data(text: str) -> tuple[str, str] | None:
    """
    Поддерживает оба формата:
    Иван Иванович, +79991234567
    +79991234567, Иван Иванович
    Возвращает: (patient_name, phone).
    """
    parts = [part.strip() for part in (text or "").split(",", maxsplit=1)]
    if len(parts) != 2:
        return None
    left, right = parts
    if not left or not right:
        return None

    left_phone = normalize_phone(left)
    right_phone = normalize_phone(right)
    if left_phone and not right_phone:
        return right, left
    if right_phone and not left_phone:
        return left, right

    # fallback для старого формата: имя, телефон
    return left, right




def parse_cashback_email_data(text: str) -> tuple[str, str] | None:
    """
    Форматы:
    Иван Иванович, patient@example.com
    Иван, patient@example.com
    Возвращает: (patient_name, email).
    """
    parts = [part.strip() for part in (text or "").split(",", maxsplit=1)]
    if len(parts) != 2:
        return None
    patient_name, email_addr = parts
    if not patient_name or not email_addr:
        return None
    if "@" not in email_addr or "." not in email_addr.split("@")[-1]:
        return None
    return patient_name, email_addr


def cashback_email_subject() -> str:
    return "У вас накоплено 1020 руб. кэшбэка в СитиКлиник Коньково"


def cashback_email_html(patient_name: str) -> str:
    safe_name = html.escape(patient_name.strip() or "")
    image_block = ""
    if settings.cashback_email_image_url:
        image_url = html.escape(settings.cashback_email_image_url)
        image_block = f"""
        <div style="margin:24px 0; text-align:center;">
          <img
            src="{image_url}"
            alt="СитиКлиник Коньково"
            width="556"
            style="width:100%; max-width:556px; border-radius:18px; display:block; margin:0 auto; box-shadow:0 6px 20px rgba(0,0,0,0.12);"
          >
          <div style="margin-top:10px; color:#6b7280; font-size:14px; line-height:1.4;">
              СитиКлиник Коньково — всегда рады видеть вас ❤️
          </div>
        </div>
"""

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Ваш бонусный счёт в СитиКлиник</title>
</head>
<body style="margin:0; padding:0; background:#f4f7fb; font-family:Arial, sans-serif; color:#1f2937;">

  <div style="max-width:620px; margin:0 auto; padding:24px 12px;">

    <div style="background:#ffffff; border-radius:20px; overflow:hidden; box-shadow:0 8px 28px rgba(30,58,95,0.10);">

      <div style="background:#1f6f8b; padding:28px 32px; color:#ffffff;">
        <div style="font-size:14px; opacity:0.9;">Медицинский центр</div>
        <div style="font-size:28px; font-weight:bold; margin-top:4px;">СитиКлиник Коньково</div>
      </div>

      <div style="padding:32px;">

        <h1 style="margin:0 0 16px; font-size:26px; line-height:1.25; color:#1f2937;">
          Здравствуйте, {safe_name}! 👋
        </h1>

        {image_block}

        <p style="font-size:17px; line-height:1.55; margin:0 0 20px;">
          На вашем бонусном счёте сейчас:
        </p>

        <div style="background:#eef9f6; border:1px solid #c8eee4; border-radius:18px; padding:24px; text-align:center; margin:20px 0;">
          <div style="font-size:15px; color:#42766c;">Доступно бонусов</div>
          <div style="font-size:42px; font-weight:bold; color:#16725f; margin-top:6px;">1 020 ₽</div>
        </div>

        <div style="background:#fff8ec; border-radius:16px; padding:20px; margin:22px 0;">
          <div style="font-weight:bold; font-size:18px; margin-bottom:12px;">💳 Срок действия бонусов:</div>
          <div style="font-size:16px; line-height:1.7;">
            • <strong>500 ₽</strong> действуют до <strong>15 июля</strong><br>
            • <strong>520 ₽</strong> действуют до <strong>15 августа</strong>
          </div>
        </div>

        <p style="font-size:16px; line-height:1.55;">
          Бонусами можно оплатить любые услуги нашей клиники.
        </p>

        <div style="background:#f0f7ff; border-radius:16px; padding:24px; margin:24px 0; text-align:center;">

          <div style="font-size:18px; line-height:1.6; color:#1f2937;">
            🎁 Запишитесь онлайн или по телефону до конца июня и мы дополнительно начислим вам
            <strong>500 ₽ бонусов</strong> со сроком действия до <strong>15 июля</strong>.
          </div>

          <div style="margin-top:24px;">

            <a href="https://policlinica24.ru/zapis-online"
               target="_blank"
               style="display:inline-block; background:#f46e3f; color:#ffffff; text-decoration:none; padding:15px 26px; border-radius:999px; font-size:16px; font-weight:bold; margin:0 6px 10px 6px;">
              Записаться онлайн
            </a>

            <a href="tel:+74954202200"
               style="display:inline-block; background:#1f6f8b; color:#ffffff; text-decoration:none; padding:15px 26px; border-radius:999px; font-size:16px; font-weight:bold; margin:0 6px 10px 6px;">
              Записаться по телефону
            </a>

          </div>

        </div>

        <p style="font-size:15px; color:#6b7280; line-height:1.5; margin-top:28px;">
          Телефон: <a href="tel:+74954202200" style="color:#1f6f8b;">+7 (495) 420-22-00</a><br>
          Будем рады видеть вас снова! ❤️
        </p>

      </div>
    </div>

    <div style="font-size:12px; color:#9ca3af; line-height:1.5; text-align:center; padding:18px;">
      Вы получили это письмо, потому что ранее обращались в медицинский центр СитиКлиник Коньково.
    </div>

  </div>

</body>
</html>"""


async def send_cashback_email(patient_name: str, to_email: str) -> None:
    """Send cashback email through Resend API."""
    if not settings.resend_api_key:
        raise RuntimeError("Не задан RESEND_API_KEY в Railway Variables")
    if not settings.email_from:
        raise RuntimeError("Не задан EMAIL_FROM в Railway Variables")

    text_content = (
        f"Здравствуйте, {patient_name}!\n\n"
        "У вас накоплено 1020 руб. кэшбэка в СитиКлиник Коньково.\n"
        "Запишитесь онлайн: https://policlinica24.ru/zapis-online\n"
        "Телефон: +7 (495) 420-22-00"
    )

    payload = {
        "from": f"СитиКлиник Коньково <{settings.email_from}>",
        "to": [to_email],
        "subject": cashback_email_subject(),
        "html": cashback_email_html(patient_name),
        "text": text_content,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code >= 400:
        raise RuntimeError(f"Resend API error {response.status_code}: {response.text}")


def admin_help_text() -> str:
    return (
        "Команды администратора:\n\n"
        "/new — выбрать сценарий: отзыв или кэшбэк-рассылка\n"
        "/review — отправить пациенту сценарий сбора отзыва\n"
        "/cashback — отправить пациенту кэшбэк-рассылку в WhatsApp\n"
        "/cashback_email — отправить пациенту кэшбэк-письмо на email\n"
        "/id — показать ID текущего Telegram-чата\n"
        "/reviewed — показать номера, которые уже оставляли отзыв\n"
        "/delete_reviewed +79991234567 — удалить номер из базы оставивших отзыв\n"
        "/wazzup_channels — показать каналы Wazzup и их channelId\n"
        "/setup_wazzup_webhook — подключить webhook Wazzup\n"
        "/wazzup_webhook — показать текущую настройку webhook Wazzup\n"
        "/health — проверить, что сервис жив"
    )


def first_whatsapp_message(patient_name: str) -> str:
    return (
        f"{patient_name}, благодарим Вас за доверие! 🙏🏻❤️\n\n"
        "Оцените, пожалуйста, качество нашей работы👩‍⚕️\n\n"
        "Напишите, пожалуйста, в ответном сообщении только цифру:\n\n"
        "*5* отлично 🌟\n"
        "*4* хорошо 👍\n"
        "*3* скорее, плохо 🤔\n"
        "*2* плохо 😕\n"
        "*1* ужасно 😞\n\n"
        "После этого Вам придёт сообщение, в ответ на которое Вы сможете "
        "написать нам свой отзыв о визите 📝, на который мы обязательно ответим 💬"
    )

def scenario_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text="⭐ Отзыв", callback_data="scenario:review"),
                types.InlineKeyboardButton(text="🎁 Кэшбэк", callback_data="scenario:cashback"),
            ],
            [
                types.InlineKeyboardButton(text="📧 Кэшбэк почта", callback_data="scenario:cashback_email"),
            ]
        ]
    )


def cashback_first_message(patient_name: str) -> str:
    return (
        f"Здравствуйте, {patient_name}! 👋\n\n"
        "На вашем бонусном счёте в медицинском центре СитиКлиник Коньково сейчас *1 020 ₽*.\n\n"
        "💳 Из них:\n\n"
        "• *500 ₽* действуют до 15 июля\n"
        "• *520 ₽* действуют до 15 августа\n\n"
        "Бонусами можно оплатить любые услуги нашей клиники.\n\n"
        "📋 Подскажите, что сейчас для вас наиболее актуально?\n\n"
        "1️⃣ Консультация врача\n\n"
        "2️⃣ Анализы\n\n"
        "3️⃣ УЗИ или диагностика\n\n"
        "Просто отправьте в ответ цифру *1, 2 или 3*.\n\n"
        "🎁 В знак благодарности за ответ мы дополнительно начислим вам *500 ₽ бонусов* "
        "со сроком действия до 15 июля.\n\n"
        "Будем рады видеть вас снова! ❤️"
    )


def cashback_bonus_message() -> str:
    return (
        "Спасибо за ответ! 😊\n\n"
        "Мы уже начислили вам дополнительные 500 ₽ бонусов со сроком действия до 15 июля.\n\n"
        "Если захотите записаться на приём, анализы или диагностику, просто напишите нам "
        "в этом чате или позвоните по телефону +7(495)420-22-00 — поможем подобрать удобное время."
    )


def yandex_tracking_url(session_id: int) -> str:
    if settings.public_base_url:
        return f"{settings.public_base_url}/yandex/{session_id}"
    return settings.yandex_maps_review_url


def positive_review_request(session_id: int) -> str:
    return (
        "Очень рады, что Вам у нас понравилось! Если Вы еще не оставляли отзыв о нас "
        f"на Яндекс Картах по ссылке\n{yandex_tracking_url(session_id)},\n"
        "то очень просим Вас сделать это. Можно даже просто пару слов написать. "
        "Врачи и администраторы видят все отзывы и им будет очень приятно 🤗"
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


def estimate_transcription_tokens(text: str) -> int:
    """
    Whisper API не возвращает точное число токенов в ответе.
    Поэтому для отчета используем оценку по длине распознанного текста.
    Если ИИ не включался, в отчете будет 0.
    """
    clean_text = (text or "").strip()
    if not clean_text:
        return 0
    return max(1, round(len(clean_text) / 4))


async def safe_send_admin_report(session_id: int, title: str) -> None:
    if not telegram_bot:
        print(f"Admin report skipped: telegram_bot is not initialized, session_id={session_id}")
        return
    try:
        await send_to_admin_group(telegram_bot, session_id, title)
    except Exception as exc:
        print(f"Admin report send error for session_id={session_id}: {exc}")


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
            f"<b>Wazzup channelId:</b> {html.escape(str(session.get('wazzup_channel_id') or 'не указан'))}\n"
            f"<b>ИИ для распознавания голоса:</b> {'да' if session.get('ai_was_used') else 'нет'}\n"
            f"<b>Токены ИИ:</b> {int(session.get('ai_tokens_used') or 0)}\n\n"
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


def _short_json(value: Any, limit: int = 2500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text[:limit]


def extract_wazzup_message_id(response: Any) -> str | None:
    if isinstance(response, dict):
        for key in ("messageId", "message_id", "id", "uuid"):
            value = response.get(key)
            if value:
                return str(value)
        data = response.get("data")
        if isinstance(data, dict):
            for key in ("messageId", "message_id", "id", "uuid"):
                value = data.get(key)
                if value:
                    return str(value)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            for key in ("messageId", "message_id", "id", "uuid"):
                value = data[0].get(key)
                if value:
                    return str(value)
        messages = response.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            for key in ("messageId", "message_id", "id", "uuid"):
                value = messages[0].get(key)
                if value:
                    return str(value)
    return None


async def send_whatsapp_status_to_admin(
    session: dict,
    *,
    wazzup_status: str,
    message_id: str | None = None,
    delivery_status: str | None = None,
    response: Any = None,
) -> None:
    if not telegram_bot:
        return
    lines = [
        "<b>WhatsApp отправка</b>",
        "",
        f"<b>Пациент:</b> {html.escape(str(session.get('patient_name') or ''))}",
        f"<b>Телефон:</b> {html.escape(str(session.get('phone') or ''))}",
        "",
        f"<b>Wazzup status:</b> {html.escape(wazzup_status)}",
        f"<b>Message ID:</b> {html.escape(str(message_id or 'не получен'))}",
        f"<b>Delivery status:</b> {html.escape(str(delivery_status or 'accepted'))}",
    ]
    if response is not None:
        lines += ["", "<b>Wazzup response:</b>", f"<code>{html.escape(_short_json(response, 1200))}</code>"]
    try:
        await telegram_bot.send_message(settings.admin_group_id, "\n".join(lines))
    except Exception as exc:
        print(f"WhatsApp status admin notify error: {exc}")


async def wazzup_send_to_patient(session: dict, text: str, *, notify_admin: bool = False) -> Any:
    if not settings.wazzup_channel_id:
        raise RuntimeError("Не задан WAZZUP_CHANNEL_ID. Выполните /wazzup_channels и добавьте нужный channelId в Railway Variables.")
    crm_message_id = f"cityclinic-{session['id']}-{uuid4().hex[:10]}"
    response = await get_wazzup_client().send_text(
        channel_id=settings.wazzup_channel_id,
        chat_id=normalize_phone(session["phone"]),
        text=text,
        crm_message_id=crm_message_id,
        clear_unanswered=False,
    )
    message_id = extract_wazzup_message_id(response)
    await update_wazzup_delivery(
        session["id"],
        message_id=message_id,
        crm_message_id=crm_message_id,
        status="accepted",
        response=_short_json(response),
    )
    if notify_admin:
        await send_whatsapp_status_to_admin(
            session,
            wazzup_status="accepted",
            message_id=message_id or crm_message_id,
            delivery_status="accepted",
            response=response,
        )
    return response


async def transcribe_audio_url(url: str) -> tuple[str, int]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY не задан. Распознавание голоса отключено.")
    tmp_dir = Path("tmp_voice")
    tmp_dir.mkdir(exist_ok=True)
    file_path = tmp_dir / f"wazzup_{uuid4().hex}.ogg"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url)
            response.raise_for_status()
            file_path.write_bytes(response.content)

        openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
        with file_path.open("rb") as audio_file:
            result = await openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru",
            )
        text = (result.text or "").strip()
        return text, estimate_transcription_tokens(text)
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass


async def finish_positive_rating(session: dict) -> None:
    session_id = session["id"]
    rating = int(session.get("rating") or 5)

    bot_text = positive_review_request(session_id)
    await update_session(session_id, rating=rating, review_text=None, status="positive_finished")
    await add_message_log(session_id, "bot", bot_text)
    await wazzup_send_to_patient(session, bot_text)

    await add_reviewed_patient(
        phone=session["phone"],
        patient_name=session["patient_name"],
        session_id=session_id,
        review_text=None,
        rating=rating,
    )

    await safe_send_admin_report(session_id, "Пациент поставил оценку 5")

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

    await safe_send_admin_report(session_id, "Отзыв пациента с оценкой ниже 5")


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

        fresh_session = await get_session_by_id(session_id) or session
        if rating == 5:
            fresh_session = dict(fresh_session)
            fresh_session["rating"] = rating
            await finish_positive_rating(fresh_session)
            return

        bot_text = negative_feedback_request()
        await update_session(session_id, status="waiting_negative_feedback")
        await add_message_log(session_id, "bot", bot_text)
        fresh_session = await get_session_by_id(session_id) or session
        await wazzup_send_to_patient(fresh_session, bot_text)
        return

    if status == "waiting_negative_feedback":
        await finish_negative_feedback(session, text)
        return

    if status == "cashback_waiting_reply":
        cleaned = (text or "").strip()
        await add_message_log(session_id, "patient", cleaned)
        if cleaned not in {"1", "2", "3"}:
            bot_text = "Пожалуйста, отправьте в ответ только цифру 1, 2 или 3."
            await add_message_log(session_id, "bot", bot_text)
            await wazzup_send_to_patient(session, bot_text)
            return

        bot_text = cashback_bonus_message()
        await update_session(session_id, status="cashback_bonus_sent", review_text=f"cashback_choice:{cleaned}")
        await add_message_log(session_id, "bot", bot_text)
        fresh_session = await get_session_by_id(session_id) or session
        await wazzup_send_to_patient(fresh_session, bot_text)
        await safe_send_admin_report(session_id, f"Пациент ответил на кэшбэк-рассылку: {cleaned}")
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


@app.get("/yandex/{session_id}")
async def yandex_redirect(session_id: int):
    session, is_first_click = await mark_yandex_clicked(session_id)

    if session and is_first_click and telegram_bot:
        try:
            text = (
                "<b>Пациент перешел по ссылке на Яндекс.Карты</b>\n\n"
                f"<b>Пациент:</b> {html.escape(session['patient_name'])}\n"
                f"<b>Телефон:</b> {html.escape(session['phone'])}\n"
                f"<b>Оценка:</b> {session.get('rating') or 'не указана'}"
            )
            await telegram_bot.send_message(settings.admin_group_id, text)
        except Exception as exc:
            print(f"Yandex click notification error for session_id={session_id}: {exc}")

    return RedirectResponse(settings.yandex_maps_review_url, status_code=302)



def extract_delivery_status_event(item: dict) -> dict | None:
    """
    Wazzup may send outbound message statuses in different shapes.
    We accept common fields and ignore inbound text messages here.
    """
    if not isinstance(item, dict):
        return None
    status = item.get("status") or item.get("messageStatus") or item.get("deliveryStatus")
    status = str(status or "").lower().strip()
    if status not in {"sent", "delivered", "read", "failed"}:
        return None
    phone = extract_message_phone(item)
    message_id = (
        item.get("messageId")
        or item.get("message_id")
        or item.get("id")
        or item.get("uuid")
        or item.get("crmMessageId")
    )
    channel_id = item.get("channelId")
    return {
        "status": status,
        "phone": phone,
        "message_id": str(message_id) if message_id else None,
        "channel_id": channel_id if isinstance(channel_id, str) else None,
        "raw": item,
    }


def iter_wazzup_status_items(payload: dict) -> list[dict]:
    items: list[dict] = []
    for key in ("statuses", "messages", "messageStatuses", "events"):
        value = payload.get(key)
        if isinstance(value, list):
            items.extend([x for x in value if isinstance(x, dict)])
    # Some webhook variants send one event as root object.
    if any(k in payload for k in ("status", "messageStatus", "deliveryStatus")):
        items.append(payload)
    return items


async def process_delivery_status_event(event: dict) -> bool:
    phone = event.get("phone")
    status = event.get("status")
    if not phone or not status:
        return False
    session = await get_active_session_by_phone(phone, channel_id=event.get("channel_id"))
    if not session:
        # Status can arrive after the scenario is finished. Try the latest session by phone.
        session = await get_latest_session_by_phone(phone, channel_id=event.get("channel_id"))
    if not session:
        print(f"Wazzup delivery status ignored: no active session for phone={phone}, event={event}")
        return False
    await update_wazzup_delivery(
        session["id"],
        message_id=event.get("message_id"),
        status=status,
        response=_short_json(event.get("raw")),
    )
    await send_whatsapp_status_to_admin(
        session,
        wazzup_status="accepted",
        message_id=event.get("message_id") or session.get("last_wazzup_message_id") or session.get("last_wazzup_crm_message_id"),
        delivery_status=status,
        response=event.get("raw"),
    )
    return True



@app.post("/wazzup/webhook")
async def wazzup_webhook(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=200)

    print(f"Wazzup webhook payload: {payload}")

    if payload.get("test") is True:
        return Response(status_code=200)

    # First process outbound delivery/read/failed statuses, if Wazzup sent them.
    for item in iter_wazzup_status_items(payload):
        event = extract_delivery_status_event(item)
        if event:
            try:
                await process_delivery_status_event(event)
            except Exception as exc:
                print(f"Wazzup delivery status processing error: {exc}, event={event}")

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
                    text, ai_tokens_used = await transcribe_audio_url(audio_url)
                    await add_ai_usage(session["id"], ai_tokens_used)
                    session = await get_session_by_id(session["id"]) or session
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


async def ask_patient_data(message: types.Message, state: FSMContext, scenario: str) -> None:
    await state.update_data(scenario=scenario)
    await state.set_state(AdminStates.waiting_patient_data)
    scenario_title = "сбор отзыва" if scenario == "review" else "кэшбэк-рассылка"
    await message.answer(
        f"Сценарий: <b>{scenario_title}</b>\n\n"
        "Отправьте данные пациента в формате:\n\n"
        "<code>Иван Иванович, +79991234567</code>\n"
        "<code>Иван, +79991234567</code>\n\n"
        "Также поддерживается формат:\n"
        "<code>+79991234567, Иван Иванович</code>\n"
        "<code>+79991234567, Иван</code>\n\n"
        "После этого сообщение пациенту уйдет в WhatsApp через Wazzup."
    )


@router.message(Command("new"))
async def new_feedback(message: types.Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.choosing_scenario)
    await message.answer(
        "Выберите сценарий отправки пациенту:",
        reply_markup=scenario_keyboard(),
    )


@router.message(Command("review"))
async def new_review_direct(message: types.Message, state: FSMContext) -> None:
    await ask_patient_data(message, state, "review")


@router.message(Command("cashback"))
async def new_cashback_direct(message: types.Message, state: FSMContext) -> None:
    await ask_patient_data(message, state, "cashback")


@router.message(Command("cashback_email"))
async def new_cashback_email_direct(message: types.Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_cashback_email_data)
    await message.answer(
        "Сценарий: <b>кэшбэк-письмо на email</b>\n\n"
        "Отправьте данные пациента в формате:\n\n"
        "<code>Иван Иванович, patient@example.com</code>\n"
        "<code>Иван, patient@example.com</code>"
    )


@router.callback_query(F.data == "scenario:review")
async def scenario_review(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await ask_patient_data(callback.message, state, "review")


@router.callback_query(F.data == "scenario:cashback")
async def scenario_cashback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await ask_patient_data(callback.message, state, "cashback")


@router.callback_query(F.data == "scenario:cashback_email")
async def scenario_cashback_email(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_cashback_email_data)
    await callback.message.answer(
        "Сценарий: <b>кэшбэк-письмо на email</b>\n\n"
        "Отправьте данные пациента в формате:\n\n"
        "<code>Иван Иванович, patient@example.com</code>\n"
        "<code>Иван, patient@example.com</code>"
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


@router.message(AdminStates.waiting_cashback_email_data, F.text)
async def receive_cashback_email_data(message: types.Message, state: FSMContext) -> None:
    parsed = parse_cashback_email_data(message.text)
    if not parsed:
        await message.answer(
            "Не получилось разобрать данные. Отправьте, пожалуйста, так:\n\n"
            "<code>Иван Иванович, patient@example.com</code>\n"
            "или:\n"
            "<code>Иван, patient@example.com</code>"
        )
        return

    patient_name, email_addr = parsed

    try:
        await send_cashback_email(patient_name, email_addr)
    except Exception as exc:
        await message.answer(f"Не удалось отправить письмо:\n<code>{html.escape(str(exc))}</code>")
        return

    await state.clear()
    await message.answer(
        "Кэшбэк-письмо отправлено ✅\n\n"
        f"Пациент: <b>{html.escape(patient_name)}</b>\n"
        f"Email: <code>{html.escape(email_addr)}</code>\n"
        f"Тема: <b>{html.escape(cashback_email_subject())}</b>"
    )


@router.message(AdminStates.waiting_patient_data, F.text)
async def receive_patient_data(message: types.Message, state: FSMContext) -> None:
    parsed = parse_patient_data(message.text)
    if not parsed:
        await message.answer(
            "Не получилось разобрать данные. Отправьте, пожалуйста, так:\n\n"
            "<code>Иван Иванович, +79991234567</code>\n"
            "или:\n"
            "<code>Иван, +79991234567</code>"
        )
        return

    patient_name, phone = parsed
    phone_normalized = normalize_phone(phone)

    if not phone_normalized:
        await message.answer("Не получилось разобрать телефон. Укажите номер в формате +79991234567.")
        return

    data = await state.get_data()
    scenario = data.get("scenario") or "review"

    if scenario == "review" and await has_patient_already_reviewed(phone):
        await message.answer("Пациент с таким телефоном уже оставлял отзыв. Сообщение в WhatsApp не отправлено.")
        await state.clear()
        return

    if not settings.wazzup_channel_id:
        await message.answer("Не задан WAZZUP_CHANNEL_ID. Выполните /wazzup_channels и добавьте нужный channelId в Railway Variables.")
        return

    initial_status = "waiting_rating" if scenario == "review" else "cashback_waiting_reply"
    session_id = await create_session(
        message.from_user.id,
        patient_name,
        phone,
        channel="whatsapp",
        wazzup_channel_id=settings.wazzup_channel_id,
        scenario=scenario,
        initial_status=initial_status,
    )
    session = await get_session_by_id(session_id)
    bot_text = first_whatsapp_message(patient_name) if scenario == "review" else cashback_first_message(patient_name)
    await add_message_log(session_id, "bot", bot_text)

    try:
        await wazzup_send_to_patient(session, bot_text, notify_admin=True)
    except Exception as exc:
        await message.answer(f"Не удалось отправить сообщение в WhatsApp через Wazzup:\n<code>{html.escape(str(exc))}</code>")
        return

    await state.clear()
    scenario_title = "сбор отзыва" if scenario == "review" else "кэшбэк-рассылка"
    await message.answer(
        "Сообщение пациенту отправлено в WhatsApp через Wazzup.\n"
        f"Сценарий: <b>{scenario_title}</b>\n"
        f"Пациент: <b>{html.escape(patient_name)}</b>\n"
        f"Телефон: <code>{html.escape(phone)}</code>"
    )


@router.message()
async def fallback(message: types.Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нажмите /new, чтобы выбрать сценарий отправки через WhatsApp.")
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
