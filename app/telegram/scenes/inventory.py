"""Запасы одной строкой: «молоко 1 л до 05.09» (TZ-M7 §5.8, А5).

Пятишаговая форма ради «яйца 10 шт» — издевательство: в чате быстрее написать
строку, чем нажать пять кнопок. Поэтому основной ввод здесь — свободный текст,
а форма включается только там, где разбор не дотянул: не понял единицу или
количество. Всё остальное — умолчания: холодильник, штуки, без срока.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.web.payloads import STORAGE_AREAS, UNIT_CODES

from ..callbacks import encode_callback, pack_uuid, unpack_uuid
from ..fsm import CANCEL_BUTTON, DialogState
from ..render import (
    CARDS_PER_PAGE, CallbackReply, Reply, build_keyboard, button_text, paginate,
    pager_row,
)
from . import SceneContext

SCENE = "inventory.list"

#: единицы: словоформы → код в базе
UNIT_WORDS = {
    "шт": "piece", "штук": "piece", "штука": "piece", "штуки": "piece",
    "штучек": "piece", "шт.": "piece", "pcs": "piece",
    "г": "g", "гр": "g", "грамм": "g", "граммов": "g", "грамма": "g", "г.": "g",
    "кг": "kg", "килограмм": "kg", "килограмма": "kg", "килограммов": "kg", "кг.": "kg",
    "мл": "ml", "миллилитр": "ml", "миллилитров": "ml", "мл.": "ml",
    "л": "l", "литр": "l", "литра": "l", "литров": "l", "л.": "l",
}

#: место хранения: словоформы → код
STORAGE_WORDS = {
    "холодильник": "fridge", "холодильнике": "fridge", "фридж": "fridge",
    "морозилка": "freezer", "морозилке": "freezer", "морозильник": "freezer",
    "морозильнике": "freezer", "заморозка": "freezer", "заморозке": "freezer",
    "шкаф": "pantry", "шкафу": "pantry", "полка": "pantry", "полке": "pantry",
    "кладовка": "pantry", "кладовке": "pantry", "кладовая": "pantry",
}

STORAGE_LABELS = {"fridge": "холодильник", "freezer": "морозилка", "pantry": "шкаф"}
UNIT_LABELS = {"g": "г", "kg": "кг", "ml": "мл", "l": "л", "piece": "шт"}

#: пресеты как в вебе — чтобы не печатать самое частое
PRESETS = ("Молоко", "Яйца", "Хлеб", "Курица", "Рис", "Картофель", "Сыр", "Масло")

_MONTHS = (
    "январ", "феврал", "март", "апрел", "мая", "июн",
    "июл", "август", "сентябр", "октябр", "ноябр", "декабр",
)

#: «1.5 кг» — это количество, а не первое мая: единица сразу после числа
#: запрещает трактовать его как дату
_UNIT_ALTERNATION = "|".join(
    re.escape(word) for word in sorted(UNIT_WORDS, key=len, reverse=True)
)

ADD_HINT = (
    "Напишите одной строкой: «молоко 1 л до 05.09», «яйца 10», "
    "«курица 800 г морозилка»."
)


@dataclass
class ParsedLot:
    """Что удалось вынуть из строки; ``missing`` — о чём придётся спросить."""

    name: str = ""
    quantity: Decimal | None = None
    unit_code: str | None = None
    expires_on: date | None = None
    storage_area: str = "fridge"
    missing: list[str] = field(default_factory=list)

    def as_item(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "quantity": self.quantity,
            "unit_code": self.unit_code,
            "expires_on": self.expires_on,
            "storage_area": self.storage_area,
        }


def _parse_date(text: str, today: date) -> tuple[date | None, str]:
    """Дата и остаток строки. Понимает «до 05.09», «+5 дней», «через неделю»."""
    lowered = text.lower().replace("ё", "е")

    # окончание слова забираем целиком: иначе «через 3 дня» оставляло «я»
    # в названии продукта
    relative = re.search(
        r"(?:\+|через\s+)(\d{1,3})\s*(дн\w*|ден\w*|сут\w*|недел\w*)", lowered
    )
    if relative:
        amount = int(relative.group(1))
        days = amount * 7 if relative.group(2).startswith("недел") else amount
        return today + timedelta(days=days), _cut(text, relative.span())

    if "через неделю" in lowered:
        start = lowered.index("через неделю")
        return today + timedelta(days=7), _cut(text, (start, start + len("через неделю")))

    # «до 05.09» — точно дата; голое «1.5» перед единицей — количество,
    # иначе «масло 1.5 кг» превращалось в первое мая
    numeric = re.search(r"до\s+(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b", lowered)
    if numeric is None:
        numeric = re.search(
            rf"\b(\d{{1,2}})[.\-/](\d{{1,2}})(?:[.\-/](\d{{2,4}}))?\b(?!\s*(?:{_UNIT_ALTERNATION}))",
            lowered,
        )
    if numeric:
        day, month, year = numeric.groups()
        year = int(year or today.year)
        if year < 100:
            year += 2000
        try:
            parsed = date(year, int(month), int(day))
        except ValueError:
            return None, text
        return parsed, _cut(text, numeric.span())

    worded = re.search(r"(?:до\s+)?(\d{1,2})\s+([а-я]{3,})", lowered)
    if worded:
        day, month_name = worded.groups()
        for index, stem in enumerate(_MONTHS, 1):
            if month_name.startswith(stem[:4]):
                try:
                    parsed = date(today.year, index, int(day))
                except ValueError:
                    return None, text
                if parsed < today:
                    parsed = date(today.year + 1, index, int(day))
                return parsed, _cut(text, worded.span())
    return None, text


def _cut(text: str, span: tuple[int, int]) -> str:
    return (text[:span[0]] + " " + text[span[1]:]).strip()


def _parse_storage(text: str) -> tuple[str | None, str]:
    for word, code in STORAGE_WORDS.items():
        match = re.search(rf"\b{re.escape(word)}\b", text.lower().replace("ё", "е"))
        if match:
            return code, _cut(text, match.span())
    return None, text


def _parse_quantity(text: str) -> tuple[Decimal | None, str | None, str]:
    """Количество и единица. «10 шт», «800 г», «1.5 л», просто «10»."""
    pattern = re.compile(r"(\d+(?:[.,]\d+)?)\s*([a-zA-Zа-яА-ЯёЁ.]+)?")
    for match in pattern.finditer(text):
        raw_amount, raw_unit = match.group(1), (match.group(2) or "").lower()
        unit = UNIT_WORDS.get(raw_unit.rstrip("."), UNIT_WORDS.get(raw_unit))
        if raw_unit and unit is None:
            # число прилипло к слову названия («молоко 2 пакета») — не количество
            continue
        try:
            amount = Decimal(raw_amount.replace(",", "."))
        except InvalidOperation:
            continue
        if amount <= 0:
            continue
        end = match.end() if raw_unit else match.end(1)
        return amount, unit, _cut(text, (match.start(), end))
    return None, None, text


def parse_lot(text: str, today: date) -> ParsedLot:
    """Строка → заготовка партии. Что не распозналось, попадает в ``missing``."""
    rest = re.sub(r"\s+", " ", (text or "").strip())
    expires_on, rest = _parse_date(rest, today)
    storage, rest = _parse_storage(rest)
    quantity, unit, rest = _parse_quantity(rest)

    name = re.sub(r"^[\s,.:;-]+|[\s,.:;-]+$", "", rest)
    lot = ParsedLot(
        name=name[:120],
        quantity=quantity,
        # только число без слова — почти всегда штуки: «яйца 10»
        unit_code=unit or ("piece" if quantity is not None else None),
        expires_on=expires_on,
        storage_area=storage or "fridge",
    )
    if not lot.name:
        lot.missing.append("name")
    if lot.quantity is None:
        lot.missing.append("quantity")
    return lot


# --- экран списка --------------------------------------------------------------

def _expiry_badge(lot: dict[str, Any], today: date) -> str:
    expires_on = lot.get("expires_on")
    if not expires_on:
        return "без срока"
    if isinstance(expires_on, str):
        expires_on = date.fromisoformat(expires_on)
    left = (expires_on - today).days
    if left < 0:
        return "просрочен"
    if left == 0:
        return "сегодня"
    if left <= 3:
        return f"{left} дн."
    return f"до {expires_on.day:02d}.{expires_on.month:02d}"


def _lot_line(lot: dict[str, Any], today: date) -> str:
    quantity = Decimal(str(lot.get("quantity") or 0)).normalize()
    unit = UNIT_LABELS.get(str(lot.get("unit_code")), str(lot.get("unit_code") or ""))
    place = STORAGE_LABELS.get(str(lot.get("storage_area")), "")
    return (
        f"• {lot.get('name')} — {quantity:f} {unit} · {_expiry_badge(lot, today)}"
        f"{f' · {place}' if place else ''}"
    )


def list_reply(lots: list[dict[str, Any]], today: date, page: int = 1,
               notice: str = "") -> Reply:
    """Запасы по сроку годности, страницами по 8 (§5.8)."""
    if not lots:
        lines = ["🧊 Запасов пока нет.", ADD_HINT]
        rows = [[{
            "text": name,
            "callback_data": encode_callback("f", "in", "p", index),
        } for index, name in enumerate(PRESETS[:4])]]
        return Reply("\n".join(filter(None, [notice, *lines])), build_keyboard(rows))

    current = paginate(lots, page, CARDS_PER_PAGE)
    expired = sum(1 for lot in lots if _expiry_badge(lot, today) == "просрочен")
    lines = [f"🧊 Запасы — {len(lots)} поз."]
    if expired:
        lines.append(f"Просрочено: {expired}.")
    if current.pages > 1:
        lines.append(f"Страница {current.page} из {current.pages}.")
    lines.append("")
    lines += [_lot_line(lot, today) for lot in current.items]
    lines.append("")
    lines.append(ADD_HINT)

    rows = [[{
        "text": button_text(f"🗑 {lot.get('name')} · {_expiry_badge(lot, today)}"),
        "callback_data": encode_callback("i", pack_uuid(lot["id"])),
    }] for lot in current.items]
    rows.append(pager_row("in", current))
    return Reply("\n".join(filter(None, [notice, *lines])), build_keyboard(rows))


async def show(app_repository: Any, session: dict[str, Any], today: date,
               page: int = 1, notice: str = "") -> Reply:
    lots = await app_repository.list_inventory(session)
    return list_reply(lots, today, page, notice)


async def begin(dialogs: Any, app_repository: Any, session: dict[str, Any],
                user_id: int, today: date) -> Reply:
    await dialogs.save(user_id, DialogState(SCENE, "line", {}))
    return await show(app_repository, session, today)


# --- добавление ----------------------------------------------------------------

def _confirm_expired_reply(lot: ParsedLot) -> Reply:
    """Просроченный срок подтверждается явно — как 422 already_expired в вебе."""
    return Reply(
        f"«{lot.name}» просрочен ({lot.expires_on:%d.%m}). Всё равно добавить?",
        build_keyboard([[
            {"text": "Да, добавить", "callback_data": encode_callback("y", "inx")},
            CANCEL_BUTTON,
        ]]),
    )


def _ask_unit_reply(lot: ParsedLot) -> Reply:
    rows = [[{
        "text": label,
        "callback_data": encode_callback("f", "in", "u", code),
    } for code, label in list(UNIT_LABELS.items())[:3]], [{
        "text": label,
        "callback_data": encode_callback("f", "in", "u", code),
    } for code, label in list(UNIT_LABELS.items())[3:]]]
    rows.append([CANCEL_BUTTON])
    return Reply(f"«{lot.name}»: сколько и в чём? Напишите число или выберите единицу.",
                 build_keyboard(rows))


async def add_line(app_repository: Any, dialogs: Any, session: dict[str, Any],
                   user_id: int, text: str, today: date) -> Reply:
    """Разобрать строку и либо добавить, либо спросить недостающее."""
    lot = parse_lot(text, today)
    if "name" in lot.missing:
        return Reply(f"Не понял, что добавить.\n\n{ADD_HINT}")
    if "quantity" in lot.missing:
        await dialogs.save(user_id, DialogState(SCENE, "quantity", {"draft": _draft(lot)}))
        return _ask_unit_reply(lot)
    if lot.expires_on is not None and lot.expires_on < today:
        await dialogs.save(user_id, DialogState(SCENE, "expired", {"draft": _draft(lot)}))
        return _confirm_expired_reply(lot)
    return await _store(app_repository, dialogs, session, user_id, lot, today)


def _draft(lot: ParsedLot) -> dict[str, Any]:
    return {
        "name": lot.name,
        "quantity": str(lot.quantity) if lot.quantity is not None else None,
        "unit_code": lot.unit_code,
        "expires_on": lot.expires_on.isoformat() if lot.expires_on else None,
        "storage_area": lot.storage_area,
    }


def _from_draft(draft: dict[str, Any]) -> ParsedLot:
    return ParsedLot(
        name=str(draft.get("name") or ""),
        quantity=Decimal(draft["quantity"]) if draft.get("quantity") else None,
        unit_code=draft.get("unit_code"),
        expires_on=date.fromisoformat(draft["expires_on"]) if draft.get("expires_on") else None,
        storage_area=str(draft.get("storage_area") or "fridge"),
    )


async def _store(app_repository: Any, dialogs: Any, session: dict[str, Any],
                 user_id: int, lot: ParsedLot, today: date) -> Reply:
    if lot.unit_code not in UNIT_CODES or lot.storage_area not in STORAGE_AREAS:
        return Reply("Не понял единицу или место хранения. Попробуйте ещё раз.")
    try:
        await app_repository.add_inventory(session, lot.as_item())
    except PermissionError as exc:
        return Reply(str(exc))
    except ValueError as exc:
        return Reply(str(exc))
    await dialogs.save(user_id, DialogState(SCENE, "line", {}))
    quantity = lot.quantity.normalize() if lot.quantity is not None else 0
    unit = UNIT_LABELS.get(str(lot.unit_code), "")
    notice = f"✅ Добавил: {lot.name} — {quantity:f} {unit}.\n"
    return await show(app_repository, session, today, 1, notice)


async def handle_step(ctx: SceneContext) -> Reply:
    """Свободный текст в сцене запасов — новая партия либо ответ на уточнение."""
    data = dict(ctx.state.data or {})
    if ctx.state.step == "quantity" and data.get("draft"):
        lot = _from_draft(data["draft"])
        quantity, unit, _rest = _parse_quantity(ctx.text or "")
        if quantity is None:
            return _ask_unit_reply(lot)
        lot.quantity = quantity
        lot.unit_code = unit or lot.unit_code or "piece"
        return await _store(
            ctx.app_repository, ctx.dialogs, ctx.session or {}, ctx.actor.user_id, lot, ctx.today
        )
    return await add_line(
        ctx.app_repository, ctx.dialogs, ctx.session or {}, ctx.actor.user_id,
        ctx.text, ctx.today,
    )



# --- кнопки --------------------------------------------------------------------

async def handle_filter(app_repository: Any, dialogs: Any, session: dict[str, Any],
                        user_id: int, parts: list[str], today: date) -> CallbackReply | None:
    """Глагол ``f`` для запасов: пресеты и выбор единицы."""
    if parts[:1] != ["in"] or len(parts) < 3:
        return None
    code, value = parts[1], parts[2]
    if code == "p":  # пресет-название
        index = int(value) if value.isdigit() else 0
        name = PRESETS[index] if index < len(PRESETS) else PRESETS[0]
        lot = ParsedLot(name=name)
        await dialogs.save(user_id, DialogState(SCENE, "quantity", {"draft": _draft(lot)}))
        return CallbackReply(edit=_ask_unit_reply(lot))
    if code == "u":  # единица для уточняемой партии
        state = await dialogs.load(user_id)
        draft = (state.data or {}).get("draft") if state else None
        if not draft:
            return CallbackReply(toast="Черновик потерялся — напишите строку заново.",
                                 show_alert=True)
        lot = _from_draft(draft)
        lot.unit_code = value
        if lot.quantity is None:
            await dialogs.save(user_id, DialogState(SCENE, "quantity", {"draft": _draft(lot)}))
            return CallbackReply(edit=Reply(
                f"«{lot.name}»: сколько {UNIT_LABELS.get(value, value)}? Напишите число."
            ))
        reply = await _store(app_repository, dialogs, session, user_id, lot, today)
        return CallbackReply(edit=reply)
    return None


async def handle_page(app_repository: Any, session: dict[str, Any], parts: list[str],
                      today: date) -> CallbackReply | None:
    if parts[:1] != ["in"]:
        return None
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    return CallbackReply(edit=await show(app_repository, session, today, page))


async def confirm_expired(app_repository: Any, dialogs: Any, session: dict[str, Any],
                          user_id: int, today: date) -> CallbackReply:
    state = await dialogs.load(user_id)
    draft = (state.data or {}).get("draft") if state else None
    if not draft:
        return CallbackReply(toast="Черновик потерялся — напишите строку заново.",
                             show_alert=True)
    reply = await _store(
        app_repository, dialogs, session, user_id, _from_draft(draft), today
    )
    return CallbackReply(edit=reply)


# --- удаление с возвратом --------------------------------------------------------

async def delete(app_repository: Any, dialogs: Any, session: dict[str, Any],
                 user_id: int, packed: str, today: date) -> CallbackReply:
    """Удалить партию, оставив кнопку «↩ Вернуть».

    Удаляем сразу, а не с отложенным таймером: перезапуск бота не должен
    оставлять «удалённую» партию в базе. Вернуть можно, пока экран открыт —
    все поля лежат в состоянии диалога.
    """
    lot_id = unpack_uuid(packed)
    if lot_id is None:
        return CallbackReply(toast="Не понял кнопку.")
    lots = await app_repository.list_inventory(session)
    lot = next((entry for entry in lots if str(entry.get("id")) == str(lot_id)), None)
    if lot is None:
        return CallbackReply(toast="Эта партия уже удалена.", show_alert=True)
    try:
        deleted = await app_repository.delete_inventory(session, lot_id)
    except PermissionError as exc:
        return CallbackReply(toast=str(exc), show_alert=True)
    if not deleted:
        return CallbackReply(toast="Эта партия уже удалена.", show_alert=True)

    await dialogs.save(user_id, DialogState(SCENE, "line", {"undo": _undo_draft(lot)}))
    reply = await show(app_repository, session, today, 1, f"🗑 Убрал «{lot.get('name')}».\n")
    rows = list((reply.keyboard or {}).get("inline_keyboard") or [])
    rows.insert(0, [{"text": "↩ Вернуть", "callback_data": encode_callback("y", "inu")}])
    return CallbackReply(
        toast=f"Убрал: {lot.get('name')}",
        edit=Reply(reply.text, build_keyboard(rows)),
    )


def _undo_draft(lot: dict[str, Any]) -> dict[str, Any]:
    expires_on = lot.get("expires_on")
    if isinstance(expires_on, date):
        expires_on = expires_on.isoformat()
    return {
        "name": lot.get("name"),
        "quantity": str(lot.get("quantity")),
        "unit_code": lot.get("unit_code"),
        "expires_on": expires_on,
        "storage_area": lot.get("storage_area") or "fridge",
    }


async def undo_delete(app_repository: Any, dialogs: Any, session: dict[str, Any],
                      user_id: int, today: date) -> CallbackReply:
    state = await dialogs.load(user_id)
    undo = (state.data or {}).get("undo") if state else None
    if not undo:
        return CallbackReply(toast="Возвращать уже нечего.", show_alert=True)
    lot = _from_draft(undo)
    await dialogs.save(user_id, DialogState(SCENE, "line", {}))
    reply = await _store(app_repository, dialogs, session, user_id, lot, today)
    return CallbackReply(toast="Вернул", edit=reply)
