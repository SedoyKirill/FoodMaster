"""Нарезка чистых страниц книги на окна-файлы для сессии Haiku (TZ-M2R, этап 3)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_PAGE_MARKER = "=== СТРАНИЦА {number} ==="
_MIN_WINDOW_ALNUM = 200
WINDOW_ID_RE = re.compile(r"^(?P<source_id>\d{3})-(?P<start>\d{4})-(?P<end>\d{4})$")


@dataclass(slots=True)
class Window:
    window_id: str
    source_id: int
    source_title: str
    page_start: int
    page_end: int
    text: str
    sha256: str

    @property
    def chars(self) -> int:
        return len(self.text)


def _alnum_count(text: str) -> int:
    return sum(character.isalnum() for character in text)


def build_windows(
    source_id: int,
    source_title: str,
    pages: dict[int, str],
    *,
    window_pages: int = 4,
    window_step: int = 3,
) -> list[Window]:
    """Окна из window_pages подряд идущих страниц с шагом window_step.

    `pages` — {номер страницы: чистый текст}; пустые страницы участвуют в
    нумерации, но не добавляют текста.
    """
    if not pages:
        return []
    numbers = sorted(pages)
    first, last = numbers[0], numbers[-1]
    windows: list[Window] = []
    start = first
    while start <= last:
        end = min(start + window_pages - 1, last)
        chunks: list[str] = []
        for number in range(start, end + 1):
            text = (pages.get(number) or "").strip()
            chunks.append(_PAGE_MARKER.format(number=number))
            if text:
                chunks.append(text)
        body = "\n".join(chunks).strip()
        if _alnum_count(body) >= _MIN_WINDOW_ALNUM:
            window_id = f"{source_id:03d}-{start:04d}-{end:04d}"
            sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            windows.append(
                Window(
                    window_id=window_id,
                    source_id=source_id,
                    source_title=source_title,
                    page_start=start,
                    page_end=end,
                    text=body,
                    sha256=sha,
                )
            )
        if end >= last:
            break
        start += window_step
    return windows


def render_window_file(window: Window) -> str:
    return (
        "---\n"
        f"window_id: {window.window_id}\n"
        f"source_id: {window.source_id}\n"
        f"source_title: {window.source_title}\n"
        f"pages: {window.page_start}-{window.page_end}\n"
        f"sha256: {window.sha256}\n"
        "---\n"
        f"{window.text}\n"
    )


def write_windows(windows: list[Window], directory: Path) -> dict[str, int]:
    """Пишет файлы окон и манифест; возвращает счётчики new/updated/unchanged."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    manifest: dict[str, dict] = {}
    if manifest_path.exists():
        try:
            manifest = {
                entry["window_id"]: entry
                for entry in json.loads(manifest_path.read_text(encoding="utf-8")).get("windows", [])
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            manifest = {}
    counters = {"new": 0, "updated": 0, "unchanged": 0}
    for window in windows:
        previous = manifest.get(window.window_id)
        path = directory / f"{window.window_id}.md"
        if previous and previous.get("sha256") == window.sha256 and path.exists():
            counters["unchanged"] += 1
        else:
            path.write_text(render_window_file(window), encoding="utf-8")
            counters["updated" if previous else "new"] += 1
        manifest[window.window_id] = {
            "window_id": window.window_id,
            "source_id": window.source_id,
            "pages": f"{window.page_start}-{window.page_end}",
            "sha256": window.sha256,
            "chars": window.chars,
        }
    manifest_path.write_text(
        json.dumps(
            {"windows": sorted(manifest.values(), key=lambda entry: entry["window_id"])},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return counters


def extraction_status(windows_dir: Path, extracted_dir: Path) -> dict[str, int]:
    """Прогресс этапа 4: сколько окон обработано сессией Haiku."""
    window_ids = {path.stem for path in windows_dir.glob("*.md")}
    extracted_ids = {path.stem for path in extracted_dir.glob("*.json")}
    return {
        "windows": len(window_ids),
        "extracted": len(window_ids & extracted_ids),
        "pending": len(window_ids - extracted_ids),
        "orphan_results": len(extracted_ids - window_ids),
    }
