/* Разделы «Пользователи» и «Устройства»: реестр identities, онлайн-статусы, привязка. */
(() => {
  const accountsRoot = document.getElementById('accounts');
  const devicesRoot = document.getElementById('devices');
  if (!accountsRoot || !devicesRoot) return;
  let registry = null; // {users, devices, unassigned, sessions, activity}
  let fetchSeq = 0;
  let fingerprint = ''; // снимок структуры (users+devices): изменился — перерисовать таблицу

  const node = (tag, text) => { const n = document.createElement(tag); if (text != null) n.textContent = text; return n; };
  const userName = id => (registry.users.find(u => u.id === id) || {}).name || '…';

  // Модель/картинка — общий модуль вкладок (devices_common.js)
  const { parseSn, pictureChain, applyPicture, modelTitle } = window.DeviceKit;

  // Тихая загрузка: без work() — поллинг не должен блокировать кнопки и дёргать DOM
  async function fetchRegistry() {
    const seq = ++fetchSeq;
    const value = await api('identities');
    if (seq !== fetchSeq) return false;
    registry = { users: value.users || [], devices: value.devices || [], unassigned: value.unassigned || [], sessions: value.sessions || [], activity: value.activity || {} };
    return true;
  }
  const refresh = () => work(async () => { if (await fetchRegistry()) fingerprint = '', refreshDevices(); });

  // ── Раздел «Пользователи»: пока только создание и удаление ─────────
  function renderAccounts() {
    accountsRoot.replaceChildren(
      node('h2', 'Пользователи'),
      node('p', 'Пользователь — владелец устройств: его профиль наследуется промптом и инструментами. Пока здесь только создание и удаление; расширенные настройки появятся позже.'));
    const form = node('form');
    const label = node('label', 'Имя нового пользователя');
    const name = node('input'); name.required = true; name.maxLength = 256; name.placeholder = 'Например, Alice';
    label.append(name); form.append(label);
    const create = node('button', 'Создать пользователя'); create.type = 'submit';
    form.append(create);
    form.onsubmit = e => { e.preventDefault(); work(async () => { await api('identities', 'POST', { operation: 'user', name: name.value }); name.value = ''; await fetchRegistry(); renderAccounts(); message('Пользователь создан.'); }); };
    accountsRoot.append(form);

    const table = node('table'); table.className = 'dev-table';
    const head = node('tr');
    for (const title of ['Пользователь', 'Устройств', '']) head.append(node('th', title));
    const thead = node('thead'); thead.append(head); table.append(thead);
    const tbody = node('tbody');
    for (const user of registry.users) {
      const row = node('tr');
      const who = node('td');
      const chip = node('span', user.name); chip.className = 'chip';
      who.append(chip, node('small', 'id: ' + user.id));
      row.append(who);
      const owned = registry.devices.filter(d => d.user_id === user.id).length;
      row.append(node('td', String(owned)));
      const actions = node('td');
      const remove = node('button', 'Удалить'); remove.type = 'button'; remove.className = 'secondary danger';
      remove.onclick = () => work(async () => {
        if (!confirm(`Удалить пользователя «${user.name}»? Его устройства останутся, но станут непривязанными.`)) return;
        await api('identities', 'POST', { operation: 'user_delete', id: user.id });
        await fetchRegistry(); renderAccounts(); message('Пользователь удалён; его устройства отвязаны.');
      });
      const paramsBtn = node('button', 'Параметры'); paramsBtn.type = 'button'; paramsBtn.className = 'secondary';
      paramsBtn.onclick = () => work(() => paramsDialog(user));
      // Кабинет пользователя (/me) в новой вкладке; с включённым входом — по токену, без логина
      const cabinet = node('button', 'Кабинет'); cabinet.type = 'button'; cabinet.className = 'secondary';
      cabinet.onclick = () => work(async () => {
        const link = await api('me_link', 'POST', { user: user.id });
        window.open(link.url, '_blank', 'noopener');
      });
      actions.append(cabinet, paramsBtn, remove); row.append(actions);
      tbody.append(row);
    }
    if (!registry.users.length) {
      const empty = node('tr'); const cell = node('td', 'Пользователей пока нет — создайте первого выше.'); cell.colSpan = 3;
      empty.append(cell); tbody.append(empty);
    }
    table.append(tbody); accountsRoot.append(table);
  }

  // ── Параметры пользователя: вкладки (пользователь / колонки), сохранение кнопкой ─
  async function paramsDialog(user) {
    const [defs, userValues] = await Promise.all([
      api('params').then(v => v.defs),
      api('param_values?owner=user:' + encodeURIComponent(user.id)).then(v => v.values)]);
    const devices = registry.devices.filter(d => d.user_id === user.id);
    const deviceValues = await Promise.all(devices.map(d =>
      api('param_values?owner=device:' + encodeURIComponent(d.sn)).then(v => v.values)));
    const login = await api('user_login?user=' + encodeURIComponent(user.id)).catch(() => ({ enabled: false, name: null }));

    const dialog = node('dialog'); dialog.className = 'params-dialog';
    const head = node('header');
    const close = node('button', '✕'); close.type = 'button'; close.className = 'icon-btn';
    close.title = 'Закрыть'; close.setAttribute('aria-label', 'Закрыть');
    head.append(node('h3', 'Параметры · ' + (user.name || user.id)));
    const allParams = node('button', 'Все параметры'); allParams.type = 'button'; allParams.className = 'secondary';
    allParams.title = 'Открыть «Значения» с этим владельцем (TASK-060)';
    allParams.onclick = () => { dialog.close(); window.paramsOpenOwner?.('user', user.id); };
    head.append(allParams, close);
    const tabs = node('nav'); tabs.className = 'pd-tabs';
    const body = node('div'); body.className = 'pd-body';
    const errLine = node('p'); errLine.className = 'pd-error'; errLine.hidden = true;
    const foot = node('footer');
    const status = node('span'); status.className = 'hint';
    const cancel = node('button', 'Отмена'); cancel.type = 'button'; cancel.className = 'secondary';
    const save = node('button', 'Сохранить'); save.type = 'button';
    foot.append(status, cancel, save);
    dialog.append(head, tabs, body, errLine, foot);

    // Изменения копятся до «Сохранить»: id → {commit(), fields, tab}
    const dirty = new Map();
    const pages = [];
    const refreshState = () => {
      status.textContent = dirty.size ? 'Изменено полей: ' + dirty.size : 'Нет изменений';
      save.disabled = !dirty.size;
      for (const page of pages) page.tab.classList.toggle('dirty', [...dirty.values()].some(d => d.tab === page.tab));
    };
    const field = (def, owner, current, tab) => {
      const label = node('label'); label.className = 'pd-field' + (def.type === 'bool' ? ' bool' : '');
      label.title = (def.hint ? def.hint + ' · ' : '') + def.key;
      const caption = node('span', def.title + (def.scope === 'device' && !def.user_editable ? ' 🔒' : ''));
      const id = owner + '|' + def.key;
      const original = JSON.stringify(current ?? null);
      const input = paramInput(def, current, value => {
        if (value === undefined || JSON.stringify(value) === original) dirty.delete(id);
        else dirty.set(id, { commit: () => api('param_values', 'PUT', { owner, key: def.key, value }), fields: [label], tab });
        label.classList.toggle('dirty', dirty.has(id));
        refreshState();
      });
      if (def.type === 'bool') label.append(input, caption); else label.append(caption, input);
      return label;
    };
    const group = (title, list, owner, values, tab) => {
      const box = node('div'); box.className = 'pd-group';
      if (title) box.append(node('h4', title));
      const grid = node('div'); grid.className = 'pd-grid';
      for (const def of list) grid.append(field(def, owner, (values[def.key] || {}).value, tab));
      if (!list.length) { const empty = node('p', '—'); empty.className = 'hint'; grid.append(empty); }
      box.append(grid);
      return box;
    };
    // Вход в кабинет /me: логин и пароль user-принципала (пустой пароль — не менять)
    const loginGroup = tab => {
      const box = node('div'); box.className = 'pd-group';
      box.append(node('h4', 'Вход в кабинет /me'));
      const grid = node('div'); grid.className = 'pd-grid';
      const nameField = node('label'); nameField.className = 'pd-field';
      const name = node('input'); name.type = 'text'; name.value = login.name || ''; name.placeholder = user.name;
      name.autocomplete = 'off';
      nameField.append(node('span', 'Логин'), name);
      const passField = node('label'); passField.className = 'pd-field';
      const pass = node('input'); pass.type = 'password'; pass.autocomplete = 'new-password';
      pass.placeholder = login.name ? 'Не менять' : 'Не задан';
      passField.append(node('span', 'Пароль'), pass);
      grid.append(nameField, passField); box.append(grid);
      if (!login.enabled) {
        name.disabled = pass.disabled = true;
        const off = node('p', 'Вход отключён (открытый режим): сначала создайте администратора командой python -m server_emulator.auth.');
        off.className = 'hint'; box.append(off);
        return box;
      }
      const onInput = () => {
        const wanted = name.value.trim() || user.name;
        const changed = pass.value !== '' || (login.name && wanted !== login.name);
        if (changed) dirty.set('login', { tab, fields: [nameField, passField], commit: async () => {
          const result = await api('user_login', 'PUT', { user: user.id, name: wanted, password: pass.value });
          login.name = result.name; pass.value = '';
        } });
        else dirty.delete('login');
        nameField.classList.toggle('dirty', changed && wanted !== login.name);
        passField.classList.toggle('dirty', pass.value !== '');
        refreshState();
      };
      name.oninput = pass.oninput = onInput;
      return box;
    };
    const addPage = (title, build) => {
      const tab = node('button', title); tab.type = 'button';
      const page = node('div'); page.className = 'pd-page';
      page.append(...build(tab));
      tab.onclick = () => { for (const p of pages) { p.tab.classList.toggle('active', p.tab === tab); p.page.hidden = p.tab !== tab; } };
      tabs.append(tab); body.append(page); pages.push({ tab, page });
    };

    const owner = 'user:' + user.id;
    addPage('Пользователь', tab => {
      const intro = node('p', 'Основные доступны самому пользователю в его кабинете (/me); дополнительные — только админу.'); intro.className = 'hint';
      return [intro,
        group('Основные', defs.filter(d => d.scope === 'user' && d.user_editable), owner, userValues, tab),
        group('Дополнительные · только админ', defs.filter(d => d.scope === 'user' && !d.user_editable), owner, userValues, tab),
        loginGroup(tab)];
    });
    // Слой аккаунта Quasar-конфига: переопределения account_config для всех устройств пользователя
    let accountItems = JSON.parse(JSON.stringify(user.quasar_overrides || []));
    const accountOriginal = JSON.stringify(accountItems);
    addPage('Настройки аккаунта', tab => {
      const intro = node('p', 'Значения account_config (spotter, childContentAccess, jingle …) для всех устройств пользователя: общий конфиг → платформа → аккаунт → устройство; переопределение на самом устройстве сильнее. Пути — внутри account_config.');
      intro.className = 'hint';
      const box = node('div'); box.className = 'pd-group';
      const list = node('div'); list.className = 'endpoint-tree';
      const markDirty = () => {
        if (JSON.stringify(accountItems) === accountOriginal) dirty.delete('account');
        else dirty.set('account', { tab, fields: [box], commit: () => api('identities', 'POST', { operation: 'user_quasar_overrides', id: user.id, quasar_overrides: accountItems }) });
        box.classList.toggle('dirty', dirty.has('account'));
        refreshState();
      };
      const renderList = () => {
        list.replaceChildren();
        if (!accountItems.length) { const empty = node('p', 'Переопределений нет.'); empty.className = 'hint'; list.append(empty); }
        accountItems.forEach((item, i) => {
          const row = node('div'); row.className = 'tree-leaf';
          row.append(node('span', item.path.slice(ACCOUNT_PREFIX.length)));
          const summary = node('code', JSON.stringify(item.source).slice(0, 80)); summary.style.flex = '1'; row.append(summary);
          const edit = node('button', 'Править'); edit.type = 'button'; edit.className = 'secondary';
          edit.onclick = () => accountOverrideDialog(item, saved => { accountItems[i] = saved; renderList(); markDirty(); });
          const drop = node('button', '✕'); drop.type = 'button'; drop.className = 'icon-btn';
          drop.title = 'Убрать'; drop.setAttribute('aria-label', 'Убрать');
          drop.onclick = () => { accountItems = accountItems.filter((_, j) => j !== i); renderList(); markDirty(); };
          row.append(edit, drop); list.append(row);
        });
      };
      const add = node('button', '＋ добавить значение'); add.type = 'button';
      add.onclick = () => accountOverrideDialog(null, saved => { accountItems.push(saved); renderList(); markDirty(); });
      renderList();
      box.append(list, add);
      return [intro, box];
    });
    const deviceDefs = defs.filter(d => d.scope === 'device');
    devices.forEach((device, i) => addPage(device.name, tab => {
      const intro = node('p', 'SN ' + device.sn + ' · значения колонки перекрывают значения пользователя; 🔒 — только админ.'); intro.className = 'hint';
      return [intro, group('', deviceDefs, 'device:' + device.sn, deviceValues[i], tab)];
    }));
    if (pages.length < 2) tabs.hidden = true;
    pages[0].tab.click();
    refreshState();

    const dismiss = () => { dialog.close(); dialog.remove(); };
    const tryClose = () => { if (!dirty.size || confirm('Закрыть без сохранения? Изменения пропадут.')) dismiss(); };
    close.onclick = tryClose; cancel.onclick = tryClose;
    dialog.addEventListener('cancel', e => { e.preventDefault(); tryClose(); });
    save.onclick = async () => {
      errLine.hidden = true; save.disabled = cancel.disabled = true;
      let saved = 0;
      try {
        for (const [id, change] of [...dirty]) {
          await change.commit();
          change.fields.forEach(f => f.classList.remove('dirty')); dirty.delete(id); saved++;
        }
        dismiss(); message('Параметры сохранены: ' + saved);
      } catch (err) {
        errLine.textContent = 'Ошибка: ' + err.message + (saved ? ' (сохранено до ошибки: ' + saved + ')' : '');
        errLine.hidden = false; cancel.disabled = false; refreshState();
      }
    };
    document.body.append(dialog);
    dialog.showModal();
  }
  // Диалог одного переопределения аккаунта: путь внутри account_config + источник значения.
  // Формат как у устройства: {path, source}; путь хранится полным (config.account_config.<путь>).
  const ACCOUNT_PREFIX = 'config.account_config.';
  function accountOverrideDialog(item, onSave) {
    const { dialog, form } = dialogFrame(item ? 'Значение аккаунта' : 'Новое значение аккаунта');
    const pathLabel = node('label', 'Путь внутри account_config');
    const path = node('input'); path.required = true; path.placeholder = 'spotter';
    path.value = item ? item.path.slice(ACCOUNT_PREFIX.length) : '';
    pathLabel.append(path);
    const typeLabel = node('label', 'Источник');
    const type = node('select');
    for (const [value, title] of Object.entries({ literal: 'своё значение (JSON)', expr: 'шаблон {{токены}}',
      db: 'из БД эмулятора (user./device.)', run: 'запуск скрипта (белый список)',
      select: 'переключить селектор (вариант $select)', delete: 'удалить ключ' })) type.append(new Option(title, value));
    typeLabel.append(type);
    const valueLabel = node('label', 'Значение');
    const text = node('input'); const json = node('textarea'); json.rows = 4;
    valueLabel.append(text, json);
    const source = item && item.source;
    const fields = source && typeof source === 'object' && !Array.isArray(source) ? source : {};
    if ('delete' in fields) type.value = 'delete';
    else if ('$select' in fields) { type.value = 'select'; text.value = fields.$select; }
    else if ('expr' in fields) { type.value = 'expr'; text.value = fields.expr; }
    else if ('db' in fields) { type.value = 'db'; text.value = fields.db; }
    else if ('run' in fields) { type.value = 'run'; text.value = fields.run; }
    else { type.value = 'literal'; json.value = item ? JSON.stringify(item.source, null, 2) : '"alisa"'; }
    const syncView = () => { json.hidden = type.value !== 'literal'; text.hidden = type.value === 'literal' || type.value === 'delete'; };
    type.onchange = syncView; syncView();
    form.append(pathLabel, typeLabel, valueLabel);
    const cancel = node('button', 'Отмена'); cancel.type = 'button'; cancel.className = 'secondary';
    cancel.onclick = () => { dialog.close(); dialog.remove(); };
    const save = node('button', 'Готово'); save.type = 'submit';
    const actions = node('div'); actions.className = 'row'; actions.append(cancel, save); form.append(actions);
    const errLine = errorLine(form);
    form.onsubmit = e => {
      e.preventDefault(); errLine.hidden = true;
      try {
        const relative = path.value.trim().replace(/^\.+|\.+$/g, '').replace(/^config\.account_config\./, '');
        if (!relative) throw Error('Укажите путь');
        let value;
        if (type.value === 'delete') value = { delete: true };
        else if (type.value === 'literal') { try { value = JSON.parse(json.value || 'null'); } catch { throw Error('Значение: корректный JSON'); } }
        else if (!text.value.trim()) throw Error('Укажите значение');
        else value = { [type.value === 'select' ? '$select' : type.value]: text.value.trim() };
        onSave({ path: ACCOUNT_PREFIX + relative, source: value });
        dialog.close(); dialog.remove();
      } catch (err) { errLine.hidden = false; errLine.textContent = err.message; }
    };
    openDialog(dialog);
  }

  // Поле значения; onChange(value) — value === undefined, если сохранять нечего (пустое число/выбор)
  function paramInput(def, current, onChange) {
    let input;
    if (def.type === 'bool') {
      input = node('input'); input.type = 'checkbox'; input.checked = !!current;
      input.onchange = () => onChange(input.checked);
    } else if (def.type === 'select' || /(^|\.)timezone$/.test(def.key)) {
      input = node('select');
      if (def.type === 'select') {
        if (current == null) input.append(new Option('— не задано —', ''));
        for (const o of def.options || []) input.append(new Option(String(o), String(o)));
      } else timeZoneOptions(input, current);
      input.value = current ?? '';
      input.onchange = () => onChange(input.value === '' && def.type === 'select' ? undefined : input.value);
    } else if (def.type === 'text-list') {
      input = node('input'); input.type = 'text'; input.value = Array.isArray(current) ? current.join(', ') : '';
      input.oninput = () => onChange(input.value.split(',').map(x => x.trim()).filter(Boolean));
    } else if (def.type === 'number') {
      input = node('input'); input.type = 'number'; input.step = 'any'; input.value = current ?? '';
      input.oninput = () => onChange(input.value === '' ? undefined : Number(input.value));
    } else {
      input = node('input'); input.type = 'text'; input.value = current ?? '';
      input.oninput = () => onChange(input.value);
    }
    return input;
  }

  // Часовые пояса: российские сверху, затем все IANA-пояса браузера; в подписи — смещение UTC
  const RU_ZONES = [['Europe/Kaliningrad', 'Калининград'], ['Europe/Moscow', 'Москва'], ['Europe/Samara', 'Самара'],
    ['Asia/Yekaterinburg', 'Екатеринбург'], ['Asia/Omsk', 'Омск'], ['Asia/Novosibirsk', 'Новосибирск'],
    ['Asia/Krasnoyarsk', 'Красноярск'], ['Asia/Irkutsk', 'Иркутск'], ['Asia/Yakutsk', 'Якутск'],
    ['Asia/Vladivostok', 'Владивосток'], ['Asia/Magadan', 'Магадан'], ['Asia/Kamchatka', 'Камчатка']];
  const utcOffset = zone => {
    try {
      const part = new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'shortOffset' })
        .formatToParts(new Date()).find(p => p.type === 'timeZoneName');
      return part ? part.value.replace('GMT', 'UTC').replace(/^UTC$/, 'UTC±0') : '';
    } catch { return ''; }
  };
  function timeZoneOptions(select, current) {
    select.append(new Option('— не задан (наследуется) —', ''));
    const ru = node('optgroup'); ru.label = 'Россия';
    for (const [zone, city] of RU_ZONES) ru.append(new Option(`${city} · ${utcOffset(zone)} · ${zone}`, zone));
    const all = node('optgroup'); all.label = 'Все пояса';
    const zones = Intl.supportedValuesOf ? Intl.supportedValuesOf('timeZone') : RU_ZONES.map(z => z[0]);
    if (current && !zones.includes(current) && !RU_ZONES.some(z => z[0] === current)) zones.unshift(current);
    for (const zone of zones) all.append(new Option(`${zone} · ${utcOffset(zone)}`, zone));
    select.append(ru, all);
  }

  // ── Раздел «Устройства»: таблица со статусами и привязкой ──────────
  // Список устройств пополняется только запросом /get_sync_info от колонки
  // (временно, до активации); ручное добавление — кнопкой «Добавить устройство».
  const sessionsOf = device => registry.sessions.filter(s => s.device_sn === device.sn || (s.device_id && device.device_ids.includes(s.device_id)));

  // IP колонки: у активной сессии — адрес соединения, иначе последний виденный
  const ipOf = device => {
    const live = sessionsOf(device);
    return (live[0] && live[0].ip) || (registry.activity[device.sn] || {}).ip || '';
  };

  function statusOf(device) {
    const live = sessionsOf(device);
    const act = registry.activity[device.sn];
    if (live.length) return { cls: 'on', text: 'Онлайн · WS-сессия' + (live[0].ip ? ' · ' + live[0].ip : '') };
    if (act && Date.now() / 1000 - act.ts < 600) return { cls: 'seen', text: 'Виден недавно (HTTP' + (act.ip ? ' · ' + act.ip : '') + ')' };
    return { cls: 'off', text: 'Офлайн' + (act ? ' · был ' + new Date(act.ts * 1000).toLocaleString('ru-RU') + (act.ip ? ' · ' + act.ip : '') : '') };
  }

  // Маршрут устройства (TASK-044): сегментированный переключатель «Эмуляция · Вместе ·
  // Прозрачно» (стиль .segmented), на узкой ширине — выпадающий список (CSS).
  // Значение proxy в данных и API прежнее, «Прозрачно» — только подпись (TASK-064);
  // «Вместе» в заданиях и контрактах прежде назывался «Гибрид» (hybrid).
  const ROUTE_LABEL = { emulate: 'Эмуляция', hybrid: 'Вместе', proxy: 'Прозрачно' };
  // Опция «Запись» устройства (TASK-054): пишет журнал трафика поверх любого маршрута.
  // Смена — сразу, без диалога: соединение не закрывается, опция действует на следующем обмене.
  const RECORD_LABEL = { off: 'Выключено', on: 'Включено', misses: 'Нет обработчиков' };
  const recordOf = device => device.record || 'off';
  function changeRecord(device, mode) {
    if (mode === recordOf(device)) return;
    work(async () => {
      await api('record_mode', 'PUT', { sn: device.sn, mode });
      await afterChange();
      message('Запись «' + device.name + '»: ' + RECORD_LABEL[mode] + '.');
    });
  }
  const effectiveRoute = device => device.route_mode || 'emulate';
  // Смена — сразу, без подтверждения (TASK-064). Сервер закрывает соединение (1012) только
  // при переходе в «Прозрачно» или из него; emulate ↔ hybrid открытый WS не трогает.
  function changeRoute(device, mode) {
    if (mode === effectiveRoute(device)) return;
    work(async () => {
      try {
        const result = await api('route_mode', 'PUT', { sn: device.sn, mode });
        await afterChange();
        message('Маршрут «' + device.name + '»: ' + ROUTE_LABEL[mode] + (result.disconnected ? '; соединение закрыто.' : '.'));
      } catch (err) { renderDevices(); throw err; } // выпадающий список вернётся к прежнему значению
    });
  }
  function routeCell(device) {
    const cell = node('td'); cell.className = 'route-cell';
    const current = effectiveRoute(device);
    const segmented = node('div'); segmented.className = 'segmented';
    for (const [value, label] of Object.entries(ROUTE_LABEL)) {
      const b = node('button', label); b.type = 'button';
      if (value === current) { b.classList.add('active'); b.disabled = true; }
      b.title = 'Маршрут: ' + label;
      b.onclick = () => changeRoute(device, value);
      segmented.append(b);
    }
    const select = node('select'); select.setAttribute('aria-label', 'Маршрут');
    for (const [value, label] of Object.entries(ROUTE_LABEL)) select.append(new Option(label, value));
    select.value = current;
    select.onchange = () => changeRoute(device, select.value);
    const box = node('div'); box.className = 'route-switch';
    box.append(segmented, select);
    cell.append(box);
    return cell;
  }

  function recordCell(device) {
    const cell = node('td'); cell.className = 'route-cell';
    const current = recordOf(device);
    const segmented = node('div'); segmented.className = 'segmented';
    for (const [value, label] of Object.entries(RECORD_LABEL)) {
      const b = node('button', label); b.type = 'button';
      if (value === current) { b.classList.add('active'); b.disabled = true; }
      b.title = 'Запись: ' + label;
      b.onclick = () => changeRecord(device, value);
      segmented.append(b);
    }
    const select = node('select'); select.setAttribute('aria-label', 'Запись');
    for (const [value, label] of Object.entries(RECORD_LABEL)) select.append(new Option(label, value));
    select.value = current;
    select.onchange = () => changeRecord(device, select.value);
    const box = node('div'); box.className = 'route-switch';
    box.append(segmented, select);
    cell.append(box);
    return cell;
  }

  function renderDevices() {
    devicesRoot.replaceChildren();
    // Пояснение маршрутов (TASK-044): построчно, названия режимов — жирным.
    const routeNote = node('div'); routeNote.className = 'hint route-note';
    const routeLine = (name, text) => { routeNote.append(node('b', name), document.createTextNode(' — ' + text)); };
    routeLine('Эмуляция', 'всё отвечает эмулятор.');
    routeNote.append(node('br'));
    routeLine('Вместе', 'эмулятор отвечает своими сценариями, остальное — настоящий Яндекс.');
    routeNote.append(node('br'));
    routeLine('Прозрачно', 'всё в Яндекс.');
    routeNote.append(node('br'), node('b', 'Новое устройство'), document.createTextNode(' — Эмуляция.'));
    routeNote.append(node('br'));
    routeLine('Запись', 'журнал трафика устройства, в любом маршруте: «Включено» — каждый обмен (ответ эмулятора или Яндекса) и AAPI-сообщения; «Нет обработчиков» — только то, чего эмулятор не умеет обработать. Звук не пишется. Смена действует сразу. Журнал — «Эмулятор → Журнал».');
    devicesRoot.append(routeNote);
    const bound = registry.devices.filter(d => d.user_id).length;
    const toolbar = node('div'); toolbar.className = 'sc-toolbar';
    const counts = node('span'); counts.className = 'hint';
    counts.textContent = `Устройств: ${registry.devices.length} · привязано: ${bound} · не привязано: ${registry.devices.length - bound}`;
    const reload = node('button', 'Обновить'); reload.type = 'button'; reload.className = 'secondary';
    reload.onclick = refresh;
    const add = node('button', 'Добавить устройство'); add.type = 'button';
    add.onclick = () => deviceDialog(null);
    toolbar.append(counts, reload, add); devicesRoot.append(toolbar);

    const table = node('table'); table.className = 'dev-table';
    const head = node('tr');
    head.append(node('th', 'Статус'), node('th', 'Модель / SN'), node('th', 'Привязка'), node('th', 'Действия'), node('th', 'Маршрут'), node('th', 'Запись'));
    const thead = node('thead'); thead.append(head); table.append(thead);
    const tbody = node('tbody');
    for (const device of registry.devices) {
      const row = node('tr'); row.dataset.sn = device.sn;
      const status = node('td');
      const state = statusOf(device);
      const dot = node('span'); dot.className = 'dot ' + state.cls; dot.title = state.text;
      status.append(dot); row.append(status);

      const parsed = parseSn(device.sn);
      const modelCell = node('td');
      const pic = node('img'); pic.className = 'thumb'; pic.title = modelTitle(parsed); pic.alt = '';
      applyPicture(pic, pictureChain(parsed));
      const about = node('span'); about.className = 'meta';
      const ip = node('small'); ip.className = 'ip'; ip.textContent = ipOf(device) ? 'IP ' + ipOf(device) : '';
      const modelLine = parsed ? parsed.name + (parsed.color ? ' · ' + parsed.color.label : '') : 'Неизвестная модель';
      about.append(node('b', modelLine), node('small', 'SN ' + device.sn), ip, node('small', device.name !== device.sn ? device.name : (device.device_ids[0] || '')));
      modelCell.append(pic, about); row.append(modelCell);

      const bind = node('td');
      if (device.user_id) {
        const chip = node('span', userName(device.user_id)); chip.className = 'chip';
        const unbind = node('button', '✕'); unbind.type = 'button'; unbind.className = 'icon-btn';
        unbind.title = 'Отвязать от пользователя'; unbind.setAttribute('aria-label', 'Отвязать от пользователя');
        unbind.onclick = () => work(async () => {
          if (!confirm(`Отвязать «${device.name}» от «${userName(device.user_id)}»? Состояние сбросится, соединение закроется — колонка переподключится сама.`)) return;
          await api('identities', 'POST', { operation: 'device', sn: device.sn, name: device.name, user_id: null, device_ids: device.device_ids, profile: device.profile, reset_state: true });
          await afterChange(); message('Устройство отвязано.');
        });
        bind.append(chip, unbind);
      } else {
        const warn = node('span', 'не привязано'); warn.className = 'chip warn';
        warn.title = 'Устройству нужен пользователь-владелец: без него AI-цепочка не запускается';
        const bindBtn = node('button', 'Привязать'); bindBtn.type = 'button';
        bindBtn.onclick = () => bindDialog(device);
        bind.append(warn, bindBtn);
      }
      row.append(bind);
      const actions = node('td');
      const del = node('button', 'Удалить'); del.type = 'button'; del.className = 'secondary danger';
      del.title = 'Убрать из списка (с отвязкой); при следующем представлении устройства (WS или /get_sync_info) оно вернётся — непривязанным';
      del.onclick = () => work(async () => {
        if (!confirm(`Удалить «${device.name}»? Уйдёт из списка вместе с привязкой. Как только колонка снова представится (WS-подключение или /get_sync_info) — вернётся в список как непривязанное.`)) return;
        await api('identities', 'POST', { operation: 'device_delete', sn: device.sn });
        await afterChange(); message('Устройство удалено; вернётся в список с первым /get_sync_info от него.');
      });
      actions.append(del); row.append(actions);
      row.append(routeCell(device), recordCell(device));
      tbody.append(row);
    }
    if (!registry.devices.length) {
      const empty = node('tr'); const cell = node('td', 'Устройств пока нет: колонка попадёт в таблицу сама — при WS-подключении или первом запросе /get_sync_info.'); cell.colSpan = 6;
      empty.append(cell); tbody.append(empty);
    }
    table.append(tbody); devicesRoot.append(table);
    fingerprint = JSON.stringify({ u: registry.users, d: registry.devices });
  }

  // Поллинг: структура не изменилась — только точечно обновляем индикаторы,
  // DOM не пересоздаётся (страница не «мигает», выделение и скролл живут).
  function updateStatuses() {
    for (const device of registry.devices) {
      const row = devicesRoot.querySelector('tr[data-sn="' + CSS.escape(device.sn) + '"]');
      if (!row) continue;
      const dot = row.querySelector('.dot');
      const state = statusOf(device);
      dot.className = 'dot ' + state.cls; dot.title = state.text;
      const ip = row.querySelector('.meta .ip');  // IP меняется вместе со статусом — тоже на месте
      if (ip) { const value = ipOf(device); ip.textContent = value ? 'IP ' + value : ''; }
    }
  }
  function refreshDevices() {
    if (JSON.stringify({ u: registry.users, d: registry.devices }) !== fingerprint) renderDevices();
    else updateStatuses();
  }
  async function afterChange() { await fetchRegistry(); fingerprint = ''; refreshDevices(); }

  // ── Диалоги ────────────────────────────────────────────────────────
  function dialogFrame(title) {
    const dialog = node('dialog'); dialog.className = 'catalog-dialog';
    const form = node('form');
    // Крестик закрывает окно мышью; Esc по-прежнему шлёт cancel — оба пути убирают диалог
    const head = node('div'); head.className = 'dialog-head';
    const close = node('button', '✕'); close.type = 'button'; close.className = 'icon-btn';
    close.title = 'Закрыть'; close.setAttribute('aria-label', 'Закрыть');
    const dismiss = () => { dialog.close(); dialog.remove(); };
    close.onclick = dismiss;
    head.append(node('h3', title), close);
    form.append(head);
    dialog.append(form);
    dialog.addEventListener('cancel', dismiss);
    document.body.append(dialog);
    return { dialog, form };
  }
  // showModal ставит фокус на первый элемент — крестик: Enter закрывал бы окно вместо
  // сохранения. Переносим фокус в первое поле ввода.
  function openDialog(dialog) {
    dialog.showModal();
    const field = dialog.querySelector('input:not([type=hidden]), select, textarea');
    if (field) field.focus();
  }
  // Ошибки видно внутри модального окна, а не только в статусе за затемнением
  const errorLine = form => { const p = node('p'); p.className = 'hint'; p.style.color = 'var(--danger)'; p.hidden = true; form.append(p); return p; };
  const submitForm = (form, errLine, fn) => form.onsubmit = e => { e.preventDefault(); errLine.hidden = true; errLine.textContent = ''; work(async () => { try { await fn(); } catch (err) { errLine.hidden = false; errLine.textContent = err.message; throw err; } }); };
  const userSelect = (form, title, selected) => {
    const label = node('label', title);
    const select = node('select');
    const empty = node('option', '— без пользователя —'); empty.value = ''; select.append(empty);
    for (const user of registry.users) { const option = node('option', user.name); option.value = user.id; select.append(option); }
    select.value = selected || '';
    label.append(select); form.append(label);
    return select;
  };

  function bindDialog(device) {
    if (!registry.users.length) { message('Сначала создайте пользователя в разделе «Пользователи».', true); return; }
    const { dialog, form } = dialogFrame('Привязать «' + device.name + '»');
    const select = userSelect(form, 'Пользователь-владелец', device.user_id);
    const warn = node('p', 'Смена владельца сбрасывает состояние устройства и закрывает его соединение — колонка переподключится сама.'); warn.className = 'hint'; form.append(warn);
    const save = node('button', 'Привязать'); save.type = 'submit'; form.append(save);
    const errLine = errorLine(form);
    submitForm(form, errLine, async () => {
      await api('identities', 'POST', { operation: 'device', sn: device.sn, name: device.name, user_id: select.value || null, device_ids: device.device_ids, profile: device.profile, reset_state: true });
      dialog.close(); dialog.remove(); await afterChange(); message('Устройство привязано к «' + userName(select.value) + '».');
    });
    openDialog(dialog);
  }

  function deviceDialog(device) {
    const { dialog, form } = dialogFrame(device ? 'Устройство' : 'Новое устройство');
    const snLabel = node('label', 'Серийный номер (SN)');
    const sn = node('input'); sn.required = true; sn.maxLength = 256; sn.value = device ? device.sn : ''; if (device) sn.readOnly = true;
    snLabel.append(sn); form.append(snLabel);
    const snHint = node('p'); snHint.className = 'hint';
    const snModel = () => {
      const parsed = parseSn(sn.value.trim());
      snHint.textContent = parsed
        ? 'Модель по SN: ' + parsed.name + (parsed.color ? ' · ' + parsed.color.label : '') + ' · платформа ' + parsed.platform + (parsed.crcOk === false ? ' · SN не сходится (CRC)' : '')
        : (sn.value.trim() ? 'Модель по SN: не распознана' : 'Модель по SN: —');
    };
    sn.oninput = snModel; snModel(); form.append(snHint);
    const nameLabel = node('label', 'Название устройства');
    const name = node('input'); name.maxLength = 256; name.value = device ? device.name : '';
    nameLabel.append(name); form.append(nameLabel);
    const aliasLabel = node('label', 'Наблюдаемые DeviceId (по одному на строку)');
    const aliases = node('textarea'); aliases.rows = 3; aliases.value = device ? device.device_ids.join('\n') : '';
    aliasLabel.append(aliases); form.append(aliasLabel);
    const select = userSelect(form, 'Пользователь-владелец', device ? device.user_id : '');
    const save = node('button', 'Сохранить'); save.type = 'submit'; form.append(save);
    const errLine = errorLine(form);
    submitForm(form, errLine, async () => {
      await api('identities', 'POST', { operation: 'device', sn: sn.value.trim(), name: name.value.trim() || sn.value.trim(), user_id: select.value || null, device_ids: aliases.value.split('\n').map(x => x.trim()).filter(Boolean), profile: device ? device.profile : {}, reset_state: true });
      dialog.close(); dialog.remove(); await afterChange(); message('Устройство сохранено.');
    });
    openDialog(dialog);
  }

  // ── Загрузка по разделам и тихий поллинг статусов ──────────────────
  const sync = () => {
    if (location.hash === '#accounts') work(async () => { if (await fetchRegistry()) renderAccounts(); });
    else if (location.hash === '#devices') work(async () => { if (await fetchRegistry()) refreshDevices(); });
  };
  window.addEventListener('hashchange', sync);
  sync();
  setInterval(async () => {
    if (location.hash !== '#devices' || document.hidden || document.querySelector('dialog[open]')) return;
    try { if (!await fetchRegistry()) return; } catch { return; }  // тихо: поллинг не трогает кнопки и статус
    refreshDevices();
  }, 5000);
})();
