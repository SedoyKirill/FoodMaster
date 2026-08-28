/* Экран «Продукты «Ленты»».
 *
 * Что чинится по ТЗ Б2: нормальная пагинация, КБЖУ из карточки (данные уже
 * приходили и выбрасывались) и фильтр «только со скидкой». Цены — подписанный
 * список из трёх пар вместо трёхстрочного блоба.
 */

import { api, debounce } from "../api.js";
import { register } from "../actions.js";
import * as format from "../format.js";
import * as router from "../router.js";
import { store } from "../state.js";
import { badge, el, frag, load, mount, statePanel } from "../render.js";

const PAGE_SIZE = 40;
let controller = null;
let categoriesLoaded = null;

/* Разделы каталога грузятся один раз; select заполняется до выставления
 * значения из фильтров, иначе value молча сбрасывается на «Все категории». */
function ensureCategories() {
  if (!categoriesLoaded) {
    categoriesLoaded = api
      .productCategories()
      .then((rows) => {
        const select = document.getElementById("product-category");
        for (const row of rows) {
          select.append(
            el("option", {
              value: row.category_slug,
              text: `${format.categoryLabel(row.category_slug)} (${format.number(row.product_count)})`,
            }),
          );
        }
      })
      .catch(() => {
        categoriesLoaded = null;
      });
  }
  return categoriesLoaded || Promise.resolve();
}

function priceRows(product) {
  const rows = [
    ["Обычная", product.regular_price_kop],
    ["По Карте №1", product.loyalty_price_kop],
    ["Акция", product.promo_price_kop],
  ].filter(([, value]) => value !== null && value !== undefined && Number(value) > 0);
  if (!rows.length) return null;

  return el(
    "dl.prices",
    {},
    rows.flatMap(([title, value]) => {
      const isCurrent = Number(value) === Number(product.effective_price_kop);
      return [
        el("dt", { text: title }),
        el(`dd.money${isCurrent ? ".is-current" : ""}`, { text: format.money(value) }),
      ];
    }),
  );
}

function nutrition(product) {
  // КБЖУ заполнены лишь у части каталога — при их отсутствии блок не рисуется,
  // вместо четырёх прочерков.
  if (product.kcal_100 === null || product.kcal_100 === undefined) return null;
  const values = [
    ["Ккал", product.kcal_100],
    ["Белки", product.protein_100],
    ["Жиры", product.fat_100],
    ["Углеводы", product.carb_100],
  ];
  return el("dl.nutrition", { "aria-label": "Пищевая ценность на 100 г" },
    values.map(([title, value]) =>
      el("div", {}, [
        el("dt", { text: title }),
        el("dd", { text: format.number(value) ?? "—" }),
      ]),
    ),
  );
}

function productCard(product) {
  const discounted =
    product.promo_price_kop && product.promo_price_kop < product.regular_price_kop;
  return el("article.card", {}, [
    product.brand ? el("span.footnote", { text: product.brand }) : null,
    el("h3.card__title", { text: product.name }),
    product.pack_text ? el("span.footnote", { text: product.pack_text }) : null,
    discounted
      ? el("div.card__badges", {}, [
          badge(
            product.discount_percent ? `−${format.number(product.discount_percent)}%` : "Акция",
            "draft",
          ),
        ])
      : null,
    priceRows(product),
    nutrition(product),
  ]);
}

function renderList() {
  const { items, total, filters } = store.get("products");
  const grid = document.getElementById("product-list");
  if (!items.length) {
    mount(
      grid,
      statePanel({
        kind: "empty",
        iconName: "search",
        title: "Товары не найдены",
        text: filters.sale
          ? "Со скидкой сейчас ничего не подходит под запрос — снимите фильтр."
          : "Попробуйте другое слово: ищем по названию и бренду.",
      }),
    );
  } else {
    mount(grid, frag(...items.map(productCard)));
  }
  mount(
    document.getElementById("product-count"),
    `Показано ${format.number(items.length)} из ${format.number(total)}`,
  );
  const more = document.getElementById("product-more");
  more.hidden = items.length >= total;
  more.textContent = `Показать ещё ${Math.min(PAGE_SIZE, total - items.length)}`;
}

async function fetchPage({ append = false } = {}) {
  const filters = store.get("products.filters");
  const offset = append ? store.get("products.items").length : 0;
  controller?.abort();
  controller = new AbortController();
  const page = await api.products(
    {
      search: filters.q,
      sort: filters.sort,
      discount_only: filters.sale,
      category: filters.category,
      limit: PAGE_SIZE,
      offset,
    },
    controller.signal,
  );
  store.set("products.total", page.total);
  store.update("products.items", (items) => (append ? [...items, ...page.items] : page.items));
  return page;
}

function pushFilters(patch) {
  const filters = { ...store.get("products.filters"), ...patch };
  router.go(
    router.buildHash("#/products", {
      q: filters.q,
      sort: filters.sort,
      sale: filters.sale,
      category: filters.category,
    }),
    { replace: true },
  );
}

export async function enter(query = {}) {
  const filters = {
    q: query.q || "",
    sort: query.sort || "name",
    sale: query.sale === "1",
    category: query.category || "",
  };
  const changed = JSON.stringify(filters) !== JSON.stringify(store.get("products.filters"));
  store.set("products.filters", filters);
  await ensureCategories();
  document.getElementById("product-search").value = filters.q;
  document.getElementById("product-sort").value = filters.sort;
  document.getElementById("product-sale").checked = filters.sale;
  document.getElementById("product-category").value = filters.category;

  if (changed || !store.get("products.items").length) {
    await load(document.getElementById("product-list"), () => fetchPage(), () => frag(), {
      skeleton: { variant: "card", count: 6 },
      errorTitle: "Каталог не загрузился",
    });
  }
  renderList();
}

export function init() {
  const push = debounce((value) => pushFilters({ q: value }), 300);
  document.getElementById("product-search").addEventListener("input", (event) => {
    push(event.target.value.trim());
  });
  document.getElementById("product-sort").addEventListener("change", (event) => {
    pushFilters({ sort: event.target.value });
  });
  document.getElementById("product-sale").addEventListener("change", (event) => {
    pushFilters({ sale: event.target.checked });
  });
  document.getElementById("product-category").addEventListener("change", (event) => {
    pushFilters({ category: event.target.value });
  });
  register("products:more", async () => {
    await fetchPage({ append: true });
    renderList();
  });
}
