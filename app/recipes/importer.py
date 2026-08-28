"""Общие утилиты конвейера импорта: события, хэши, метаданные, OCR.

Старый эвристический импортёр (pypdf + regex-парсер) удалён по TZ-M2R;
извлечение страниц теперь в extract.py, оркестрация — в pipeline.py.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from .cleaning import normalize_page
from .config import RecipeImportConfig


def event(name: str, **fields: Any) -> None:
    print(json.dumps({"event": name, **fields}, ensure_ascii=False), flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _filename_metadata(path: Path) -> tuple[str | None, str | None, int | None]:
    stem = path.stem.strip()
    year_matches = re.findall(r"(?:19|20)\d{2}", stem)
    year = int(year_matches[-1]) if year_matches else None
    cleaned = re.sub(r"\s*-?\s*(?:19|20)\d{2}(?:\.a4)?\s*$", "", stem).strip(" -")
    parts = [part.strip() for part in cleaned.split(" - ") if part.strip()]
    if len(parts) >= 2:
        return parts[1], parts[0], year
    compact = cleaned.split("-", 1)
    if len(compact) == 2 and len(compact[0]) <= 80:
        return compact[1].strip(), compact[0].strip(), year
    return cleaned or None, None, year


def _alnum_count(text: str) -> int:
    return sum(character.isalnum() for character in text)


def _pgm_ratios(path: Path) -> tuple[float, float]:
    data = path.read_bytes()
    index = 0
    tokens: list[bytes] = []
    while len(tokens) < 4:
        while index < len(data) and chr(data[index]).isspace():
            index += 1
        if index < len(data) and data[index] == 35:
            while index < len(data) and data[index] not in {10, 13}:
                index += 1
            continue
        start = index
        while index < len(data) and not chr(data[index]).isspace():
            index += 1
        tokens.append(data[start:index])
    if tokens[0] != b"P5" or int(tokens[3]) > 255:
        raise ValueError("Unsupported PGM format")
    while index < len(data) and chr(data[index]).isspace():
        index += 1
    pixels = data[index:]
    if not pixels:
        return 1.0, 0.0
    sample = pixels[:: max(1, len(pixels) // 250_000)]
    white = sum(value >= 245 for value in sample) / len(sample)
    dark = sum(value <= 100 for value in sample) / len(sample)
    return white, dark


def _render_page(pdf_path: Path, page_number: int, prefix: Path, *, dpi: int, gray: bool) -> Path:
    command = [
        "pdftoppm",
        "-f",
        str(page_number),
        "-l",
        str(page_number),
        "-r",
        str(dpi),
        "-singlefile",
    ]
    if gray:
        command.append("-gray")
        suffix = ".pgm"
    else:
        command.append("-png")
        suffix = ".png"
    command.extend([str(pdf_path), str(prefix)])
    completed = subprocess.run(command, capture_output=True, timeout=120, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace")[:1000])
    output = Path(f"{prefix}{suffix}")
    if not output.exists():
        raise RuntimeError("pdftoppm did not create an output file")
    return output


def _ocr_page(pdf_path: Path, page_number: int, config: RecipeImportConfig) -> tuple[str, Decimal | None, str]:
    with tempfile.TemporaryDirectory(prefix="ration-ocr-") as temp_name:
        temp_dir = Path(temp_name)
        preview = _render_page(pdf_path, page_number, temp_dir / "preview", dpi=72, gray=True)
        white, dark = _pgm_ratios(preview)
        white_decimal = Decimal(str(round(white, 5)))
        if white < 0.25 or dark < 0.001 or dark > 0.38:
            return "", white_decimal, "image_only"

        image = _render_page(
            pdf_path, page_number, temp_dir / "ocr", dpi=config.ocr_dpi, gray=False
        )
        completed = subprocess.run(
            [
                "tesseract",
                str(image),
                "stdout",
                "-l",
                config.ocr_languages,
                "--psm",
                "3",
            ],
            capture_output=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode("utf-8", errors="replace")[:1000])
        text = normalize_page(completed.stdout.decode("utf-8", errors="replace"))
        return text, white_decimal, "ocr" if _alnum_count(text) >= 40 else "image_only"
