/* Экран «План питания».
 *
 * Что чинится по ТЗ Б2:
 *  — история планов вместо единственного «последнего»;
 *  — список покупок стал таблицей с группировкой по категориям и итогом;
 *    прежде это была строка фактов через « · » в одном <small>;
 *  — отметка «куплено» переживает перезагрузку;
 *  — предупреждение о бюджете показывается только при реальном превышении;
 *  — при ошибке генерации экран не пустеет, а показывает плашку с «Повторить».
 */

import { api } from "../api.js";
import { register } from "../actions.js";
import * as dialog from "../dialog.js";
import * as format from "../format.js";
import * as router from "../router.js";
import * as toast from "../toast.js";
import { canEdit, store } from "../state.js";
import { badge, el, frag, humanError, load, metaList, mount, statePanel } from "../render.js";

const MEAL_SLOTS = ["breakfast", "lunch", "dinner"];
const selectedCuisines = new Set();
let profileApplied = false;

/* Форма плана предзаполняется профилем семьи (TZ-M8 §3.4): вводить кухни,
 * бюджет и режим заново при каждой генерации — ровно та работа, ради отмены
 * которой профиль и заводился. Правки в форме действуют на один план и
 * обратно в профиль не пишутся. */
function renderModeChoices(current) {
  const hint = document.getElementById("plan-mode-hint");
  hint.textContent = format.PLAN_MODE_HINTS[current] || "";
  mount(
    document.getElementById("plan-modes"),
    frag(
      ...Object.entries(format.PLAN_MODES).map(([code, title]) =>
        el("label.check-option", { title: format.PLAN_MODE_HINTS[code] }, [
          el("input", { type: "radio", name: "plan-mode", value: code, checked: code === current }),
          el("span", { text: title }),
        ]),
      ),
    ),
  );
}

function renderMealChoices(selected) {
  const chosen = new Set(selected);
  mount(
    document.getElementById("plan-meals"),
    frag(
      ...MEAL_SLOTS.map((meal) =>
        el("label.check-option", {}, [
          el("input", { type: "checkbox", name: "plan-meal", value: meal, checked: chosen.has(meal) }),
          el("span", { text: format.mealLabel(meal) }),
        ]),
      ),
    ),
  );
}

async function applyProfile() {
  let profile = store.get("planProfile");
  if (!profile) {
    try {
      profile = await api.planProfile();
      store.set("planProfile", profile);
    } catch (error) {
      profile = null;
    }
  }
  renderModeChoices(profile?.mode || "balanced");
  renderMealChoices(profile?.meals || MEAL_SLOTS);
  document.getElementById("plan-leftovers").checked = profile?.allow_leftovers !== false;
  // Значения формы перетираются профилем один раз за сессию: если человек
  // уже поменял «на 3 дня», возврат на экран не должен это отменять.
  if (profile && !profileApplied) {
    document.getElementById("plan-days").value = String(profile.default_days || 7);
    if (profile.weekly_budget_kop) {
      const days = Number(profile.default_days || 7);
      document.getElementById("plan-budget").value = String(
        Math.round((profile.weekly_budget_kop / 100) * (days / 7)),
      );
    }
    for (const code of profile.cuisines || []) selectedCuisines.add(code);
    profileApplied = true;
  }
}

/* Кухни размечены — рисуем чипы-переключатели в форме; пустые фасеты
 * оставляют блок скрытым (как в фильтрах рецептов). */
function renderCuisineChoices() {
  const facets = store.get("facets") || {};
  const cuisines = facets.cuisines || [];
  const block = document.getElementById("plan-cuisines-block");
  block.hidden = cuisines.length === 0;
  if (!cuisines.length) return;
  mount(
    document.getElementById("plan-cuisines"),
    frag(
      ...cuisines.map((code) =>
        el("button.chip", {
          type: "button",
          text: format.cuisineLabel(code) || code,
          "aria-pressed": String(selectedCuisines.has(code)),
          dataset: { action: "plan:cuisine", cuisine: code },
        }),
      ),
    ),
  );
}

/* --- история ---------------------------------------------------------------- */

function historyItem(plan, activeId) {
  return el("li", {}, [
    el(
      "a",
      { href: `#/plan/${plan.id}`, "aria-current": plan.id === activeId ? "true" : null },
      [
        el("span", { text: `С ${format.dateLong(plan.starts_on) || plan.starts_on}` }),
        metaList([
          format.countWord(plan.days, "день", "дня", "дней"),
          format.money(plan.estimated_cost_kop),
        ]),
        plan.meal_count === 0 ? badge("без блюд", "draft") : null,
      ],
    ),
    canEdit()
      ? el("button.icon-btn.icon-btn--danger.btn--small", {
          type: "button",
          "aria-label": `Удалить план от ${format.dateShort(plan.starts_on) || plan.starts_on}`,
          dataset: { action: "plan:delete", id: plan.id },
        }, [])
      : null,
  ]);
}

function renderHistory() {
  const container = document.getElementById("plan-history-list");
  const { list, activeId } = store.get("plans");
  const panel = document.getElementById("plan-history");
  panel.hidden = list.length === 0;
  if (!list.length) return;
  mount(container, frag(...list.map((plan) => historyItem(plan, activeId))));
  // Кнопки-иконки строятся без текста — иконку вставляем после монтирования.
  import("../icons.js").then(({ icon }) => {
    for (const button of container.querySelectorAll('[data-action="plan:delete"]')) {
      if (!button.firstChild) button.append(icon("trash", { size: 18 }));
    }
  });
}

/* --- дни и блюда -------------------------------------------------------------- */

const MEAL_WARNING_BADGES = {
  draft: ["черновик", "draft"],
  scale_unknown: ["порции как в книге", "draft"],
  // Кухня выбрана, но подходящих блюд на слот не хватило — планировщик
  // подставил другое и говорит об этом прямо, а не молча.
  cuisine_fallback: ["кухня не совпала", "draft"],
};

function warningBadge(code) {
  if (MEAL_WARNING_BADGES[code]) return badge(...MEAL_WARNING_BADGES[code]);
  // «kcal_partial:3/10» — калории посчитаны не по всем ингредиентам.
  const partial = /^kcal_partial:(\d+)\/(\d+)$/.exec(code);
  if (partial) return badge(`ккал: ${partial[1]} из ${partial[2]} ингр.`, "draft");
  return null;
}

function mealBadges(meal) {
  const warnings = Array.isArray(meal.warnings) ? meal.warnings : [];
  const badges = warnings.map(warningBadge).filter(Boolean);
  // «На два раза» (§6.2): у наследника — что готовить не нужно, у источника —
  // что порций больше, чем едоков за этим столом.
  if (meal.leftover_of) badges.unshift(badge("остаток вчерашнего", "ok"));
  else if (meal.cooks_ahead) badges.unshift(badge("на два раза", "ok"));
  return badges.length ? el("div.slot__badges", {}, badges) : null;
}

/* «Почему это блюдо» (§5): коды с параметрами, текст собирает format. */
function mealReasons(meal) {
  const texts = (Array.isArray(meal.reasons) ? meal.reasons : [])
    .map(format.reasonText)
    .filter(Boolean);
  if (!texts.length) return null;
  return el("ul.slot__reasons", {}, texts.map((text) => el("li", { text })));
}

/* Отметки «приготовили / пропустили» — самый честный сигнал вкуса (§4.1).
 * Предлагаются только для прошедших слотов: спрашивать про завтрашний ужин
 * бессмысленно, а молчание мнением не считается. */
function mealStatusControls(plan, meal) {
  const today = new Date().toISOString().slice(0, 10);
  if (!meal.id || !canEdit() || String(meal.meal_date) > today) return null;
  if (meal.status) {
    return el("span.slot__status.footnote", {
      text: meal.status === "cooked" ? "Приготовили" : "Пропустили",
    });
  }
  return el("span.slot__status", {}, [
    el("button.btn.btn--small", {
      type: "button", text: "Приготовили",
      dataset: { action: "plan:cooked", planId: plan.id, mealId: meal.id, status: "cooked" },
    }),
    el("button.btn.btn--small", {
      type: "button", text: "Пропустили",
      dataset: { action: "plan:cooked", planId: plan.id, mealId: meal.id, status: "skipped" },
    }),
  ]);
}

function slot(plan, mealType, meal) {
  const canSwap = Boolean(meal && meal.id && canEdit());
  return el("div.slot", {}, [
    el("span.slot__label", { text: format.mealLabel(mealType) }),
    meal
      ? frag(
          el("a.slot__dish", { href: `#/recipes/${meal.recipe_id}`, text: meal.title }),
          mealBadges(meal),
          mealReasons(meal),
          el("div.slot__foot", {}, [
            metaList([
              meal.servings ? format.countWord(meal.servings, "порция", "порции", "порций") : null,
              meal.estimated_kcal != null
                ? `≈ ${format.number(meal.estimated_kcal)} ккал на всё блюдо`
                : "нет оценки ккал",
              meal.estimated_protein !== null && meal.estimated_protein !== undefined
                ? `Б/Ж/У ${meal.estimated_protein}/${meal.estimated_fat}/${meal.estimated_carb} г`
                : null,
            ]),
            mealStatusControls(plan, meal),
            el("button.btn.btn--small", {
              type: "button",
              text: "Заменить",
              disabled: !canSwap,
              dataset: canSwap
                ? { action: "plan:swap", planId: plan.id, mealId: meal.id }
                : null,
              "aria-describedby": "plan-swap-hint",
            }),
          ]),
        )
      : el("span.footnote", { text: "не запланировано — не хватило подходящих рецептов" }),
  ]);
}

function dayCard(plan, date, meals) {
  const byType = new Map(meals.map((meal) => [meal.meal_type, meal]));
  const title = [format.weekday(date), format.dateLong(date)].filter(Boolean).join(", ");
  // Итог дня: сумма известных ккал и Б/Ж/У; помечаем, если посчитаны не все блюда.
  const counted = meals.filter((meal) => meal.estimated_kcal != null);
  const dayKcal = counted.reduce((sum, meal) => sum + Number(meal.estimated_kcal), 0);
  const withMacros = meals.filter((meal) => meal.estimated_protein != null);
  const sumOf = (key) => withMacros.reduce((sum, meal) => sum + Number(meal[key] || 0), 0);
  const macroNote = withMacros.length
    ? ` · Б ${sumOf("estimated_protein")} / Ж ${sumOf("estimated_fat")} / У ${sumOf("estimated_carb")} г`
    : "";
  const kcalNote = counted.length
    ? `≈ ${format.number(dayKcal)} ккал/день${macroNote}${counted.length < meals.length ? ` (по ${counted.length} из ${meals.length} блюд)` : ""}`
    : null;
  return el("li.day", {}, [
    el("h3.day__title", { text: title || date }),
    kcalNote ? el("p.footnote", { text: kcalNote }) : null,
    ...MEAL_SLOTS.map((mealType) => slot(plan, mealType, byType.get(mealType))),
  ]);
}

function daysView(plan) {
  const byDate = new Map();
  for (const meal of plan.meals) {
    if (!byDate.has(meal.meal_date)) byDate.set(meal.meal_date, []);
    byDate.get(meal.meal_date).push(meal);
  }
  return el(
    "ol.plan-days",
    {},
    [...byDate.entries()].map(([date, meals]) => dayCard(plan, date, meals)),
  );
}

/* --- список покупок ------------------------------------------------------------ */

function shoppingRow(plan, item) {
  const need =
    item.buy_quantity === null
      ? item.to_taste
        ? "по вкусу"
        : "количество уточнить"
      : Number(item.buy_quantity) === 0
        ? "не нужно покупать"
        : format.quantity({ quantity_min: item.buy_quantity, unit_code: item.unit_code });
  const covered = Number(item.covered_from_inventory) > 0
    ? format.quantity({ quantity_min: item.covered_from_inventory, unit_code: item.unit_code })
    : "";

  return el(`tr${item.purchased_at ? ".is-purchased" : ""}`, { dataset: { itemId: item.id } }, [
    el("td.cell--check", {}, [
      el("input", {
        type: "checkbox",
        checked: Boolean(item.purchased_at),
        disabled: !canEdit(),
        "aria-label": `Отметить «${item.normalized_name}» купленным`,
        dataset: { change: "plan:purchase", planId: plan.id, itemId: item.id },
      }),
    ]),
    el("td.cell--name", { text: item.normalized_name }),
    el("td.qty", { text: need }),
    el("td.qty", { text: covered }),
    el("td", {}, [
      item.matched_product_name
        ? item.matched_product_url
          ? el("a", {
              href: item.matched_product_url,
              target: "_blank",
              rel: "noopener",
              text: item.matched_product_name,
              title: "Открыть товар на lenta.com",
            })
          : el("span", { text: item.matched_product_name })
        : null,
    ]),
    el("td.num.money", {}, [
      el("div", { text: format.money(item.estimated_cost_kop) || "" }),
      item.pack_count
        ? el("div.footnote", { text: format.countWord(item.pack_count, "упаковка", "упаковки", "упаковок") })
        : null,
    ]),
  ]);
}

function shoppingTable(plan) {
  if (!plan.shopping.length) {
    return statePanel({
      kind: "empty",
      iconName: "basket",
      title: "Список покупок пуст",
      text: "Все продукты уже есть дома либо блюда не удалось разложить на ингредиенты.",
    });
  }

  const groups = new Map();
  for (const item of plan.shopping) {
    const key = item.category_slug || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  const ordered = [...groups.entries()].sort((left, right) => {
    if (!left[0]) return 1;
    if (!right[0]) return -1;
    return format.categoryLabel(left[0]).localeCompare(format.categoryLabel(right[0]), "ru");
  });

  const body = [];
  for (const [slug, items] of ordered) {
    if (groups.size > 1) {
      body.push(
        el("tr.row--section", {}, [
          el("th", {
            scope: "colgroup",
            colSpan: 6,
            // Без сопоставления с каталогом — отдельная секция: сюда попадают
            // и составные ингредиенты книг («готовая грудка в меду» — это
            // блюдо из другого рецепта книги, а не товар с полки).
            text: slug ? format.categoryLabel(slug) : "Уточнить в магазине",
          }),
        ]),
      );
    }
    body.push(...items.map((item) => shoppingRow(plan, item)));
  }

  return el("div.table-wrap", {}, [
    el("table.table.table--shopping", {}, [
      el("caption.visually-hidden", { text: "Список покупок" }),
      el("thead", {}, [
        el("tr", {}, [
          el("th", { scope: "col" }, [el("span.visually-hidden", { text: "Куплено" })]),
          el("th", { scope: "col", text: "Продукт" }),
          el("th", { scope: "col", text: "Нужно купить" }),
          el("th", { scope: "col", text: "Есть дома" }),
          el("th", { scope: "col", text: "Товар «Ленты»" }),
          el("th.num", { scope: "col", text: "Цена" }),
        ]),
      ]),
      el("tbody", {}, body),
      el("tfoot", {}, [
        el("tr", {}, [
          el("th", { scope: "row", colSpan: 5, text: "Итого" }),
          el("td.num.money", { text: format.money(plan.estimated_cost_kop) || "—" }),
        ]),
      ]),
    ]),
  ]);
}

/* --- план целиком --------------------------------------------------------------- */

function planView(plan) {
  const overBudget = plan.budget_kop && plan.estimated_cost_kop > plan.budget_kop;
  const coverage = plan.total_cost_items
    ? Math.round((plan.matched_cost_items / plan.total_cost_items) * 100)
    : 0;

  return el("div.plan-body", {}, [
    el("div.plan-summary", {}, [
      el("div", {}, [
        el("p", { text: "Предварительная стоимость" }),
        el("strong", {
          text: `${plan.matched_cost_items} из ${plan.total_cost_items} позиций сопоставлены с «Лентой»`,
        }),
        metaList([
          `${coverage}% покрытия`,
          format.planModeLabel(plan.mode),
        ]),
      ]),
      el("div.plan-summary__price.money", { text: format.money(plan.estimated_cost_kop) || "—" }),
    ]),

    overBudget
      ? el("div.banner.banner--warn", { role: "status" }, [
          el("div.banner__body", {}, [
            el("strong", { text: "Бюджет превышен" }),
            el("span", {
              text: `Оценка ${format.money(plan.estimated_cost_kop)} против бюджета ${format.money(plan.budget_kop)}.`,
            }),
          ]),
        ])
      : null,

    plan.meals.length === 0
      ? el("div.banner", {}, [
          el("div.banner__body", {}, [
            el("strong", { text: "Блюда этого плана не сохранились" }),
            el("span", {
              text: "Библиотека рецептов переимпортировалась, и связи с блюдами потерялись. Список покупок остался — составьте меню заново.",
            }),
            el("a.btn.btn--action", { href: "#/plan", text: "Составить меню" }),
          ]),
        ])
      : daysView(plan),

    plan.meals.length
      ? el("p.footnote", {
          id: "plan-swap-hint",
          text: "«Заменить» предложит три альтернативы без повторов в соседние дни. Порции пересчитаны на семью; блюда «как в книге» — без указанных в книге порций.",
        })
      : null,

    el("section.panel", {}, [
      el("div.panel__head", {}, [el("h2", { text: "Список покупок" })]),
      shoppingTable(plan),
      el("p.footnote", {
        text: "Стоимость рассчитана только для сопоставленных товаров текущего каталога. Калорийность — приблизительная оценка и есть не у всех блюд.",
      }),
    ]),
  ]);
}

/* --- ход сборки ------------------------------------------------------------------- */

/* N1: сервер собирает меню за секунды, а на остывших кэшах — заметно дольше.
 * Раньше на это время экран не менялся вовсе: неподвижная кнопка «Собираем
 * меню…» выглядела как зависшая программа, и пользователь перезагружал
 * страницу. Теперь виден и ход работы, и сколько она уже идёт. */
const SLOW_GENERATION_SECONDS = 15;

function generationProgress() {
  const elapsed = el("span", { text: "0 с" });
  const hint = el("p.footnote", { text: "" });
  const panel = el("div.state.state--busy", { role: "status", "aria-live": "polite" }, [
    el("p.state__title", { text: "Собираем меню…" }),
    el("p.state__text", {
      text: "Подбираем блюда под ваши ограничения, пересчитываем порции и цены «Ленты».",
    }),
    el("div.state__progress", { "aria-hidden": "true" }),
    el("p.footnote", {}, ["Идёт ", elapsed]),
    hint,
  ]);
  const started = performance.now();
  const timer = setInterval(() => {
    const seconds = Math.round((performance.now() - started) / 1000);
    elapsed.textContent = `${seconds} с`;
    if (seconds >= SLOW_GENERATION_SECONDS && !hint.textContent) {
      hint.textContent =
        "Первая сборка после долгого перерыва идёт дольше: прогреваются кэши рецептов и каталога.";
    }
  }, 1000);
  return { panel, stop: () => clearInterval(timer) };
}

/* --- загрузка ------------------------------------------------------------------- */

async function loadHistory() {
  try {
    const response = await api.plans(20);
    store.set("plans.list", response.items);
  } catch (error) {
    store.set("plans.list", []);
  }
  renderHistory();
}

async function showPlan(planId) {
  const output = document.getElementById("plan-output");
  await load(
    output,
    async () => {
      const plan = planId ? await api.plan(planId) : await api.latestPlan();
      if (plan) {
        store.set(`plans.byId.${plan.id}`, plan);
        store.set("plans.activeId", plan.id);
        renderHistory();
      }
      return plan;
    },
    planView,
    {
      skeleton: { variant: "row", count: 4 },
      errorTitle: "План не открылся",
      empty: {
        iconName: "calendar",
        title: "Плана ещё нет",
        text: "Заполните форму выше — планировщик подберёт блюда на выбранные дни и соберёт список покупок с ценами «Ленты».",
        action: { label: "Заполнить форму", onClick: () => document.getElementById("plan-start").focus() },
      },
    },
  );
}

export async function enter(planId = null) {
  document.getElementById("plan-start").value ||= new Date().toISOString().slice(0, 10);
  await applyProfile();
  renderCuisineChoices();
  await loadHistory();
  await showPlan(planId);
}

/* --- инициализация --------------------------------------------------------------- */

export function init() {
  document.getElementById("plan-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("plan-generate");
    const budget = document.getElementById("plan-budget").value;
    const payload = {
      starts_on: document.getElementById("plan-start").value,
      days: Number(document.getElementById("plan-days").value),
      cuisines: [...selectedCuisines],
      mode: document.querySelector('input[name="plan-mode"]:checked')?.value || "balanced",
      meals: [...document.querySelectorAll('input[name="plan-meal"]:checked')].map(
        (input) => input.value,
      ),
      allow_leftovers: document.getElementById("plan-leftovers").checked,
      budget_rub: budget ? Number(budget) : null,
    };
    button.disabled = true;
    button.textContent = "Собираем меню…";
    const progress = generationProgress();
    mount(document.getElementById("plan-output"), progress.panel);
    try {
      const plan = await api.generatePlan(payload);
      store.set(`plans.byId.${plan.id}`, plan);
      await loadHistory();
      router.go(`#/plan/${plan.id}`);
      toast.ok("Меню составлено");
    } catch (error) {
      mount(
        document.getElementById("plan-output"),
        statePanel({
          kind: "error",
          iconName: "alert",
          title: "Меню не собралось",
          text:
            error.status === 422
              ? `${humanError(error)} ${error.detail || ""}`.trim()
              : humanError(error),
          action: { label: "Настроить ограничения", href: "#/settings" },
        }),
      );
    } finally {
      progress.stop();
      button.disabled = false;
      button.textContent = "Составить меню";
    }
  });

  document.getElementById("plan-modes").addEventListener("change", (event) => {
    if (event.target.name !== "plan-mode") return;
    document.getElementById("plan-mode-hint").textContent =
      format.PLAN_MODE_HINTS[event.target.value] || "";
  });

  register("plan:cooked", async (target) => {
    const { planId, mealId, status } = target.dataset;
    target.disabled = true;
    try {
      await api.setMealStatus(planId, mealId, status);
      const plan = store.get(`plans.byId.${planId}`);
      const meal = plan?.meals?.find((entry) => entry.id === mealId);
      if (meal) meal.status = status;
      mount(
        target.parentElement,
        el("span.footnote", { text: status === "cooked" ? "Приготовили" : "Пропустили" }),
      );
      toast.ok(
        status === "cooked"
          ? "Запомнили: это блюдо у вас пошло"
          : "Запомнили: это блюдо не пригодилось",
      );
    } catch (error) {
      target.disabled = false;
      toast.ok(humanError(error));
    }
  });

  register("plan:cuisine", (target) => {
    const code = target.dataset.cuisine;
    if (selectedCuisines.has(code)) selectedCuisines.delete(code);
    else selectedCuisines.add(code);
    target.setAttribute("aria-pressed", String(selectedCuisines.has(code)));
  });

  register("plan:swap", async (target) => {
    const { planId, mealId } = target.dataset;
    target.disabled = true;
    try {
      const { alternatives } = await api.replaceMeal(planId, mealId);
      if (!alternatives.length) {
        toast.ok("Подходящих замен не нашлось — ослабьте ограничения.");
        return;
      }
      dialog.open(
        el("div.dialog__body", {}, [
          el("h2", { id: "swap-title", text: "Чем заменить блюдо?" }),
          el("ul.swap-options", {}, alternatives.map((alt) =>
            el("li", {}, [
              el("button.btn.swap-option", {
                type: "button",
                dataset: { action: "plan:swap-apply", planId, mealId, recipeId: alt.recipe_id },
              }, [
                el("span", { text: alt.title }),
                alt.reason ? el("small.swap-option__why", { text: format.reasonText(alt.reason) || "" }) : null,
                alt.draft ? badge("черновик", "draft") : null,
              ]),
            ]),
          )),
          el("button.btn.btn--small", {
            type: "button",
            text: "Отмена",
            onClick: () => dialog.closeSilently(),
          }),
        ]),
        { labelledBy: "swap-title" },
      );
    } catch (error) {
      toast.ok(humanError(error));
    } finally {
      target.disabled = false;
    }
  });

  register("plan:swap-apply", async (target) => {
    const { planId, mealId, recipeId } = target.dataset;
    target.disabled = true;
    try {
      const plan = await api.replaceMeal(planId, mealId, Number(recipeId));
      dialog.closeSilently();
      store.set(`plans.byId.${plan.id}`, plan);
      await showPlan(plan.id);
      toast.ok("Блюдо заменено, список покупок пересобран");
    } catch (error) {
      target.disabled = false;
      toast.ok(humanError(error));
    }
  });

  register("plan:purchase", async (target) => {
    const { planId, itemId } = target.dataset;
    const purchased = target.checked;
    const row = target.closest("tr");
    row.classList.toggle("is-purchased", purchased);
    try {
      await api.markPurchased(planId, itemId, purchased);
      const plan = store.get(`plans.byId.${planId}`);
      if (plan) {
        const item = plan.shopping.find((entry) => entry.id === itemId);
        if (item) item.purchased_at = purchased ? new Date().toISOString() : null;
      }
    } catch (error) {
      target.checked = !purchased;
      row.classList.toggle("is-purchased", !purchased);
      toast.ok(humanError(error));
    }
  });

  register("plan:delete", async (target) => {
    const planId = target.dataset.id;
    const confirmed = await dialog.confirm({
      title: "Удалить план?",
      text: "Вместе с планом исчезнут блюда и список покупок. Отменить это нельзя.",
    });
    if (!confirmed) return;
    try {
      await api.deletePlan(planId);
      store.update("plans.list", (list) => list.filter((plan) => plan.id !== planId));
      renderHistory();
      toast.ok("План удалён");
      if (store.get("plans.activeId") === planId) router.go("#/plan");
      else renderHistory();
    } catch (error) {
      toast.ok(humanError(error));
    }
  });
}
