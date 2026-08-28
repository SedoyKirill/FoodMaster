from __future__ import annotations

from decimal import Decimal


# Небольшой прозрачный справочник только для предварительной оценки MVP.
# Значения приблизительные, ккал на 100 г/мл.
KCAL_RULES: tuple[tuple[tuple[str, ...], Decimal], ...] = (
    (("масло", "сливоч"), Decimal("748")),
    (("масло",), Decimal("899")),
    (("мука",), Decimal("334")),
    (("сахар",), Decimal("387")),
    (("молоко",), Decimal("52")),
    (("кефир",), Decimal("53")),
    (("сливки",), Decimal("205")),
    (("сметан",), Decimal("206")),
    (("сыр",), Decimal("350")),
    (("творог",), Decimal("121")),
    (("яйц",), Decimal("157")),
    (("куриц",), Decimal("165")),
    (("индей",), Decimal("144")),
    (("говядин",), Decimal("250")),
    (("свинин",), Decimal("242")),
    (("фарш",), Decimal("250")),
    (("рыб",), Decimal("140")),
    (("лосос",), Decimal("208")),
    (("рис",), Decimal("344")),
    (("греч",), Decimal("343")),
    (("овся",), Decimal("366")),
    (("макарон",), Decimal("350")),
    (("картоф",), Decimal("77")),
    (("морков",), Decimal("41")),
    (("лук",), Decimal("40")),
    (("томат",), Decimal("18")),
    (("помидор",), Decimal("18")),
    (("капуст",), Decimal("25")),
    (("кабач",), Decimal("24")),
    (("тыкв",), Decimal("26")),
    (("банан",), Decimal("89")),
    (("яблок",), Decimal("52")),
    (("шоколад",), Decimal("546")),
    (("мед",), Decimal("304")),
    (("орех",), Decimal("620")),
)

PIECE_MASS_G: tuple[tuple[tuple[str, ...], Decimal], ...] = (
    (("яйц",), Decimal("55")),
    (("лук",), Decimal("100")),
    (("морков",), Decimal("90")),
    (("яблок",), Decimal("160")),
    (("банан",), Decimal("120")),
)


def kcal_per_100(name: str) -> Decimal | None:
    lowered = name.lower()
    for needles, value in KCAL_RULES:
        if all(needle in lowered for needle in needles):
            return value
    return None


def piece_mass(name: str) -> Decimal:
    lowered = name.lower()
    for needles, value in PIECE_MASS_G:
        if all(needle in lowered for needle in needles):
            return value
    return Decimal("100")


def amount_grams(
    name: str, quantity: Decimal | None, unit: str | None,
    piece_mass_g: Decimal | None = None,
) -> Decimal | None:
    """Количество в граммах/мл; None — единица не пересчитывается честно."""
    if quantity is None:
        return None
    if unit in {"g", "ml"}:
        return quantity
    if unit in {"kg", "l"}:
        return quantity * 1000
    if unit == "piece":
        mass = piece_mass_g if piece_mass_g is not None else piece_mass(name)
        return quantity * mass
    return None


def ingredient_kcal(name: str, quantity: Decimal | None, unit: str | None) -> Decimal | None:
    density_value = kcal_per_100(name)
    if density_value is None:
        return None
    amount = amount_grams(name, quantity, unit)
    if amount is None:
        return None
    return amount * density_value / 100


def nutrition_from_row(
    row: dict, name: str, quantity: Decimal | None, unit: str | None
) -> tuple[Decimal, Decimal | None, Decimal | None, Decimal | None] | None:
    """(ккал, Б, Ж, У) по строке recipe_library.ingredient_nutrition.

    Масса штуки берётся из строки (piece_mass_g), иначе — из PIECE_MASS_G.
    """
    piece = row.get("piece_mass_g")
    piece_decimal = Decimal(str(piece)) if piece is not None else None
    amount = amount_grams(name, quantity, unit, piece_decimal)
    if amount is None or row.get("kcal_100") is None:
        return None
    factor = amount / 100

    def _value(key: str) -> Decimal | None:
        raw = row.get(key)
        return Decimal(str(raw)) * factor if raw is not None else None

    kcal = Decimal(str(row["kcal_100"])) * factor
    return kcal, _value("protein_100"), _value("fat_100"), _value("carb_100")
