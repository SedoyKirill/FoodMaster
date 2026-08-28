"""Список покупок по разделам магазина (TZ-M7 §5.6).

Плоский чек-лист из сорока позиций в чате нечитаем и не совпадает с тем, как
человек ходит по магазину. Поэтому сначала — разделы с прогрессом («Молочные
2/5»), внутри раздела — чек-лист, и отдельно «Все подряд» для тех, кому
привычнее сплошной список.

Позиции «по вкусу» и то, что не сопоставилось с каталогом, живут отдельной
группой без чекбоксов: соль по вкусу не покупают, а «готовую грудку в меду»
из книги в магазине не найти.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.web.categories import UNMATCHED_LABEL, category_label

from ..callbacks import encode_callback, pack_uuid, unpack_uuid
from ..render import (
    BUTTONS_PER_PAGE, CallbackReply, Reply, build_keyboard, button_text,
    paginate, pager_row, quantity_text, to_buy,
)

#: псевдоразделы: у позиции нет категории каталога либо она «по вкусу»
NO_CATEGORY = "~none"
TASTE = "~taste"
TASTE_LABEL = "🧂 По вкусу и уточнить"

#: сколько разделов показываем сразу — их в каталоге девятнадцать
MAX_CATEGORIES = 20


# --- раскладка позиций ---------------------------------------------------------

def split_items(items: list[dict[str, Any]]) -> tuple[dict[str, list], list, list]:
    """Позиции → разделы с чекбоксами, «по вкусу» и «есть дома».

    «Есть дома» — то, что план покрыл запасами: покупать нечего, но в деталях
    показать стоит, иначе человек решит, что продукт забыли.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    taste: list[dict[str, Any]] = []
    covered: list[dict[str, Any]] = []
    buyable = {id(item) for item in to_buy(items)}
    for item in items:
        if item.get("to_taste"):
            taste.append(item)
        elif id(item) in buyable:
            groups.setdefault(item.get("category_slug") or NO_CATEGORY, []).append(item)
        else:
            covered.append(item)
    return groups, taste, covered


def group_label(slug: str) -> str:
    if slug == TASTE:
        return TASTE_LABEL
    if slug == NO_CATEGORY:
        return UNMATCHED_LABEL
    return category_label(slug)


def _sorted_slugs(groups: dict[str, list]) -> list[str]:
    """Разделы по алфавиту; «уточнить в магазине» — всегда последним."""
    return sorted(groups, key=lambda slug: (slug == NO_CATEGORY, group_label(slug).lower()))


def _remaining_cost(items: list[dict[str, Any]]) -> int:
    return sum(
        int(item.get("estimated_cost_kop") or 0)
        for item in items if item.get("purchased_at") is None
    )


# --- экран разделов ------------------------------------------------------------

def overview_reply(plan: dict[str, Any]) -> Reply:
    """Разделы магазина с прогрессом покупок (§5.6)."""
    items = plan.get("shopping") or []
    groups, taste, _covered = split_items(items)
    buyable = to_buy(items)
    if not buyable and not taste:
        return Reply("🛒 Списка покупок нет — сначала составьте меню.")

    bought = [item for item in buyable if item.get("purchased_at") is not None]
    total_kop = sum(int(item.get("estimated_cost_kop") or 0) for item in buyable)
    left_kop = _remaining_cost(buyable)
    if buyable and len(bought) == len(buyable):
        lines = ["🛒 Всё куплено. Отличная работа!"]
    else:
        lines = [f"🛒 Покупки — куплено {len(bought)} из {len(buyable)}."]
        if total_kop:
            lines.append(f"Осталось купить на {left_kop // 100} ₽ из {total_kop // 100} ₽.")
        lines.append("Выберите раздел магазина — внутри отмечайте купленное.")

    plan_id = pack_uuid(plan["id"])
    rows = []
    for slug in _sorted_slugs(groups)[:MAX_CATEGORIES]:
        group = groups[slug]
        done = sum(1 for item in group if item.get("purchased_at") is not None)
        rows.append([{
            "text": button_text(f"{group_label(slug)} · {done}/{len(group)}"),
            "callback_data": encode_callback("f", "sh", plan_id, slug),
        }])
    if taste:
        rows.append([{
            "text": button_text(f"{TASTE_LABEL} · {len(taste)}"),
            "callback_data": encode_callback("f", "sh", plan_id, TASTE),
        }])
    rows.append([
        {"text": "📋 Все подряд", "callback_data": encode_callback("p", "sh", plan_id, 1)},
        {"text": "📅 Меню", "callback_data": encode_callback("d", plan_id, 1)},
    ])
    return Reply("\n".join(lines), build_keyboard(rows))


# --- чек-лист раздела ----------------------------------------------------------

def _item_button(plan_id: str, item: dict[str, Any], marker: str = "c") -> dict[str, Any]:
    """Кнопка позиции. ``marker`` говорит, куда вернуться после отметки:
    «c» — в раздел, номер страницы — в сквозной список."""
    mark = "✅" if item.get("purchased_at") else "☐"
    parts = [str(item.get("normalized_name") or "позиция")]
    packs = item.get("pack_count")
    parts.append(f"{packs} уп" if packs else quantity_text(item))
    cost = item.get("estimated_cost_kop")
    if cost:
        parts.append(f"{int(cost) // 100} ₽")
    return {
        "text": button_text(f"{mark} {' · '.join(parts)}"),
        "callback_data": encode_callback("s", plan_id, pack_uuid(item["id"]), marker),
    }


def category_reply(plan: dict[str, Any], slug: str, page: int = 1) -> Reply:
    """Чек-лист одного раздела; «по вкусу» — без чекбоксов (§5.6)."""
    items = plan.get("shopping") or []
    groups, taste, _covered = split_items(items)
    plan_id = pack_uuid(plan["id"])

    if slug == TASTE:
        lines = [
            f"{TASTE_LABEL} — {len(taste)} поз.",
            "Отмечать нечего: соль и специи берут на глаз, а состав уточняют на месте.",
            "",
        ]
        lines += [f"• {item.get('normalized_name')}" for item in taste]
        return Reply("\n".join(lines), build_keyboard([[_back_button(plan_id)]]))

    group = groups.get(slug)
    if not group:
        return Reply(
            "Этот раздел опустел — список покупок пересобрался.",
            build_keyboard([[_back_button(plan_id)]]),
        )

    current = paginate(group, page, BUTTONS_PER_PAGE)
    done = sum(1 for item in group if item.get("purchased_at") is not None)
    lines = [f"{group_label(slug)} — куплено {done} из {len(group)}."]
    if done == len(group):
        lines.append("Всё куплено в этом разделе.")
    left_kop = _remaining_cost(group)
    if left_kop:
        lines.append(f"Осталось на {left_kop // 100} ₽.")
    if current.pages > 1:
        lines.append(f"Страница {current.page} из {current.pages}.")

    rows = [[_item_button(plan_id, item)] for item in current.items]
    rows.append(pager_row("sc", current, plan_id, slug))
    rows.append([
        {
            "text": "ℹ️ Подробнее",
            "callback_data": encode_callback("f", "sh", plan_id, slug, "i"),
        },
        _back_button(plan_id),
    ])
    return Reply("\n".join(lines), build_keyboard(rows))


def _back_button(plan_id: str) -> dict[str, Any]:
    return {"text": "◀ К разделам", "callback_data": encode_callback("f", "sh", plan_id, "")}


def details_reply(plan: dict[str, Any], slug: str) -> Reply:
    """Развёрнутый текст раздела: что есть дома, упаковки, ссылки на «Ленту»."""
    items = plan.get("shopping") or []
    groups, _taste, covered = split_items(items)
    group = groups.get(slug) or []
    plan_id = pack_uuid(plan["id"])
    lines = [f"{group_label(slug)} — подробно:", ""]
    for item in group:
        mark = "✅" if item.get("purchased_at") else "☐"
        lines.append(f"{mark} {item.get('normalized_name')} — нужно {quantity_text(item)}")
        home = item.get("covered_from_inventory")
        if home and Decimal(str(home)) > 0:
            lines.append(f"   есть дома: {Decimal(str(home)).normalize():f}")
        product = item.get("matched_product_name")
        if product:
            packs = item.get("pack_count")
            cost = item.get("estimated_cost_kop")
            detail = product
            if packs:
                detail += f" · {packs} уп"
            if cost:
                detail += f" · {int(cost) // 100} ₽"
            lines.append(f"   {detail}")
            url = item.get("matched_product_url")
            if url:
                lines.append(f"   {url}")
        else:
            lines.append("   товар не сопоставлен — уточните в магазине")
    # «есть дома» — только по этому разделу: один и тот же список под каждым
    # заголовком читался бы как ошибка
    at_home = [
        item for item in covered
        if not item.get("to_taste")
        and (item.get("category_slug") or NO_CATEGORY) == slug
    ]
    if at_home:
        lines.append("")
        lines.append("Полностью хватает домашних запасов:")
        lines += [f"• {item.get('normalized_name')}" for item in at_home]
    rows = [[
        {
            "text": "◀ К разделу",
            "callback_data": encode_callback("f", "sh", plan_id, slug),
        },
        _back_button(plan_id),
    ]]
    return Reply("\n".join(lines), build_keyboard(rows))


# --- сквозной список ------------------------------------------------------------

def flat_reply(plan: dict[str, Any], page: int = 1) -> Reply:
    """«Все подряд»: один чек-лист страницами по тридцать позиций."""
    items = plan.get("shopping") or []
    buyable = to_buy(items)
    if not buyable:
        return Reply("🛒 Покупать нечего — всё есть дома или берётся по вкусу.")
    current = paginate(buyable, page, BUTTONS_PER_PAGE)
    plan_id = pack_uuid(plan["id"])
    done = sum(1 for item in buyable if item.get("purchased_at") is not None)
    lines = [f"🛒 Все покупки — куплено {done} из {len(buyable)}."]
    left_kop = _remaining_cost(buyable)
    if left_kop:
        lines.append(f"Осталось на {left_kop // 100} ₽.")
    if current.pages > 1:
        lines.append(f"Страница {current.page} из {current.pages}.")
    # позиция из сквозного списка возвращает в него же, а не в раздел
    rows = [[_item_button(plan_id, item, str(current.page))] for item in current.items]
    rows.append(pager_row("sh", current, plan_id))
    rows.append([_back_button(plan_id)])
    return Reply("\n".join(lines), build_keyboard(rows))


# --- обработка нажатий ----------------------------------------------------------

def item_view(plan: dict[str, Any], item_id: Any, marker: str) -> Reply:
    """Куда вернуть человека после отметки: в раздел или в сквозной список."""
    if marker.isdigit():
        return flat_reply(plan, int(marker))
    item = next(
        (entry for entry in plan.get("shopping") or []
         if str(entry.get("id")) == str(item_id)),
        None,
    )
    slug = (item or {}).get("category_slug") or NO_CATEGORY
    group = split_items(plan.get("shopping") or [])[0].get(slug) or []
    page = 1
    for index, entry in enumerate(group):
        if str(entry.get("id")) == str(item_id):
            page = index // BUTTONS_PER_PAGE + 1
            break
    return category_reply(plan, slug, page)


async def handle_filter(app_repository: Any, session: dict[str, Any],
                        parts: list[str]) -> CallbackReply | None:
    """Глагол ``f`` для покупок: разделы, раздел, подробности."""
    if parts[:1] != ["sh"] or len(parts) < 3:
        return None
    plan_id = unpack_uuid(parts[1])
    if plan_id is None:
        return CallbackReply(toast="Не понял кнопку.")
    plan = await app_repository.get_plan(session, plan_id)
    if plan is None:
        return CallbackReply(toast="План не найден — откройте покупки заново.", show_alert=True)
    slug = parts[2]
    if not slug:
        return CallbackReply(edit=overview_reply(plan))
    if len(parts) > 3 and parts[3] == "i":
        return CallbackReply(edit=details_reply(plan, slug))
    return CallbackReply(edit=category_reply(plan, slug))


async def handle_page(app_repository: Any, session: dict[str, Any],
                      parts: list[str]) -> CallbackReply | None:
    """Глагол ``p``: страницы сквозного списка (``sh``) и раздела (``sc``)."""
    scope = parts[0] if parts else ""
    if scope not in {"sh", "sc"} or len(parts) < 2:
        return None
    plan_id = unpack_uuid(parts[1])
    if plan_id is None:
        return CallbackReply(toast="Не понял кнопку.")
    plan = await app_repository.get_plan(session, plan_id)
    if plan is None:
        return CallbackReply(toast="План не найден — откройте покупки заново.", show_alert=True)
    if scope == "sh":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        return CallbackReply(edit=flat_reply(plan, page))
    slug = parts[2] if len(parts) > 2 else NO_CATEGORY
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    return CallbackReply(edit=category_reply(plan, slug, page))
