/* Тема оформления: «светлая» / «тёмная» / «авто» — один выбор (localStorage, ключ theme),
   применяется через data-theme на <html>. До первой отрисовки (без мигания) его уже
   выставляет инлайн-скрипт в <head> index.html — здесь это не повторяется, только держится
   синхронно после. «Авто» разрешается в конкретное значение по matchMedia и живьём следит
   за системной темой, без перезагрузки страницы.
   Переключатель в шапке (#theme-switch, три значка без подписи) и радио-группа в настройках
   («Дополнительно → Сервер → Оформление», server_config.js) — один и тот же источник: любой
   элемент с [data-theme-choice] (button или input[type=radio]) держится в синхроне через
   делегирование на document, переживает перестройку панели настроек без повторной привязки. */
(() => {
  const KEY = 'theme';
  const media = matchMedia('(prefers-color-scheme: dark)');
  let current;
  try { current = localStorage.getItem(KEY) || 'auto'; } catch (e) { current = 'auto'; }

  const resolved = (choice) => (choice === 'auto' ? (media.matches ? 'dark' : 'light') : choice);
  const apply = () => document.documentElement.setAttribute('data-theme', resolved(current));

  function sync() {
    document.querySelectorAll('[data-theme-choice]').forEach((el) => {
      const active = el.dataset.themeChoice === current;
      if (el.tagName === 'INPUT') el.checked = active;
      else { el.setAttribute('aria-checked', String(active)); el.classList.toggle('active', active); }
    });
  }

  function setTheme(choice) {
    current = choice;
    try { localStorage.setItem(KEY, choice); } catch (e) { /* приватный режим — переживёт только эту вкладку */ }
    apply();
    sync();
  }

  media.addEventListener('change', () => { if (current === 'auto') apply(); });
  document.addEventListener('click', (ev) => {
    const el = ev.target.closest('button[data-theme-choice]');
    if (el) setTheme(el.dataset.themeChoice);
  });
  document.addEventListener('change', (ev) => {
    const el = ev.target.closest('input[data-theme-choice]');
    if (el && el.checked) setTheme(el.dataset.themeChoice);
  });

  window.AppTheme = {current: () => current, set: setTheme, sync};
  apply();
  sync();
})();
