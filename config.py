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

    # Email cashback mailing
    email_smtp_host: str = "smtp.mail.ru"
    email_smtp_port: int = 465
    email_smtp_user: str | None = None
    email_smtp_password: str | None = None
    email_from: str = "deti.cityclinic@mail.ru"
    cashback_email_image_url: str | None = None


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

    email_smtp_host = os.getenv("EMAIL_SMTP_HOST", "smtp.mail.ru").strip() or "smtp.mail.ru"
    email_smtp_port_raw = os.getenv("EMAIL_SMTP_PORT", "465").strip() or "465"
    email_smtp_user = os.getenv("EMAIL_SMTP_USER", "deti.cityclinic@mail.ru").strip() or None
    email_smtp_password = os.getenv("EMAIL_SMTP_PASSWORD", "").strip() or None
    email_from = os.getenv("EMAIL_FROM", "deti.cityclinic@mail.ru").strip() or "deti.cityclinic@mail.ru"
    cashback_email_image_url = os.getenv("CASHBACK_EMAIL_IMAGE_URL", "").strip() or None

    try:
        email_smtp_port = int(email_smtp_port_raw)
    except ValueError as exc:
        raise RuntimeError("EMAIL_SMTP_PORT должен быть числом, например 465") from exc

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
        email_smtp_host=email_smtp_host,
        email_smtp_port=email_smtp_port,
        email_smtp_user=email_smtp_user,
        email_smtp_password=email_smtp_password,
        email_from=email_from,
        cashback_email_image_url=cashback_email_image_url,
    )
