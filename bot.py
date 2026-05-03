import asyncio
import html
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai import AsyncOpenAI

from config import get_settings
from database import (
    add_message_log,
    add_reviewed_patient,
    create_session,
    delete_reviewed_patient,
    get_all_reviewed_patients,
    get_latest_session,
    get_session_messages,
    has_patient_already_reviewed,
    init_db,
    update_session,
)

settings = get_settings()
router = Router()


class FeedbackStates(StatesGroup):
    waiting_patient_data = State()
    waiting_rating = State()
    waiting_positive_review = State()
    waiting_negative_feedback = State()


def rating_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value in range(1, 6):
        builder.button(text=str(value), callback_data=f"rating:{value}")
    builder.adjust(5)
    return builder.as_markup()


def voice_hint_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продиктовать голосом", callback_data="voice_hint")]
        ]
    )


def yandex_review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть Яндекс.Карты", url=settings.yandex_maps_review_url)]
        ]
    )


def parse_patient_data(text: str) -> tuple[str, str] | None:
    """
    Ожидаемый формат:
    Иван Иванович, +79991234567
    """
    parts = [part.strip() for part in text.split(",", maxsplit=1)]
    if len(parts) != 2:
        return None

    patient_name, phone = parts
    if not patient_name or not phone:
        return None

    return patient_name, phone


def is_admin_chat(message: types.Message) -> bool:
    """Команды просмотра/удаления базы разрешены только в админ-группе."""
    return message.chat.id == settings.admin_group_id


def format_reviewed_patient_row(row: dict) -> str:
    name = row.get("patient_name") or "без имени"
    phone = row.get("original_phone") or row.get("phone_normalized") or "без телефона"
    rating = row.get("rating") or "—"
    created_at = (row.get("created_at") or "")[:19].replace("T", " ")
    return (
        f"ID: {row.get('id')}\n"
        f"Пациент: {html.escape(str(name))}\n"
        f"Телефон: <code>{html.escape(str(phone))}</code>\n"
        f"Нормализованный: <code>{html.escape(str(row.get('phone_normalized') or ''))}</code>\n"
        f"Оценка: {html.escape(str(rating))}\n"
        f"Дата: {html.escape(created_at)}"
    )


async def send_to_admin_group(bot: Bot, session_id: int, title: str) -> None:
    session = await get_session_by_id_safe(session_id)
    messages = await get_session_messages(session_id)

    if session:
        patient_block = (
            f"<b>{html.escape(title)}</b>\n\n"
            f"<b>Пациент:</b> {html.escape(session['patient_name'])}\n"
            f"<b>Телефон:</b> {html.escape(session['phone'])}\n"
            f"<b>Оценка:</b> {session.get('rating') or 'не указана'}\n\n"
        )
    else:
        patient_block = f"<b>{html.escape(title)}</b>\n\n"

    log_lines = []
    for item in messages:
        sender = "Бот" if item["sender"] == "bot" else "Пациент"
        log_lines.append(f"<b>{sender}:</b> {html.escape(item['text'])}")

    text = patient_block + "<b>Переписка:</b>\n" + "\n\n".join(log_lines)

    # Telegram ограничивает длину сообщения. Если лог большой, режем на части.
    max_len = 3900
    chunks = [text[i : i + max_len] for i in range(0, len(text), max_len)]
    for chunk in chunks:
        await bot.send_message(settings.admin_group_id, chunk)


async def get_session_by_id_safe(session_id: int) -> dict | None:
    # В текущем сценарии достаточно latest_session, но для отчета надежнее прочитать напрямую.
    import aiosqlite
    from database import DB_PATH

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM feedback_sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def transcribe_voice(bot: Bot, voice: types.Voice) -> str:
    """
    Скачивает голосовое сообщение Telegram и отправляет его в OpenAI Whisper.
    Telegram voice приходит как OGG/OPUS. Whisper умеет распознавать такие файлы.
    """
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY не задан. Распознавание голоса отключено.")

    file = await bot.get_file(voice.file_id)
    if not file.file_path:
        raise RuntimeError("Telegram не вернул file_path для голосового сообщения.")

    tmp_dir = Path("tmp_voice")
    tmp_dir.mkdir(exist_ok=True)
    ogg_path = tmp_dir / f"{voice.file_unique_id}.ogg"

    try:
        await bot.download_file(file.file_path, destination=ogg_path)

        if not ogg_path.exists() or ogg_path.stat().st_size == 0:
            raise RuntimeError("Голосовой файл не скачался или скачался пустым.")

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        with ogg_path.open("rb") as audio_file:
            result = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru",
            )

        text = (result.text or "").strip()
        if not text:
            raise RuntimeError("OpenAI вернул пустую расшифровку.")

        return text
    finally:
        try:
            os.remove(ogg_path)
        except OSError:
            pass


@router.message(CommandStart())
async def start(message: types.Message, state: FSMContext) -> None:
    await state.set_state(FeedbackStates.waiting_patient_data)
    text = (
        "Здравствуйте! Отправьте данные пациента в формате:\n\n"
        "<code>Иван Иванович, +79991234567</code>\n\n"
        "После этого бот начнет сценарий сбора обратной связи."
    )
    await message.answer(text)


@router.message(Command("new"))
async def new_feedback(message: types.Message, state: FSMContext) -> None:
    await state.set_state(FeedbackStates.waiting_patient_data)
    await message.answer(
        "Отправьте данные пациента в формате:\n\n"
        "<code>Иван Иванович, +79991234567</code>"
    )


@router.message(Command("id"))
async def get_chat_id(message: types.Message) -> None:
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>")


@router.message(Command("reviewed"))
async def show_reviewed(message: types.Message) -> None:
    if not is_admin_chat(message):
        await message.answer("Команда /reviewed доступна только в админ-группе.")
        return

    rows = await get_all_reviewed_patients()
    if not rows:
        await message.answer("В базе пока нет номеров пациентов, уже оставивших отзыв.")
        return

    header = f"<b>Пациенты, уже оставившие отзыв:</b> {len(rows)}\n\n"
    blocks = [format_reviewed_patient_row(row) for row in rows]
    text = header + "\n\n".join(blocks)

    max_len = 3900
    for i in range(0, len(text), max_len):
        await message.answer(text[i : i + max_len])


@router.message(Command("delete_reviewed"))
async def delete_reviewed(message: types.Message) -> None:
    if not is_admin_chat(message):
        await message.answer("Команда /delete_reviewed доступна только в админ-группе.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Укажите телефон после команды. Например:\n"
            "<code>/delete_reviewed +79991234567</code>"
        )
        return

    phone = parts[1].strip()
    deleted = await delete_reviewed_patient(phone)
    if deleted:
        await message.answer(f"Номер удален из базы: <code>{html.escape(phone)}</code>")
    else:
        await message.answer(
            f"Такой номер не найден в базе: <code>{html.escape(phone)}</code>"
        )


@router.message(FeedbackStates.waiting_patient_data, F.text)
async def receive_patient_data(message: types.Message, state: FSMContext) -> None:
    parsed = parse_patient_data(message.text)
    if not parsed:
        await message.answer(
            "Не получилось разобрать данные. Отправьте, пожалуйста, так:\n\n"
            "<code>Иван Иванович, +79991234567</code>"
        )
        return

    patient_name, phone = parsed

    if await has_patient_already_reviewed(phone):
        await message.answer(
            "Пациент с таким телефоном уже оставлял отзыв. "
            "Сообщение пациенту не отправлено."
        )
        await state.clear()
        return

    session_id = await create_session(message.from_user.id, patient_name, phone)
    await state.update_data(session_id=session_id, patient_name=patient_name, phone=phone)
    await state.set_state(FeedbackStates.waiting_rating)

    bot_text = (
        f"{html.escape(patient_name)}, спасибо Вам, что посетили {html.escape(settings.clinic_name)}! "
        "Мы надеемся, что Вы остались довольны нашей работой. "
        "Оцените, пожалуйста, Ваш визит от 1 до 5."
    )

    await add_message_log(session_id, "bot", bot_text)
    await message.answer(bot_text, reply_markup=rating_keyboard())


@router.callback_query(F.data.startswith("rating:"))
async def process_rating(callback: types.CallbackQuery, state: FSMContext) -> None:
    rating = int(callback.data.split(":", maxsplit=1)[1])
    data = await state.get_data()
    session_id = data.get("session_id")

    if not session_id:
        latest_session = await get_latest_session(callback.from_user.id)
        if not latest_session:
            await callback.message.answer("Сессия не найдена. Нажмите /new и начните заново.")
            await callback.answer()
            return
        session_id = latest_session["id"]

    await update_session(session_id, rating=rating)
    await add_message_log(session_id, "patient", f"Оценка: {rating}")

    if rating == 5:
        await state.set_state(FeedbackStates.waiting_positive_review)
        bot_text = "Оставьте, пожалуйста, отзыв о нашей работе. Напишите Ваш отзыв ниже или продиктуйте мне."
    else:
        await state.set_state(FeedbackStates.waiting_negative_feedback)
        bot_text = "Напишите, пожалуйста, подробнее о Ваших впечатлениях и замечаниях ниже или продиктуйте мне."

    await add_message_log(session_id, "bot", bot_text)
    await callback.message.answer(bot_text, reply_markup=voice_hint_keyboard())
    await callback.answer()


@router.callback_query(F.data == "voice_hint")
async def voice_hint(callback: types.CallbackQuery) -> None:
    await callback.message.answer(
        "Запишите и отправьте голосовое сообщение прямо в этот чат. Я переведу его в текст."
    )
    await callback.answer()


@router.message(FeedbackStates.waiting_positive_review, F.text)
async def receive_positive_text_review(message: types.Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, отправьте непустой текст отзыва или голосовое сообщение.")
        return
    await finish_positive_review(message, state, message.text)


@router.message(FeedbackStates.waiting_negative_feedback, F.text)
async def receive_negative_text_feedback(message: types.Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, отправьте непустой текст замечания или голосовое сообщение.")
        return
    await finish_negative_feedback(message, state, message.text)


@router.message(FeedbackStates.waiting_positive_review, F.voice)
async def receive_positive_voice_review(message: types.Message, state: FSMContext, bot: Bot) -> None:
    try:
        text = await transcribe_voice(bot, message.voice)
    except Exception as exc:
        await message.answer(
            "Не получилось распознать голосовое сообщение. "
            "Пожалуйста, отправьте отзыв текстом."
        )
        print(f"Voice transcription error: {type(exc).__name__}: {exc}")
        return

    await finish_positive_review(message, state, text)


@router.message(FeedbackStates.waiting_negative_feedback, F.voice)
async def receive_negative_voice_feedback(message: types.Message, state: FSMContext, bot: Bot) -> None:
    try:
        text = await transcribe_voice(bot, message.voice)
    except Exception as exc:
        await message.answer(
            "Не получилось распознать голосовое сообщение. "
            "Пожалуйста, отправьте замечание текстом."
        )
        print(f"Voice transcription error: {type(exc).__name__}: {exc}")
        return

    await finish_negative_feedback(message, state, text)


async def finish_positive_review(message: types.Message, state: FSMContext, review_text: str) -> None:
    data = await state.get_data()
    session_id = data.get("session_id")
    if not session_id:
        await message.answer("Сессия не найдена. Нажмите /new и начните заново.")
        return

    review_text = review_text.strip()
    await update_session(session_id, review_text=review_text, status="positive_finished")
    await add_message_log(session_id, "patient", review_text)

    session = await get_session_by_id_safe(session_id)
    if session:
        await add_reviewed_patient(
            phone=session["phone"],
            patient_name=session["patient_name"],
            session_id=session_id,
            review_text=review_text,
            rating=session.get("rating"),
        )

    instruction = (
        "Спасибо! Ниже текст Вашего отзыва.\n\n"
        f"<blockquote>{html.escape(review_text)}</blockquote>\n\n"
        "Пожалуйста, выберите 5*, вставьте отзыв и нажмите \"Опубликовать\"."
    )
    await add_message_log(session_id, "bot", instruction)
    await message.answer(instruction, reply_markup=yandex_review_keyboard())

    final_text = "Спасибо большое за Ваш отзыв! Ждем Вас снова!"
    await add_message_log(session_id, "bot", final_text)
    await message.answer(final_text)

    await send_to_admin_group(message.bot, session_id, "Положительный отзыв пациента")
    await state.clear()


async def finish_negative_feedback(message: types.Message, state: FSMContext, feedback_text: str) -> None:
    data = await state.get_data()
    session_id = data.get("session_id")
    if not session_id:
        await message.answer("Сессия не найдена. Нажмите /new и начните заново.")
        return

    feedback_text = feedback_text.strip()
    await update_session(session_id, review_text=feedback_text, status="negative_finished")
    await add_message_log(session_id, "patient", feedback_text)

    session = await get_session_by_id_safe(session_id)
    if session:
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
    await message.answer(final_text)

    await send_to_admin_group(message.bot, session_id, "Отзыв пациента с оценкой ниже 5")
    await state.clear()


@router.message()
async def fallback(message: types.Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нажмите /new, чтобы начать сбор обратной связи.")
    else:
        await message.answer("Пожалуйста, следуйте текущему шагу сценария.")


async def main() -> None:
    await init_db()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    print("CityClinic feedback bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
