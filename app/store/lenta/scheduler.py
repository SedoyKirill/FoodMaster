from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .collector import collect_once
from .config import LentaConfig


def next_run(now: datetime, run_at: str) -> datetime:
    try:
        hour_text, minute_text = run_at.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"LENTA_RUN_AT must be HH:MM, got {run_at!r}") from exc
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


async def run_scheduler() -> None:
    config = LentaConfig.from_env()
    timezone = ZoneInfo(config.timezone)

    if config.run_on_start:
        try:
            result = await collect_once(config)
            print(json.dumps({"event": "collection_finished", **result.to_dict()}, ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({"event": "collection_failed", "error": str(exc)}, ensure_ascii=False))

    while True:
        now = datetime.now(timezone)
        target = next_run(now, config.run_at)
        print(
            json.dumps(
                {"event": "next_collection", "at": target.isoformat()},
                ensure_ascii=False,
            )
        )
        await asyncio.sleep(max(1.0, (target - now).total_seconds()))
        try:
            result = await collect_once(config)
            print(json.dumps({"event": "collection_finished", **result.to_dict()}, ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({"event": "collection_failed", "error": str(exc)}, ensure_ascii=False))


def main() -> None:
    asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()

