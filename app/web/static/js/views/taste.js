/* Экран «Вкусы семьи» (TZ-M8 §4.4, §7).
 *
 * Планировщик учится на событиях — оценках, заменах, отметках «приготовили».
 * Новой семье учиться не на чем, поэтому здесь есть холодный старт: два
 * десятка карточек с 👍 / 👎 / пропуском. Пропуск событием не считается:
 * молчание — не мнение, и притворяться, что семья что-то сказала, нельзя.
 *
 * Второй блок показывает, что модель уже поняла. Без него «почему это блюдо»
 * в плане выглядит гаданием: здесь видно, из чего оно выведено.
 */

import { api } from "../api.js";
import { register } from "../actions.js";
import * as format from "../format.js";
import * as toast from "../toast.js";
import { canEdit } from "../state.js";
import { el, frag, humanError, load, statePanel } from "../render.js";

/** recipe_id → true (нравится) / false (не моё); пропуск не хранится. */
const answers = new Map();

function cardMeta(card) {
  return [format.cuisineLabel(card.cuisine_code), format.dishLabel(card.dish_type)]
    .filter(Boolean)
    .join(" · ");
}

function choiceButton(card, value, label, title) {
  const chosen = answers.get(card.recipe_id);
  const active = value === null ? chosen === undefined : chosen === value;
  return el("button.btn.btn--small", {
    type: "button",
    text: label,
    title,
    "aria-pressed": String(active),
    disabled: !canEdit(),
    dataset: {
      action: "taste:choose",
      recipeId: String(card.recipe_id),
      value: value === null ? "skip" : String(value),
    },
  });
}

function onboardingCard(card) {
  return el("li.taste-card", { dataset: { recipeId: String(card.recipe_id) } }, [
    el("a.taste-card__title", { href: `#/recipes/${card.recipe_id}`, text: card.title }),
    cardMeta(card) ? el("p.footnote", { text: cardMeta(card) }) : null,
    el("div.taste-card__choices", {}, [
      choiceButton(card, true, "Нравится", "Такое мы любим"),
      choiceButton(card, false, "Не моё", "Такое не готовим"),
      choiceButton(card, null, "Пропустить", "Не знаю — не считать мнением"),
    ]),
  ]);
}

function onboardingView(data) {
  const cards = data.cards || [];
  if (!cards.length) {
    return statePanel({
      kind: "empty",
      iconName: "star",
      title: "Карточек больше нет",
      text: "Вы прошли весь холодный старт. Дальше планировщик учится на самих планах: оценках, заменах и отметках «приготовили».",
      action: { label: "Составить меню", href: "#/plan" },
    });
  }
  return frag(
    el("p.footnote", {
      text: data.needed
        ? "Отметьте всё, что узнаёте. Двадцати ответов хватает, чтобы первое меню было не случайным."
        : "Событий у планировщика уже достаточно, но подсказать напрямую никогда не поздно.",
    }),
    el("ul.taste-cards", {}, cards.map(onboardingCard)),
    el("div.taste-actions", {}, [
      el("button.btn.btn--action", {
        type: "button",
        text: "Сохранить ответы",
        disabled: !canEdit(),
        dataset: { action: "taste:save" },
      }),
      el("span.footnote", {
        id: "taste-answered",
        text: "Пропущенные карточки мнением не считаются.",
      }),
    ]),
  );
}

function scoreRow(title, score, events) {
  return el("li", {}, [
    el("span", { text: title }),
    el("span.footnote", {
      text: `${score > 0 ? "+" : ""}${score}${events ? ` · ${format.countWord(events, "отметка", "отметки", "отметок")}` : ""}`,
    }),
  ]);
}

function group(title, items, render) {
  if (!items || !items.length) return null;
  return el("div.taste-group", {}, [
    el("h3", { text: title }),
    el("ul.taste-list", {}, items.map(render)),
  ]);
}

function summaryView(summary) {
  const groups = [
    group("Любимые блюда", summary.favourite_recipes, (item) =>
      el("li", {}, [
        el("a", { href: `#/recipes/${item.recipe_id}`, text: item.title || `Рецепт ${item.recipe_id}` }),
        el("span.footnote", { text: `+${item.score}` }),
      ]),
    ),
    group("Что не идёт", summary.disliked_recipes, (item) =>
      el("li", {}, [
        el("a", { href: `#/recipes/${item.recipe_id}`, text: item.title || `Рецепт ${item.recipe_id}` }),
        el("span.footnote", { text: String(item.score) }),
      ]),
    ),
    group("Типы блюд", summary.favourite_dish_types, (item) =>
      scoreRow(format.dishLabel(item.key) || item.key, item.score, item.events_count),
    ),
    group("Кухни", summary.favourite_cuisines, (item) =>
      scoreRow(format.cuisineLabel(item.key) || item.key, item.score, item.events_count),
    ),
    group("Продукты, которые не заходят", summary.disliked_ingredients, (item) =>
      scoreRow(item.key, item.score, item.events_count),
    ),
  ].filter(Boolean);

  if (!groups.length) {
    return statePanel({
      kind: "empty",
      iconName: "star",
      title: "Пока ничего не понятно",
      text: "Отметьте любимое выше или оцените блюда в составленном меню — после этого планировщик начнёт различать ваши вкусы.",
    });
  }
  return frag(
    el("p.footnote", {
      text: `Учтено событий: ${format.number(summary.events_count || 0)}. Оценка от −1 до +1: чем дальше от нуля, тем увереннее вывод.`,
    }),
    el("div.taste-groups", {}, groups),
  );
}

function refreshAnswerCount() {
  const node = document.getElementById("taste-answered");
  if (!node) return;
  node.textContent = answers.size
    ? `Отмечено: ${format.countWord(answers.size, "карточка", "карточки", "карточек")}. Пропущенные мнением не считаются.`
    : "Пропущенные карточки мнением не считаются.";
}

async function showOnboarding() {
  await load(
    document.getElementById("taste-onboarding"),
    () => api.tasteOnboarding(),
    onboardingView,
    { skeleton: { variant: "card", count: 6 }, errorTitle: "Карточки не загрузились" },
  );
}

async function showSummary() {
  await load(
    document.getElementById("taste-summary"),
    () => api.tasteSummary(),
    summaryView,
    { skeleton: { variant: "row", count: 4 }, errorTitle: "Сводка не загрузилась" },
  );
}

export async function enter() {
  answers.clear();
  await Promise.all([showOnboarding(), showSummary()]);
}

export function init() {
  register("taste:choose", (target) => {
    const recipeId = Number(target.dataset.recipeId);
    const value = target.dataset.value;
    if (value === "skip") answers.delete(recipeId);
    else answers.set(recipeId, value === "true");
    for (const button of target.parentElement.querySelectorAll("[data-action]")) {
      const chosen = answers.get(recipeId);
      const isSkip = button.dataset.value === "skip";
      const active = isSkip ? chosen === undefined : String(chosen) === button.dataset.value;
      button.setAttribute("aria-pressed", String(active));
    }
    refreshAnswerCount();
  });

  register("taste:save", async (target) => {
    if (!answers.size) {
      toast.ok("Отметьте хотя бы одну карточку — пропуски мнением не считаются.");
      return;
    }
    target.disabled = true;
    try {
      const payload = [...answers.entries()].map(([recipe_id, liked]) => ({ recipe_id, liked }));
      const { saved } = await api.saveTasteOnboarding(payload);
      answers.clear();
      toast.ok(`Учтено ответов: ${saved}`);
      await enter();
    } catch (error) {
      target.disabled = false;
      toast.ok(humanError(error));
    }
  });
}
