"""Чистка текста страниц перед нарезкой на окна (TZ-M2R, этап 2)."""

from __future__ import annotations

import re
import unicodedata

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_PAGE_NUMBER_RE = re.compile(r"^\s*[-—–|•·]*\s*\d{1,4}\s*[-—–|•·]*\s*$")
_FOOTER_WITH_NUMBER_RE = re.compile(r"^\s*[-—–]\s*[^\d\n]{3,60}\s*[-—–]\s*\d{1,4}\s*$")
_LATIN1_SUSPECT_RE = re.compile(r"[À-ÿ]")
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_DUP_WORD_RE = re.compile(r"\b(\w{4,})(\s+\1)+\b", re.UNICODE)


def normalize_page(text: str) -> str:
    """Базовая нормализация без потери переводов строк (антидефект D2)."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("­", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def dedupe_glyphs(line: str) -> str:
    """Убирает дубли от псевдожирного шрифта: «sXsX» → «sX», «слово слово» → «слово»."""
    stripped = line.strip()
    if not stripped:
        return line
    half = len(stripped) // 2
    if half >= 3:
        for gap in (0, 1):
            left = stripped[:half]
            right = stripped[half + gap :]
            if left == right and (gap == 0 or not stripped[half].isalnum()):
                return left
    collapsed = _DUP_WORD_RE.sub(r"\1", stripped)
    # Дубли вида «10–1210–12 …» внутри строки: пары токенов подряд.
    tokens = collapsed.split(" ")
    result: list[str] = []
    for token in tokens:
        if len(token) >= 6 and len(token) % 2 == 0:
            half_token = len(token) // 2
            if token[:half_token] == token[half_token:]:
                token = token[:half_token]
        if not (result and result[-1] == token and len(token) >= 4):
            result.append(token)
    return " ".join(result)


def fix_mojibake(text: str) -> str:
    """Чинит CP1251-текст, прочитанный как Latin-1 («Áåëüêîâè÷» → «Белькович»)."""
    suspects = len(_LATIN1_SUSPECT_RE.findall(text))
    letters = len(_WORD_RE.findall(text)) or 1
    if not suspects or suspects / max(1, len(text.replace(" ", ""))) <= 0.3:
        return text
    try:
        decoded = text.encode("latin-1").decode("cp1251")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    before = len(_CYRILLIC_RE.findall(text))
    after = len(_CYRILLIC_RE.findall(decoded))
    return decoded if after > before else text


def _header_key(line: str) -> str:
    lowered = line.lower()
    lowered = re.sub(r"[\d]+", "", lowered)
    lowered = re.sub(r"[^\w ]+", "", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip()


_HEADER_WHITELIST = {
    "ингредиенты",
    "приготовление",
    "способ приготовления",
    "ingredients",
    "method",
    "directions",
}


def strip_running_headers(pages: list[str], *, min_pages: int = 5, ratio: float = 0.15) -> list[str]:
    """Удаляет колонтитулы: строки, повторяющиеся на заметной доле страниц книги.

    Короткие ключи (< 8 символов) и служебные маркеры секций («Ингредиенты»,
    «Приготовление») не считаются колонтитулами — они нужны распознаванию.
    """
    counts: dict[str, int] = {}
    for page in pages:
        seen: set[str] = set()
        for line in page.splitlines():
            key = _header_key(line)
            if len(key) >= 8 and key not in _HEADER_WHITELIST and key not in seen:
                seen.add(key)
                counts[key] = counts.get(key, 0) + 1
    total = max(1, len(pages))
    headers = {
        key
        for key, count in counts.items()
        if count >= min_pages and count / total >= ratio
    }
    if not headers:
        return list(pages)
    cleaned: list[str] = []
    for page in pages:
        kept = [line for line in page.splitlines() if _header_key(line) not in headers]
        cleaned.append("\n".join(kept))
    return cleaned


def strip_page_numbers(page_text: str) -> str:
    """Убирает строки-номера страниц и колонтитулы вида «-НАЗВАНИЕ- 69» по краям."""
    lines = page_text.splitlines()

    def is_noise(line: str) -> bool:
        return bool(_PAGE_NUMBER_RE.match(line) or _FOOTER_WITH_NUMBER_RE.match(line))

    start = 0
    end = len(lines)
    while start < end and (not lines[start].strip() or is_noise(lines[start])):
        start += 1
    while end > start and (not lines[end - 1].strip() or is_noise(lines[end - 1])):
        end -= 1
    return "\n".join(lines[start:end])


def clean_page(text: str) -> str:
    """Полная чистка одной страницы (без межстраничных колонтитулов)."""
    normalized = normalize_page(text)
    lines = [dedupe_glyphs(fix_mojibake(line)) for line in normalized.splitlines()]
    return strip_page_numbers("\n".join(lines))


def clean_book_pages(pages: list[str]) -> list[str]:
    """Чистка всех страниц книги: построчная чистка + удаление колонтитулов."""
    cleaned = [clean_page(page) for page in pages]
    return strip_running_headers(cleaned)
