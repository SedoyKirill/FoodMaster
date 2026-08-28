"""Оркестрация конвейера TZ-M2R: export-windows и load-extracted."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from .cleaning import clean_book_pages
from .config import RecipeImportConfig
from .database import RecipeRepository
from .importer import event, sha256_file
from .models import ParsedIngredient, RecipeCandidate
from .validate import looks_like_recipe_window, review_recipe, validate_payload
from .windows import WINDOW_ID_RE, build_windows, extraction_status, write_windows


def windows_dir(config: RecipeImportConfig) -> Path:
    return config.data_dir / "recipes" / "windows"


def extracted_dir(config: RecipeImportConfig) -> Path:
    return config.data_dir / "recipes" / "extracted"


def _book_paths(config: RecipeImportConfig) -> list[Path]:
    if not config.books_dir.exists():
        raise FileNotFoundError(f"Папка книг не найдена: {config.books_dir}")
    paths = sorted(
        path
        for path in config.books_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".pdf"
    )
    if config.file_pattern:
        needle = config.file_pattern.lower()
        paths = [path for path in paths if needle in path.name.lower()]
    if config.max_books is not None:
        paths = paths[: config.max_books]
    return paths


async def run_export_windows(config: RecipeImportConfig) -> dict[str, Any]:
    """Этапы 1–3: извлечение страниц (PyMuPDF/OCR), чистка, файлы окон."""
    from .extract import book_metadata, extract_book_pages

    repository = RecipeRepository(config.database_url)
    await repository.connect()
    totals = {"books": 0, "windows": 0, "new": 0, "updated": 0, "unchanged": 0}
    target_dir = windows_dir(config)
    try:
        for path in _book_paths(config):
            event("book_started", file=path.name)
            digest = await asyncio.to_thread(sha256_file, path)
            title, author, year, metadata = await asyncio.to_thread(book_metadata, path)
            language = "ru"
            if title and not any("а" <= ch.lower() <= "я" for ch in title):
                language = "en"
            page_count = 0
            source_id, _status = await repository.upsert_source(
                sha256=digest,
                file_name=path.name,
                file_path=str(path),
                file_size=path.stat().st_size,
                title=title,
                author=author,
                published_year=year,
                language=language,
                page_count=0,
                metadata=metadata,
            )
            methods = await repository.page_methods(source_id)
            needs_extract = config.force or not ({"pymupdf", "ocr"} & methods)
            if needs_extract:
                pages = await asyncio.to_thread(extract_book_pages, path, config)
                page_count = len(pages)
                for page in pages:
                    await repository.save_page(source_id, page)
                event(
                    "book_extracted",
                    file=path.name,
                    pages=page_count,
                    ocr=sum(p.method == "ocr" for p in pages),
                    empty=sum(p.method in {"empty", "image_only"} for p in pages),
                )
            stored_pages = await repository.load_pages(source_id)
            page_count = page_count or len(stored_pages)
            ordered = sorted(stored_pages, key=lambda item: item.page_number)
            cleaned = clean_book_pages([page.text for page in ordered])
            page_map = {
                page.page_number: text
                for page, text in zip(ordered, cleaned)
                if text.strip()
            }
            book_windows = build_windows(
                source_id,
                title or path.stem,
                page_map,
                window_pages=config.window_pages,
                window_step=config.window_step,
            )
            counters = write_windows(book_windows, target_dir)
            totals["books"] += 1
            totals["windows"] += len(book_windows)
            for key in ("new", "updated", "unchanged"):
                totals[key] += counters[key]
            event(
                "book_windows",
                file=path.name,
                windows=len(book_windows),
                **counters,
            )
        event("export_finished", **totals, directory=str(target_dir))
        return totals
    finally:
        await repository.close()


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _normalize_title(title: str) -> str:
    lowered = title.lower()
    lowered = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in lowered)
    return " ".join(lowered.split())


def _candidate_from_json(
    recipe: dict, window_body: str, source_title: str | None
) -> RecipeCandidate:
    reasons = review_recipe(recipe, source_title)
    ingredients = [
        ParsedIngredient(
            raw_text=item.get("raw_text") or "",
            ingredient_text=item.get("name") or "",
            quantity_min=_decimal(item.get("quantity_min")),
            quantity_max=_decimal(item.get("quantity_max")),
            unit_raw=item.get("unit"),
            unit_code=item.get("unit"),
            normalized_name=(item.get("name") or "").strip().lower() or None,
            confidence=Decimal("0.9"),
            is_to_taste=bool(item.get("is_to_taste")),
            section=item.get("section"),
            note=item.get("note"),
        )
        for item in recipe.get("ingredients", [])
    ]
    page_start = int(recipe["page_start"])
    page_end = int(recipe.get("page_end") or page_start)
    return RecipeCandidate(
        title=(recipe.get("title") or "").strip()[:200],
        page_start=page_start,
        page_end=max(page_start, page_end),
        raw_text=window_body,
        source_servings_min=_decimal(recipe.get("servings_min")),
        source_servings_max=_decimal(recipe.get("servings_max")),
        source_yield_text=recipe.get("yield_text"),
        cuisine_code=None,
        cuisine_confidence=None,
        meal_types=sorted(recipe.get("meal_types") or []),
        diet_tags=sorted(recipe.get("diet_tags") or []),
        appliances=sorted(recipe.get("appliances") or []),
        confidence=Decimal("0.9") if not reasons else Decimal("0.6"),
        ingredients=ingredients,
        steps=[step.strip() for step in recipe.get("steps", []) if step.strip()],
        time_total_minutes=recipe.get("time_total_minutes"),
        extraction_method="llm",
        review_status="ready" if not reasons else "needs_review",
        review_reasons=reasons,
    )


def _dedupe(candidates: list[RecipeCandidate]) -> list[RecipeCandidate]:
    ordered = sorted(
        candidates,
        key=lambda item: (item.page_start, item.review_status != "ready", -len(item.ingredients)),
    )
    kept: list[RecipeCandidate] = []
    for candidate in ordered:
        duplicate = False
        norm = _normalize_title(candidate.title)
        for existing in kept:
            if (
                _normalize_title(existing.title) == norm
                and abs(existing.page_start - candidate.page_start) <= 1
            ):
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
    return kept


def _window_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        closing = text.find("\n---", 3)
        if closing != -1:
            return text[closing + 4 :].strip()
    return text.strip()


async def run_load_extracted(config: RecipeImportConfig) -> dict[str, Any]:
    """Этап 5: валидация результатов Haiku, дедуп, правила готовности, запись в БД."""
    win_dir = windows_dir(config)
    ext_dir = extracted_dir(config)
    ext_dir.mkdir(parents=True, exist_ok=True)
    repository = RecipeRepository(config.database_url)
    await repository.connect()
    invalid: list[dict[str, Any]] = []
    per_source: dict[int, list[RecipeCandidate]] = defaultdict(list)
    processed_windows: dict[int, int] = defaultdict(int)
    suspicious_empty: list[tuple[Path, int, int, int]] = []
    try:
        sources = await repository.sources_index()
        for result_path in sorted(ext_dir.glob("*.json")):
            match = WINDOW_ID_RE.match(result_path.stem)
            if not match:
                invalid.append({"file": result_path.name, "errors": ["непонятный window_id"]})
                continue
            source_id = int(match.group("source_id"))
            if source_id not in sources:
                invalid.append({"file": result_path.name, "errors": ["неизвестный source_id"]})
                continue
            if config.file_pattern:
                needle = config.file_pattern.lower()
                info = sources[source_id]
                haystack = f"{info.get('title') or ''} {info.get('file_name') or ''}".lower()
                if needle not in haystack:
                    continue
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                invalid.append({"file": result_path.name, "errors": [f"JSON: {exc}"]})
                continue
            errors = validate_payload(payload)
            if errors:
                invalid.append({"file": result_path.name, "errors": errors[:10]})
                continue
            window_file = win_dir / f"{result_path.stem}.md"
            body = _window_body(window_file) if window_file.exists() else ""
            if not payload["recipes"] and not payload.get("verified_empty"):
                if looks_like_recipe_window(body):
                    # Кандидат в заглушки; окончательное решение — после сбора
                    # всех окон источника (рецепт мог попасть в соседнее окно).
                    suspicious_empty.append(
                        (result_path, source_id, int(match.group("start")), int(match.group("end")))
                    )
            await repository.save_llm_extraction(
                window_sha256=payload["sha256"],
                model=payload["model"],
                schema_version=int(payload["schema_version"]),
                source_id=source_id,
                page_start=int(match.group("start")),
                page_end=int(match.group("end")),
                response=payload,
            )
            processed_windows[source_id] += 1
            source_title = sources[source_id].get("title")
            for recipe in payload["recipes"]:
                per_source[source_id].append(
                    _candidate_from_json(recipe, body, source_title)
                )

        # Заглушко-контроль с учётом перекрытия окон: пустой результат на
        # рецептном окне прощается, если рецепт с этих страниц уже извлечён
        # соседним окном того же источника; иначе файл уходит в карантин.
        for result_path, source_id, page_start, page_end in suspicious_empty:
            covered = any(
                page_start <= candidate.page_start <= page_end
                or page_start <= candidate.page_end <= page_end
                for candidate in per_source.get(source_id, [])
            )
            if not covered:
                rejected_dir = ext_dir / "_rejected"
                rejected_dir.mkdir(exist_ok=True)
                result_path.replace(rejected_dir / result_path.name)
                invalid.append(
                    {
                        "file": result_path.name,
                        "errors": ["suspicious_empty: окно похоже на рецепт, а результат пуст"],
                    }
                )

        written = {"sources": 0, "recipes": 0, "ready": 0}
        status = extraction_status(win_dir, ext_dir)

        # Источник, у которого все окна распознаны, но рецептов нет, тоже
        # очищается (иначе в БД навсегда остаются записи старого парсера).
        for source_id in sorted(set(processed_windows) - set(per_source)):
            pending_for_source = len(
                [
                    path
                    for path in win_dir.glob(f"{source_id:03d}-*.md")
                    if not (ext_dir / f"{path.stem}.json").exists()
                ]
            )
            await repository.replace_recipes(source_id, [])
            if pending_for_source == 0:
                await repository.finish_source(
                    source_id, "llm", "excluded", "llm: рецепты не найдены"
                )
            event(
                "source_loaded",
                source_id=source_id,
                recipes=0,
                ready=0,
                pending_windows=pending_for_source,
            )

        for source_id, candidates in sorted(per_source.items()):
            unique = _dedupe(candidates)
            await repository.replace_recipes(source_id, unique)
            pending_for_source = len(
                [
                    path
                    for path in win_dir.glob(f"{source_id:03d}-*.md")
                    if not (ext_dir / f"{path.stem}.json").exists()
                ]
            )
            if unique:
                await repository.finish_source(source_id, "llm", "completed", None)
            elif pending_for_source == 0:
                await repository.finish_source(
                    source_id, "llm", "excluded", "llm: рецепты не найдены"
                )
            written["sources"] += 1
            written["recipes"] += len(unique)
            written["ready"] += sum(c.review_status == "ready" for c in unique)
            event(
                "source_loaded",
                source_id=source_id,
                recipes=len(unique),
                ready=sum(c.review_status == "ready" for c in unique),
                pending_windows=pending_for_source,
            )
        summary = await repository.library_summary()
        result = {
            "status": "partial" if invalid or status["pending"] else "success",
            "written": written,
            "invalid_files": invalid,
            "windows": status,
            "library": summary,
        }
        event("load_finished", **{k: v for k, v in result.items() if k != "library"})
        return result
    finally:
        await repository.close()
