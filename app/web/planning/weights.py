"""Веса целевой функции и режимы планирования (TZ-M8 §6.4).

Правило проекта: все веса — именованные константы в одном месте, а формула
коэффициента слота одна на солвер, жадный запасной алгоритм и список замен.
До M8 весов был один набор на всех; теперь семья выбирает режим, и «экономно»
отличается от «разнообразно» не подсказкой в интерфейсе, а числами.

Значения стартовые: они калибруются на живых данных (§9.2), поэтому меняются
здесь и больше нигде.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Пустой слот хуже любого реалистичного перерасхода: план с дырами — крайняя
#: мера при настоящей нехватке рецептов, а не способ «сэкономить».
EMPTY_SLOT_PENALTY = 10_000_000


@dataclass(frozen=True)
class WeightProfile:
    """Веса слагаемых целевой функции для одного режима планирования."""

    name: str
    #: за рубль стоимости блюда
    cost: int = 10
    #: за рубль сверх бюджета плана
    budget: int = 500
    #: за 100 ккал отклонения слота от цели
    kcal: int = 50
    #: за 10 г недобора белка в день
    protein: int = 30
    #: множитель аффинити семьи [-1, 1]
    taste: int = 600
    #: за блюдо, о котором семья ничего не знает
    unknown: int = 150
    #: за нелюбимый продукт в блюде
    dislike: int = 500
    #: за то, что блюдо ели совсем недавно
    recency: int = 400
    #: за повтор белковой базы или кухни подряд
    variety: int = 400
    #: бонус за использование скоропортящегося запаса
    waste: int = 300
    #: за рубль остатка упаковки, взвешенный на скорость порчи
    leftover_waste: int = 20
    #: бонус за «приготовили один раз — съели дважды»
    leftover: int = 150
    #: за каждые 15 минут сверх лимита семьи
    time: int = 100
    #: за неизвестное время приготовления
    time_unknown: int = 100
    #: бонус за сезонные продукты
    season: int = 100
    #: бонус за попадание в выбранную кухню
    cuisine: int = 200
    #: бонус за соответствие блюда приёму пищи
    fit: int = 300
    #: штраф за пустой слот
    empty_slot: int = EMPTY_SLOT_PENALTY


#: Режимы из §6.4. «Экономно» давит ценой и бережёт остатки, «разнообразно»
#: втрое сильнее наказывает повторы, «фитнес» держит калории и белок, «быстро»
#: делает время почти таким же весомым, как вкус.
MODES: dict[str, WeightProfile] = {
    "balanced": WeightProfile(name="balanced"),
    "economy": WeightProfile(
        name="economy", cost=30, budget=800, kcal=30, protein=10, taste=300,
        unknown=100, recency=200, variety=200, waste=500, leftover_waste=40,
        leftover=250, time=50, time_unknown=50, season=150, cuisine=100,
    ),
    "variety": WeightProfile(
        name="variety", cost=8, budget=400, kcal=40, protein=20, taste=500,
        unknown=0, recency=900, variety=900, waste=200, leftover_waste=10,
        leftover=50, time=50, time_unknown=50, season=200, cuisine=150,
    ),
    "fitness": WeightProfile(
        name="fitness", cost=8, budget=400, kcal=120, protein=150, taste=400,
        unknown=150, recency=300, variety=300, waste=200, leftover_waste=10,
        leftover=100, time=50, time_unknown=50, season=100, cuisine=100,
    ),
    "quick": WeightProfile(
        name="quick", cost=10, budget=500, kcal=40, protein=20, taste=500,
        unknown=150, recency=300, variety=300, waste=300, leftover_waste=20,
        leftover=400, time=600, time_unknown=300, season=50, cuisine=100,
    ),
}

#: ценовая стратегия матчера товаров, выводимая из режима (§6.4)
MODE_PRICE_TIER = {"economy": "economy"}


def weights_for(mode: str | None) -> WeightProfile:
    """Веса режима; незнакомое имя — «сбалансированно», а не отказ."""
    return MODES.get(str(mode or "balanced"), MODES["balanced"])


def price_tier_for(mode: str | None) -> str:
    return MODE_PRICE_TIER.get(str(mode or "balanced"), "balanced")
