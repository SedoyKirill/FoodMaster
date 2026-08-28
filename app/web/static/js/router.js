/* Роутер по хешу: Back/Forward, прямые ссылки и средний клик работают,
 * потому что навигация — обычные <a href="#/...">, а не обработчики кликов. */

const routes = [];
let current = { path: "#/", params: {}, query: {} };

export function define(pattern, handler) {
  const keys = [];
  const source = pattern.replace(/:([A-Za-z_]+)/g, (_match, key) => {
    keys.push(key);
    return "([^/?]+)";
  });
  routes.push({ re: new RegExp(`^${source}$`), keys, handler });
}

export function currentRoute() {
  return current;
}

export function go(hash, { replace = false } = {}) {
  if (replace) {
    history.replaceState(null, "", hash);
    dispatch();
  } else if (location.hash === hash) {
    dispatch();
  } else {
    location.hash = hash;
  }
}

/** Собирает `#/recipes?q=рис&meal=lunch`, выбрасывая пустые значения. */
export function buildHash(path, params = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "" || value === false) continue;
    search.set(key, value === true ? "1" : String(value));
  }
  const text = search.toString();
  return text ? `${path}?${text}` : path;
}

function dispatch() {
  const raw = location.hash || "#/";
  const [path, search = ""] = raw.split("?");
  const query = Object.fromEntries(new URLSearchParams(search));
  for (const route of routes) {
    const match = path.match(route.re);
    if (!match) continue;
    const params = Object.fromEntries(
      route.keys.map((key, index) => [key, decodeURIComponent(match[index + 1])]),
    );
    current = { path, params, query };
    route.handler(params, query);
    return;
  }
  go("#/", { replace: true });
}

export function start() {
  window.addEventListener("hashchange", dispatch);
  dispatch();
}
