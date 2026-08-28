/* Общее состояние приложения.
 *
 * Нужно ровно для одного: экраны читают уже загруженные данные вместо того,
 * чтобы перезапрашивать их после каждого действия. Из-за прежнего подхода
 * дашборд дёргался после любого сохранения, а последний план грузился дважды.
 */

function get(source, path) {
  return String(path)
    .split(".")
    .reduce((value, key) => (value === null || value === undefined ? value : value[key]), source);
}

function set(source, path, value) {
  const keys = String(path).split(".");
  const last = keys.pop();
  let cursor = source;
  for (const key of keys) {
    if (typeof cursor[key] !== "object" || cursor[key] === null) cursor[key] = {};
    cursor = cursor[key];
  }
  cursor[last] = value;
}

export function createStore(initial) {
  const data = structuredClone(initial);
  const listeners = new Map();

  function notify(path) {
    const keys = String(path).split(".");
    for (let depth = keys.length; depth > 0; depth -= 1) {
      const prefix = keys.slice(0, depth).join(".");
      for (const listener of listeners.get(prefix) || []) listener(get(data, prefix), prefix);
    }
    for (const listener of listeners.get("*") || []) listener(data, path);
  }

  return {
    get: (path) => (path ? get(data, path) : data),
    set(path, value) {
      set(data, path, value);
      notify(path);
      return value;
    },
    update(path, updater) {
      return this.set(path, updater(get(data, path)));
    },
    subscribe(path, listener) {
      if (!listeners.has(path)) listeners.set(path, new Set());
      listeners.get(path).add(listener);
      return () => listeners.get(path).delete(listener);
    },
  };
}

export const store = createStore({
  me: null,
  dashboard: null,
  facets: { cuisines: [], meal_types: [] },
  recipes: {
    items: [],
    total: 0,
    readyTotal: null,
    offset: 0,
    filters: { q: "", meal: "", cuisine: "", dish: "", ready: false },
  },
  recipeById: {},
  plans: { list: [], activeId: null, byId: {} },
  inventory: { items: [], sort: "expires" },
  products: { items: [], total: 0, offset: 0, filters: { q: "", sort: "name", sale: false, category: "" } },
});

export function role() {
  return store.get("me.household.role") || "viewer";
}

export function canReview() {
  return ["owner", "admin"].includes(role());
}

export function canEdit() {
  return role() !== "viewer";
}
