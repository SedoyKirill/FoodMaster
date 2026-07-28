"""Unit conversion — the point where recipe text becomes grams."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.recipes.units import Amount, Unit, parse_amount, to_grams


class TestParseAmount:
    @pytest.mark.parametrize(
        ("text", "value", "unit"),
        [
            ("800 г", Decimal(800), Unit.GRAM),
            ("1 кг", Decimal(1), Unit.KILOGRAM),
            ("200 мл", Decimal(200), Unit.MILLILITRE),
            ("2 столовые ложки", Decimal(2), Unit.TABLESPOON),
            ("1 столовая ложка", Decimal(1), Unit.TABLESPOON),
            ("5 столовых ложек", Decimal(5), Unit.TABLESPOON),
            ("0.5 чайных ложек", Decimal("0.5"), Unit.TEASPOON),
            ("2 штуки", Decimal(2), Unit.PIECE),
            ("1 штука", Decimal(1), Unit.PIECE),
            ("3 зубчика", Decimal(3), Unit.CLOVE),
            ("2 стакана", Decimal(2), Unit.GLASS),
            ("1 пучок", Decimal(1), Unit.BUNCH),
            # povarenok's abbreviations, so the parser survives a source change
            ("2 шт", Decimal(2), Unit.PIECE),
            ("3 зуб.", Decimal(3), Unit.CLOVE),
            ("1 ст. л.", Decimal(1), Unit.TABLESPOON),
            ("2 веточ.", Decimal(2), Unit.SPRIG),
        ],
    )
    def test_parses_the_forms_both_sources_use(self, text: str, value: Decimal, unit: Unit) -> None:
        amount = parse_amount(text)
        assert amount is not None
        assert amount.value == value
        assert amount.unit == unit

    def test_comma_decimal_separator(self) -> None:
        amount = parse_amount("1,5 кг")
        assert amount is not None
        assert amount.value == Decimal("1.5")

    def test_unicode_fraction(self) -> None:
        amount = parse_amount("½ стакана")
        assert amount is not None
        assert amount.value == Decimal("0.5")
        assert amount.unit == Unit.GLASS

    @pytest.mark.parametrize("text", ["по вкусу", "На кончике ножа", "для подачи"])
    def test_to_taste_is_recognised_not_guessed(self, text: str) -> None:
        amount = parse_amount(text)
        assert amount is not None
        assert amount.to_taste is True
        # Crucially it must not become grams: "to taste" salt is not 1 g.
        assert to_grams(amount) is None

    def test_empty_text(self) -> None:
        assert parse_amount("   ") is None


class TestToGrams:
    def test_mass_is_exact(self) -> None:
        assert to_grams(Amount(Decimal(800), Unit.GRAM)) == Decimal(800)
        assert to_grams(Amount(Decimal("1.5"), Unit.KILOGRAM)) == Decimal(1500)

    def test_water_like_volume_defaults_to_density_one(self) -> None:
        assert to_grams(Amount(Decimal(200), Unit.MILLILITRE)) == Decimal(200)

    def test_density_is_applied_when_the_ingredient_is_known(self) -> None:
        """A glass of flour is not a glass of milk.

        Without density, 200 ml of flour would be recorded as 200 g instead of
        110 g — an 80% error in the calorie count of every baked recipe.
        """
        flour = to_grams(Amount(Decimal(1), Unit.GLASS), ingredient_name="пшеничная мука")
        assert flour == Decimal("110.00")

        milk = to_grams(Amount(Decimal(1), Unit.GLASS), ingredient_name="молоко")
        assert milk == Decimal("206.00")

    def test_explicit_density_wins_over_the_table(self) -> None:
        grams = to_grams(
            Amount(Decimal(100), Unit.MILLILITRE),
            ingredient_name="молоко",
            density_g_per_ml=Decimal("0.5"),
        )
        assert grams == Decimal(50)

    def test_piece_weight_from_the_reference_table(self) -> None:
        assert to_grams(Amount(Decimal(2), Unit.PIECE), ingredient_name="яйцо") == Decimal(110)
        assert to_grams(Amount(Decimal(1), Unit.PIECE), ingredient_name="картофель") == Decimal(100)

    def test_ingredient_piece_weight_wins_over_the_table(self) -> None:
        grams = to_grams(
            Amount(Decimal(2), Unit.PIECE),
            ingredient_name="яйцо",
            piece_grams=Decimal(70),
        )
        assert grams == Decimal(140)

    def test_unknown_piece_falls_back_to_a_generic_weight(self) -> None:
        grams = to_grams(Amount(Decimal(1), Unit.PIECE), ingredient_name="нечто невиданное")
        assert grams == Decimal(100)

    def test_clove_uses_its_own_weight_not_a_whole_head(self) -> None:
        # "чеснок" as a piece is a head (40 g); as a clove it is 5 g.
        assert to_grams(Amount(Decimal(3), Unit.CLOVE), ingredient_name="чеснок") == Decimal(15)

    def test_spoons(self) -> None:
        assert to_grams(Amount(Decimal(2), Unit.TABLESPOON)) == Decimal(30)
        assert to_grams(Amount(Decimal(1), Unit.TEASPOON)) == Decimal(5)
        # Oil is lighter than water, so a spoon of it weighs less.
        oil = to_grams(Amount(Decimal(1), Unit.TABLESPOON), ingredient_name="оливковое масло")
        assert oil == Decimal("13.80")

    def test_unknown_unit_returns_none_rather_than_guessing(self) -> None:
        assert to_grams(Amount(Decimal(1), None)) is None
