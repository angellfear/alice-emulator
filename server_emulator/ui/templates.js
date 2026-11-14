// «Эмулятор → Шаблоны divui»: реестр шаблонов экранных директив (divui_show_view,
// draw_animation_directive, divui_hide_view). Список с кнопкой «Изменить», редактор
// JSON (CodeMirror) рядом с живым предпросмотром DivKit (sandbox-iframe), значения
// переменных и отправка на колонку (Push с собранной директивой в живую сессию SN).
// Редактор один за раз: повторное открытие заменяет диалог, закрытие удаляет его
// из DOM. API — ui/api/templates.py; контракт — docs/contracts/templates.md.
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
  let editorDlg = null;   // открытый редактор: второй клик заменяет, а не наслаивает
  let opening = false;    // гонка двух быстрых кликов по строке

  function dismissEditor() {
    // без опоры на событие close: в части движков оно не срабатывает — окно
    // «не закрывалось» и накапливалось в DOM
    const dlg = editorDlg;
    editorDlg = null;
    if (!dlg) return;
    try { if (dlg.open) dlg.close(); } catch (e) { /* уже закрыт */ }
    dlg.remove();
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
      const td = node => { const c = document.createElement('td'); c.append(node); return c; };
      const name = make('div', null, 'ws-name');
      name.append(make('b', t.id));
      if (t.title) name.append(make('span', t.title, 'ws-filter'));
      const src = make('span', SOURCE[t.source] || t.source);
      if (t.source !== 'builtin') src.classList.add('chip');
      const edit = make('button', 'Изменить', 'secondary');
      edit.type = 'button';
      edit.onclick = e => { e.stopPropagation(); onOpen(t); };
      const actions = make('div', null, 'tpl-actions');
      actions.append(edit);
      tr.append(td(name),
        td(make('code', t.name)),
        td(make('code', t.variables.length ? t.variables.map(v => '{{' + v + '}}').join(' ') : '—', 'tpl-vars')),
        td(src), td(actions));
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
    root.append(make('p', 'Экранные директивы собираются из шаблонов: встроенные — образцы настоящего сервера (app_cards), правка создаёт переопределение в state/screen_templates — оно сильнее встроенного, «Сбросить» в редакторе возвращает образец. В редакторе справа живой предпросмотр карточки: {{имя}} — значения переменных, <uuid> — свежий request_id при сборке.', 'hint'));
    if (!data.templates.length) root.append(make('p', 'Шаблонов нет.', 'hint'));
    else root.append(table(['Шаблон', 'Директива', 'Переменные', 'Источник', ''],
      templateRows(data.templates, t => work(() => openEditor(t)))));
    section.replaceChildren(root);
  }

  // ── редактор шаблона: код + живой предпросмотр + отправка ───────────────
  async function openEditor(t) {
    if (opening) return;
    opening = true;
    try {
      if (editorDlg && editorDlg.dataset.tplId === t.id && editorDlg.open) return;
      dismissEditor();  // закрывает и удаляет прошлый редактор
      const entry = await api('templates?id=' + encodeURIComponent(t.id));
      const dlg = document.createElement('dialog');
      dlg.className = 'catalog-dialog sc-edit-dialog tpl-dialog';
      dlg.dataset.tplId = entry.id;
      const form = document.createElement('form');

      const headRow = make('div', null, 'sc-edit-head');
      headRow.append(make('h3', 'Шаблон: ' + entry.id));
      const close = make('button', '✕', 'icon-btn'); close.type = 'button'; close.setAttribute('aria-label', 'Закрыть');
      headRow.append(close);
      form.append(headRow);

      const meta = make('p', null, 'tpl-meta');
      meta.append(make('code', entry.name));
      meta.append(make('span', SOURCE[entry.source] || entry.source, 'chip'));
      if (entry.title) meta.append(make('span', entry.title));
      form.append(meta);

      const grid = make('div', null, 'tpl-grid');
      const codeBox = make('div', null, 'tpl-code');
      const previewPane = make('div', null, 'tpl-preview-pane');
      previewPane.append(make('div', 'Предпросмотр на колонке', 'tpl-preview-head'));
      const previewErr = make('div', null, 'tpl-preview-err'); previewErr.hidden = true;
      previewPane.append(previewErr);
      const frame = document.createElement('iframe');
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.className = 'template-preview';
      frame.title = 'Предпросмотр карточки';
      const frameWrap = make('div', null, 'tpl-preview-frame');
      frameWrap.append(frame);
      previewPane.append(frameWrap);
      grid.append(codeBox, previewPane);
      form.append(grid);

      const foot = make('div', null, 'tpl-foot');
      const err = make('p', null, 'sc-err'); err.hidden = true;
      const fail = e => { err.textContent = e.message; err.hidden = false; };

      const varsRow = make('div', null, 'tpl-vars-row');
      const inputs = {};
      entry.variables.forEach(v => {
        const input = make('input'); input.type = 'text'; input.placeholder = v;
        input.addEventListener('input', schedule);
        inputs[v] = input;
        const label = make('label', null, 'tpl-var');
        label.append(make('span', '{{' + v + '}}'), input);
        varsRow.append(label);
      });
      const sessions = make('select');
      const sl = make('label', null, 'tpl-station');
      sl.append(make('span', 'Колонка'), sessions);
      varsRow.append(sl);
      foot.append(varsRow, err);

      const row = make('div', null, 'row');
      const saveBtn = make('button', 'Сохранить'); saveBtn.type = 'submit';
      row.append(saveBtn);
      if (entry.source === 'override') {
        const resetBtn = make('button', 'Сбросить', 'secondary'); resetBtn.type = 'button';
        resetBtn.onclick = async () => {
          err.hidden = true;
          try {
            await api('templates', 'POST', { op: 'reset', id: entry.id });
            notify('Сброшено к встроенному: ' + entry.id);
            dismissEditor(); await load();
          } catch (e) { fail(e); }
        };
        row.append(resetBtn);
      }
      const sendBtn = make('button', 'Отправить на колонку', 'secondary'); sendBtn.type = 'button';
      const cancel = make('button', 'Закрыть', 'secondary'); cancel.type = 'button';
      row.append(sendBtn, cancel);
      foot.append(row);
      form.append(foot);
      dlg.append(form);
      document.body.append(dlg);
      editorDlg = dlg;
      dlg.showModal();

      const identities = await api('identities');
      identities.sessions.filter(s => s.device_sn && s.user_id).forEach(s => {
        const o = make('option', s.device_sn); o.value = s.device_sn; sessions.append(o);
      });

      const cm = makeCM(codeBox, { value: JSON.stringify(entry.body, null, 1), height: '100%' });
      const readBody = () => {
        try { return JSON.parse(cm.getValue()); }
        catch (e) { throw Error('JSON: ' + e.message); }
      };
      const values = () => {
        const result = {};
        for (const [name, input] of Object.entries(inputs)) if (input.value.trim()) result[name] = input.value.trim();
        return result;
      };

      // живой предпросмотр: правка кода или значений перерисовывает карточку
      let timer = null;
      function schedule() {
        clearTimeout(timer);
        timer = setTimeout(render, 400);
      }
      function render() {
        previewErr.hidden = true;
        const body = readBody();
        if (!body.Div2Card) {  // draw_animation_directive: Div-карточки нет, анимацию рисует колонка
          frameWrap.hidden = true;
          frame.srcdoc = '';
          previewErr.textContent = 'У ' + (body.Name || 'этой директивы') + ' нет Div-карточки: '
            + 'анимацию рисует колонка по пути S3Directory.';
          previewErr.hidden = false;
          return;
        }
        if (/webview/i.test(String(body.Div2Card.CardName || ''))) {
          // webview-карта (ютуб): содержимое рисует нативный webview колонки,
          // в sandbox-iframe браузера DivKit её не показывает
          frameWrap.hidden = true;
          frame.srcdoc = '';
          previewErr.textContent = 'Webview-карта («' + String(body.Div2Card.CardName)
            + '») в предпросмотре не рисуется: содержимое — webview колонки.';
          previewErr.hidden = false;
          return;
        }
        frameWrap.hidden = false;
        divkitSourceReady.then(() => {
          try {
            cardH = 0;
            clearTimeout(cardTimer);
            frame.srcdoc = previewDoc(previewCard(body, values()));
            // чёрная панель без ответа — ошибка, которую надо видеть: если iframe
            // не сообщил высоту за 2.5 с, показываем таймаут вместо тишины
            cardTimer = setTimeout(() => {
              if (!cardH) {
                previewErr.textContent = 'Предпросмотр: DivKit не ответил — подробности в консоли браузера.';
                previewErr.hidden = false;
              }
            }, 2500);
          } catch (e) {
            previewErr.textContent = e.message;
            previewErr.hidden = false;
          }
        }, e => {
          previewErr.textContent = e.message;
          previewErr.hidden = false;
        });
      }
      cm.on('change', schedule);

      // канвас карточки — экран станции шириной 720; высоту канвы подсказывает сам
      // iframe (postMessage с фактической высотой карточки) — карточка всегда видна
      // целиком, масштаб по меньшей стороне, без прокрутки и обрезки. Первый рендер —
      // после раскладки диалога: DivKit измеряет target при старте, рендер в
      // неразложенный диалог даёт схлопнутый контейнер (1px)
      let cardH = 0, cardTimer = 0;
      const onCardSize = e => {
        if (e.source !== frame.contentWindow || !e.data || e.data.type !== 'tpl-card-size') return;
        cardH = Math.max(360, Math.min(1280, Math.round(e.data.height) || 0));
        clearTimeout(cardTimer);
        fit();
      };
      window.addEventListener('message', onCardSize);
      const dismiss = () => {
        clearTimeout(cardTimer);
        window.removeEventListener('message', onCardSize); dismissEditor();
      };
      close.onclick = dismiss;
      cancel.onclick = dismiss;
      let laidOut = false;
      const fit = () => {
        const r = frameWrap.getBoundingClientRect();
        if (!r.width || !r.height) return;
        const h = cardH || 1280;
        frame.style.height = h + 'px';
        const scale = Math.min(r.width / 720, r.height / h);
        frame.style.transform = 'scale(' + scale + ')';
        frame.style.left = Math.max(0, Math.round((r.width - 720 * scale) / 2)) + 'px';
        frame.style.top = Math.max(0, Math.round((r.height - h * scale) / 2)) + 'px';
        if (!laidOut) {
          laidOut = true;
          render();
        }
      };
      new ResizeObserver(fit).observe(frameWrap);
      fit();

      const save = async () => {
        err.hidden = true;
        try {
          const body = readBody();
          await api('templates', 'PUT', { id: entry.id, body, name: body.Name, title: entry.title || '' });
          notify('Сохранено: ' + entry.id);
          dismissEditor(); await load();
        } catch (e) { fail(e); }
      };
      form.onsubmit = e => { e.preventDefault(); save(); };
      sendBtn.onclick = async () => {
        err.hidden = true;
        if (!sessions.value) { fail(Error('Нет активных колонок: привяжите станцию к эмулятору')); return; }
        try {
          await api('templates', 'POST', { op: 'send', id: entry.id, sn: sessions.value, values: values() });
          notify('Отправлено на ' + sessions.value);
        } catch (e) { fail(e); }
      };
    } finally {
      opening = false;
    }
  }

  // DivKit-карточка из тела шаблона: StringBody (строка или объект) → {templates, card}.
  // Глобальные шаблоны (GlobalTemplates) — {"имя": {"StringBody": "{\"имя\": <определение>"}}:
  // разбирать StringBody и сливать определения. Подмешивать обёртки как есть нельзя —
  // DivKit не находит определение по type=<имя шаблона> и рисует пустую карточку.
  // {{name}} — значениями из формы; пустое значение — имя переменной
  // (превью всегда видно), {{...имя шаблона...}} — первое имя из шаблонов,
  // чтобы ссылка на шаблон иконки разрешалась. <uuid> — пробный uuid
  function previewCard(body, values) {
    const card = body.Div2Card || {};
    let inner = card.StringBody;
    if (typeof inner === 'string') inner = JSON.parse(inner);
    inner = JSON.parse(JSON.stringify(inner));
    const templates = {...(inner.templates || {})};
    const globals = (card.GlobalTemplates && typeof card.GlobalTemplates === 'object')
      ? card.GlobalTemplates : {};
    for (const entry of Object.values(globals)) {
      if (!entry || typeof entry.StringBody !== 'string') continue;
      try {
        Object.assign(templates, JSON.parse(entry.StringBody));
      } catch (e) { /* битый глобальный шаблон: остальные продолжают работать */ }
    }
    inner.templates = templates;
    const templateNames = Object.keys(templates);
    const filled = JSON.stringify(inner)
      .replace(/\{\{([a-z0-9_]+)\}\}/g, (m, name) => {
        if (values && values[name] !== undefined) return values[name];
        return /template|icon/i.test(name) && templateNames.length ? templateNames[0] : name;
      })
      .replace(/<uuid>/g, '00000000-0000-4000-8000-000000000000');
    const out = JSON.parse(filled);
    // Плейсхолдер переменной не годится адресом картинки: браузер ходит за ним
    // и получает 404 в консоли. Картинки без настоящего адреса — прозрачный пиксель.
    const stub = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
    const visit = node => {
      if (Array.isArray(node)) { node.forEach(visit); return; }
      if (!node || typeof node !== 'object') return;
      for (const [key, val] of Object.entries(node)) {
        if ((key === 'image_url' || key === 'image_url_lowres') && typeof val === 'string'
            && !/^(https?:|data:|blob:)/i.test(val)) node[key] = stub;
        else visit(val);
      }
    };
    visit(out.card);
    return out;
  }

  // Бандл DivKit для предпросмотра. Iframe с непрозрачным origin (sandbox без
  // allow-same-origin) в части движков не загружает внешние скрипты вовсе —
  // ни по относительному, ни по абсолютному адресу. Поэтому iframe не ходит в сеть:
  // родитель получает текст бандла один раз (fetch, кэш в модуле) и встраивает
  // инлайн в srcdoc. '</script' в тексте бандла экранируется: в строках, шаблонах
  // и regex '<\/script' равно '</script', парсер HTML внешний <script> не обрывает.
  let divkitSource = null;
  const divkitSourceReady = fetch('vendor/divkit.min.js?v=33')
    .then(r => { if (!r.ok) throw Error('DivKit: HTTP ' + r.status); return r.text(); })
    .then(t => { divkitSource = t.replace(/<\/script/gi, '<\\/script'); });

  function previewDoc(card) {
    if (divkitSource == null) throw Error('Бандл DivKit ещё загружается');
    // скрипты — в конце body: на момент их исполнения document.body существует
    // (в head ещё нет — target получал undefined, рендер падал).
    // overflow:hidden — прокрутка внутри iframe не нужна: высоту карточки панель
    // узнаёт через postMessage и масштабирует канву сама
    return '<!doctype html><html><head><meta charset="utf-8"><style>'
      + 'html,body{margin:0;height:100%;background:#0b0f14;color:#e6eef9;'
      + 'font-family:Inter,system-ui,sans-serif;overflow:hidden}'
      + '</style></head><body>'
      + '<script>' + divkitSource + '<\/script>'
      + '<script>try{window.Ya.DivKit.render({id:"preview",target:document.body,json:'
      + JSON.stringify(card).replace(/</g, '\\u003c') + '});'
      + 'var __tell=function(){var el=document.body.lastElementChild;'
      + 'parent.postMessage({type:"tpl-card-size",'
      + 'height:el&&el.getBoundingClientRect?el.getBoundingClientRect().height:0},"*");};'
      + 'requestAnimationFrame(__tell);setTimeout(__tell,700);'
      + '}catch(e){document.body.textContent="DivKit: "+e.message;}<\/script>'
      + '</body></html>';
  }

  // ── редактор кода: CodeMirror (vendor/), запас — textarea ───────────────
  function makeCM(container, { value = '', height = '420px' } = {}) {
    if (typeof CodeMirror === 'undefined') {
      const ta = make('textarea', null, 'code'); ta.value = value; ta.rows = 14; ta.spellcheck = false;
      container.append(ta);
      return { getValue: () => ta.value, refresh: () => {}, on: (evt, fn) => { if (evt === 'change') ta.addEventListener('input', fn); } };
    }
    const cm = CodeMirror(container, { value, mode: 'application/json', theme: 'material-darker',
      lineNumbers: true, foldGutter: true, indentUnit: 2, tabSize: 2, matchBrackets: true,
      gutters: ['CodeMirror-linenumbers', 'CodeMirror-foldgutter'] });
    cm.setSize('100%', height);
    setTimeout(() => cm.refresh(), 0);
    return { getValue: () => cm.getValue(), refresh: () => cm.refresh(), on: (evt, fn) => cm.on(evt, fn) };
  }

  window.addEventListener('hashchange', load);
  load();
})();
