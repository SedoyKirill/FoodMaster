"""Import recipes from a source site into the database.

    python -m scripts.import_recipes --limit 4000
    python -m scripts.import_recipes --limit 30 --dry-run   # parse only, no writes

Design notes:

* **Pages are cached on disk** (``data/raw/<source>/``). Re-running the importer
  after a normalisation change costs no requests, which matters because
  normalisation is the part that gets iterated on most.
* **Idempotent by ``source_url``.** A second run updates rather than duplicates,
  which is TZ-M2's acceptance criterion. Ingredient rows for a recipe are
  replaced wholesale inside the same transaction, so a changed ingredient set
  cannot leave orphans.
* **Politeness**: one request per ``--delay`` seconds (default 2.5, as TZ-M2
  requires), a browser User-Agent, and only URLs robots.txt allows — the source
  yields them from the sitemap.
* **Quarantine**: a recipe whose ingredient lines are more than 30% unresolvable
  is stored with ``status='quarantine'`` instead of being dropped, so the admin
  page in M6 can show what went wrong and a later normalisation pass can rescue
  it.

Ingredient matching here is exact-name only. The embedding pass (cosine > 0.85)
that TZ-M2 describes as step 3 runs separately in backfill_embeddings.py, once
the ingredient table exists to embed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import httpx
import structlog
from sqlalchemy import delete, select

from app.core.config import PROJECT_ROOT, get_settings
from app.core.db import dispose_engine, session_scope
from app.core.logging import configure_logging
from app.core.models import Ingredient, Recipe, RecipeIngredient
from app.core.models.enums import RecipeStatus
from app.recipes.normalize import normalize_name, split_ingredient_line
from app.recipes.source import SOURCES, USER_AGENT, EdaSource, RawRecipe, RecipeSource
from app.recipes.units import parse_amount, to_grams

log = structlog.get_logger("import_recipes")

#: A recipe with more unresolved ingredient lines than this goes to quarantine.
QUARANTINE_THRESHOLD = 0.30


@dataclass
class Stats:
    seen: int = 0
    imported: int = 0
    updated: int = 0
    quarantined: int = 0
    skipped: int = 0
    failed: int = 0
    lines_total: int = 0
    lines_with_grams: int = 0
    lines_to_taste: int = 0
    lines_unresolved: int = 0
    ingredients_created: int = 0
    from_cache: int = 0
    fetched: int = 0
    by_category: Counter[str] = field(default_factory=Counter)

    def report(self) -> dict[str, object]:
        pct = 100 * self.lines_with_grams // max(self.lines_total, 1)
        return {
            "seen": self.seen,
            "imported": self.imported,
            "updated": self.updated,
            "quarantined": self.quarantined,
            "failed": self.failed,
            "ingredient_lines": self.lines_total,
            "with_grams_pct": pct,
            "to_taste": self.lines_to_taste,
            "unresolved": self.lines_unresolved,
            "new_ingredients": self.ingredients_created,
            "fetched": self.fetched,
            "from_cache": self.from_cache,
        }


class PageCache:
    """Disk cache keyed by URL, so re-parsing never re-fetches."""

    def __init__(self, root: Path, delay: float) -> None:
        self.root = root
        self.delay = delay
        self.root.mkdir(parents=True, exist_ok=True)
        self._last_request = 0.0

    def path_for(self, url: str) -> Path:
        return self.root / f"{hashlib.sha1(url.encode()).hexdigest()}.html"

    def get(self, url: str, client: httpx.Client, stats: Stats) -> str | None:
        path = self.path_for(url)
        if path.exists():
            stats.from_cache += 1
            return path.read_text(encoding="utf-8")

        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.monotonic()

        try:
            response = client.get(url, timeout=30)
        except httpx.HTTPError as exc:
            log.warning("fetch.failed", url=url, error=str(exc))
            return None
        if response.status_code != 200:
            log.warning("fetch.status", url=url, status=response.status_code)
            return None

        stats.fetched += 1
        html = response.text
        path.write_text(html, encoding="utf-8")
        return html


def select_urls(source: RecipeSource, client: httpx.Client, limit: int) -> list[str]:
    """Pick a category-balanced sample.

    Taking the first N of the sitemap would return whatever the site happened to
    publish most recently, which skews heavily towards one category. The planner
    needs breakfasts and soups as much as main courses.
    """
    by_category: dict[str, list[str]] = defaultdict(list)
    for url in source.list_urls(client):
        category = getattr(source, "category_of", lambda _u: None)(url) or "other"
        by_category[category].append(url)

    total = sum(len(v) for v in by_category.values())
    log.info(
        "sitemap.loaded",
        total=total,
        categories={k: len(v) for k, v in sorted(by_category.items())},
    )

    random.seed(20260728)  # reproducible samples across runs
    quota = max(limit // max(len(by_category), 1), 1)
    chosen: list[str] = []
    leftovers: list[str] = []
    for urls in by_category.values():
        random.shuffle(urls)
        chosen.extend(urls[:quota])
        leftovers.extend(urls[quota:])
    # Fill any remainder from the larger categories.
    random.shuffle(leftovers)
    chosen.extend(leftovers[: max(limit - len(chosen), 0)])
    random.shuffle(chosen)
    return chosen[:limit]


async def get_or_create_ingredient(
    session, name: str, display_name: str, cache: dict[str, int], stats: Stats
) -> int:
    """Exact-name lookup, creating the ingredient when it is new."""
    if name in cache:
        return cache[name]

    existing = await session.scalar(select(Ingredient.id).where(Ingredient.name == name))
    if existing is not None:
        cache[name] = existing
        return existing

    ingredient = Ingredient(name=name, display_name=display_name)
    session.add(ingredient)
    await session.flush()
    stats.ingredients_created += 1
    cache[name] = ingredient.id
    return ingredient.id


async def store_recipe(
    session,
    raw: RawRecipe,
    meal_types: list[str],
    ingredient_cache: dict[str, int],
    stats: Stats,
) -> None:
    resolved = 0
    rows: list[dict[str, object]] = []

    for line in raw.ingredients:
        stats.lines_total += 1
        name_text, amount_text = split_ingredient_line(line.raw_text)
        normalized = normalize_name(name_text)
        amount = parse_amount(amount_text)

        ingredient_id: int | None = None
        if normalized and not normalized.suspicious:
            ingredient_id = await get_or_create_ingredient(
                session, normalized.name, normalized.display_name, ingredient_cache, stats
            )

        grams: Decimal | None = None
        if amount is not None and normalized is not None:
            grams = to_grams(amount, ingredient_name=normalized.name)

        if amount is not None and amount.to_taste:
            stats.lines_to_taste += 1
            resolved += 1  # "по вкусу" is understood, just not weighable
        elif grams is not None:
            stats.lines_with_grams += 1
            resolved += 1
        else:
            stats.lines_unresolved += 1

        rows.append(
            {
                "position": line.position,
                "ingredient_id": ingredient_id,
                "grams": grams,
                "raw_text": line.raw_text,
                "raw_amount": amount.value if amount and not amount.to_taste else None,
                "raw_unit": amount.unit.value if amount and amount.unit else None,
                "is_optional": bool(amount and amount.to_taste),
            }
        )

    share_resolved = resolved / max(len(raw.ingredients), 1)
    quarantined = share_resolved < (1 - QUARANTINE_THRESHOLD)

    recipe = await session.scalar(select(Recipe).where(Recipe.source_url == raw.source_url))
    is_new = recipe is None
    if recipe is None:
        recipe = Recipe(source_url=raw.source_url)
        session.add(recipe)

    recipe.title = raw.title
    recipe.source = raw.source
    recipe.description = raw.description
    recipe.meal_type = meal_types
    recipe.cook_minutes = raw.cook_minutes
    recipe.steps = raw.steps
    recipe.tags = [raw.category] if raw.category else []
    recipe.servings = raw.servings or 1
    recipe.status = RecipeStatus.QUARANTINE if quarantined else RecipeStatus.ACTIVE
    recipe.quarantine_reason = (
        f"{int(100 * (1 - share_resolved))}% ингредиентных строк не распознано"
        if quarantined
        else None
    )
    await session.flush()

    # Replace the ingredient set wholesale: simpler than diffing, and correct
    # even when the source reorders or drops lines.
    await session.execute(delete(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id))
    for row in rows:
        session.add(RecipeIngredient(recipe_id=recipe.id, **row))

    if quarantined:
        stats.quarantined += 1
    elif is_new:
        stats.imported += 1
    else:
        stats.updated += 1
    if raw.category:
        stats.by_category[raw.category] += 1


async def run(limit: int, delay: float, dry_run: bool, source_name: str) -> Stats:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_format == "json")

    source = SOURCES[source_name]
    cache = PageCache(PROJECT_ROOT / "data" / "raw" / source_name.replace(".", "_"), delay)
    stats = Stats()
    ingredient_cache: dict[str, int] = {}

    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
        follow_redirects=True,
    ) as client:
        urls = select_urls(source, client, limit)
        log.info("import.start", source=source_name, urls=len(urls), dry_run=dry_run)

        for index, url in enumerate(urls, start=1):
            html = cache.get(url, client, stats)
            if html is None:
                stats.failed += 1
                continue
            stats.seen += 1

            raw = source.parse(url, html)
            if raw is None:
                stats.skipped += 1
                continue

            meal_types = source.meal_types(url) if isinstance(source, EdaSource) else []
            if dry_run:
                for line in raw.ingredients:
                    stats.lines_total += 1
                    name_text, amount_text = split_ingredient_line(line.raw_text)
                    normalized = normalize_name(name_text)
                    amount = parse_amount(amount_text)
                    grams = (
                        to_grams(amount, ingredient_name=normalized.name)
                        if amount and normalized
                        else None
                    )
                    if amount and amount.to_taste:
                        stats.lines_to_taste += 1
                    elif grams is not None:
                        stats.lines_with_grams += 1
                    else:
                        stats.lines_unresolved += 1
                stats.imported += 1
            else:
                try:
                    async with session_scope() as session:
                        await store_recipe(session, raw, meal_types, ingredient_cache, stats)
                except Exception as exc:
                    stats.failed += 1
                    log.warning("store.failed", url=url, error=str(exc))
                    ingredient_cache.clear()  # ids from the rolled-back transaction are gone

            if index % 100 == 0:
                log.info("import.progress", done=index, of=len(urls), **stats.report())

    log.info("import.finished", **stats.report(), categories=dict(stats.by_category))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=4000, help="how many recipes to import")
    parser.add_argument("--delay", type=float, default=2.5, help="seconds between requests")
    parser.add_argument("--source", default=EdaSource.name, choices=sorted(SOURCES))
    parser.add_argument("--dry-run", action="store_true", help="parse only, write nothing")
    args = parser.parse_args(argv)

    async def _main() -> Stats:
        try:
            return await run(args.limit, args.delay, args.dry_run, args.source)
        finally:
            await dispose_engine()

    stats = asyncio.run(_main())
    return 0 if stats.seen else 1


if __name__ == "__main__":
    sys.exit(main())
