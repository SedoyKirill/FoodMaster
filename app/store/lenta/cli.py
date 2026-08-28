from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace

from .collector import CollectorError, collect_once
from .config import LentaConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect the selected Lenta catalogue")
    parser.add_argument("--once", action="store_true", help="Run one collection (default CLI mode)")
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        help="Category slug ending with its numeric ID, or just the ID; may be repeated",
    )
    parser.add_argument("--max-pages", type=int, help="Maximum pages per category")
    parser.add_argument("--detail-limit", type=int, help="Maximum new product details to enrich")
    parser.add_argument("--no-db", action="store_true", help="Write JSONL only")
    return parser


async def _run(args: argparse.Namespace) -> int:
    config = LentaConfig.from_env()
    updates = {}
    if args.categories:
        updates["categories"] = tuple(item.strip().strip("/") for item in args.categories)
    if args.max_pages is not None:
        updates["max_pages"] = max(1, args.max_pages)
    if args.detail_limit is not None:
        updates["detail_limit"] = max(0, args.detail_limit)
    if args.no_db:
        updates["database_url"] = None
    if updates:
        config = replace(config, **updates)

    try:
        result = await collect_once(config)
    except CollectorError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status == "success" else 2


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
