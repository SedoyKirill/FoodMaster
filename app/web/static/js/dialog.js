/* Модальные окна поверх нативного <dialog>: фокус и Esc он держит сам,
 * нам остаётся вернуть фокус вызвавшему элементу и связать закрытие с историей. */

import { el, mount } from "./render.js";
import { icon } from "./icons.js";

let invoker = null;
let silent = false;

function dialogNode() {
  return document.getElementById("recipe-dialog");
}

/**
 * Открывает основной диалог.
 * `onClose` вызывается только при закрытии пользователем (Esc, ×, фон) —
 * закрытие, инициированное роутером, его не запускает, иначе history.back()
 * ушёл бы в цикл.
 */
export function open(content, { labelledBy = null, onClose = null } = {}) {
  const dialog = dialogNode();
  invoker = document.activeElement;
  dialog.onCloseCallback = onClose;
  if (labelledBy) dialog.setAttribute("aria-labelledby", labelledBy);
  mount(
    dialog,
    el("button.icon-btn.dialog__close", {
      type: "button",
      "aria-label": "Закрыть",
      autofocus: true,
      onClick: () => dialog.close(),
    }, [icon("close", { size: 22 })]),
    el("div.dialog__body", {}, [content]),
  );
  if (!dialog.open) dialog.showModal();
}

/** Закрытие без побочных эффектов — когда его инициировал сам роутер. */
export function closeSilently() {
  const dialog = dialogNode();
  if (!dialog.open) return;
  silent = true;
  dialog.close();
}

export function isOpen() {
  return dialogNode().open;
}

export function initDialog() {
  const dialog = dialogNode();
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => {
    const callback = dialog.onCloseCallback;
    dialog.onCloseCallback = null;
    if (invoker && invoker.isConnected) invoker.focus();
    invoker = null;
    if (silent) {
      silent = false;
      return;
    }
    if (callback) callback();
  });
}

/** Подтверждение вместо window.confirm(): своё окно, свои кнопки, свой фокус. */
export function confirm({ title, text = null, confirmLabel = "Удалить", danger = true }) {
  return new Promise((resolve) => {
    const dialog = el("dialog.dialog.dialog--confirm");
    let answer = false;
    const close = (value) => {
      answer = value;
      dialog.close();
    };
    mount(
      dialog,
      el("div.dialog__body", {}, [
        el("h2", { text: title }),
        text && el("p.footnote.dialog__text", { text }),
        el("div.dialog__actions", {}, [
          el("button.btn", { type: "button", text: "Отмена", onClick: () => close(false) }),
          el(`button.btn.${danger ? "btn--danger" : "btn--action"}`, {
            type: "button",
            text: confirmLabel,
            autofocus: true,
            onClick: () => close(true),
          }),
        ]),
      ]),
    );
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(answer);
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}
