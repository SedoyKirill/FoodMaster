/* Экран «Настройки».
 *
 * Что чинится по ТЗ Б2:
 *  — люди правятся по одному через PATCH и сохраняют свои id;
 *  — правила питания — список с типом и флагом «строгое»; прежде интерфейс
 *    схлопывал всё в exclude+is_hard, теряя данные при каждом сохранении;
 *  — техника с подсказкой из A2;
 *  — новые секции «Оформление» и «Данные».
 */

import { api } from "../api.js";
import { register } from "../actions.js";
import * as dialog from "../dialog.js";
import * as format from "../format.js";
import * as theme from "../theme.js";
import * as toast from "../toast.js";
import { store } from "../state.js";
import { el, frag, humanError, mount } from "../render.js";

let draftPeople = [];
let draftRules = [];
let planProfile = null;

const MEAL_SLOTS = ["breakfast", "lunch", "dinner"];

function selectField(title, field, options, current) {
  return el("label.field", {}, [
    el("span", { text: title }),
    el(
      "select",
      { dataset: { field } },
      Object.entries(options).map(([value, label]) =>
        el("option", { value, text: label, selected: String(current) === value }),
      ),
    ),
  ]);
}

function numberField(title, field, value, attrs = {}) {
  return el("label.field", {}, [
    el("span", { text: title }),
    el("input", { type: "number", value: value ?? "", dataset: { field }, ...attrs }),
  ]);
}

/* Норма едока показывается вместе с тем, как она получена: «2000 ккал» без
 * пояснения выглядят медицинским заключением, а это среднее по палате. */
function personTargetNote(person) {
  return el("p.footnote.person-card__target", {
    dataset: { personTarget: person.id || "" },
    text: person.id ? "Считаем норму…" : "Норма появится после сохранения.",
  });
}

function personCard(person, index) {
  const isSaved = Boolean(person.id);
  const eats = new Set(person.eats_meals || MEAL_SLOTS);
  return el("div.person-card", { dataset: { index: String(index) } }, [
    el("label.field", {}, [
      el("span", { text: "Имя" }),
      el("input", { type: "text", value: person.name || "", maxLength: 80, dataset: { field: "name" } }),
    ]),
    selectField("Тип", "person_type", { adult: "Взрослый", child: "Ребёнок" }, person.person_type || "adult"),
    el("label.field", {}, [
      el("span", { text: "Дата рождения" }),
      el("input", {
        type: "date", value: person.birth_date || "", dataset: { field: "birth_date" },
      }),
    ]),
    selectField("Пол", "sex", { "": "Не указан", ...format.SEXES }, person.sex || ""),
    numberField("Рост, см", "height_cm", person.height_cm, { min: 30, max: 250 }),
    numberField("Вес, кг", "weight_kg", person.weight_kg, { min: 2, max: 400, step: 0.1 }),
    selectField("Активность", "activity", format.ACTIVITY_LEVELS, person.activity || "moderate"),
    selectField("Цель", "goal", format.GOALS, person.goal || "maintain"),
    numberField("Ккал в день", "target_kcal", person.target_kcal, {
      min: 500, max: 6000, placeholder: "посчитать самим",
    }),
    numberField("Порция", "portion_factor", Number(person.portion_factor ?? 1), {
      min: 0.1, max: 3, step: 0.05,
    }),
    el("fieldset.field.person-card__meals.fieldset", {}, [
      el("legend", { text: "Ест дома" }),
      el(
        "div.chips.chips--tiers",
        {},
        MEAL_SLOTS.map((meal) =>
          el("label.check-option", {}, [
            el("input", {
              type: "checkbox", value: meal, checked: eats.has(meal),
              dataset: { field: "eats_meals" },
            }),
            el("span", { text: format.mealLabel(meal) }),
          ]),
        ),
      ),
    ]),
    personTargetNote(person),
    el("div.person-card__actions", {}, [
      isSaved
        ? el("button.btn.btn--small", {
            type: "button", text: "Сохранить",
            dataset: { action: "settings:patch-person", id: person.id, index: String(index) },
          })
        : null,
      el("button.btn.btn--small.btn--danger", {
        type: "button", text: "Убрать",
        dataset: { action: "settings:remove-person", index: String(index) },
      }),
    ]),
  ]);
}

function ruleRow(rule, index) {
  // «Для кого» (TZ-M8 §3.2): аллергия ребёнка на орехи не должна лишать
  // орехов всю семью — она действует только на те приёмы, где ребёнок ест.
  const people = { "": "Вся семья" };
  for (const person of draftPeople) {
    if (person.id) people[person.id] = person.name || "Без имени";
  }
  return el("div.rule-row", { dataset: { index: String(index) } }, [
    el("label.field", {}, [
      el("span", { text: "Тип" }),
      el(
        "select",
        { dataset: { field: "rule_type" } },
        Object.entries(format.RULE_TYPES).map(([code, title]) =>
          el("option", { value: code, text: title, selected: rule.rule_type === code }),
        ),
      ),
    ]),
    el("label.field", {}, [
      el("span", { text: "Продукт или ингредиент" }),
      el("input", {
        type: "text", value: rule.term || "", maxLength: 100,
        placeholder: "например, арахис", dataset: { field: "term" },
      }),
    ]),
    el("label.field", {}, [
      el("span", { text: "Для кого" }),
      el(
        "select",
        { dataset: { field: "person_id" } },
        Object.entries(people).map(([value, title]) =>
          el("option", { value, text: title, selected: String(rule.person_id || "") === value }),
        ),
      ),
    ]),
    el("label.field", {}, [
      el("span", { text: "Диета" }),
      el("input", {
        type: "text", value: rule.diet_tag || "", maxLength: 40,
        placeholder: "например, vegetarian", dataset: { field: "diet_tag" },
      }),
    ]),
    el("label.switch", {}, [
      el("input", { type: "checkbox", checked: rule.is_hard !== false, dataset: { field: "is_hard" } }),
      el("span", { text: "Строгое" }),
    ]),
    el("button.btn.btn--small.btn--danger", {
      type: "button", text: "Убрать",
      dataset: { action: "settings:remove-rule", index: String(index) },
    }),
  ]);
}

function readPeople() {
  return [...document.querySelectorAll("#settings-people .person-card")].map((card, index) => {
    const value = (field) => card.querySelector(`[data-field="${field}"]`).value;
    const optional = (field) => (value(field) ? Number(value(field)) : null);
    return {
      id: draftPeople[index]?.id || null,
      name: value("name").trim() || `Человек ${index + 1}`,
      person_type: value("person_type"),
      target_kcal: optional("target_kcal"),
      portion_factor: Number(value("portion_factor")) || 1,
      birth_date: value("birth_date") || null,
      sex: value("sex") || null,
      height_cm: optional("height_cm"),
      weight_kg: optional("weight_kg"),
      activity: value("activity"),
      goal: value("goal"),
      eats_meals: [...card.querySelectorAll('[data-field="eats_meals"]:checked')].map(
        (input) => input.value,
      ),
    };
  });
}

function readRules() {
  return [...document.querySelectorAll("#settings-rules .rule-row")]
    .map((row) => ({
      rule_type: row.querySelector('[data-field="rule_type"]').value,
      term: row.querySelector('[data-field="term"]').value.trim(),
      is_hard: row.querySelector('[data-field="is_hard"]').checked,
      person_id: row.querySelector('[data-field="person_id"]').value || null,
      diet_tag: row.querySelector('[data-field="diet_tag"]').value.trim() || null,
    }))
    .filter((rule) => rule.term);
}

/* --- профиль планирования (TZ-M8 §3.4) ------------------------------------- */

function radioGroup(name, options, current, hints = null) {
  return el(
    "div.chips.chips--tiers",
    { role: "radiogroup" },
    Object.entries(options).map(([value, title]) =>
      el("label.check-option", { title: hints ? hints[value] : null }, [
        el("input", { type: "radio", name, value, checked: current === value }),
        el("span", { text: title }),
      ]),
    ),
  );
}

function renderPlanProfile() {
  const profile = planProfile || {};
  const meals = new Set(profile.meals || MEAL_SLOTS);
  mount(
    document.getElementById("settings-plan-profile"),
    frag(
      el("fieldset.fieldset", {}, [
        el("legend", { text: "Режим" }),
        radioGroup("profile-mode", format.PLAN_MODES, profile.mode || "balanced", format.PLAN_MODE_HINTS),
      ]),
      el("div.form-grid", {}, [
        el("label.field", {}, [
          el("span", { text: "Дней по умолчанию" }),
          el(
            "select",
            { dataset: { field: "default_days" } },
            [1, 3, 5, 7, 14].map((days) =>
              el("option", {
                value: String(days),
                text: format.countWord(days, "день", "дня", "дней"),
                selected: Number(profile.default_days) === days,
              }),
            ),
          ),
        ]),
        numberField(
          "Бюджет на неделю, ₽",
          "weekly_budget_kop",
          profile.weekly_budget_kop ? Math.round(profile.weekly_budget_kop / 100) : "",
          { min: 0, step: 100, placeholder: "без ограничения" },
        ),
        selectField("Кухни", "cuisine_mode", format.CUISINE_MODES, profile.cuisine_mode || "only"),
        selectField("Сколько нового", "novelty", format.NOVELTY_LEVELS, profile.novelty || "medium"),
        numberField("Минут на будни", "weekday_max_minutes", profile.weekday_max_minutes, {
          min: 5, max: 600, placeholder: "без лимита",
        }),
        numberField("Минут в выходные", "weekend_max_minutes", profile.weekend_max_minutes, {
          min: 5, max: 600, placeholder: "без лимита",
        }),
        numberField("Минут на завтрак", "breakfast_max_minutes", profile.breakfast_max_minutes, {
          min: 5, max: 600, placeholder: "без лимита",
        }),
        numberField("Повторов блюда за план", "max_repeats_per_horizon", profile.max_repeats_per_horizon ?? 2, {
          min: 1, max: 7,
        }),
      ]),
      el("fieldset.fieldset", {}, [
        el("legend", { text: "Что планируем" }),
        el(
          "div.chips.chips--tiers",
          {},
          MEAL_SLOTS.map((meal) =>
            el("label.check-option", {}, [
              el("input", {
                type: "checkbox", value: meal, checked: meals.has(meal),
                dataset: { field: "profile_meals" },
              }),
              el("span", { text: format.mealLabel(meal) }),
            ]),
          ),
        ),
      ]),
      el("label.check-option", {}, [
        el("input", {
          type: "checkbox", checked: profile.allow_leftovers !== false,
          dataset: { field: "allow_leftovers" },
        }),
        el("span", { text: "Разрешать «готовим на два раза»" }),
      ]),
    ),
  );
}

function readPlanProfile() {
  const root = document.getElementById("settings-plan-profile");
  const value = (field) => root.querySelector(`[data-field="${field}"]`).value;
  const optional = (field) => (value(field) ? Number(value(field)) : null);
  const budget = optional("weekly_budget_kop");
  return {
    mode: root.querySelector('input[name="profile-mode"]:checked')?.value || "balanced",
    default_days: Number(value("default_days")),
    weekly_budget_kop: budget === null ? null : Math.round(budget * 100),
    // Кухни живут в форме плана и на этом экране не редактируются — профиль
    // сохраняет то, что в нём уже есть, а не затирает пустым списком.
    cuisines: (planProfile && planProfile.cuisines) || [],
    cuisine_mode: value("cuisine_mode"),
    novelty: value("novelty"),
    weekday_max_minutes: optional("weekday_max_minutes"),
    weekend_max_minutes: optional("weekend_max_minutes"),
    breakfast_max_minutes: optional("breakfast_max_minutes"),
    max_repeats_per_horizon: Number(value("max_repeats_per_horizon")) || 2,
    meals: [...root.querySelectorAll('[data-field="profile_meals"]:checked')].map(
      (input) => input.value,
    ),
    allow_leftovers: root.querySelector('[data-field="allow_leftovers"]').checked,
  };
}

/** Норма каждого сохранённого едока — с пометкой, как она посчитана. */
async function renderTargets() {
  for (const person of draftPeople) {
    if (!person.id) continue;
    const node = document.querySelector(`[data-person-target="${person.id}"]`);
    if (!node) continue;
    try {
      const target = await api.personTarget(person.id);
      const source = format.TARGET_SOURCES[target.target_source] || target.target_source;
      node.textContent =
        `Норма: ${format.number(target.kcal)} ккал · Б ${target.protein_g}` +
        ` / Ж ${target.fat_g} / У ${target.carb_g} г — ${source}`;
    } catch (error) {
      node.textContent = "Норму посчитать не удалось.";
    }
  }
}

function renderPeople() {
  mount(document.getElementById("settings-people"), frag(...draftPeople.map(personCard)));
}

function renderRules() {
  mount(document.getElementById("settings-rules"), frag(...draftRules.map(ruleRow)));
}

function renderAppliances(selected) {
  const chosen = new Set(selected);
  mount(
    document.getElementById("settings-appliances"),
    frag(
      ...Object.entries(format.APPLIANCES).map(([code, title]) =>
        el("label.check-option", {}, [
          el("input", { type: "checkbox", value: code, checked: chosen.has(code) }),
          el("span", { text: title }),
        ]),
      ),
    ),
  );
}

function renderTheme() {
  const current = theme.getTheme();
  mount(
    document.getElementById("settings-theme"),
    frag(
      ...[
        ["auto", "Как в системе"],
        ["light", "Светлая"],
        ["dark", "Тёмная"],
      ].map(([value, title]) =>
        el("label.check-option", {}, [
          el("input", { type: "radio", name: "theme", value, checked: current === value }),
          el("span", { text: title }),
        ]),
      ),
    ),
  );
}

function renderData() {
  const dashboard = store.get("dashboard") || {};
  const facets = store.get("facets");
  mount(
    document.getElementById("settings-data"),
    frag(
      el("div.stat-row", {}, [
        el("div.stat", {}, [
          el("span.stat__value", { text: format.number(dashboard.recipes ?? 0) }),
          el("span.stat__label", { text: "рецептов в библиотеке" }),
        ]),
        el("div.stat", {}, [
          el("span.stat__value", { text: format.number(dashboard.recipes_ready ?? 0) }),
          el("span.stat__label", { text: "проверено" }),
        ]),
        el("div.stat", {}, [
          el("span.stat__value", { text: format.number(dashboard.sources ?? 0) }),
          el("span.stat__label", { text: "источников обработано" }),
        ]),
        el("div.stat", {}, [
          el("span.stat__value", { text: format.number(dashboard.products ?? 0) }),
          el("span.stat__label", { text: "товаров «Ленты»" }),
        ]),
      ]),
      el("a.btn", { href: "#/recipes?ready=", text: "Очередь проверки рецептов" }),
      facets.cuisines.length
        ? null
        : el("p.footnote", {
            text: "Кухни в библиотеке пока не размечены, поэтому фильтр по кухне скрыт — он появится сам, как только данные появятся.",
          }),
    ),
  );
}

export async function enter() {
  const me = store.get("me");
  if (!me) return;
  document.getElementById("household-name").value = me.household.name;
  draftPeople = me.people.map((person) => ({ ...person }));
  draftRules = me.dietary_rules.map((rule) => ({ ...rule }));
  renderPeople();
  renderRules();
  renderAppliances(me.appliances);
  renderTheme();
  renderData();
  renderAccount(me);
  try {
    planProfile = await api.planProfile();
    store.set("planProfile", planProfile);
  } catch (error) {
    planProfile = null;
  }
  renderPlanProfile();
  await renderTargets();
}

function renderAccount(me) {
  const linked = Boolean(me.telegram_linked);
  const hasPassword = Boolean(me.user.has_password);
  document.getElementById("telegram-status").textContent = linked
    ? `Привязан к аккаунту «${me.user.login}».`
    : "Бот подключается одноразовой командой; без токена он выключен.";
  document.getElementById("telegram-unlink").hidden = !linked;
  document.getElementById("telegram-link").hidden = linked;
  // TZ-M7 §3.4: аккаунт из бота без пароля после отвязки станет недоступен
  document.getElementById("account-status").textContent = hasPassword
    ? "Пароль задан — можно входить и без Telegram."
    : "Пароля нет: аккаунт заведён из бота, вход в браузер идёт по коду (/web).";
}

export function init() {
  document.getElementById("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      household_name: document.getElementById("household-name").value.trim() || "Моя семья",
      people: readPeople(),
      appliances: [...document.querySelectorAll("#settings-appliances input:checked")].map(
        (input) => input.value,
      ),
      dietary_rules: readRules(),
    };
    try {
      await api.saveSettings(payload);
      store.set("me", await api.me());
      enter();
      toast.ok("Настройки сохранены");
    } catch (error) {
      toast.ok(humanError(error));
    }
  });

  register("settings:add-person", () => {
    draftPeople = [...readPeople(), { id: null, name: "", person_type: "adult", portion_factor: 1 }];
    renderPeople();
    // Список «для кого» в правилах строится по людям — он должен успевать.
    renderRules();
  });

  register("settings:remove-person", (target) => {
    const index = Number(target.dataset.index);
    draftPeople = readPeople().filter((_person, position) => position !== index);
    renderPeople();
  });

  register("settings:patch-person", async (target) => {
    const index = Number(target.dataset.index);
    const person = readPeople()[index];
    target.disabled = true;
    try {
      const { id: _id, ...changes } = person;
      const updated = await api.patchPerson(target.dataset.id, changes);
      store.update("me.people", (people) =>
        people.map((item) => (item.id === updated.id ? updated : item)),
      );
      toast.ok(`«${updated.name}» сохранён`);
    } catch (error) {
      toast.ok(humanError(error));
    } finally {
      target.disabled = false;
    }
  });

  register("settings:add-rule", () => {
    draftRules = [...readRules(), { rule_type: "exclude", term: "", is_hard: true }];
    renderRules();
  });

  register("settings:remove-rule", (target) => {
    const index = Number(target.dataset.index);
    // Читаем без фильтра по пустому продукту: убрать надо именно эту строку,
    // а недозаполненные соседние должны остаться на месте.
    draftRules = [...document.querySelectorAll("#settings-rules .rule-row")]
      .map((row) => ({
        rule_type: row.querySelector('[data-field="rule_type"]').value,
        term: row.querySelector('[data-field="term"]').value,
        is_hard: row.querySelector('[data-field="is_hard"]').checked,
        person_id: row.querySelector('[data-field="person_id"]').value || null,
        diet_tag: row.querySelector('[data-field="diet_tag"]').value.trim() || null,
      }))
      .filter((_rule, position) => position !== index);
    renderRules();
  });

  document.getElementById("plan-profile-save").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      planProfile = await api.savePlanProfile(readPlanProfile());
      store.set("planProfile", planProfile);
      renderPlanProfile();
      toast.ok("Настройки планирования сохранены");
    } catch (error) {
      toast.ok(humanError(error));
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("settings-theme").addEventListener("change", (event) => {
    if (event.target.name === "theme") theme.setTheme(event.target.value);
  });

  document.getElementById("telegram-link").addEventListener("click", async () => {
    try {
      const link = await api.telegramToken();
      const node = document.getElementById("telegram-command");
      node.textContent = link.command;
      node.hidden = false;
    } catch (error) {
      toast.ok(humanError(error));
    }
  });

  document.getElementById("telegram-unlink").addEventListener("click", async () => {
    const me = store.get("me");
    const confirmed = await dialog.confirm({
      title: "Отвязать Telegram?",
      text: me?.user?.has_password
        ? "Бот перестанет присылать меню. Данные семьи останутся на месте."
        : "У аккаунта нет пароля — после отвязки войти будет нечем. Сначала задайте пароль ниже.",
      confirmLabel: "Отвязать",
    });
    if (!confirmed) return;
    try {
      await api.telegramUnlink();
      store.set("me", await api.me());
      enter();
      toast.ok("Telegram отвязан");
    } catch (error) {
      document.getElementById("telegram-error").textContent = humanError(error);
    }
  });

  document.getElementById("account-save-password").addEventListener("click", async () => {
    const input = document.getElementById("account-password");
    const error = document.getElementById("account-password-error");
    error.textContent = "";
    if (input.value.length < 8) {
      error.textContent = "Пароль: не менее 8 символов.";
      return;
    }
    try {
      await api.setPassword(input.value);
      input.value = "";
      store.set("me", await api.me());
      enter();
      toast.ok("Пароль сохранён");
    } catch (requestError) {
      error.textContent = humanError(requestError);
    }
  });
}
