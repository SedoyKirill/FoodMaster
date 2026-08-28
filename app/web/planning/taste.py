"""Вкус семьи: события → аффинити (TZ-M8 §4).

До M8 «вкус» был одной звездой на рецепт: пять звёзд поднимали конкретное
блюдо и ничего не говорили о том, что семья любит супы и не любит рыбу.
Замены, отметки «приготовили» и «пропустили» не учитывались вовсе.

Здесь любое действие семьи становится событием со своей ценой, события
затухают со временем и обобщаются на тип блюда, кухню и главные ингредиенты.
Ни обучения на чужих семьях, ни LLM: экспоненциальное затухание, сглаженное
среднее и арифметика (TZ-M8 §10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

#: за сколько дней вес события падает вдвое
HALF_LIFE_DAYS = 90

#: Сглаживание: чем шире уровень, тем больше событий нужно, чтобы поверить.
#: Одна пятёрка не делает блюдо «любимым блюдом семьи».
K_PRIOR = {"recipe": 2.0, "dish_type": 4.0, "cuisine": 6.0, "ingredient": 6.0}

#: Цена действия в шкале [-1, 1]. «Запланировано» — ноль: это показ, а не
#: мнение, но он нужен, чтобы отличать «не пробовали» от «пробовали и молчат».
EVENT_VALUES = {
    "replaced_out": -0.6,
    "replaced_in": 0.3,
    "cooked": 0.5,
    "skipped": -0.4,
    "planned": 0.0,
    "onboarding_like": 0.7,
    "onboarding_skip": -0.7,
    "rated": 0.0,  # считается из оценки, см. event_value
}

#: с каким коэффициентом событие о рецепте переносится на его тип/кухню/продукты
PROPAGATION = 0.5

#: доли уровней, когда о самом рецепте ничего не известно
LEVEL_WEIGHTS = {"dish_type": 0.6, "cuisine": 0.25, "ingredient": 0.15}

#: насколько сильно семья считается с тем, кому блюдо не нравится (§4.3)
SUFFERING_WEIGHT = 0.5
#: аффинити ниже этого — «человек этого не ест»
STRONG_DISLIKE = -0.5


def event_value(kind: str, rating: int | None = None) -> float:
    """Цена события; для оценки — линейно из звёзд: (rating − 3) / 2."""
    if kind == "rated":
        return 0.0 if rating is None else max(-1.0, min(1.0, (int(rating) - 3) / 2))
    return EVENT_VALUES.get(kind, 0.0)


@dataclass(frozen=True)
class RecipeMeta:
    """То, на что обобщается вкус: тип блюда, кухни и главные продукты."""

    recipe_id: int
    dish_type: str | None = None
    cuisines: tuple[str, ...] = ()
    #: (канонический продукт, вес): главный — 1.0, два следующих — 0.5
    ingredients: tuple[tuple[str, float], ...] = ()


def _age_days(created_at: Any, today: date) -> float:
    if isinstance(created_at, datetime):
        created_at = created_at.date()
    if not isinstance(created_at, date):
        return 0.0
    return max(0.0, float((today - created_at).days))


def _decay(age_days: float) -> float:
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


@dataclass
class _Level:
    """Накопленные вес и сумма значений по одному ключу."""

    weight: float = 0.0
    total: float = 0.0
    events: int = 0

    def add(self, value: float, weight: float) -> None:
        self.weight += weight
        self.total += value * weight
        self.events += 1

    def score(self, prior: float) -> float:
        if self.weight <= 0:
            return 0.0
        return max(-1.0, min(1.0, self.total / (self.weight + prior)))


@dataclass
class TasteModel:
    """Аффинити четырёх уровней, при необходимости — отдельно по людям."""

    levels: dict[str | None, dict[str, dict[str, _Level]]] = field(default_factory=dict)
    events_count: int = 0

    @classmethod
    def fit(
        cls,
        events: Iterable[dict[str, Any]],
        recipes: dict[int, RecipeMeta],
        today: date,
    ) -> "TasteModel":
        model = cls()
        for entry in events:
            recipe_id = entry.get("recipe_id")
            if recipe_id is None:
                continue
            meta = recipes.get(int(recipe_id))
            value = float(entry.get("value") or 0.0)
            weight = _decay(_age_days(entry.get("created_at"), today))
            model.events_count += 1
            if weight <= 0:
                continue
            person_id = entry.get("person_id")
            person_key = str(person_id) if person_id is not None else None
            for key in {None, person_key}:
                model._add(key, "recipe", str(int(recipe_id)), value, weight)
                if meta is None or value == 0.0:
                    continue
                spread = weight * PROPAGATION
                if meta.dish_type:
                    model._add(key, "dish_type", meta.dish_type, value, spread)
                for cuisine in meta.cuisines:
                    model._add(key, "cuisine", cuisine, value, spread)
                for name, share in meta.ingredients:
                    model._add(key, "ingredient", name, value, spread * share)
        return model

    def _add(
        self, person: str | None, level: str, key: str, value: float, weight: float
    ) -> None:
        person_levels = self.levels.setdefault(person, {})
        person_levels.setdefault(level, {}).setdefault(key, _Level()).add(value, weight)

    def _score(self, person: str | None, level: str, key: str) -> float:
        entry = self.levels.get(person, {}).get(level, {}).get(key)
        return entry.score(K_PRIOR[level]) if entry else 0.0

    def _has_recipe_events(self, person: str | None, recipe_id: int) -> bool:
        entry = self.levels.get(person, {}).get("recipe", {}).get(str(recipe_id))
        return bool(entry and entry.weight > 0)

    def affinity(self, meta: RecipeMeta, person_id: str | None = None) -> float:
        """Оценка блюда: сам рецепт, иначе — обобщение по типу/кухне/продуктам."""
        if self._has_recipe_events(person_id, meta.recipe_id):
            return self._score(person_id, "recipe", str(meta.recipe_id))
        parts = 0.0
        if meta.dish_type:
            parts += LEVEL_WEIGHTS["dish_type"] * self._score(
                person_id, "dish_type", meta.dish_type
            )
        if meta.cuisines:
            cuisine_scores = [
                self._score(person_id, "cuisine", cuisine) for cuisine in meta.cuisines
            ]
            parts += LEVEL_WEIGHTS["cuisine"] * (sum(cuisine_scores) / len(cuisine_scores))
        if meta.ingredients:
            ingredient_scores = [
                self._score(person_id, "ingredient", name) for name, _ in meta.ingredients
            ]
            parts += LEVEL_WEIGHTS["ingredient"] * (
                sum(ingredient_scores) / len(ingredient_scores)
            )
        return max(-1.0, min(1.0, parts))

    def family_affinity(self, meta: RecipeMeta, people: list[dict[str, Any]]) -> float:
        """Среднее по едокам минус страдание того, кому блюдо не нравится.

        Блюдо, которое один член семьи откровенно не любит, должно проигрывать
        нейтральному даже при восторге остальных (§4.3). Без личных событий
        формула вырождается в общий семейный аффинити.
        """
        personal = [
            person for person in people
            if str(person.get("id")) in self.levels and self.levels[str(person.get("id"))]
        ]
        if not personal:
            return self.affinity(meta)
        weights = []
        scores = []
        for person in personal:
            factor = float(person.get("portion_factor") or 1)
            weights.append(factor)
            scores.append(self.affinity(meta, str(person.get("id"))))
        total_weight = sum(weights) or 1.0
        weighted = sum(
            score * weight for score, weight in zip(scores, weights)
        ) / total_weight
        suffering = max(0.0, -min(scores))
        return max(-1.0, min(1.0, weighted - SUFFERING_WEIGHT * suffering))

    def is_strong_dislike(self, meta: RecipeMeta, people: list[dict[str, Any]]) -> bool:
        return any(
            self.affinity(meta, str(person.get("id"))) <= STRONG_DISLIKE
            for person in people
        )

    def rows(self) -> list[dict[str, Any]]:
        """Строки для ``app_core.taste_affinities`` (семейный уровень)."""
        result: list[dict[str, Any]] = []
        for level, keys in self.levels.get(None, {}).items():
            for key, entry in keys.items():
                result.append(
                    {
                        "level": level,
                        "key": key,
                        "score": entry.score(K_PRIOR[level]),
                        "events_count": entry.events,
                    }
                )
        return result

    def summary(self, recipes: dict[int, RecipeMeta], limit: int = 5) -> dict[str, Any]:
        """Топ любимого и нелюбимого — для экрана «Вкусы семьи» и бота."""
        def _top(level: str, reverse: bool) -> list[dict[str, Any]]:
            keys = self.levels.get(None, {}).get(level, {})
            scored = [
                {"key": key, "score": round(entry.score(K_PRIOR[level]), 3),
                 "events_count": entry.events}
                for key, entry in keys.items()
            ]
            scored = [item for item in scored if (item["score"] > 0) == reverse]
            scored.sort(key=lambda item: item["score"], reverse=reverse)
            return scored[:limit]

        def _recipes(reverse: bool) -> list[dict[str, Any]]:
            items = []
            for item in _top("recipe", reverse):
                recipe_id = int(item["key"])
                meta = recipes.get(recipe_id)
                items.append(
                    {
                        "recipe_id": recipe_id,
                        "score": item["score"],
                        "dish_type": meta.dish_type if meta else None,
                    }
                )
            return items

        return {
            "events_count": self.events_count,
            "favourite_recipes": _recipes(True),
            "disliked_recipes": _recipes(False),
            "favourite_dish_types": _top("dish_type", True),
            "favourite_cuisines": _top("cuisine", True),
            "disliked_ingredients": _top("ingredient", False),
        }


def recipe_meta(recipe: dict[str, Any], canonical: Any = None) -> RecipeMeta:
    """``RecipeMeta`` из строки рецепта планировщика.

    Главный ингредиент весит 1.0, два следующих по массе — 0.5: вкус к блюду
    это в первую очередь вкус к тому, из чего оно сделано.
    """
    from ..planner import json_list, recipe_cuisines

    weighted: list[tuple[str, float]] = []
    ingredients = recipe.get("ingredients") or []
    ranked = sorted(
        ingredients,
        key=lambda item: float(item.get("quantity_max") or item.get("quantity_min") or 0),
        reverse=True,
    )
    for position, ingredient in enumerate(ranked[:3]):
        name = str(
            ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""
        ).strip().casefold()
        if canonical is not None:
            name = canonical(name)
        if name:
            weighted.append((name, 1.0 if position == 0 else 0.5))
    dish_type = recipe.get("dish_type")
    return RecipeMeta(
        recipe_id=int(recipe["id"]),
        dish_type=str(dish_type) if dish_type else None,
        cuisines=tuple(sorted(recipe_cuisines(recipe))),
        ingredients=tuple(weighted),
    )


def build_metas(recipes: Iterable[dict[str, Any]], canonical: Any = None) -> dict[int, RecipeMeta]:
    return {int(recipe["id"]): recipe_meta(recipe, canonical) for recipe in recipes}


def affinity_map(
    model: TasteModel, metas: dict[int, RecipeMeta], people: list[dict[str, Any]]
) -> dict[int, float]:
    """Аффинити всех кандидатов одним словарём — то, что видит планировщик."""
    return {
        recipe_id: model.family_affinity(meta, people)
        for recipe_id, meta in metas.items()
    }


def known_recipes(model: TasteModel) -> set[int]:
    """Рецепты, о которых у семьи есть хоть какое-то мнение."""
    recipe_levels = model.levels.get(None, {}).get("recipe", {})
    return {int(key) for key, entry in recipe_levels.items() if entry.weight > 0}
