"""Turning "Филе куриное охлажденное (мелко нарезанное)" into "куриное филе".

This is the part TZ-M2 calls the most important one, and the reason is
arithmetic: an ingredient that fails to normalise gets no nutrition, so the
recipe carrying it is excluded from planning. Every percent here is recipes.

The pipeline is deliberately conservative — it strips only what is known to be
noise. Over-stripping is worse than under-stripping: merging "куриное филе" and
"филе трески" into "филе" would silently feed fish to someone with a fish
allergy, and M5's allergy filter is a SQL join on exactly this name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Qualifiers that describe the state of a product, not the product itself.
#: Removing them merges "охлаждённое куриное филе" with "куриное филе", which
#: is correct: they have the same nutrition per 100 g raw.
STATE_WORDS = frozenset(
    {
        "свежий",
        "свежая",
        "свежее",
        "свежие",
        "св",
        "охлажденный",
        "охлажденная",
        "охлажденное",
        "охлажденные",
        "охлажд",
        "охл",
        "замороженный",
        "замороженная",
        "замороженное",
        "замороженные",
        "зам",
        "отборный",
        "отборная",
        "отборное",
        "отборные",
        "домашний",
        "домашняя",
        "домашнее",
        "домашние",
        "натуральный",
        "натуральная",
        "натуральное",
        "натуральные",
        "качественный",
        "качественная",
        "качественное",
        "готовый",
        "готовая",
        "готовое",
        "готовые",
        "крупный",
        "крупная",
        "крупное",
        "крупные",
        "мелкий",
        "мелкая",
        "мелкое",
        "мелкие",
        "средний",
        "средняя",
        "среднее",
        "средние",
        "любой",
        "любая",
        "любое",
        "любые",
        "по",
        "вкусу",
        "желанию",
    }
)

#: Preparation instructions that leak into ingredient names.
PREP_WORDS = frozenset(
    {
        "нарезанный",
        "нарезанная",
        "нарезанное",
        "нарезанные",
        "измельченный",
        "измельченная",
        "измельченное",
        "измельченные",
        "очищенный",
        "очищенная",
        "очищенное",
        "очищенные",
        "мелко",
        "крупно",
        "тонко",
        "порезанный",
        "порубленный",
        "разрезанный",
        "промытый",
        "просеянная",
        "просеянный",
        "размягченное",
        "размягченный",
        "растопленное",
        "растопленный",
        "комнатной",
        "температуры",
    }
)

#: Words that carry real nutritional meaning and must survive.
KEEP_WORDS = frozenset(
    {
        "молотый",
        "молотая",
        "молотое",  # ground spice is still that spice
        "сушеный",
        "сушеная",
        "сушеное",
        "сушеные",  # dried changes weight a lot
        "копченый",
        "копченая",
        "копченое",
        "соленый",
        "соленая",
        "соленое",
        "маринованный",
        "маринованная",
        "маринованное",
        "консервированный",
        "консервированная",
        "консервированное",
        "вареный",
        "вареная",
        "вареное",
        "жареный",
        "жареная",
        "жареное",
    }
)

_PARENTHESES = re.compile(r"\([^)]*\)")
#: "30%", and also the adjective glued to it: "Сливки 30%-ные", "шоколад 70%-ный".
#: Without swallowing that suffix the name becomes "сливки -ные", which is a
#: different join key from "сливки" — so the ingredient would never match a
#: price, a nutrition row, or an allergy filter.
_PERCENTAGE = re.compile(r"\d+[.,]?\d*\s*%(?:\s*-?\s*[а-яё]{1,5}\b)?", re.I)
_WEIGHT_IN_NAME = re.compile(r"\b\d+[.,]?\d*\s*(?:г|гр|кг|мл|л|шт)\b\.?", re.I)
_NON_NAME_CHARS = re.compile(r"[^\w\s\-]", re.UNICODE)
_MULTISPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class NormalizedName:
    """The canonical join key plus what it was derived from."""

    name: str
    display_name: str
    #: True when normalisation had to throw away so much that the result is
    #: probably not a real ingredient.
    suspicious: bool = False


def normalize_name(raw: str) -> NormalizedName | None:
    """Reduce an ingredient name to its canonical form.

    Returns None when nothing usable is left — the caller stores the raw line
    with `ingredient_id = NULL` rather than inventing an ingredient.
    """
    display = " ".join(raw.split())
    if not display:
        return None

    # "Мука пшеничная / Мука" — sources offer a fallback name after a slash.
    text = display.split("/")[0]

    text = text.lower().replace("ё", "е")
    text = _PARENTHESES.sub(" ", text)
    text = _PERCENTAGE.sub(" ", text)
    text = _WEIGHT_IN_NAME.sub(" ", text)
    text = _NON_NAME_CHARS.sub(" ", text)
    text = _MULTISPACE.sub(" ", text).strip()
    if not text:
        return None

    words = [w for w in text.split() if len(w) > 1 or w.isdigit()]
    kept = [w for w in words if w in KEEP_WORDS or (w not in STATE_WORDS and w not in PREP_WORDS)]

    # Everything was noise — fall back to the pre-filter form rather than
    # returning nothing, so "свежая зелень" does not vanish entirely.
    if not kept:
        kept = words
    if not kept:
        return None

    name = " ".join(kept)
    return NormalizedName(
        name=name,
        display_name=display,
        suspicious=len(name) < 3 or name.isdigit(),
    )


def split_ingredient_line(line: str) -> tuple[str, str]:
    """Split a source line into (name, amount text).

    Handles the two shapes the recon found:
      * eda.rambler.ru — "Куриное филе, 800 г"
      * povarenok.ru   — "Куриное филе — 800 г"
    """
    for separator in ("—", "–", "—"):
        if separator in line:
            name, _, tail = line.partition(separator)
            return name.strip(), tail.strip()
    if "," in line:
        name, _, tail = line.rpartition(",")
        return name.strip(), tail.strip()
    return line.strip(), ""
