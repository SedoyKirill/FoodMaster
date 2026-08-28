/* Экран входа и регистрации.
 *
 * Что чинится по ТЗ Б2: настоящий tablist со стрелками, инлайновые ошибки полей
 * вместо единственной строки внизу, требования к паролю видны ДО отправки,
 * и отдельная обработка 429 из A6 — с обратным отсчётом.
 */

import { api } from "../api.js";
import { humanError } from "../render.js";

const RULES = [{ id: "length", text: "не менее 8 символов", test: (value) => value.length >= 8 }];

let mode = "register";
let countdown = null;

function setFieldError(inputId, message) {
  const input = document.getElementById(inputId);
  const error = document.getElementById(`${inputId}-error`);
  error.textContent = message || "";
  input.setAttribute("aria-invalid", message ? "true" : "false");
}

function clearErrors() {
  setFieldError("auth-login", "");
  setFieldError("auth-password", "");
  setFieldError("auth-code", "");
  document.getElementById("auth-form-error").textContent = "";
}

const TITLES = {
  register: "Создайте аккаунт",
  login: "С возвращением",
  telegram: "Вход по коду из бота",
};

const SUBTITLES = {
  register: "Аккаунт хранится только на этом компьютере.",
  login: "Введите логин и пароль от локального аккаунта.",
  telegram: "Отправьте боту «Супостат» команду /web — он пришлёт код.",
};

const SUBMIT_LABELS = { register: "Создать аккаунт", login: "Войти", telegram: "Войти по коду" };

export function setMode(next) {
  mode = next;
  for (const tab of document.querySelectorAll(".auth-tab")) {
    const selected = tab.dataset.mode === next;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  const registering = next === "register";
  const byCode = next === "telegram";
  document.getElementById("auth-title").textContent = TITLES[next];
  document.getElementById("auth-subtitle").textContent = SUBTITLES[next];
  // Вход по коду не спрашивает ни логина, ни пароля: код и есть удостоверение.
  document.getElementById("auth-login").closest(".field").hidden = byCode;
  document.getElementById("auth-password").closest(".field").hidden = byCode;
  document.getElementById("auth-code-field").hidden = !byCode;
  document.getElementById("auth-household-field").hidden = !registering;
  document.getElementById("auth-rules").hidden = !registering;
  document.getElementById("auth-submit").textContent = SUBMIT_LABELS[next];
  document.getElementById("auth-password").setAttribute(
    "autocomplete",
    registering ? "new-password" : "current-password",
  );
  clearErrors();
}

function renderRules(value) {
  for (const rule of RULES) {
    const node = document.getElementById(`auth-rule-${rule.id}`);
    if (node) node.dataset.met = String(rule.test(value));
  }
}

function startCountdown(seconds) {
  clearInterval(countdown);
  const banner = document.getElementById("auth-form-error");
  let left = seconds;
  const tick = () => {
    banner.textContent = `Слишком много попыток. Повторите через ${left} с.`;
    if (left <= 0) {
      clearInterval(countdown);
      banner.textContent = "";
      return;
    }
    left -= 1;
  };
  tick();
  countdown = setInterval(tick, 1000);
}

export function init(onSuccess) {
  const tabs = [...document.querySelectorAll(".auth-tab")];
  for (const tab of tabs) {
    tab.addEventListener("click", () => setMode(tab.dataset.mode));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
      next.focus();
      setMode(next.dataset.mode);
    });
  }

  document.getElementById("auth-password").addEventListener("input", (event) => {
    renderRules(event.target.value);
  });

  document.getElementById("auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearErrors();
    const login = document.getElementById("auth-login").value.trim();
    const password = document.getElementById("auth-password").value;
    const code = document.getElementById("auth-code").value.trim();
    if (mode === "telegram") {
      if (!code) {
        setFieldError("auth-code", "Введите код из бота.");
        return;
      }
    } else {
      if (login.length < 3) {
        setFieldError("auth-login", "Логин: не короче трёх символов.");
        return;
      }
      if (mode === "register" && password.length < 8) {
        setFieldError("auth-password", "Пароль: не менее 8 символов.");
        return;
      }
    }

    const button = document.getElementById("auth-submit");
    button.disabled = true;
    try {
      if (mode === "telegram") {
        await api.telegramLogin(code);
      } else if (mode === "register") {
        await api.register({
          login,
          password,
          household_name:
            document.getElementById("auth-household").value.trim() || "Моя семья",
        });
      } else {
        await api.login({ login, password });
      }
      await onSuccess(mode);
    } catch (error) {
      if (error.status === 401 && mode === "telegram") {
        setFieldError("auth-code", "Код не подошёл: он просрочен или уже использован.");
      } else if (error.status === 401) {
        setFieldError("auth-password", "Неверный логин или пароль.");
      } else if (error.status === 409) setFieldError("auth-login", "Такой логин уже занят.");
      else if (error.status === 429) startCountdown(error.retryAfter || 60);
      else document.getElementById("auth-form-error").textContent = humanError(error);
    } finally {
      button.disabled = false;
    }
  });

  setMode("register");
  renderRules("");
}
