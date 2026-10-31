// Раздел «Эндпоинты»: сверху зафиксированные запросы без ответа (+ создаёт JSON),
// затем свёрнутые разделы своих JSON-эндпоинтов, в конце — маршруты, которые
// обслуживают другие вкладки (Квазар, Обновления). Редактор — модальное окно.
(() => {
  const root = document.getElementById('endpoints-list');
  if (!root) return;
  let catalog = null;

  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (text, onclick, cls = 'secondary') => { const b = node('button', text, cls); b.type = 'button'; b.onclick = onclick; return b; };
  const when = ts => ts ? new Date(ts * 1000).toLocaleString('ru-RU') : '';
  const CATEGORIES = { quasar: 'Квазар', gproxy: 'gproxy (rpc.alice)', egress: 'egress', other: 'Прочее' };
  const OWNERS = {
    'quasar-http': { title: 'Квазар — встроенная логика', tab: 'Квазар' },
    updates: { title: 'Обновления (OTA)', tab: 'Обновления' },
  };
  const HOSTS = ['quasar.yandex.net', 'rpc.alice.yandex.net', 'uniproxy.alice.yandex.net', 'egress.yandex.net'];
  const TYPES = ['application/json', 'application/x-protobuf', 'text/plain', 'application/octet-stream'];
  const TOKENS = ['device_id', 'platform', 'client_ip', 'host', 'method', 'path', 'now', 'now_ms', 'now_iso', 'uuid'];

  // Разделы по умолчанию свёрнуты; запоминаем развёрнутые.
  let expanded = new Set();
  try { expanded = new Set(JSON.parse(localStorage.getItem('endpointTreeExpanded') || '[]')); } catch { }
  const saveExpanded = () => { try { localStorage.setItem('endpointTreeExpanded', JSON.stringify([...expanded])); } catch { } };

  async function load() {
    catalog = await api('endpoints');
    render();
  }

  function render() {
    root.replaceChildren();
    const own = catalog.endpoints.filter(e => !e.readonly);
    const system = catalog.endpoints.filter(e => e.readonly);
    document.getElementById('endpoints-counts').textContent =
      `Своих: ${catalog.total} · включено: ${catalog.enabled} · без ответа: ${(catalog.misses || []).length} (Эмулятор → Без ответа)`;
    const groups = new Map();
    for (const e of own) {
      const name = e.category || 'other';
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(e);
    }
    for (const [name, items] of [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0], 'ru')))
      root.append(group('own:' + name, CATEGORIES[name] || name, node('span', 'JSON: state/endpoints/' + name), items, row));
    const owners = new Map();
    for (const e of system) {
      if (!owners.has(e.owner)) owners.set(e.owner, []);
      owners.get(e.owner).push(e);
    }
    for (const [owner, items] of owners) {
      const info = OWNERS[owner] || { title: owner, tab: owner };
      const link = node('a', 'обслуживает вкладка «' + info.tab + '» →'); link.href = '#' + owner;
      root.append(group('sys:' + owner, info.title, link, items, systemRow, 'system'));
    }
  }

  function group(key, title, note, items, makeRow, kind = '') {
    const box = node('div', null, 'sc-group ep-group ' + kind);
    const head = node('div', null, 'sc-group-head');
    const open = expanded.has(key);
    const toggle = () => { open ? expanded.delete(key) : expanded.add(key); saveExpanded(); render(); };
    const caret = button('▶', toggle, 'sc-caret' + (open ? ' open' : ''));
    const label = node('span', title, 'sc-title'); label.onclick = toggle;
    note.classList.add('ep-owner');
    head.append(caret, label, node('span', String(items.length), 'count'), note);
    box.append(head);
    if (open) {
      const rows = node('div', null, 'sc-group-rows');
      for (const e of items) rows.append(makeRow(e));
      box.append(rows);
    }
    return box;
  }

  const url = e => { const s = node('span', null, 'ep-url'); s.append(node('b', e.method), document.createTextNode(' ' + e.host + e.path)); return s; };

  function row(e) {
    const line = node('div', null, 'ep-row' + (e.enabled === false ? ' off' : ''));
    line.append(url(e));
    if (e.mode === 'script') line.append(node('span', 'py', 'chip'));
    const ext = String(e.origin || '').startsWith('extension:') ? e.origin.slice('extension:'.length) : '';
    if (ext) { const chip = node('span', ext, 'chip'); chip.title = 'Эндпоинт расширения ' + ext; line.append(chip); }
    if (e.title && e.title !== e.method + ' ' + e.path) line.append(node('span', e.title, 'ep-title'));
    const actions = node('span', null, 'ep-actions');
    actions.append(
      button('Редактировать', () => work(() => openEditor(e))),
      button(e.enabled === false ? 'Включить' : 'Отключить', () => work(async () => {
        await api('endpoints', 'PUT', { id: e.id, enabled: e.enabled === false });
        await load();
      })),
      ...(ext ? [] : [button('Удалить', () => {  // запись расширения только для чтения: убирается выключением расширения
        if (!confirm('Удалить эндпоинт ' + e.method + ' ' + e.host + e.path + '?')) return;
        work(async () => { await api('endpoints?id=' + encodeURIComponent(e.id), 'DELETE'); await load(); message('Эндпоинт удалён.'); });
      }, 'secondary danger')]));
    line.append(actions);
    return line;
  }

  function systemRow(e) {
    const line = node('div', null, 'ep-row');
    line.append(url(e));
    return line;
  }

  // ── Редактор (модальное окно) ────────────────────────────────────────
  const dialog = node('dialog', null, 'catalog-dialog ep-dialog');
  document.body.append(dialog);
  const f = {};
  let editing = null;    // копия редактируемого эндпоинта (null — новый)
  let source = 'static'; // 'static' | 'template' | 'script'
  let binary = null;     // base64 двоичного тела (текстом не редактируется)
  let editor = null;
  let newSource = 'manual';  // откуда новый эндпоинт: вручную или из зафиксированного запроса
  let currentMiss = null;    // зафиксированный запрос, из которого создаётся эндпоинт
  let hostApi = null;
  let bodyText = '';         // текст тела/шаблона (для static/template)
  let scriptCode = '';       // код .py (для script)
  window.endpointEditor = () => hostApi;  // расширение, загруженное позже окна
  // «Эмулятор → Без ответа» (misses.js): эндпоинт из зафиксированного запроса
  window.openEndpointFromMiss = miss => work(async () => { if (!catalog) await load(); await openEditor(null, miss); });

  function makeEditor(container) {
    if (typeof CodeMirror === 'undefined') {
      const ta = node('textarea', null, 'code'); ta.rows = 16; ta.spellcheck = false; container.append(ta);
      return { getValue: () => ta.value, setValue: v => { ta.value = v; }, refresh: () => { }, readOnly: v => { ta.readOnly = v; },
        mode: () => { }, insert: t => { const at = ta.selectionStart || 0; ta.setRangeText(t, at, at, 'end'); ta.focus(); } };
    }
    const cm = CodeMirror(container, { value: '', mode: 'application/json', theme: 'material-darker', lineNumbers: true,
      foldGutter: true, matchBrackets: true, indentUnit: 2, tabSize: 2, gutters: ['CodeMirror-linenumbers', 'CodeMirror-foldgutter'] });
    cm.setSize('100%', '360px');
    return { getValue: () => cm.getValue(), setValue: v => { cm.setValue(v); cm.clearHistory(); }, refresh: () => cm.refresh(),
      readOnly: v => cm.setOption('readOnly', v), mode: m => cm.setOption('mode', m), insert: t => { cm.replaceSelection(t); cm.focus(); } };
  }

  const field = (label, control) => { const l = node('label', label); l.append(control); return l; };
  const listInput = (values) => {
    const control = node('input'), list = node('datalist');
    list.id = 'ep-list-' + Math.random().toString(36).slice(2);
    for (const v of values) list.append(new Option(v));
    control.setAttribute('list', list.id); control._list = list;
    return control;
  };

  function build() {
    const form = node('form');
    f.heading = node('h3');
    f.method = node('select'); for (const m of ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']) f.method.append(new Option(m, m));
    f.host = listInput(HOSTS); f.path = node('input'); f.path.placeholder = '/путь';
    const urlRow = node('div', null, 'ep-form-row ep-form-url');
    urlRow.append(field('Метод', f.method), field('Хост', f.host), field('Путь', f.path), f.host._list);
    f.status = node('input'); f.status.type = 'number'; f.status.min = 100; f.status.max = 599;
    f.type = listInput(TYPES);
    f.category = listInput([]); f.category.placeholder = 'по хосту';
    f.enabled = node('input'); f.enabled.type = 'checkbox';
    const on = node('label', 'Включён', 'check'); on.append(f.enabled);
    const metaRow = node('div', null, 'ep-form-row ep-form-meta');
    metaRow.append(field('Статус', f.status), field('Content-Type', f.type), field('Раздел', f.category), on, f.type._list, f.category._list);
    f.title = node('input'); f.title.placeholder = 'необязательно';

    f.request = node('details', null, 'ep-request');
    const sourceRow = node('div', null, 'ep-source');
    f.static = button('Ответ как есть', () => work(() => setSource('static')));
    f.template = button('Шаблон с {{токенами}}', () => work(() => setSource('template')));
    f.script = button('Скрипт (.py)', () => work(() => setSource('script')));
    sourceRow.append(f.static, f.template, f.script, button('Форматировать JSON', formatBody));
    f.tokens = node('div', null, 'ep-tokens');
    for (const t of TOKENS) f.tokens.append(button('{{' + t + '}}', () => editor.insert('{{' + t + '}}'), 'chip'));
    f.binary = node('div', null, 'ep-binary');
    f.upload = node('input'); f.upload.type = 'file'; f.upload.hidden = true;
    f.upload.onchange = async () => {
      const file = f.upload.files[0]; if (!file) return;
      const bytes = new Uint8Array(await file.arrayBuffer());
      const text = decodeText(bytes, file.type);
      if (file.type) f.type.value = file.type;
      binary = text === null ? toBase64(bytes) : null;
      if (binary !== null) markSource('static');
      showBody(text === null ? '' : pretty(text));
      f.upload.value = '';
    };
    const box = node('div', null, 'ep-editor');
    f.editorBox = box;
    editor = makeEditor(box);
    const actions = node('div', null, 'row');
    const save = node('button', 'Сохранить'); save.type = 'submit';
    actions.append(button('Тело из файла…', () => f.upload.click()), f.upload, button('Отмена', () => dialog.close()), save);
    form.append(f.heading, urlRow, metaRow, field('Название', f.title), f.request, sourceRow, f.tokens, f.binary, box, actions);
    form.onsubmit = event => { event.preventDefault(); work(saveItem); };
    dialog.append(form);
    f.actions = actions;
    hostApi = {
      actions,                                   // ряд кнопок окна — сюда расширение добавляет свои
      request: () => ({ method: f.method.value, host: f.host.value.trim().toLowerCase(), path: f.path.value.trim(),
        query: currentMiss?.query || '', body_base64: currentMiss?.request_base64 || '', headers: currentMiss?.headers || [],
        content_type: currentMiss?.content_type || '' }),
      apply: ({ status, content_type, body_base64 }) => {   // подставить ответ в форму
        if (status) f.status.value = status;
        if (content_type) f.type.value = content_type.split(';')[0].trim();
        const text = decodeText(fromBase64(body_base64), f.type.value.trim());
        binary = text === null ? body_base64 : null;
        markSource('static'); showBody(text === null ? '' : pretty(text));
      },
    };
    // Хост (прокси) расширяет редактор: событие приходит один раз на окно.
    document.dispatchEvent(new CustomEvent('endpoint-editor-ready', { detail: hostApi }));
  }

  const toBase64 = bytes => { let s = ''; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000)); return btoa(s); };
  const fromBase64 = b64 => Uint8Array.from(atob(b64 || ''), c => c.charCodeAt(0));
  const TEXT_MIME = /^(text\/|application\/(json|.*\+json|xml|.*\+xml|javascript|x-www-form-urlencoded))/i;
  const MEDIA_MIME = /^(image|video|audio)\//i;
  const PROTOBUF_MIME = /protobuf|grpc/i;
  const decodeText = (bytes, mime = '') => {
    if (mime && !TEXT_MIME.test(mime)) return null;
    // NUL/управляющие — не текст, если тип не объявлен текстовым (JSON/текст показываем как есть)
    if (!mime && bytes.subarray(0, 4096).some(b => b < 9 || (b > 13 && b < 32))) return null;
    try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); } catch { return null; }
  };
  const pretty = text => { try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; } };

  let mediaUrl = null;
  function showBody(text) {
    f.binary.hidden = binary === null;
    f.editorBox.hidden = binary !== null;
    if (mediaUrl) { URL.revokeObjectURL(mediaUrl); mediaUrl = null; }
    if (binary !== null) {
      const bytes = fromBase64(binary), mime = f.type.value.trim();
      f.binary.replaceChildren(node('span', `Двоичное тело: ${mime || 'тип не указан'}, ${bytes.length} байт — отдаётся как есть.`));
      if (!bytes.some(b => b !== 0)) f.binary.append(node('span', ' Все байты нулевые — захват битый.', 'ep-bad'));
      else if (MEDIA_MIME.test(mime)) {
        mediaUrl = URL.createObjectURL(new Blob([bytes], { type: mime }));
        const kind = mime.split('/')[0];
        const media = node(kind === 'image' ? 'img' : kind); media.src = mediaUrl; media.className = 'ep-media';
        if (kind !== 'image') media.controls = true;
        f.binary.append(media);
      }
      if (PROTOBUF_MIME.test(mime)) {
        if (protoTypes.length) f.binary.append(f.protoBox);
        f.binary.append(button('Разбор', () => work(parseProtobuf)));
      }
      const download = node('a', 'Скачать'); download.className = 'button secondary';
      download.href = URL.createObjectURL(new Blob([bytes], { type: mime || 'application/octet-stream' }));
      download.download = (f.path.value.split('/').pop() || 'body') + '.bin';
      f.binary.append(download);
    }
    editor.readOnly(binary !== null);
    editor.setValue(binary !== null ? '' : text);
    setTimeout(() => editor.refresh(), 0);
  }

  // Двоичный protobuf → скрипт с разобранным сообщением (тело остаётся запасным ответом).
  // Тип по схеме: выбранный вручную или найденный сервером по пути /gproxy/<метод>.
  async function parseProtobuf() {
    const r = await api('endpoints-protobuf', 'POST', { id: editing?.id || suggestId(f.host.value.trim(), f.path.value.trim()),
      path: f.path.value.trim(), type: f.protoType.value.trim(), body_base64: binary, content_type: f.type.value.trim() });
    scriptCode = r.code;
    await setSource('script');
    if (r.notice) message('Схема не подошла, разбор без схемы: ' + r.notice, true);
    else message(r.type ? 'Разобран по схеме ' + r.type + ' — правьте MESSAGE и сохраните'
      : 'Protobuf разобран в скрипт без схемы — правьте MESSAGE и сохраните');
  }

  // Импортированные схемы (zip с .proto): имена типов для выбора при разборе.
  let protoTypes = [];
  let protoTypesLoaded = false;
  async function loadProtoTypes() {
    if (protoTypesLoaded) return;
    const r = await api('endpoints-proto-import');
    protoTypesLoaded = true;
    protoTypes = r.types || [];
    const importButton = document.getElementById('endpoints-proto-import');
    importButton.title = r.meta ? `Импортировано: ${(r.meta.sources || [{ name: r.meta.source }]).map(x => x.name).join(', ')}; `
      + `${r.meta.files} файлов, ${r.meta.messages} типов (${String(r.meta.imported).slice(0, 10)})` : '';
    if (!f.protoType) {
      f.protoType = listInput([]); f.protoType.placeholder = 'тип по схеме (авто по пути)'; f.protoType.className = 'ep-proto-type';
      f.protoBox = node('span'); f.protoBox.append(f.protoType, f.protoType._list);
    }
    f.protoType._list.replaceChildren(...protoTypes.map(t => new Option(t)));
  }

  function markSource(next) {
    source = next;
    f.static.className = next === 'static' ? '' : 'secondary';
    f.template.className = next === 'template' ? '' : 'secondary';
    f.script.className = next === 'script' ? '' : 'secondary';
    f.tokens.hidden = next !== 'template';
  }

  async function setSource(next) {
    if (next === 'template') {
      // Шаблон строится из текущего JSON-ответа: правьте значения, вставляйте токены.
      const currentBody = source === 'script' ? bodyText : editor.getValue();
      if (binary !== null) { message('Двоичное тело нельзя превратить в шаблон: загрузите JSON.', true); return; }
      try { JSON.parse(currentBody || 'null'); } catch { message('Ответ не JSON: шаблон строится только из JSON.', true); return; }
    }

    if (source === 'script' && next !== 'script') {
      // Уходим из режима скрипта — сохраняем код скрипта
      scriptCode = editor.getValue();
      editor.mode('application/json');  // подсветка по типу кода: тело — JSON
      // Показываем тело/шаблон
      showBody(binary !== null ? '' : bodyText);
    } else if (source !== 'script' && next === 'script') {
      // Переходим в режим скрипта — сохраняем текущее тело
      bodyText = editor.getValue();
      // Загружаем код скрипта
      if (!scriptCode) {
        const id = editing?.id || '';
        const r = await api('endpoints?script=' + encodeURIComponent(id));
        scriptCode = r.code || r.stub;
      }
      editor.readOnly(false);
      f.binary.hidden = true;
      f.editorBox.hidden = false;
      editor.mode('python');  // подсветка: код эндпоинтов — python (vendor/python.min.js)
      editor.setValue(scriptCode);
      editor.refresh();
    }
    markSource(next);
  }

  function formatBody() {
    try { editor.setValue(JSON.stringify(JSON.parse(editor.getValue()), null, 2)); }
    catch (e) { message('Не JSON: ' + e.message, true); }
  }

  async function openEditor(e, miss) {
    if (!dialog.firstChild) build();
    editor.mode('application/json');  // режим по типу кода: тело — JSON, скрипт — python
    await loadProtoTypes();
    f.protoType.value = '';
    editing = e ? JSON.parse(JSON.stringify(e)) : null;
    if (editing) delete editing.origin;  // метка списка, не поле определения (у записи расширения правка сохранит пользовательскую копию)
    newSource = miss ? 'miss' : 'manual';
    currentMiss = miss || null;
    scriptCode = ''; // сброс буфера скрипта

    const item = editing || { method: miss?.method || 'GET', host: miss?.host || '', path: miss?.path || '', status: 200,
      content_type: 'application/json', enabled: true, body_base64: '', source: miss ? 'miss' : 'manual' };
    f.heading.textContent = e ? e.method + ' ' + e.host + e.path : 'Новый эндпоинт';
    f.method.value = item.method; f.host.value = item.host; f.path.value = item.path;
    f.status.value = item.status || 200; f.type.value = item.content_type || 'application/json';
    f.category.value = item.category || '';
    f.category._list.replaceChildren(...[...new Set(Object.keys(CATEGORIES).concat(catalog.categories || []))]
      .filter(c => c !== 'updates').map(c => new Option(c)));
    f.enabled.checked = item.enabled !== false;
    f.title.value = item.title && item.title !== item.method + ' ' + item.path ? item.title : '';
    f.request.hidden = !miss;
    if (miss) {
      const body = decodeText(fromBase64(miss.request_base64), miss.content_type);
      f.request.replaceChildren(node('summary', `Запрос (×${miss.count}, последний ${when(miss.last_ts)})`),
        node('pre', (miss.query ? '?' + miss.query + '\n' : '') + (miss.content_type ? miss.content_type + '\n' : '')
          + (body === null ? `(двоичное тело, ${miss.request_size} байт)` : pretty(body) || '(без тела)'), 'code'));
    }

    // Загружаем скрипт если режим script или есть has_script
    if (e && (e.mode === 'script' || e.has_script)) {
      const r = await api('endpoints?script=' + encodeURIComponent(e.id));
      scriptCode = r.code || '';
    }

    if (item.template != null) {
      binary = null; markSource('template'); showBody(JSON.stringify(item.template, null, 2));
      bodyText = editor.getValue();
    } else {
      const text = decodeText(fromBase64(item.body_base64), item.content_type);
      binary = text === null ? item.body_base64 : null;
      markSource('static'); showBody(text === null ? '' : pretty(text));
      bodyText = editor.getValue();
    }

    // Если эндпоинт в режиме script — переключаемся в него после показа тела
    if (e && e.mode === 'script') {
      markSource('script');
      editor.readOnly(false);
      f.binary.hidden = true;
      f.editorBox.hidden = false;
      editor.mode('python');
      editor.setValue(scriptCode);
      editor.refresh();
      // bodyText уже сохранён выше — не перезаписываем его
    }

    dialog.showModal();
    setTimeout(() => editor.refresh(), 0);
  }

  function suggestId(host, path) {
    return host + '.' + ((path.replace(/^\//, '') || 'root').replace(/[/?]/g, '.').replace(/[^a-z0-9._-]/gi, '_'));
  }

  async function saveItem() {
    const host = f.host.value.trim().toLowerCase(), path = f.path.value.trim();
    const item = Object.assign(editing || { source: newSource }, {
      method: f.method.value, host, path,
      status: Number(f.status.value) || 200,
      content_type: f.type.value.trim() || 'application/json',
      enabled: f.enabled.checked,
      title: f.title.value.trim() || f.method.value + ' ' + path,
    });
    item.id = item.id || suggestId(host, path);
    if (f.category.value.trim()) item.category = f.category.value.trim(); else delete item.category;

    if (source === 'template') {
      item.template = JSON.parse(editor.getValue());
      item.body_base64 = item.body_base64 || '';
      item.mode = 'template';
      await api('endpoints', 'PUT', { endpoint: item });
    } else if (source === 'script') {
      const code = editor.getValue();
      item.mode = 'script';
      // тело/шаблон берутся из bodyText как для static
      if (binary !== null) {
        item.body_base64 = binary;
      } else {
        item.body_base64 = toBase64(new TextEncoder().encode(bodyText));
      }
      delete item.template;
      await api('endpoints', 'PUT', { endpoint: item, script: code });
    } else { // static
      delete item.template;
      item.body_base64 = binary !== null ? binary : toBase64(new TextEncoder().encode(editor.getValue()));
      item.mode = 'static';
      await api('endpoints', 'PUT', { endpoint: item });
    }
    dialog.close();
    await load();
    const saved = catalog.endpoints.find(x => x.id === item.id);
    if (saved) { expanded.add('own:' + saved.category); saveExpanded(); render(); }
    message('Эндпоинт сохранён: ' + item.method + ' ' + item.host + item.path);
  }

  document.getElementById('endpoints-new').onclick = () => work(async () => { if (!catalog) await load(); await openEditor(null); });
  document.getElementById('endpoints-reload').onclick = () => work(load);
  // Импорт схем: zip с .proto, сервер собирает их protoc в state/proto/; режим — обновить или заменить набор.
  const protoFile = document.getElementById('endpoints-proto-file');
  document.getElementById('endpoints-proto-import').onclick = () => protoFile.click();
  const askMode = file => new Promise(resolve => {
    const box = document.createElement('dialog');
    box.className = 'catalog-dialog ep-proto-dialog';
    box.innerHTML = '<form method="dialog"><h3></h3><p class="hint">Обновить — файлы из архива заменят одноимённые, '
      + 'остальные схемы останутся. Заменить — набор будет только из этого архива.</p><div class="row">'
      + '<button value="update">Обновить</button><button value="replace" class="secondary">Заменить</button>'
      + '<button value="" class="secondary">Отмена</button></div></form>';
    box.querySelector('h3').textContent = 'Импорт proto: ' + file.name;
    box.onclose = () => { box.remove(); resolve(box.returnValue); };
    document.body.append(box); box.showModal();
  });
  protoFile.onchange = () => work(async () => {
    const file = protoFile.files[0]; protoFile.value = '';
    if (!file) return;
    const mode = await askMode(file);
    if (!mode) return;
    const importButton = document.getElementById('endpoints-proto-import'), label = importButton.textContent;
    importButton.disabled = true; importButton.textContent = 'Импорт…';
    const started = Date.now(), timer = setInterval(() => {  // сборка большого архива идёт десятки секунд
      importButton.textContent = `Импорт… ${Math.round((Date.now() - started) / 1000)} с`;
    }, 1000);
    try {
      const r = await fetch(`api/endpoints-proto-import?mode=${mode}&name=` + encodeURIComponent(file.name), { method: 'POST',
        headers: { 'X-Emulator': '1', 'Content-Type': 'application/octet-stream' }, body: file });
      const result = await r.json();
      if (!r.ok) throw Error(result.error || r.statusText);
      const bound = Object.values(result.bindings || {}).filter(Boolean).length, last = result.last || {};
      message(`Схемы ${mode === 'replace' ? 'заменены' : 'обновлены'} из ${file.name}: собрано ${last.compiled} из ${last.found}`
        + (last.duplicates ? ` (дубликатов ${last.duplicates})` : '') + (mode === 'update' ? `, заменено ${last.replaced}` : '') + `; всего ${result.files} файлов, ${result.messages} типов, `
        + `методов gproxy с типом ${bound} из ${Object.keys(result.bindings || {}).length}`
        + (result.dropped.length ? `; не собрались: ${result.dropped.length}` : '')
        + (result.stubs.length ? `, нет в архиве: ${result.stubs.length}` : ''));
      protoTypesLoaded = false; await loadProtoTypes();
    } finally { clearInterval(timer); importButton.disabled = false; importButton.textContent = label; }
  });
  const sync = () => { if (location.hash === '#endpoints') work(async () => { await load(); await loadProtoTypes(); }); };
  window.addEventListener('hashchange', sync);
  document.addEventListener('DOMContentLoaded', sync);  // api/work объявляет app.js, он грузится позже
})();