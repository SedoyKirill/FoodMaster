/* Экран «Запасы».
 *
 * Что чинится по ТЗ Б2:
 *  — таблица вместо « · »-строк; продукт, количество, место и срок — свои колонки;
 *  — сортировка по сроку с aria-sort;
 *  — удаление через отложенную операцию с undo-тостом вместо native confirm();
 *  — пресеты частых продуктов; пустое состояние объясняет, зачем это нужно.
 */

import { api } from "../api.js";
import { register } from "../actions.js";
import * as format from "../format.js";
import * as toast from "../toast.js";
import { canEdit, store } from "../state.js";
import { el, frag, humanError, load, mount, pill, statePanel } from "../render.js";
import { icon } from "../icons.js";

const PRESETS = [
  { name: "Молоко", quantity: 1, unit_code: "l", storage_area: "fridge" },
  { name: "Яйца", quantity: 10, unit_code: "piece", storage_area: "fridge" },
  { name: "Хлеб", quantity: 1, unit_code: "piece", storage_area: "pantry" },
  { name: "Куриное филе", quantity: 500, unit_code: "g", storage_area: "fridge" },
  { name: "Рис", quantity: 900, unit_code: "g", storage_area: "pantry" },
  { name: "Картофель", quantity: 2, unit_code: "kg", storage_area: "pantry" },
  { name: "Сыр", quantity: 200, unit_code: "g", storage_area: "fridge" },
  { name: "Сливочное масло", quantity: 180, unit_code: "g", storage_area: "fridge" },
];

/** Отложенные удаления: пока тост не истёк, запрос не уходит. */
const pendingDeletes = new Map();

/** Срок годности: бейдж плюс отдельная строка «сколько осталось».
 *  Цвет не единственный сигнал — есть иконка и текст. */
function expiryCell(item) {
  if (!item.expires_on) return pill("без срока");
  const days = format.daysUntil(item.expires_on);
  const date = format.dateShort(item.expires_on);
  if (days < 0) return el("div", {}, [
    pill(`просрочен ${date}`, "expired", "alert"),
    el("div.footnote", { text: format.countWord(-days, "день", "дня", "дней") + " назад" }),
  ]);
  if (days <= 3) return el("div", {}, [
    pill(`до ${date}`, "soon", "alert"),
    el("div.footnote", {
      text: days === 0 ? "истекает сегодня" : `осталось ${format.countWord(days, "день", "дня", "дней")}`,
    }),
  ]);
  return pill(`до ${date}`);
}

function sortItems(items, key) {
  const copy = [...items];
  if (key === "name") {
    copy.sort((left, right) => String(left.name).localeCompare(String(right.name), "ru"));
    return copy;
  }
  copy.sort((left, right) => {
    if (!left.expires_on) return 1;
    if (!right.expires_on) return -1;
    return left.expires_on.localeCompare(right.expires_on);
  });
  return copy;
}

function table(items) {
  const sortKey = store.get("inventory.sort");
  return el("div.table-wrap", {}, [
    el("table.table.table--inventory", {}, [
      el("caption.visually-hidden", { text: "Запасы дома" }),
      el("thead", {}, [
        el("tr", {}, [
          el("th", { scope: "col", "aria-sort": sortKey === "name" ? "ascending" : "none" }, [
            el("button.table__sort", {
              type: "button",
              text: "Продукт",
              dataset: { action: "inventory:sort", key: "name" },
            }),
          ]),
          el("th.num", { scope: "col", text: "Количество" }),
          el("th", { scope: "col", text: "Где хранится" }),
          el("th", { scope: "col", "aria-sort": sortKey === "expires" ? "ascending" : "none" }, [
            el("button.table__sort", {
              type: "button",
              text: "Годен до",
              dataset: { action: "inventory:sort", key: "expires" },
            }),
          ]),
          el("th", { scope: "col" }, [el("span.visually-hidden", { text: "Действия" })]),
        ]),
      ]),
      el(
        "tbody",
        {},
        sortItems(items, sortKey).map((item) =>
          el("tr", {}, [
            el("td", { text: item.name }),
            el("td.num.qty", {
              text: format.quantity({ quantity_min: item.quantity, unit_code: item.unit_code }) || "",
            }),
            el("td", { text: format.STORAGE_AREAS[item.storage_area] || "" }),
            el("td", {}, [expiryCell(item)]),
            el("td", {}, [
              canEdit()
                ? el("button.icon-btn.icon-btn--danger", {
                    type: "button",
                    "aria-label": `Удалить «${item.name}» из запасов`,
                    dataset: { action: "inventory:delete", id: item.id },
                  }, [icon("trash", { size: 18 })])
                : null,
            ]),
          ]),
        ),
      ),
    ]),
  ]);
}

function renderList() {
  const items = store.get("inventory.items");
  const container = document.getElementById("inventory-list");
  if (!items.length) {
    mount(
      container,
      statePanel({
        kind: "empty",
        iconName: "fridge",
        title: "Запасы пока пусты",
        text: "Добавьте то, что уже лежит дома: планировщик сначала израсходует продукты с ближним сроком и не отправит вас за ними в магазин.",
        action: { label: "Добавить первый продукт", onClick: () => document.getElementById("inventory-name").focus() },
      }),
    );
  } else {
    mount(container, table(items));
  }
  mount(
    document.getElementById("inventory-count"),
    items.length ? format.countWord(items.length, "партия", "партии", "партий") : "",
  );
}

async function fetchInventory() {
  const items = await api.inventory();
  store.set("inventory.items", items);
  return items;
}

export async function enter() {
  await load(document.getElementById("inventory-list"), fetchInventory, () => frag(), {
    skeleton: { variant: "row", count: 4 },
    errorTitle: "Не удалось загрузить запасы",
  });
  renderList();
}

function flushPending() {
  for (const [id, entry] of pendingDeletes) {
    clearTimeout(entry.timer);
    // keepalive нужен, чтобы удаление ушло даже при закрытии вкладки.
    fetch(`/api/inventory/${id}`, {
      method: "DELETE",
      credentials: "same-origin",
      keepalive: true,
      headers: { "X-CSRF-Token": entry.csrf },
    });
  }
  pendingDeletes.clear();
}

export function init() {
  document.getElementById("inventory-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      name: document.getElementById("inventory-name").value.trim(),
      quantity: Number(document.getElementById("inventory-quantity").value),
      unit_code: document.getElementById("inventory-unit").value,
      storage_area: document.getElementById("inventory-storage").value,
      expires_on: document.getElementById("inventory-expiry").value || null,
      already_expired: document.getElementById("inventory-expired").checked,
    };
    try {
      const created = await api.addInventory(payload);
      store.update("inventory.items", (items) => [...items, created]);
      renderList();
      event.target.reset();
      document.getElementById("inventory-quantity").value = "1";
      document.getElementById("inventory-name").focus();
      toast.ok("Запас добавлен");
    } catch (error) {
      const hint = document.getElementById("inventory-error");
      hint.textContent = error.status === 422 ? error.detail || humanError(error) : humanError(error);
      // Просроченный срок — не ошибка ввода, а требование подтверждения.
      document.getElementById("inventory-expired-field").hidden = error.status !== 422;
    }
  });

  register("inventory:sort", (target) => {
    store.set("inventory.sort", target.dataset.key);
    renderList();
  });

  register("inventory:delete", async (target) => {
    const id = target.dataset.id;
    const items = store.get("inventory.items");
    const removed = items.find((item) => item.id === id);
    if (!removed) return;
    store.set("inventory.items", items.filter((item) => item.id !== id));
    renderList();

    const csrf = decodeURIComponent(
      (document.cookie.split("; ").find((pair) => pair.startsWith("ration_csrf=")) || "").slice(12),
    );
    const entry = { csrf, timer: null };
    pendingDeletes.set(id, entry);

    const restored = await toast.undo(`«${removed.name}» удалён`, "Вернуть");
    if (restored) {
      pendingDeletes.delete(id);
      store.update("inventory.items", (current) => [...current, removed]);
      renderList();
      return;
    }
    if (!pendingDeletes.has(id)) return;
    pendingDeletes.delete(id);
    try {
      await api.deleteInventory(id);
    } catch (error) {
      store.update("inventory.items", (current) => [...current, removed]);
      renderList();
      toast.ok(humanError(error));
    }
  });

  mount(
    document.getElementById("inventory-presets"),
    frag(
      ...PRESETS.map((preset) =>
        el("button.chip", {
          type: "button",
          text: preset.name,
          dataset: { action: "inventory:preset", preset: JSON.stringify(preset) },
        }),
      ),
    ),
  );

  register("inventory:preset", (target) => {
    const preset = JSON.parse(target.dataset.preset);
    document.getElementById("inventory-name").value = preset.name;
    document.getElementById("inventory-quantity").value = String(preset.quantity);
    document.getElementById("inventory-unit").value = preset.unit_code;
    document.getElementById("inventory-storage").value = preset.storage_area;
    document.getElementById("inventory-expiry").focus();
  });

  window.addEventListener("pagehide", flushPending);
}
