from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import LentaConfig
from .models import ProductCard, ProductDetails
from .parsing import KCAL_RE, NUTRITION_RE, parse_pack


PRODUCT_URL = "https://lenta.com/product/{slug}-{product_id}/"
CATEGORY_ID_RE = re.compile(r"(?:^|-)(?P<id>\d+)$")
DISCOUNT_RE = re.compile(r"-(?P<discount>\d{1,3})\s*%")


class LentaApiError(RuntimeError):
    pass


class LentaApiHttpError(LentaApiError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


class StoreSelectionError(LentaApiError):
    pass


def category_id(value: str) -> int:
    match = CATEGORY_ID_RE.search(value.strip().strip("/"))
    if not match:
        raise ValueError(f"В категории нет числового ID: {value!r}")
    return int(match.group("id"))


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _price_unit(payload: dict[str, Any]) -> str | None:
    raw = str(payload.get("units", {}).get("saleUnit", "")).strip().lower()
    return {
        "шт": "piece",
        "кг": "kg",
        "г": "g",
        "л": "l",
        "мл": "ml",
    }.get(raw) or raw or None


def _discount_percent(payload: dict[str, Any], regular: int | None, current: int | None) -> int | None:
    badges = payload.get("badges")
    if isinstance(badges, dict):
        candidates = badges.get("discount", [])
        if isinstance(candidates, list):
            for badge in candidates:
                if not isinstance(badge, dict):
                    continue
                match = DISCOUNT_RE.search(str(badge.get("title", "")))
                if match:
                    return int(match.group("discount"))
    if regular and current is not None and 0 <= current < regular:
        value = (Decimal(regular - current) * 100 / Decimal(regular)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return int(value)
    return None


def _sanitized_payload(payload: dict[str, Any]) -> dict[str, Any]:
    # Пользователь отказался от изображений. Также не сохраняем видео и отзывы:
    # они не нужны планировщику и сильно раздувают снимки.
    return {
        key: value
        for key, value in payload.items()
        if key not in {"images", "videos", "reviews", "certificates"}
    }


def parse_api_product_card(payload: dict[str, Any], category_slug: str) -> ProductCard:
    product_id = str(payload.get("id", "")).strip()
    if not product_id.isdigit():
        raise ValueError("API вернул товар без числового ID")

    display = payload.get("display") if isinstance(payload.get("display"), dict) else {}
    name = str(display.get("name") or payload.get("name") or "").strip()
    if not name:
        raise ValueError(f"API вернул товар {product_id} без названия")
    weight = payload.get("weight") if isinstance(payload.get("weight"), dict) else {}
    pack_text = str(display.get("package") or weight.get("package") or "").strip() or None
    parsed_pack_text, pack_quantity, pack_unit = parse_pack(pack_text or name)
    if pack_text is None:
        pack_text = parsed_pack_text

    prices = payload.get("prices") if isinstance(payload.get("prices"), dict) else {}
    current = _integer(prices.get("cost"))
    if current is None:
        current = _integer(prices.get("price"))
    regular = _integer(prices.get("costRegular"))
    if regular is None:
        regular = _integer(prices.get("priceRegular"))
    if regular is None:
        regular = current

    features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
    loyalty = current if prices.get("isLoyaltyCardPrice") else None
    promo = current if prices.get("isPromoactionPrice") else None
    personal = current if features.get("isPersonalPrice") else None
    if loyalty is None and promo is None and personal is None:
        regular = current if current is not None else regular

    count = _decimal(payload.get("count"))
    available = (count is None or count > 0) and not bool(features.get("isBlockedForSale"))
    sale_limit = payload.get("saleLimit") if isinstance(payload.get("saleLimit"), dict) else {}
    slug = str(payload.get("slug") or "product").strip("/")

    clean_payload = _sanitized_payload(payload)
    return ProductCard(
        external_id=product_id,
        url=PRODUCT_URL.format(slug=slug, product_id=product_id),
        name=name,
        raw_text=json.dumps(clean_payload, ensure_ascii=False, separators=(",", ":")),
        category_slug=category_slug,
        image_url=None,
        pack_text=pack_text,
        pack_quantity=pack_quantity,
        pack_unit=pack_unit,
        price_unit=_price_unit(payload),
        regular_price_kop=regular,
        loyalty_price_kop=loyalty,
        promo_price_kop=promo,
        personal_price_kop=personal,
        discount_percent=_discount_percent(payload, regular, current),
        available_for_order=available,
        purchase_limit=_integer(sale_limit.get("maxSaleQuantity")),
    )


def _attribute_map(payload: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    by_key: dict[str, str] = {}
    characteristics: dict[str, str] = {}
    attributes = payload.get("attributes")
    if not isinstance(attributes, list):
        return by_key, characteristics
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        value = str(attribute.get("value") or "").strip()
        if not value:
            continue
        name = str(attribute.get("name") or "").strip()
        if name:
            characteristics[name] = value
        for key in (attribute.get("alias"), attribute.get("slug")):
            normalized = str(key or "").strip()
            if normalized:
                by_key[normalized] = value
    return by_key, characteristics


def parse_api_product_details(payload: dict[str, Any]) -> ProductDetails:
    by_key, characteristics = _attribute_map(payload)
    nutrition = by_key.get("nutritionalValue") or by_key.get("pishchevaya-cennost") or ""
    nutrition_match = NUTRITION_RE.search(nutrition)
    energy = by_key.get("energeticheskaya-cennost") or characteristics.get(
        "Энергетическая ценность", ""
    )
    kcal_match = KCAL_RE.search(energy)

    shelf_life = None
    for key, value in by_key.items():
        if key in {"srok-godnosti", "srok-hraneniya"} or (
            "srok" in key and ("godnost" in key or "hraneni" in key)
        ):
            shelf_life = value
            break

    display = payload.get("display") if isinstance(payload.get("display"), dict) else {}
    return ProductDetails(
        name=str(display.get("name") or payload.get("name") or "").strip() or None,
        article=by_key.get("vendorId") or by_key.get("artikul"),
        brand=by_key.get("brand"),
        composition=by_key.get("ingredients") or by_key.get("sostav"),
        kcal_100=_decimal(kcal_match.group("kcal")) if kcal_match else None,
        protein_100=_decimal(nutrition_match.group("protein")) if nutrition_match else None,
        fat_100=_decimal(nutrition_match.group("fat")) if nutrition_match else None,
        carb_100=_decimal(nutrition_match.group("carb")) if nutrition_match else None,
        storage_conditions=by_key.get("usloviya-hraneniya"),
        shelf_life_text=shelf_life,
        characteristics=characteristics,
        raw_text=json.dumps(_sanitized_payload(payload), ensure_ascii=False, separators=(",", ":")),
    )


def _address_matches(store: dict[str, Any], config: LentaConfig) -> bool:
    text = " ".join(
        str(store.get(key, ""))
        for key in (
            "city",
            "address",
            "addressFull",
            "addressShort",
            "name",
            "title",
            "fullAddress",
        )
    ).lower()
    normalized = set(re.sub(r"[^а-яё0-9]+", " ", text).split())
    required = {"иваново", "карла", "маркса", "3"}
    configured_number = re.search(r"\d+", config.store_address)
    if configured_number:
        required.discard("3")
        required.add(configured_number.group(0))
    return required.issubset(normalized)


class AdaptiveThrottle:
    """Адаптивный темп запросов: каждый 429 удваивает множитель задержки,
    длинная серия успешных запросов постепенно возвращает его к 1."""

    def __init__(self, *, factor_cap: float = 8.0, recovery_successes: int = 50) -> None:
        self.factor = 1.0
        self._factor_cap = factor_cap
        self._recovery_successes = recovery_successes
        self._streak = 0

    def on_throttled(self) -> None:
        self.factor = min(self._factor_cap, self.factor * 2)
        self._streak = 0

    def on_success(self) -> None:
        self._streak += 1
        if self._streak >= self._recovery_successes and self.factor > 1.0:
            self.factor = max(1.0, self.factor / 2)
            self._streak = 0

    def delay(self, base: float, cap: float) -> float:
        return min(cap, base * self.factor)


class LentaApiClient:
    def __init__(self, config: LentaConfig) -> None:
        self.config = config
        self.device_id = self._load_device_id()
        self.session_token: str | None = None
        self.internal_store_id: int | None = None
        self.throttle = AdaptiveThrottle()

    def suggested_delay(self, base: float) -> float:
        """Пауза между запросами сборщика с учётом полученных 429."""
        return self.throttle.delay(base, self.config.max_delay_seconds)

    def _load_device_id(self) -> str:
        path = self.config.device_id_path
        if path.exists():
            try:
                return str(uuid.UUID(path.read_text(encoding="utf-8").strip()))
            except (ValueError, OSError):
                pass
        value = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return value

    def _headers(self, *, include_session: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "DeviceId": self.device_id,
            "X-Device-ID": self.device_id,
            "X-Device-Brand": "Docker",
            "X-Device-Name": "Ration Collector",
            "X-Device-OS": "Android",
            "X-Device-OS-Version": self.config.android_sdk,
            "X-Delivery-Mode": "pickup",
            "Client": (
                f"android_{self.config.android_release}_{self.config.app_version}_apk"
            ),
            "LocalTime": datetime.now().astimezone().isoformat(),
            "X-Platform": "omniapp",
            "X-Retail-Brand": "lo",
            "X-Real-Retail-Brand": "ldostavka",
            "User-Agent": f"lo, {self.config.app_version}",
        }
        if include_session:
            if not self.session_token:
                raise LentaApiError("Гостевая сессия «Ленты» ещё не создана")
            headers["SessionToken"] = self.session_token
        return headers

    def _request_sync(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        include_session: bool,
    ) -> dict[str, Any]:
        url = f"{self.config.api_base_url}/{path.lstrip('/')}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None
        # 429 стоит целой категории — на него даём больше попыток, чем на 5xx.
        throttled_retries = max(self.config.request_retries, 5)
        for attempt in range(throttled_retries + 1):
            request = Request(
                url,
                data=body,
                method=method,
                headers=self._headers(include_session=include_session),
            )
            try:
                with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                    raw = response.read()
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(decoded, dict):
                    raise LentaApiError(f"API вернул неожиданный JSON для {path}")
                self.throttle.on_success()
                return decoded
            except HTTPError as exc:
                message = exc.read(2_000).decode("utf-8", errors="replace")
                if exc.code == 429:
                    self.throttle.on_throttled()
                allowed = throttled_retries if exc.code == 429 else self.config.request_retries
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= allowed:
                    raise LentaApiHttpError(exc.code, f"HTTP {exc.code} для {path}: {message}") from exc
                last_error = exc
                retry_after = exc.headers.get("Retry-After")
                # 429 без Retry-After ждём дольше обычных 5xx: базовая пауза
                # 2 с растёт экспоненциально и умножается на текущий троттл.
                if retry_after and retry_after.isdigit():
                    delay = float(retry_after)
                elif exc.code == 429:
                    delay = 2.0 * 2**attempt * self.throttle.factor
                else:
                    delay = float(2**attempt)
                time.sleep(min(120.0, max(0.25, delay)))
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= self.config.request_retries:
                    break
                time.sleep(min(10.0, 2**attempt))
        raise LentaApiError(f"Не удалось вызвать {path}: {last_error}") from last_error

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        include_session: bool = True,
        refresh_on_unauthorized: bool = True,
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._request_sync,
                method,
                path,
                payload,
                include_session=include_session,
            )
        except LentaApiHttpError as exc:
            if include_session and refresh_on_unauthorized and exc.status in {401, 403}:
                await self.open()
                return await self._request(
                    method,
                    path,
                    payload,
                    include_session=True,
                    refresh_on_unauthorized=False,
                )
            raise

    async def open(self) -> None:
        guest = await self._request(
            "GET", "auth/session/guest/token", include_session=False, refresh_on_unauthorized=False
        )
        token = str(guest.get("sessionId") or "").strip()
        if not token:
            raise LentaApiError("API не вернул sessionId гостевой сессии")
        self.session_token = token

        store = await self._request(
            "GET", f"stores/{self.config.store_id}", refresh_on_unauthorized=False
        )
        if not _address_matches(store, self.config):
            raise StoreSelectionError(
                f"Магазин {self.config.store_id} не совпал с адресом {self.config.city}, "
                f"{self.config.store_address}: {store.get('address')!r}"
            )
        internal_id = _integer(store.get("id"))
        if internal_id is None:
            raise StoreSelectionError("API не вернул внутренний ID выбранного магазина")
        self.internal_store_id = internal_id

        selected = await self._request(
            "PUT",
            f"stores/pickup/{internal_id}?autodetection=false",
            {},
            refresh_on_unauthorized=False,
        )
        if selected.get("selected") is False or not _address_matches(selected, self.config):
            raise StoreSelectionError("API не подтвердил выбор магазина на Карла Маркса, 3")

    async def catalog_page(self, category: str, offset: int) -> tuple[list[dict[str, Any]], int]:
        payload = await self._request(
            "POST",
            "catalog/items",
            {
                "categoryId": category_id(category),
                "limit": self.config.page_size,
                "offset": offset,
            },
        )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise LentaApiError(f"API не вернул items для категории {category}")
        items = [item for item in raw_items if isinstance(item, dict)]
        total = _integer(payload.get("total"))
        return items, total if total is not None else offset + len(items)

    async def product_details(self, external_id: str) -> dict[str, Any]:
        if not external_id.isdigit():
            raise ValueError(f"Некорректный ID товара: {external_id!r}")
        return await self._request("GET", f"catalog/items/{external_id}")
