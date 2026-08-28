/* Экран «Рецепты» и деталь рецепта в диалоге.
 *
 * Что чинится по ТЗ Б2:
 *  — на карточке видны первые ингредиенты (сервер их больше не выбрасывает);
 *  — бейдж «Черновик» стоит только у needs_review, а не у всех подряд;
 *  — ингредиенты в детали — таблица с секциями, количество не мельче названия;
 *  — цены «Ленты» скрыты до явного переключателя;
 *  — фильтр кухни не рисуется, пока кухни не размечены в данных.
 */

import { api, debounce } from "../api.js";
import { register } from "../actions.js";
import * as dialog from "../dialog.js";
import * as format from "../format.js";
import * as router from "../router.js";
import * as toast from "../toast.js";
import { canEdit, canReview, store } from "../state.js";
import { badge, el, frag, humanError, load, metaList, mount, statePanel } from "../render.js";

const PAGE_SIZE = 24;
const MEAL_ORDER = ["breakfast", "lunch", "dinner", "snack", "dessert", "drink"];

let searchController = null;
let pricesVisible = false;

try {
  pricesVisible = localStorage.getItem("ration:prices") === "1";
} catch (error) {
  pricesVisible = false;
}

/* --- список ---------------------------------------------------------------- */

function recipeCard(recipe) {
  const facts = [
    format.countWord(recipe.ingredient_count, "ингредиент", "ингредиента", "ингредиентов"),
    recipe.time_total_minutes ? `${recipe.time_total_minutes} мин` : null,
    recipe.source_servings_min
      ? format.countWord(recipe.source_servings_min, "порция", "порции", "порций")
      : null,
  ];
  const badges = [
    recipe.review_status === "needs_review" ? badge("Черновик", "draft") : null,
    format.dishLabel(recipe.dish_type) ? badge(format.dishLabel(recipe.dish_type)) : null,
    format.cuisineLabel(recipe.cuisine_code) ? badge(format.cuisineLabel(recipe.cuisine_code)) : null,
  ].filter(Boolean);

  return el("a.card.card--recipe", { href: `#/recipes/${recipe.id}` }, [
    el("h3.card__title", { text: recipe.title }),
    recipe.ingredient_names?.length
      ? el("p.card__ingredients", { text: recipe.ingredient_names.join(", ") })
      : null,
    metaList(facts),
    badges.length ? el("div.card__badges", {}, badges) : null,
  ]);
}

function renderList() {
  const { items, total, readyTotal } = store.get("recipes");
  const grid = document.getElementById("recipe-list");
  if (!items.length) {
    mount(
      grid,
      statePanel({
        kind: "empty",
        iconName: "search",
        title: "По этим условиям ничего не нашлось",
        text: "Попробуйте другое слово или снимите фильтры — библиотека большая.",
        action: { label: "Сбросить фильтры", onClick: () => router.go("#/recipes") },
      }),
    );
  } else {
    mount(grid, frag(...items.map(recipeCard)));
  }

  mount(
    document.getElementById("recipe-count"),
    metaList(
      [
        `Показано ${format.number(items.length)} из ${format.number(total)}`,
        readyTotal !== null ? `проверенных ${format.number(readyTotal)}` : null,
      ].filter(Boolean),
    ) || "",
  );

  const more = document.getElementById("recipe-more");
  more.hidden = items.length >= total;
  more.textContent = `Показать ещё ${Math.min(PAGE_SIZE, total - items.length)}`;
}

async function fetchPage({ append = false } = {}) {
  const filters = store.get("recipes.filters");
  const offset = append ? store.get("recipes.items").length : 0;
  searchController?.abort();
  searchController = new AbortController();
  const page = await api.recipes(
    {
      search: filters.q,
      meal_type: filters.meal,
      cuisine: filters.cuisine,
      dish_type: filters.dish,
      ready_only: filters.ready,
      limit: PAGE_SIZE,
      offset,
    },
    searchController.signal,
  );
  store.set("recipes.total", page.total);
  store.set("recipes.offset", offset);
  store.update("recipes.items", (items) => (append ? [...items, ...page.items] : page.items));
  for (const recipe of page.items) {
    const known = store.get(`recipeById.${recipe.id}`) || {};
    store.set(`recipeById.${recipe.id}`, { ...known, ...recipe });
  }
  return page;
}

async function refresh({ append = false } = {}) {
  const grid = document.getElementById("recipe-list");
  if (!append) {
    await load(grid, () => fetchPage(), () => frag(), {
      skeleton: { variant: "card", count: 6 },
      errorTitle: "Не удалось загрузить рецепты",
    });
    renderList();
    return;
  }
  try {
    await fetchPage({ append: true });
    renderList();
  } catch (error) {
    toast.ok(humanError(error));
  }
}

async function loadReadyTotal() {
  if (store.get("recipes.readyTotal") !== null) return;
  try {
    const page = await api.recipes({ limit: 1, ready_only: true });
    store.set("recipes.readyTotal", page.total);
  } catch (error) {
    store.set("recipes.readyTotal", null);
  }
}

/* --- фильтры ---------------------------------------------------------------- */

function renderFilters() {
  const facets = store.get("facets");
  const filters = store.get("recipes.filters");

  const mealTypes = facets.meal_types.length ? facets.meal_types : MEAL_ORDER;
  const ordered = [...mealTypes].sort(
    (left, right) => MEAL_ORDER.indexOf(left) - MEAL_ORDER.indexOf(right),
  );
  mount(
    document.getElementById("recipe-meal-chips"),
    frag(
      ...ordered.map((code) =>
        el("button.chip", {
          type: "button",
          text: format.mealLabel(code),
          "aria-pressed": String(filters.meal === code),
          dataset: { action: "recipes:meal", meal: code },
        }),
      ),
    ),
  );

  // Если фасеты кухонь пустые, блок фильтра не рисуется вовсе,
  // а не показывается пустым или отключённым.
  const cuisineBlock = document.getElementById("recipe-cuisine-block");
  cuisineBlock.hidden = facets.cuisines.length === 0;
  if (facets.cuisines.length) {
    mount(
      document.getElementById("recipe-cuisine-chips"),
      frag(
        ...facets.cuisines.map((code) =>
          el("button.chip", {
            type: "button",
            text: format.cuisineLabel(code) || code,
            "aria-pressed": String(filters.cuisine === code),
            dataset: { action: "recipes:cuisine", cuisine: code },
          }),
        ),
      ),
    );
  }

  // Типы блюд появляются после разметки; до этого фильтр скрыт целиком.
  const dishBlock = document.getElementById("recipe-dish-block");
  const dishTypes = facets.dish_types || [];
  dishBlock.hidden = dishTypes.length === 0;
  if (dishTypes.length) {
    const select = document.getElementById("recipe-dish");
    mount(
      select,
      frag(
        el("option", { value: "", text: "Любое" }),
        ...dishTypes.map((code) =>
          el("option", { value: code, text: format.dishLabel(code) || code }),
        ),
      ),
    );
    select.value = filters.dish;
  }

  document.getElementById("recipe-search").value = filters.q;
  document.getElementById("recipe-ready").checked = filters.ready;
}

function pushFilters(patch) {
  const filters = { ...store.get("recipes.filters"), ...patch };
  router.go(
    router.buildHash("#/recipes", {
      q: filters.q,
      meal: filters.meal,
      cuisine: filters.cuisine,
      dish: filters.dish,
      ready: filters.ready,
    }),
    { replace: true },
  );
}

/* --- деталь ------------------------------------------------------------------ */

function ingredientRows(ingredients) {
  const rows = [];
  let section = null;
  for (const item of ingredients) {
    if (item.section && item.section !== section) {
      section = item.section;
      rows.push(
        el("tr.row--section", {}, [
          el("th", { scope: "colgroup", colSpan: 4, text: item.section }),
        ]),
      );
    }
    const product = item.matched_product;
    rows.push(
      el("tr", {}, [
        el("td.cell--qty", { text: format.quantity(item) || "" }),
        el("td.cell--product", { text: item.ingredient_text || item.normalized_name || "" }),
        el("td.cell--note", { text: item.note || "" }),
        el("td.num", { dataset: { col: "price" } }, [
          product
            ? frag(
                product.url
                  ? el("a", {
                      href: product.url,
                      target: "_blank",
                      rel: "noopener",
                      text: product.name,
                      title: "Открыть товар на lenta.com",
                    })
                  : el("div", { text: product.name }),
                metaList(
                  [product.pack_text, format.money(product.effective_price_kop)].filter(Boolean),
                ),
              )
            : el("span.footnote", { text: "нет в каталоге" }),
        ]),
      ]),
    );
  }
  return rows;
}

function ratingStars(recipe) {
  const current = recipe.my_rating || 0;
  return el("div.rating", { role: "group", "aria-label": "Оценка рецепта" }, [
    ...[1, 2, 3, 4, 5].map((value) =>
      el(`button.rating__star${value <= current ? ".is-active" : ""}`, {
        type: "button",
        text: value <= current ? "★" : "☆",
        disabled: !canEdit(),
        "aria-label": `Оценить на ${value}`,
        "aria-pressed": String(value === current),
        dataset: { action: "recipes:rate", id: recipe.id, rating: value },
      }),
    ),
    current
      ? el("button.btn.btn--small", {
          type: "button",
          text: "Снять оценку",
          dataset: { action: "recipes:rate", id: recipe.id, rating: "" },
        })
      : null,
  ]);
}

function reviewButtons(recipe) {
  if (!canReview()) return null;
  return el("div.btn-group", {}, [
    el("button.btn.btn--small.btn--accent", {
      type: "button",
      text: recipe.review_status === "ready" ? "Уже проверен" : "Готов",
      disabled: recipe.review_status === "ready",
      title:
        recipe.review_status === "ready"
          ? "Рецепт уже отмечен готовым — вернуть в черновики можно кнопкой справа"
          : "Отметить рецепт проверенным",
      dataset: { action: "recipes:review", id: recipe.id, status: "ready" },
    }),
    el("button.btn.btn--small.btn--danger", {
      type: "button",
      text: "Отклонить",
      dataset: { action: "recipes:review", id: recipe.id, status: "rejected" },
    }),
    recipe.review_status !== "needs_review"
      ? el("button.btn.btn--small", {
          type: "button",
          text: "Вернуть в черновики",
          dataset: { action: "recipes:review", id: recipe.id, status: "needs_review" },
        })
      : null,
  ]);
}

function detailView(recipe) {
  const pages =
    recipe.source_page_end && recipe.source_page_end !== recipe.source_page_start
      ? `стр. ${recipe.source_page_start}–${recipe.source_page_end}`
      : `стр. ${recipe.source_page_start}`;

  return el(`div.recipe${pricesVisible ? ".is-prices" : ""}`, { id: "recipe-detail" }, [
    el("h2", { id: "recipe-dialog-title", text: recipe.title }),
    metaList([
      format.countWord(recipe.ingredient_count, "ингредиент", "ингредиента", "ингредиентов"),
      recipe.step_count ? format.countWord(recipe.step_count, "шаг", "шага", "шагов") : null,
      recipe.time_total_minutes ? `${recipe.time_total_minutes} мин` : null,
      recipe.source_servings_min
        ? format.countWord(recipe.source_servings_min, "порция", "порции", "порций")
        : null,
      format.dishLabel(recipe.dish_type),
      format.cuisineLabel(recipe.cuisine_code),
      pages,
    ]),
    recipe.review_status === "needs_review"
      ? el("div.card__badges", {}, [badge("Черновик", "draft")])
      : null,

    el("div.recipe__toolbar", {}, [
      el("label.switch", {}, [
        el("input", {
          type: "checkbox",
          checked: pricesVisible,
          dataset: { change: "recipes:prices" },
        }),
        el("span", { text: "Показать цены «Ленты»" }),
      ]),
      ratingStars(recipe),
      reviewButtons(recipe),
    ]),

    el("section.recipe__section", {}, [
      el("h3", { text: "Ингредиенты" }),
      recipe.ingredients.length
        ? el("div.table-wrap", {}, [
            el("table.table.table--ing", {}, [
              el("caption.visually-hidden", { text: "Ингредиенты рецепта" }),
              el("thead", {}, [
                el("tr", {}, [
                  el("th", { scope: "col", text: "Количество" }),
                  el("th", { scope: "col", text: "Продукт" }),
                  el("th", { scope: "col", text: "Примечание" }),
                  el("th.num", { scope: "col", text: "Товар «Ленты»", dataset: { col: "price" } }),
                ]),
              ]),
              el("tbody", {}, ingredientRows(recipe.ingredients)),
            ]),
          ])
        : el("p.footnote", { text: "Ингредиенты не распознаны." }),
    ]),

    el("section.recipe__section", {}, [
      el("h3", { text: "Приготовление" }),
      recipe.steps.length
        ? el("ol.steps", {}, recipe.steps.map((step) => el("li", {}, [el("span", { text: step.instruction })])))
        : el("p.footnote", {
            text: "Шаги не удалось надёжно разделить. Откройте исходный материал на указанной странице.",
          }),
    ]),

    el("p.footnote", { text: `Локальная библиотека, ${pages}.` }),
  ]);
}

async function openDetail(id) {
  const cached = store.get(`recipeById.${id}`);
  const placeholder = el("div", {}, [
    el("h2", { id: "recipe-dialog-title", text: cached?.title || "Загрузка…" }),
  ]);
  dialog.open(placeholder, {
    labelledBy: "recipe-dialog-title",
    onClose: () => {
      if (location.hash.startsWith("#/recipes/")) history.back();
    },
  });
  try {
    const recipe = await api.recipe(id);
    store.set(`recipeById.${id}`, recipe);
    dialog.open(detailView(recipe), {
      labelledBy: "recipe-dialog-title",
      onClose: () => {
        if (location.hash.startsWith("#/recipes/")) history.back();
      },
    });
  } catch (error) {
    dialog.open(
      statePanel({
        kind: "error",
        iconName: "alert",
        title: "Рецепт не открылся",
        text: humanError(error),
        action: { label: "Повторить", onClick: () => openDetail(id) },
      }),
    );
  }
}

/* --- вход в экран -------------------------------------------------------------- */

export async function enter(query = {}) {
  const filters = {
    q: query.q || "",
    meal: query.meal || "",
    cuisine: query.cuisine || "",
    dish: query.dish || "",
    ready: query.ready === "1",
  };
  const changed = JSON.stringify(filters) !== JSON.stringify(store.get("recipes.filters"));
  store.set("recipes.filters", filters);
  renderFilters();
  loadReadyTotal();
  if (changed || !store.get("recipes.items").length) await refresh();
  else renderList();
}

export async function enterDetail(id, query = {}) {
  await enter(query);
  await openDetail(id);
}

export function init() {
  const search = document.getElementById("recipe-search");
  const push = debounce((value) => pushFilters({ q: value }), 300);
  search.addEventListener("input", (event) => push(event.target.value.trim()));

  document.getElementById("recipe-ready").addEventListener("change", (event) => {
    pushFilters({ ready: event.target.checked });
  });

  register("recipes:meal", (target) => {
    const meal = store.get("recipes.filters").meal === target.dataset.meal ? "" : target.dataset.meal;
    pushFilters({ meal });
  });

  register("recipes:cuisine", (target) => {
    const value = store.get("recipes.filters").cuisine === target.dataset.cuisine
      ? ""
      : target.dataset.cuisine;
    pushFilters({ cuisine: value });
  });

  document.getElementById("recipe-dish").addEventListener("change", (event) => {
    pushFilters({ dish: event.target.value });
  });

  register("recipes:more", () => refresh({ append: true }));

  register("recipes:prices", (target) => {
    pricesVisible = target.checked;
    try {
      localStorage.setItem("ration:prices", pricesVisible ? "1" : "0");
    } catch (error) {
      /* без localStorage настройка живёт до перезагрузки */
    }
    document.getElementById("recipe-detail")?.classList.toggle("is-prices", pricesVisible);
  });

  register("recipes:review", async (target) => {
    const id = Number(target.dataset.id);
    const status = target.dataset.status;
    target.disabled = true;
    try {
      const updated = await api.reviewRecipe(id, status);
      // Патчим состояние — список за диалогом обновляется без перезапроса.
      store.update("recipeById", (byId) => ({
        ...byId,
        [id]: { ...byId[id], review_status: updated.review_status },
      }));
      store.update("recipes.items", (items) =>
        items
          .map((item) => (item.id === id ? { ...item, review_status: updated.review_status } : item))
          .filter((item) => item.review_status !== "rejected"),
      );
      store.set("recipes.readyTotal", null);
      renderList();
      loadReadyTotal();
      const detail = store.get(`recipeById.${id}`);
      if (dialog.isOpen() && detail) dialog.open(detailView(detail), { labelledBy: "recipe-dialog-title",
        onClose: () => {
          if (location.hash.startsWith("#/recipes/")) history.back();
        } });
      toast.ok(status === "ready" ? "Рецепт отмечен готовым" : "Статус обновлён");
    } catch (error) {
      target.disabled = false;
      toast.ok(humanError(error));
    }
  });

  register("recipes:rate", async (target) => {
    const id = Number(target.dataset.id);
    const rating = target.dataset.rating === "" ? null : Number(target.dataset.rating);
    target.disabled = true;
    try {
      const updated = await api.rateRecipe(id, rating);
      store.update("recipeById", (byId) => ({
        ...byId,
        [id]: { ...byId[id], my_rating: updated.my_rating },
      }));
      const detail = store.get(`recipeById.${id}`);
      if (dialog.isOpen() && detail) {
        dialog.open(detailView(detail), {
          labelledBy: "recipe-dialog-title",
          onClose: () => {
            if (location.hash.startsWith("#/recipes/")) history.back();
          },
        });
      }
      toast.ok(rating ? "Оценка сохранена — планировщик учтёт её" : "Оценка снята");
    } catch (error) {
      target.disabled = false;
      toast.ok(humanError(error));
    }
  });
}
