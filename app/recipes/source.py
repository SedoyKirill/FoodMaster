"""Recipe sources behind one interface.

The importer talks to `RecipeSource`, not to a site. Adding povarenok.ru later —
or replacing eda.rambler.ru when its markup changes — is a new implementation,
not a rewrite, which is the same principle M3 applies to store adapters.

Source choice (recorded here because it contradicts the tentative preference in
TZ-M2, which asks for the decision to be made "по итогам разведки"):

    Sampled 20 recipes from each site, measuring the share of ingredient lines
    that can be turned into grams, plus whether servings and cooking time are
    machine-readable at all.

                          povarenok.ru   eda.rambler.ru
      lines with amounts        79%            81%
      servings present        12/20          20/20
      cooking time            12/20          20/20
      markup            microdata + HTML   schema.org JSON-LD
      encoding            windows-1251        UTF-8
      sitemap                    404      57 167 recipes

    The deciding factor is servings. Without it there is no per-portion
    nutrition, so M4 cannot size a portion and M5 cannot match a dish to a
    family member's calorie budget — a recipe without servings is unusable for
    the planner, and povarenok omits it for 40% of recipes. eda.rambler.ru also
    publishes proper JSON-LD, which is a far more stable contract than scraping
    class names, and puts the category in the URL.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import httpx
import structlog
from selectolax.parser import HTMLParser

log = structlog.get_logger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class RawIngredient:
    """One ingredient line exactly as the source stated it."""

    raw_text: str
    position: int


@dataclass(frozen=True)
class RawRecipe:
    """A recipe as scraped, before any normalisation."""

    source: str
    source_url: str
    title: str
    ingredients: list[RawIngredient]
    steps: list[str] = field(default_factory=list)
    servings: int | None = None
    cook_minutes: int | None = None
    category: str | None = None
    description: str | None = None
    #: Nutrition per recipe as published by the site. Not authoritative — M4
    #: computes its own from ingredients — but useful to sanity-check that
    #: computation against a second opinion.
    published_kcal: float | None = None


class RecipeSource(Protocol):
    """What the importer needs from any recipe site."""

    name: str

    def list_urls(self, client: httpx.Client) -> Iterator[str]:
        """Yield recipe page URLs, cheapest enumeration first."""
        ...

    def parse(self, url: str, html: str) -> RawRecipe | None:
        """Turn one fetched page into a RawRecipe, or None if it is unusable."""
        ...


_ISO_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?")


def parse_iso_duration(value: str | None) -> int | None:
    """ "PT1H30M" -> 90 minutes."""
    if not value:
        return None
    match = _ISO_DURATION.fullmatch(value.strip())
    if not match:
        digits = re.search(r"\d+", value)
        return int(digits.group()) if digits else None
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    total = days * 1440 + hours * 60 + minutes
    return total or None


def _first_int(value: object) -> int | None:
    if isinstance(value, int):
        return value or None
    if isinstance(value, list) and value:
        return _first_int(value[0])
    if isinstance(value, str):
        digits = re.search(r"\d+", value)
        return int(digits.group()) if digits else None
    return None


class EdaSource:
    """eda.rambler.ru — schema.org Recipe in JSON-LD."""

    name = "eda.rambler.ru"
    base = "https://eda.rambler.ru"

    #: robots.txt allows /recepty/; these are the sitemap shards holding them.
    SITEMAPS = (
        "https://eda.rambler.ru/sitemap/map_RecipePagesGroup_https_0.xml.gz",
        "https://eda.rambler.ru/sitemap/map_RecipePagesGroup_https_1.xml.gz",
    )

    #: URL segment -> the meal types a dish of that category can serve.
    #: Taken from the real category distribution found during recon.
    CATEGORY_MEAL_TYPES: ClassVar[dict[str, tuple[str, ...]]] = {
        "zavtraki": ("breakfast",),
        "supy": ("lunch", "dinner"),
        "bulony": ("lunch", "dinner"),
        "osnovnye-blyuda": ("lunch", "dinner"),
        "pasta-picca": ("lunch", "dinner"),
        "rizotto": ("lunch", "dinner"),
        "salaty": ("lunch", "dinner"),
        "zakuski": ("snack",),
        "sendvichi": ("breakfast", "snack"),
        "vypechka-deserty": ("snack",),
        "sousy-marinady": (),
        "napitki": (),
        "zagotovki": (),
    }

    #: Categories worth importing for a meal planner. Sauces, drinks and
    #: preserves are components or beverages, not dishes to plan a day around.
    WANTED = (
        "zavtraki",
        "supy",
        "bulony",
        "osnovnye-blyuda",
        "salaty",
        "zakuski",
        "pasta-picca",
        "rizotto",
        "sendvichi",
    )

    def list_urls(self, client: httpx.Client) -> Iterator[str]:
        for sitemap in self.SITEMAPS:
            response = client.get(sitemap, timeout=60)
            response.raise_for_status()
            body = response.content
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            text = body.decode("utf-8", "replace")
            for url in re.findall(r"<loc>([^<]+)</loc>", text):
                if "/recepty/" in url and self.category_of(url) in self.WANTED:
                    yield url

    def category_of(self, url: str) -> str | None:
        if "/recepty/" not in url:
            return None
        tail = url.split("/recepty/", 1)[1]
        return tail.split("/", 1)[0] or None

    def meal_types(self, url: str) -> list[str]:
        return list(self.CATEGORY_MEAL_TYPES.get(self.category_of(url) or "", ()))

    def parse(self, url: str, html: str) -> RawRecipe | None:
        recipe: dict[str, Any] | None = self._extract_recipe_ld(html)
        if not recipe:
            log.debug("recipe.no_jsonld", url=url)
            return None

        title = str(recipe.get("name") or "").strip()
        raw_ingredients = [
            RawIngredient(raw_text=str(item).strip(), position=index)
            for index, item in enumerate(recipe.get("recipeIngredient") or [])
            if str(item).strip()
        ]
        if not title or not raw_ingredients:
            return None

        steps: list[str] = []
        for step in recipe.get("recipeInstructions") or []:
            text = step.get("text") or step.get("name") if isinstance(step, dict) else step
            if text and str(text).strip():
                steps.append(" ".join(str(text).split()))

        nutrition = recipe.get("nutrition")
        kcal = None
        if isinstance(nutrition, dict):
            for key in ("kilocalories", "calories"):
                raw = nutrition.get(key)
                if raw is not None:
                    digits = re.search(r"\d+(?:[.,]\d+)?", str(raw))
                    if digits:
                        kcal = float(digits.group().replace(",", "."))
                    break

        return RawRecipe(
            source=self.name,
            source_url=url,
            title=" ".join(title.split()),
            ingredients=raw_ingredients,
            steps=steps,
            servings=_first_int(recipe.get("recipeYield")),
            cook_minutes=parse_iso_duration(recipe.get("totalTime") or recipe.get("cookTime")),
            category=self.category_of(url),
            description=(str(recipe.get("description")).strip() or None)
            if recipe.get("description")
            else None,
            published_kcal=kcal,
        )

    @staticmethod
    def _extract_recipe_ld(html: str) -> dict[str, Any] | None:
        for node in HTMLParser(html).css('script[type="application/ld+json"]'):
            try:
                data = json.loads(node.text())
            except ValueError:
                continue
            for candidate in data if isinstance(data, list) else [data]:
                if isinstance(candidate, dict) and candidate.get("@type") == "Recipe":
                    return candidate
        return None


SOURCES: dict[str, RecipeSource] = {EdaSource.name: EdaSource()}
