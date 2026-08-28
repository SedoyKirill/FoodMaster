from __future__ import annotations

import asyncio
import mimetypes
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter, Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .database import (
    PLANNER_WARM_INTERVAL_SECONDS, AppRepository, AuthenticationError, ConflictError,
)
from .payloads import (
    EXPIRED_TEXT, STORAGE_AREAS, UNIT_CODES, UNKNOWN_STORAGE_TEXT,
    UNKNOWN_UNIT_TEXT, AuthPayload, InventoryPayload,
    PasswordPayload, PersonPatchPayload, PersonPayload, PlanPayload,
    PurchasePayload, RatingPayload, ReplaceMealPayload, ReviewPayload,
    RulePayload, SettingsPayload, TelegramLoginPayload,
)
from .ratelimit import RateLimiter


SESSION_COOKIE = "ration_session"
CSRF_COOKIE = "ration_csrf"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# В slim-образе нет /etc/mime.types, поэтому шрифт уходил как text/plain —
# а с X-Content-Type-Options: nosniff браузер такой ответ отвергает.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# CSP держит приложение в рамках собственного источника: скриптов и стилей
# «инлайном» в проекте нет, шрифты и иконки лежат локально в /assets.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'; img-src 'self' data:; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}

router = APIRouter()


def get_repository(request: Request) -> Any:
    """Репозиторий берётся из приложения, а не из глобальной переменной.

    Благодаря этому ``create_app(repository=...)`` подменяет слой данных целиком —
    тесты по TZ-TESTS §4 работают без Postgres.
    """
    return request.app.state.repository


# ``Any`` вместо AppRepository намеренно: FastAPI не валидирует аннотацию
# параметра-зависимости, поэтому подставленный фейковый репозиторий проходит как есть.
Repo = Annotated[Any, Depends(get_repository)]


def set_auth_cookies(response: Response, session_token: str, csrf_token: str) -> None:
    secure = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    response.set_cookie(
        SESSION_COOKIE, session_token, max_age=30 * 24 * 3600,
        httponly=True, secure=secure, samesite="lax", path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf_token, max_age=30 * 24 * 3600,
        httponly=False, secure=secure, samesite="lax", path="/",
    )


async def current_session(
    repo: Repo,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> dict[str, Any]:
    session = await repo.authenticate(session_token)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход")
    return session


Session = Annotated[dict[str, Any], Depends(current_session)]


async def mutating_session(
    repo: Repo,
    session: Session,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, Any]:
    if not repo.csrf_valid(session, csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недействительный CSRF-токен")
    return session


Mutating = Annotated[dict[str, Any], Depends(mutating_session)]


@router.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    """Без иконки браузер каждую загрузку получал 404 на /favicon.ico — этот
    мусор в консоли легко принять за настоящую ошибку приложения (N1)."""
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "работает"}


@router.post("/api/auth/register", status_code=201)
async def register(payload: AuthPayload, response: Response, repo: Repo) -> dict[str, Any]:
    try:
        session_token, csrf_token = await repo.register(
            payload.login, payload.password, payload.household_name
        )
    except (ValueError, ConflictError) as exc:
        raise HTTPException(status_code=409 if isinstance(exc, ConflictError) else 422, detail=str(exc)) from exc
    set_auth_cookies(response, session_token, csrf_token)
    return {"ok": True}


@router.post("/api/auth/login")
async def login(
    payload: AuthPayload, request: Request, response: Response, repo: Repo
) -> dict[str, Any]:
    client_ip = request.client.host if request.client else "unknown"
    key = f"{client_ip}|{payload.login.strip().casefold()}"
    # Два бакета: по ТЗ — 5/мин на пару IP+логин; второй, более широкий, закрывает
    # DoS через Argon2 (19 МиБ на попытку), когда логин перебирается.
    retry_after = max(
        request.app.state.login_limiter.hit(key),
        request.app.state.login_ip_limiter.hit(client_ip),
    )
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток входа. Попробуйте позже.",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        session_token, csrf_token = await repo.login(payload.login, payload.password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    request.app.state.login_limiter.reset(key)
    set_auth_cookies(response, session_token, csrf_token)
    return {"ok": True}


@router.post("/api/auth/telegram-login")
async def telegram_login(
    payload: TelegramLoginPayload, request: Request, response: Response, repo: Repo
) -> dict[str, Any]:
    """Вход по одноразовому коду из бота (TZ-M7 §3.3).

    Код короткий — шесть цифр, — поэтому лимит здесь строже, чем на обычном
    входе: он и есть основная защита от перебора.
    """
    client_ip = request.client.host if request.client else "unknown"
    retry_after = request.app.state.code_limiter.hit(client_ip)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток. Попробуйте позже.",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        session_token, csrf_token = await repo.telegram_login(payload.code)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    set_auth_cookies(response, session_token, csrf_token)
    return {"ok": True}


@router.post("/api/auth/set-password")
async def set_password(payload: PasswordPayload, repo: Repo, session: Mutating) -> dict[str, Any]:
    """Задать пароль аккаунту, заведённому из бота (TZ-M7 §3.3, шаг 4)."""
    try:
        await repo.set_password(session, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/api/auth/logout")
async def logout(
    response: Response,
    repo: Repo,
    session: Session,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> dict[str, Any]:
    del session
    await repo.logout(session_token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/me")
async def me(repo: Repo, session: Session) -> dict[str, Any]:
    return await repo.get_profile(session)


@router.put("/api/settings")
async def save_settings(payload: SettingsPayload, repo: Repo, session: Mutating) -> dict[str, Any]:
    try:
        await repo.save_settings(
            session,
            payload.household_name,
            [person.model_dump() for person in payload.people],
            payload.appliances,
            [rule.model_dump() for rule in payload.dietary_rules],
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=403 if isinstance(exc, PermissionError) else 422, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/api/dashboard")
async def dashboard(repo: Repo, session: Session) -> dict[str, Any]:
    return await repo.dashboard(session)


@router.get("/api/recipes")
async def recipes(
    repo: Repo,
    session: Session,
    search: str = "",
    cuisine: str = "",
    meal_type: str = "",
    limit: int = 48,
    offset: int = 0,
    ready_only: bool = False,
    dish_type: str = "",
) -> dict[str, Any]:
    del session
    return await repo.list_recipes(
        search, cuisine, meal_type, limit, offset, ready_only, dish_type
    )


# Объявлено до /api/recipes/{recipe_id}: иначе «facets» уедет в разбор int.
@router.get("/api/recipes/facets")
async def recipe_facets(repo: Repo, session: Session) -> dict[str, Any]:
    del session
    return await repo.recipe_facets()


@router.get("/api/recipes/{recipe_id}")
async def recipe_detail(recipe_id: int, repo: Repo, session: Session) -> dict[str, Any]:
    recipe = await repo.recipe_detail(recipe_id, session["household_id"])
    if not recipe:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    return recipe


@router.post("/api/recipes/{recipe_id}/rating")
async def rate_recipe(
    recipe_id: int, payload: RatingPayload, repo: Repo, session: Mutating
) -> dict[str, Any]:
    try:
        result = await repo.set_recipe_rating(session, recipe_id, payload.rating)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    return result


@router.post("/api/recipes/{recipe_id}/review")
async def review_recipe(
    recipe_id: int, payload: ReviewPayload, repo: Repo, session: Mutating
) -> dict[str, Any]:
    try:
        updated = await repo.set_review_status(session, recipe_id, payload.status)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Рецепт не найден")
    return updated


@router.get("/api/products")
async def products(
    repo: Repo,
    session: Session,
    search: str = "",
    sort: str = "name",
    limit: int = 100,
    offset: int = 0,
    discount_only: bool = False,
    category: str = "",
) -> dict[str, Any]:
    del session
    return await repo.list_products(search, sort, limit, offset, discount_only, category)


@router.get("/api/products/categories")
async def product_categories(repo: Repo, session: Session) -> list[dict[str, Any]]:
    del session
    return await repo.product_categories()


@router.get("/api/inventory")
async def inventory(repo: Repo, session: Session) -> list[dict[str, Any]]:
    return await repo.list_inventory(session)


@router.post("/api/inventory", status_code=201)
async def add_inventory(payload: InventoryPayload, repo: Repo, session: Mutating) -> dict[str, Any]:
    if payload.unit_code not in UNIT_CODES:
        raise HTTPException(status_code=422, detail=UNKNOWN_UNIT_TEXT)
    if payload.storage_area not in STORAGE_AREAS:
        raise HTTPException(status_code=422, detail=UNKNOWN_STORAGE_TEXT)
    if payload.expires_on is not None and payload.expires_on < date.today() and not payload.already_expired:
        raise HTTPException(
            status_code=422,
            detail=EXPIRED_TEXT,
        )
    item = payload.model_dump()
    item.pop("already_expired", None)
    try:
        return await repo.add_inventory(session, item)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/api/inventory/{item_id}")
async def delete_inventory(item_id: uuid.UUID, repo: Repo, session: Mutating) -> dict[str, Any]:
    try:
        deleted = await repo.delete_inventory(session, item_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Запас не найден")
    return {"ok": True}


@router.post("/api/plans/generate", status_code=201)
async def generate_plan(payload: PlanPayload, repo: Repo, session: Mutating) -> dict[str, Any]:
    budget_kop = int(payload.budget_rub * 100) if payload.budget_rub is not None else None
    try:
        # Сборка живёт в слое данных: тот же вызов делает бот (TZ-M7 §2).
        return await repo.create_plan(
            session,
            starts_on=payload.starts_on,
            days=payload.days,
            budget_kop=budget_kop,
            cuisines=payload.cuisines,
            price_tier=payload.price_tier,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/plans")
async def list_plans(repo: Repo, session: Session, limit: int = 20) -> dict[str, Any]:
    return {"items": await repo.list_plans(session, min(max(limit, 1), 100))}


# /api/plans/latest объявляется раньше /api/plans/{plan_id}: иначе «latest»
# попытается разобраться как UUID и вернёт 422.
@router.get("/api/plans/latest")
async def latest_plan(repo: Repo, session: Session) -> dict[str, Any] | None:
    return await repo.latest_plan(session)


@router.get("/api/plans/{plan_id}")
async def get_plan(plan_id: uuid.UUID, repo: Repo, session: Session) -> dict[str, Any]:
    plan = await repo.get_plan(session, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="План не найден")
    return plan


@router.post("/api/plans/{plan_id}/meals/{meal_id}/replace")
async def replace_plan_meal(
    plan_id: uuid.UUID,
    meal_id: uuid.UUID,
    payload: ReplaceMealPayload,
    repo: Repo,
    session: Mutating,
) -> dict[str, Any]:
    """TZ-M5R §3: без recipe_id — 3 альтернативы, с recipe_id — замена."""
    try:
        result = await repo.replace_meal(session, plan_id, meal_id, payload.recipe_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="План или приём пищи не найден")
    return result


@router.delete("/api/plans/{plan_id}")
async def delete_plan(plan_id: uuid.UUID, repo: Repo, session: Mutating) -> dict[str, Any]:
    try:
        deleted = await repo.delete_plan(session, plan_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="План не найден")
    return {"ok": True}


@router.patch("/api/plans/{plan_id}/items/{item_id}")
async def mark_purchased(
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: PurchasePayload,
    repo: Repo,
    session: Mutating,
) -> dict[str, Any]:
    try:
        updated = await repo.mark_purchased(session, plan_id, item_id, payload.purchased)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Позиция списка покупок не найдена")
    return updated


@router.patch("/api/settings/people/{person_id}")
async def patch_person(
    person_id: uuid.UUID, payload: PersonPatchPayload, repo: Repo, session: Mutating
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    try:
        updated = await repo.update_person(session, person_id, changes)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Человек не найден")
    return updated


@router.post("/api/telegram/link-token", status_code=201)
async def telegram_link_token(repo: Repo, session: Mutating) -> dict[str, Any]:
    token = await repo.telegram_link_token(session)
    bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "")
    return {
        "token": token,
        "expires_in_seconds": 600,
        "deep_link": f"https://t.me/{bot_username}?start=link_{token}" if bot_username else None,
        "command": f"/start link_{token}",
    }


@router.delete("/api/telegram/link")
async def telegram_unlink(repo: Repo, session: Mutating) -> dict[str, Any]:
    """Отвязать Telegram (TZ-M7 §3.4). Раньше отвязки не было нигде."""
    unlinked = await repo.unlink_telegram(session)
    if not unlinked:
        raise HTTPException(status_code=404, detail="Telegram не был привязан")
    return {"ok": True}


async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": "Проверьте правильность заполнения полей"},
    )


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


async def disable_local_frontend_cache(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/fonts/"):
        # Шрифт версионируется именем файла и не меняется — иначе 352 КБ
        # перекачивались бы при каждой загрузке страницы.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path == "/" or path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def keep_planner_warm(warm_up: Any) -> None:
    """Фоновый прогрев планировщика (N1).

    Пользователь нажимал «Составить меню» и полторы минуты смотрел на
    неподвижную кнопку: остывшие страницы Postgres и пустая мемоизация матчера
    считались прямо в запросе. Теперь это делается заранее и вне запроса; сбой
    прогрева — не повод ронять приложение, план соберётся и по холодному кэшу.
    """
    while True:
        try:
            started = time.monotonic()
            warmed = await warm_up()
            if warmed:
                print(
                    f"planner warm-up: {warmed} ingredients in "
                    f"{time.monotonic() - started:.1f}s",
                    flush=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - прогрев не критичен
            print(f"planner warm-up failed: {error!r}", flush=True)
        await asyncio.sleep(PLANNER_WARM_INTERVAL_SECONDS)


def create_app(repository: Any | None = None) -> FastAPI:
    """Собирает приложение. ``repository=None`` — рабочий режим с Postgres.

    Инъекция репозитория выключает подключение к БД в lifespan: жизненный цикл
    по-прежнему проходит целиком (тест видит то же приложение, что и продакшен),
    но соединением владеет тот, кто репозиторий создал.
    """
    owns_repository = repository is None
    repo = AppRepository() if owns_repository else repository

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if owns_repository:
            await repo.connect()
        warm_up = getattr(repo, "warm_planner_caches", None)
        warm_task = asyncio.create_task(keep_planner_warm(warm_up)) if warm_up else None
        yield
        if warm_task is not None:
            warm_task.cancel()
            with suppress(asyncio.CancelledError):
                await warm_task
        if owns_repository:
            await repo.close()

    application = FastAPI(title="Рацион", version="0.2.0", lifespan=lifespan)
    application.state.repository = repo
    application.state.login_limiter = RateLimiter(capacity=5, window_seconds=60.0)
    application.state.login_ip_limiter = RateLimiter(capacity=20, window_seconds=60.0)
    # TZ-M7 §3.3: код входа всего шестизначный, поэтому лимит здесь жёстче
    # обычного входа — за время жизни кода можно проверить единицы вариантов.
    application.state.code_limiter = RateLimiter(capacity=10, window_seconds=60.0)
    application.add_exception_handler(RequestValidationError, validation_error)
    application.middleware("http")(security_headers)
    application.middleware("http")(disable_local_frontend_cache)
    application.include_router(router)
    application.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.web.main:app", host="0.0.0.0", port=8080, reload=False)
