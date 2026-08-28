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
import * as format from "../format.js";
import * as theme from "../theme.js";
import * as toast from "../toast.js";
import { store } from "../state.js";
import { el, frag, humanError, mount } from "../render.js";

let draftPeople = [];
let draftRules = [];

function personCard(person, index) {
  const isSaved = Boolean(person.id);
  return el("div.person-card", { dataset: { index: String(index) } }, [
    el("label.field", {}, [
      el("span", { text: "Имя" }),
      el("input", { type: "text", value: person.name || "", maxLength: 80, dataset: { field: "name" } }),
    ]),
    el("label.field", {}, [
      el("span", { text: "Тип" }),
      el("select", { dataset: { field: "person_type" } }, [
        el("option", { value: "adult", text: "Взрослый", selected: person.person_type !== "child" }),
        el("option", { value: "child", text: "Ребёнок", selected: person.person_type === "child" }),
      ]),
    ]),
    el("label.field", {}, [
      el("span", { text: "Ккал в день" }),
      el("input", {
        type: "number", min: 500, max: 6000, placeholder: "не задано",
        value: person.target_kcal ?? "", dataset: { field: "target_kcal" },
      }),
    ]),
    el("label.field", {}, [
      el("span", { text: "Порция" }),
      el("input", {
        type: "number", min: 0.1, max: 3, step: 0.05,
        value: Number(person.portion_factor ?? 1), dataset: { field: "portion_factor" },
      }),
    ]),
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
    return {
      id: draftPeople[index]?.id || null,
      name: value("name").trim() || `Человек ${index + 1}`,
      person_type: value("person_type"),
      target_kcal: value("target_kcal") ? Number(value("target_kcal")) : null,
      portion_factor: Number(value("portion_factor")) || 1,
    };
  });
}

function readRules() {
  return [...document.querySelectorAll("#settings-rules .rule-row")]
    .map((row) => ({
      rule_type: row.querySelector('[data-field="rule_type"]').value,
      term: row.querySelector('[data-field="term"]').value.trim(),
      is_hard: row.querySelector('[data-field="is_hard"]').checked,
    }))
    .filter((rule) => rule.term);
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

export function enter() {
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
      const updated = await api.patchPerson(target.dataset.id, {
        name: person.name,
        person_type: person.person_type,
        target_kcal: person.target_kcal,
        portion_factor: person.portion_factor,
      });
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
    draftRules = [...document.querySelectorAll("#settings-rules .rule-row")]
      .map((row) => ({
        rule_type: row.querySelector('[data-field="rule_type"]').value,
        term: row.querySelector('[data-field="term"]').value,
        is_hard: row.querySelector('[data-field="is_hard"]').checked,
      }))
      .filter((_rule, position) => position !== index);
    renderRules();
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
}
