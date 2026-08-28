/* Тема: «как в системе» / светлая / тёмная.
 * «Как в системе» = атрибут снят, работает @media (prefers-color-scheme). */

const KEY = "ration:theme";

export function getTheme() {
  try {
    const saved = localStorage.getItem(KEY);
    return saved === "light" || saved === "dark" ? saved : "auto";
  } catch (error) {
    return "auto";
  }
}

export function resolvedTheme() {
  const choice = getTheme();
  if (choice !== "auto") return choice;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function setTheme(choice) {
  const root = document.documentElement;
  if (choice === "auto") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", choice);
  }
  try {
    if (choice === "auto") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch (error) {
    /* приватный режим — тема продержится до перезагрузки */
  }
  document.dispatchEvent(new CustomEvent("themechange", { detail: resolvedTheme() }));
}

/** Переключает светлую/тёмную относительно того, что видно сейчас. */
export function toggleTheme() {
  setTheme(resolvedTheme() === "dark" ? "light" : "dark");
}
