"""Ingredient name normalisation."""

from __future__ import annotations

import pytest

from app.recipes.normalize import normalize_name, split_ingredient_line


class TestSplitIngredientLine:
    @pytest.mark.parametrize(
        ("line", "name", "amount"),
        [
            ("Куриное филе, 800 г", "Куриное филе", "800 г"),
            ("Мед, 2 столовые ложки", "Мед", "2 столовые ложки"),
            ("Соль,  по вкусу", "Соль", "по вкусу"),
            ("Ветчина — 100 г", "Ветчина", "100 г"),
            ("Яйцо куриное — 2 шт", "Яйцо куриное", "2 шт"),
            ("Соль", "Соль", ""),
        ],
    )
    def test_handles_both_source_formats(self, line: str, name: str, amount: str) -> None:
        assert split_ingredient_line(line) == (name, amount)

    def test_splits_on_the_last_comma(self) -> None:
        # A name may itself contain a comma; the amount is always at the end.
        name, amount = split_ingredient_line("Перец красный, острый, 1 штука")
        assert name == "Перец красный, острый"
        assert amount == "1 штука"


class TestNormalizeName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Куриное филе", "куриное филе"),
            ("Филе куриное охлажденное", "филе куриное"),
            ("Сыр твердый", "сыр твердый"),
            ("Молоко 3,2%", "молоко"),
            # The adjective glued to the percentage must go with it, or the
            # join key becomes "сливки -ные" and matches nothing.
            ("Сливки 30%-ные", "сливки"),
            ("Черный шоколад 70%-ный", "черный шоколад"),
            ("Чеснок (очищенные и мелко нарезанные)", "чеснок"),
            ("Мука пшеничная / Мука", "мука пшеничная"),
            ("Масло сливочное 82,5% 180 г", "масло сливочное"),
            ("Свежая петрушка", "петрушка"),
        ],
    )
    def test_canonical_form(self, raw: str, expected: str) -> None:
        result = normalize_name(raw)
        assert result is not None
        assert result.name == expected

    def test_yo_is_folded_to_ye(self) -> None:
        """Otherwise "мёд" and "мед" become two ingredients with two prices."""
        a = normalize_name("Мёд")
        b = normalize_name("Мед")
        assert a is not None and b is not None
        assert a.name == b.name == "мед"

    @pytest.mark.parametrize(
        "raw",
        ["Молотый черный перец", "Сушеный базилик", "Копченая паприка", "Маринованные огурцы"],
    )
    def test_meaningful_qualifiers_survive(self, raw: str) -> None:
        """Dried basil weighs a tenth of fresh basil; the word has to stay."""
        result = normalize_name(raw)
        assert result is not None
        first = raw.split()[0].lower().replace("ё", "е")
        assert first in result.name

    def test_display_name_keeps_the_original(self) -> None:
        result = normalize_name("Филе куриное охлажденное")
        assert result is not None
        assert result.display_name == "Филе куриное охлажденное"

    def test_nothing_usable(self) -> None:
        assert normalize_name("   ") is None
        assert normalize_name("!!!") is None

    def test_does_not_collapse_different_proteins(self) -> None:
        """The allergy filter is a join on this name; over-merging is unsafe."""
        chicken = normalize_name("Куриное филе")
        cod = normalize_name("Филе трески")
        assert chicken is not None and cod is not None
        assert chicken.name != cod.name
