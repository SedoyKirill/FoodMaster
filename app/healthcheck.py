"""Container HEALTHCHECK entry point: `python -m app.healthcheck`.

Uses only the standard library so the slim runtime image does not have to carry
curl or wget just to check itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

#: The container always listens on 8000 internally; WEB_PORT only controls
#: which host port that is published on.
INTERNAL_PORT = int(os.environ.get("APP_INTERNAL_PORT", "8000"))


def check_api(url: str, timeout: float) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                print(f"health: HTTP {response.status}", file=sys.stderr)
                return 1
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"health: {exc}", file=sys.stderr)
        return 1

    if body.get("db") != "ok":
        print(f"health: db={body.get('db')}", file=sys.stderr)
        return 1
    return 0


def check_scheduler() -> int:
    """The scheduler serves no HTTP; reaching the database is the liveness bar."""
    import asyncio

    from app.core.db import check_db, dispose_engine

    async def run() -> bool:
        try:
            return await check_db(timeout=3.0)
        finally:
            await dispose_engine()

    return 0 if asyncio.run(run()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Container health probe.")
    parser.add_argument("--role", choices=("api", "scheduler"), default="api")
    parser.add_argument("--timeout", type=float, default=4.0)
    args = parser.parse_args(argv)

    if args.role == "scheduler":
        return check_scheduler()

    return check_api(f"http://127.0.0.1:{INTERNAL_PORT}/health", args.timeout)


if __name__ == "__main__":
    sys.exit(main())
