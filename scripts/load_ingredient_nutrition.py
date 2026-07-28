"""Load the curated nutrition reference into the ingredients table.

    python -m scripts.load_ingredient_nutrition
    python -m scripts.load_ingredient_nutrition --report   # coverage only

`data/ingredient_nutrition.csv` is the source of truth: editing a row and
re-running updates the database. Matching is by normalised name, with a
containment fallback so "тертый сыр пармезан" picks up the values of
"сыр пармезан" — the embedding pass in M2 will merge such pairs properly, but
until then a good approximation beats a NULL, because an ingredient without
nutrition disqualifies every recipe that uses it.

Coverage is reported weighted by how often each ingredient actually appears, not
by row count: getting "соль" right matters more than getting "топинамбур" right.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from decimal import Decimal
from pathlib import Path

import structlog
from sqlalchemy import func, select

from app.core.config import PROJECT_ROOT, get_settings
from app.core.db import dispose_engine, session_scope
from app.core.logging import configure_logging
from app.core.models import Ingredient, RecipeIngredient
from app.recipes.normalize import normalize_name

log = structlog.get_logger("load_nutrition")

CSV_PATH = PROJECT_ROOT / "data" / "ingredient_nutrition.csv"


def _decimal(value: str) -> Decimal | None:
    value = value.strip()
    return Decimal(value) if value else None


def read_reference(path: Path = CSV_PATH) -> dict[str, dict[str, object]]:
    """Read the CSV, keyed by normalised name."""
    table: dict[str, dict[str, object]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        rows = (line for line in handle if not line.lstrip().startswith("#"))
        for row in csv.DictReader(rows):
            raw_name = (row.get("name") or "").strip()
            if not raw_name:
                continue
            normalized = normalize_name(raw_name)
            key = normalized.name if normalized else raw_name.lower()
            table[key] = {
                "category": (row.get("category") or "").strip() or None,
                "kcal_100": _decimal(row.get("kcal_100", "")),
                "protein_100": _decimal(row.get("protein_100", "")),
                "fat_100": _decimal(row.get("fat_100", "")),
                "carb_100": _decimal(row.get("carb_100", "")),
                "density_g_per_ml": _decimal(row.get("density_g_per_ml", "")),
                "piece_grams": _decimal(row.get("piece_grams", "")),
            }
    return table


def best_match(name: str, table: dict[str, dict[str, object]]) -> tuple[str, str] | None:
    """Exact name first, then the longest reference name contained in it.

    Containment is directional on purpose: "тертый сыр пармезан" contains
    "сыр пармезан" and inherits it, but "сыр" must not claim every cheese —
    hence longest-match-wins and a minimum length.
    """
    if name in table:
        return name, "exact"
    best: tuple[int, str] | None = None
    for candidate in table:
        if len(candidate) >= 4 and candidate in name and (best is None or len(candidate) > best[0]):
            best = (len(candidate), candidate)
    return (best[1], "contains") if best else None


async def run(report_only: bool) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_format == "json")
    table = read_reference()
    log.info("reference.loaded", entries=len(table))

    exact = contains = missed = 0
    missing_top: list[tuple[str, int]] = []

    async with session_scope() as session:
        usage = dict(
            (
                await session.execute(
                    select(Ingredient.name, func.count(RecipeIngredient.id))
                    .join(RecipeIngredient, RecipeIngredient.ingredient_id == Ingredient.id)
                    .group_by(Ingredient.name)
                )
            ).all()
        )
        ingredients = (await session.scalars(select(Ingredient))).all()

        for ingredient in ingredients:
            match = best_match(ingredient.name, table)
            if match is None:
                missed += 1
                if usage.get(ingredient.name):
                    missing_top.append((ingredient.name, usage[ingredient.name]))
                continue

            key, how = match
            exact += how == "exact"
            contains += how == "contains"
            if report_only:
                continue

            values = table[key]
            ingredient.kcal_100 = values["kcal_100"]  # type: ignore[assignment]
            ingredient.protein_100 = values["protein_100"]  # type: ignore[assignment]
            ingredient.fat_100 = values["fat_100"]  # type: ignore[assignment]
            ingredient.carb_100 = values["carb_100"]  # type: ignore[assignment]
            ingredient.nutrition_source = "skurikhin"
            if values["category"]:
                ingredient.category = values["category"]  # type: ignore[assignment]
            if values["density_g_per_ml"] is not None:
                ingredient.density_g_per_ml = values["density_g_per_ml"]  # type: ignore[assignment]
            if values["piece_grams"] is not None:
                ingredient.piece_grams = values["piece_grams"]  # type: ignore[assignment]

    # Coverage weighted by real usage: a missing "соль" costs far more recipes
    # than a missing rare ingredient.
    total_uses = sum(usage.values())
    covered_uses = sum(n for name, n in usage.items() if best_match(name, table))
    log.info(
        "nutrition.loaded" if not report_only else "nutrition.report",
        ingredients=len(ingredients),
        matched_exact=exact,
        matched_contains=contains,
        unmatched=missed,
        coverage_by_rows=f"{100 * (exact + contains) // max(len(ingredients), 1)}%",
        coverage_by_usage=f"{100 * covered_uses // max(total_uses, 1)}%",
    )

    missing_top.sort(key=lambda pair: -pair[1])
    if missing_top:
        log.info(
            "nutrition.missing_top",
            items=[f"{name} ({count})" for name, count in missing_top[:25]],
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="report coverage, write nothing")
    args = parser.parse_args(argv)

    async def _main() -> int:
        try:
            return await run(args.report)
        finally:
            await dispose_engine()

    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
