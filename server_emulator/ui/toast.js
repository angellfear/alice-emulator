/* Уведомления-тосты вместо строки #notice под меню: фиксированный стек справа
   сверху поверх содержимого, страницу не сдвигает. window.notify(text, {error, timeout}):
   обычное сообщение скрывается через 4 с, ошибка — через 8 с, наведение мыши останавливает
   таймер, кнопка «×» закрывает; тот же текст, пока тост виден, — счётчик «×N» и таймер
   заново, нового тоста нет. Видно не больше 5, старые уходят первыми. Ошибки — role="alert".
   notify.pin(id, text, {error}) — закреплённое уведомление: без «×» и автоскрытия, в лимит не
   входит; тот же id заменяет текст, notify.unpin(id) убирает (когда причина устранена).
   Подключён до app.js: message() в app.js и обёртки вкладок вызывают notify. */
(() => {
  const MAX_VISIBLE = 5;   // одновременно видно не больше
  const PLAIN_MS = 4000;   // автоскрытие обычного сообщения
  const ERROR_MS = 8000;   // автоскрытие ошибки
  let root = null;         // контейнер-стек, создаётся при первом notify
  const stack = [];        // видимые тосты, старые в начале
  const byText = new Map();  // текст → тост (для счётчика повторов)
  const pinned = new Map();  // id → закреплённый тост

  function container() {
    if (!root) {
      root = document.createElement('div');
      root.id = 'toasts';
      root.className = 'toasts';
      root.setAttribute('aria-live', 'polite');  // скринридер озвучивает появления
      document.body.append(root);
    }
    return root;
  }

  function create(text, error, ms, pin) {
    const el = document.createElement('div');
    el.className = 'toast' + (error ? ' error' : '');
    if (error) el.setAttribute('role', 'alert');
    const label = document.createElement('span');
    label.className = 'toast-text';
    label.textContent = text;
    const count = document.createElement('span');
    count.className = 'toast-count';
    count.hidden = true;  // появляется со второго повтора того же текста: ×2, ×3…
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.textContent = '×';
    close.setAttribute('aria-label', 'Скрыть уведомление');
    el.append(label, count);
    if (pin == null) el.append(close);

    const state = { el, label, count, n: 1, remove, restart, pin };
    let timer = null, left = ms, since = 0, hover = false;
    const pause = () => { if (timer) { clearTimeout(timer); timer = null; left -= Date.now() - since; } };
    const run = () => { if (!timer && !hover) { since = Date.now(); timer = setTimeout(remove, Math.max(0, left)); } };
    function restart(next) {  // повтор того же текста: таймер на весь срок заново
      left = next;
      if (timer) { clearTimeout(timer); timer = null; }
      run();
    }
    function remove() {
      const at = stack.indexOf(state);
      if (at < 0) return;  // уже убран (крестик после автоскрытия)
      stack.splice(at, 1);
      if (pin == null) byText.delete(text); else pinned.delete(pin);
      el.remove();
    }
    stack.push(state);
    if (pin != null) {  // закреплённое: без таймера и крестика, снимается только unpin
      pinned.set(pin, state);
      container().prepend(el);
      return;
    }
    el.addEventListener('mouseenter', () => { hover = true; pause(); });
    el.addEventListener('mouseleave', () => { hover = false; run(); });
    close.addEventListener('click', remove);
    byText.set(text, state);
    container().append(el);  // новое — снизу стека
    const plain = stack.filter(item => item.pin == null);
    while (plain.length > MAX_VISIBLE) plain.shift().remove();  // старые уходят первыми
    run();  // автоскрытие идёт с момента появления, без ожидания ухода курсора
  }

  window.notify = (text, { error = false, timeout } = {}) => {
    if (text == null || text === '') return;
    const ms = timeout != null ? timeout : (error ? ERROR_MS : PLAIN_MS);
    const key = String(text);
    const seen = byText.get(key);
    if (seen) {
      seen.n += 1;
      seen.count.textContent = '×' + seen.n;
      seen.count.hidden = false;
      seen.restart(ms);
      return;
    }
    create(key, error, ms);
  };
  window.notify.pin = (id, text, { error = true } = {}) => {
    const seen = pinned.get(id);
    if (seen) { seen.label.textContent = String(text); return; }
    create(String(text), error, null, id);
  };
  window.notify.unpin = id => { const seen = pinned.get(id); if (seen) seen.remove(); };
})();
