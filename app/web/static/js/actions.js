/* Реестр действий для делегирования событий.
 *
 * Разметка несёт data-action="имя", один слушатель на документ находит
 * обработчик. Благодаря этому рендер никогда не переназначает слушатели —
 * прежний код после каждой отрисовки заново раздавал .onclick.
 */

const handlers = new Map();

export function register(name, handler) {
  handlers.set(name, handler);
}

export function registerAll(map) {
  for (const [name, handler] of Object.entries(map)) register(name, handler);
}

export function initActions() {
  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-action]");
    if (!target || target.disabled) return;
    const handler = handlers.get(target.dataset.action);
    if (!handler) return;
    event.preventDefault();
    handler(target, event);
  });

  document.addEventListener("change", (event) => {
    const target = event.target.closest("[data-change]");
    if (!target) return;
    const handler = handlers.get(target.dataset.change);
    if (handler) handler(target, event);
  });
}
