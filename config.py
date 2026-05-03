import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_group_id: int
    yandex_maps_review_url: str
    openai_api_key: str | None = None
    clinic_name: str = "СитиКлиник"


def get_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    admin_group_id_raw = os.getenv("ADMIN_GROUP_ID", "").strip()
    yandex_maps_review_url = os.getenv("YANDEX_MAPS_REVIEW_URL", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip() or None
    clinic_name = os.getenv("CLINIC_NAME", "СитиКлиник").strip() or "СитиКлиник"

    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN в .env")
    if not admin_group_id_raw:
        raise RuntimeError("Не задан ADMIN_GROUP_ID в .env")
    if not yandex_maps_review_url:
        raise RuntimeError("Не задан YANDEX_MAPS_REVIEW_URL в .env")

    try:
        admin_group_id = int(admin_group_id_raw)
    except ValueError as exc:
        raise RuntimeError("ADMIN_GROUP_ID должен быть числом, например -1001234567890") from exc

    return Settings(
        bot_token=bot_token,
        admin_group_id=admin_group_id,
        yandex_maps_review_url=yandex_maps_review_url,
        openai_api_key=openai_api_key,
        clinic_name=clinic_name,
    )
