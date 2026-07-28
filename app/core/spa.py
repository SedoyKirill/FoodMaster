"""The M1 <-> M6 seam for serving the built SPA.

In M1 `web/dist` does not exist, so this serves the placeholder page the
specification asks for. In M6 the Dockerfile's node stage produces `web/dist`
and the other branch takes over — `main.py` and every route file stay untouched.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

_PLACEHOLDER = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Рацион — API</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: system-ui, sans-serif; margin: 0; display: grid;
           place-items: center; min-height: 100vh; }
    main { max-width: 34rem; padding: 2rem; }
    h1 { margin: 0 0 .25rem; font-size: 1.6rem; }
    p  { color: #6b7280; margin: 0 0 1.5rem; }
    ul { list-style: none; padding: 0; display: grid; gap: .5rem; }
    a  { display: block; padding: .75rem 1rem; border: 1px solid currentColor;
         border-radius: .5rem; text-decoration: none; }
    code { opacity: .7; font-size: .85em; }
  </style>
</head>
<body>
  <main>
    <h1>Рацион</h1>
    <p>Веб-интерфейс появится в модуле M6. Пока доступно:</p>
    <ul>
      <li><a href="/docs">Swagger UI <code>/docs</code></a></li>
      <li><a href="/openapi.json">Схема OpenAPI <code>/openapi.json</code></a></li>
      <li><a href="/health">Состояние системы <code>/health</code></a></li>
    </ul>
  </main>
</body>
</html>
"""


def mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """Serve the built SPA, or a placeholder when it has not been built.

    MUST be the last thing ``create_app()`` does: the catch-all route registered
    here matches every path that earlier routes did not.
    """
    index = dist_dir / "index.html"

    if not index.is_file():

        @app.get("/", include_in_schema=False, response_class=HTMLResponse)
        async def placeholder() -> str:
            return _PLACEHOLDER

        return

    # Derived once so the reserved list cannot drift from the real routes.
    reserved = tuple(
        path.lstrip("/")
        for path in ("/api", app.docs_url, app.redoc_url, app.openapi_url, "/health")
        if path
    )
    root = dist_dir.resolve()

    if (dist_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist_dir / "assets"), name="assets")

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa_fallback(spa_path: str) -> FileResponse:
        # Without this, /api/v1/typo would return index.html with HTTP 200 and
        # the frontend would report a JSON parse error instead of a 404.
        if spa_path.startswith(reserved):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (dist_dir / spa_path).resolve()
        if spa_path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
