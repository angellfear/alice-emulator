(() => {
  // Память агента: что он помнит о пользователе реестра — факты, сводки разговоров,
  // пожелания к ответам. Пишется по просьбе («запомни»/«забудь») и фоном после паузы
  // в диалоге. Сервер: server_emulator/agent/memory.py, API: ui/api/memory.py,
  // контракт: docs/contracts/memory.md.
  const root = document.getElementById('memory');
  let data = null, user = null;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; };
  const notice = (text, error = false) => { notify(text, { error }); };
  async function api(value, query = '') {
    const r = await fetch('api/memory' + query, { method: value ? 'POST' : 'GET', headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: value ? JSON.stringify(value) : undefined });
    const v = await r.json(); if (!r.ok) throw Error(v.error || r.statusText); return v;
  }
  async function work(fn) { try { await fn(); } catch (e) { notice(e.message, true); } }
  async function load() {
    data = await api(null, user ? '?user=' + encodeURIComponent(user) : '');
    user = data.user; render();
  }
  const field = (label, input) => { const box = el('label', null, 'mem-field'); box.append(el('span', label), input); return box; };
  const input = (value, type = 'text') => { const i = el('input'); i.type = type; i.value = value ?? ''; return i; };

  function settingsPanel() {
    const s = data.settings, box = el('div', null, 'panel');
    box.append(el('h3', 'Настройки'));
    const enabled = el('input'); enabled.type = 'checkbox'; enabled.checked = !!s.enabled;
    const on = el('label', null, 'mem-check'); on.append(enabled, el('span', 'Память включена'));
    const url = input(s.embed.url), model = input(s.embed.model), key = input(s.embed.api_key, 'password');
    url.placeholder = 'http://127.0.0.1:11434/v1/embeddings'; model.placeholder = 'bge-m3';
    key.placeholder = s.embed.api_key ? 'Не менять' : 'Не нужен';
    const recall = input(s.recall_limit, 'number'), reflect = input(s.reflect_after_s, 'number');
    const grid = el('div', null, 'mem-grid');
    grid.append(field('Эмбеддинги · URL', url), field('Модель', model), field('API-ключ', key),
      field('Фактов в промпт', recall), field('Разбор после паузы, с', reflect));
    const save = el('button', 'Сохранить'), test = el('button', 'Проверить эмбеддинги', 'secondary');
    save.type = test.type = 'button';
    save.onclick = () => work(async () => {
      await api({ op: 'settings', enabled: enabled.checked, recall_limit: +recall.value, reflect_after_s: +reflect.value,
        embed: { url: url.value.trim(), model: model.value.trim(), api_key: key.value || (s.embed.api_key ? '__KEEP__' : '') } });
      notice('Настройки памяти сохранены'); await load();
    });
    test.onclick = () => work(async () => { const r = await api({ op: 'test_embed' }); notice('Эмбеддинги работают: вектор ' + r.dims); });
    const actions = el('div', null, 'actions'); actions.append(save, test);
    box.append(on, grid, actions);
    return box;
  }

  function list(title, kind, items, addable) {
    const box = el('div', null, 'panel');
    const head = el('div', null, 'mem-head'); head.append(el('h3', title), el('span', String(items.length), 'chip'));
    box.append(head);
    const rows = el('div', null, 'mem-list');
    for (const item of items) {
      const row = el('div', null, 'mem-row');
      const text = el('span', item.text); text.title = item.key;
      const del = el('button', 'Удалить', 'secondary'); del.type = 'button';
      del.onclick = () => work(async () => { await api({ op: 'delete', user, kind, key: item.key }); await load(); });
      row.append(el('time', item.updated.slice(0, 10)), text, del);
      rows.append(row);
    }
    if (!items.length) rows.append(el('p', 'Пусто.', 'hint'));
    box.append(rows);
    if (addable) {
      const add = input(''), button = el('button', 'Добавить', 'secondary');
      add.placeholder = 'Пользователь любит джаз'; button.type = 'button';
      button.onclick = () => work(async () => { await api({ op: 'put', user, kind, text: add.value }); await load(); });
      const line = el('div', null, 'mem-add'); line.append(add, button); box.append(line);
    }
    return box;
  }

  function instructionsPanel(items) {
    const box = el('div', null, 'panel');
    box.append(el('h3', 'Пожелания к ответам'));
    const text = el('textarea'); text.rows = 3; text.value = items[0]?.text || '';
    text.placeholder = 'Отвечай коротко, обращайся на «ты»';
    const save = el('button', 'Сохранить', 'secondary'); save.type = 'button';
    save.onclick = () => work(async () => { await api({ op: 'put', user, kind: 'instructions', text: text.value }); notice('Пожелания сохранены'); });
    box.append(text, save);
    return box;
  }

  function logPanel() {
    const box = el('div', null, 'panel');
    box.append(el('h3', 'Фоновый разбор диалогов'));
    const log = el('div', null, 'log-console'); log.setAttribute('role', 'log');
    for (const r of data.log) {
      const what = r.error ? 'ошибка: ' + r.error : r.skipped || ('реплик ' + r.messages + ', фактов ' + r.facts);
      log.append(el('div', new Date(r.ts * 1000).toLocaleTimeString() + '  ' + r.thread + '  ' + what, r.error ? 'mem-bad' : null));
    }
    if (!data.log.length) log.append(el('div', '— разборов пока не было', 'log-console-empty'));
    box.append(log);
    return box;
  }

  function render() {
    root.replaceChildren();
    root.append(el('h2', 'Память · агент'), el('p', 'Агент помнит владельца колонки: факты и предпочтения, сводки прошлых разговоров, пожелания к ответам. '
      + 'Пишет по просьбе («запомни», «забудь») и сам — после паузы в диалоге. Без эмбеддингов в промпт идут последние записи, с ними — подходящие к реплике.'));
    if (!data.available) { root.append(el('p', 'Агент не установлен: pip install -e ".[agent]"', 'hint')); return; }
    root.append(settingsPanel());
    if (!data.users.length) { root.append(el('p', 'Нет пользователей реестра.', 'hint')); return; }
    const bar = el('div', null, 'sc-toolbar mem-bar');
    const select = el('select'); select.setAttribute('aria-label', 'Пользователь');
    for (const u of data.users) { const o = el('option', u.name); o.value = u.id; select.append(o); }
    select.value = user; select.onchange = () => work(async () => { user = select.value; await load(); });
    const reflect = el('button', 'Разобрать диалоги сейчас', 'secondary'), forget = el('button', 'Забыть всё', 'danger');
    reflect.type = forget.type = 'button';
    reflect.onclick = () => work(async () => { const r = await api({ op: 'reflect', user }); notice('Разобрано тредов: ' + r.records.length); await load(); });
    forget.onclick = () => work(async () => {
      if (!confirm('Удалить всю память пользователя «' + select.selectedOptions[0].text + '»?')) return;
      const r = await api({ op: 'forget', user }); notice('Удалено записей: ' + r.removed); await load();
    });
    bar.append(select, reflect, forget);
    root.append(bar, list('Факты и предпочтения', 'facts', data.items.facts, true),
      list('Прошлые разговоры', 'episodes', data.items.episodes, false), instructionsPanel(data.items.instructions), logPanel());
  }

  window.addEventListener('hashchange', () => { if (location.hash === '#memory') work(load); });
  if (location.hash === '#memory') work(load);
})();
