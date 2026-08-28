/* Точка входа: сессия, роутер, общие обработчики. */

import { api, setUnauthorizedHandler } from "./api.js";
import { initActions, register } from "./actions.js";
import { initDialog, closeSilently } from "./dialog.js";
import { hydrateIcons, icon } from "./icons.js";
import * as router from "./router.js";
import * as theme from "./theme.js";
import { store } from "./state.js";
import { ROLES } from "./format.js";
import { mount } from "./render.js";

import * as dashboard from "./views/dashboard.js";
import * as plan from "./views/plan.js";
import * as recipes from "./views/recipes.js";
import * as inventory from "./views/inventory.js";
import * as products from "./views/products.js";
import * as settings from "./views/settings.js";
import * as auth from "./views/auth.js";

const VIEW_BY_PATH = {
  "#/": "dashboard",
  "#/plan": "plan",
  "#/recipes": "recipes",
  "#/inventory": "inventory",
  "#/products": "products",
  "#/settings": "settings",
};

function showView(name) {
  for (const section of document.querySelectorAll(".view")) {
    section.classList.toggle("is-active", section.id === `view-${name}`);
  }
  for (const link of document.querySelectorAll(".nav__item")) {
    const target = link.getAttribute("href").split("?")[0];
    const active = VIEW_BY_PATH[target] === name;
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.querySelector(".sidebar").classList.remove("is-open");
  // Мобильный ящик выставляет inert на содержимое; при переходе его надо снять,
  // иначе экран остаётся недоступным для клавиатуры и мыши.
  const main = document.getElementById("main");
  main.inert = false;
  main.focus({ preventScroll: true });
  window.scrollTo({ top: 0 });
}

function showAuth() {
  document.getElementById("auth-view").hidden = false;
  document.getElementById("app-view").hidden = true;
  document.getElementById("auth-login").focus();
}

function showApp() {
  const me = store.get("me");
  document.getElementById("auth-view").hidden = true;
  document.getElementById("app-view").hidden = false;
  document.getElementById("user-login").textContent = me.user.login;
  document.getElementById("user-role").textContent = ROLES[me.household.role] || me.household.role;
  document.getElementById("user-avatar").textContent = me.user.login.slice(0, 1).toUpperCase();
}

function defineRoutes() {
  const guard = (name, handler) => async (params, query) => {
    if (!store.get("me")) return;
    if (name !== "recipes") closeSilently();
    showView(name);
    await handler(params, query);
  };

  router.define("#/", guard("dashboard", () => dashboard.enter()));
  router.define("#/plan", guard("plan", () => plan.enter(null)));
  router.define("#/plan/:id", guard("plan", (params) => plan.enter(params.id)));
  router.define("#/recipes", guard("recipes", (_params, query) => {
    closeSilently();
    return recipes.enter(query);
  }));
  router.define("#/recipes/:id", guard("recipes", (params, query) =>
    recipes.enterDetail(params.id, query),
  ));
  router.define("#/inventory", guard("inventory", () => inventory.enter()));
  router.define("#/products", guard("products", (_params, query) => products.enter(query)));
  router.define("#/settings", guard("settings", () => settings.enter()));
}

function updateThemeButton() {
  const button = document.getElementById("theme-toggle");
  const dark = theme.resolvedTheme() === "dark";
  mount(button, icon(dark ? "sun" : "moon", { size: 20 }));
  button.setAttribute("aria-label", dark ? "Включить светлую тему" : "Включить тёмную тему");
}

async function startSession() {
  showApp();
  // Профиль, справочники и запасы грузятся один раз за сессию: экраны читают
  // их из общего состояния, а не перезапрашивают после каждого действия.
  const [facets, inventoryItems] = await Promise.all([
    api.recipeFacets().catch(() => ({ cuisines: [], meal_types: [] })),
    api.inventory().catch(() => []),
    api.dashboard().then((data) => store.set("dashboard", data)).catch(() => null),
  ]);
  store.set("facets", facets);
  store.set("inventory.items", inventoryItems);
  try {
    const latest = await api.latestPlan();
    if (latest) {
      store.set(`plans.byId.${latest.id}`, latest);
      store.set("plans.activeId", latest.id);
    }
  } catch (error) {
    /* план необязателен для запуска приложения */
  }
  router.start();
}

async function boot() {
  hydrateIcons();
  initActions();
  initDialog();
  updateThemeButton();
  defineRoutes();

  setUnauthorizedHandler(() => {
    store.set("me", null);
    showAuth();
  });

  register("app:logout", async () => {
    try {
      await api.logout();
    } catch (error) {
      /* сессия могла истечь — всё равно возвращаемся на экран входа */
    }
    location.reload();
  });

  register("app:theme", () => {
    theme.toggleTheme();
    updateThemeButton();
  });

  register("app:menu", () => {
    const sidebar = document.querySelector(".sidebar");
    const open = sidebar.classList.toggle("is-open");
    document.getElementById("main").inert = open;
  });

  document.addEventListener("themechange", updateThemeButton);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", updateThemeButton);

  plan.init();
  recipes.init();
  inventory.init();
  products.init();
  settings.init();
  dashboard.init();
  auth.init(async () => {
    store.set("me", await api.me());
    await startSession();
    if (!location.hash || location.hash === "#/") router.go("#/", { replace: true });
  });

  try {
    store.set("me", await api.me());
    await startSession();
  } catch (error) {
    showAuth();
  }
}

boot();
