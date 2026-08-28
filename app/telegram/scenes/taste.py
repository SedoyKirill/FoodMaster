"""Вкусы семьи из чата: карточки онбординга и сводка (TZ-M7 §5.10, T11).

Считает вкус не бот, а модель из TZ-M8 (``app/web/planning/taste.py``): бот
только показывает карточки и складывает ответы событиями. Поэтому здесь нет ни
одной формулы — вся арифметика живёт в репозитории и одинакова для веба и чата.

Пока обновление планировщика не выкачено, методов ``taste_*`` у репозитория
нет. Сцена это переживает: ``available()`` гасит пункт меню и команду, а если
кнопка осталась в старом сообщении — отвечает по-человечески, а не трассировкой.
"""

from __future__ import annotations

from typing import Any

from app.web.categories import cuisine_label, dish_type_label

from ..callbacks import encode_callback
from ..fsm import DialogState
from ..render import CallbackReply, Reply, build_keyboard, button_text
from . import SceneContext

SCENE = "taste.cards"

#: сколько имён показываем в одной строке сводки
SUMMARY_LIMIT = 3

UNAVAILABLE_TEXT = (
    "😋 «Вкусы» пока не включены: обновление планировщика, которое их считает, "
    "ещё не выкачено. Оценки блюд ★ при этом уже учитываются."
)

VIEWER_TEXT = "В режиме просмотра вкусы менять нельзя."

EMPTY_DECK_TEXT = (
    "😋 Карточки закончились — вы ответили на всё, что я могу предложить."
)

NO_EVENTS_TEXT = (
    "😋 Пока я ничего не знаю о вкусах семьи.\n"
    "Ответьте на десяток карточек — и меню начнёт подстраиваться."
)

#: ответы карточки: подпись кнопки → вид события в модели вкуса
ANSWERS: dict[str, tuple[str, str]] = {
    "like": ("👍 Нравится", "onboarding_like"),
    "skip": ("👎 Не моё", "onboarding_skip"),
}
PASS_LABEL = "⏭ Пропустить"


def available(app_repository: Any) -> bool:
    """Приехала ли модель вкуса. Проверяем метод, а не версию схемы."""
    return hasattr(app_repository, "taste_onboarding")


def _back_row() -> list[dict[str, Any]]:
    return [{"text": "◀ К настройкам", "callback_data": encode_callback("n", "st", "menu")}]


def unavailable_reply() -> Reply:
    return Reply(UNAVAILABLE_TEXT, build_keyboard([_back_row()]))


# --- карточки ------------------------------------------------------------------

def _card_text(card: dict[str, Any], left: int, events: int, needed: bool) -> str:
    marks = [
        label for label in (
            cuisine_label(card.get("cuisine_code")) if card.get("cuisine_code") else "",
            dish_type_label(card.get("dish_type")) if card.get("dish_type") else "",
        ) if label
    ]
    head = f"«{card.get('title')}»"
    if marks:
        head += " · " + " · ".join(marks)
    tail = (
        "Чем больше ответов, тем точнее меню."
        if needed
        else "Картины уже хватает — можно посмотреть сводку."
    )
    return (
        f"😋 Вкусы — осталось карточек: {left}\n\n"
        f"{head}\n\n"
        f"Ответов: {events}. {tail}"
    )


def _card_keyboard(recipe_id: int) -> dict[str, Any] | None:
    answers = [
        {"text": label, "callback_data": encode_callback("t", recipe_id, key)}
        for key, (label, _) in ANSWERS.items()
    ]
    answers.append({
        "text": PASS_LABEL,
        "callback_data": encode_callback("t", recipe_id, "pass"),
    })
    return build_keyboard([
        answers,
        [{"text": "📊 Сводка", "callback_data": encode_callback("n", "ts", "sum")}],
        _back_row(),
    ])


def _deal(cards: list[dict[str, Any]]) -> list[int]:
    """Порядок колоды: по одной карточке из каждой пары «кухня + тип», по кругу.

    Модель отдаёт карточки пачками по две на пару, и в вебе это незаметно —
    там сетка, видно все сразу. В чате они идут строго одна за другой, поэтому
    без перемешивания человек, ответивший на первые четыре, рассказал бы только
    про средиземноморские гарниры — ровно то, чего онбординг избегает.
    """
    groups: dict[tuple[Any, Any], list[int]] = {}
    for card in cards:
        key = (card.get("cuisine_code"), card.get("dish_type"))
        groups.setdefault(key, []).append(int(card["recipe_id"]))
    depth = max((len(items) for items in groups.values()), default=0)
    return [items[index] for index in range(depth)
            for items in groups.values() if index < len(items)]


async def cards_reply(
    app_repository: Any, dialogs: Any, session: dict[str, Any], user_id: int,
    passed: list[int] | None = None, deck: list[int] | None = None,
) -> Reply:
    """Следующая неотвеченная карточка.

    Что живёт в состоянии диалога и почему: ``passed`` — пропущенные (событием
    ⏭ не считается, иначе карточка возвращалась бы вечно), ``deck`` — выбранный
    порядок. Порядок приходится помнить: пересчитать его на каждом шаге нельзя,
    после ухода карточки её соседка по паре снова оказывалась бы первой, и
    перемешивание не давало бы ничего.

    Сами карточки перечитываются каждый раз: 👍/👎 уходят событием, и рецепт
    выпадает из выдачи сам. Так экран не расходится с тем, что человек мог
    ответить в браузере, и переживает перезапуск бота.
    """
    if not available(app_repository):
        return unavailable_reply()
    passed = list(passed or [])
    payload = await app_repository.taste_onboarding(session)
    cards = {int(card["recipe_id"]): card for card in (payload.get("cards") or [])}
    order = [recipe_id for recipe_id in (deck or []) if recipe_id in cards]
    if not order:
        order = _deal(list(cards.values()))
    await dialogs.save(
        user_id, DialogState(SCENE, "card", {"passed": passed, "deck": order})
    )
    remaining = [recipe_id for recipe_id in order if recipe_id not in set(passed)]
    if not remaining:
        return Reply(EMPTY_DECK_TEXT, build_keyboard([
            [{"text": "📊 Сводка", "callback_data": encode_callback("n", "ts", "sum")}],
            _back_row(),
        ]))
    card = cards[remaining[0]]
    return Reply(
        _card_text(card, len(remaining), int(payload.get("events_count") or 0),
                   bool(payload.get("needed"))),
        _card_keyboard(int(card["recipe_id"])),
    )


async def begin(app_repository: Any, dialogs: Any, session: dict[str, Any],
                user_id: int) -> Reply:
    """Команда /taste и пункт «😋 Вкусы»: колода с чистого листа."""
    return await cards_reply(app_repository, dialogs, session, user_id, [], [])


async def answer(app_repository: Any, dialogs: Any, session: dict[str, Any],
                 user_id: int, parts: list[str]) -> CallbackReply:
    """Глагол ``t``: ответ на карточку (§4.3)."""
    if not available(app_repository):
        return CallbackReply(edit=unavailable_reply())
    if len(parts) < 2 or not parts[0].isdigit():
        return CallbackReply(toast="Не понял кнопку.")
    recipe_id, choice = int(parts[0]), parts[1]
    data = ((await dialogs.load(user_id)) or DialogState(SCENE, "card", {})).data or {}
    passed = [int(value) for value in data.get("passed", [])]
    deck = [int(value) for value in data.get("deck", [])]

    if choice == "pass":
        if recipe_id not in passed:
            passed.append(recipe_id)
        toast = "Пропустили."
    elif choice in ANSWERS:
        if session.get("role") == "viewer":
            return CallbackReply(toast=VIEWER_TEXT, show_alert=True)
        await app_repository.record_taste_event(
            session, recipe_id, ANSWERS[choice][1], channel="telegram",
        )
        toast = "Запомнил." if choice == "like" else "Больше не предложу."
    else:
        return CallbackReply(toast="Не понял кнопку.")

    return CallbackReply(
        toast=toast,
        edit=await cards_reply(app_repository, dialogs, session, user_id, passed, deck),
    )


# --- сводка --------------------------------------------------------------------

def _names(items: list[dict[str, Any]], label: Any = None) -> str:
    """Имена через запятую. Подписи справочника — со строчной: это середина фразы
    («любите: суп, грузинская»), а не заголовок списка, как в вебе."""
    names = []
    for item in items[:SUMMARY_LIMIT]:
        key = item.get("key")
        names.append(label(key).lower() if label else str(key))
    return ", ".join(name for name in names if name)


def _titles(items: list[dict[str, Any]]) -> str:
    return ", ".join(
        str(item.get("title")) for item in items[:SUMMARY_LIMIT] if item.get("title")
    )


def summary_text(summary: dict[str, Any]) -> str:
    """«любите: супы, грузинская; не любите: рыба» (§5.10)."""
    events = int(summary.get("events_count") or 0)
    if not events:
        return NO_EVENTS_TEXT
    liked = [
        part for part in (
            _names(summary.get("favourite_dish_types") or [], dish_type_label),
            _names(summary.get("favourite_cuisines") or [], cuisine_label),
        ) if part
    ]
    lines = [f"😋 Вкусы семьи — учтено событий: {events}."]
    if liked:
        lines.append("")
        lines.append("👍 Любите: " + " · ".join(liked))
    disliked = _names(summary.get("disliked_ingredients") or [])
    if disliked:
        lines.append("👎 Не любите: " + disliked)
    favourites = _titles(summary.get("favourite_recipes") or [])
    if favourites:
        lines.append("")
        lines.append("⭐ Заходит: " + favourites)
    rejected = _titles(summary.get("disliked_recipes") or [])
    if rejected:
        lines.append("🚫 Не заходит: " + rejected)
    if len(lines) == 1:
        lines.append("")
        lines.append("Ответов пока мало, чтобы делать выводы — ответьте ещё на пару карточек.")
    return "\n".join(lines)


async def summary_reply(app_repository: Any, session: dict[str, Any]) -> Reply:
    if not available(app_repository):
        return unavailable_reply()
    summary = await app_repository.taste_summary(session)
    return Reply(summary_text(summary), build_keyboard([
        [{"text": button_text("🃏 Ещё карточки"),
          "callback_data": encode_callback("n", "ts", "cards")}],
        _back_row(),
    ]))


# --- точки входа ---------------------------------------------------------------

async def handle_navigation(app_repository: Any, dialogs: Any, session: dict[str, Any],
                            user_id: int, parts: list[str]) -> CallbackReply | None:
    """Глагол ``n`` в области ``ts``."""
    if parts[:1] != ["ts"] or len(parts) < 2:
        return None
    if parts[1] == "cards":
        return CallbackReply(
            edit=await cards_reply(app_repository, dialogs, session, user_id, [])
        )
    if parts[1] == "sum":
        return CallbackReply(edit=await summary_reply(app_repository, session))
    return None


async def handle_step(context: SceneContext) -> Reply:
    """Свободного текста в сцене нет — возвращаем человека к кнопкам."""
    if context.session is None:
        return unavailable_reply()
    data = context.state.data or {}
    reply = await cards_reply(
        context.app_repository, context.dialogs, context.session, context.actor.user_id,
        [int(value) for value in data.get("passed", [])],
        [int(value) for value in data.get("deck", [])],
    )
    return Reply("Выберите кнопкой под карточкой.\n\n" + reply.text, reply.keyboard)
