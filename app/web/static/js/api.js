/* Клиент HTTP-API.
 *
 * CSRF-схема сервера не менялась: токен лежит в не-HttpOnly куке ration_csrf
 * и уходит заголовком X-CSRF-Token на любой не-GET.
 */

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null, retryAfter = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}

let onUnauthorized = null;

/** Один обработчик на всё приложение: 401 переводит на экран входа. */
export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler;
}

function readCookie(name) {
  const match = document.cookie
    .split("; ")
    .find((pair) => pair.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : "";
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === "" || value === false) continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export async function request(path, { method = "GET", body, signal, params } = {}) {
  const headers = {};
  const init = { method, credentials: "same-origin", headers, signal };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  if (method !== "GET") headers["X-CSRF-Token"] = readCookie("ration_csrf");

  const response = await fetch(`${path}${query(params)}`, init);
  if (!response.ok) {
    let detail = null;
    try {
      const payload = await response.json();
      detail = Array.isArray(payload.detail)
        ? payload.detail.map((entry) => entry.msg).join(", ")
        : payload.detail;
    } catch (error) {
      detail = null;
    }
    const failure = new ApiError(detail || `Ошибка ${response.status}`, {
      status: response.status,
      detail,
      retryAfter: Number(response.headers.get("Retry-After")) || null,
    });
    if (response.status === 401 && onUnauthorized) onUnauthorized();
    throw failure;
  }
  if (response.status === 204) return null;
  return response.json();
}

export const api = {
  me: () => request("/api/me"),
  register: (payload) => request("/api/auth/register", { method: "POST", body: payload }),
  login: (payload) => request("/api/auth/login", { method: "POST", body: payload }),
  telegramLogin: (code) =>
    request("/api/auth/telegram-login", { method: "POST", body: { code } }),
  setPassword: (password) =>
    request("/api/auth/set-password", { method: "POST", body: { password } }),
  logout: () => request("/api/auth/logout", { method: "POST" }),

  dashboard: () => request("/api/dashboard"),

  recipes: (params, signal) => request("/api/recipes", { params, signal }),
  recipeFacets: () => request("/api/recipes/facets"),
  recipe: (id) => request(`/api/recipes/${id}`),
  reviewRecipe: (id, status) =>
    request(`/api/recipes/${id}/review`, { method: "POST", body: { status } }),
  rateRecipe: (id, rating) =>
    request(`/api/recipes/${id}/rating`, { method: "POST", body: { rating } }),

  products: (params, signal) => request("/api/products", { params, signal }),
  productCategories: () => request("/api/products/categories"),
  replaceMeal: (planId, mealId, recipeId = null) =>
    request(`/api/plans/${planId}/meals/${mealId}/replace`, {
      method: "POST",
      body: { recipe_id: recipeId },
    }),

  inventory: () => request("/api/inventory"),
  addInventory: (payload) => request("/api/inventory", { method: "POST", body: payload }),
  deleteInventory: (id) => request(`/api/inventory/${id}`, { method: "DELETE" }),

  plans: (limit = 20) => request("/api/plans", { params: { limit } }),
  plan: (id) => request(`/api/plans/${id}`),
  latestPlan: () => request("/api/plans/latest"),
  generatePlan: (payload) => request("/api/plans/generate", { method: "POST", body: payload }),
  deletePlan: (id) => request(`/api/plans/${id}`, { method: "DELETE" }),
  markPurchased: (planId, itemId, purchased) =>
    request(`/api/plans/${planId}/items/${itemId}`, { method: "PATCH", body: { purchased } }),

  saveSettings: (payload) => request("/api/settings", { method: "PUT", body: payload }),
  patchPerson: (id, changes) =>
    request(`/api/settings/people/${id}`, { method: "PATCH", body: changes }),

  telegramToken: () => request("/api/telegram/link-token", { method: "POST" }),
  telegramUnlink: () => request("/api/telegram/link", { method: "DELETE" }),
};

/** Отложенный вызов с отменой предыдущего — для живого поиска. */
export function debounce(fn, delay = 300) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}
