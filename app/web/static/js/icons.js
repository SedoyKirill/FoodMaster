/* Иконки — инлайновые SVG вместо текстовых глифов (⌂ ▦ ◫ ₽ ◌ ⚙ ☰ ⌕ × ❄ ● ✓ !).
 *
 * Глифы рисовались разными шрифтами по-разному и не имели подписей.
 * Спрайт в index.html не годится: страница отдаётся с Cache-Control: no-store,
 * так что он перекачивался бы при каждой загрузке.
 */

const SVG_NS = "http://www.w3.org/2000/svg";

const PATHS = {
  home: "M3 10.5 12 3l9 7.5M5.5 9.5V20a1 1 0 0 0 1 1h11a1 1 0 0 0 1-1V9.5",
  calendar: "M4 6.5h16v14H4zM4 10.5h16M8.5 3v4M15.5 3v4",
  book: "M5 4h9a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zM17 7h2v13H8",
  price: "M4 5h9l7 7-8 8-7-7zM8.5 9.5h.01",
  fridge: "M6 3h12v18H6zM6 10h12M9.5 6v2M9.5 13v2",
  // Ползунки вместо шестерёнки: та же роль, но контур не тонет на 20 px.
  settings: "M4 7h9M17 7h3M4 17h3M11 17h9M15 4v6M7 14v6",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4",
  close: "M6 6l12 12M18 6 6 18",
  plus: "M12 5v14M5 12h14",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6",
  check: "M4 12.5 9 18 20 6",
  chevron: "M9 5l7 7-7 7",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 2v2M12 20v2M4.9 4.9l1.4 1.4"
    + "M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  moon: "M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z",
  alert: "M12 3 2 20h20zM12 10v4M12 17h.01",
  refresh: "M20 5v5h-5M4 19v-5h5M19.5 10a8 8 0 0 0-14-3M4.5 14a8 8 0 0 0 14 3",
  menu: "M4 7h16M4 12h16M4 17h16",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  basket: "M3 9h18l-2 11H5zM8 9 12 3l4 6",
  star: "M12 3.5 14.6 9l6 .9-4.3 4.2 1 6-5.3-2.8L6.7 20l1-6L3.4 9.9l6-.9z",
};

/**
 * Создаёт SVG-иконку.
 * Без `title` она декоративная и скрыта от скринридера; с `title` — картинка
 * с текстовой альтернативой.
 */
export function icon(name, { size = 20, title = null, className = "" } = {}) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("focusable", "false");
  if (className) svg.setAttribute("class", className);

  if (title) {
    svg.setAttribute("role", "img");
    const node = document.createElementNS(SVG_NS, "title");
    node.textContent = title;
    svg.append(node);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }

  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", PATHS[name] || PATHS.alert);
  svg.append(path);
  return svg;
}

/** Заполняет иконками статические элементы с data-icon в разметке. */
export function hydrateIcons(root = document) {
  for (const node of root.querySelectorAll("[data-icon]")) {
    if (node.dataset.iconDone === "1") continue;
    node.prepend(icon(node.dataset.icon, { size: Number(node.dataset.iconSize) || 20 }));
    node.dataset.iconDone = "1";
  }
}
