// Раздел «Дополнительно → Расширения»: источники (каталог / GitHub / zip / локальная папка),
// карточки установленных расширений (версия, класс, что добавляет, разрешения, самопроверка,
// настройки по schema манифеста), окно подтверждения установки кода (список .py, разрешения,
// размер, python-зависимости). API — ui/api/extensions.py (менеджер extensions/manager.py),
// режимы «Интеллекта» для кнопки «Набор: всё через X» — /api/intelligence (TASK-034).
(() => {
  const section = document.getElementById('extensions');
  if (!section) return;
  const root = document.createElement('div');
  section.append(root);

  const FOLDERS = { scenarios: 'сценарии', endpoints: 'эндпоинты', quasar: 'конфиги Quasar', voice: 'голосовой стек',
    lists: 'списки фраз', modes: 'режимы «Интеллекта»', mcp: 'MCP-серверы', actions: 'действия сценариев',
    datasets: 'источники данных', animations: 'анимации', params: 'параметры устройств', replies: 'стандартные ответы',
    catalog: 'каталог приложений', updates: 'правила OTA', sounds: 'звуки', ui: 'вкладки' };
  const CLASSES = { data: 'данные', code: 'код', install: 'установка' };
  const KINDS = { folder: 'папка', github: 'GitHub', zip: 'zip', catalog: 'каталог' };
  const CHECK_DOT = { ok: 'running', warning: 'stopped', skipped: '' };

  const make = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const say = (text, error = false) => { notify(text, { error }); };
  const call = async (op, value = {}) => {
    const r = await fetch('api/extensions', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Emulator': '1' }, body: JSON.stringify({ op, ...value }) });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  };
  const getJson = async path => {
    const r = await fetch('api/' + path, { headers: { 'X-Emulator': '1' } });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  };
  const sizeText = (v = 0) => v >= 1048576 ? (v / 1048576).toFixed(1) + ' МБ' : v >= 1024 ? (v / 1024).toFixed(1) + ' КБ' : v + ' Б';
  const kindOf = url => !url.includes('://') ? 'folder'
    : /^https?:\/\/(www\.)?github\.com\//i.test(url) ? 'github'
      : /\.zip$/i.test(url.split('#')[0].split('?')[0]) ? 'zip' : 'catalog';

  let data = null, modes = [], busy = false;
  const catalogs = {};   // url каталога → записи, полученные кнопкой «Проверить обновления» (в этой сессии страницы)
  const drafts = {};     // id расширения → {values}: введённые, но не сохранённые настройки
  const openSettings = new Set();
  let sourceDraft = '';

  // Любое действие: блокирует кнопки, показывает ошибку в строке состояния, перечитывает список.
  async function work(fn) {
    if (busy) return;
    busy = true;
    root.querySelectorAll('button,input,select,textarea').forEach(b => { b.disabled = true; });
    try { await fn(); } catch (e) { say(e.message, true); } finally {
      busy = false;
      try { await load(); } catch (e) { say(e.message, true); }
    }
  }
  const button = (text, fn, cls = 'secondary') => { const b = make('button', cls, text); b.type = 'button'; b.onclick = () => work(fn); return b; };
  // изменение набора расширений влияет на режимы «Интеллекта», списки сценариев и настройки в app.js
  const changed = async () => { try { await window.refreshSettings?.(); } catch (e) { say(e.message, true); } };

  // --- окно подтверждения (в странице, не confirm()) --------------------------------------
  function ask(title, body, okText, danger = false) {
    return new Promise(resolve => {
      const dialog = make('dialog', 'catalog-dialog ext-dialog');
      const done = value => { dialog.close(); dialog.remove(); resolve(value); };
      const cancel = make('button', 'secondary', 'Отмена'); cancel.type = 'button'; cancel.onclick = () => done(false);
      const ok = make('button', danger ? 'danger' : null, okText); ok.type = 'button'; ok.onclick = () => done(true);
      const row = make('div', 'row'); row.append(cancel, ok);
      dialog.append(make('h3', null, title), ...body, row);
      dialog.addEventListener('cancel', e => { e.preventDefault(); done(false); });
      document.body.append(dialog);
      dialog.showModal();
      cancel.focus();
    });
  }
  const group = (title, items, empty) => {
    const box = make('div', 'ext-group');
    box.append(make('h4', null, title));
    if (!items.length) { box.append(make('p', 'ext-line', empty)); return box; }
    const list = make('ul', 'ext-list');
    for (const text of items.slice(0, 15)) list.append(make('li', null, text));
    if (items.length > 15) list.append(make('li', null, '… и ещё ' + (items.length - 15)));
    box.append(list);
    return box;
  };
  const permissionLines = p => {
    const out = [];
    if (p?.network?.length) out.push('сеть: ' + p.network.join(', '));
    if (p?.actions?.length) out.push('действия сценариев: ' + p.actions.join(', '));
    if (p?.device_push) out.push('отправка команд на колонки');
    if (p?.install) out.push('скачивание программ и моделей');
    return out;
  };
  // Карточка подтверждения из ответа install/update с requires_confirmation
  function confirmInstall(card, okText) {
    const body = [make('p', 'ext-line', (CLASSES[card.class] || card.class) + ' · версия ' + card.version + ' · ' + sizeText(card.size)),
      make('p', 'vs-desc', card.description || '')];
    body.push(group('Файлы Python (' + (card.py_files || []).length + ')', card.py_files || [], 'нет'));
    if ((card.folders || []).length) body.push(group('Добавляет', card.folders.map(f => FOLDERS[f] || f), ''));
    body.push(group('Разрешения', permissionLines(card.permissions), 'не запрашивает'));
    if ((card.python_packages || []).length) body.push(group('Python-зависимости (после установки — отдельной кнопкой)', card.python_packages, ''));
    body.push(make('p', 'error', 'Код исполняется в процессе сервера без песочницы. Устанавливайте только то, чему доверяете.'));
    return ask(card.name + ' — подтверждение', body, okText, true);
  }

  // --- действия ----------------------------------------------------------------------------
  async function install(spec) {
    let result = await call('install', spec);
    if (result.requires_confirmation) {
      if (!await confirmInstall(result, 'Установить')) return;
      result = await call('install', { ...spec, confirm: true });
    }
    await changed();
    say('Установлено: ' + result.installed + ' ' + result.version + ' — теперь его можно включить.');
  }
  async function update(card) {
    let result = await call('update', { id: card.id });
    if (result.requires_confirmation) {
      if (!await confirmInstall(result, 'Обновить')) return;
      result = await call('update', { id: card.id, confirm: true });
    }
    await changed();
    say(result.dev ? 'Перечитано: ' + card.id + ' ' + result.version : 'Обновлено: ' + card.id + ' → ' + result.version);
  }
  async function installPackages(card) {
    const first = await call('install_packages', { id: card.id });
    const body = [group('pip install в окружение сервера', first.packages, ''), make('p', 'ext-line', 'Установка может занять несколько минут.')];
    if (!await ask(card.name + ' — Python-зависимости', body, 'Установить пакеты')) return;
    await call('install_packages', { id: card.id, confirm: true });
    say('Python-пакеты установлены: ' + first.packages.join(', '));
  }
  async function remove(card) {
    const text = card.dev ? 'Подключение будет снято, папка расширения останется на диске.' : 'Файлы и настройки расширения будут удалены.';
    if (!await ask('Удалить «' + (card.name || card.id) + '»?', [make('p', 'vs-desc', text)], 'Удалить', true)) return;
    await call('uninstall', { id: card.id });
    delete drafts[card.id];
    await changed();
    say('Удалено: ' + card.id);
  }
  // «Набор: всё через X» — пресеты голосового стека из MODE.preset ({asr|tts|llm: "p:<пресет>" | "k:<пакет>" | id пресета}),
  // затем режим «Интеллекта» ext:<id режима>
  async function applyPreset(mode) {
    for (const service of ['asr', 'tts', 'llm']) {
      const value = mode.preset?.[service];
      if (!value) continue;
      const ref = /^[pk]:/.test(value) ? value : 'p:' + value;
      const r = await fetch('api/voice-stack-apply', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Emulator': '1' }, body: JSON.stringify({ service, ref }) });
      const v = await r.json().catch(() => ({}));
      if (!r.ok) throw Error('Набор «' + mode.title + '», ' + service.toUpperCase() + ': ' + (v.error || r.statusText));
    }
    await changed();
    await window.setLevel?.('ext:' + mode.id);
    say('Набор применён: всё через «' + mode.title + '».');
  }
  async function checkCatalogs() {
    const urls = (data.sources || []).filter(u => kindOf(u) === 'catalog');
    if (!urls.length) throw Error('Нет источников-каталогов: проверять обновления нечем. Для остальных источников — «Обновить» в карточке.');
    const errors = [];
    for (const url of urls) {
      try { catalogs[url] = await call('catalog', { url }); } catch (e) { errors.push(url + ': ' + e.message); delete catalogs[url]; }
    }
    if (errors.length) throw Error('Каталог недоступен — ' + errors.join('; '));
    say('Каталоги обновлены.');
  }

  // --- поля настроек (тот же формат fields/when, что у пакетов голосового стека) ---------------
  const shown = (f, values) => Object.entries(f.when || {}).every(([k, v]) => String(values[k]) === String(v));
  function field(spec, values) {
    const label = make('label', 'vs-field', spec.label);
    let el;
    if (spec.type === 'select') {
      el = make('select');
      for (const o of spec.options || []) el.append(typeof o === 'object' ? new Option(o.label, o.value) : new Option(o, o));
    } else if (spec.type === 'textarea') { el = make('textarea'); el.rows = 3; }
    else {
      el = make('input'); el.type = spec.type === 'number' ? 'number' : spec.type === 'password' ? 'password' : 'text';
      if (spec.type === 'number') el.step = spec.step || 'any';
      if (spec.type === 'password') el.autocomplete = 'off';
    }
    el.value = values[spec.key] ?? '';
    el.oninput = () => { values[spec.key] = spec.type === 'number' && el.value !== '' ? Number(el.value) : el.value; };
    if (spec.type === 'select') el.onchange = () => { el.oninput(); render(); };  // поля с when зависят от выбора
    label.append(el);
    return label;
  }

  // --- отрисовка -----------------------------------------------------------------------------
  function sourcesPanel() {
    const panel = make('div', 'panel');
    const head = make('div', 'sc-toolbar');
    head.append(make('h3', null, 'Источники'), button('Проверить обновления', async () => { await checkCatalogs(); await changed(); }));
    panel.append(head);

    const add = make('div', 'ext-add');
    const input = make('input'); input.placeholder = 'https://…/index.json, https://github.com/автор/репозиторий, ссылка на .zip или путь к папке';
    input.setAttribute('aria-label', 'Новый источник'); input.value = sourceDraft;
    input.oninput = () => { sourceDraft = input.value; };
    const addSource = button('Добавить', async () => {
      const url = input.value.trim(); if (!url) throw Error('Укажите URL или путь к папке');
      await call('add_source', { url }); sourceDraft = '';
    }, null);
    input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); addSource.click(); } };
    add.append(input, addSource);
    panel.append(add);

    const sources = data.sources || [];
    if (!sources.length) panel.append(make('p', 'hint', 'Источников пока нет.'));
    for (const url of sources) {
      const kind = kindOf(url), row = make('div', 'ext-source');
      row.append(make('span', 'chip', KINDS[kind]), make('span', 'ext-url', url));
      if (kind !== 'catalog') row.append(button('Установить', () => install({ url })));
      row.append(button('Убрать', async () => { await call('remove_source', { url }); delete catalogs[url]; }, 'secondary danger'));
      panel.append(row);
      for (const entry of catalogs[url] || []) panel.append(catalogEntry(url, entry));
    }
    return panel;
  }
  function catalogEntry(url, entry) {
    const has = data.extensions.find(x => x.id === entry.id);
    const row = make('div', 'ext-source ext-entry');
    const text = make('span', 'ext-url');
    text.append(make('b', null, entry.id), document.createTextNode(' ' + (entry.version || '') + (entry.description ? ' · ' + entry.description : '')));
    row.append(text);
    if (!has) row.append(button('Установить', () => install({ url, id: entry.id })));
    else row.append(make('span', 'chip', has.version === entry.version ? 'установлено' : 'есть ' + has.version));
    return row;
  }

  function card(c) {
    const box = make('section', 'vs-card ext-card'), head = make('div', 'ext-head');
    head.append(make('span', 'vs-dot' + (c.enabled ? ' on' : '')), make('h3', null, c.name || c.id), make('span', 'chip', c.version || '?'));
    if (c.class) head.append(make('span', 'chip' + (c.class === 'data' ? '' : ' warn'), CLASSES[c.class] || c.class));
    head.append(make('span', 'ext-state', c.enabled ? 'включено' : 'выключено'));
    box.append(head);
    if (c.error) { box.append(make('pre', 'vs-log', c.error)); }
    if (c.description) box.append(make('p', 'vs-desc', c.description));
    const meta = [c.id, c.author, c.license].filter(Boolean).join(' · ');
    if (meta) box.append(make('p', 'ext-line', meta));
    box.append(make('p', 'ext-line', 'Источник: ' + (c.dev ? 'локальная папка ' + c.dev : c.source || '—')));
    if (c.folders?.length) box.append(make('p', 'ext-line', 'Добавляет: ' + c.folders.map(f => FOLDERS[f] || f).join(', ')));
    if (c.permissions) box.append(make('p', 'ext-line', 'Разрешения: ' + (permissionLines(c.permissions).join('; ') || 'нет')));
    if (c.check) {
      const line = make('p', 'ext-line ext-check'), dot = make('span', 'vs-dot' + (CHECK_DOT[c.check.status] ? ' ' + CHECK_DOT[c.check.status] : ''));
      line.append(dot, document.createTextNode(' Самопроверка: ' + (c.check.detail || c.check.status)));
      box.append(line);
    }
    if (c.update) box.append(make('p', 'ext-line', 'Доступна версия ' + c.update.version));
    if (c.python_packages?.length) {
      const line = make('p', 'ext-line', 'Python-пакеты: ' + c.python_packages.join(', '));
      box.append(line);
    }

    const actions = make('div', 'vs-actions');
    if (c.enabled) actions.append(button('Выключить', async () => { await call('disable', { id: c.id }); await changed(); say('Выключено: ' + c.id); }));
    else actions.append(button('Включить', async () => { await call('enable', { id: c.id }); await changed(); say('Включено: ' + c.id); }, null));
    if (c.dev || c.source) actions.append(button(c.dev ? 'Перечитать' : 'Обновить', () => update(c), c.update ? null : 'secondary'));
    if (c.previous) actions.append(button('Откатить', async () => { await call('rollback', { id: c.id }); await changed(); say('Откат: ' + c.id + ' → ' + c.previous); }));
    if (c.python_packages?.length) actions.append(button('Установить пакеты', () => installPackages(c)));
    for (const mode of modes.filter(m => m.ext === c.id && m.preset))
      actions.append(button('Набор: всё через ' + mode.title, () => applyPreset(mode)));
    actions.append(button('Удалить', () => remove(c), 'secondary danger'));
    box.append(actions);

    const fields = c.settings?.fields?.filter(f => f.type !== 'file') || [];
    if (fields.length && !c.error) {
      const details = make('details', 'ext-settings'); details.open = openSettings.has(c.id);
      details.ontoggle = () => { details.open ? openSettings.add(c.id) : openSettings.delete(c.id); };
      details.append(make('summary', null, 'Настройки'));
      const d = drafts[c.id] || (drafts[c.id] = { values: { ...c.settings.values } });
      const form = make('div', 'vs-fields');
      fields.filter(f => shown(f, d.values)).forEach(f => form.append(field(f, d.values)));
      const save = button('Сохранить настройки', async () => { await call('set', { id: c.id, values: d.values }); delete drafts[c.id]; say('Настройки сохранены: ' + c.id); });
      const row = make('div', 'vs-actions'); row.append(save);
      details.append(form, row);
      box.append(details);
    }
    return box;
  }

  function render() {
    if (!data) return;
    const list = data.extensions || [];
    const grid = make('div', 'vs-grid');
    list.forEach(c => grid.append(card(c)));
    root.replaceChildren(sourcesPanel(),
      make('h3', 'ext-title', 'Установленные' + (list.length ? ' · ' + list.length : '')),
      list.length ? grid : make('p', 'hint', 'Пока ничего не установлено: добавьте источник и нажмите «Установить».'));
    if (busy) root.querySelectorAll('button,input,select,textarea').forEach(b => { b.disabled = true; });
  }

  async function load() {
    data = await getJson('extensions');
    try { modes = (await getJson('intelligence')).modes || []; } catch (e) { modes = []; }
    for (const id of Object.keys(drafts)) if (!data.extensions.some(c => c.id === id)) delete drafts[id];
    render();
  }

  // Ошибка загрузки — в самой секции, а не пустой раздел
  const failed = e => {
    const retry = make('button', 'secondary', 'Повторить'); retry.type = 'button'; retry.onclick = () => sync();
    root.replaceChildren(make('p', 'hint', 'Не удалось загрузить: ' + e.message), retry);
  };
  const sync = () => { if (location.hash === '#extensions') load().catch(failed); };
  window.addEventListener('hashchange', sync);
  sync();
})();
