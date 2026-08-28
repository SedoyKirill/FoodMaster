from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace

from .config import RecipeImportConfig
from .database import RecipeRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import locally owned PDF cookbooks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser(
        "export-windows", help="TZ-M2R этапы 1-3: извлечь страницы и записать окна"
    )
    export.add_argument("--force", action="store_true", help="Переизвлечь страницы заново")
    export.add_argument("--only", help="Только книги, чьё имя файла содержит текст")
    export.add_argument("--max-books", type=int, help="Ограничить число книг")
    export.add_argument("--no-ocr", action="store_true", help="Без OCR")

    subparsers.add_parser("extract-status", help="Прогресс обработки окон сессией Haiku")

    load = subparsers.add_parser(
        "load-extracted", help="TZ-M2R этап 5: загрузить результаты Haiku в БД"
    )
    load.add_argument("--only", help="Только книги, чьё название/файл содержит текст")

    subparsers.add_parser("summary", help="Print database counters")
    return parser


async def _run(args: argparse.Namespace) -> int:
    config = RecipeImportConfig.from_env()

    if args.command == "summary":
        repository = RecipeRepository(config.database_url)
        await repository.connect()
        try:
            print(json.dumps(await repository.library_summary(), ensure_ascii=False, indent=2))
        finally:
            await repository.close()
        return 0

    if args.command == "extract-status":
        from .pipeline import extracted_dir, windows_dir
        from .windows import extraction_status

        status = extraction_status(windows_dir(config), extracted_dir(config))
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    if args.command == "export-windows":
        from .pipeline import run_export_windows

        config = replace(
            config,
            force=bool(args.force),
            ocr_enabled=False if args.no_ocr else config.ocr_enabled,
            file_pattern=args.only,
            max_books=max(1, args.max_books) if args.max_books else None,
        )
        totals = await run_export_windows(config)
        print(json.dumps(totals, ensure_ascii=False, indent=2))
        return 0

    if args.command == "load-extracted":
        from .pipeline import run_load_extracted

        config = replace(config, file_pattern=args.only)
        result = await run_load_extracted(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "success" else 2

    raise SystemExit(f"неизвестная команда: {args.command}")


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
