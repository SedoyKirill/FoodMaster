"""Упаковки и остатки на весь горизонт (TZ-M8 §6.3).

Главный экономический эффект M8: солвер видит не «цену ингредиента по
пропорции пачки», а целые пачки на весь план. Пачка сметаны, купленная ради
блинов в понедельник, закрывает и запеканку в среду — до сих пор модель
считала её дважды по 40 %, а список покупок покупал одну и получал план
дороже собственного итога.

Моделируются только **общие** товары — те, что встречаются минимум у двух
кандидатов горизонта. Товар, нужный одному блюду, ничего не экономит: его
цена целыми пачками остаётся в «личной» стоимости кандидата. Это же держит
размер модели: общих товаров сотня, а не десять тысяч.

Ноль остатка — не всегда оптимум (TZ-v2 §10): 200 г лишней крупы пролежат
до следующего плана, 200 мл лишних сливок пропадут. Поэтому остаток
штрафуется пропорционально скорости порчи, цене и объёму.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .candidates import canonical_overlap, stock_available

#: Насколько быстро портится то, что осталось в пачке: 0 — переживёт следующий
#: план, 1 — пропадёт. По категориям каталога Ленты (`store_product_categories`).
PERISH_BY_CATEGORY = {
    "molochnye-produkty-yajjco-3": 1.0,
    "myaso-i-ptica-136": 1.0,
    "ryba-ikra-moreprodukty-183": 1.0,
    "kolbasa-sosiski-754": 1.0,
    "gotovaya-eda-42": 1.0,
    "hleb-i-vypechka-165": 1.0,
    "ovoshchi-frukty-144": 0.5,
    "syry-2": 0.5,
    "zamorozka-77": 0.0,
    "makarony-krupy-muka-25": 0.0,
    "maslo-sousy-specii-20824": 0.0,
    "konservaciya-94": 0.0,
    "kofe-chajj-kakao-242": 0.0,
    "napitki-4": 0.0,
    "sladosti-1028": 0.0,
    "sneki-20195": 0.0,
    "alkogol-17036": 0.0,
    "zdorovoe-pitanie-1879": 0.0,
    "detskoe-pitanie-19327": 0.0,
}
#: товар без известной категории — середина шкалы, а не «не портится»
DEFAULT_PERISH = 0.5

#: сколько общих товаров попадает в модель (§6.3): дальше растёт время, а не
#: качество — остальные считаются «личными»
MAX_SHARED_PRODUCTS = 120
#: потолок переменных модели упаковок; за ним модель усекается
MAX_PACK_VARIABLES = 20_000
#: товар моделируется, только если он нужен минимум стольким кандидатам
MIN_SHARING_RECIPES = 2


@dataclass(frozen=True)
class PackProduct:
    """Товар каталога в модели: пачка, цена, порча и то, что есть дома."""

    product_id: int
    unit: str | None
    pack_base: int
    price_kop: int
    perish: float
    stock_base: int
    #: верхняя граница числа пачек на горизонте
    max_packs: int
    #: как продукт называется в рецепте — для причины «одна пачка с…» (§5)
    label: str = ""


@dataclass
class PackModel:
    """Общие товары горизонта и потребность в них у каждого кандидата."""

    products: dict[int, PackProduct] = field(default_factory=dict)
    #: recipe_id → product_id → потребность на семью в базовых единицах
    needs: dict[int, dict[int, int]] = field(default_factory=dict)
    #: recipe_id → стоимость товаров, которые моделью не покрыты
    private_cost_kop: dict[int, int] = field(default_factory=dict)
    #: модель урезана лимитом — план получит предупреждение
    truncated: bool = False

    def __bool__(self) -> bool:
        return bool(self.products)

    def need(self, recipe_id: int, product_id: int) -> int:
        return self.needs.get(recipe_id, {}).get(product_id, 0)


def perish_of(product: dict[str, Any]) -> float:
    """Скорость порчи товара по категориям каталога; худшая из известных."""
    values = [
        PERISH_BY_CATEGORY[slug]
        for slug in (product.get("category_slugs") or ())
        if slug in PERISH_BY_CATEGORY
    ]
    return max(values) if values else DEFAULT_PERISH


def _quantity_of(ingredient: dict[str, Any]) -> Decimal | None:
    quantity = ingredient.get("quantity_max") or ingredient.get("quantity_min")
    if quantity is None or ingredient.get("is_to_taste"):
        return None
    try:
        return Decimal(str(quantity))
    except ArithmeticError:  # pragma: no cover - защита от мусора в книге
        return None


def _allocate_stock(
    stock: list[tuple[str, str | None, Decimal]],
    labels: dict[int, str],
    units: dict[int, str | None],
) -> dict[int, int]:
    """Делит домашние запасы между товарами, а не раздаёт каждому целиком.

    ``stock_available`` смотрит запас, не списывая его: каждому кандидату
    важно, сколько ему не придётся покупать. Для модели горизонта это
    неверно — одна пачка лука дома не закрывает и лук в супе, и лук в рагу
    дважды. Список покупок списывает лоты по одному разу (FEFO), и модель
    расходилась с ним ровно на эту величину.
    """
    remaining = [[canonical, unit, amount] for canonical, unit, amount in stock]
    allocated: dict[int, int] = {}
    # Порядок — по каноническому имени, тот же, в котором собирается список
    # покупок: при равных правах на лот выигрывает один и тот же продукт.
    for product_id in sorted(labels, key=lambda item: (labels[item], item)):
        canonical = labels[product_id]
        unit = units.get(product_id)
        total = Decimal("0")
        for lot in remaining:
            if lot[1] != unit or not canonical_overlap(lot[0], canonical):
                continue
            total += lot[2]
            lot[2] = Decimal("0")
        allocated[product_id] = int(total)
    return allocated


def build_pack_model(
    *,
    recipe_ids: list[int],
    recipes_by_id: dict[int, dict[str, Any]],
    costs_by_recipe: dict[int, int],
    slots: int,
    scale_of: Any,
    matcher: Any,
    price_tier: str,
    stock: list[tuple[str, str | None, Decimal]],
    synonyms: Any,
    normal: Any,
    base_quantity: Any,
    max_products: int = MAX_SHARED_PRODUCTS,
) -> PackModel:
    """Сводит кандидатов горизонта к общим товарам и потребности в них.

    ``costs_by_recipe`` — стоимость кандидата, посчитанная скорингом; из неё
    вычитается всё, что модель берёт на себя, и остаётся «личная» цена.
    """
    model = PackModel()
    needs: dict[int, dict[int, int]] = {}
    #: recipe → product → сколько придётся купить (запасы уже вычтены)
    buy_by_product: dict[int, dict[int, Decimal]] = {}
    recipes_of_product: dict[int, set[int]] = {}
    products: dict[int, dict[str, Any]] = {}
    labels: dict[int, str] = {}
    units_of_product: dict[int, str | None] = {}
    max_need_of_product: dict[int, int] = {}

    for recipe_id in recipe_ids:
        recipe = recipes_by_id.get(recipe_id)
        if recipe is None:
            continue
        scale = scale_of(recipe) if scale_of is not None else None
        if scale is None:
            scale = Decimal("1")
        for ingredient in recipe.get("ingredients", []):
            name = str(
                ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""
            )
            quantity = _quantity_of(ingredient)
            if not name or quantity is None:
                continue
            unit = ingredient.get("unit_code")
            needed_base, unit_base = base_quantity(quantity * scale, unit)
            if needed_base is None or needed_base <= 0:
                continue
            canonical = synonyms.canonical_name(name, normal)
            have = stock_available(stock, canonical, unit_base) if canonical else Decimal("0")
            remaining = max(Decimal("0"), needed_base - have)
            # Товар ищется ровно так же, как его ищет список покупок: по
            # каноническому имени и по тому количеству, которое придётся
            # купить. Скоринг ищет по книжному названию, и на «луке» это
            # давало разные товары — модель считала одну пачку, список другую
            # (диагностика 28.08.2026: 16 расхождений на 30 сборок).
            product = matcher.match(
                canonical or name, unit_base, price_tier, remaining or needed_base
            )
            if not product:
                continue
            pack_base, pack_unit = base_quantity(
                Decimal(str(product.get("pack_quantity") or 0)), product.get("pack_unit")
            )
            if not pack_base or pack_base <= 0 or pack_unit != unit_base:
                # Пачку в других единицах модель честно не считает — такая
                # позиция остаётся «личной» стоимостью кандидата.
                continue
            product_id = int(product["id"])
            units_of_product[product_id] = unit_base
            need_units = int(math.ceil(needed_base))
            needs.setdefault(recipe_id, {})
            needs[recipe_id][product_id] = needs[recipe_id].get(product_id, 0) + need_units
            if remaining > 0:
                buy_by_product.setdefault(recipe_id, {})
                buy_by_product[recipe_id][product_id] = (
                    buy_by_product[recipe_id].get(product_id, Decimal("0")) + remaining
                )
            recipes_of_product.setdefault(product_id, set()).add(recipe_id)
            products[product_id] = product
            labels.setdefault(product_id, canonical or name)
            max_need_of_product[product_id] = max(
                max_need_of_product.get(product_id, 0), needs[recipe_id][product_id]
            )

    stock_of_product = _allocate_stock(stock, labels, units_of_product)
    shared = [
        product_id
        for product_id, users in recipes_of_product.items()
        if len(users) >= MIN_SHARING_RECIPES
    ]
    # Лимит §6.3: в модель идут самые дорогие по суммарной потребности, а не
    # первые попавшиеся — экономия от них и наибольшая.
    def _value(product_id: int) -> int:
        product = products[product_id]
        price = int(product.get("effective_price_kop") or 0)
        return max_need_of_product.get(product_id, 0) * price

    if len(shared) > max_products:
        shared = sorted(shared, key=_value, reverse=True)[:max_products]
        model.truncated = True
    shared_set = set(shared)

    for product_id in shared:
        product = products[product_id]
        pack_base, _unit = base_quantity(
            Decimal(str(product.get("pack_quantity") or 0)), product.get("pack_unit")
        )
        pack_units = max(1, int(pack_base))
        upper_need = max_need_of_product.get(product_id, 0) * max(1, slots)
        model.products[product_id] = PackProduct(
            product_id=product_id,
            unit=product.get("pack_unit"),
            pack_base=pack_units,
            price_kop=int(product.get("effective_price_kop") or 0),
            perish=perish_of(product),
            stock_base=stock_of_product.get(product_id, 0),
            max_packs=math.ceil(upper_need / pack_units) + 1,
            label=labels.get(product_id, ""),
        )

    variables = sum(product.max_packs for product in model.products.values())
    if variables > MAX_PACK_VARIABLES:
        keep = sorted(model.products, key=_value, reverse=True)
        total = 0
        kept: dict[int, PackProduct] = {}
        for product_id in keep:
            total += model.products[product_id].max_packs
            if total > MAX_PACK_VARIABLES:
                break
            kept[product_id] = model.products[product_id]
        model.products = kept
        shared_set = set(kept)
        model.truncated = True

    model.needs = {
        recipe_id: {
            product_id: need
            for product_id, need in by_product.items()
            if product_id in shared_set
        }
        for recipe_id, by_product in needs.items()
    }
    # «Личная» цена кандидата (§6.1): всё, чего модель не берёт на себя.
    # Скоринг считал каждый товар по пропорции пачки — эту часть вычитаем и
    # заменяем: общие товары уходят в модель целиком, а одиночные считаются
    # целыми пачками. Пачка гречки покупается целиком, даже если блюду нужно
    # сто граммов, и блюдо должно стоить именно столько.
    private: dict[int, int] = {}
    for recipe_id, cost in costs_by_recipe.items():
        proportional = 0
        whole_packs = 0
        for product_id, buy in buy_by_product.get(recipe_id, {}).items():
            product = products[product_id]
            price = int(product.get("effective_price_kop") or 0)
            pack_base, _unit = base_quantity(
                Decimal(str(product.get("pack_quantity") or 0)), product.get("pack_unit")
            )
            proportional += int(price * (buy / pack_base))
            if product_id not in shared_set:
                whole_packs += math.ceil(buy / pack_base) * price
        private[recipe_id] = max(0, cost - proportional) + whole_packs
    model.private_cost_kop = private
    return model


def pack_hint_by_product(packs: dict[int, int]) -> dict[int, int]:
    """Подсказка списку покупок: сколько пачек товара решил взять солвер."""
    return {product_id: count for product_id, count in packs.items() if count > 0}
