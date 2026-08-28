"""Почему в плане именно это блюдо (TZ-M8 §5).

Оптимизатор минимизирует сумму штрафов, и до M8 пользователь видел только
результат: «вторник, ужин — гречка с грибами». Здесь тот же коэффициент слота
разбирается на слагаемые, и три самых весомых превращаются в коды причин с
параметрами. Текст собирают интерфейс и бот: в runtime нет ни LLM, ни
шаблонного «генератора объяснений» — только арифметика (TZ-v2 §3).

Причина «потому что дёшево» не всегда честна: если блюдо выбрано из-за
скоропортящейся сметаны, так и надо сказать. Поэтому вклад считается по тем
же весам, что и решение.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import optimizer as optimizer_mod
from .candidates import CandidateScore

#: сколько причин показывать под блюдом
MAX_REASONS = 3
#: вклад слабее этого в рублях-эквиваленте не стоит показывать
MIN_CONTRIBUTION = 60
#: блюдо дешевле медианы слота на столько рублей — уже «выгодно»
CHEAP_DELTA_RUB = 30
#: до скольких минут блюдо считается быстрым
QUICK_MINUTES = 30
#: с какого аффинити говорим «вы часто выбираете супы», а не «любимое блюдо»
LIKED_TYPE_AFFINITY = 0.2


@dataclass(frozen=True)
class Reason:
    """Код причины и параметры для текста в интерфейсе."""

    code: str
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, **self.params}


@dataclass
class ExplainContext:
    """Всё, что знает планировщик о блюде в этом слоте, кроме коэффициента."""

    meal_type: str
    cost_factor: int = 10
    median_cost_kop: int | None = None
    expiring_names: tuple[str, ...] = ()
    stock_names: tuple[str, ...] = ()
    seasonal_names: tuple[str, ...] = ()
    days_since: int | None = None
    rating: int | None = None
    known: bool = True
    kcal_target: int | None = None
    dish_type: str | None = None


def contributions(
    score: CandidateScore, meal_type: str, cost_factor: int
) -> dict[str, int]:
    """Слагаемые ``slot_coefficient``: отрицательное — довод «за» блюдо."""
    fit = score.meal_fit.get(meal_type, 0.0) + score.meal_bias.get(meal_type, 0.0)
    return {
        "cost": cost_factor * (score.cost_kop // 100),
        "dislike": optimizer_mod.W_TASTE * score.dislike_penalty,
        "waste": -optimizer_mod.W_WASTE * score.expiry_bonus,
        "cuisine": -optimizer_mod.W_CUISINE * score.cuisine_bonus,
        "taste": -int(optimizer_mod.W_TASTE_AFFINITY * score.affinity),
        "unknown": int(optimizer_mod.W_UNKNOWN * score.unknown),
        "recency": int(optimizer_mod.W_RECENCY * score.recency_penalty),
        "season": -int(optimizer_mod.W_SEASON * score.season_bonus),
        "fit": -int(optimizer_mod.W_FIT * fit),
    }


def _reason_for(term: str, score: CandidateScore, context: ExplainContext) -> Reason | None:
    """Довод «за» по названию слагаемого целевой функции."""
    if term == "waste" and context.expiring_names:
        return Reason("uses_expiring", {"ingredients": list(context.expiring_names)})
    if term == "taste":
        if context.rating:
            return Reason("favorite", {"rating": context.rating})
        if score.affinity >= LIKED_TYPE_AFFINITY and context.dish_type:
            # Мнения об этом блюде нет, но вкус к его типу уже виден (§4.2).
            return Reason("liked_type", {"dish_type": context.dish_type})
        return Reason("favorite", {"affinity": round(score.affinity, 2)})
    if term == "season" and context.seasonal_names:
        return Reason("seasonal", {"ingredients": list(context.seasonal_names)})
    if term == "cuisine":
        return Reason("cuisine_match", {})
    return None


def explain(score: CandidateScore, context: ExplainContext) -> list[dict[str, Any]]:
    """1–3 причины выбора блюда, самые весомые — первыми."""
    reasons: list[Reason] = []

    # Доводы «за» из целевой функции: берём самые сильные отрицательные вклады.
    parts = contributions(score, context.meal_type, context.cost_factor)
    for term, value in sorted(parts.items(), key=lambda item: item[1]):
        if value > -MIN_CONTRIBUTION:
            break
        reason = _reason_for(term, score, context)
        if reason is not None and all(item.code != reason.code for item in reasons):
            reasons.append(reason)

    # Цена в коэффициенте всегда со знаком «против», поэтому довод «за» — это
    # сравнение с остальными кандидатами слота, а не само слагаемое.
    if context.median_cost_kop:
        delta_rub = (context.median_cost_kop - score.cost_kop) // 100
        if delta_rub >= CHEAP_DELTA_RUB:
            reasons.append(Reason("cheap_today", {"delta_rub": int(delta_rub)}))

    # Доводы, которых в коэффициенте нет: они про удобство, а не про штрафы.
    if context.stock_names:
        reasons.append(Reason("uses_stock", {"ingredients": list(context.stock_names)}))
    if score.time_minutes is not None and score.time_minutes <= QUICK_MINUTES:
        reasons.append(Reason("quick", {"minutes": int(score.time_minutes)}))
    if not context.known:
        reasons.append(Reason("new_for_you"))
    elif context.days_since is not None and context.days_since >= 14:
        reasons.append(Reason("rotation", {"days": int(context.days_since)}))
    if (
        context.kcal_target
        and score.kcal
        and abs(score.kcal - context.kcal_target) <= context.kcal_target * 0.15
    ):
        reasons.append(Reason("kcal_fit", {"kcal": int(score.kcal)}))

    if not reasons:
        # Сказать «подходит для обеда» честнее, чем не сказать ничего.
        reasons.append(Reason("fits_meal", {"meal_type": context.meal_type}))
    return [reason.as_dict() for reason in reasons[:MAX_REASONS]]


def main_reason(score: CandidateScore, context: ExplainContext) -> dict[str, Any]:
    """Одна главная причина — для карточки альтернативы при замене."""
    return explain(score, context)[0]
