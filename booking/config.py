"""Settings from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    whatsapp_token: str = ""
    phone_number_id: str = ""
    verify_token: str = ""
    app_secret: str = ""
    graph_version: str = "v23.0"
    owner_phone: str = ""
    reminder_template: str = ""
    reminder_template_lang: str = "es_AR"
    owner_alert_template: str = ""
    owner_alert_template_lang: str = "es_AR"
    allow_unsigned_webhooks: bool = False
    business_file: Path = Path("business.json")
    db_path: Path = Path("data/bookings.sqlite3")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        env = os.environ.get
        return cls(
            whatsapp_token=env("WHATSAPP_TOKEN", ""),
            phone_number_id=env("WHATSAPP_PHONE_NUMBER_ID", ""),
            verify_token=env("WHATSAPP_VERIFY_TOKEN", ""),
            app_secret=env("WHATSAPP_APP_SECRET", ""),
            graph_version=env("GRAPH_API_VERSION", "v23.0"),
            owner_phone=env("OWNER_PHONE", "").lstrip("+"),
            reminder_template=env("REMINDER_TEMPLATE", ""),
            reminder_template_lang=env("REMINDER_TEMPLATE_LANG", "es_AR"),
            owner_alert_template=env("OWNER_ALERT_TEMPLATE", ""),
            owner_alert_template_lang=env("OWNER_ALERT_TEMPLATE_LANG", "es_AR"),
            allow_unsigned_webhooks=env("ALLOW_UNSIGNED_WEBHOOKS", "") == "1",
            business_file=Path(env("BUSINESS_FILE", "business.json")),
            db_path=Path(env("DB_PATH", "data/bookings.sqlite3")),
        )
