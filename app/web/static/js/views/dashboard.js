/* Экран «Сегодня».
 *
 * Что чинится по ТЗ Б2:
 *  — ближайшие приёмы пищи вместо четырёх абстрактных счётчиков;
 *  — истекающие запасы ПОИМЁННО (выводятся из уже загруженного /api/inventory,
 *    новый эндпоинт не нужен) вместо одного числа;
 *  — один призыв к действию;
 *  — счётчики «Источников» и «Товаров «Ленты»» уехали в Настройки → Данные.
 *
 * /api/dashboard запрашивается один раз за сессию: прежде он дёргался после
 * каждого сохранения, добавления и удаления.
 */

import { api } from "../api.js";
import * as format from "../format.js";
import { store } from "../state.js";
import { el, frag, mount, pill, statePanel } from "../render.js";

function upcomingMeals() {
  const plans = store.get("plans.byId");
  const activeId = store.get("plans.activeId");
  const plan = activeId ? plans[activeId] : Object.values(plans)[0];
  if (!plan || !plan.meals?.length) return [];
  const today = new Date().toISOString().slice(0, 10);
  const future = plan.meals.filter((meal) => meal.meal_date >= today);
  return (future.length ? future : plan.meals).slice(0, 3);
}

function mealsPanel() {
  const meals = upcomingMeals();
  const body = meals.length
    ? el(
        "div.stack",
        {},
        meals.map((meal) =>
          el("div.stack__row", {}, [
            el("div", {}, [
              el("ul.meta.slot__label", {}, [
                el("li", { text: format.mealLabel(meal.meal_type) }),
                el("li", { text: format.dateShort(meal.meal_date) || "" }),
              ]),
              el("a.slot__dish", { href: `#/recipes/${meal.recipe_id}`, text: meal.title }),
            ]),
            el("span.footnote", {
              text: meal.estimated_kcal != null
                ? `≈ ${format.number(meal.estimated_kcal)} ккал`
                : "нет оценки ккал",
            }),
          ]),
        ),
      )
    : statePanel({
        kind: "empty",
        iconName: "calendar",
        title: "Меню ещё не составлено",
        text: "Планировщик подберёт блюда и соберёт список покупок с ценами «Ленты».",
        action: { label: "Составить меню", href: "#/plan" },
      });

  return el("section.panel", {}, [
    el("div.panel__head", {}, [el("h2", { text: "Ближайшие приёмы пищи" })]),
    body,
  ]);
}

function expiringPanel() {
  const items = store
    .get("inventory.items")
    .filter((item) => item.expires_on && format.daysUntil(item.expires_on) <= 3)
    .sort((left, right) => left.expires_on.localeCompare(right.expires_on));

  const body = items.length
    ? el(
        "div.stack",
        {},
        items.map((item) => {
          const days = format.daysUntil(item.expires_on);
          return el("div.stack__row", {}, [
            el("a", { href: "#/inventory", text: item.name }),
            pill(
              days < 0 ? `просрочен ${format.dateShort(item.expires_on)}` : `до ${format.dateShort(item.expires_on)}`,
              days < 0 ? "expired" : "soon",
              "alert",
            ),
          ]);
        }),
      )
    : statePanel({
        kind: "empty",
        iconName: "fridge",
        title: "Ничего не портится",
        text: "Здесь появятся продукты со сроком до трёх дней, чтобы их успели съесть.",
        action: { label: "Открыть запасы", href: "#/inventory" },
      });

  return el("section.panel", {}, [
    el("div.panel__head", {}, [el("h2", { text: "Использовать скорее" })]),
    body,
  ]);
}

function statsRow() {
  const dashboard = store.get("dashboard");
  if (!dashboard) return null;
  const inventoryCount = store.get("inventory.items").length;
  return el("div.stat-row", {}, [
    el("a.stat", { href: "#/recipes" }, [
      el("span.stat__value", { text: format.number(dashboard.recipes) }),
      el("span.stat__label", {
        text: dashboard.recipes_ready
          ? `рецептов, из них ${format.number(dashboard.recipes_ready)} проверено`
          : "рецептов",
      }),
    ]),
    el("a.stat", { href: "#/inventory" }, [
      el("span.stat__value", { text: format.number(inventoryCount) }),
      el("span.stat__label", { text: "партий дома" }),
    ]),
  ]);
}

export function render() {
  const name = store.get("me.household.name") || "семьи";
  mount(document.getElementById("dashboard-greeting"), `Что готовим для «${name}»?`);
  mount(document.getElementById("dashboard-stats"), statsRow() || frag());
  mount(document.getElementById("dashboard-panels"), frag(mealsPanel(), expiringPanel()));
}

export async function enter() {
  if (!store.get("dashboard")) {
    try {
      store.set("dashboard", await api.dashboard());
    } catch (error) {
      store.set("dashboard", null);
    }
  }
  render();
}

export function init() {
  // Панели перерисовываются сами, когда меняются данные соседних экранов —
  // именно это убирает перезагрузку дашборда после каждого действия.
  store.subscribe("inventory.items", () => {
    if (document.getElementById("view-dashboard").classList.contains("is-active")) render();
  });
  store.subscribe("plans.byId", () => {
    if (document.getElementById("view-dashboard").classList.contains("is-active")) render();
  });
}
