// «Эмулятор → WS»: протокольный слой колонки (TASK-058). Сверху — свёрнутый блок «AAPI без
// ответа», ниже — «Входящие» (реестр обработчиков), «Колбэки» (маршрут → поставщик данных)
// и «Отправка» (ручные push-директивы, переехали из «Устройства → Действия»). API —
// ui/api/ws_handlers.py (реестр), ui/api/misses.py (aapi), ui/api/actions.py (отправка).
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
  const when = ts => ts ? new Date(ts * 1000).toLocaleString('ru-RU') : '—';

  function head(title) {
    const bar = make('div', null, 'sc-toolbar');
    bar.append(make('h2', title), make('button', 'Обновить', 'secondary'));
    bar.lastChild.type = 'button';
    bar.lastChild.onclick = () => work(load);
    return bar;
  }

  // Свёрнутый блок «AAPI без ответа»: те же записи, что были во вкладке «Без ответа»
  // (обработчика из UI не создать — нужен код; образец запроса — в подсказке строки)
  function aapiMisses(data) {
    const box = make('details');
    const summary = make('summary');
    const count = (data.aapi || []).length;
    summary.append(make('b', 'AAPI без ответа'), make('span', count ? ' · ' + count : '', 'count'));
    if (count) summary.lastChild.className = 'chip warn';
    box.append(summary);
    const body = make('div', null, 'sc-list');
    if (!count) body.append(make('p', 'Все сообщения колонок обработаны.', 'hint'));
    for (const m of data.aapi || []) {
      const row = make('div', null, 'misses-row');
      const name = make('span', m.key, 'ep-url');
      name.title = m.examples[0] || '';
      row.append(name, make('span', '×' + m.count, 'count'));
      body.append(row);
    }
    box.append(body);
    return box;
  }

  const counters = c => `пришло ${c.seen} · обработано ${c.handled} · последний ${when(c.last)}`;

  function handlers(list) {
    const box = make('div', null, 'list');
    if (!list.length) box.append(make('p', 'Реестр пуст.', 'hint'));
    for (const h of list) {
      const row = make('div', null, 'panel');
      const line = make('div', null, 'name-line');
      line.append(make('b', h.trigger), make('span', h.name, 'chip'));
      if (h.source === 'callback') line.append(make('span', 'колбэк', 'chip'));
      else if (h.source.startsWith('extension:')) line.append(make('span', h.source, 'chip'));
      if (!h.enabled) line.append(make('span', 'выключен', 'chip warn'));
      row.append(line, make('small', (h.description || '') + (h.enabled_text ? ' · ' + h.enabled_text : ''), 'hint'),
                 make('small', counters(h.counters), 'hint'));
      box.append(row);
    }
    return box;
  }

  // «Колбэки»: маршрут колбэка → внутреннее событие; кто отвечает — поставщик
  // (пользовательский сценарий перекрывает встроенный) или умолчание (тихое подтверждение)
  function callbacks(list) {
    const box = make('div', null, 'list');
    for (const c of list) {
      const row = make('div', null, 'panel');
      const line = make('div', null, 'name-line');
      line.append(make('b', c.event));
      if (c.filters) line.append(make('span', c.filters, 'chip'));
      row.append(line, make('small', '→ ' + c.provide + (c.description ? ' · ' + c.description : ''), 'hint'));
      const provider = c.provider
        ? make('small', (c.overridden ? 'поставщик (переопределён): ' : 'поставщик: ') + c.provider.id, 'hint')
        : make('small', 'поставщика нет — умолчание: тихое подтверждение', 'hint');
      row.append(provider, make('small', counters(c.counters), 'hint'));
      box.append(row);
    }
    return box;
  }

  // «Отправка»: ручные push-директивы выбранному устройству (переехали из «Устройства → Действия»)
  async function send(root) {
    root.replaceChildren();
    const inventory = await api('actions');
    const registry = await api('identities');
    const form = make('form');
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
    const sendBtn = make('button', 'Отправить на выбранное устройство'); sendBtn.type = 'submit';
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
    if (!sessions.options.length) root.append(make('p', 'Нет активных привязанных WS-соединений эмулятора. В режиме проксирования команды через этот адаптер не отправляются.', 'hint'));
    const refresh = make('button', 'Обновить сессии и доставку', 'secondary'); refresh.type = 'button';
    refresh.onclick = () => work(load);
    root.append(refresh);
    for (const h of inventory.history.slice(-15).reverse()) {
      root.append(make('p', `${h.device_sn} · ${h.action} · доставка: ${h.delivery} · выполнение: ${h.execution}`, 'hint'));
    }
  }

  async function load() {
    const [ws, misses] = await Promise.all([api('ws_handlers'), api('misses')]);
    const root = $('ws');
    if (!root || location.hash !== '#ws') return;
    root.replaceChildren(head('WS'),
      make('p', 'Всё, что приходит и уходит по протоколу колонки: обработчики входящих сообщений, служебные колбэки (ответ собирает сценарий-поставщик, его можно переопределить своим) и ручная отправка директив. Колбэки без обработчика видны в свёрнутом блоке сверху.', 'hint'),
      aapiMisses(misses),
      make('h3', 'Входящие'), handlers(ws.handlers),
      make('h3', 'Колбэки'), callbacks(ws.callbacks),
      make('h3', 'Отправка'));
    const target = make('div');
    root.append(target);
    await send(target);
  }

  const sync = () => { if (location.hash === '#ws') work(load); };
  window.addEventListener('hashchange', sync);
  sync();
})();
