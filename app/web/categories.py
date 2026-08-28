"""Человеческие названия кодов: разделы каталога, кухни, типы блюд.

В базе всё лежит кодами — «molochnye-produkty-yajjco-3», «georgian», «stew», —
и в интерфейсе они непригодны. Те же словари есть в ``static/js/format.js``:
браузер не может импортировать питон, поэтому копии две, но расхождение ловят
тесты ``test_telegram_shopping.CategoryLabelTests``.
"""

from __future__ import annotations

import re

CATEGORY_LABELS: dict[str, str] = {
    "molochnye-produkty-yajjco-3": "Молочные продукты, яйцо",
    "syry-2": "Сыры",
    "myaso-i-ptica-136": "Мясо и птица",
    "kolbasa-sosiski-754": "Колбаса и сосиски",
    "ryba-ikra-moreprodukty-183": "Рыба и морепродукты",
    "ovoshchi-frukty-144": "Овощи и фрукты",
    "makarony-krupy-muka-25": "Макароны, крупы, мука",
    "maslo-sousy-specii-20824": "Масло, соусы, специи",
    "hleb-i-vypechka-165": "Хлеб и выпечка",
    "zamorozka-77": "Заморозка",
    "konservaciya-94": "Консервация",
    "napitki-4": "Напитки",
    "kofe-chajj-kakao-242": "Кофе, чай, какао",
    "sladosti-1028": "Сладости",
    "sneki-20195": "Снеки",
    "gotovaya-eda-42": "Готовая еда",
    "zdorovoe-pitanie-1879": "Здоровое питание",
    "detskoe-pitanie-19327": "Детское питание",
    "alkogol-17036": "Алкоголь",
}

#: раздел, куда попадают позиции без сопоставления с каталогом
UNMATCHED_LABEL = "Уточнить в магазине"


CUISINES: dict[str, str] = {
    "russian": "Русская",
    "east_european": "Восточноевропейская",
    "italian": "Итальянская",
    "french": "Французская",
    "georgian": "Грузинская",
    "asian": "Азиатская",
    "mediterranean": "Средиземноморская",
    "middle_eastern": "Ближневосточная",
    "mexican": "Мексиканская",
    "indian": "Индийская",
    "japanese": "Японская",
    "american": "Американская",
}

DISH_TYPES: dict[str, str] = {
    "soup": "Суп",
    "salad": "Салат",
    "appetizer": "Закуска",
    "sandwich": "Сэндвич",
    "steak": "Стейк",
    "main_course": "Горячее",
    "stew": "Рагу",
    "cutlets": "Котлеты",
    "casserole": "Запеканка",
    "porridge": "Каша",
    "pasta": "Паста",
    "pizza": "Пицца",
    "dumplings": "Пельмени и вареники",
    "pancakes": "Блины и оладьи",
    "bread": "Хлеб",
    "pie": "Пирог",
    "cake": "Торт и кексы",
    "cookies": "Печенье",
    "dessert": "Десерт",
    "drink": "Напиток",
    "sauce": "Соус",
    "preserves": "Заготовки",
    "side": "Гарнир",
}


def cuisine_label(code: str | None) -> str:
    return CUISINES.get(str(code or ""), str(code or "")) or "—"


def dish_type_label(code: str | None) -> str:
    return DISH_TYPES.get(str(code or ""), str(code or "")) or "—"


def category_label(slug: str | None) -> str:
    """Название раздела. Незнакомый слаг показываем словами, а не прочерком."""
    if not slug:
        return UNMATCHED_LABEL
    known = CATEGORY_LABELS.get(slug)
    if known:
        return known
    words = re.sub(r"-\d+$", "", str(slug)).replace("-", " ")
    return words[:1].upper() + words[1:]
