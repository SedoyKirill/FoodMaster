import json
import tempfile
import unittest
from pathlib import Path

from app.recipes.windows import (
    build_windows,
    extraction_status,
    render_window_file,
    write_windows,
)

PAGE = "Рецепт блюда\n" + "200 г продукта строка ингредиента номер\n" * 10


class BuildWindowsTests(unittest.TestCase):
    def test_ten_pages_step_three(self) -> None:
        pages = {number: PAGE for number in range(1, 11)}
        windows = build_windows(5, "Книга", pages)
        spans = [(w.page_start, w.page_end) for w in windows]
        self.assertEqual(spans, [(1, 4), (4, 7), (7, 10)])
        self.assertEqual(windows[0].window_id, "005-0001-0004")

    def test_sha_is_stable(self) -> None:
        pages = {1: PAGE, 2: PAGE, 3: PAGE, 4: PAGE}
        first = build_windows(1, "Книга", pages)[0].sha256
        second = build_windows(1, "Книга", pages)[0].sha256
        self.assertEqual(first, second)

    def test_sparse_window_skipped(self) -> None:
        pages = {1: "мало", 2: "", 3: "текста", 4: "тут"}
        self.assertEqual(build_windows(1, "Книга", pages), [])

    def test_markers_present(self) -> None:
        pages = {7: PAGE, 8: PAGE, 9: PAGE, 10: PAGE}
        window = build_windows(2, "Книга", pages)[0]
        self.assertIn("=== СТРАНИЦА 7 ===", window.text)
        self.assertIn("=== СТРАНИЦА 10 ===", window.text)

    def test_render_contains_header(self) -> None:
        pages = {1: PAGE, 2: PAGE, 3: PAGE, 4: PAGE}
        window = build_windows(3, "Моя книга", pages)[0]
        rendered = render_window_file(window)
        self.assertIn("window_id: 003-0001-0004", rendered)
        self.assertIn("sha256: " + window.sha256, rendered)


class WriteWindowsTests(unittest.TestCase):
    def test_write_and_rewrite(self) -> None:
        pages = {1: PAGE, 2: PAGE, 3: PAGE, 4: PAGE}
        windows = build_windows(1, "Книга", pages)
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            counters = write_windows(windows, directory)
            self.assertEqual(counters, {"new": 1, "updated": 0, "unchanged": 0})
            counters = write_windows(windows, directory)
            self.assertEqual(counters, {"new": 0, "updated": 0, "unchanged": 1})
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["windows"][0]["window_id"], "001-0001-0004")

    def test_status(self) -> None:
        pages = {1: PAGE, 2: PAGE, 3: PAGE, 4: PAGE}
        windows = build_windows(1, "Книга", pages)
        with tempfile.TemporaryDirectory() as name:
            win_dir = Path(name) / "windows"
            ext_dir = Path(name) / "extracted"
            ext_dir.mkdir(parents=True)
            write_windows(windows, win_dir)
            status = extraction_status(win_dir, ext_dir)
            self.assertEqual(status["windows"], 1)
            self.assertEqual(status["pending"], 1)
            (ext_dir / f"{windows[0].window_id}.json").write_text("{}", encoding="utf-8")
            status = extraction_status(win_dir, ext_dir)
            self.assertEqual(status["extracted"], 1)
            self.assertEqual(status["pending"], 0)


if __name__ == "__main__":
    unittest.main()
