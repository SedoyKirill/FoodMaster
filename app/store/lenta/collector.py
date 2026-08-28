from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .api import (
    LentaApiClient,
    StoreSelectionError,
    parse_api_product_card,
    parse_api_product_details,
)
from .config import DEFAULT_CATEGORIES, LentaConfig
from .database import LentaRepository
from .models import ProductCard


class CollectorError(RuntimeError):
    pass


def _event(name: str, **fields: Any) -> None:
    print(json.dumps({"event": name, **fields}, ensure_ascii=False), flush=True)


@dataclass(slots=True)
class CollectionResult:
    started_at: str
    finished_at: str | None = None
    status: str = "running"
    pages_seen: int = 0
    products_seen: int = 0
    products_without_pack: int = 0
    details_enriched: int = 0
    products_marked_unavailable: int = 0
    snapshot_path: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JsonlSnapshot:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def __enter__(self) -> "JsonlSnapshot":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8", newline="\n")
        return self

    def write(self, observed_at: datetime, card: ProductCard) -> None:
        if self._file is None:
            raise RuntimeError("Snapshot is not open")
        payload = {"observed_at": observed_at.isoformat(), "product": card.to_dict()}
        self._file.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._file is not None:
            self._file.close()


class LentaApiCollector:
    def __init__(self, config: LentaConfig, repository: LentaRepository | None) -> None:
        self.config = config
        self.repository = repository
        self.client = LentaApiClient(config)

    async def _collect_category(
        self,
        category_slug: str,
        snapshot: JsonlSnapshot,
        unique_products: set[str],
        result: CollectionResult,
    ) -> bool:
        offset = 0
        total: int | None = None
        _event("category_started", category=category_slug)
        for _page_number in range(1, self.config.max_pages + 1):
            items, total = await self.client.catalog_page(category_slug, offset)
            result.pages_seen += 1
            _event(
                "catalog_page",
                category=category_slug,
                offset=offset,
                received=len(items),
                total=total,
            )
            if not items:
                return total <= offset

            observed_at = datetime.now(UTC)
            for payload in items:
                try:
                    card = parse_api_product_card(payload, category_slug)
                except (TypeError, ValueError) as exc:
                    result.errors.append(
                        {
                            "category": category_slug,
                            "product_id": payload.get("id"),
                            "error": str(exc),
                        }
                    )
                    continue

                first_observation = card.external_id not in unique_products
                if first_observation:
                    unique_products.add(card.external_id)
                    if card.pack_quantity is None:
                        result.products_without_pack += 1
                    snapshot.write(observed_at, card)

                # Повтор товара в другой корневой категории сохраняет вторую
                # классификацию, но не создаёт дубль товара или снимка.
                if self.repository is not None:
                    await self.repository.upsert_card(self.config, card, observed_at)

            offset += len(items)
            if offset >= total or len(items) < self.config.page_size:
                return True
            await asyncio.sleep(self.client.suggested_delay(self.config.page_delay_seconds))

        result.errors.append(
            {
                "category": category_slug,
                "error": f"Достигнут LENTA_MAX_PAGES={self.config.max_pages}",
                "offset": offset,
                "total": total,
            }
        )
        return False

    async def _enrich_details(self, result: CollectionResult) -> None:
        if self.repository is None or self.config.detail_limit <= 0:
            return
        candidates = await self.repository.detail_candidate_ids(
            self.config, self.config.detail_limit
        )
        for external_id in candidates:
            try:
                payload = await self.client.product_details(external_id)
                details = parse_api_product_details(payload)
                await self.repository.save_details(external_id, details, datetime.now(UTC))
                result.details_enriched += 1
            except Exception as exc:
                result.errors.append(
                    {"detail_product_id": external_id, "error": str(exc)}
                )
            await asyncio.sleep(self.client.suggested_delay(self.config.page_delay_seconds))
            if result.details_enriched and result.details_enriched % 50 == 0:
                _event("details_progress", enriched=result.details_enriched, total=len(candidates))

    async def collect(self) -> CollectionResult:
        started_at = datetime.now(UTC)
        stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        snapshot_path = self.config.data_dir / "lenta" / f"catalog-{stamp}.jsonl"
        result = CollectionResult(started_at=started_at.isoformat(), snapshot_path=str(snapshot_path))
        run_id: int | None = None
        fatal_error: Exception | None = None
        unique_products: set[str] = set()
        all_categories_complete = True

        try:
            if self.repository is not None:
                await self.repository.connect()
                await self.repository.upsert_store(self.config)
                run_id = await self.repository.begin_run(self.config, started_at)

            await self.client.open()
            _event(
                "store_selected",
                store=self.config.store_id,
                address=f"{self.config.city}, {self.config.store_address}",
                internal_id=self.client.internal_store_id,
            )
            with JsonlSnapshot(snapshot_path) as snapshot:
                failed_categories: list[tuple[str, str]] = []
                for category_slug in self.config.categories:
                    try:
                        complete = await self._collect_category(
                            category_slug, snapshot, unique_products, result
                        )
                        all_categories_complete = all_categories_complete and complete
                    except StoreSelectionError:
                        raise
                    except Exception as exc:
                        failed_categories.append((category_slug, str(exc)))

                # Второй заход по упавшим категориям: к этому моменту темп уже
                # снижен адаптивным троттлом, поэтому шанс пройти выше. Дубли
                # страхуют unique_products и идемпотентный upsert.
                for category_slug, first_error in failed_categories:
                    _event("category_retry", category=category_slug, first_error=first_error)
                    await asyncio.sleep(
                        self.client.suggested_delay(self.config.page_delay_seconds)
                    )
                    try:
                        complete = await self._collect_category(
                            category_slug, snapshot, unique_products, result
                        )
                        all_categories_complete = all_categories_complete and complete
                    except StoreSelectionError:
                        raise
                    except Exception as exc:
                        all_categories_complete = False
                        result.errors.append(
                            {"category": category_slug, "error": str(exc), "attempts": 2}
                        )

            result.products_seen = len(unique_products)

            full_standard_run = self.config.categories == DEFAULT_CATEGORIES
            if (
                self.repository is not None
                and full_standard_run
                and all_categories_complete
                and not result.errors
            ):
                result.products_marked_unavailable = (
                    await self.repository.mark_unseen_unavailable(self.config, started_at)
                )

            await self._enrich_details(result)
            result.status = "partial" if result.errors else "success"
        except Exception as exc:
            fatal_error = exc
            result.errors.append({"fatal": str(exc)})
            result.status = "failed"
        finally:
            result.finished_at = datetime.now(UTC).isoformat()
            if run_id is not None and self.repository is not None:
                await self.repository.finish_run(
                    run_id,
                    status=result.status,
                    finished_at=datetime.now(UTC),
                    pages_seen=result.pages_seen,
                    products_seen=result.products_seen,
                    products_without_pack=result.products_without_pack,
                    details_enriched=result.details_enriched,
                    errors=result.errors,
                )
            if self.repository is not None:
                await self.repository.close()

        if fatal_error is not None:
            raise CollectorError(str(fatal_error)) from fatal_error
        return result


async def collect_once(config: LentaConfig) -> CollectionResult:
    repository = LentaRepository(config.database_url) if config.database_url else None
    return await LentaApiCollector(config, repository).collect()
