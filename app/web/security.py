from __future__ import annotations

import hashlib
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


LOGIN_RE = re.compile(r"^[a-zA-Zа-яА-ЯёЁ0-9_.-]{3,40}$")
PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)


def validate_login(login: str) -> str:
    value = login.strip()
    if not LOGIN_RE.fullmatch(value):
        raise ValueError("Логин: 3–40 букв, цифр или символов . _ -")
    return value


def validate_password(password: str) -> None:
    if len(password) < 8 or len(password) > 200:
        raise ValueError("Пароль должен содержать от 8 до 200 символов")


def hash_password(password: str) -> str:
    validate_password(password)
    return PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
