from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(slots=True)
class ProductCard:
    external_id: str
    url: str
    name: str
    raw_text: str
    category_slug: str | None = None
    image_url: str | None = None
    pack_text: str | None = None
    pack_quantity: Decimal | None = None
    pack_unit: str | None = None
    price_unit: str | None = None
    regular_price_kop: int | None = None
    loyalty_price_kop: int | None = None
    promo_price_kop: int | None = None
    personal_price_kop: int | None = None
    discount_percent: int | None = None
    available_for_order: bool = True
    purchase_limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.pack_quantity is not None:
            result["pack_quantity"] = str(self.pack_quantity)
        return result


@dataclass(slots=True)
class ProductDetails:
    name: str | None = None
    article: str | None = None
    brand: str | None = None
    composition: str | None = None
    kcal_100: Decimal | None = None
    protein_100: Decimal | None = None
    fat_100: Decimal | None = None
    carb_100: Decimal | None = None
    storage_conditions: str | None = None
    shelf_life_text: str | None = None
    characteristics: dict[str, str] = field(default_factory=dict)
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("kcal_100", "protein_100", "fat_100", "carb_100"):
            value = result[key]
            if value is not None:
                result[key] = str(value)
        return result
