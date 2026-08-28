/* Тост — только подтверждение действия и отмена.
 * Ошибки показываются встроенной плашкой на экране: исчезающее сообщение
 * было единственным способом узнать о проблеме и это была ошибка прежнего UI. */

import { el, mount } from "./render.js";

let node = null;
let timer = null;

function surface() {
  if (!node) node = document.getElementById("toast");
  return node;
}

function show(children, duration) {
  const element = surface();
  mount(element, children);
  element.classList.add("is-visible");
  clearTimeout(timer);
  timer = setTimeout(() => element.classList.remove("is-visible"), duration);
}

export function ok(message, duration = 3000) {
  show([el("span", { text: message })], duration);
}

/**
 * Показывает сообщение с кнопкой отмены и возвращает промис:
 * true — пользователь нажал «Вернуть», false — время вышло.
 */
export function undo(message, actionLabel = "Вернуть", duration = 8000) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      surface().classList.remove("is-visible");
      resolve(value);
    };
    show(
      [
        el("span", { text: message }),
        el("button.toast__action", {
          type: "button",
          text: actionLabel,
          onClick: () => finish(true),
        }),
      ],
      duration,
    );
    timer = setTimeout(() => finish(false), duration);
  });
}

export function hide() {
  clearTimeout(timer);
  surface().classList.remove("is-visible");
}
