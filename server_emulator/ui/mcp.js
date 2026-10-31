(() => {
  // MCP: встроенные инструменты «Колонки Алисы» (всегда у модели) и внешние
  // MCP-серверы из стандартного state/mcp.json ({"mcpServers": {…}}), который
  // правится здесь же редактором. Выбор инструментов внешних серверов: включён ли
  // и режим — «Авто» (модель вызывает и получает результат сама) или «Подтверждение».
  // Сервер: server_emulator/mcp_hub.py, API: ui/api/mcp.py, контракт: docs/contracts/mcp.md.
  const root = document.getElementById('mcp');
  let data = null, filter = '';
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; };
  const notice = text => { document.getElementById('notice').textContent = text; };
  // Класс риска (agent/policy.py): внешнее, настройки и удаление агент выполняет после подтверждения
  const RISKS = [['read', 'Чтение'], ['device', 'Устройство'], ['external', 'Внешнее · подтверждение'],
    ['config', 'Настройки · подтверждение'], ['destructive', 'Удаление · подтверждение']];
  async function api(value) {
    const r = await fetch('api/mcp', { method: value ? 'POST' : 'GET', headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: value ? JSON.stringify(value) : undefined });
    const v = await r.json(); if (!r.ok) throw Error(v.error || r.statusText); return v;
  }
  async function work(fn) { try { await fn(); } catch (e) { notice(e.message); } }
  async function load() {
    data = await api(); render();
    // app.js держит копию настроек и сохраняет её целиком — обновляем, чтобы она не вернула старые tools
    try { settings = await (await fetch('api/settings')).json(); renderSettings(); } catch (e) { /* панель без app.js */ }
  }
  const matches = tool => !filter || ((tool.server_tool || tool.name) + ' ' + (tool.description || '')).toLowerCase().includes(filter);

  function toolRow(tool, builtin) {
    const row = el('div', null, 'mcp-tool' + (builtin || tool.enabled ? ' on' : ''));
    if (builtin) { const lock = el('span', '●', 'mcp-always'); lock.title = 'Встроенный — всегда у модели'; row.append(lock); }
    else {
      const check = el('input'); check.type = 'checkbox'; check.checked = !!tool.enabled;
      check.setAttribute('aria-label', 'Давать модели ' + tool.server_tool);
      check.onchange = () => work(async () => { await api({ op: 'tool', name: tool.name, enabled: check.checked }); await load(); });
      row.append(check);
    }
    const text = el('div', null, 'mcp-tool-text');
    text.append(el('b', tool.server_tool || tool.name), el('small', tool.description || ''));
    text.title = tool.description || '';
    let mode;
    if (builtin) mode = el('span', 'Авто', 'mcp-mode-fixed');
    else {
      mode = el('select'); mode.setAttribute('aria-label', 'Класс риска');
      for (const [v, t] of RISKS) { const o = el('option', t); o.value = v; mode.append(o); }
      mode.value = tool.risk || (tool.mode === 'confirm' ? 'external' : 'device'); mode.disabled = !tool.enabled;
      mode.onchange = () => work(async () => { await api({ op: 'tool', name: tool.name, risk: mode.value }); notice('Класс риска сохранён: ' + tool.server_tool); });
    }
    const run = el('button', 'Проверить', 'secondary'); run.type = 'button'; run.onclick = () => callDialog(tool);
    row.append(text, mode, run);
    return row;
  }

  function panel(title, subtitle, chipText, tools, builtin, extra) {
    const box = el('div', null, 'panel mcp-source');
    const head = el('div', null, 'mcp-source-head');
    const titles = el('div'); titles.append(el('h3', title), el('small', subtitle, 'hint'));
    head.append(titles, el('span', chipText, 'chip'));
    if (extra) head.append(extra);
    box.append(head);
    const shown = tools.filter(matches);
    const list = el('div', null, 'mcp-tools');
    for (const tool of shown) list.append(toolRow(tool, builtin));
    if (tools.length && !shown.length) list.append(el('p', 'Ничего не найдено по фильтру.', 'hint'));
    box.append(list);
    return box;
  }

  function serverPanel(server) {
    const tools = data.tools.filter(t => t.server_id === server.id);
    const actions = el('div', null, 'actions');
    if (!server.disabled) {
      const discover = el('button', tools.length ? 'Обновить список' : 'Получить инструменты', 'secondary'); discover.type = 'button';
      discover.onclick = () => work(async () => { discover.disabled = true; notice('tools/list · ' + server.id + '…'); try { const r = await api({ op: 'discover', server_id: server.id }); notice(server.id + ': инструментов ' + r.count); } finally { await load(); } });
      actions.append(discover);
    }
    if (tools.length) {
      const all = el('button', 'Включить все', 'secondary'), none = el('button', 'Выключить все', 'secondary');
      all.type = none.type = 'button';
      const bulk = on => work(async () => { for (const t of tools.filter(matches).filter(t => !!t.enabled !== on)) await api({ op: 'tool', name: t.name, enabled: on }); await load(); });
      all.onclick = () => bulk(true); none.onclick = () => bulk(false);
      actions.append(all, none);
    }
    const chip = server.disabled ? 'выключен' : tools.filter(t => t.enabled).length + ' из ' + tools.length;
    const box = panel(server.id, (server.type === 'stdio' ? 'stdio · ' : '') + server.target, chip, tools, false, actions);
    box.classList.toggle('off', server.disabled);
    if (server.error) box.querySelector('.mcp-source-head').after(el('p', server.error, 'mcp-error'));
    if (server.disabled) box.append(el('p', 'Выключен в mcp.json ("disabled": true): впишите настоящий URL и уберите disabled.', 'hint'));
    else if (!tools.length && !server.error) box.append(el('p', 'Инструменты ещё не получены.', 'hint'));
    return box;
  }

  // Пробный вызов: аргументы JSON-объектом, заготовка — из схемы инструмента
  function callDialog(tool) {
    const dialog = el('dialog', null, 'mcp-dialog');
    const head = el('header'); head.append(el('h3', tool.server_tool || tool.name));
    const close = el('button', '✕', 'icon-btn'); close.type = 'button'; close.setAttribute('aria-label', 'Закрыть'); close.onclick = () => dialog.close(); head.append(close);
    const schema = tool.inputSchema || {};
    const sample = Object.fromEntries(Object.entries(schema.properties || {}).filter(([k]) => (schema.required || []).includes(k))
      .map(([k, s]) => [k, s.enum ? s.enum[0] : s.type === 'number' || s.type === 'integer' ? 0 : s.type === 'boolean' ? false : s.type === 'array' ? [] : s.type === 'object' ? {} : '']));
    if (tool.name === 'db_query') sample.sql = 'SELECT name, room, online FROM speakers';
    const label = el('label', 'Аргументы (JSON)'), args = el('textarea', JSON.stringify(sample, null, 2), 'code');
    args.rows = 6; args.spellcheck = false; label.append(args);
    const details = el('details'); details.append(el('summary', 'Схема аргументов'), el('pre', JSON.stringify(schema, null, 2)));
    const out = el('pre', '', 'mcp-result'); out.hidden = true;
    const run = el('button', 'Вызвать'); run.type = 'button';
    run.onclick = () => work(async () => {
      run.disabled = true; out.hidden = false; out.classList.remove('bad'); out.textContent = 'Выполняется…';
      try {
        const r = await api({ op: 'call', name: tool.name, arguments: JSON.parse(args.value || '{}') });
        let text = r.run.result; try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { /* не JSON */ }
        out.textContent = text + '\n\n· ' + r.run.ms + ' мс'; out.classList.toggle('bad', !!r.result.isError);
      } catch (e) { out.textContent = e.message; out.classList.add('bad'); }
      finally { run.disabled = false; refreshLog(); }
    });
    const actions = el('div', null, 'actions'); actions.append(run);
    dialog.append(head, el('p', tool.description || '', 'hint'), label, details, actions, out);
    document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove()); dialog.showModal();
  }

  // Редактор state/mcp.json: стандартный формат клиентов MCP
  function configDialog() {
    const dialog = el('dialog', null, 'mcp-dialog mcp-config');
    const head = el('header'); head.append(el('h3', 'mcp.json'));
    const close = el('button', '✕', 'icon-btn'); close.type = 'button'; close.setAttribute('aria-label', 'Закрыть'); close.onclick = () => dialog.close(); head.append(close);
    const help = el('p', '{"mcpServers": {"имя": {"type": "http", "url": "…", "headers": {…}}}} или {"command": "…", "args": […], "env": {…}}; "disabled": true — выключить. ' + data.path, 'hint');
    const holder = el('div', null, 'mcp-editor');
    let getText;
    const out = el('pre', '', 'mcp-result'); out.hidden = true;
    const save = el('button', 'Сохранить и обновить инструменты'); save.type = 'button';
    save.onclick = () => work(async () => {
      save.disabled = true; out.hidden = false; out.classList.remove('bad'); out.textContent = 'Сохраняю, опрашиваю серверы…';
      try {
        const r = await api({ op: 'config_save', text: getText() });
        const lines = Object.entries(r.servers).map(([k, v]) => k + ': ' + (v.error ? 'ошибка — ' + v.error : 'инструментов ' + v.count));
        out.textContent = 'Сохранено.' + (lines.length ? '\n' + lines.join('\n') : '');
        out.classList.toggle('bad', Object.values(r.servers).some(v => v.error));
        await load();
      } catch (e) { out.textContent = e.message; out.classList.add('bad'); }
      finally { save.disabled = false; }
    });
    const actions = el('div', null, 'actions'); actions.append(save);
    dialog.append(head, help, holder, actions, out);
    document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove()); dialog.showModal();
    if (typeof CodeMirror !== 'undefined') {
      const cm = CodeMirror(holder, { value: data.config, mode: 'application/json', theme: 'material-darker', lineNumbers: true,
        matchBrackets: true, indentUnit: 2, tabSize: 2 });
      cm.setSize(null, 360); setTimeout(() => cm.refresh(), 0); cm.focus();
      getText = () => cm.getValue();
    } else {
      const area = el('textarea', data.config, 'code'); area.rows = 18; area.spellcheck = false; holder.append(area);
      getText = () => area.value;
    }
  }

  function logLines(target) {
    target.replaceChildren();
    const rows = data.log || [];
    if (!rows.length) { target.append(el('div', '— вызовов пока не было', 'log-console-empty')); return; }
    for (const run of rows) {
      const line = el('div', null, 'mcp-log-line' + (String(run.result).startsWith('Ошибка') ? ' bad' : ''));
      const time = new Date(run.ts * 1000);
      line.append(el('span', time.toLocaleTimeString('ru-RU'), 't'), el('span', run.origin === 'panel' ? 'панель' : 'модель', 'o'),
        el('span', run.name, 'n'), el('span', typeof run.arguments === 'string' ? run.arguments : JSON.stringify(run.arguments), 'a'),
        el('span', String(run.result).replace(/\s+/g, ' '), 'r'));
      line.title = String(run.result);
      target.append(line);
    }
    target.scrollTop = target.scrollHeight;
  }
  async function refreshLog() {
    const fresh = await api().catch(() => null); if (!fresh) return;
    data.log = fresh.log; const log = root.querySelector('.log-console'); if (log) logLines(log);
  }

  function render() {
    root.replaceChildren(el('h2', 'MCP · инструменты модели'),
      el('p', 'Встроенные инструменты модель получает всегда и вызывает сама. Внешние MCP-серверы — из mcp.json; у их инструментов «Авто» — модель вызывает и сама получает результат, «Подтверждение» — вызов ждёт ручного запуска во вкладке «Проверка».'));
    const toolbar = el('div', null, 'sc-toolbar');
    const edit = el('button', 'mcp.json'); edit.type = 'button'; edit.title = 'Редактор mcp.json'; edit.onclick = configDialog;
    const search = el('input'); search.type = 'search'; search.placeholder = 'Фильтр инструментов'; search.value = filter;
    search.oninput = () => { filter = search.value.trim().toLowerCase(); const pos = search.selectionStart; render(); const s = root.querySelector('input[type=search]'); s.focus(); s.setSelectionRange(pos, pos); };
    toolbar.append(edit, search);
    root.append(toolbar);
    root.append(panel(data.builtin.title, 'встроенные · всегда у модели', String(data.builtin.tools.length), data.builtin.tools, true));
    for (const server of data.servers) root.append(serverPanel(server));
    if (!data.servers.length) root.append(el('p', 'В mcp.json нет серверов — добавьте их кнопкой «mcp.json».', 'hint'));
    const head = el('div', null, 'sc-toolbar'); const reload = el('button', 'Обновить', 'secondary'); reload.type = 'button'; reload.onclick = () => work(refreshLog);
    head.append(el('h3', 'Журнал вызовов'), reload);
    const log = el('div', null, 'log-console'); log.setAttribute('role', 'log');
    root.append(head, log); logLines(log);
  }

  setInterval(() => { if (location.hash === '#mcp' && !document.hidden && data && !document.querySelector('dialog[open]')) refreshLog(); }, 10000);
  window.addEventListener('hashchange', () => { if (location.hash === '#mcp') work(load); });
  if (location.hash === '#mcp') work(load);
})();
