/* Форматирование значений и словари подписей.
 *
 * Правило всего интерфейса: если данных нет — элемент не создаётся вовсе.
 * Поэтому большинство функций здесь возвращают null, а не прочерк. Явные
 * «нет оценки» пишутся только там, где само отсутствие — факт для пользователя.
 */

export const CUISINES = {
  russian: "Русская",
  east_european: "Восточноевропейская",
  italian: "Итальянская",
  french: "Французская",
  georgian: "Грузинская",
  asian: "Азиатская",
  mediterranean: "Средиземноморская",
  middle_eastern: "Ближневосточная",
  mexican: "Мексиканская",
  indian: "Индийская",
  japanese: "Японская",
  american: "Американская",
};

export const DISH_TYPES = {
  soup: "Суп",
  salad: "Салат",
  appetizer: "Закуска",
  sandwich: "Сэндвич",
  steak: "Стейк",
  main_course: "Горячее",
  stew: "Рагу",
  cutlets: "Котлеты",
  casserole: "Запеканка",
  porridge: "Каша",
  pasta: "Паста",
  pizza: "Пицца",
  dumplings: "Пельмени и вареники",
  pancakes: "Блины и оладьи",
  bread: "Хлеб",
  pie: "Пирог",
  cake: "Торт и кексы",
  cookies: "Печенье",
  dessert: "Десерт",
  drink: "Напиток",
  sauce: "Соус",
  preserves: "Заготовки",
  side: "Гарнир",
};

export const MEAL_TYPES = {
  breakfast: "Завтрак",
  lunch: "Обед",
  dinner: "Ужин",
  snack: "Перекус",
  dessert: "Десерт",
  drink: "Напиток",
};

export const APPLIANCES = {
  stove: "Плита",
  oven: "Духовка",
  microwave: "Микроволновка",
  blender: "Блендер",
  mixer: "Миксер",
  multicooker: "Мультиварка",
  air_fryer: "Аэрогриль",
  grill: "Гриль",
  electric_grill: "Электрогриль",
  steamer: "Пароварка",
  food_processor: "Кухонный комбайн",
  pressure_cooker: "Скороварка",
  fridge_freezer: "Холодильник с морозилкой",
};

export const UNITS = {
  g: "г",
  kg: "кг",
  mg: "мг",
  ml: "мл",
  l: "л",
  piece: "шт",
  tablespoon: "ст. л.",
  teaspoon: "ч. л.",
  cup: "чашка",
  glass: "стакан",
  clove: "зубчик",
  bunch: "пучок",
  can: "банка",
  package: "упаковка",
  pinch: "щепотка",
  slice: "ломтик",
  stalk: "стебель",
  leaf: "лист",
  sprig: "веточка",
  handful: "горсть",
  drop: "капля",
};

export const STORAGE_AREAS = {
  fridge: "Холодильник",
  freezer: "Морозилка",
  pantry: "Шкаф",
};

export const ROLES = {
  owner: "Владелец",
  admin: "Администратор",
  editor: "Редактор",
  viewer: "Только просмотр",
};

export const RULE_TYPES = {
  allergy: "Аллергия",
  intolerance: "Непереносимость",
  exclude: "Исключение",
  dislike: "Не любит",
};

export const PRICE_TIERS = {
  economy: "Экономно",
  balanced: "Сбалансированно",
  premium: "Премиально",
};

/* Разделы каталога «Ленты». Слаг несёт числовой идентификатор раздела,
 * человекочитаемого имени в схеме нет — отсюда этот словарь. */
export const CATEGORY_LABELS = {
  "molochnye-produkty-yajjco-3": "Молочные продукты, яйцо",
  "syry-2": "Сыры",
  "myaso-i-ptica-136": "Мясо и птица",
  "kolbasa-sosiski-754": "Колбаса и сосиски",
  "ryba-ikra-moreprodukty-183": "Рыба и морепродукты",
  "ovoshchi-frukty-144": "Овощи и фрукты",
  "makarony-krupy-muka-25": "Макароны, крупы, мука",
  "maslo-sousy-specii-20824": "Масло, соусы, специи",
  "hleb-i-vypechka-165": "Хлеб и выпечка",
  "zamorozka-77": "Заморозка",
  "konservaciya-94": "Консервация",
  "napitki-4": "Напитки",
  "kofe-chajj-kakao-242": "Кофе, чай, какао",
  "sladosti-1028": "Сладости",
  "sneki-20195": "Снеки",
  "gotovaya-eda-42": "Готовая еда",
  "zdorovoe-pitanie-1879": "Здоровое питание",
  "detskoe-pitanie-19327": "Детское питание",
  "alkogol-17036": "Алкоголь",
};

const RUB = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});
const NUMBER = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const DATE_LONG = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long" });
const DATE_SHORT = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" });
const WEEKDAY = new Intl.DateTimeFormat("ru-RU", { weekday: "long" });

export function label(dictionary, code, fallback = null) {
  if (!code) return fallback;
  return dictionary[code] || code;
}

export function cuisineLabel(code) {
  return label(CUISINES, code);
}

export function dishLabel(code) {
  return label(DISH_TYPES, code);
}

export function mealLabel(code) {
  return label(MEAL_TYPES, code, "Приём пищи");
}

export function unitLabel(code) {
  return label(UNITS, code);
}

export function categoryLabel(slug) {
  if (!slug) return "Нет в каталоге «Ленты»";
  if (CATEGORY_LABELS[slug]) return CATEGORY_LABELS[slug];
  // Новый раздел каталога: показываем слаг без числового хвоста, а не «—».
  const words = String(slug).replace(/-\d+$/, "").split("-").join(" ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Копейки → «1 240 ₽». Возвращает null, когда цены нет. */
export function money(kopecks) {
  if (kopecks === null || kopecks === undefined) return null;
  return RUB.format(Number(kopecks) / 100);
}

export function number(value) {
  if (value === null || value === undefined || value === "") return null;
  return NUMBER.format(Number(value));
}

/** «200 г», «2 шт», «по вкусу», либо null, если количество неизвестно. */
export function quantity(item) {
  if (item.is_to_taste) return "по вкусу";
  const min = item.quantity_min ?? item.quantity ?? null;
  const max = item.quantity_max ?? null;
  if (min === null) return null;
  const unit = unitLabel(item.unit_code) || item.unit_raw || "";
  const amount =
    max !== null && Number(max) !== Number(min)
      ? `${number(min)}–${number(max)}`
      : number(min);
  return unit ? `${amount} ${unit}` : amount;
}

function toDate(value) {
  if (!value) return null;
  // Дата без времени: полдень, чтобы часовой пояс не сдвинул сутки назад.
  const text = String(value).length === 10 ? `${value}T12:00:00` : value;
  const parsed = new Date(text);
  return Number.isNaN(parsed.valueOf()) ? null : parsed;
}

export function dateLong(value) {
  const parsed = toDate(value);
  return parsed ? DATE_LONG.format(parsed) : null;
}

export function dateShort(value) {
  const parsed = toDate(value);
  return parsed ? DATE_SHORT.format(parsed) : null;
}

export function weekday(value) {
  const parsed = toDate(value);
  return parsed ? WEEKDAY.format(parsed) : null;
}

export function daysUntil(value) {
  const parsed = toDate(value);
  if (!parsed) return null;
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  return Math.round((parsed - today) / 86400000);
}

/** Русское склонение: plural(2, 'рецепт', 'рецепта', 'рецептов'). */
export function plural(count, one, few, many) {
  const value = Math.abs(Number(count)) % 100;
  const tail = value % 10;
  if (value > 10 && value < 20) return many;
  if (tail > 1 && tail < 5) return few;
  if (tail === 1) return one;
  return many;
}

export function countWord(count, one, few, many) {
  return `${number(count)} ${plural(count, one, few, many)}`;
}
