"""Извлечение текста страниц с сохранением вёрстки (TZ-M2R, этап 1).

PyMuPDF даёт блоки с координатами; колонки читаются колонка за колонкой,
переводы строк сохраняются. Для страниц без текстового слоя — OCR (300 dpi).
"""

from __future__ import annotations

from pathlib import Path

from .cleaning import normalize_page
from .config import RecipeImportConfig
from .models import ExtractedPage

_COLUMN_GAP_PT = 40.0
_MIN_ALNUM_FOR_TEXT = 40


def _alnum_count(text: str) -> int:
    return sum(character.isalnum() for character in text)


def _looks_garbled(text: str) -> bool:
    """Битая таблица ToUnicode: буквы выходят из Latin Extended (ƧDžNJNjƽ…).

    Такие страницы визуально нормальны — их нужно отправлять в OCR.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) >= 40:
        weird = sum(1 for ch in letters if "ƀ" <= ch <= "ʯ" or "ǀ" <= ch <= "ǿ")
        if weird / len(letters) > 0.2:
            return True
    # Второй вариант порчи: буквы выпадают в пунктуацию/символы
    # («# ? $ F …»), связного текста нет, выживают только цифры и
    # отдельные слова. Признак: букв меньше трети «непробельных» символов.
    dense = [ch for ch in text if not ch.isspace()]
    if len(dense) >= 150 and len(letters) / len(dense) < 0.35:
        return True
    # Третий вариант порчи: текст рассыпается на одиночные латинские буквы
    # и символы («b GJ 5 [F( …») — почти все «слова» из 1–2 символов.
    tokens = text.split()
    if len(tokens) >= 30:
        short = sum(1 for token in tokens if len(token) <= 2)
        if short / len(tokens) > 0.5:
            return True
    return False


def _block_text(block: dict) -> str:
    lines: list[str] = []
    for line in block.get("lines", []):
        spans = [span.get("text", "") for span in line.get("spans", [])]
        text = "".join(spans).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _split_columns(blocks: list[dict]) -> list[list[dict]]:
    """Делит блоки на колонки, если между ними есть вертикальный «жёлоб»."""
    if len(blocks) < 4:
        return [blocks]
    intervals = sorted((block["bbox"][0], block["bbox"][2]) for block in blocks)
    merged: list[list[float]] = []
    for x0, x1 in intervals:
        if merged and x0 <= merged[-1][1] + _COLUMN_GAP_PT:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    if len(merged) < 2:
        return [blocks]
    columns: list[list[dict]] = [[] for _ in merged]
    for block in blocks:
        center = (block["bbox"][0] + block["bbox"][2]) / 2
        for index, (x0, x1) in enumerate(merged):
            if x0 - 1 <= center <= x1 + 1:
                columns[index].append(block)
                break
        else:
            columns[0].append(block)
    return [column for column in columns if column]


def extract_page_layout(page) -> str:
    """Текст страницы PyMuPDF в порядке чтения (колонки слева направо)."""
    data = page.get_text("dict")
    blocks = [block for block in data.get("blocks", []) if block.get("type") == 0]
    if not blocks:
        return ""
    parts: list[str] = []
    for column in _split_columns(blocks):
        for block in sorted(column, key=lambda item: (item["bbox"][1], item["bbox"][0])):
            text = _block_text(block)
            if text:
                parts.append(text)
    return normalize_page("\n\n".join(parts))


def extract_book_pages(pdf_path: Path, config: RecipeImportConfig) -> list[ExtractedPage]:
    """Все страницы книги: PyMuPDF, для пустых — OCR (если включён)."""
    import fitz  # PyMuPDF; ленивый импорт, чтобы тесты не требовали зависимость

    pages: list[ExtractedPage] = []
    with fitz.open(str(pdf_path)) as document:
        page_count = document.page_count
        for page_number in range(1, page_count + 1):
            try:
                text = extract_page_layout(document.load_page(page_number - 1))
            except Exception as exc:  # повреждённая страница не валит книгу
                pages.append(
                    ExtractedPage(page_number=page_number, method="error", text="", error=str(exc))
                )
                continue
            if _alnum_count(text) >= _MIN_ALNUM_FOR_TEXT and not _looks_garbled(text):
                pages.append(ExtractedPage(page_number=page_number, method="pymupdf", text=text))
                continue
            native = "" if _looks_garbled(text) else text
            pages.append(_ocr_or_empty(pdf_path, page_number, native, config))
    return pages


def _ocr_or_empty(
    pdf_path: Path, page_number: int, native_text: str, config: RecipeImportConfig
) -> ExtractedPage:
    if not config.ocr_enabled:
        return ExtractedPage(
            page_number=page_number,
            method="pdf_text_short" if native_text else "empty",
            text=native_text,
        )
    from .importer import _ocr_page, event

    try:
        ocr_text, white_ratio, method = _ocr_page(pdf_path, page_number, config)
    except Exception as exc:
        return ExtractedPage(page_number=page_number, method="error", text=native_text, error=str(exc))
    if method == "ocr" and _alnum_count(ocr_text) > _alnum_count(native_text):
        event("page_ocr", file=pdf_path.name, page=page_number, chars=len(ocr_text))
        return ExtractedPage(
            page_number=page_number, method="ocr", text=normalize_page(ocr_text), white_ratio=white_ratio
        )
    return ExtractedPage(
        page_number=page_number,
        method="image_only" if not native_text else "pdf_text_short",
        text=native_text,
        white_ratio=white_ratio,
    )


def book_metadata(pdf_path: Path) -> tuple[str | None, str | None, int | None, dict]:
    """Название, автор и год книги из метаданных PDF (фолбэк — имя файла)."""
    import fitz

    from .importer import _filename_metadata

    fallback_title, fallback_author, year = _filename_metadata(pdf_path)
    with fitz.open(str(pdf_path)) as document:
        metadata = {key: value for key, value in (document.metadata or {}).items() if value}
    title = (metadata.get("title") or "").strip() or fallback_title
    author = (metadata.get("author") or "").strip() or fallback_author
    return title, author, year, metadata
