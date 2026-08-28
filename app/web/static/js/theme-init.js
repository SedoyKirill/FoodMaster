/* Ставит тему до первой отрисовки, иначе тёмная тема мигает белым.
 * Отдельным файлом, а не инлайном: CSP запрещает инлайновые скрипты. */
try {
  var saved = localStorage.getItem("ration:theme");
  if (saved === "light" || saved === "dark") {
    document.documentElement.setAttribute("data-theme", saved);
  }
} catch (error) {
  /* приватный режим без localStorage — остаётся системная тема */
}
