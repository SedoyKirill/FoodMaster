"""Turning "2 столовые ложки" into grams.

Everything downstream — nutrition, portions, the shopping optimizer — works in
grams of the raw product, so this is where recipe text stops being text.

Three sources of truth, in priority order:

1. The ingredient's own physical constants (``density_g_per_ml``,
   ``piece_grams``) when the ingredient is already known. A glass of milk and a
   glass of flour are not the same weight.
2. The generic tables below, used when the ingredient is unknown or has no
   constants recorded.
3. Nothing — the amount stays unconvertible and the line keeps `grams = NULL`,
   which the planner treats as "по вкусу" and ignores.

Amounts are ``Decimal`` throughout: M2's acceptance criterion asserts nutrition
within +-2%, and float noise has no place in a number that is then multiplied by
portions and summed across a four-day plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Unit(StrEnum):
    """Canonical units. Everything a source writes is mapped onto these."""

    GRAM = "g"
    KILOGRAM = "kg"
    MILLILITRE = "ml"
    LITRE = "l"
    PIECE = "piece"
    TABLESPOON = "tbsp"
    TEASPOON = "tsp"
    GLASS = "glass"
    CLOVE = "clove"  # зубчик чеснока
    HEAD = "head"  # головка чеснока/лука
    BUNCH = "bunch"  # пучок зелени
    STALK = "stalk"  # стебель сельдерея
    CAN = "can"  # банка
    PINCH = "pinch"  # щепотка, на кончике ножа
    SLICE = "slice"  # долька, кусочек
    SPRIG = "sprig"  # веточка
    DROP = "drop"  # капля
    HANDFUL = "handful"  # горсть
    PACK = "pack"  # упаковка
    SACHET = "sachet"  # пакетик
    LEAF = "leaf"  # лист


#: Every spelling seen in the wild, including Russian declensions, mapped to a
#: canonical unit. Sources write "столовая ложка", "столовые ложки" and
#: "столовых ложек" for the same thing.
UNIT_ALIASES: dict[str, Unit] = {
    # mass
    "г": Unit.GRAM,
    "гр": Unit.GRAM,
    "грамм": Unit.GRAM,
    "грамма": Unit.GRAM,
    "граммов": Unit.GRAM,
    "граммы": Unit.GRAM,
    "кг": Unit.KILOGRAM,
    "килограмм": Unit.KILOGRAM,
    "килограмма": Unit.KILOGRAM,
    "килограммов": Unit.KILOGRAM,
    # volume
    "мл": Unit.MILLILITRE,
    "миллилитр": Unit.MILLILITRE,
    "миллилитра": Unit.MILLILITRE,
    "миллилитров": Unit.MILLILITRE,
    "л": Unit.LITRE,
    "литр": Unit.LITRE,
    "литра": Unit.LITRE,
    "литров": Unit.LITRE,
    # countable
    "шт": Unit.PIECE,
    "штука": Unit.PIECE,
    "штуки": Unit.PIECE,
    "штук": Unit.PIECE,
    "штуку": Unit.PIECE,
    # spoons and glasses
    "ст.л": Unit.TABLESPOON,
    "ст. л": Unit.TABLESPOON,
    "стл": Unit.TABLESPOON,
    "столовая ложка": Unit.TABLESPOON,
    "столовые ложки": Unit.TABLESPOON,
    "столовых ложек": Unit.TABLESPOON,
    "столовой ложки": Unit.TABLESPOON,
    "ч.л": Unit.TEASPOON,
    "ч. л": Unit.TEASPOON,
    "чл": Unit.TEASPOON,
    "чайная ложка": Unit.TEASPOON,
    "чайные ложки": Unit.TEASPOON,
    "чайных ложек": Unit.TEASPOON,
    "чайной ложки": Unit.TEASPOON,
    "стакан": Unit.GLASS,
    "стакана": Unit.GLASS,
    "стаканов": Unit.GLASS,
    # botanical / packaging
    "зубчик": Unit.CLOVE,
    "зубчика": Unit.CLOVE,
    "зубчиков": Unit.CLOVE,
    "зуб": Unit.CLOVE,
    "головка": Unit.HEAD,
    "головки": Unit.HEAD,
    "головок": Unit.HEAD,
    "пучок": Unit.BUNCH,
    "пучка": Unit.BUNCH,
    "пучков": Unit.BUNCH,
    "стебель": Unit.STALK,
    "стебля": Unit.STALK,
    "стеблей": Unit.STALK,
    "банка": Unit.CAN,
    "банки": Unit.CAN,
    "банок": Unit.CAN,
    "щепотка": Unit.PINCH,
    "щепотки": Unit.PINCH,
    "щепоток": Unit.PINCH,
    "на кончике ножа": Unit.PINCH,
    "долька": Unit.SLICE,
    "дольки": Unit.SLICE,
    "долек": Unit.SLICE,
    "дол": Unit.SLICE,
    "кусочек": Unit.SLICE,
    "кусочка": Unit.SLICE,
    "кусочков": Unit.SLICE,
    "веточка": Unit.SPRIG,
    "веточки": Unit.SPRIG,
    "веточек": Unit.SPRIG,
    "веточ": Unit.SPRIG,
    "капля": Unit.DROP,
    "капли": Unit.DROP,
    "капель": Unit.DROP,
    "горсть": Unit.HANDFUL,
    "горсти": Unit.HANDFUL,
    "упаковка": Unit.PACK,
    "упаковки": Unit.PACK,
    "уп": Unit.PACK,
    "пакетик": Unit.SACHET,
    "пакетика": Unit.SACHET,
    "пакетиков": Unit.SACHET,
    "лист": Unit.LEAF,
    "листа": Unit.LEAF,
    "листов": Unit.LEAF,
    "листьев": Unit.LEAF,
}

#: Units that convert without knowing which ingredient it is.
ABSOLUTE_GRAMS: dict[Unit, Decimal] = {
    Unit.GRAM: Decimal(1),
    Unit.KILOGRAM: Decimal(1000),
}

#: Volume units, in millilitres. Turning these into grams needs a density.
ABSOLUTE_MILLILITRES: dict[Unit, Decimal] = {
    Unit.MILLILITRE: Decimal(1),
    Unit.LITRE: Decimal(1000),
    Unit.GLASS: Decimal(200),
    Unit.TABLESPOON: Decimal(15),
    Unit.TEASPOON: Decimal(5),
}

#: Fallback weights for units that are neither mass nor volume, used when the
#: ingredient has no `piece_grams` of its own. Deliberately conservative: a
#: wrong small number distorts a plan less than a wrong large one.
GENERIC_PIECE_GRAMS: dict[Unit, Decimal] = {
    Unit.PIECE: Decimal(100),
    Unit.CLOVE: Decimal(5),
    Unit.HEAD: Decimal(50),
    Unit.BUNCH: Decimal(40),
    Unit.STALK: Decimal(40),
    Unit.CAN: Decimal(400),
    Unit.PINCH: Decimal("0.5"),
    Unit.SLICE: Decimal(20),
    Unit.SPRIG: Decimal(3),
    Unit.DROP: Decimal("0.05"),
    Unit.HANDFUL: Decimal(30),
    Unit.PACK: Decimal(200),
    Unit.SACHET: Decimal(10),
    Unit.LEAF: Decimal(1),
}

#: Average weight of one piece, by normalised ingredient name. This is the
#: table TZ-M2 asks for ("справочник средних весов штучных продуктов"). It is a
#: seed: `ingredients.piece_grams` overrides it once an ingredient is known, and
#: the admin UI can correct it without a code change.
PIECE_WEIGHTS: dict[str, Decimal] = {
    "яйцо": Decimal(55),
    "яйцо куриное": Decimal(55),
    "перепелиное яйцо": Decimal(12),
    "яичный желток": Decimal(18),
    "яичный белок": Decimal(35),
    "лук репчатый": Decimal(80),
    "репчатый лук": Decimal(80),
    "картофель": Decimal(100),
    "морковь": Decimal(75),
    "помидор": Decimal(120),
    "томат": Decimal(120),
    "огурец": Decimal(100),
    "болгарский перец": Decimal(150),
    "сладкий перец": Decimal(150),
    "баклажан": Decimal(250),
    "кабачок": Decimal(300),
    "яблоко": Decimal(180),
    "банан": Decimal(120),
    "лимон": Decimal(100),
    "лайм": Decimal(60),
    "апельсин": Decimal(180),
    "авокадо": Decimal(200),
    "чеснок": Decimal(40),  # a whole head; a clove is handled by Unit.CLOVE
    "куриное филе": Decimal(180),
    "куриное яйцо": Decimal(55),
    "куриная грудка": Decimal(250),
    "куриное бедро": Decimal(120),
    "шампиньоны": Decimal(20),
    "свекла": Decimal(200),
    "капуста": Decimal(1500),
    "цветная капуста": Decimal(800),
    "брокколи": Decimal(400),
    "лаваш": Decimal(100),
    "багет": Decimal(250),
    "тортилья": Decimal(50),
    "лавровый лист": Decimal("0.2"),
}

#: Ingredients whose volume->mass conversion is nowhere near water.
DENSITY_G_PER_ML: dict[str, Decimal] = {
    "мука": Decimal("0.55"),
    "пшеничная мука": Decimal("0.55"),
    "сахар": Decimal("0.85"),
    "соль": Decimal("1.20"),
    "растительное масло": Decimal("0.92"),
    "оливковое масло": Decimal("0.92"),
    "подсолнечное масло": Decimal("0.92"),
    "сливочное масло": Decimal("0.91"),
    "мед": Decimal("1.40"),
    "мёд": Decimal("1.40"),
    "сметана": Decimal("1.00"),
    "молоко": Decimal("1.03"),
    "сливки": Decimal("1.00"),
    "рис": Decimal("0.85"),
    "гречка": Decimal("0.80"),
    "овсяные хлопья": Decimal("0.35"),
    "манная крупа": Decimal("0.80"),
    "панировочные сухари": Decimal("0.45"),
    "какао": Decimal("0.45"),
    "крахмал": Decimal("0.65"),
    "молотые орехи": Decimal("0.60"),
}

DEFAULT_DENSITY = Decimal("1.00")

#: "по вкусу", "для подачи" and friends: a real ingredient with no amount.
NO_AMOUNT_MARKERS = re.compile(
    r"по вкусу|на кончике ножа|для (?:смазыв|подач|украш|жарк|фритюр|теста)|при подаче|"
    r"сколько понадобится|по желанию",
    re.I,
)

_NUMBER = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*(?:/\s*(\d+))?\s*(.*)$",
)
_UNICODE_FRACTIONS = {"½": "0.5", "¼": "0.25", "¾": "0.75", "⅓": "0.33", "⅔": "0.67"}


@dataclass(frozen=True)
class Amount:
    """A parsed quantity: how much, in what unit."""

    value: Decimal
    unit: Unit | None
    #: True when the source said "по вкусу" rather than giving a number.
    to_taste: bool = False


def parse_amount(text: str) -> Amount | None:
    """Parse the tail of an ingredient line, e.g. "2 столовые ложки"."""
    raw = " ".join(text.split())
    if not raw:
        return None
    if NO_AMOUNT_MARKERS.search(raw):
        return Amount(Decimal(0), None, to_taste=True)

    for glyph, replacement in _UNICODE_FRACTIONS.items():
        raw = raw.replace(glyph, replacement)

    match = _NUMBER.match(raw)
    if not match:
        # No number at all, but maybe a bare unit ("щепотка").
        unit = lookup_unit(raw)
        return Amount(Decimal(1), unit) if unit else None

    whole, denominator, rest = match.groups()
    value = Decimal(whole.replace(",", "."))
    if denominator:
        value = value / Decimal(denominator)
    return Amount(value, lookup_unit(rest))


def lookup_unit(text: str) -> Unit | None:
    """Map a unit as written to a canonical one."""
    cleaned = " ".join(text.lower().replace(".", ". ").split()).strip(" .")
    if not cleaned:
        return None
    if cleaned in UNIT_ALIASES:
        return UNIT_ALIASES[cleaned]
    # "столовые ложки с горкой" -> try progressively shorter prefixes.
    words = cleaned.split()
    for size in range(min(3, len(words)), 0, -1):
        candidate = " ".join(words[:size])
        if candidate in UNIT_ALIASES:
            return UNIT_ALIASES[candidate]
    return None


def to_grams(
    amount: Amount,
    *,
    ingredient_name: str | None = None,
    piece_grams: Decimal | None = None,
    density_g_per_ml: Decimal | None = None,
) -> Decimal | None:
    """Convert a parsed amount to grams of raw product.

    Returns None when the amount cannot be expressed in grams — "по вкусу", or a
    unit we do not understand. Callers store NULL rather than guessing: a wrong
    number silently corrupts every calorie downstream, a NULL is visible.
    """
    if amount.to_taste or amount.unit is None:
        return None

    unit = amount.unit

    if unit in ABSOLUTE_GRAMS:
        return amount.value * ABSOLUTE_GRAMS[unit]

    if unit in ABSOLUTE_MILLILITRES:
        density = density_g_per_ml
        if density is None and ingredient_name:
            density = lookup_density(ingredient_name)
        return amount.value * ABSOLUTE_MILLILITRES[unit] * (density or DEFAULT_DENSITY)

    # Countable units: the ingredient's own weight wins over the generic one.
    if unit == Unit.PIECE:
        per_piece = piece_grams
        if per_piece is None and ingredient_name:
            per_piece = lookup_piece_weight(ingredient_name)
        return amount.value * (per_piece or GENERIC_PIECE_GRAMS[Unit.PIECE])

    if unit in GENERIC_PIECE_GRAMS:
        return amount.value * GENERIC_PIECE_GRAMS[unit]

    return None


def lookup_piece_weight(name: str) -> Decimal | None:
    return _lookup(PIECE_WEIGHTS, name)


def lookup_density(name: str) -> Decimal | None:
    return _lookup(DENSITY_G_PER_ML, name)


def _lookup(table: dict[str, Decimal], name: str) -> Decimal | None:
    """Exact match first, then the longest table key contained in the name."""
    key = " ".join(name.lower().split())
    if key in table:
        return table[key]
    best: tuple[int, Decimal] | None = None
    for candidate, value in table.items():
        if candidate in key and (best is None or len(candidate) > best[0]):
            best = (len(candidate), value)
    return best[1] if best else None
