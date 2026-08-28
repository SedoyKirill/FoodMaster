import unittest

from app.recipes.cleaning import (
    clean_page,
    dedupe_glyphs,
    fix_mojibake,
    strip_page_numbers,
    strip_running_headers,
)


class DedupeGlyphsTests(unittest.TestCase):
    def test_full_line_duplicate(self) -> None:
        self.assertEqual(dedupe_glyphs("ДЛЯ ГЛАЗУРИ:ДЛЯ ГЛАЗУРИ:"), "ДЛЯ ГЛАЗУРИ:")

    def test_duplicated_token_and_word(self) -> None:
        self.assertEqual(
            dedupe_glyphs("10–1210–12 крупных фиников фиников"),
            "10–12 крупных фиников",
        )

    def test_regular_line_unchanged(self) -> None:
        self.assertEqual(dedupe_glyphs("200 г муки"), "200 г муки")

    def test_legitimate_repeat_of_short_word_kept(self) -> None:
        self.assertEqual(dedupe_glyphs("чуть-чуть соли и еще еще"), "чуть-чуть соли и еще еще")


class MojibakeTests(unittest.TestCase):
    def test_cp1251_read_as_latin1_is_fixed(self) -> None:
        broken = "Áåëüêîâè÷"
        self.assertEqual(fix_mojibake(broken), "Белькович")

    def test_normal_text_unchanged(self) -> None:
        self.assertEqual(fix_mojibake("Обычный текст recipe"), "Обычный текст recipe")

    def test_french_accents_unchanged(self) -> None:
        self.assertEqual(fix_mojibake("crème fraîche"), "crème fraîche")


class RunningHeadersTests(unittest.TestCase):
    def test_frequent_header_removed(self) -> None:
        pages = [f"ГОТОВИМ В АЭРОГРИЛЕ\nРецепт {i}\n200 г муки" for i in range(20)]
        cleaned = strip_running_headers(pages)
        for i, page in enumerate(cleaned):
            self.assertNotIn("АЭРОГРИЛЕ", page)
            self.assertIn(f"Рецепт {i}", page)

    def test_unique_lines_kept(self) -> None:
        words = ["борщ", "плов", "салат", "суп", "рагу"]
        pages = [f"Готовим {words[i % 5]} с овощами и травами {i}" for i in range(20)]
        self.assertEqual(strip_running_headers(pages), pages)

    def test_short_repeating_marker_kept(self) -> None:
        pages = [f"Рецепт\nИнгредиенты\n200 г муки {i}" for i in range(20)]
        cleaned = strip_running_headers(pages)
        self.assertIn("Ингредиенты", cleaned[0])
        self.assertIn("Рецепт", cleaned[0])

    def test_rare_header_kept(self) -> None:
        pages = ["Уникальный текст"] * 96 + ["РЕДКИЙ ЗАГОЛОВОК\nтекст"] * 4
        cleaned = strip_running_headers(pages)
        self.assertIn("РЕДКИЙ ЗАГОЛОВОК", cleaned[-1])


class PageNumberTests(unittest.TestCase):
    def test_edges_stripped(self) -> None:
        text = "12\nСалат из свеклы\n200 г свеклы\n- БОБОВЫЕ И ЗЛАКИ - 69"
        self.assertEqual(strip_page_numbers(text), "Салат из свеклы\n200 г свеклы")

    def test_number_inside_recipe_kept(self) -> None:
        text = "Салат\n2\nяйца"
        self.assertEqual(strip_page_numbers(text), "Салат\n2\nяйца")


class CleanPageTests(unittest.TestCase):
    def test_combined(self) -> None:
        raw = "  7  \nСалат «Свежесть»Салат «Свежесть»\n200 г   огурцов\n\n\n\nсоль по вкусу\n 8 "
        cleaned = clean_page(raw)
        self.assertEqual(cleaned.splitlines()[0], "Салат «Свежесть»")
        self.assertIn("200 г огурцов", cleaned)
        self.assertNotIn("\n\n\n", cleaned)


if __name__ == "__main__":
    unittest.main()
