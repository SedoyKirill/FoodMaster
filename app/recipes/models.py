from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(slots=True)
class ExtractedPage:
    page_number: int
    method: str
    text: str
    white_ratio: Decimal | None = None
    error: str | None = None


@dataclass(slots=True)
class ParsedIngredient:
    raw_text: str
    ingredient_text: str
    quantity_min: Decimal | None = None
    quantity_max: Decimal | None = None
    unit_raw: str | None = None
    unit_code: str | None = None
    normalized_name: str | None = None
    confidence: Decimal = Decimal("0.5")
    is_to_taste: bool = False
    section: str | None = None
    note: str | None = None


@dataclass(slots=True)
class RecipeCandidate:
    title: str
    page_start: int
    page_end: int
    raw_text: str
    source_servings_min: Decimal | None
    source_servings_max: Decimal | None
    source_yield_text: str | None
    cuisine_code: str | None
    cuisine_confidence: Decimal | None
    meal_types: list[str]
    diet_tags: list[str]
    appliances: list[str]
    confidence: Decimal
    ingredients: list[ParsedIngredient] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    time_total_minutes: int | None = None
    extraction_method: str = "heuristic"
    review_status: str = "needs_review"
    review_reasons: list[str] = field(default_factory=list)

    def to_report_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("raw_text", None)
        for key in ("source_servings_min", "source_servings_max"):
            value = result[key]
            result[key] = str(value) if value is not None else None
        result["confidence"] = str(self.confidence)
        result["cuisine_confidence"] = (
            str(self.cuisine_confidence) if self.cuisine_confidence is not None else None
        )
        result["ingredient_count"] = len(self.ingredients)
        result["step_count"] = len(self.steps)
        result.pop("ingredients", None)
        result.pop("steps", None)
        return result
