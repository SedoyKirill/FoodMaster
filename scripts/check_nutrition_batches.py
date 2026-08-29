"""Проверка ответов волны разметки КБЖУ до загрузки в базу.

Прошлые волны показали, что исполнитель охотно халтурит: переписывает имя
по-своему (и разметка ложится мимо ключа поиска), ставит нули там, где просто
не нашёл данных, размечает половину пачки и молчит об этом. Загрузчик поймает
только грубую арифметику, поэтому проверка идёт раньше и говорит,
что именно не так.

Запуск:
    python scripts/check_nutrition_batches.py [batch-001 ...]

Без аргументов проверяет все пачки, для которых есть ответ. Возвращает
ненулевой код, если хоть одна пачка не годится к загрузке.
"""

import glob
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.load_nutrition import valid_row  # noqa: E402

WAVE_DIR = "data/nutrition/wave"
ANSWER_DIR = "data/nutrition/extracted"

#: Нули по всем полям честны только у воды, соли и льда — в любом написании:
#: книги на английском дают «water» и «kosher salt». Всё остальное с нулевой
#: калорийностью — признак «не нашёл данных, поставил нули».
HONEST_ZERO_TOKENS = {
    "вода", "воды", "кипяток", "лед", "лёд", "соль", "соли",
    "water", "ice", "salt",
}


def honest_zero(name: str) -> bool:
    return bool(set(name.lower().replace("-", " ").split()) & HONEST_ZERO_TOKENS)

#: Опорные значения: если исполнитель промахнулся здесь, доверять пачке
#: нельзя целиком. Допуск щедрый — источники расходятся на десяток процентов.
ANCHORS = {
    "масло сливочный": 748,
    "сливочный масло": 748,
    "гриб": 22,
    "лук порей": 61,
    "мука ржаная": 298,
    "корица молотая": 247,
    "нори": 349,
    "рукола": 25,
    "дайкон": 21,
    "honey": 304,
}
ANCHOR_TOLERANCE = 0.35


def batch_names(path: str) -> list[str]:
    """Канонические имена пачки — первая колонка, кроме заголовков."""
    names = []
    for line in io.open(path, encoding="utf-8").read().splitlines():
        if not line or line.startswith("#") or line.startswith("имя |"):
            continue
        names.append(line.split("|")[0].strip())
    return names


#: Доля строк, которую исполнитель вправе пропустить, не опознав продукт:
#: «овощи», «приправы», «обрезки овощей» — это не еда, а заголовки разделов.
#: Больше — значит, размечал невнимательно.
SKIP_TOLERANCE = 0.2


def is_placeholder(item: dict) -> bool:
    """Строка-заглушка: имя есть, чисел нет. Так исполнитель говорит «не знаю»."""
    return all(
        item.get(key) is None
        for key in ("kcal_100", "protein_100", "fat_100", "carb_100")
    )


def check(batch: str) -> tuple[list[str], list[str]]:
    """(что мешает загрузке, что просто стоит знать)."""
    wave_path = os.path.join(WAVE_DIR, f"{batch}.md")
    answer_path = os.path.join(ANSWER_DIR, f"wave-{batch}.json")
    if not os.path.exists(answer_path):
        return [f"ответа нет: {answer_path}"], []
    expected = batch_names(wave_path)
    try:
        items = json.loads(io.open(answer_path, encoding="utf-8-sig").read())
    except Exception as error:  # noqa: BLE001
        return [f"JSON не читается: {error}"], []
    if not isinstance(items, list):
        return ["ожидался плоский список объектов"], []

    problems: list[str] = []
    notes: list[str] = []
    seen: dict[str, dict] = {}
    for item in items:
        name = str(item.get("name") or "")
        if name in seen:
            notes.append(f"имя повторяется, останется последнее: «{name}»")
        seen[name] = item
    placeholders = [name for name, item in seen.items() if is_placeholder(item)]
    for name in placeholders:
        del seen[name]

    unknown = [name for name in seen if name not in expected]
    if unknown:
        # Самая частая халтура: имя переписано «по-человечески». Ключ поиска
        # при этом ломается, а на глаз файл выглядит правильным.
        problems.append(
            f"имён нет в пачке ({len(unknown)}): " + ", ".join(f"«{n}»" for n in unknown[:5])
        )
    missing = [name for name in expected if name not in seen]
    if missing:
        # Пропуск разрешён промптом: неопознанный продукт честнее оставить
        # без данных. Тревожит только массовый пропуск.
        line = (
            f"без данных {len(missing)} из {len(expected)}: "
            + ", ".join(f"«{n}»" for n in missing[:5])
        )
        if len(missing) > len(expected) * SKIP_TOLERANCE:
            problems.append(line + " — это слишком много")
        else:
            notes.append(line)

    for name, item in seen.items():
        reason = valid_row(item)
        if reason:
            problems.append(f"«{name}»: {reason}")
            continue
        values = (item.get("kcal_100"), item.get("protein_100"),
                  item.get("fat_100"), item.get("carb_100"))
        if all(float(value or 0) == 0 for value in values) and not honest_zero(name):
            # Ноль калорий бывает честным далеко за пределами воды и соли:
            # сода, лимонная кислота, чайный настой, стевия. Это повод
            # посмотреть глазами, а не отказывать в загрузке.
            notes.append(f"«{name}»: нули по всем полям — проверить глазами")
        anchor = ANCHORS.get(name)
        if anchor is not None:
            kcal = float(item.get("kcal_100") or 0)
            if abs(kcal - anchor) > anchor * ANCHOR_TOLERANCE:
                problems.append(f"«{name}»: {kcal:.0f} ккал против ожидаемых ~{anchor}")
    return problems, notes


def main() -> int:
    batches = sys.argv[1:] or sorted(
        os.path.basename(path).removeprefix("wave-").removesuffix(".json")
        for path in glob.glob(os.path.join(ANSWER_DIR, "wave-batch-*.json"))
    )
    if not batches:
        print("Ответов волны нет.")
        return 0
    bad = 0
    for batch in batches:
        problems, notes = check(batch)
        if problems:
            bad += 1
            print(f"\n{batch}: НЕ ГОДИТСЯ, замечаний {len(problems)}")
            for problem in problems[:12]:
                print(f"  — {problem}")
            if len(problems) > 12:
                print(f"  … и ещё {len(problems) - 12}")
        else:
            usable = sum(
                1
                for item in json.loads(
                    io.open(
                        os.path.join(ANSWER_DIR, f"wave-{batch}.json"), encoding="utf-8-sig"
                    ).read()
                )
                if not is_placeholder(item)
            )
            print(f"{batch}: годится, записей {usable}")
        for note in notes[:3]:
            print(f"    · {note}")
        if len(notes) > 3:
            print(f"    · … и ещё {len(notes) - 3} замечания к сведению")
    print(f"\nпачек проверено: {len(batches)}, не годится: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
