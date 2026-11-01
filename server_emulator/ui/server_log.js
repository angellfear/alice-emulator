// Раздел «Дополнительно → Лог» (TASK-068): журнал событий сервера (state/server_log.sqlite3).
// Пишут logging (aiohttp/asyncio/эмулятор, WARNING+), ошибки из recent и события сервера.
// Повторы в пределах часа склеены (×N, время последнего). Хранение — 3 дня.
// Фильтры: уровень, источник, поиск; «Показать ещё» — пагинация по before; клик по строке — detail.
(() => {
  const root = document.getElementById('server-log');
  if (!root) return;

  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (text, onclick, cls = 'secondary') => { const b = node('button', text, cls); b.type = 'button'; b.onclick = onclick; return b; };
  const hhmm = ts => new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  const when = ts => ts ? new Date(ts * 1000).toLocaleString('ru-RU') : '';
  const notice = (text, error = false) => { notify(text, { error }); };
  async function api(path, method = 'GET') {
    const r = await fetch('api/' + path, { method, headers: { 'X-Emulator': '1' } });
    const v = await r.json().catch(() => ({})); if (!r.ok) throw Error(v.error || `${r.status} ${r.statusText}`); return v;
  }
  async function work(fn) { try { await fn(); } catch (e) { notice(e.message, true); } }

  const filters = { level: '', source: '', q: '' };
  let sources = [], oldest = null, more = false, list = null;
  const opened = new Set();  // раскрытые detail переживают обновление

  const LEVEL_CLASS = { info: 'info', warning: 'warning', error: 'error' };
  const LEVEL_LABEL = { info: 'инфо', warning: 'внимание', error: 'ошибка' };

  // Строка — details: клик раскрывает detail (хост, путь, код, причина). Время — последнего случая.
  const entryRow = item => {
    const details = node('details', null, 'log-entry'); details.dataset.id = item.id;
    if (opened.has(String(item.id))) details.open = true;
    const summary = node('summary');
    summary.append(node('span', when(item.last_ts), 'log-time'));
    if (item.count > 1) {
      const glue = node('span', '×' + item.count, 'chip');
      glue.title = 'Случаев с ' + when(item.ts) + ' — повторы в пределах часа склеены';
      summary.append(glue, node('span', 'с ' + hhmm(item.ts), 'log-first'));
    }
    summary.append(node('span', LEVEL_LABEL[item.level] || item.level, 'log-level ' + (LEVEL_CLASS[item.level] || '')));
    summary.append(node('span', item.source, 'log-source'));
    summary.append(node('span', item.message, 'log-message'));
    if (item.device_sn) summary.append(node('span', item.device_sn, 'log-device'));
    details.addEventListener('toggle', () => {
      if (details.open) opened.add(String(item.id)); else opened.delete(String(item.id));
    });
    details.append(summary);
    if (item.detail && Object.keys(item.detail).length) {
      const pre = node('pre', JSON.stringify(item.detail, null, 2), 'log-detail');
      details.append(pre);
    }
    return details;
  };

  const levelSelect = () => {
    const select = node('select'); select.setAttribute('aria-label', 'Уровень');
    for (const [value, label] of [['', 'Все уровни'], ['warning,error', 'Предупреждения и ошибки'], ['error', 'Только ошибки']]) {
      select.append(new Option(label, value));
    }
    select.value = filters.level;
    select.onchange = () => { filters.level = select.value; work(load); };
    return select;
  };
  const sourceSelect = () => {
    const select = node('select'); select.setAttribute('aria-label', 'Источник');
    select.append(new Option('Все источники', ''));
    for (const name of sources) select.append(new Option(name, name));
    select.value = filters.source;
    if (filters.source && !sources.includes(filters.source)) { filters.source = ''; select.value = ''; }
    select.onchange = () => { filters.source = select.value; work(load); };
    return select;
  };
  const searchInput = () => {
    const input = node('input'); input.type = 'search'; input.placeholder = 'Поиск по тексту';
    input.setAttribute('aria-label', 'Поиск по тексту'); input.value = filters.q;
    let timer = null;
    input.addEventListener('input', () => {  // поиск по мере ввода (задержка 400 мс)
      clearTimeout(timer);
      timer = setTimeout(() => { const q = input.value.trim(); if (q !== filters.q) { filters.q = q; work(load); } }, 400);
    });
    return input;
  };
  const requestClear = () => {
    const dialog = document.createElement('dialog');
    dialog.append(node('p', 'Очистить журнал сервера целиком?'));
    const row = node('div', null, 'row');
    row.append(button('Очистить', () => { dialog.close(); dialog.remove(); work(async () => { await api('server_log', 'DELETE'); opened.clear(); await load(); notice('Журнал очищен.'); }); }),
      button('Отмена', () => { dialog.close(); dialog.remove(); }));
    dialog.append(row);
    dialog.addEventListener('cancel', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  };

  async function load(append = false) {
    const params = new URLSearchParams({ limit: '200' });
    if (filters.level) params.set('level', filters.level);
    if (filters.source) params.set('source', filters.source);
    if (filters.q) params.set('q', filters.q);
    if (append && oldest != null) params.set('before', String(oldest));
    const data = await api('server_log?' + params);
    sources = data.sources;
    more = data.more;
    oldest = data.items.length ? data.items[data.items.length - 1].last_ts : (append ? oldest : null);
    render(append ? [...current, ...data.items] : data.items);
  }
  let current = [];

  function render(items) {
    current = items;
    const head = node('div', null, 'sc-toolbar');
    head.append(node('h2', 'Лог'), button('Обновить', () => work(load)), button('Очистить', requestClear));
    const note = node('p', 'Сюда попадают события сервера: ошибки HTTP и TLS-фронта, сбои сценариев и обработчиков, пересылка наверх, самообновление, неудачный вход. Повторы в пределах часа склеены (×N). Хранение — 3 дня.', 'hint');
    const bar = node('div', null, 'sc-toolbar');
    bar.append(levelSelect(), sourceSelect(), searchInput());
    list = node('div', null, 'ep-misses');
    if (!items.length) list.append(node('p', 'Записей по фильтру нет.', 'hint'));
    for (const item of items) list.append(entryRow(item));
    if (more) list.append(button('Показать ещё', () => work(() => load(true))));
    root.replaceChildren(head, note, bar, list);
  }

  // Автообновление раз в 10 с, пока вкладка открыта и документ виден
  setInterval(() => {
    if (location.hash === '#server-log' && !document.hidden && !document.querySelector('dialog[open]')) work(load);
  }, 10000);

  const failed = e => { const head = node('div', null, 'sc-toolbar'); head.append(node('h2', 'Лог'), button('Повторить', () => work(load)));
    root.replaceChildren(head, node('p', 'Не удалось загрузить: ' + e.message, 'hint')); };
  const sync = () => { if (location.hash === '#server-log') load().catch(failed); };
  window.addEventListener('hashchange', sync);
  sync();
})();
