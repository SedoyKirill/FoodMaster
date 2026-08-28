from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True, slots=True)
class RecipeImportConfig:
    database_url: str
    books_dir: Path
    data_dir: Path
    ocr_enabled: bool
    ocr_dpi: int
    ocr_languages: str
    min_native_chars: int
    force: bool = False
    file_pattern: str | None = None
    max_books: int | None = None
    window_pages: int = 4
    window_step: int = 3
    llm_schema_version: int = 1

    @classmethod
    def from_env(cls) -> "RecipeImportConfig":
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError("DATABASE_URL обязателен для импорта книг")
        return cls(
            database_url=database_url,
            books_dir=Path(os.getenv("BOOKS_DIR", "книги")),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            ocr_enabled=env_bool("RECIPE_OCR_ENABLED", True),
            ocr_dpi=max(120, min(400, int(os.getenv("RECIPE_OCR_DPI", "300")))),
            ocr_languages=os.getenv("RECIPE_OCR_LANGUAGES", "rus+eng"),
            min_native_chars=max(20, int(os.getenv("RECIPE_MIN_NATIVE_CHARS", "80"))),
            window_pages=max(2, int(os.getenv("RECIPE_WINDOW_PAGES", "4"))),
            window_step=max(1, int(os.getenv("RECIPE_WINDOW_STEP", "3"))),
            llm_schema_version=int(os.getenv("RECIPE_LLM_SCHEMA_VERSION", "1")),
        )
