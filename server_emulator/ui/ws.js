// «Эмулятор → WS»: протокольный слой колонки (TASK-058). Сверху — свёрнутые блоки промахов
// (AAPI без ответа, колбэки без сценария), ниже — «Входящие» (реестр обработчиков), «Колбэки»
// (маршрут → поставщик данных) и «Отправка» (ручные push-директивы). Списки — компактные таблицы,
// на узком экране строки складываются в блоки. API — ui/api/ws_handlers.py (реестр),
// ui/api/misses.py (промахи), ui/api/actions.py (отправка).
(() => {
  const section = document.getElementById('ws');
  if (!section) return;
  const make = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const $ = id => document.getElementById(id);
  const api = async (action, value) => {
    const r = await fetch('api/' + action, { method: value === undefined ? 'GET' : 'POST',
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
  // Время: сегодня — только часы, иначе дата и время
  const when = ts => {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toDateString() === new Date().toDateString() ? d.toLocaleTimeString('ru-RU') : d.toLocaleString('ru-RU');
  };

  function head(title) {
    const bar = make('div', null, 'sc-toolbar ws-head');
    const refresh = make('button', 'Обновить', 'secondary'); refresh.type = 'button';
    refresh.onclick = () => work(load);
    bar.append(make('h2', title), refresh);
    return bar;
  }

  function table(columns, rows) {
    const t = make('table', null, 'ws-table');
    const tr = make('tr');
    for (const c of columns) tr.append(make('th', c));
    const thead = make('thead'); thead.append(tr);
    const tbody = make('tbody'); tbody.append(...rows);
    t.append(thead, tbody);
    return t;
  }
  function cell(label, ...children) {
    const td = make('td'); td.dataset.label = label;
    td.append(...children.filter(Boolean));
    return td;
  }
  const desc = text => text ? make('span', text, 'ws-desc') : null;
  const counts = c => { const s = make('span', `${c.handled} / ${c.seen}`, 'ws-num'); s.title = 'обработано / пришло'; return s; };
  const last = c => make('span', when(c.last), 'ws-num ws-muted');

  function panel(title, body, note) {
    const box = make('div', null, 'panel ws-panel');
    const h = make('h3', title);
    if (note != null) h.append(make('span', note, 'count'));
    box.append(h, body);
    return box;
  }

  // «Входящие»: строка на обработчик; у события с несколькими обработчиками имя не повторяется
  function handlers(list) {
    if (!list.length) return make('p', 'Реестр пуст.', 'hint');
    let previous = null;
    const rows = list.map(h => {
      const tr = make('tr');
      if (!h.enabled) tr.className = 'ws-off';
      const trigger = make('b', h.trigger === previous ? '' : h.trigger);
      previous = h.trigger;
      const name = make('div', null, 'ws-name');
      name.append(make('code', h.name));
      if (h.source === 'callback') name.append(make('span', 'колбэк', 'chip'));
      else if (h.source.startsWith('extension:')) name.append(make('span', h.source.slice('extension:'.length), 'chip'));
      if (!h.enabled) name.append(make('span', 'выключен', 'chip warn'));
      tr.append(cell('Событие', trigger),
        cell('Обработчик', name, desc([h.description, h.enabled_text].filter(Boolean).join(' · '))),
        cell('Обработано', counts(h.counters)), cell('Последний', last(h.counters)));
      return tr;
    });
    return table(['Событие', 'Обработчик', 'Обработано', 'Последний'], rows);
  }

  // «Колбэки»: маршрут колбэка → внутреннее событие; кто отвечает — поставщик
  // (пользовательский сценарий перекрывает встроенный) или умолчание (тихое подтверждение)
  function callbacks(list) {
    if (!list.length) return make('p', 'Колбэков нет.', 'hint');
    const rows = list.map(c => {
      const tr = make('tr');
      const name = make('div', null, 'ws-name');
      name.append(make('b', c.event));
      const filters = c.filters ? make('code', c.filters, 'ws-filter') : null;
      const provider = make('div', null, 'ws-name');
      provider.append(make('code', c.provide), make('span', '→', 'ws-muted'));
      if (c.provider) {
        provider.append(make('span', c.provider.id));
        if (c.overridden) provider.append(make('span', 'свой', 'chip'));
      } else provider.append(make('span', 'тихое подтверждение', 'ws-muted'));
      tr.append(cell('Колбэк', name, filters, desc(c.description)), cell('Поставщик', provider),
        cell('Обработано', counts(c.counters)), cell('Последний', last(c.counters)));
      return tr;
    });
    return table(['Колбэк', 'Событие → поставщик', 'Обработано', 'Последний'], rows);
  }

  // «Отправка»: ручные push-директивы выбранному устройству (переехали из «Устройства → Действия»)
  async function send(root) {
    root.replaceChildren();
    const inventory = await api('actions');
    const registry = await api('identities');
    const form = make('form', null, 'ws-send');
    const sessions = make('select');
    const sl = make('label', 'Сессия устройства'); sl.append(sessions); form.append(sl);
    for (const s of registry.sessions.filter(s => s.device_sn && s.user_id)) {
      const o = make('option', `${s.device_sn} · ${s.connection_id.slice(0, 8)}`);
      o.value = s.connection_id; o.dataset.sn = s.device_sn; sessions.append(o);
    }
    const action = make('select');
    const al = make('label', 'Команда'); al.append(action); form.append(al);
    for (const a of inventory.catalog) {
      const o = make('option', a.name + ' · ' + a.id); o.value = a.id; action.append(o);
    }
    const level = make('input'); level.type = 'number'; level.min = 0; level.max = 10; level.step = 0.1; level.value = 5;
    const label = make('label', 'Громкость (0–10)'); label.append(level); form.append(label);
    action.onchange = () => label.hidden = action.value !== 'sound_set_level'; action.onchange();
    const sendBtn = make('button', 'Отправить'); sendBtn.type = 'submit';
    sendBtn.disabled = !sessions.options.length;
    form.append(sendBtn);
    form.onsubmit = e => {
      e.preventDefault();
      work(async () => {
        sendBtn.disabled = true;
        try {
          await api('actions', { connection_id: sessions.value, device_sn: sessions.selectedOptions[0].dataset.sn,
            action: action.value, arguments: action.value === 'sound_set_level' ? { level: Number(level.value) } : {} });
          notify('Команда отправлена. Выполнение ещё не подтверждено.');
          await load();
        } finally { sendBtn.disabled = !sessions.options.length; }
      });
    };
    root.append(form);
    if (!sessions.options.length) root.append(make('p', 'Нет активных привязанных WS-соединений эмулятора: в маршруте «Прозрачно» команды через эмулятор не отправляются.', 'hint'));
    const history = inventory.history.slice(-15).reverse();
    if (history.length) {
      const rows = history.map(h => {
        const tr = make('tr');
        tr.append(cell('Устройство', make('span', h.device_sn)), cell('Команда', make('code', h.action)),
          cell('Доставка', make('span', h.delivery)), cell('Выполнение', make('span', h.execution)));
        return tr;
      });
      root.append(table(['Устройство', 'Команда', 'Доставка', 'Выполнение'], rows));
    }
  }

  async function load() {
    const ws = await api('ws_handlers');
    const root = $('ws');
    if (!root || location.hash !== '#ws') return;
    const missesMount = make('div', null, 'ws-misses');
    const target = make('div');
    root.replaceChildren(head('WS'),
      make('p', 'Всё, что приходит и уходит по протоколу колонки: обработчики входящих сообщений, служебные колбэки (ответ собирает сценарий-поставщик, его можно переопределить своим) и ручная отправка директив. «Обработано» — обработано / пришло с запуска сервера.', 'hint'),
      missesMount,
      panel('Входящие', handlers(ws.handlers), String(ws.handlers.length)),
      panel('Колбэки', callbacks(ws.callbacks), String(ws.callbacks.length)),
      panel('Отправка', target));
    await window.renderMisses(missesMount, ['aapi', 'callback']);
    await send(target);
  }

  const sync = () => { if (location.hash === '#ws') work(load); };
  window.addEventListener('hashchange', sync);
  sync();
})();
