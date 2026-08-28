"""Каталог «Ленты» в чате: поиск, разделы, сортировка, скидки (§5.9).

Витрина нужна, чтобы проверить цену и состав, не открывая браузер: планировщик
считает по этим же данным, и когда сумма кажется странной, полезно посмотреть,
какой именно товар он подобрал.
"""

from __future__ import annotations

from typing import Any

from app.web.categories import category_label

from ..callbacks import encode_callback
from ..fsm import DialogState
from ..render import (
    CARDS_PER_PAGE, CallbackReply, Reply, build_keyboard, button_text,
)
from . import SceneContext

SCENE = "products.search"

#: порядок выдачи: код в callback → (значение для репозитория, подпись)
SORTS = {
    "n": ("name", "По алфавиту"),
    "a": ("price_asc", "Сначала дешевле"),
    "d": ("price_desc", "Сначала дороже"),
}

#: сколько разделов показываем при выборе
CATEGORY_CHOICES = 12

SEARCH_HINT = "Напишите название товара — покажу цены и состав."


def _state(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "search": str(data.get("q") or ""),
        "category": str(data.get("category") or ""),
        "sort": str(data.get("sort") or "name"),
        "discount_only": bool(data.get("discount")),
    }


def money(kopecks: Any) -> str:
    return f"{int(kopecks) // 100} ₽" if kopecks else "—"


def _summary(data: dict[str, Any]) -> str:
    parts = []
    if data.get("q"):
        parts.append(f"«{data['q']}»")
    if data.get("category"):
        parts.append(category_label(data["category"]))
    if data.get("discount"):
        parts.append("только со скидкой")
    code = next((key for key, (value, _) in SORTS.items() if value == data.get("sort")), "n")
    if code != "n":
        parts.append(SORTS[code][1].lower())
    return " · ".join(parts)


async def results_reply(app_repository: Any, data: dict[str, Any]) -> Reply:
    page = max(int(data.get("page") or 1), 1)
    found = await app_repository.list_products(
        limit=CARDS_PER_PAGE, offset=(page - 1) * CARDS_PER_PAGE, **_state(data)
    )
    items = found.get("items") or []
    total = int(found.get("total") or 0)
    pages = max(1, -(-total // CARDS_PER_PAGE))

    lines = ([f"🏷 Нашёл {total} товаров — страница {min(page, pages)} из {pages}."]
             if total else ["🏷 Ничего не нашлось."])
    summary = _summary(data)
    if summary:
        lines.append(f"Фильтры: {summary}")
    lines.append(SEARCH_HINT if total else "Снимите фильтр или напишите другое название.")

    rows = [[{
        "text": button_text(
            f"{product.get('name')} · {money(product.get('effective_price_kop'))}"
        ),
        "callback_data": encode_callback("n", "pr", "c", int(product["id"])),
    }] for product in items]
    rows.append(_pager(page, pages))
    rows.append([
        {"text": "🗂 Раздел", "callback_data": encode_callback("f", "pr", "k")},
        {"text": "↕ Сортировка", "callback_data": encode_callback("f", "pr", "s")},
    ])
    rows.append([{
        "text": ("🔥 Только со скидкой" if data.get("discount")
                 else "☐ Только со скидкой"),
        "callback_data": encode_callback("f", "pr", "d"),
    }, {
        "text": "🧹 Сбросить", "callback_data": encode_callback("f", "pr", "x"),
    }])
    return Reply("\n".join(lines), build_keyboard(rows))


def _pager(page: int, pages: int) -> list[dict]:
    if pages <= 1:
        return []
    row = []
    if page > 1:
        row.append({"text": "◀", "callback_data": encode_callback("p", "pr", page - 1)})
    row.append({"text": f"{min(page, pages)}/{pages}",
                "callback_data": encode_callback("n", "noop")})
    if page < pages:
        row.append({"text": "▶", "callback_data": encode_callback("p", "pr", page + 1)})
    return row


async def begin(dialogs: Any, app_repository: Any, user_id: int) -> Reply:
    await dialogs.save(user_id, DialogState(SCENE, "query", {}))
    return await results_reply(app_repository, {})


async def handle_step(ctx: SceneContext) -> Reply:
    data = dict(ctx.state.data or {})
    data["q"] = (ctx.text or "").strip()
    data["page"] = 1
    await ctx.dialogs.save(ctx.actor.user_id, DialogState(SCENE, "query", data))
    return await results_reply(ctx.app_repository, data)


# --- карточка товара -----------------------------------------------------------

def card_reply(product: dict[str, Any]) -> Reply:
    lines = [f"🏷 {product.get('name')}"]
    meta = [str(product[key]) for key in ("brand", "pack_text") if product.get(key)]
    if meta:
        lines.append(" · ".join(meta))
    lines.append("")
    lines.append(f"Обычная цена: {money(product.get('regular_price_kop'))}")
    if product.get("loyalty_price_kop"):
        lines.append(f"По карте: {money(product['loyalty_price_kop'])}")
    if product.get("promo_price_kop"):
        discount = product.get("discount_percent")
        suffix = f" (−{int(discount)} %)" if discount else ""
        lines.append(f"Акция: {money(product['promo_price_kop'])}{suffix}")

    nutrition = [
        (label, product.get(key)) for label, key in (
            ("ккал", "kcal_100"), ("белки", "protein_100"),
            ("жиры", "fat_100"), ("углеводы", "carb_100"),
        )
    ]
    known = [f"{label} {value}" for label, value in nutrition if value is not None]
    if known:
        lines.append("")
        lines.append("На 100 г: " + ", ".join(known))
    if product.get("url"):
        lines.append("")
        lines.append(str(product["url"]))

    rows = [[{"text": "◀ К поиску", "callback_data": encode_callback("n", "pr", "back")}]]
    return Reply("\n".join(lines), build_keyboard(rows))


async def open_card(app_repository: Any, data: dict[str, Any],
                    product_id: int) -> CallbackReply:
    """Карточку собираем из той же выдачи: отдельного метода «товар по id» в
    репозитории нет, а заводить его ради одного экрана незачем."""
    page = max(int(data.get("page") or 1), 1)
    current = await app_repository.list_products(
        limit=CARDS_PER_PAGE, offset=(page - 1) * CARDS_PER_PAGE, **_state(data)
    )
    product = next(
        (item for item in (current.get("items") or []) if int(item["id"]) == product_id),
        None,
    )
    if product is None:
        return CallbackReply(toast="Товар не найден — обновите поиск.", show_alert=True)
    return CallbackReply(edit=card_reply(product))


# --- кнопки --------------------------------------------------------------------

async def _state_data(dialogs: Any, user_id: int) -> dict[str, Any]:
    state = await dialogs.load(user_id) if dialogs is not None else None
    return dict(state.data or {}) if state is not None and state.scene == SCENE else {}


async def _save(dialogs: Any, user_id: int, data: dict[str, Any]) -> None:
    if dialogs is not None:
        await dialogs.save(user_id, DialogState(SCENE, "query", data))


async def handle_filter(app_repository: Any, dialogs: Any, user_id: int,
                        parts: list[str]) -> CallbackReply | None:
    if parts[:1] != ["pr"] or len(parts) < 2:
        return None
    code = parts[1]
    data = await _state_data(dialogs, user_id)

    if code == "d":
        data["discount"] = not data.get("discount")
        data["page"] = 1
    elif code == "x":
        data = {}
    elif code == "k":
        if len(parts) > 2:
            data["category"] = "" if parts[2] == "-" else parts[2]
            data["page"] = 1
        else:
            return CallbackReply(edit=await _categories_reply(app_repository, data))
    elif code == "s":
        if len(parts) > 2 and parts[2] in SORTS:
            data["sort"] = SORTS[parts[2]][0]
            data["page"] = 1
        else:
            return CallbackReply(edit=_sorts_reply(data))
    else:
        return None

    await _save(dialogs, user_id, data)
    return CallbackReply(edit=await results_reply(app_repository, data))


async def _categories_reply(app_repository: Any, data: dict[str, Any]) -> Reply:
    categories = await app_repository.product_categories()
    current = data.get("category") or ""
    rows = [[{
        "text": button_text(
            f"{'✅' if entry['category_slug'] == current else '☐'} "
            f"{category_label(entry['category_slug'])} · {entry.get('product_count', 0)}"
        ),
        "callback_data": encode_callback("f", "pr", "k", entry["category_slug"]),
    }] for entry in categories[:CATEGORY_CHOICES]]
    rows.append([{"text": "Все разделы", "callback_data": encode_callback("f", "pr", "k", "-")}])
    return Reply("Раздел каталога:", build_keyboard(rows))


def _sorts_reply(data: dict[str, Any]) -> Reply:
    current = data.get("sort") or "name"
    rows = [[{
        "text": f"{'✅' if value == current else '☐'} {label}",
        "callback_data": encode_callback("f", "pr", "s", code),
    }] for code, (value, label) in SORTS.items()]
    return Reply("Как сортировать?", build_keyboard(rows))


async def handle_page(app_repository: Any, dialogs: Any, user_id: int,
                      parts: list[str]) -> CallbackReply | None:
    if parts[:1] != ["pr"]:
        return None
    data = await _state_data(dialogs, user_id)
    data["page"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    await _save(dialogs, user_id, data)
    return CallbackReply(edit=await results_reply(app_repository, data))


async def handle_navigation(app_repository: Any, dialogs: Any, user_id: int,
                            parts: list[str]) -> CallbackReply | None:
    if parts[:1] != ["pr"] or len(parts) < 2:
        return None
    data = await _state_data(dialogs, user_id)
    if parts[1] == "back":
        return CallbackReply(edit=await results_reply(app_repository, data))
    if parts[1] == "c" and len(parts) > 2 and parts[2].isdigit():
        return await open_card(app_repository, data, int(parts[2]))
    return None
