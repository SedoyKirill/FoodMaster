from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CATEGORIES = (
    "alkogol-17036",
    "gotovaya-eda-42",
    "ovoshchi-frukty-144",
    "ryba-ikra-moreprodukty-183",
    "myaso-i-ptica-136",
    "molochnye-produkty-yajjco-3",
    "kolbasa-sosiski-754",
    "syry-2",
    "zamorozka-77",
    "makarony-krupy-muka-25",
    "hleb-i-vypechka-165",
    "kofe-chajj-kakao-242",
    "maslo-sousy-specii-20824",
    "napitki-4",
    "sladosti-1028",
    "konservaciya-94",
    "sneki-20195",
    "zdorovoe-pitanie-1879",
    "detskoe-pitanie-19327",
)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _categories_from_env() -> tuple[str, ...]:
    raw = os.getenv("LENTA_CATEGORIES", "").strip()
    if not raw:
        return DEFAULT_CATEGORIES
    return tuple(item.strip().strip("/") for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class LentaConfig:
    store_code: str
    store_id: str
    city: str
    store_address: str
    database_url: str | None
    data_dir: Path
    categories: tuple[str, ...]
    max_pages: int
    detail_limit: int
    page_delay_seconds: float
    max_delay_seconds: float
    run_at: str
    timezone: str
    run_on_start: bool
    api_base_url: str
    app_version: str
    android_release: str
    android_sdk: str
    request_timeout_seconds: float
    request_retries: int
    page_size: int

    @property
    def device_id_path(self) -> Path:
        return self.data_dir / "lenta" / "device-id"

    @classmethod
    def from_env(cls) -> "LentaConfig":
        database_url = os.getenv("DATABASE_URL", "").strip() or None
        return cls(
            store_code=os.getenv("LENTA_STORE_CODE", "lenta-155"),
            store_id=os.getenv("LENTA_STORE_ID", "155"),
            city=os.getenv("LENTA_CITY", "Иваново"),
            store_address=os.getenv("LENTA_STORE_ADDRESS", "Карла Маркса ул., 3"),
            database_url=database_url,
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            categories=_categories_from_env(),
            max_pages=max(1, int(os.getenv("LENTA_MAX_PAGES", "100"))),
            detail_limit=max(0, int(os.getenv("LENTA_DETAIL_LIMIT", "1500"))),
            page_delay_seconds=max(0.05, float(os.getenv("LENTA_PAGE_DELAY_SECONDS", "1.2"))),
            max_delay_seconds=max(1.0, float(os.getenv("LENTA_MAX_DELAY_SECONDS", "15.0"))),
            run_at=os.getenv("LENTA_RUN_AT", "03:00"),
            timezone=os.getenv("LENTA_TIMEZONE", "Europe/Moscow"),
            run_on_start=env_bool("LENTA_RUN_ON_START", False),
            api_base_url=os.getenv("LENTA_API_BASE_URL", "https://api.baseomni.ru/v1").rstrip("/"),
            app_version=os.getenv("LENTA_APP_VERSION", "6.93.0"),
            android_release=os.getenv("LENTA_ANDROID_RELEASE", "15"),
            android_sdk=os.getenv("LENTA_ANDROID_SDK", "35"),
            request_timeout_seconds=max(5.0, float(os.getenv("LENTA_REQUEST_TIMEOUT_SECONDS", "30"))),
            request_retries=max(0, int(os.getenv("LENTA_REQUEST_RETRIES", "3"))),
            page_size=min(100, max(1, int(os.getenv("LENTA_PAGE_SIZE", "40")))),
        )
