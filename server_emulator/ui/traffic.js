// Раздел «Эмулятор → Журнал» (TASK-043): трафик прохода наверх — обмены HTTP и сессии WS
// с настоящими серверами Яндекса (source=upstream). Фильтры по устройству/хосту/источнику,
// карточка обмена (заголовки, тела, кадры WS), экспорт записи JSON, очистка.
// API — ui/api/traffic.py.
(() => {
  const root = document.getElementById('traffic');
  if (!root) return;

  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (text, onclick, cls = 'secondary') => { const b = node('button', text, cls); b.type = 'button'; b.onclick = onclick; return b; };
  const when = ts => ts ? new Date(ts * 1000).toLocaleString('ru-RU') : '';
  const notice = text => { document.getElementById('notice').textContent = text; };
  const pre = (text, cap) => { const p = node('pre', text, 'hint'); p.style.whiteSpace = 'pre-wrap'; p.style.overflowWrap = 'anywhere'; if (cap) { p.style.maxHeight = cap; p.style.overflow = 'auto'; } return p; };
  async function api(path, method = 'GET') {
    const r = await fetch('api/' + path, { method, headers: { 'X-Emulator': '1' } });
    const v = await r.json().catch(() => ({})); if (!r.ok) throw Error(v.error || `${r.status} ${r.statusText}`); return v;
  }
  async function work(fn) { try { await fn(); } catch (e) { notice(e.message); } }

  const SOURCE_NAME = { emulator: 'эмулятор', upstream: 'наверх' };
  const statusText = item => item.error ? 'ошибка' : (item.status || '—');

  // --- карточка обмена (dialog) ---------------------------------------------
  const appendHeaders = (form, key, title) => {
    let pairs = [];
    try { pairs = JSON.parse(key === 'request' ? form.dataset.requestHeaders : form.dataset.responseHeaders || '[]'); } catch (e) { }
    const head = node('h4', title); head.style.margin = '10px 0 4px';
    form.append(head, pre(pairs.map(([k, v]) => k + ': ' + v).join('\n')));
  };
  const appendBody = (form, title, value) => {
    const head = node('h4', title); head.style.margin = '10px 0 4px';
    if (!value) { form.append(head, node('p', '—', 'hint')); return; }
    const body = pre(value.text, '220px');
    if (value.encoding !== 'text' && value.encoding !== 'json') body.title = 'Показано как ' + value.encoding;
    form.append(head, body);
  };
  const frameLine = frame => {
    const line = node('div', null, 'ep-row');
    const label = node('span', '#' + frame.sequence + ' ' + (frame.direction === 'client_to_server' ? '→ наверх' : '← оттуда'), 'count');
    const size = node('span', frame.size + ' Б', 'count'); size.title = when(frame.ts);
    line.append(label, size);
    if (frame.data) line.append(pre(frame.data.text));
    return line;
  };
  async function openCard(id) {
    const item = await api('traffic?item=' + id);
    const dialog = document.createElement('dialog'); dialog.className = 'catalog-dialog sc-edit-dialog';
    const form = node('form'); form.onsubmit = e => e.preventDefault();
    form.dataset.requestHeaders = JSON.stringify(item.request_headers || []);
    form.dataset.responseHeaders = JSON.stringify(item.response_headers || []);
    const head = node('div', null, 'sc-edit-head');
    const title = node('h3', (item.kind === 'ws' ? 'WS ' : (item.method ? item.method + ' ' : '')) +
      (item.path || item.host || '') + ' · ' + statusText(item));
    const close = button('✕', () => dialog.close());
    close.className = 'icon-btn'; close.setAttribute('aria-label', 'Закрыть');
    head.append(title, close);
    form.append(head);
    const facts = node('p', [
      'Время: ' + when(item.ts),
      'Источник: ' + (SOURCE_NAME[item.source] || item.source || '—'),
      'Устройство: ' + (item.device_sn || item.device_id || '—'),
      'Хост: ' + (item.host || '—'),
      'Размеры: ' + (item.request_size || 0) + ' → ' + (item.response_size || 0) + ' Б',
      'Длительность: ' + (item.duration_ms != null ? Math.round(item.duration_ms) + ' мс' : '—'),
      item.error ? 'Ошибка: ' + item.error : '',
      item.close_code ? 'Код закрытия WS: ' + item.close_code : '',
    ].filter(Boolean).join('\n'), 'hint');
    facts.style.whiteSpace = 'pre-wrap';
    form.append(facts);
    appendHeaders(form, 'request', 'Заголовки запроса');
    appendHeaders(form, 'response', 'Заголовки ответа');
    appendBody(form, 'Тело запроса', item.request_body);
    appendBody(form, 'Тело ответа', item.response_body);
    if ((item.frames || []).length) {
      const h = node('h4', 'Кадры WS' + (item.frames_truncated ? ' (запись ограничена лимитом)' : ''));
      h.style.margin = '10px 0 4px';
      form.append(h);
      for (const frame of item.frames) form.append(frameLine(frame));
    }
    const row = node('div', null, 'row');
    const exportLink = node('a', 'Экспорт JSON', 'button secondary');
    exportLink.href = 'api/traffic?export=' + item.id;
    exportLink.setAttribute('download', 'traffic-' + item.id + '.json');
    row.append(exportLink, button('Закрыть', () => dialog.close()));
    form.append(row);
    dialog.append(form);
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }

  // --- список ----------------------------------------------------------------
  const filters = {};
  const filterInput = (key, placeholder) => {
    const input = node('input'); input.placeholder = placeholder; input.value = filters[key] || '';
    input.oninput = () => { filters[key] = input.value.trim(); };
    return input;
  };
  const sourceSelect = () => {
    const select = node('select');
    for (const [value, label] of [['', 'Все источники'], ['upstream', 'наверх'], ['emulator', 'эмулятор']]) {
      const option = node('option', label); option.value = value; select.append(option);
    }
    select.value = filters.source || '';
    select.onchange = () => { filters.source = select.value; work(load); };
    return select;
  };
  const requestClear = () => {
    const dialog = document.createElement('dialog');
    dialog.append(node('p', 'Очистить журнал трафика целиком?'));
    const row = node('div', null, 'row');
    row.append(button('Очистить', () => { dialog.close(); dialog.remove(); work(async () => { await api('traffic', 'DELETE'); await load(); notice('Журнал очищен.'); }); }),
      button('Отмена', () => { dialog.close(); dialog.remove(); }));
    dialog.append(row);
    dialog.addEventListener('cancel', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  };
  const itemLine = item => {
    const row = node('div', null, 'ep-row');
    const main = node('button', null, 'ep-url'); main.type = 'button';
    main.style.cssText = 'all:unset;cursor:pointer;flex:1;min-width:0';
    main.append(node('b', (item.kind === 'ws' ? 'WS ' : (item.method || '')) + ' ' + (item.path || '')));
    main.append(node('small', (item.host || '') + ' · ' + when(item.ts) + (item.device_sn ? ' · ' + item.device_sn : '') + (item.error ? ' · ' + item.error : '')));
    main.onclick = () => work(() => openCard(item.id));
    const status = node('span', String(statusText(item)), 'count');
    status.title = 'Источник: ' + (SOURCE_NAME[item.source] || item.source || '—') + (item.error ? '; ' + item.error : '');
    row.append(main, status);
    return row;
  };

  async function load() {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
    const data = await api('traffic' + (params.toString() ? '?' + params : ''));
    const head = node('div', null, 'sc-toolbar');
    head.append(node('h2', 'Журнал'), node('span', String(data.total), 'count'),
      button('Обновить', () => work(load)), button('Очистить', requestClear));
    const bar = node('div', null, 'sc-toolbar');
    bar.append(filterInput('device', 'SN устройства'), filterInput('host', 'хост'), sourceSelect());
    const list = node('div', null, 'ep-misses');
    if (!data.items.length) list.append(node('p', 'Обменов с настоящими серверами пока не было: проход наверх включается режимами устройств (TASK-044).', 'hint'));
    for (const item of data.items) list.append(itemLine(item));
    root.replaceChildren(head, bar, list);
  }

  const failed = e => { const head = node('div', null, 'sc-toolbar'); head.append(node('h2', 'Журнал'), button('Повторить', () => work(load)));
    root.replaceChildren(head, node('p', 'Не удалось загрузить: ' + e.message, 'hint')); };
  const sync = () => { if (location.hash === '#traffic') load().catch(failed); };
  window.addEventListener('hashchange', sync);
  sync();
})();
