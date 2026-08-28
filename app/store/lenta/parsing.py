from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlparse

from .models import ProductCard, ProductDetails


MONEY_RE = re.compile(r"(?P<amount>\d[\d \u00a0\u202f]*[,.]\d{2})[ \u00a0\u202f]*₽")
PRODUCT_ID_RE = re.compile(r"-(?P<id>\d+)/?$")
DISCOUNT_RE = re.compile(r"-(?P<discount>\d{1,3})\s*%")
LIMIT_RE = re.compile(r"\bдо\s+(?P<limit>\d+)\s*шт\b", re.IGNORECASE)
UNAVAILABLE_MARKERS = (
    "нет в наличии",
    "нет в продаже",
    "недоступен для заказа",
    "недоступно для заказа",
)
PRICE_UNIT_RE = re.compile(
    r"Цена\s+за\s+(?P<amount>\d+(?:[.,]\d+)?)?\s*(?P<unit>кг|г|л|мл|шт)",
    re.IGNORECASE,
)
MULTIPACK_RE = re.compile(
    r"(?P<count>\d+)\s*[xх×*]\s*(?P<size>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>кг|г|л|мл|шт)\b",
    re.IGNORECASE,
)
PACK_RE = re.compile(
    r"(?<!\d)(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>кг|г|л|мл|шт)\b",
    re.IGNORECASE,
)
RATING_RE = re.compile(r"^[0-5](?:[.,]\d)?$")
NUTRITION_RE = re.compile(
    r"Белки\s*[–—-]\s*(?P<protein>\d+(?:[.,]\d+)?)\s*г?\s*,?\s*"
    r"жиры\s*[–—-]\s*(?P<fat>\d+(?:[.,]\d+)?)\s*г?\s*,?\s*"
    r"углеводы\s*[–—-]\s*(?P<carb>\d+(?:[.,]\d+)?)\s*г?",
    re.IGNORECASE,
)
KCAL_RE = re.compile(r"(?P<kcal>\d+(?:[.,]\d+)?)\s*кКал", re.IGNORECASE)


def _lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value.replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except InvalidOperation:
        return None


def money_to_kopecks(value: str) -> int:
    amount = _decimal(value)
    if amount is None:
        raise ValueError(f"Invalid money value: {value!r}")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def extract_product_id(url: str) -> str:
    path = urlparse(url).path.rstrip("/") + "/"
    match = PRODUCT_ID_RE.search(path)
    if not match:
        raise ValueError(f"Product id is absent in URL: {url}")
    return match.group("id")


def _canonical_quantity(amount: Decimal, unit: str) -> tuple[Decimal, str]:
    normalized = unit.lower()
    if normalized == "кг":
        return amount * 1000, "g"
    if normalized == "г":
        return amount, "g"
    if normalized == "л":
        return amount * 1000, "ml"
    if normalized == "мл":
        return amount, "ml"
    return amount, "piece"


def parse_pack(text: str) -> tuple[str | None, Decimal | None, str | None]:
    multipack = MULTIPACK_RE.search(text)
    if multipack:
        count = _decimal(multipack.group("count"))
        size = _decimal(multipack.group("size"))
        if count is not None and size is not None:
            quantity, unit = _canonical_quantity(count * size, multipack.group("unit"))
            return multipack.group(0), quantity, unit

    match = PACK_RE.search(text)
    if not match:
        return None, None, None
    amount = _decimal(match.group("amount"))
    if amount is None:
        return match.group(0), None, None
    quantity, unit = _canonical_quantity(amount, match.group("unit"))
    return match.group(0), quantity, unit


def _find_name_and_pack(lines: list[str], price_index: int) -> tuple[str, str | None, Decimal | None, str | None]:
    prefix = lines[:price_index]
    pack_index: int | None = None
    pack_result: tuple[str | None, Decimal | None, str | None] = (None, None, None)

    for index in range(len(prefix) - 1, -1, -1):
        line = prefix[index]
        if LIMIT_RE.search(line) or line.lower().startswith("цена за"):
            continue
        candidate = parse_pack(line)
        if candidate[0] is not None:
            pack_index = index
            pack_result = candidate
            break

    if pack_index is not None:
        name_index = pack_index - 1
        while name_index >= 0 and RATING_RE.match(prefix[name_index]):
            name_index -= 1
        if name_index >= 0:
            return prefix[name_index], *pack_result

    rating_index = next((i for i, line in enumerate(prefix) if RATING_RE.match(line)), None)
    if rating_index is not None and rating_index + 1 < len(prefix):
        name = prefix[rating_index + 1]
    elif prefix:
        name = prefix[-1]
    else:
        name = ""

    fallback_pack = parse_pack(name)
    return name, *fallback_pack


def parse_product_card_text(
    text: str,
    url: str,
    *,
    category_slug: str | None = None,
    image_url: str | None = None,
) -> ProductCard:
    lines = _lines(text)
    price_index = next(
        (index for index, line in enumerate(lines) if line.lower().startswith("цена за")),
        len(lines),
    )
    name, pack_text, pack_quantity, pack_unit = _find_name_and_pack(lines, price_index)

    price_unit = None
    if price_index < len(lines):
        price_unit_match = PRICE_UNIT_RE.search(lines[price_index])
        if price_unit_match:
            _, price_unit = _canonical_quantity(Decimal("1"), price_unit_match.group("unit"))

    prices = [money_to_kopecks(match.group("amount")) for match in MONEY_RE.finditer(text)]
    has_loyalty = any("картой №1" in line.lower() for line in lines)
    has_personal = any("персональ" in line.lower() for line in lines)

    loyalty_price = None
    regular_price = None
    personal_price = None
    if has_personal and prices:
        personal_price = prices[0]
        if len(prices) > 1:
            loyalty_price = prices[1]
        if len(prices) > 2:
            regular_price = prices[2]
    elif has_loyalty and prices:
        loyalty_price = prices[0]
        if len(prices) > 1:
            regular_price = prices[1]
    elif prices:
        regular_price = prices[0]

    discount_match = DISCOUNT_RE.search(text)
    limit_match = LIMIT_RE.search(text)

    return ProductCard(
        external_id=extract_product_id(url),
        url=url,
        name=name,
        raw_text=text,
        category_slug=category_slug,
        image_url=image_url,
        pack_text=pack_text,
        pack_quantity=pack_quantity,
        pack_unit=pack_unit,
        price_unit=price_unit,
        regular_price_kop=regular_price,
        loyalty_price_kop=loyalty_price,
        personal_price_kop=personal_price,
        discount_percent=int(discount_match.group("discount")) if discount_match else None,
        available_for_order=not any(marker in text.lower() for marker in UNAVAILABLE_MARKERS),
        purchase_limit=int(limit_match.group("limit")) if limit_match else None,
    )


def _section(lines: list[str], start: str, end: str) -> list[str]:
    try:
        start_index = lines.index(start) + 1
    except ValueError:
        return []
    try:
        end_index = lines.index(end, start_index)
    except ValueError:
        end_index = len(lines)
    return lines[start_index:end_index]


def parse_product_details_text(text: str) -> ProductDetails:
    lines = _lines(text)
    name = None
    article = None
    for index, line in enumerate(lines):
        if line.startswith("Арт. "):
            article = line.removeprefix("Арт. ").strip()
            if index > 0:
                name = lines[index - 1]
            break

    nutrition_match = NUTRITION_RE.search(text)
    composition_lines = _section(lines, "Состав", "Характеристики")
    composition = " ".join(composition_lines).strip() or None

    characteristics_lines = [
        line
        for line in _section(lines, "Характеристики", "Описание")
        if line not in {"Показать полностью", "Скрыть"}
    ]
    characteristics: dict[str, str] = {}
    for index in range(0, len(characteristics_lines) - 1, 2):
        characteristics[characteristics_lines[index]] = characteristics_lines[index + 1]

    kcal = None
    energy_value = characteristics.get("Энергетическая ценность")
    if energy_value:
        kcal_match = KCAL_RE.search(energy_value)
        if kcal_match:
            kcal = _decimal(kcal_match.group("kcal"))

    shelf_life = characteristics.get("Срок годности") or characteristics.get("Срок хранения")

    return ProductDetails(
        name=name,
        article=article or characteristics.get("Артикул"),
        brand=characteristics.get("Бренд"),
        composition=composition,
        kcal_100=kcal,
        protein_100=_decimal(nutrition_match.group("protein")) if nutrition_match else None,
        fat_100=_decimal(nutrition_match.group("fat")) if nutrition_match else None,
        carb_100=_decimal(nutrition_match.group("carb")) if nutrition_match else None,
        storage_conditions=characteristics.get("Условия хранения"),
        shelf_life_text=shelf_life,
        characteristics=characteristics,
        raw_text=text,
    )
