from __future__ import annotations

import asyncio
import functools
import json
import math
import re
import time
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable, Sequence

from .nutrition import ingredient_kcal, nutrition_from_row


MEAL_LABELS = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}
MEAL_KEYWORDS = {
    "breakfast": ("завтрак", "каша", "омлет", "сырник", "блин", "панкейк", "олад", "вафл", "тост"),
    "lunch": ("суп", "салат", "борщ", "щи", "обед", "рагу"),
    "dinner": ("ужин", "мяс", "рыб", "куриц", "паста", "котлет", "запекан"),
}
UNIT_FACTORS = {
    "kg": ("g", Decimal("1000")),
    "g": ("g", Decimal("1")),
    "l": ("ml", Decimal("1000")),
    "ml": ("ml", Decimal("1")),
    "tablespoon": ("ml", Decimal("15")),
    "teaspoon": ("ml", Decimal("5")),
    "cup": ("ml", Decimal("240")),
    "piece": ("piece", Decimal("1")),
}

# PDF extraction occasionally treats a cover, a chapter heading or the first
# sentence of an introduction as a recipe title.  Such records may still have
# ingredients and steps, so ingredient_count alone is not a sufficient guard
# for the menu planner.
NON_DISH_TITLE_PATTERNS = (
    r"\bрецепт(?:ы|ов|а)?\s+на\s+каждый\s+день\b",
    r"\bсам\s+себе\s+нутрициолог\b",
    r"\b(?:приготовле[нh]ие|обработка)\s+(?:мяса|рыбы|овощей)\b",
    r"\bсладости\s+без\s+сахара\b",
    r"\bдетокс[-\s]?(?:завтраки|обеды|ужины)\b",
    r"\b(?:содержание|оглавление|предисловие|введение)\b",
    r"\b(?:кулинарная|поваренная)\s+книга\b",
    r"\bсборник\s+рецептов\b",
    r"\bвес\s+готового\b",
    r"\bбез\s+распознанного\s+названия\b",
    r"\b(?:and|with|of|the|or|и|с|или|либо|на|для|из|к|по|от|под|над)\s*$",
    r"^serves?\s+\d+\b",
    r"^на\s+заметку\b",
    r"^easy\s+skillet$",
    r"^(?:казалось\s+бы|кто\s+не\s+любит|представьте)\b",
    r"^на\s+\d+\s+порц(?:ии|ий)?\b",
    r"^на\s+мой\s+взгляд\b",
    r"^immediately\b",
    r"^basic\b.*\bno$",
    r"\bnote$",
    r"\bэто\b",
    r"^правильно\b",
    r"\bя\s+(?:готовлю|люблю|часто|обычно|предпочитаю)\b",
    r"\b(?:добавить|выложить|нарезать|обжарить|перемешать|положить|распределить|распределятся|смешать)\b",
    r"^\d\S*\s+(?:ст|ч)\s+л\b",
    r"^с\s+",
    r"^(?:add|bake|cook|heat|heated|mix|place|remove|stir|transfer)\b",
    r"\bдля\s+теста\b",
    r"^время\s+приготовления\b",
    r"\bполучается\b",
    r"^готовим\s+в\b",
    r"^на\s+\d+\s+(?:буханк|штук|формоч)\w*\b",
    r"^цедра\s+\d+\b",
    r"^морковок\s+из\b",
    r"^воскресный\s+обед\b",
    r"^премиум\s+класса\b",
    r"^(?:завтраки|обеды|ужины)\b",
    r"^мясо\s+и\s+рыба\b",
    r"^(?:салаты\s+и\s+закуски|закуски\s+и\s+салаты)\b",
    r"\bрецепт$",
)
GENERIC_TITLES = {
    "завтрак",
    "завтраки",
    "обед",
    "обеды",
    "ужин",
    "ужины",
    "рецепт",
    "рецепты",
    "десерт",
    "десерты",
    "салаты",
    "супы",
    "выпечка",
}
INGREDIENT_INSTRUCTION_WORDS = (
    "вымыть",
    "добавить",
    "довести",
    "замочить",
    "залить",
    "измельчить",
    "нарезать",
    "натереть",
    "обжарить",
    "очистить",
    "перемешать",
    "положить",
    "порвать",
    "посолить",
    "промыть",
    "разрезать",
    "смешать",
    "снять с огня",
    "варить до",
)
PRODUCT_MATCH_STOPWORDS = {
    "без",
    "для",
    "или",
    "как",
    "либо",
    "немного",
    "около",
    "при",
    "свежий",
    "свежая",
    "свежие",
}
FOOD_TOKEN_WORDS = {
    "масло": "масло",
    "масла": "масло",
    "маслу": "масло",
    "маслом": "масло",
    "масле": "масло",
    "мука": "мука",
    "муки": "мука",
    "муку": "мука",
    "мукой": "мука",
    "муке": "мука",
    "сыр": "сыр",
    "сыра": "сыр",
    "сыру": "сыр",
    "сыром": "сыр",
    "сыре": "сыр",
    "сыры": "сыр",
    "сыров": "сыр",
}
FOOD_TOKEN_PREFIXES = {
    "баклаж": "баклажан",
    "говяд": "говядина",
    "говяж": "говядина",
    "греч": "гречка",
    "гриб": "грибы",
    "индей": "индейка",
    "индюш": "индейка",
    "картоф": "картофель",
    "курин": "курица",
    "куриц": "курица",
    "морков": "морковь",
    "помид": "томаты",
    "подсолнеч": "растительное масло",
    "растител": "растительное масло",
    "свек": "свекла",
    "томат": "томаты",
    "шампин": "грибы",
}
LONG_PREP_TITLE_WORDS = ("сыровялен", "ферментирован")
PREPARED_PRODUCT_WORDS = (
    "в маринаде",
    "в соусе",
    "варен",
    "жарен",
    "запечен",
    "коктейл",
    "маринован",
    "напиток",
    "паштет",
    "пицца",
    "салат из",
    "смесь",
    "фри",
    "шашлык",
)
NON_MEAL_TITLE_PREFIXES = ("заправка ", "маринад ", "соус ")
#: типы блюд-компонентов: хлеб, соусы и заготовки — не самостоятельный приём
#: пищи («Тостовый хлеб» не должен быть завтраком)
NON_STANDALONE_DISH_TYPES = frozenset({"bread", "sauce", "preserves"})
DESSERT_TITLE_WORDS = ("десерт", "кекс", "парфе", "печенье", "пирог", "пирож", "торт")
LATIN_WORD_RE = re.compile(r"[a-z]{3,}", re.IGNORECASE)


#: Код блюда, которое не принадлежит ни одной кухне (универсальная выпечка,
#: смузи, детское питание). Проходит любой жёсткий фильтр по кухне: иначе
#: выбор «итальянская» выкинул бы из пула овсянку и омлет.
UNIVERSAL_CUISINE = "universal"


def json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


# N1: разбор имён — самая горячая точка планировщика. Одно сопоставление
# ингредиента перебирает сотни товаров, а каждая генерация меню — тысячи
# ингредиентов, поэтому одни и те же слова разбирались миллионы раз (профиль
# холодной генерации: 14 из 22 секунд в _food_token_key). Функции чистые и
# зависят только от строки, так что ответ считается один раз на слово.
@functools.lru_cache(maxsize=100_000)
def _normal(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", value.casefold()).strip()


@functools.lru_cache(maxsize=100_000)
def _tokens(value: str) -> frozenset[str]:
    # frozenset, а не set: кэш отдаёт один и тот же объект всем вызывающим.
    return frozenset(
        token
        for token in _normal(value).split()
        if len(token) >= 3 and token not in PRODUCT_MATCH_STOPWORDS
    )


@functools.lru_cache(maxsize=100_000)
def _food_token_key(token: str) -> str | None:
    word_key = FOOD_TOKEN_WORDS.get(token)
    if word_key is not None:
        return word_key
    return next(
        (value for prefix, value in FOOD_TOKEN_PREFIXES.items() if token.startswith(prefix)),
        None,
    )


def _tokens_related(left: str, right: str) -> bool:
    if left == right:
        return True
    left_food_key = _food_token_key(left)
    if left_food_key is not None and left_food_key == _food_token_key(right):
        return True
    return len(left) >= 6 and len(right) >= 6 and left[:6] == right[:6]


def _token_index_keys(token: str) -> set[str]:
    keys = {f"exact:{token}"}
    food_key = _food_token_key(token)
    if food_key is not None:
        keys.add(f"food:{food_key}")
    if len(token) >= 6:
        keys.add(f"prefix:{token[:6]}")
    return keys


def is_dish_title(value: Any) -> bool:
    """Return True only for a short title that looks like an actual dish."""
    title = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;—–-_")
    normalized = _normal(title)
    words = normalized.split()
    if not normalized or normalized in GENERIC_TITLES or LATIN_WORD_RE.search(normalized):
        return False
    if len(title) > 90 or len(words) > 13 or "?" in title:
        return False
    if any(re.search(pattern, normalized) for pattern in NON_DISH_TITLE_PATTERNS):
        return False
    if any(marker in normalized for marker in LONG_PREP_TITLE_WORDS):
        return False
    return True


def clean_dish_title(value: Any) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    title = re.sub(r"^рецепт\s*[:—–-]?\s*", "", title, flags=re.IGNORECASE).strip()
    uppercase_lead = re.match(r"^([А-ЯЁ0-9«»()'’\-\s]{5,}?)(?=\s+[А-ЯЁ][а-яё])", title)
    if uppercase_lead:
        title = uppercase_lead.group(1).strip()
    trailing_uppercase = re.search(
        r"(?:^|\s)([А-ЯЁ][А-ЯЁ0-9«»()'’\-]*(?:\s+[А-ЯЁ0-9«»()'’\-]+)+)$",
        title,
    )
    if trailing_uppercase and trailing_uppercase.start(1) > 0:
        title = trailing_uppercase.group(1).strip()
    return title


def _ingredient_is_suspicious(ingredient: dict[str, Any]) -> bool:
    value = str(
        ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""
    )
    normalized = _normal(value)
    if not normalized:
        return True
    if LATIN_WORD_RE.search(normalized):
        return True
    if len(value) > 120 or len(normalized.split()) > 14:
        return True
    if any(marker in normalized for marker in INGREDIENT_INSTRUCTION_WORDS):
        return True
    if "рецепт" in normalized and "состав" in normalized:
        return True
    if re.search(r"\b(?:страница|глава|совет)\b|\bсм\s+рецепт\b|\bна\s+стр\b", normalized):
        return True
    return False


def is_recipe_clean(recipe: dict[str, Any]) -> bool:
    title = recipe.get("title")
    steps = recipe.get("steps", [])
    return (
        is_dish_title(title)
        and is_dish_title(clean_dish_title(title))
        and not any(_ingredient_is_suspicious(item) for item in recipe.get("ingredients", []))
        and not any(
            LATIN_WORD_RE.search(str(step.get("instruction") or step))
            for step in steps
        )
    )


def _recipe_allowed(
    recipe: dict[str, Any], hard_terms: tuple[str, ...], appliances: set[str]
) -> bool:
    if not is_recipe_clean(recipe):
        return False
    if recipe.get("dish_type") in NON_STANDALONE_DISH_TYPES:
        # Хлеб, соусы и заготовки — компоненты, а не приём пищи.
        return False
    normalized_title = _normal(clean_dish_title(recipe.get("title")))
    if normalized_title.startswith(NON_MEAL_TITLE_PREFIXES) or re.search(
        r"\b(?:dressing|marinade|sauce)$", normalized_title
    ):
        return False
    ingredient_text = " ".join(
        str(item.get("normalized_name") or item.get("ingredient_text") or "")
        for item in recipe["ingredients"]
    ).casefold()
    if any(term in ingredient_text for term in hard_terms):
        return False
    # TZ-M8 T1 (дефект P8): фильтр по технике работает всегда. Пустой список
    # больше не значит «разрешено всё» — семье при регистрации выдаётся
    # DEFAULT_APPLIANCES, а рецепт без требований проходит при любом наборе.
    return set(json_list(recipe.get("appliances"))).issubset(appliances)


def _meal_score(recipe: dict[str, Any], meal_type: str) -> int:
    tags = set(json_list(recipe.get("meal_types")))
    title = str(recipe["title"]).casefold()
    score = 0
    if meal_type in tags:
        score += 14
    elif not tags:
        score += 3
    if any(word in title for word in MEAL_KEYWORDS[meal_type]):
        score += 8
    if meal_type == "breakfast" and "dessert" in tags:
        score -= 2
    if meal_type != "breakfast" and "breakfast" in tags:
        score -= 8
    if meal_type != "breakfast" and any(
        word in title for word in MEAL_KEYWORDS["breakfast"]
    ):
        score -= 30
    if meal_type != "breakfast" and any(word in title for word in DESSERT_TITLE_WORDS):
        score -= 12
    score += int(Decimal(str(recipe.get("extraction_confidence") or 0)) * 5)
    if recipe.get("review_status") == "ready":
        score += 6
    return score


def _base_quantity(quantity: Decimal | None, unit: str | None) -> tuple[Decimal | None, str | None]:
    if quantity is None or unit not in UNIT_FACTORS:
        return None, unit
    base_unit, factor = UNIT_FACTORS[unit]
    return quantity * factor, base_unit


def _canonical_tokens(tokens: frozenset[str] | set[str]) -> set[str]:
    return {_food_token_key(token) or token for token in tokens}


def _product_quality_score(
    ingredient_tokens: frozenset[str], product_tokens: frozenset[str], product_normalized: str
) -> int | None:
    """Filter substitutes and rank objective quality signals before price."""
    ingredient_keys = _canonical_tokens(ingredient_tokens)
    product_keys = _canonical_tokens(product_tokens)
    score = 0

    if ingredient_keys == {"мука"}:
        # An unqualified "flour" in a recipe means ordinary wheat baking flour.
        if not any(token.startswith("пшенич") for token in product_tokens):
            return None
        score += 6
        if "высший сорт" in product_normalized:
            score += 2
        if "гост" in product_normalized:
            score += 1
        if "цельнозерн" in product_normalized:
            score -= 2

    if "масло" in ingredient_keys and any(
        token.startswith("сливоч") for token in ingredient_tokens
    ):
        if "масло" not in product_keys or not any(
            token.startswith("сливоч") for token in product_tokens
        ):
            return None
        if any(
            marker in product_normalized
            for marker in (
                "маргарин",
                "маслосодержащ",
                "растительно сливоч",
                "растительный жир",
                "спред",
                "заменител молочного жира",
            )
        ):
            return None
        # For cooking, traditional unsalted butter with 82-82.5% fat is the
        # strongest general-purpose choice. Flavoured butter is a different product.
        if "шоколад" in product_normalized:
            return None
        if re.search(r"\b82(?:\s+5)?\b", product_normalized):
            score += 8
        elif re.search(r"\b72\s+5\b", product_normalized):
            score += 3
        if "традицион" in product_normalized:
            score += 3
        if "высший сорт" in product_normalized:
            score += 2
        if "гост" in product_normalized:
            score += 1
        if "без змж" in product_normalized:
            score += 1
        if "несолен" in product_normalized:
            score += 2
        elif re.search(r"\bсолен", product_normalized):
            score -= 3

    if "масло" in ingredient_keys and any(
        token.startswith(("растител", "подсолнеч")) for token in ingredient_tokens
    ):
        if not any(token.startswith("подсолнеч") for token in product_tokens):
            return None
        if "рафинирован" not in product_normalized or any(
            marker in product_normalized
            for marker in (
                "нерафинирован",
                "с ароматом",
                "с добавлением",
                "смесь",
                "спрей",
            )
        ):
            return None
        score += 5
        if "дезодорирован" in product_normalized:
            score += 2
        if "высший сорт" in product_normalized:
            score += 1

    if "масло" in ingredient_keys and any(
        token.startswith("кунжут") for token in ingredient_tokens
    ):
        if not any(token.startswith("кунжут") for token in product_tokens):
            return None
        if "смесь" in product_normalized:
            return None
        score += 3

    if "сыр" in ingredient_keys:
        product_lead_keys = _canonical_tokens(set(product_normalized.split()[:2]))
        if "сыр" not in product_lead_keys:
            return None
        if any(
            marker in product_normalized
            for marker in ("сырный продукт", "заменител молочного жира", "растительный жир")
        ):
            return None
        if any(token.startswith("сливоч") for token in ingredient_tokens):
            if not (
                any(token.startswith("творож") for token in product_tokens)
                and (
                    any(token.startswith("сливоч") for token in product_tokens)
                    or "cream cheese" in product_normalized
                    or "кремчиз" in product_normalized
                )
            ):
                return None
            if "фетакса" in product_normalized:
                return None
            score += 6
        if "без змж" in product_normalized:
            score += 1

    return score


def _product_unit_price(product: dict[str, Any]) -> Decimal:
    price = Decimal(str(product.get("effective_price_kop") or 10**9))
    pack_quantity, _ = _base_quantity(
        Decimal(str(product.get("pack_quantity") or 0)), product.get("pack_unit")
    )
    if pack_quantity and pack_quantity > 0:
        return price / pack_quantity
    return price


def _product_match(
    ingredient_name: str,
    unit_code: str | None,
    products: list[dict[str, Any]],
    price_tier: str,
    required_quantity: Decimal | None = None,
) -> dict[str, Any] | None:
    ingredient_tokens = _tokens(ingredient_name)
    if not ingredient_tokens:
        return None
    matches: list[tuple[int, int, Decimal, int, int, Decimal, dict[str, Any]]] = []
    for product in products:
        product_unit = product.get("pack_unit")
        normalized_pack_unit = UNIT_FACTORS.get(str(product_unit), (product_unit, Decimal("1")))[0]
        if unit_code:
            if not normalized_pack_unit or unit_code != normalized_pack_unit:
                continue
        product_tokens = _tokens(str(product["name"]))
        exact = len(ingredient_tokens & product_tokens)
        prefix_matches = {
            ingredient_token
            for ingredient_token in ingredient_tokens
            for product_token in product_tokens
            if _tokens_related(ingredient_token, product_token)
        }
        matched_ingredient_tokens = (ingredient_tokens & product_tokens) | prefix_matches
        minimum_matches = 1 if len(ingredient_tokens) == 1 else 2
        if len(matched_ingredient_tokens) < minimum_matches:
            continue
        product_normalized = _normal(str(product["name"]))
        if len(ingredient_tokens) == 1:
            ingredient_token = next(iter(ingredient_tokens))
            product_categories = set(product.get("category_slugs") or ())
            if "gotovaya-eda-42" in product_categories:
                continue
            if not any(
                _tokens_related(ingredient_token, product_token)
                for product_token in product_normalized.split()[:2]
            ):
                continue
            if any(marker in product_normalized for marker in PREPARED_PRODUCT_WORDS):
                continue
            if re.search(rf"\bбез\s+{re.escape(ingredient_token[:5])}\w*", product_normalized):
                continue
        prefix = len(prefix_matches)
        score = exact * 10 + prefix * 4
        if score <= 0:
            continue
        quality_score = _product_quality_score(
            ingredient_tokens, product_tokens, product_normalized
        )
        if quality_score is None:
            continue
        price = int(product.get("effective_price_kop") or 10**9)
        pack_base, _ = _base_quantity(
            Decimal(str(product.get("pack_quantity") or 0)), product.get("pack_unit")
        )
        if required_quantity is not None and required_quantity > 0 and pack_base:
            pack_count = math.ceil(required_quantity / pack_base)
            purchase_cost = pack_count * price
            leftover = pack_count * pack_base - required_quantity
        else:
            purchase_cost = price
            leftover = Decimal("0")
        matches.append(
            (
                score,
                quality_score,
                _product_unit_price(product),
                price,
                purchase_cost,
                leftover,
                product,
            )
        )
    if not matches:
        return None
    best_score = max(item[0] for item in matches)
    close_matches = [item for item in matches if item[0] >= best_score - 4]
    best_quality = max(item[1] for item in close_matches)
    close_matches = [item for item in close_matches if item[1] >= best_quality - 2]
    if required_quantity is not None and required_quantity > 0:
        best_leftover = min(item[5] for item in close_matches)
        waste_tolerance = max(
            required_quantity * Decimal("0.5"),
            Decimal("1") if unit_code == "piece" else Decimal("50"),
        )
        close_matches = [
            item for item in close_matches if item[5] <= best_leftover + waste_tolerance
        ]
        close_matches.sort(
            key=lambda item: (item[4], item[2], item[3], _normal(str(item[6]["name"])))
        )
    else:
        close_matches.sort(
            key=lambda item: (item[2], item[3], _normal(str(item[6]["name"])))
        )
    if price_tier == "premium":
        return close_matches[-1][6]
    if price_tier == "economy":
        return close_matches[0][6]
    return close_matches[len(close_matches) // 2][6]


class ProductMatcher:
    def __init__(self, products: list[dict[str, Any]]) -> None:
        self.products = products
        # Прогрет ли фоном (N1): мемоизация живёт внутри экземпляра, а он
        # пересоздаётся на каждой ревизии каталога.
        self.warmed = False
        self.index: dict[str, set[int]] = defaultdict(set)
        self.cache: dict[
            tuple[str, str | None, str, Decimal | None], dict[str, Any] | None
        ] = {}
        for position, product in enumerate(products):
            for token in _tokens(str(product["name"])):
                for key in _token_index_keys(token):
                    self.index[key].add(position)

    def match(
        self,
        ingredient_name: str,
        unit_code: str | None,
        price_tier: str,
        required_quantity: Decimal | None = None,
    ) -> dict[str, Any] | None:
        cache_key = (_normal(ingredient_name), unit_code, price_tier, required_quantity)
        if cache_key in self.cache:
            return self.cache[cache_key]
        positions: set[int] = set()
        for token in _tokens(ingredient_name):
            for key in _token_index_keys(token):
                positions.update(self.index.get(key, ()))
        candidates = [self.products[position] for position in positions]
        result = _product_match(
            ingredient_name, unit_code, candidates, price_tier, required_quantity
        )
        self.cache[cache_key] = result
        return result


class ProductMatcherCache:
    """Один ``ProductMatcher`` на ревизию каталога (A5/B4).

    Полный скан прайса и построение индекса раньше выполнялись при каждом
    открытии рецепта и при каждой генерации плана. Тяжёлый запрос теперь идёт
    только когда истёк TTL И изменилась отметка истории цен; при попадании в кэш
    сохраняется и внутренняя мемоизация матчера — там основной выигрыш.

    Загрузчики и часы инъектируются, поэтому класс тестируется без БД.
    """

    def __init__(
        self,
        ttl_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = float(ttl_seconds)
        self.clock = clock
        self.loads = 0
        self._matcher: ProductMatcher | None = None
        self._stamp: Any = None
        self._checked_at = 0.0
        self._lock = asyncio.Lock()

    async def get(
        self,
        load_stamp: Callable[[], Awaitable[Any]],
        load_products: Callable[[], Awaitable[list[dict[str, Any]]]],
    ) -> ProductMatcher:
        if self._matcher is not None and self.clock() - self._checked_at < self.ttl:
            return self._matcher
        async with self._lock:
            now = self.clock()
            if self._matcher is not None and now - self._checked_at < self.ttl:
                return self._matcher  # каталог обновил другой запрос, пока мы ждали
            stamp = await load_stamp()
            if self._matcher is not None and stamp == self._stamp:
                self._checked_at = now
                return self._matcher
            self._matcher = ProductMatcher(await load_products())
            self._stamp = stamp
            self._checked_at = now
            self.loads += 1
            return self._matcher

    def invalidate(self) -> None:
        self._matcher = None
        self._stamp = None
        self._checked_at = 0.0


def warm_product_matcher(
    matcher: ProductMatcher,
    recipes: list[dict[str, Any]],
    price_tier: str = "balanced",
) -> int:
    """Прогревает мемоизацию матчера ингредиентами планировщика (N1).

    Матчер помнит ответы по ключу «имя + единица + тариф + количество», поэтому
    прогрев повторяет ровно те вызовы, которые сделает ``score_candidates``.
    Возвращает число прогретых ингредиентов; вызывается в фоне, чтобы первая
    после перезапуска или обновления каталога сборка меню не ждала сопоставлений.
    """
    warmed = 0
    for recipe in recipes:
        for ingredient in recipe.get("ingredients") or ():
            _ingredient_cost_hint(ingredient, matcher, price_tier)
            warmed += 1
    return warmed


DEFAULT_TARGET_KCAL = {"adult": 2000, "child": 1400}
#: Техника, которая есть почти в каждом доме (TZ-M8 §3.3). Выдаётся семье при
#: регистрации и миграцией — тем семьям, у которых техника не заполнена: фильтр
#: по технике работает всегда, и пустой набор иначе отсекал бы почти всё.
DEFAULT_APPLIANCES = ("stove", "oven", "microwave", "fridge_freezer")
#: минимум подходящих по типу приёма кандидатов, после которого неподходящие
#: (fit=0) в слот уже не допускаются
_MIN_SLOT_CANDIDATES = 8


def _ingredient_cost_hint(
    ingredient: dict[str, Any],
    matcher: ProductMatcher,
    price_tier: str,
    needed: Decimal | None = None,
    unit: str | None = None,
) -> int | None:
    """Стоимость ингредиента по каталогу; None — сопоставления нет.

    ``needed``/``unit`` — потребность в базовых единицах уже на семью и за
    вычетом домашних запасов (TZ-M8 T1, дефект P6). Без них считается
    книжное количество — так работает фоновый прогрев матчера.

    Товар ищется по книжному количеству: ключ мемоизации не должен зависеть
    от размера семьи, иначе прогрев (N1) перестаёт попадать в кэш.
    """
    name = str(ingredient.get("normalized_name") or ingredient.get("ingredient_text") or "")
    quantity = ingredient.get("quantity_max") or ingredient.get("quantity_min")
    book_qty, book_unit = _base_quantity(
        Decimal(str(quantity)) if quantity is not None else None,
        ingredient.get("unit_code"),
    )
    product = matcher.match(name, book_unit, price_tier, book_qty)
    if not product:
        return None
    price = int(product.get("effective_price_kop") or 0)
    pack_base, pack_unit = _base_quantity(
        Decimal(str(product.get("pack_quantity") or 0)), product.get("pack_unit")
    )
    required = book_qty if needed is None else needed
    required_unit = book_unit if needed is None else unit
    if required is not None and pack_base and required_unit == pack_unit and pack_base > 0:
        # Пропорция пачки, без потолка в одну штуку: три килограмма картофеля
        # стоят трёх пачек, а не одной. Целые упаковки считает TZ-M8 T7.
        return int(price * (required / pack_base))
    return price


def _ingredient_macros(
    name: str,
    quantity: Decimal | None,
    unit: str | None,
    matcher: ProductMatcher,
    price_tier: str,
) -> tuple[Decimal, Decimal | None, Decimal | None, Decimal | None] | None:
    """КБЖУ ингредиента из карточки сопоставленного товара Ленты.

    Работает только для весовых/объёмных единиц (г/мл после нормализации):
    пересчитать «2 шт» в граммы без справочника нельзя честно.
    """
    if quantity is None:
        return None
    base_qty, base_unit = _base_quantity(Decimal(str(quantity)), unit)
    if base_qty is None or base_unit not in {"g", "ml"}:
        return None
    product = matcher.match(name, base_unit, price_tier, base_qty)
    if not product or product.get("kcal_100") is None:
        return None
    factor = base_qty / Decimal("100")

    def _value(field: str) -> Decimal | None:
        raw = product.get(field)
        return Decimal(str(raw)) * factor if raw is not None else None

    kcal = _value("kcal_100")
    if kcal is None:
        return None
    return kcal, _value("protein_100"), _value("fat_100"), _value("carb_100")


def _meal_nutrition(
    ingredients: list[dict[str, Any]],
    scale: Decimal | None,
    matcher: ProductMatcher,
    price_tier: str,
    synonyms: Any = None,
    normal: Any = None,
    nutrition: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Оценка КБЖУ приёма: таблица ingredient_nutrition → карточки Ленты →
    substring-справочник (фолбэк).

    Помимо цифр возвращает ``kcal_coverage`` — (учтено, всего) по ингредиентам
    с количеством: цифра из 2 ингредиентов из 10 не должна выглядеть полной (N2).
    """
    from .planning import scaling as scaling_mod

    kcal_total = Decimal("0")
    protein = Decimal("0")
    fat = Decimal("0")
    carb = Decimal("0")
    kcal_known = macros_known = False
    counted = countable = 0
    for ingredient in ingredients:
        quantity = scaling_mod.scaled_quantity(ingredient, scale)
        name = str(
            ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""
        )
        # N2: словоформы приводятся к канону — «масла»/«муки» иначе мимо правил.
        lookup_name = name
        if synonyms is not None and normal is not None:
            canonical = synonyms.canonical_name(name, normal)
            if canonical:
                lookup_name = canonical
        unit = ingredient.get("unit_code")
        # 1) справочник Haiku-разметки: и ккал, и Б/Ж/У, включая штучные.
        macros = None
        row = None
        if nutrition:
            row = nutrition.get(lookup_name) or nutrition.get(name.strip().lower())
        if row is not None:
            macros = nutrition_from_row(row, name, quantity, unit)
        # 2) карточка сопоставленного товара Ленты.
        if macros is None:
            macros = _ingredient_macros(name, quantity, unit, matcher, price_tier)
        kcal = macros[0] if macros is not None else None
        # 3) старый substring-справочник — только ккал.
        if kcal is None:
            kcal = ingredient_kcal(lookup_name, quantity, unit)
        if not ingredient.get("is_to_taste"):
            countable += 1
            if kcal is not None:
                counted += 1
        if kcal is not None:
            kcal_total += kcal
            kcal_known = True
        if macros is not None:
            _, p, f, c = macros
            if p is not None or f is not None or c is not None:
                protein += p or Decimal("0")
                fat += f or Decimal("0")
                carb += c or Decimal("0")
                macros_known = True
    return {
        "estimated_kcal": int(kcal_total) if kcal_known else None,
        "estimated_protein": int(protein) if macros_known else None,
        "estimated_fat": int(fat) if macros_known else None,
        "estimated_carb": int(carb) if macros_known else None,
        "kcal_coverage": (counted, countable),
    }


#: человеческий текст к упрощениям модели упаковок (§6.5)
PACK_WARNING_TEXTS = {
    "pack_model_truncated": (
        "pack_model_truncated: продуктов в плане слишком много — целыми "
        "пачками посчитаны только самые дорогие из общих."
    ),
    "pack_model_skipped": (
        "pack_model_skipped: расчёт целых упаковок не уложился в лимит "
        "времени — стоимость блюд посчитана по пропорции пачки."
    ),
}


def _shared_packs(
    assignment: dict[tuple[int, str], int | None],
    positions: dict[tuple[int, str], int],
    pack_model: Any,
    packs: dict[int, int],
) -> dict[tuple[int, str], tuple[str, int]]:
    """Слот → (продукт, номер блюда), с которым он делит одну пачку.

    Считается только по товарам, которых куплено меньше, чем понадобилось бы
    блюдам по отдельности: иначе «делим пачку» превратилось бы в «оба блюда
    содержат соль».
    """
    users: dict[int, list[tuple[int, str]]] = {}
    for slot, recipe_id in assignment.items():
        if recipe_id is None:
            continue
        for product_id in pack_model.needs.get(recipe_id, {}):
            users.setdefault(product_id, []).append(slot)
    result: dict[tuple[int, str], tuple[str, int]] = {}
    for product_id, slots in users.items():
        product = pack_model.products.get(product_id)
        if product is None or len(slots) < 2:
            continue
        bought = packs.get(product_id)
        alone = sum(
            math.ceil(pack_model.need(assignment[slot], product_id) / product.pack_base)
            for slot in slots
        )
        if bought is None or bought >= alone:
            continue
        ordered = sorted(slots, key=lambda slot: positions.get(slot, 0))
        for index, slot in enumerate(ordered):
            if slot in result:
                continue
            partner = ordered[(index + 1) % len(ordered)]
            result[slot] = (product.label, positions.get(partner, 0))
    return result


def _pack_warnings(codes: list[str]) -> list[str]:
    return [PACK_WARNING_TEXTS[code] for code in codes if code in PACK_WARNING_TEXTS]


def _leftover_cost_share(servings_by_meal: dict[str, Decimal]) -> float:
    """Во сколько раз дороже ужин, который готовится и на завтрашний обед.

    Не всегда вдвое: если один из взрослых обедает на работе, лишних порций
    нужно меньше (§3.1). Ноль порций ужина — деление не нужно, доли нет.
    """
    dinner = servings_by_meal.get("dinner") or Decimal("0")
    lunch = servings_by_meal.get("lunch") or Decimal("0")
    if dinner <= 0:
        return 1.0
    return float(lunch / dinner)


def _make_macros_hint(nutrition: dict[str, dict[str, Any]] | None):
    """КБЖУ ингредиента для скоринга: таблица Haiku → substring-справочник.

    Возвращает четвёрку (ккал, Б, Ж, У): белок кандидата входит в целевую
    функцию (TZ-M8 §6.2), поэтому одних калорий солверу больше не хватает.
    Substring-справочник знает только калории — БЖУ там честно None.
    """

    def _hint(
        name: str, canonical: str, quantity: Decimal | None, unit: str | None
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
        if nutrition:
            row = nutrition.get(canonical) or nutrition.get(name.strip().lower())
            if row is not None:
                result = nutrition_from_row(row, name, quantity, unit)
                if result is not None:
                    return result
        return ingredient_kcal(canonical or name, quantity, unit), None, None, None

    return _hint


def build_plan(
    *,
    household_id: str,
    starts_on: date,
    days: int,
    cuisines: list[str],
    people: list[dict[str, Any]],
    appliances: list[str],
    rules: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    recipes: list[dict[str, Any]],
    products: list[dict[str, Any]],
    price_tier: str | None = None,
    product_matcher: ProductMatcher | None = None,
    budget_kop: int | None = None,
    synonyms: Any = None,
    ratings: dict[int, int] | None = None,
    nutrition: dict[str, dict[str, Any]] | None = None,
    meals: list[str] | None = None,
    cuisine_mode: str = "only",
    history: list[dict[str, Any]] | None = None,
    plan_profile: dict[str, Any] | None = None,
    taste_events: list[dict[str, Any]] | None = None,
    mode: str | None = None,
    weights: Any = None,
) -> dict[str, Any]:
    """Фасад TZ-M5R: кандидаты → оценка → оптимизация → масштабирование → покупки.

    ``meals`` — какие приёмы планировать вообще (профиль семьи, TZ-M8 §3.4):
    семья, которая завтракает по дороге, не должна получать завтраки.
    ``cuisine_mode`` — ``only`` (жёсткий фильтр, по умолчанию) или ``prefer``
    (мягкое предпочтение: кухня даёт бонус, но не отсекает кандидатов).
    ``history`` — блюда семьи за три недели (ротация, TZ-M8 §3.7),
    ``plan_profile`` — лимиты времени и прочие настройки семьи (§3.4).
    ``mode`` — режим планирования (§6.4): он задаёт веса целевой функции и,
    если ``price_tier`` не передан явно, ценовую стратегию матчера товаров.
    ``weights`` перекрывает веса режима целиком — это нужно калибровке
    (``scripts/calibrate_protein_weight.py``), чтобы собрать один и тот же
    план с разными числами; приложение веса не передаёт.
    """
    from .planning import candidates as candidates_mod
    from .planning import context as context_mod
    from .planning import explain as explain_mod
    from .planning import optimizer as optimizer_mod
    from .planning import profile as profile_mod
    from .planning import scaling as scaling_mod
    from .planning import taste as taste_mod
    from .planning import shopping as shopping_mod
    from .planning import features as features_mod
    from .planning import packs as packs_mod
    from .planning import weights as weights_mod
    from .planning.candidates import (
        MIN_READY_CANDIDATES, Synonyms, hard_rule_terms, ingredient_matches_terms,
        score_candidates,
    )

    if isinstance(synonyms, Synonyms):
        synonyms_dict = synonyms
    else:
        synonyms_dict = Synonyms.from_rows(synonyms or [])

    available_appliances = set(appliances)
    # Режим планирования (§6.4) — единственный источник весов; ценовая
    # стратегия матчера выводится из него, но явный price_tier сильнее.
    plan_mode = mode or (plan_profile or {}).get("mode")
    weights = weights or weights_mod.weights_for(plan_mode)
    price_tier = price_tier or weights_mod.price_tier_for(plan_mode)

    # TZ-M8 §3.1–3.2: слот принадлежит тем, кто ест его дома. Приём, который
    # дома не ест никто, не планируется вовсе; жёсткое правило действует на
    # слот, только если его автор за этим столом.
    planned_meals = set(meals) if meals else set(MEAL_LABELS)
    meal_types = [
        meal
        for meal in MEAL_LABELS
        if meal in planned_meals and profile_mod.slot_servings(people, meal) > 0
    ] or list(MEAL_LABELS)
    slot_terms: dict[str, set[str]] = {}
    slot_diets: dict[str, set[str]] = {}
    for meal in meal_types:
        applicable, diet_tags = profile_mod.rule_terms_for_meal(rules, people, meal)
        slot_terms[meal] = hard_rule_terms(applicable, synonyms_dict, _normal)
        slot_diets[meal] = diet_tags
    # Запрет, действующий во всех слотах, отсекает рецепт из пула целиком —
    # остальные проверяются на своём слоте.
    always_banned = set.intersection(*slot_terms.values()) if slot_terms else set()

    def _blocked(recipe: dict[str, Any], terms: set[str]) -> bool:
        if not terms:
            return False
        for ingredient in recipe.get("ingredients", []):
            name = str(
                ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""
            )
            if ingredient_matches_terms(name, terms, synonyms_dict, _normal):
                return True
        return False

    pool = [
        recipe
        for recipe in recipes
        if _recipe_allowed(recipe, (), available_appliances)
        and not _blocked(recipe, always_banned)
    ]
    # Черновики добираются, только если готовых рецептов мало (TZ §2.1).
    ready_pool = [recipe for recipe in pool if recipe.get("review_status") == "ready"]
    if len(ready_pool) >= MIN_READY_CANDIDATES:
        pool = ready_pool

    if not pool:
        raise ValueError(
            "После применения ограничений осталось слишком мало рецептов "
            "со статусом «готов». Ослабьте ограничения в настройках."
        )

    # Порции считаются по едокам слота: обед на двоих, ужин на троих. Для
    # скоринга берётся полный состав — цена и калории кандидата пока едины на
    # все слоты (уточнение до слота приходит с FeatureVector, TZ-M8 T6).
    servings_by_meal = {
        meal: profile_mod.slot_servings(people, meal) for meal in meal_types
    }
    desired_servings = scaling_mod.desired_servings(people)
    plan_key = f"{household_id}:{starts_on.isoformat()}:{','.join(sorted(cuisines))}"
    product_matcher = product_matcher or ProductMatcher(products)

    def _scale_of(recipe: dict[str, Any]) -> Decimal | None:
        """Во сколько раз книжный рецепт разворачивается на эту семью.

        K2/P6: и калории, и стоимость кандидата солвер должен видеть в
        масштабе семьи — иначе дневной коридор ±20 % вырождается в «выбирай
        калорийнее», а цена блюда остаётся «как в книге на две порции».
        """
        scale, _unknown = scaling_mod.recipe_scale(recipe, desired_servings)
        return scale

    scores = score_candidates(
        pool,
        meal_types=meal_types,
        cuisines=cuisines,
        rules=rules,
        inventory=inventory,
        starts_on=starts_on,
        synonyms=synonyms_dict,
        normal=_normal,
        tokens=_tokens,
        cost_hint=lambda ingredient, needed, unit: _ingredient_cost_hint(
            ingredient, product_matcher, price_tier, needed, unit
        ),
        meal_score=_meal_score,
        macros_hint=_make_macros_hint(nutrition),
        base_quantity=_base_quantity,
        scale_of=_scale_of,
    )
    # Вкус семьи (TZ-M8 §4): звёзды — лишь одно из событий, наравне с
    # заменами и отметками «приготовили»/«пропустили». Обобщение на тип
    # блюда, кухню и продукты даёт мнение даже о том, что семья не пробовала.
    taste_metas = taste_mod.build_metas(pool)
    taste_model = taste_mod.TasteModel.fit(taste_events or [], taste_metas, starts_on)
    taste_known = taste_mod.known_recipes(taste_model)

    # Ротация и сезон (TZ-M8 §3.6–3.7).
    plan_history = context_mod.build_history(history or [], starts_on)
    for recipe in pool:
        recipe_id = int(recipe["id"])
        score = scores[recipe_id]
        affinity = taste_model.family_affinity(taste_metas[recipe_id], people)
        score.affinity = affinity
        score.unknown = recipe_id not in taste_known
        score.recency_penalty = plan_history.recency_penalty(recipe_id, affinity)
        score.season_bonus = context_mod.season_share(
            [
                str(ingredient.get("normalized_name") or ingredient.get("ingredient_text") or "")
                for ingredient in recipe.get("ingredients", [])
            ],
            starts_on,
        )
        if recipe.get("time_total_minutes") is not None:
            score.time_minutes = int(recipe["time_total_minutes"])

    features_mod.attach_bases(scores, pool, synonyms_dict, _normal)
    cuisine_set = set(cuisines)
    matches_cuisine = {
        int(recipe["id"]): cuisine_matches(recipe, cuisine_set) for recipe in pool
    }
    needed_distinct = _min_distinct_for_horizon(days)
    candidates_by_slot: dict[tuple[int, str], list[int]] = {}
    slot_time_limits: dict[tuple[int, str], int | None] = {}
    slot_warnings: list[str] = []
    profile = plan_profile or {}
    cooking_minutes = {
        int(recipe["id"]): (
            int(recipe["time_total_minutes"])
            if recipe.get("time_total_minutes") is not None
            else None
        )
        for recipe in pool
    }
    for meal_type in meal_types:
        # Личные запреты тех, кто за этим столом (TZ-M8 §3.2): блюдо с орехами
        # уходит из обеда ребёнка, но остаётся во взрослом завтраке.
        personal = slot_terms[meal_type] - always_banned
        eligible = [
            recipe for recipe in pool if not _blocked(recipe, personal)
        ]
        required_diets = slot_diets[meal_type]
        if required_diets:
            fitting = [
                recipe
                for recipe in eligible
                if required_diets.issubset(
                    {str(tag) for tag in json_list(recipe.get("diet_tags"))}
                )
            ]
            if fitting:
                eligible = fitting
            else:
                # Молча кормить вегетарианца мясом нельзя — но и оставлять его
                # без обеда тоже: слот заполняется и честно помечается.
                slot_warnings.append(
                    f"diet_conflict: {MEAL_LABELS[meal_type].lower()} — в библиотеке нет "
                    f"блюд с требуемой диетой ({', '.join(sorted(required_diets))})."
                )
        ranked = sorted(
            (int(recipe["id"]) for recipe in eligible),
            key=lambda recipe_id: (
                scores[recipe_id].meal_fit.get(meal_type, 0.0) <= 0,
                optimizer_mod.slot_coefficient(scores[recipe_id], meal_type, weights),
                optimizer_mod.stable_tiebreak(recipe_id, f"{plan_key}:{meal_type}"),
            ),
        )
        # Иерархия слота: сначала блюда, помеченные этим приёмом пищи; если
        # их мало — добавляются непомеченные (fit 0.5); напитки и десерты
        # (fit 0) допустимы только при тотальной нехватке. Иначе дешёвый
        # непомеченный хлеб вытесняет настоящие обеды, а напитки — завтраки.
        exact = [
            recipe_id
            for recipe_id in ranked
            if scores[recipe_id].meal_fit.get(meal_type, 0.0) >= 1.0
        ]
        partial = [
            recipe_id
            for recipe_id in ranked
            if scores[recipe_id].meal_fit.get(meal_type, 0.0) > 0
        ]
        if len(exact) >= _MIN_SLOT_CANDIDATES:
            slot_list = exact
        elif len(partial) >= _MIN_SLOT_CANDIDATES:
            slot_list = partial
        else:
            slot_list = ranked
        # Выбранная кухня — фильтр, а не пожелание: пока блюд этой кухни хватает
        # на горизонт, солвер других и не видит. Если не хватает (в библиотеке
        # четыре азиатских завтрака на все дни), слот дополняется остальными —
        # блюдо не своей кухни получит пометку cuisine_fallback.
        if cuisine_set and cuisine_mode != "prefer":
            preferred = [
                recipe_id for recipe_id in slot_list if matches_cuisine.get(recipe_id)
            ]
            if len(preferred) >= needed_distinct:
                slot_list = preferred
            elif preferred:
                rest = [
                    recipe_id for recipe_id in slot_list if recipe_id not in set(preferred)
                ]
                slot_list = preferred + rest
        # Время готовки — свойство дня, а не слота: в среду вечером двух часов
        # нет, в субботу есть (TZ-M8 §3.5). Рецепт без указанного времени не
        # отсеивается — он получит мягкий штраф в целевой функции (T6).
        for day in range(days):
            context = context_mod.day_context(starts_on + timedelta(days=day))
            limit = context_mod.slot_time_limit(meal_type, context, profile)
            slot_time_limits[day, meal_type] = limit
            candidates_by_slot[day, meal_type] = _fit_time_limit(
                slot_list, limit, cooking_minutes, slot_warnings, context, meal_type
            )

    # Цели слота — суммы норм тех, кто ест этот приём дома (§6.2). Норма
    # считается на дату слота, а не на первый день плана: у ребёнка внутри
    # двухнедельного горизонта может смениться возрастная группа.
    slot_targets = {
        (day, meal_type): optimizer_mod.SlotTarget(
            kcal=profile_mod.slot_kcal_target(
                people, meal_type, starts_on + timedelta(days=day)
            ),
            protein_g=profile_mod.slot_protein_target(
                people, meal_type, starts_on + timedelta(days=day)
            ),
        )
        for day in range(days)
        for meal_type in meal_types
    }
    # Целые пачки на весь горизонт (§6.3): продукт, который нужен двум
    # блюдам, покупается один раз, и солвер об этом знает ещё до выбора.
    candidate_ids = sorted({
        recipe_id for slot_list in candidates_by_slot.values() for recipe_id in slot_list
    })
    pack_model = packs_mod.build_pack_model(
        recipe_ids=candidate_ids,
        recipes_by_id={int(recipe["id"]): recipe for recipe in pool},
        costs_by_recipe={recipe_id: scores[recipe_id].cost_kop for recipe_id in candidate_ids},
        slots=len(candidates_by_slot),
        scale_of=_scale_of,
        matcher=product_matcher,
        price_tier=price_tier,
        stock=candidates_mod._stock_lots(
            inventory, synonyms_dict, _normal, _base_quantity
        ),
        synonyms=synonyms_dict,
        normal=_normal,
        base_quantity=_base_quantity,
    )
    for recipe_id, private_cost in pack_model.private_cost_kop.items():
        scores[recipe_id].cost_private_kop = private_cost

    solution = optimizer_mod.optimize(
        days=days,
        meal_types=meal_types,
        candidates_by_slot=candidates_by_slot,
        scores=scores,
        budget_kop=budget_kop,
        slot_targets=slot_targets,
        weights=weights,
        time_limits=slot_time_limits,
        plan_key=plan_key,
        max_repeats=int(profile.get("max_repeats_per_horizon") or optimizer_mod.MAX_USES_PER_HORIZON),
        allow_leftovers=bool(profile.get("allow_leftovers", False)),
        novelty=profile.get("novelty"),
        cuisine_mode=cuisine_mode,
        leftover_cost_share=_leftover_cost_share(servings_by_meal),
        packs=pack_model,
        recently_eaten={
            recipe_id
            for recipe_id in candidate_ids
            if (plan_history.days_since(recipe_id) or 999)
            <= optimizer_mod.ROTATION_WINDOW_DAYS
            and scores[recipe_id].affinity < context_mod.FAVOURITE_AFFINITY
        },
    )
    assignment = solution.assignment
    solver_status = solution.status
    # Слот-источник знает, что готовит на два раза, ещё до сборки блюд:
    # от этого зависят и порции, и список покупок.
    leftover_sources = {source: target for target, source in solution.leftovers.items()}

    recipes_by_id = {int(recipe["id"]): recipe for recipe in pool}
    # Данные для «почему это блюдо» (TZ-M8 §5): считаются один раз на план,
    # а применяются только к выбранным блюдам — их три десятка, не пятьсот.
    expiring_names = candidates_mod._expiring_canonicals(
        inventory, starts_on, synonyms_dict, _normal
    )
    stock_names = {
        synonyms_dict.canonical_name(str(lot.get("name") or ""), _normal)
        for lot in inventory
    }
    stock_names.discard("")
    seasonal_names = context_mod.SEASONAL_INGREDIENTS[starts_on.month]
    median_cost_by_slot: dict[tuple[int, str], int] = {}
    for slot, slot_candidates in candidates_by_slot.items():
        costs = sorted(scores[recipe_id].cost_kop for recipe_id in slot_candidates)
        if costs:
            median_cost_by_slot[slot] = costs[len(costs) // 2]
    plan_warnings: list[str] = []
    meals: list[dict[str, Any]] = []
    meal_ingredients: list[dict[str, Any]] = []
    # Номера блюд известны до сборки: причина «одна пачка с обедом в среду»
    # может ссылаться на блюдо, которого в списке ещё нет.
    meal_position_by_slot = {
        slot: position
        for position, slot in enumerate(
            (
                (day_index, meal_type)
                for day_index in range(days)
                for meal_type in meal_types
                if assignment.get((day_index, meal_type)) is not None
            ),
            start=1,
        )
    }
    # Кто с кем делит пачку (§6.3): продукт, нужный двум выбранным блюдам,
    # покупается один раз — и это стоит сказать вслух.
    shared_pack_by_slot = _shared_packs(
        assignment, meal_position_by_slot, pack_model, solution.packs
    )
    for day_index in range(days):
        meal_date = starts_on + timedelta(days=day_index)
        for meal_type in meal_types:
            recipe_id = assignment.get((day_index, meal_type))
            if recipe_id is None:
                plan_warnings.append(
                    f"not_enough_recipes: {meal_date.isoformat()} {meal_type} — "
                    "не хватило подходящих рецептов, слот пуст."
                )
                continue
            selected = recipes_by_id[recipe_id]
            score = scores[recipe_id]
            slot_servings = servings_by_meal.get(meal_type, desired_servings)
            # «На два раза» (§6.2): ужин-источник готовится и на завтрашний
            # обед, поэтому порций у него столько, сколько едоков за обоими
            # столами — не механическое ×2, а те же правила eats_meals.
            heir_slot = leftover_sources.get((day_index, meal_type))
            if heir_slot is not None:
                slot_servings += servings_by_meal.get(heir_slot[1], desired_servings)
            source_slot = solution.leftovers.get((day_index, meal_type))
            scale, scale_unknown = scaling_mod.recipe_scale(selected, slot_servings)
            meal_warnings: list[str] = []
            if scale_unknown:
                meal_warnings.append("scale_unknown")
            if score.draft:
                meal_warnings.append("draft")
            if cuisine_set and not cuisine_matches(selected, cuisine_set):
                meal_warnings.append("cuisine_fallback")
            # Обед из вчерашнего ужина уже куплен и приготовлен: его
            # ингредиенты в список покупок не попадают второй раз.
            for ingredient in ([] if source_slot else selected.get("ingredients", [])):
                meal_ingredients.append(
                    {
                        "name": str(
                            ingredient.get("normalized_name")
                            or ingredient.get("ingredient_text")
                            or "Продукт"
                        ),
                        "quantity": scaling_mod.scaled_quantity(ingredient, scale),
                        "unit_code": ingredient.get("unit_code"),
                        "is_to_taste": bool(ingredient.get("is_to_taste")),
                    }
                )
            ingredient_names = [
                synonyms_dict.canonical_name(
                    str(ingredient.get("normalized_name")
                        or ingredient.get("ingredient_text") or ""),
                    _normal,
                )
                for ingredient in selected.get("ingredients", [])
            ]
            if source_slot is not None:
                reasons = [{
                    "code": "leftover",
                    "source_meal": meal_position_by_slot.get(source_slot),
                }]
            else:
                reasons = explain_mod.explain(
                    score,
                    explain_mod.ExplainContext(
                        meal_type=meal_type,
                        weights=weights,
                        median_cost_kop=median_cost_by_slot.get((day_index, meal_type)),
                        expiring_names=tuple(
                            name for name in ingredient_names if name in expiring_names
                        )[:3],
                        stock_names=tuple(
                            name for name in ingredient_names
                            if name in stock_names and name not in expiring_names
                        )[:3],
                        seasonal_names=tuple(
                            name for name in ingredient_names
                            if name in seasonal_names
                            or any(word in seasonal_names for word in name.split())
                        )[:3],
                        days_since=plan_history.days_since(recipe_id),
                        rating=(ratings or {}).get(recipe_id),
                        known=bool(
                            (ratings or {}).get(recipe_id)
                            or plan_history.days_since(recipe_id) is not None
                        ),
                        kcal_target=profile_mod.slot_kcal_target(
                            people, meal_type, meal_date
                        ),
                        dish_type=score.dish_type,
                        shared_pack=shared_pack_by_slot.get((day_index, meal_type)),
                    ),
                )
            meal_nutrition = _meal_nutrition(
                selected.get("ingredients", []), scale, product_matcher, price_tier,
                synonyms=synonyms_dict, normal=_normal, nutrition=nutrition,
            )
            counted, countable = meal_nutrition.pop("kcal_coverage", (0, 0))
            if countable and counted < countable:
                # N2: честная пометка неполноты — ккал посчитаны не по всем
                # ингредиентам блюда.
                meal_warnings.append(f"kcal_partial:{counted}/{countable}")
            meals.append(
                {
                    "meal_date": meal_date,
                    "meal_type": meal_type,
                    "recipe_id": recipe_id,
                    "title": clean_dish_title(selected["title"]),
                    "source_page_start": selected.get("source_page_start"),
                    "cuisine_code": selected.get("cuisine_code"),
                    "review_status": selected.get("review_status"),
                    "draft": score.draft,
                    "scale": scale if scale is not None else Decimal("1"),
                    "scale_unknown": scale_unknown,
                    "servings": slot_servings,
                    "warnings": meal_warnings,
                    "reasons": reasons,
                    # Позиция блюда-источника в этом же плане: id появятся
                    # только при сохранении (§6.2).
                    "leftover_of": meal_position_by_slot.get(source_slot)
                    if source_slot
                    else None,
                    "cooks_ahead": heir_slot is not None,
                    **meal_nutrition,
                }
            )

    aggregate = shopping_mod.aggregate_ingredients(
        meal_ingredients, synonyms_dict, _normal, _base_quantity
    )
    inventory_lots = shopping_mod.prepare_inventory(
        inventory, synonyms_dict, _normal, _base_quantity
    )
    shopping, total_cost, matched_cost_items = shopping_mod.build_shopping(
        aggregate, inventory_lots, product_matcher, price_tier, _base_quantity,
        pack_hint=solution.packs,
    )

    plan_warnings.extend(slot_warnings)
    plan_warnings.extend(_pack_warnings(solution.warnings))
    if solver_status == "greedy":
        # K3: жадный запасной алгоритм не учитывает бюджет и калории —
        # деградация не должна быть тихой.
        plan_warnings.append(
            "solver_fallback: план собран упрощённым алгоритмом (без OR-Tools) — "
            "бюджет и калории при подборе не учитывались."
        )
    if budget_kop is not None and total_cost > budget_kop:
        over_rub = (total_cost - budget_kop) / 100
        plan_warnings.append(f"budget_exceeded: бюджет превышен на {over_rub:.0f} ₽.")
    if any(meal.get("scale_unknown") for meal in meals):
        plan_warnings.append(
            "scale_unknown: у части блюд в книге не указаны порции — количества "
            "даны как в оригинале, без пересчёта на семью."
        )

    return {
        "meals": meals,
        "shopping": shopping,
        "estimated_cost_kop": total_cost,
        "matched_cost_items": matched_cost_items,
        "total_cost_items": sum(
            1
            for item in shopping
            if item["buy_quantity"] is not None and item["buy_quantity"] > 0
        ),
        "desired_servings": desired_servings,
        "price_tier": price_tier,
        "solver_status": solver_status,
        "warnings": plan_warnings
        + [
            "Рецепты импортированы автоматически и пока требуют проверки.",
            "Калорийность и стоимость предварительные: учитываются только распознанные количества и товары, найденные в текущем каталоге Ленты.",
        ],
    }


def meal_entry_for(
    recipe: dict[str, Any],
    meal_date: date,
    meal_type: str,
    desired_servings: Decimal,
    product_matcher: ProductMatcher | None = None,
    price_tier: str = "balanced",
    synonyms: Any = None,
    nutrition_table: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Запись plan_meals для одного рецепта (используется заменой блюда)."""
    from .planning import scaling as scaling_mod
    from .planning.candidates import Synonyms

    scale, scale_unknown = scaling_mod.recipe_scale(recipe, desired_servings)
    warnings: list[str] = []
    if scale_unknown:
        warnings.append("scale_unknown")
    if recipe.get("review_status") != "ready":
        warnings.append("draft")
    if synonyms is not None and not isinstance(synonyms, Synonyms):
        synonyms = Synonyms.from_rows(synonyms)
    nutrition = _meal_nutrition(
        recipe.get("ingredients", []),
        scale,
        product_matcher or ProductMatcher([]),
        price_tier,
        synonyms=synonyms, normal=_normal, nutrition=nutrition_table,
    )
    counted, countable = nutrition.pop("kcal_coverage", (0, 0))
    if countable and counted < countable:
        warnings.append(f"kcal_partial:{counted}/{countable}")
    return {
        "meal_date": meal_date,
        "meal_type": meal_type,
        "recipe_id": int(recipe["id"]),
        "title": clean_dish_title(recipe["title"]),
        "source_page_start": recipe.get("source_page_start"),
        "cuisine_code": recipe.get("cuisine_code"),
        "review_status": recipe.get("review_status"),
        "draft": recipe.get("review_status") != "ready",
        "scale": scale if scale is not None else Decimal("1"),
        "scale_unknown": scale_unknown,
        "servings": desired_servings,
        "warnings": warnings,
        **nutrition,
    }


def _diversify_by_dish_type(recipes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Круговой обход типов блюд с сохранением порядка внутри типа.

    Список замен ранжируется по той же целевой функции, что и план, поэтому
    десятка легко оказывалась пятью блинами: они просто дешевле. Сначала
    лучшее каждого типа, потом вторые по счёту и так далее. Блюда без типа
    остаются каждое само по себе и не сбиваются в одну группу.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for recipe in recipes:
        dish_type = recipe.get("dish_type")
        key = str(dish_type) if dish_type else f"#{recipe['id']}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(recipe)
    result: list[dict[str, Any]] = []
    while len(result) < len(recipes):
        for key in order:
            if groups[key]:
                result.append(groups[key].pop(0))
    return result


def recipe_cuisines(recipe: dict[str, Any]) -> set[str]:
    """Кухни рецепта. Одна колонка-код осталась для совместимости (TZ-M8)."""
    codes = {str(code) for code in json_list(recipe.get("cuisine_codes")) if code}
    if not codes and recipe.get("cuisine_code"):
        codes = {str(recipe["cuisine_code"])}
    return codes or {UNIVERSAL_CUISINE}


def cuisine_matches(recipe: dict[str, Any], selected: set[str]) -> bool:
    """Подходит ли блюдо под выбранные кухни (пустой выбор — подходит всё)."""
    if not selected:
        return True
    codes = recipe_cuisines(recipe)
    return bool(codes & selected) or UNIVERSAL_CUISINE in codes


def _fit_time_limit(
    slot_list: list[int],
    limit: int | None,
    cooking_minutes: dict[int, int | None],
    warnings: list[str],
    context: Any,
    meal_type: str,
) -> list[int]:
    """Кандидаты, укладывающиеся в отведённое на слот время (TZ-M8 §3.5).

    Если после фильтра кандидатов почти не остаётся, лимит удваивается, а
    слот честно помечается: лучше сказать «ужин выйдет долгим», чем оставить
    семью без ужина.
    """
    if not limit:
        return slot_list

    def _fits(recipe_id: int, minutes_limit: int) -> bool:
        minutes = cooking_minutes.get(recipe_id)
        return minutes is None or minutes <= minutes_limit

    fitting = [recipe_id for recipe_id in slot_list if _fits(recipe_id, limit)]
    if len(fitting) >= _MIN_SLOT_CANDIDATES or len(fitting) == len(slot_list):
        return fitting
    relaxed = [recipe_id for recipe_id in slot_list if _fits(recipe_id, limit * 2)]
    if len(relaxed) <= len(fitting):
        # Удвоение ничего не добавило: молчим, если выбор и так есть, и
        # снимаем лимит целиком, только когда слот иначе останется пустым.
        if fitting:
            return fitting
        warnings.append(
            f"time_limit_relaxed: {context.day.isoformat()} "
            f"{MEAL_LABELS[meal_type].lower()} — быстрых блюд нет, "
            f"лимит в {limit} минут снят."
        )
        return slot_list
    warnings.append(
        f"time_limit_relaxed: {context.day.isoformat()} "
        f"{MEAL_LABELS[meal_type].lower()} — блюд в {limit} минут не хватило, "
        f"лимит увеличен до {limit * 2}."
    )
    return relaxed


#: сколько альтернатив каждой группы показывать при замене (TZ-M8 §6.6)
ALTERNATIVE_GROUP_QUOTAS = (("similar", 4), ("other", 4), ("new", 2))


def _alternative_group(
    recipe: dict[str, Any],
    current: dict[str, Any] | None,
    known: bool,
) -> str:
    """«Похожее», «другое» или «новое» — относительно текущего блюда.

    Похожесть сильнее новизны: тому, кто меняет суп, полезнее увидеть другой
    суп, даже если семья его ещё не пробовала. «Новое» остаётся для блюд
    иного типа, о которых семья ничего не знает.
    """
    same_type = bool(
        current is not None
        and recipe.get("dish_type")
        and recipe.get("dish_type") == current.get("dish_type")
    )
    if same_type:
        return "similar"
    return "other" if known else "new"


def _alternative_cards(
    ranked: list[dict[str, Any]],
    *,
    scores: dict[int, Any],
    current: dict[str, Any] | None,
    meal_type: str,
    meal_date: date,
    weights: Any,
    people: list[dict[str, Any]],
    ratings: dict[int, int],
    history: Any,
    limit: int,
) -> list[dict[str, Any]]:
    """Десятка замен по группам: похожие, другие и то, что семья не пробовала.

    Раньше список ранжировался одной целевой функцией и легко оказывался
    пятью блинами подряд — они просто дешевле.
    """
    from .planning import explain as explain_mod
    from .planning import profile as profile_mod

    current_score = scores.get(int(current["id"])) if current else None
    kcal_target = profile_mod.slot_kcal_target(people, meal_type, meal_date)
    costs = sorted(scores[int(recipe["id"])].cost_kop for recipe in ranked)
    median_cost = costs[len(costs) // 2] if costs else None

    by_group: dict[str, list[dict[str, Any]]] = {"similar": [], "other": [], "new": []}
    for recipe in ranked:
        recipe_id = int(recipe["id"])
        score = scores[recipe_id]
        known = bool(ratings.get(recipe_id) or history.days_since(recipe_id) is not None)
        card = {
            "recipe": recipe,
            "group": _alternative_group(recipe, current, known),
            "reason": explain_mod.main_reason(
                score,
                explain_mod.ExplainContext(
                    meal_type=meal_type,
                    weights=weights,
                    median_cost_kop=median_cost,
                    days_since=history.days_since(recipe_id),
                    rating=ratings.get(recipe_id),
                    known=known,
                    kcal_target=kcal_target,
                    dish_type=score.dish_type,
                ),
            ),
            "delta_kcal": (
                score.kcal - current_score.kcal
                if current_score is not None and score.kcal and current_score.kcal
                else None
            ),
            "delta_cost_kop": (
                score.cost_kop - current_score.cost_kop
                if current_score is not None
                else None
            ),
        }
        by_group[card["group"]].append(card)

    cards: list[dict[str, Any]] = []
    for group, quota in ALTERNATIVE_GROUP_QUOTAS:
        cards.extend(by_group[group][:quota])
    if len(cards) < limit:
        # Группы добираются друг из друга: пустая «новое» не должна оставлять
        # пользователя с шестью вариантами вместо десяти.
        chosen = {id(card) for card in cards}
        for recipe_group in by_group.values():
            for card in recipe_group:
                if len(cards) >= limit:
                    break
                if id(card) not in chosen:
                    cards.append(card)
                    chosen.add(id(card))
    return cards[: max(1, limit)]


def _min_distinct_for_horizon(days: int) -> int:
    """Сколько разных блюд нужно слоту на горизонт.

    Одно блюдо разрешено ставить дважды (``MAX_USES_PER_HORIZON``), поэтому на
    три дня хватает двух рецептов. Меньше — слот не заполнить без пустот.
    """
    from .planning.optimizer import MAX_USES_PER_HORIZON

    return max(1, -(-days // MAX_USES_PER_HORIZON))


def slot_alternatives(
    *,
    meal_date: date,
    meal_type: str,
    current_recipe_id: int | None,
    other_meals: list[dict[str, Any]],
    cuisines: list[str],
    people: list[dict[str, Any]],
    appliances: list[str],
    rules: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    recipes: list[dict[str, Any]],
    products: list[dict[str, Any]],
    price_tier: str | None = None,
    product_matcher: ProductMatcher | None = None,
    synonyms: Any = None,
    ratings: dict[int, int] | None = None,
    limit: int = 3,
    nutrition: dict[str, dict[str, Any]] | None = None,
    keep_ids: Sequence[int] = (),
    history: list[dict[str, Any]] | None = None,
    plan_profile: dict[str, Any] | None = None,
    taste_events: list[dict[str, Any]] | None = None,
    with_details: bool = False,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    """Кандидаты на один слот при зафиксированных остальных блюдах (TZ-M5R §3).

    Жёсткие ограничения повторяемости учитывают остальные приёмы плана:
    блюдо не предлагается, если оно уже стоит в этот же или соседний день
    или использовано дважды на горизонте.

    ``with_details`` (TZ-M8 §6.6) возвращает не голые рецепты, а карточки
    замены: группа («похожее», «другое», «новое»), одна главная причина и
    дельты к калориям и стоимости относительно текущего блюда. Десятка из
    одних блинов бесполезна — группы гарантируют, что выбор действительно
    разный.
    """
    from .planning import context as context_mod
    from .planning import optimizer as optimizer_mod
    from .planning import taste as taste_mod
    from .planning import weights as weights_mod
    from .planning.candidates import (
        Synonyms, hard_rule_terms, ingredient_matches_terms, score_candidates,
    )

    synonyms_dict = synonyms if isinstance(synonyms, Synonyms) else Synonyms.from_rows(synonyms or [])
    banned = hard_rule_terms(rules, synonyms_dict, _normal)
    available_appliances = set(appliances)
    # Замены ранжируются той же формулой и теми же весами, что и план:
    # иначе «лучшая альтернатива» была бы лучшей по другому критерию.
    plan_mode = mode or (plan_profile or {}).get("mode")
    weights = weights_mod.weights_for(plan_mode)
    price_tier = price_tier or weights_mod.price_tier_for(plan_mode)

    blocked_ids: set[int] = set()
    use_count: dict[int, int] = {}
    for meal in other_meals:
        other_id = int(meal["recipe_id"])
        use_count[other_id] = use_count.get(other_id, 0) + 1
        other_date = meal["meal_date"]
        if isinstance(other_date, str):
            other_date = date.fromisoformat(other_date)
        if abs((other_date - meal_date).days) <= 1:
            blocked_ids.add(other_id)
    for other_id, count in use_count.items():
        if count >= 2:
            blocked_ids.add(other_id)
    if current_recipe_id is not None:
        blocked_ids.add(int(current_recipe_id))

    def _passes(recipe: dict[str, Any]) -> bool:
        for ingredient in recipe.get("ingredients", []):
            name = str(
                ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""
            )
            if ingredient_matches_terms(name, banned, synonyms_dict, _normal):
                return False
        return True

    pool = [
        recipe
        for recipe in recipes
        if int(recipe["id"]) not in blocked_ids
        and _recipe_allowed(recipe, (), available_appliances)
        and _passes(recipe)
    ]
    if not pool:
        return []
    # Текущее блюдо оценивается вместе с остальными: без него не посчитать,
    # насколько альтернатива дороже или калорийнее.
    current_recipe = next(
        (
            recipe
            for recipe in recipes
            if current_recipe_id is not None and int(recipe["id"]) == int(current_recipe_id)
        ),
        None,
    )
    scored_pool = pool + ([current_recipe] if current_recipe is not None else [])

    product_matcher = product_matcher or ProductMatcher(products)
    from .planning import scaling as scaling_mod

    servings = scaling_mod.desired_servings(people)

    def _scale_of(recipe: dict[str, Any]) -> Decimal | None:
        scale, _unknown = scaling_mod.recipe_scale(recipe, servings)
        return scale

    scores = score_candidates(
        scored_pool,
        meal_types=[meal_type],
        cuisines=cuisines,
        rules=rules,
        inventory=inventory,
        starts_on=meal_date,
        synonyms=synonyms_dict,
        normal=_normal,
        tokens=_tokens,
        cost_hint=lambda ingredient, needed, unit: _ingredient_cost_hint(
            ingredient, product_matcher, price_tier, needed, unit
        ),
        meal_score=_meal_score,
        macros_hint=_make_macros_hint(nutrition),
        base_quantity=_base_quantity,
        scale_of=_scale_of,
    )
    # Вкус семьи — и в заменах: альтернативы ранжируются той же формулой,
    # что и план (TZ-M8 §4, §6.6).
    taste_metas = taste_mod.build_metas(scored_pool)
    taste_model = taste_mod.TasteModel.fit(taste_events or [], taste_metas, meal_date)
    taste_known = taste_mod.known_recipes(taste_model)
    for recipe in scored_pool:
        recipe_id = int(recipe["id"])
        scores[recipe_id].affinity = taste_model.family_affinity(
            taste_metas[recipe_id], people
        )
        scores[recipe_id].unknown = recipe_id not in taste_known
    ranked = sorted(
        pool,
        key=lambda recipe: (
            scores[int(recipe["id"])].meal_fit.get(meal_type, 0.0) <= 0,
            optimizer_mod.slot_coefficient(
                scores[int(recipe["id"])], meal_type, weights
            ),
            optimizer_mod.stable_tiebreak(int(recipe["id"]), f"replace:{meal_type}"),
        ),
    )
    threshold = min(_MIN_SLOT_CANDIDATES, max(1, limit))
    exact = [
        recipe
        for recipe in ranked
        if scores[int(recipe["id"])].meal_fit.get(meal_type, 0.0) >= 1.0
    ]
    partial = [
        recipe
        for recipe in ranked
        if scores[int(recipe["id"])].meal_fit.get(meal_type, 0.0) > 0
    ]
    if len(exact) >= threshold:
        ranked = exact
    elif len(partial) >= threshold:
        ranked = partial
    # keep_ids: блюдо, выбранное человеком вручную, ранжирующий фильтр
    # отсекать не должен. «Поставить в четверг на ужин» — это решение, а не
    # подсказка; жёсткие ограничения (техника, аллергии, повторы) уже
    # применены к пулу выше, а разметка meal_types есть не у всех рецептов.
    if keep_ids:
        present = {int(recipe["id"]) for recipe in ranked}
        wanted = {int(value) for value in keep_ids}
        ranked = ranked + [
            recipe for recipe in pool
            if int(recipe["id"]) in wanted and int(recipe["id"]) not in present
        ]
    # Блюда выбранной кухни идут первыми; остальные добираются, только если
    # своих не хватило на весь список.
    cuisine_set = set(cuisines)
    if cuisine_set:
        preferred = [recipe for recipe in ranked if cuisine_matches(recipe, cuisine_set)]
        if preferred:
            preferred_ids = {int(recipe["id"]) for recipe in preferred}
            rest = [recipe for recipe in ranked if int(recipe["id"]) not in preferred_ids]
            ranked = _diversify_by_dish_type(preferred) + _diversify_by_dish_type(rest)
        else:
            ranked = _diversify_by_dish_type(ranked)
    else:
        ranked = _diversify_by_dish_type(ranked)
    if not with_details:
        return ranked[: max(1, limit)]
    return _alternative_cards(
        ranked,
        scores=scores,
        current=current_recipe,
        meal_type=meal_type,
        meal_date=meal_date,
        weights=weights,
        people=people,
        ratings=ratings or {},
        history=context_mod.build_history(history or [], meal_date),
        limit=limit,
    )
