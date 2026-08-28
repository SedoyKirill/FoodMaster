"""Аккаунт целиком из чата: регистрация, вход в веб, отвязка (TZ-M7 §3.2–3.4).

Ключевое решение — А1: аккаунт, заведённый из бота, **не имеет пароля**.
Пароль, введённый в чат, навсегда остаётся в истории Telegram и в резервных
копиях мессенджера, поэтому в веб человек входит по одноразовому коду, а
пароль при желании задаёт уже в браузере.
"""

from __future__ import annotations

import os
from typing import Any

from app.web.database import ConflictError
from app.web.payloads import validate_household_name

from ..callbacks import encode_callback
from ..fsm import CANCEL_BUTTON, DialogState
from ..render import HELP_TEXT, CallbackReply, Reply, build_keyboard
from . import SceneContext

#: имя сцены в telegram_dialog_state
SCENE = "auth.register"

DEFAULT_HOUSEHOLD_NAME = "Моя семья"

WELCOME_TEXT = (
    "Привет! Я «Супостат» — планирую меню семьи.\n\n"
    "Если аккаунта ещё нет, заведу его прямо здесь: пароль не понадобится."
)

ALREADY_LINKED_TEXT = "Ваш Telegram уже привязан к аккаунту «{login}»."

HAVE_ACCOUNT_TEXT = (
    "Откройте веб-приложение «Рацион» → Настройки → Телеграм → «Получить "
    "команду» и пришлите мне команду вида «/start link_…» в течение 10 минут."
)

ASK_HOUSEHOLD_TEXT = (
    "Как назовём семью? Название видно только вам — его можно поменять в "
    "настройках."
)

UNLINK_WARNING_TEXT = (
    "У аккаунта нет пароля: после отвязки войти будет нечем.\n"
    "Сначала задайте пароль — /web, затем в вебе «Настройки → Аккаунт»."
)


def start_keyboard() -> dict[str, Any]:
    return build_keyboard([
        [{"text": "✳️ Создать аккаунт", "callback_data": encode_callback("n", "reg")}],
        [{"text": "🔗 У меня уже есть аккаунт", "callback_data": encode_callback("n", "link")}],
    ])


def welcome_reply() -> Reply:
    return Reply(WELCOME_TEXT, start_keyboard())


def have_account_reply() -> Reply:
    """Инструкция по привязке; со ссылкой, если известно имя бота."""
    username = os.getenv("TELEGRAM_BOT_USERNAME", "").strip()
    text = HAVE_ACCOUNT_TEXT
    if username:
        text += f"\n\nСсылка-помощник: https://t.me/{username}?start=link_ВАШ_ТОКЕН"
    return Reply(text)


def ask_household_reply() -> Reply:
    keyboard = build_keyboard([
        [{
            "text": f"Так и оставить: «{DEFAULT_HOUSEHOLD_NAME}»",
            "callback_data": encode_callback("n", "regdef"),
        }],
        [CANCEL_BUTTON],
    ])
    return Reply(ASK_HOUSEHOLD_TEXT, keyboard)


async def begin(dialogs: Any, user_id: int) -> Reply:
    """Начать регистрацию: спросить название семьи."""
    await dialogs.save(user_id, DialogState(SCENE, "household"))
    return ask_household_reply()


async def create_account(
    app_repository: Any,
    bot_repository: Any,
    dialogs: Any,
    user_id: int,
    household_name: str,
) -> Reply:
    """Завести аккаунт без пароля и сразу привязать его к этому Telegram.

    Привязка идёт в той же транзакции, что и создание: иначе сбой между двумя
    шагами оставил бы человеку аккаунт, в который он не может войти никак.
    """
    login = f"tg{user_id}"
    try:
        await app_repository.register_account(
            login, None, household_name,
            telegram_user_id=user_id, channel="telegram",
        )
    except ConflictError:
        # Логин выведен из Telegram-id, так что это тот же человек: он уже
        # заводил аккаунт и потом отвязался. Возвращаем ему прежний, а не
        # плодим второй с теми же данными.
        relinked = await bot_repository.relink_account(login, user_id)
        await dialogs.clear(user_id)
        if relinked:
            return Reply(
                f"Нашёл ваш прежний аккаунт «{login}» и привязал его заново.\n\n{HELP_TEXT}"
            )
        return Reply(
            f"Аккаунт «{login}» уже существует и привязан к другому чату.\n"
            f"{HAVE_ACCOUNT_TEXT}"
        )
    await dialogs.clear(user_id)
    return Reply(
        f"Готово! Аккаунт «{login}» создан, семья — «{household_name}».\n"
        "Пароль не нужен: в браузер вы войдёте по коду из команды /web.\n\n"
        f"{HELP_TEXT}"
    )


async def handle_step(ctx: SceneContext) -> Reply:
    """Шаг сцены регистрации: сейчас он один — название семьи."""
    try:
        household_name = validate_household_name(ctx.text)
    except ValueError as exc:
        return Reply(f"{exc}\n\n{ASK_HOUSEHOLD_TEXT}", ask_household_reply().keyboard)
    return await create_account(
        ctx.app_repository, ctx.bot_repository, ctx.dialogs,
        ctx.actor.user_id, household_name,
    )


# --- /web: вход в браузер по одноразовому коду (§3.3) -------------------------

def web_url(code: str) -> str:
    base = os.getenv("WEB_PUBLIC_URL", "http://localhost:8080").rstrip("/")
    return f"{base}/#/login/tg/{code}"


async def web_login(app_repository: Any, context: dict[str, Any]) -> Reply:
    code = await app_repository.web_login_code(context["user_id"])
    return Reply(
        "Вход в веб-приложение:\n"
        f"{web_url(code)}\n\n"
        f"Или откройте «Войти через Telegram» и введите код: {code}\n"
        "Код действует 5 минут и срабатывает один раз."
    )


# --- /unlink: отвязка (§3.4) --------------------------------------------------

def unlink_confirmation(has_password: bool) -> Reply:
    warning = "" if has_password else f"\n\n{UNLINK_WARNING_TEXT}"
    keyboard = build_keyboard([
        [{"text": "Да, отвязать", "callback_data": encode_callback("y", "unlink")}],
        [CANCEL_BUTTON],
    ])
    return Reply(
        f"Отвязать Telegram от аккаунта? Данные семьи останутся на месте.{warning}",
        keyboard,
    )


async def unlink(app_repository: Any, session: dict[str, Any]) -> CallbackReply:
    # Незавершённый диалог стирает сам репозиторий, в одной транзакции с
    # удалением привязки, — боту тут делать нечего.
    unlinked = await app_repository.unlink_telegram(session)
    if not unlinked:
        return CallbackReply(edit=Reply("Telegram и так не был привязан."))
    return CallbackReply(
        edit=Reply(
            "Готово, Telegram отвязан. Чтобы снова получать меню, привяжите его "
            "в вебе: Настройки → Телеграм."
        )
    )
