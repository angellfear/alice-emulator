// «Эмулятор → Шаблоны»: реестр шаблонов экранных директив (divui_show_view,
// draw_animation_directive). Список шаблонов, редактор JSON (CodeMirror),
// предпросмотр div-карточки (DivKit web в sandbox-iframe) и отправка на колонку
// (Push с собранной директивой в живую сессию SN). API — ui/api/templates.py;
// контракт — docs/contracts/templates.md.
(() => {
  const section = document.getElementById('templates');
  if (!section) return;
  const make = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const api = async (action, method, value) => {
    const r = await fetch('api/' + action, { method: method || 'GET',
      headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' },
      body: value === undefined ? undefined : JSON.stringify(value) });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  };
  const work = async fn => {
    const buttons = [...section.querySelectorAll('button')];
    buttons.forEach(b => b.disabled = true);
    try { await fn(); } catch (e) { notify(e.message, { error: true }); }
    finally { buttons.forEach(b => b.disabled = false); }
  };
  const SOURCE = { builtin: 'встроенный', override: 'переопределён', custom: 'свой' };

  function cell(title, node) {
    const box = make('div', null, 'ws-name');
    box.append(make('span', title, 'ws-muted'));
    box.append(node);
    return box;
  }

  function table(headers, rows) {
    const tb = make('div', null, 'tbl');
    const tbl = document.createElement('table');
    const thead = document.createElement('thead');
    const hr = document.createElement('tr');
    headers.forEach(h => hr.append(make('th', h)));
    thead.append(hr); tbl.append(thead);
    const tbody = document.createElement('tbody');
    rows.forEach(r => tbody.append(r));
    tbl.append(tbody); tb.append(tbl);
    return tb;
  }

  function templateRows(list, onOpen) {
    return list.map(t => {
      const tr = document.createElement('tr');
      const name = make('div', null, 'ws-name');
      name.append(make('b', t.id));
      if (t.title) name.append(make('span', t.title, 'ws-filter'));
      const src = make('span', SOURCE[t.source] || t.source);
      if (t.source !== 'builtin') src.classList.add('chip');
      tr.append(
        cell('Шаблон', name),
        cell('Директива', make('code', t.name)),
        cell('Переменные', make('code', t.variables.length ? t.variables.map(v => '{{' + v + '}}').join(' ') : '—')),
        cell('Источник', src));
      tr.onclick = () => onOpen(t);
      return tr;
    });
  }

  function head() {
    const bar = make('div', null, 'sc-toolbar ws-head');
    const refresh = make('button', 'Обновить', 'secondary'); refresh.type = 'button';
    refresh.onclick = () => work(load);
    bar.append(refresh);
    return bar;
  }

  async function load() {
    if (location.hash !== '#templates') return;
    const data = await api('templates');
    const root = make('div');
    root.append(head());
    root.append(make('p', 'Экранные директивы собираются из шаблонов: встроенные — образцы настоящего сервера (app_cards); правка встроенного создаёт переопределение в state/screen_templates, оно сильнее встроенного, «Сбросить» возвращает образец. Подстановки {{имя}} заменяются значениями при сборке и отправке; <uuid> — свежим request_id.', 'hint'));
    if (!data.templates.length) root.append(make('p', 'Шаблонов нет.', 'hint'));
    else root.append(table(['Шаблон', 'Директива', 'Переменные', 'Источник'],
      templateRows(data.templates, openEditor)));
    section.replaceChildren(root);
  }

  // ── редактор шаблона: <dialog> с CodeMirror ─────────────────────────────
  function openEditor(t) {
    work(async () => {
      const entry = await api('templates?id=' + encodeURIComponent(t.id));
      const dlg = document.createElement('dialog');
      dlg.className = 'catalog-dialog sc-edit-dialog';
      const form = document.createElement('form');
      const headRow = make('div', null, 'sc-edit-head');
      headRow.append(make('h3', 'Шаблон: ' + entry.id));
      const close = make('button', '✕', 'icon-btn'); close.type = 'button'; close.setAttribute('aria-label', 'Закрыть');
      close.onclick = () => dlg.close();
      headRow.append(close);
      form.append(headRow);
      if (entry.title) form.append(make('p', entry.title, 'hint'));
      const err = make('p', null, 'sc-err'); err.hidden = true;
      form.append(err);
      const box = make('div');
      form.append(box);
      const cm = makeCM(box, { value: JSON.stringify(entry.body, null, 1) });
      const vars = entry.variables.length
        ? 'Переменные: ' + entry.variables.map(v => '{{' + v + '}}').join(', ') + '; <uuid> заменяется свежим при сборке.'
        : 'Подстановок нет; <uuid> заменяется свежим при сборке.';
      form.append(make('p', vars, 'hint'));

      // значения переменных и колонка — для предпросмотра и отправки
      const valuesBox = make('div', null, 'vs-flags');
      const inputs = {};
      entry.variables.forEach(v => {
        const input = make('input'); input.type = 'text'; input.placeholder = v;
        inputs[v] = input;
        const label = make('label', null, 'vs-flag');
        label.append(make('span', '{{' + v + '}}'), input);
        valuesBox.append(label);
      });
      const sessions = make('select');
      const sendRow = make('div', null, 'vs-flags');
      const sl = make('label', null, 'vs-flag');
      sl.append(make('span', 'Колонка'), sessions);
      sendRow.append(sl);
      const identities = await api('identities');
      identities.sessions.filter(s => s.device_sn && s.user_id).forEach(s => {
        const o = make('option', s.device_sn); o.value = s.device_sn; sessions.append(o);
      });
      form.append(valuesBox, sendRow);

      const row = make('div', null, 'row');
      const saveBtn = make('button', 'Сохранить'); saveBtn.type = 'submit';
      const previewBtn = make('button', 'Предпросмотр', 'secondary'); previewBtn.type = 'button';
      const sendBtn = make('button', 'Отправить на колонку', 'secondary'); sendBtn.type = 'button';
      const cancel = make('button', 'Закрыть', 'secondary'); cancel.type = 'button'; cancel.onclick = () => dlg.close();
      row.append(saveBtn, previewBtn, sendBtn, cancel);
      if (entry.source === 'override') {
        const resetBtn = make('button', 'Сбросить', 'secondary'); resetBtn.type = 'button';
        resetBtn.onclick = async () => {
          err.hidden = true;
          try {
            await api('templates', 'POST', { op: 'reset', id: entry.id });
            notify('Сброшено к встроенному: ' + entry.id);
            dlg.close(); await load();
          } catch (e) { fail(e); }
        };
        row.append(resetBtn);
      }
      form.append(row);
      dlg.append(form);
      document.body.append(dlg);
      dlg.showModal();

      const fail = e => { err.textContent = e.message; err.hidden = false; };
      const readBody = () => {
        try { return JSON.parse(cm.getValue()); }
        catch (e) { throw Error('JSON: ' + e.message); }
      };
      const values = () => {
        const result = {};
        for (const [name, input] of Object.entries(inputs)) if (input.value.trim()) result[name] = input.value.trim();
        return result;
      };

      const save = async () => {
        err.hidden = true;
        try {
          const body = readBody();
          await api('templates', 'PUT', { id: entry.id, body, name: body.Name, title: entry.title || '' });
          notify('Сохранено: ' + entry.id);
          dlg.close(); await load();
        } catch (e) { fail(e); }
      };
      saveBtn.onclick = save;
      form.onsubmit = e => { e.preventDefault(); save(); };
      previewBtn.onclick = () => {
        err.hidden = true;
        try { openPreview(entry.id, readBody(), values()); }
        catch (e) { fail(e); }
      };
      sendBtn.onclick = async () => {
        err.hidden = true;
        if (!sessions.value) { fail(Error('Нет активных колонок: привяжите станцию к эмулятору')); return; }
        try {
          await api('templates', 'POST', { op: 'send', id: entry.id, sn: sessions.value, values: values() });
          notify('Отправлено на ' + sessions.value);
        } catch (e) { fail(e); }
      };
    });
  }

  // ── предпросмотр: DivKit web в sandbox-iframe ───────────────────────────
  function openPreview(id, body, values) {
    const card = previewCard(body, values);
    const dlg = document.createElement('dialog');
    dlg.className = 'catalog-dialog preview-dialog';
    dlg.append(make('h3', 'Предпросмотр: ' + id));
    const frame = document.createElement('iframe');
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.className = 'template-preview';
    frame.srcdoc = previewDoc(card);
    dlg.append(frame);
    const row = make('div', null, 'row');
    const close = make('button', 'Закрыть', 'secondary'); close.type = 'button'; close.onclick = () => dlg.close();
    row.append(close);
    dlg.append(row);
    document.body.append(dlg);
    dlg.showModal();
  }

  // DivKit-карточка из тела шаблона: StringBody (строка или объект) → {templates, card};
  // {{name}} — значениями из формы (пусто — остаётся как есть), <uuid> — пробный uuid
  function previewCard(body, values) {
    const card = body.Div2Card || {};
    let inner = card.StringBody;
    if (typeof inner === 'string') inner = JSON.parse(inner);
    const filled = JSON.stringify(inner)
      .replace(/\{\{([a-z0-9_]+)\}\}/g, (m, name) => values && values[name] !== undefined ? values[name] : m)
      .replace(/<uuid>/g, '00000000-0000-4000-8000-000000000000');
    return JSON.parse(filled);
  }

  function previewDoc(card) {
    return '<!doctype html><html><head><meta charset="utf-8"><style>'
      + 'html,body{margin:0;height:100%;background:#0b0f14;color:#e6eef9;font-family:Inter,system-ui,sans-serif}'
      + '</style><script>window.exports={};<\/script>'
      + '<script src="vendor/divkit.min.js"><\/script>'
      + '<script>try{window.exports.render({id:"preview",target:document.body,card:'
      + JSON.stringify(card).replace(/</g, '\\u003c') + '});}catch(e){document.body.textContent="DivKit: "+e.message;}<\/script>'
      + '</head><body></body></html>';
  }

  // ── редактор кода: CodeMirror (vendor/), запас — textarea ───────────────
  function makeCM(container, { value = '', height = '420px' } = {}) {
    if (typeof CodeMirror === 'undefined') {
      const ta = make('textarea', null, 'code'); ta.value = value; ta.rows = 14; ta.spellcheck = false;
      container.append(ta);
      return { getValue: () => ta.value, refresh: () => {} };
    }
    const cm = CodeMirror(container, { value, mode: 'application/json', theme: 'material-darker',
      lineNumbers: true, foldGutter: true, indentUnit: 2, tabSize: 2, matchBrackets: true,
      gutters: ['CodeMirror-linenumbers', 'CodeMirror-foldgutter'] });
    cm.setSize('100%', height);
    setTimeout(() => cm.refresh(), 0);
    return { getValue: () => cm.getValue(), refresh: () => cm.refresh() };
  }

  window.addEventListener('hashchange', load);
  load();
})();
