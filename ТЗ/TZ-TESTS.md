# TZ-TESTS — ТЗ на написание тестов

> **Для исполнителя.** Применяется вместе с TZ-M2R / TZ-M6R / TZ-M5R: каждый
> из них ссылается сюда. Фреймворк — стандартный `unittest` (уже используется),
> без pytest и новых зависимостей. Запуск: `python -m unittest discover -s tests -v`.

## 1. Текущее состояние

Три файла: `tests/test_lenta_parsing.py`, `tests/test_recipe_parsing.py`
(удаляется вместе со старым парсером в TZ-M2R T9), `tests/test_web_planner.py`.
Не покрыто вообще: HTTP-слой (`app/web/main.py`), запросы к БД
(`app/web/database.py`), безопасность (CSRF, роли, изоляция household),
фронтенд. Критерий TZ-M1 «тесты покрывают горизонтальное повышение прав и
перебор household_id» не выполнен.

## 2. Правила

1. Тесты без сети, без Docker, без реального Postgres — юнит-уровень изолирует
   логику; для запросов БД см. §4 (фейковый пул).
2. Один тестовый файл на модуль: `tests/test_<модуль>.py`.
3. Каждое исправление бага из AUDIT начинается с падающего теста,
   воспроизводящего баг (TDD), в имени — номер: `test_b1_plan_survives_reload`.
4. Фикстуры — обычные словари/константы в файле теста; общие — в
   `tests/fixtures.py`. Никаких JSON-файлов фикстур без необходимости.
5. Детерминизм: никаких `datetime.now()` в проверяемых значениях — время
   передавать параметром или фиксировать.

## 3. Обязательные тесты по слоям

### 3.1 Импорт рецептов (из TZ-M2R §7)

`test_recipe_cleaning.py`, `test_recipe_windows.py`, `test_recipe_validate.py`,
`test_recipe_load.py` — полный список кейсов в TZ-M2R §7 (дубли глифов,
mojibake, колонтитулы, окна, validate_payload, правила готовности, дедуп).

### 3.2 Планировщик (из TZ-M5R §4)

10 кейсов из TZ-M5R §4: синонимы аллергий, повторяемость, бюджет, игрушечная
оптимальность, scale_unknown, агрегация покупок, FEFO, fallback без ortools.

### 3.3 Веб-API — `tests/test_web_api.py`

Через `fastapi.testclient.TestClient` с подменённым репозиторием (§4):

- регистрация → куки сессии и CSRF установлены, 201;
- мутация без CSRF-заголовка → 403; с неверным → 403; с верным → 2xx;
- `GET /api/recipes` без сессии → 401;
- изоляция household: пользователь A не видит inventory/план пользователя B
  (обращение с чужим id → 404, не 403 с утечкой существования);
- роль viewer: POST /api/inventory → 403;
- login rate-limit (после A6): 6-я попытка за минуту → 429 с Retry-After;
- security-заголовки (после A6): CSP/XCTO/XFO присутствуют в любом ответе;
- `recipe_detail` не содержит ключей `source_id`/`fingerprint` (S5);
- B1: план с «плохим» названием блюда всё равно возвращается из
  `/api/plans/latest`;
- A4: история планов, отметка «куплено», смена review_status — happy-path +
  проверка прав.

### 3.4 Запросы БД — `tests/test_web_database.py`

`AppRepository` вызывает пул через небольшой набор методов — сделать
`FakePool`/`FakeConnection` (записывают SQL и параметры, возвращают заданные
строки). Проверять:

- `list_recipes` строит LIMIT/OFFSET и не фильтрует в Python (A3);
- `delete_inventory` возвращает True только при «DELETE 1» (S6);
- `csrf_valid` использует compare_digest (S1) — прямой юнит-тест функции;
- `authenticate` не пишет `last_seen_at` чаще раза в 5 минут (A5).

### 3.5 Сборщик Ленты

`test_lenta_parsing.py` сохранить как есть; при изменениях сборщика — тем же
стилем (чистые функции разбора страниц каталога).

## 4. Инфраструктура тестов

- `tests/fakes.py`: `FakePool` (async `fetch/fetchrow/fetchval/execute`,
  журнал вызовов), `FakeRepository` для TestClient (in-memory словари:
  users/sessions/households/inventory/plans). Плюс фабрики
  `make_recipe(**overrides)`, `make_person(**overrides)`.
- FastAPI-приложение должно позволять инъекцию репозитория: фабрика
  `create_app(repository=None)` в `app/web/main.py` (маленький рефакторинг,
  входит в TZ-M6R A-часть).
- Никаких «интеграционных с Docker» в обязательном наборе; ручной смоук
  описан в приёмках TZ-M6R/TZ-M5R.

## 5. Приёмка

- `python -m unittest discover -s tests -v` — один процесс, < 60 секунд,
  без сети; все зелёные.
- Для каждого номера из AUDIT §2, помеченного как исправленный, существует
  тест с этим номером в имени.
- Новые модули из TZ-M2R/M5R имеют свой файл тестов (см. 3.1–3.2).
