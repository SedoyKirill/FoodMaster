/* Построение DOM без innerHTML.
 *
 * Значения попадают на страницу через textContent и append(String) — разметка
 * из данных не парсится в принципе, поэтому ручная функция экранирования
 * (и риск забыть её в одном из 17 мест) исчезает вместе со строковыми
 * шаблонами на 2000 символов.
 */

import { icon } from "./icons.js";

/**
 * el('div.card#id', {props}, [children])
 * — свойства: dataset, text, on<Event>, всё остальное как свойство или атрибут;
 * — дети: узлы, строки, вложенные массивы; null/undefined/false пропускаются,
 *   поэтому условные части пишутся обычным `условие && узел`.
 */
export function el(spec, props = {}, children = []) {
  const parts = String(spec).split(/(?=[.#])/);
  const node = document.createElement(parts[0] || "div");
  for (const token of parts.slice(1)) {
    if (token.startsWith(".")) node.classList.add(token.slice(1));
    else if (token.startsWith("#")) node.id = token.slice(1);
  }
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key in node && key !== "list" && key !== "form") {
      node[key] = value;
    } else {
      node.setAttribute(key, value === true ? "" : value);
    }
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of [children].flat(4)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

export function frag(...children) {
  return append(document.createDocumentFragment(), children);
}

export function mount(container, ...children) {
  container.replaceChildren();
  append(container, children);
  return container;
}

/** Список фактов, разделённых точкой средствами CSS. Пустые отбрасываются. */
export function metaList(items, className = "meta") {
  const values = [items].flat().filter((value) => value !== null && value !== undefined && value !== "");
  if (!values.length) return null;
  return el(`ul.${className}`, {}, values.map((value) => el("li", {}, [value])));
}

export function badge(text, modifier = null) {
  return el(`span.badge${modifier ? `.badge--${modifier}` : ""}`, { text });
}

export function pill(text, modifier = null, iconName = null) {
  return el(`span.pill${modifier ? `.pill--${modifier}` : ""}`, {}, [
    iconName && icon(iconName, { size: 14 }),
    el("span", { text }),
  ]);
}

export function skeleton({ variant = "row", count = 6 } = {}) {
  const wrapper = variant === "card" ? el("div.card-grid") : el("div");
  for (let index = 0; index < count; index += 1) {
    wrapper.append(el(`div.skeleton.skeleton--${variant}`, { "aria-hidden": true }));
  }
  return wrapper;
}

/**
 * Пустое состояние или ошибка: объяснение и действие, а не исчезающий тост.
 */
export function statePanel({ kind = "empty", iconName = null, title, text = null, action = null }) {
  return el(
    `div.state.state--${kind}`,
    kind === "error" ? { role: "alert" } : {},
    [
      iconName && el("span.state__icon", {}, [icon(iconName, { size: 28 })]),
      el("p.state__title", { text: title }),
      text && el("p.state__text", { text }),
      action &&
        (action.href
          ? el("a.btn.btn--action", { href: action.href, text: action.label })
          : el("button.btn.btn--action", { type: "button", text: action.label, onClick: action.onClick })),
    ],
  );
}

const HTTP_MESSAGES = {
  400: "Запрос не принят — проверьте введённые данные.",
  401: "Сессия истекла. Войдите заново.",
  403: "Недостаточно прав для этого действия.",
  404: "Не найдено.",
  409: "Такая запись уже существует.",
  422: "Проверьте заполнение полей.",
  429: "Слишком много попыток. Повторите чуть позже.",
};

/** Понятная фраза вместо сырого backend-detail в заголовке ошибки. */
export function humanError(error) {
  if (!error) return "Неизвестная ошибка.";
  if (error.status === 429 && error.retryAfter) {
    return `Слишком много попыток. Повторите через ${error.retryAfter} с.`;
  }
  if (HTTP_MESSAGES[error.status]) return HTTP_MESSAGES[error.status];
  if (error.status >= 500) return "Сервер не ответил. Попробуйте ещё раз.";
  if (error.name === "TypeError") return "Нет связи с сервером. Проверьте, запущен ли он.";
  return error.message || "Неизвестная ошибка.";
}

/**
 * Единый цикл загрузки экрана: скелетон → данные / пустое состояние / ошибка
 * со встроенной кнопкой «Повторить».
 */
export async function load(container, loader, view, options = {}) {
  const {
    skeleton: skeletonOptions = { variant: "row", count: 5 },
    empty = null,
    errorTitle = "Не удалось загрузить",
    onError = null,
  } = options;
  container.setAttribute("aria-busy", "true");
  mount(container, skeleton(skeletonOptions));
  try {
    const data = await loader();
    const isEmpty = Array.isArray(data) ? data.length === 0 : !data;
    if (isEmpty && empty) {
      mount(container, statePanel({ kind: "empty", ...empty }));
    } else {
      mount(container, view(data));
    }
    return data;
  } catch (error) {
    if (onError && onError(error)) return null;
    mount(
      container,
      statePanel({
        kind: "error",
        iconName: "alert",
        title: errorTitle,
        text: humanError(error),
        action: {
          label: "Повторить",
          onClick: () => load(container, loader, view, options),
        },
      }),
    );
    return null;
  } finally {
    container.setAttribute("aria-busy", "false");
  }
}
