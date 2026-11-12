// Вкладки «Поставщики данных»: одна вкладка-шаблон на любой вид из api/providers
// (вид объявляет плагин в contributes.providers) + вкладка «Порядок» — ручной
// порядок справочника фраз (равная точность — выше в списке): drag-and-drop
// плагинов внутри вида и видов между собой; сохранение точечное — PUT
// api/providers {order}. Секции #providers-<kind> создаются здесь — нет вида,
// нет вкладки; группа в меню строится app.js из того же ответа (нет поставщиков —
// группы нет). Радиосписок «Авто — первый включённый» + поставщики (встроенные —
// с пометкой; Яндекс Музыка — статус захвата токена аккаунта, без вывода токена).
// Настройки выбранного — готовая форма «Расширений» (window.extensionSettingsBlock),
// не вторая копия. Выбор — PUT api/providers {kind, id} с заголовком X-Emulator.
(() => {
  const make = (tag, cls, text) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  let busy = false, selfDispatch = false, order = { kinds: [], plugins: {} };

  const say = (text, error = false) => { notify(text, { error }); };
  const kindOf = () => { const m = location.hash.match(/^#providers-([a-z0-9_-]+)$/); return m ? m[1] : null; };

  // drag-and-drop списка: перетаскивание строк, после броска — сохранение нового порядка
  const draggable = (list, rows, save) => {
    let drag = null;
    list.ondragstart = e => { drag = e.target.closest('[data-key]'); e.dataTransfer.effectAllowed = 'move'; };
    list.ondragover = e => {
      e.preventDefault();
      const over = e.target.closest('[data-key]');
      if (!over || over === drag) return;
      const box = over.getBoundingClientRect();
      list.insertBefore(drag, e.clientY < box.top + box.height / 2 ? over : over.nextSibling);
    };
    list.ondrop = e => { e.preventDefault(); if (drag) save([...list.querySelectorAll('[data-key]')].map(n => n.dataset.key)); };
    list.ondragend = () => { drag = null; };
    for (const row of rows) { row.draggable = true; row.dataset.key = row.dataset.key || ''; }
  };

  const saveOrder = async patch => {
    if (busy) return;
    busy = true;
    try {
      order = { kinds: order.kinds || [], plugins: order.plugins || {}, ...patch };
      const r = await fetch('api/providers', { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Emulator': '1' },
                                               body: JSON.stringify({ order }) });
      if (!r.ok) { const v = await r.json().catch(() => ({})); throw Error(v.error || r.statusText); }
      say('Порядок сохранён');
    } catch (e) { say(e.message, true); }
    finally { busy = false; }
  };


  const ensureSection = kind => {
    let s = document.getElementById('providers-' + kind);
    if (s) return s;
    s = make('section'); s.id = 'providers-' + kind; s.className = 'tab'; s.hidden = true;
    document.querySelector('main').append(s);
    selfDispatch = true;  // перерисовать меню/вкладки под новую секцию, свой слушатель пропустит
    try { window.dispatchEvent(new HashChangeEvent('hashchange')); } finally { selfDispatch = false; }
    return s;
  };

  async function load() {
    const kind = kindOf();
    if (!kind) return;
    const section = ensureSection(kind);
    let data;
    try { data = await (await fetch('api/providers')).json(); } catch (e) {
      section.replaceChildren(make('p', 'hint', 'Не удалось загрузить: ' + e.message));
      return;
    }
    order = data.phrase_order || { kinds: [], plugins: {} };
    if (kind === 'order') return renderOrder(section, data);
    const info = (data.kinds || {})[kind];
    if (!info) {
      section.replaceChildren(make('h2', null, kind),
        make('p', 'hint', 'Вид недоступен: включите расширение с поставщиками этого вида (вкладка «Расширения»).'));
      return;
    }
    const box = make('div');  // одна колонка: выбор → порядок → настройки
    section.replaceChildren(make('h2', null, info.title || kind),
      make('p', 'hint', 'Откуда сценарии этого вида берут данные. «Авто» — первый включённый поставщик.'),
      box);
    if (!info.providers.length) {
      const p = make('p', 'hint', 'Поставщиков нет: установите или включите расширение с папкой вида во вкладке «Расширения». ');
      const a = document.createElement('a');
      a.href = '#extensions';
      a.textContent = 'Открыть «Расширения»';
      a.onclick = e => { e.preventDefault(); window.goTab('extensions'); };
      p.append(a);
      box.append(p);
      return;
    }
    const form = make('div');
    const radio = (id, label, checked) => {
      const l = make('label', 'check');
      const r = document.createElement('input');
      r.type = 'radio'; r.name = 'provider-' + kind; r.value = id; r.checked = checked;
      r.onchange = () => select(id);
      l.append(r, document.createTextNode(' ' + label));
      return l;
    };
    form.append(radio('', 'Авто — первый включённый', info.selected === ''));
    for (const p of info.providers) {
      let extra = p.origin === 'builtin' ? ' · встроенный' : '';
      if (p.origin !== 'builtin' && p.extension && p.id !== p.extension) extra += ' · ' + p.extension;
      if (p.status && typeof p.status.oauth_captured === 'boolean')
        extra += p.status.oauth_captured ? ' · токен аккаунта захвачен' : ' · токен аккаунта ещё не захвачен';
      form.append(radio(p.id, (p.title || p.id) + extra, info.selected === p.id));
    }
    box.append(form);
    // вход аккаунта: появляется только у поставщиков, объявивших точки login (старт/колбэк/статус)
    for (const p of info.providers) {
      if (!p.login) continue;
      const authorized = !!(p.status && p.status.login_authorized);
      const row = make('div', 'provider-login');
      const btn = make('button', authorized ? 'secondary' : null,
                       'Войти в ' + (p.title || p.id) + (authorized ? ' (перевойти)' : ''));
      btn.type = 'button';
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          const r = await fetch('api/providers_login', { method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Emulator': '1' },
            body: JSON.stringify({ kind, id: p.id, action: 'start' }) });
          const v = await r.json().catch(() => ({}));
          if (!r.ok || !v.url) throw Error(v.error || r.statusText);
          window.location.assign(v.url);  // страница входа Spotify → redirect обратно на колбэк
        } catch (e) { say('Вход не удался: ' + e.message, true); btn.disabled = false; }
      };
      row.append(btn, make('span', 'hint', authorized ? 'вход выполнен' : 'вход ещё не выполнен'));
      box.append(row);
    }
    if (info.providers.some(p => p.login)) {
      box.append(make('p', 'hint', 'Вход аккаунта: кнопка работает при открытии панели по http://127.0.0.1:<порт> — '
        + 'redirect регистрируется в Spotify Dashboard без порта; у владельца приложения Premium, '
        + 'один Client ID и до 5 пользователей (Development Mode).'));
    }
    // порядок ответов вида: перетаскивание плагинов (равная точность — выше в списке)
    const orderList = make('ul', 'order-list');
    const plugins = order.plugins && Array.isArray(order.plugins[kind]) ? order.plugins[kind] : [];
    const ordered = [...info.providers].sort((a, b) => {
      const ia = plugins.indexOf(a.extension), ib = plugins.indexOf(b.extension);
      return (ia === -1 ? plugins.length : ia) - (ib === -1 ? plugins.length : ib) || a.extension.localeCompare(b.extension);
    });
    for (const p of ordered) {
      const row = make('li', 'order-row', (p.title || p.id) + (p.origin === 'builtin' ? ' · встроенный' : ''));
      row.dataset.key = p.extension;
      orderList.append(row);
    }
    draggable(orderList, [...orderList.children], ids => {
      saveOrder({ plugins: { ...(order.plugins || {}), [kind]: ids } });
    });
    box.append(make('h3', null, 'Порядок ответов'), orderList);

    // настройки выбранного поставщика — форма его расширения
    const active = info.selected || (info.providers[0] || {}).id || '';
    const provider = info.providers.find(p => p.id === active);
    if (provider && provider.extension) {
      try {
        const ext = await (await fetch('api/extensions')).json();
        const card = (ext.extensions || []).find(x => x.id === provider.extension);
        const block = card && typeof window.extensionSettingsBlock === 'function' ? window.extensionSettingsBlock(card) : null;
        if (block) box.append(make('h3', null, 'Настройки: ' + (card.name || card.id)), block);
      } catch (e) { /* настройки — не часть выбора, сбой загрузки не критичен */ }
    }
  }

  function renderOrder(section, data) {
    const kinds = Object.entries(data.kinds || {}).map(([id, v]) => ({ id, title: v.title || id }));
    section.replaceChildren(make('h2', null, 'Порядок'),
      make('p', 'hint', 'Порядок видов для споров между ними и плагинов внутри вида. Равная точность — выше в списке.'));
    const list = make('ul', 'order-list');
    const titles = Object.fromEntries(kinds.map(k => [k.id, k.title]));
    const known = kinds.map(k => k.id);
    const listed = (order.kinds || []).filter(k => known.includes(k));
    const full = [...listed, ...known.filter(k => !listed.includes(k))];
    for (const id of full) {
      const row = make('li', 'order-row', titles[id] || id);
      row.dataset.key = id;
      list.append(row);
    }
    draggable(list, [...list.children], ids => saveOrder({ kinds: ids }));
    section.append(list);
  }

  async function select(id) {
    const kind = kindOf();
    if (!kind || busy) return;
    busy = true;
    try {
      const r = await fetch('api/providers', { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Emulator': '1' },
                                               body: JSON.stringify({ kind, id }) });
      if (!r.ok) { const v = await r.json().catch(() => ({})); throw Error(v.error || r.statusText); }
      say(id ? 'Поставщик: ' + id : 'Поставщик: авто');
      await load();
    } catch (e) { say(e.message, true); }
    finally { busy = false; }
  }

  window.addEventListener('hashchange', () => { if (!selfDispatch && kindOf()) load(); });
  if (kindOf()) load();
})();
