// Раздел «Эмулятор → Журнал» (TASK-043, TASK-054): трафик устройств, который пишется по опции
// «Запись» устройства (Устройства → Устройства). Список по устройствам: блок на устройство →
// группы событий (ключ, число всех случаев, время последнего) → до 10 записей группы.
// Фильтры: устройство, источник (эмулятор / Яндекс), только «без обработчика». Карточка записи
// (заголовки, тела, для AAPI — текстовый вид), экспорт записи JSON, очистка. API — ui/api/traffic.py.
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

  const SOURCE_NAME = { emulator: 'эмулятор', upstream: 'Яндекс' };
  const RECORD_NAME = { off: 'выключена', on: 'включена', misses: 'нет обработчиков' };
  const statusText = item => item.error ? 'ошибка' : (item.status || '—');
  const bytes = n => (n || 0) + ' Б';

  // --- карточка записи (dialog) ---------------------------------------------
  const appendHeaders = (form, headers, title) => {
    if (!(headers || []).length) return;
    const head = node('h4', title); head.style.margin = '10px 0 4px';
    form.append(head, pre(headers.map(([k, v]) => k + ': ' + v).join('\n')));
  };
  const appendBody = (form, title, value) => {
    if (!value) return;
    const head = node('h4', title); head.style.margin = '10px 0 4px';
    const body = pre(value.text, '220px');
    if (value.encoding !== 'text' && value.encoding !== 'json') body.title = 'Показано как ' + value.encoding;
    form.append(head, body);
  };
  const frameLine = frame => {  // кадры WS старых записей (до TASK-054)
    const line = node('div', null, 'ep-row');
    const label = node('span', '#' + frame.sequence + ' ' + (frame.direction === 'client_to_server' ? '→' : '←'), 'count');
    const size = node('span', bytes(frame.size), 'count'); size.title = when(frame.ts);
    line.append(label, size);
    if (frame.data) line.append(pre(frame.data.text));
    return line;
  };
  const cardTitle = item => item.kind === 'ws'
    ? (item.key || 'WS ' + (item.event || ''))
    : (item.method ? item.method + ' ' : '') + (item.path || item.host || '') + ' · ' + statusText(item);
  async function openCard(id) {
    const item = await api('traffic?item=' + id);
    const dialog = document.createElement('dialog'); dialog.className = 'catalog-dialog sc-edit-dialog';
    const form = node('form'); form.onsubmit = e => e.preventDefault();
    const head = node('div', null, 'sc-edit-head');
    const close = button('✕', () => dialog.close());
    close.className = 'icon-btn'; close.setAttribute('aria-label', 'Закрыть');
    head.append(node('h3', cardTitle(item)), close);
    form.append(head);
    const audio = item.audio_in_frames || item.audio_out_frames
      ? 'Звук (не записан): → ' + item.audio_in_frames + ' кадр., ' + bytes(item.audio_in_bytes) +
        '; ← ' + item.audio_out_frames + ' кадр., ' + bytes(item.audio_out_bytes) : '';
    const facts = node('p', [
      'Время: ' + when(item.ts),
      'Источник: ' + (SOURCE_NAME[item.source] || item.source || '—'),
      'Устройство: ' + (item.device_sn || item.device_id || '—'),
      'Хост: ' + (item.host || '—'),
      item.unhandled ? 'У эмулятора нет обработчика' : '',
      item.message_id ? 'MessageId: ' + item.message_id : '',
      item.ref_message_id ? 'RefMessageId: ' + item.ref_message_id : '',
      item.event === 'unknown' ? 'Неизвестный кадр: показан как есть' : '',
      item.kind === 'ws' ? '' : 'Размеры: ' + (item.request_size || 0) + ' → ' + (item.response_size || 0) + ' Б',
      item.duration_ms != null ? 'Длительность: ' + Math.round(item.duration_ms) + ' мс' : '',
      audio,
      item.error ? 'Ошибка: ' + item.error : '',
      item.close_code ? 'Код закрытия WS: ' + item.close_code : '',
    ].filter(Boolean).join('\n'), 'hint');
    facts.style.whiteSpace = 'pre-wrap';
    form.append(facts);
    appendHeaders(form, item.request_headers, 'Заголовки запроса');
    appendHeaders(form, item.response_headers, 'Заголовки ответа');
    appendBody(form, item.kind === 'ws' ? 'Кадр' : 'Тело запроса', item.request_body);
    appendBody(form, item.kind === 'ws' ? 'Кадр' : 'Тело ответа', item.response_body);
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
  const filters = { device_sn: '', source: '', unhandled: false };
  const opened = new Set();  // раскрытые блоки переживают «Обновить»
  let devices = [];

  const query = extra => {
    const params = new URLSearchParams(extra);
    if (filters.source) params.set('source', filters.source);
    if (filters.unhandled) params.set('unhandled', '1');
    return params.toString();
  };
  const deviceSelect = () => {
    const select = node('select'); select.setAttribute('aria-label', 'Устройство');
    select.append(new Option('Все устройства', ''));
    for (const entry of devices.filter(d => d.device_sn)) {
      select.append(new Option((entry.name && entry.name !== entry.device_sn ? entry.name + ' · ' : '') + entry.device_sn, entry.device_sn));
    }
    select.value = filters.device_sn;
    select.onchange = () => { filters.device_sn = select.value; work(load); };
    return select;
  };
  const sourceSelect = () => {
    const select = node('select'); select.setAttribute('aria-label', 'Источник');
    for (const [value, label] of [['', 'Все источники'], ['emulator', 'Эмулятор'], ['upstream', 'Яндекс']]) select.append(new Option(label, value));
    select.value = filters.source;
    select.onchange = () => { filters.source = select.value; work(load); };
    return select;
  };
  const unhandledToggle = () => {
    const label = node('label', null, 'mem-check');
    const box = node('input'); box.type = 'checkbox'; box.checked = filters.unhandled;
    box.onchange = () => { filters.unhandled = box.checked; work(load); };
    label.append(box, document.createTextNode(' только без обработчика'));
    return label;
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
    const main = node('button', null, 'tr-item'); main.type = 'button';
    main.append(node('b', when(item.ts)));
    main.append(node('small', [SOURCE_NAME[item.source] || item.source || '',
      item.unhandled ? 'нет обработчика' : '', item.error || ''].filter(Boolean).join(' · ')));
    main.onclick = () => work(() => openCard(item.id));
    const size = item.kind === 'ws' ? bytes(item.request_size || item.response_size) : String(statusText(item));
    row.append(main, node('span', size, 'count'));
    return row;
  };

  const groupBlock = (sn, group) => {
    const id = 'g:' + (sn || '') + '\n' + group.key;
    const details = node('details', null, 'traffic-group'); details.open = opened.has(id);
    const summary = node('summary');
    summary.append(node('b', group.key), document.createTextNode(' '), node('span', '×' + group.count, 'count'),
      document.createTextNode(' '), node('small', when(group.last_ts)));
    summary.title = 'Первый случай: ' + when(group.first_ts) + '; последний: ' + when(group.last_ts);
    const body = node('div', null, 'traffic-items');
    let loaded = false;
    const fill = () => work(async () => {
      const data = await api('traffic?' + query({ device_sn: sn || '', key: group.key }));
      body.replaceChildren(...(data.items.length ? data.items.map(itemLine) : [node('p', 'Записей по фильтру нет.', 'hint')]));
      loaded = true;
    });
    details.addEventListener('toggle', () => {
      if (details.open) { opened.add(id); if (!loaded) fill(); } else opened.delete(id);
    });
    details.append(summary, body);
    if (details.open) fill();
    return details;
  };

  const deviceBlock = entry => {
    const id = '-d:' + (entry.device_sn || '');  // в opened — свёрнутые устройства (по умолчанию раскрыты)
    const details = node('details'); details.open = !opened.has(id);
    const summary = node('summary');
    const named = entry.name && entry.name !== entry.device_sn;
    summary.append(node('b', named ? entry.name : (entry.device_sn || 'Без устройства')));
    if (named && entry.device_sn) summary.append(document.createTextNode(' · SN ' + entry.device_sn));
    if (entry.record) summary.append(document.createTextNode(' '), node('span', 'запись: ' + RECORD_NAME[entry.record], 'chip'));
    details.addEventListener('toggle', () => { if (details.open) opened.delete(id); else opened.add(id); });
    details.append(summary, ...entry.groups.map(group => groupBlock(entry.device_sn, group)));
    return details;
  };

  async function load() {
    const [all, shown] = await Promise.all([
      api('traffic?groups=1'),
      api('traffic?' + query({ groups: '1', ...(filters.device_sn ? { device_sn: filters.device_sn } : {}) }))]);
    devices = all.devices;
    const head = node('div', null, 'sc-toolbar');
    head.append(node('h2', 'Журнал'), button('Обновить', () => work(load)), button('Очистить', requestClear));
    const note = node('p', 'Пишется по опции «Запись» устройства (Устройства → Устройства). На событие хранятся последние 10 записей, счётчик «×N» — все случаи. Звук не пишется.', 'hint');
    const bar = node('div', null, 'sc-toolbar');
    bar.append(deviceSelect(), sourceSelect(), unhandledToggle());
    const list = node('div', null, 'ep-misses');
    if (!shown.devices.length) list.append(node('p', 'Записей нет: включите «Запись» устройству в разделе «Устройства» или смягчите фильтры.', 'hint'));
    for (const entry of shown.devices) list.append(deviceBlock(entry));
    root.replaceChildren(head, note, bar, list);
  }

  // Переход из других разделов («Без ответа» → Журнал) с готовым фильтром
  window.openTraffic = preset => {
    Object.assign(filters, preset || {});
    const same = location.hash === '#traffic';
    window.goTab('traffic');
    if (same) sync();
  };

  const failed = e => { const head = node('div', null, 'sc-toolbar'); head.append(node('h2', 'Журнал'), button('Повторить', () => work(load)));
    root.replaceChildren(head, node('p', 'Не удалось загрузить: ' + e.message, 'hint')); };
  const sync = () => { if (location.hash === '#traffic') load().catch(failed); };
  window.addEventListener('hashchange', sync);
  sync();
})();
