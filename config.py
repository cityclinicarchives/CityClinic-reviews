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

    # Wazzup
    wazzup_api_token: str | None = None
    wazzup_api_base_url: str = "https://api.wazzup24.com/v3"
    wazzup_channel_id: str | None = None
    public_base_url: str | None = None


def get_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    admin_group_id_raw = os.getenv("ADMIN_GROUP_ID", "").strip()
    yandex_maps_review_url = os.getenv("YANDEX_MAPS_REVIEW_URL", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip() or None
    clinic_name = os.getenv("CLINIC_NAME", "СитиКлиник").strip() or "СитиКлиник"

    wazzup_api_token = os.getenv("WAZZUP_API_TOKEN", "").strip() or None
    wazzup_api_base_url = os.getenv("WAZZUP_API_BASE_URL", "https://api.wazzup24.com/v3").strip().rstrip("/")
    wazzup_channel_id = os.getenv("WAZZUP_CHANNEL_ID", "").strip() or None
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or None

    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN в .env / Railway Variables")
    if not admin_group_id_raw:
        raise RuntimeError("Не задан ADMIN_GROUP_ID в .env / Railway Variables")
    if not yandex_maps_review_url:
        raise RuntimeError("Не задан YANDEX_MAPS_REVIEW_URL в .env / Railway Variables")

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
        wazzup_api_token=wazzup_api_token,
        wazzup_api_base_url=wazzup_api_base_url,
        wazzup_channel_id=wazzup_channel_id,
        public_base_url=public_base_url,
    )
