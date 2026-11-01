// «Эмулятор → Параметры» (TASK-060): две подвкладки. «Определения» — каталог ключей по
// областям (правка в <dialog>); «Значения» — владелец (пользователь / устройство /
// платформа / сервер), форма по определениям его области; у устройства — итог цепочки
// устройство → пользователь → платформа → сервер и уровень, откуда он взят
// (GET param_values?owner=device:<sn>&resolve=1). Секреты — маска __KEEP__, как в
// карточке пользователя и /me. API — ui/api/params_api.py.
(() => {
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const $ = id => document.getElementById(id);
  let data = null, registry = null, platforms = [], subtab = 'defs';
  let owner = {kind: 'server', id: ''};  // владелец значений: {kind: user|device|platform|server, id}

  const SCOPE_HINT = {user: 'Пользователь — у каждого пользователя своё значение.',
                      device: 'Устройство — значение конкретной колонки.',
                      server: 'Сервер — общий для всего эмулятора.'};
  const SCOPE_TITLE = {user: 'Пользователь', device: 'Устройство', server: 'Сервер', platform: 'Платформа'};
  const SOURCE_LABEL = {device: 'устройство', user: 'пользователь', platform: 'платформа', server: 'сервер', default: 'по умолчанию'};

  async function api(action, value, method) {
    const response = await fetch('api/' + action, {
      method: method || (value ? 'PUT' : 'GET'),
      headers: {'X-Emulator': '1', 'Content-Type': 'application/json'},
      body: value ? JSON.stringify(value) : undefined});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || response.statusText);
    return result;
  }

  async function load() {
    [data, registry] = await Promise.all([api('params'), api('identities')]);
    try { platforms = (await api('quasar_platforms')).platforms.map(p => p.platform); }
    catch (e) { platforms = []; }  // конфиги Quasar не настроены — платформ из них нет
    render();
  }

  function render() {
    if (!$('params-list')) return;
    document.querySelectorAll('#params-subtabs button').forEach(b => {
      b.classList.toggle('active', b.dataset.sub === subtab);
      b.setAttribute('aria-checked', String(b.dataset.sub === subtab));
    });
    $('params-defs-wrap').hidden = subtab !== 'defs';
    $('params-values').hidden = subtab !== 'values';
    if (subtab === 'defs') renderDefs(); else renderValues();
  }

  // ── Определения ────────────────────────────────────────────────────────────────
  function renderDefs() {
    const editable = data.defs.filter(d => d.user_editable).length;
    $('params-counts').textContent = `Определений: ${data.defs.length} · доступно пользователям: ${editable}`;
    const root = $('params-list');
    root.replaceChildren();
    for (const scope of data.scopes) {
      const defs = data.defs.filter(d => d.scope === scope);
      const box = node('div', null, 'panel');
      box.append(node('h3', SCOPE_TITLE[scope] || scope), node('p', SCOPE_HINT[scope] || '', 'hint'));
      const table = node('table'); table.className = 'dev-table';
      const head = node('tr');
      head.append(node('th', 'Ключ'), node('th', 'Тип'), node('th', 'Категория'),
                  node('th', 'Правит владелец'), node('th', 'Подпись'), node('th', 'Действия'));
      const thead = node('thead'); thead.append(head); table.append(thead);
      const tbody = node('tbody');
      for (const def of defs) {
        const row = node('tr');
        const key = node('td'); key.append(node('code', def.key)); row.append(key);
        row.append(node('td', def.type + (def.options ? ` (${JSON.stringify(def.options).slice(0, 40)})` : '')));
        row.append(node('td', def.category));
        const editableCell = node('td');
        if (def.user_editable) editableCell.append(node('span', '✓ правит владелец', 'chip'));
        row.append(editableCell);
        row.append(node('td', def.title + (def.hint ? ' — ' + def.hint : '')));
        const actions = node('td');
        const edit = node('button', 'Править'); edit.type = 'button'; edit.className = 'secondary';
        edit.onclick = () => dialog(def);
        const del = node('button', 'Удалить'); del.type = 'button'; del.className = 'secondary danger';
        del.onclick = () => {
          const confirmDialog = document.createElement('dialog');
          const text = node('p', `Удалить параметр ${def.key} вместе со значениями?`);
          const row = node('div', null, 'row');
          const yes = node('button', 'Удалить'); yes.type = 'button'; yes.className = 'danger';
          const no = node('button', 'Отмена'); no.type = 'button'; no.className = 'secondary';
          yes.onclick = () => work(async () => {
            await api('params', null, 'DELETE'); confirmDialog.close(); confirmDialog.remove();
            await load(); message('Параметр удалён: ' + def.key); });
          no.onclick = () => { confirmDialog.close(); confirmDialog.remove(); };
          row.append(no, yes); confirmDialog.append(text, row); document.body.append(confirmDialog);
          confirmDialog.showModal();
        };
        actions.append(edit, del); row.append(actions);
        tbody.append(row);
      }
      if (!defs.length) {
        const empty = node('tr'); const cell = node('td', 'Определений нет.');
        cell.colSpan = 6; empty.append(cell); tbody.append(empty);
      }
      table.append(tbody); box.append(table); root.append(box);
    }
  }

  // ── Значения ───────────────────────────────────────────────────────────────────
  function ownerKey() {
    return owner.kind === 'server' ? 'server' : `${owner.kind}:${owner.id}`;
  }

  function renderValues() {
    const root = $('params-values');
    root.replaceChildren();
    root.append(node('p', 'Значения выбранного владельца по определениям его области; сохраняются сразу при правке. Для устройства видно итоговое значение цепочки устройство → пользователь → платформа → сервер и уровень, откуда оно взято (клик по уровню — переход к тому владельцу).', 'hint'));
    const kindSelect = node('select');
    for (const [k, label] of [['user', 'Пользователь'], ['device', 'Устройство'],
                              ['platform', 'Платформа'], ['server', 'Сервер']])
      kindSelect.append(new Option(label, k));
    kindSelect.value = owner.kind;
    const specific = node('select');
    const fillSpecific = () => {
      specific.replaceChildren();
      if (kindSelect.value === 'user')
        for (const u of registry.users) specific.append(new Option(u.name || u.id, u.id));
      else if (kindSelect.value === 'device')
        for (const d of registry.devices) specific.append(new Option((d.name || d.sn) + ' · ' + d.sn, d.sn));
      else if (kindSelect.value === 'platform')
        for (const p of platforms) specific.append(new Option(p, p));
    };
    fillSpecific();
    if (owner.kind !== 'server' && owner.id) specific.value = owner.id;
    owner = kindSelect.value === 'server' ? {kind: 'server', id: ''} : {kind: kindSelect.value, id: specific.value};
    kindSelect.onchange = () => { owner = {kind: kindSelect.value, id: ''}; renderValues(); };
    specific.onchange = () => { owner = {kind: kindSelect.value, id: specific.value}; renderValues(); };
    const bar = node('div', null, 'sc-toolbar');
    const kl = node('label', 'Тип владельца'); kl.append(kindSelect);
    const sl = node('label', 'Владелец'); sl.append(specific);
    sl.style.display = kindSelect.value === 'server' ? 'none' : '';
    bar.append(kl, sl);
    root.append(bar);

    const scope = kindSelect.value === 'server' ? 'server' : kindSelect.value;
    const defs = data.defs.filter(d => d.scope === scope);
    if (!defs.length) { root.append(node('p', 'Для этой области определений нет.', 'hint')); return; }
    if (owner.kind !== 'server' && !owner.id) { root.append(node('p', 'Выберите владельца.', 'hint')); return; }

    const table = node('table'); table.className = 'dev-table';
    const head = node('tr');
    head.append(node('th', 'Параметр'), node('th', 'Значение'));
    if (owner.kind === 'device') { head.append(node('th', 'Итог'), node('th', 'Откуда')); }
    const thead = node('thead'); thead.append(head); table.append(thead);
    const tbody = node('tbody'); table.append(tbody);
    root.append(table);
    work(async () => {
      const query = 'param_values?owner=' + encodeURIComponent(ownerKey()) +
                    (owner.kind === 'device' ? '&resolve=1' : '');
      const answer = await api(query);
      const resolved = answer.resolved || {};
      for (const def of defs) {
        const row = node('tr');
        const name = node('td'); name.append(node('code', def.key),
                                             node('div', def.title + (def.hint ? ' — ' + def.hint : ''), 'hint'));
        row.append(name);
        const valueCell = node('td');
        valueCell.append(valueInput(def, (answer.values[def.key] || {}).value, async value => {
          await api('param_values', {owner: ownerKey(), key: def.key, value});
          message('Сохранено: ' + def.key);
        }));
        row.append(valueCell);
        if (owner.kind === 'device') {
          const info = resolved[def.key] || {};
          row.append(node('td', info.value === undefined ? '—' : JSON.stringify(info.value)));
          const sourceCell = node('td');
          if (info.source && info.source !== 'device') {
            const chip = node('span', SOURCE_LABEL[info.source] || info.source, 'chip');
            chip.style.cursor = 'pointer';
            chip.title = 'Открыть значения этого владельца';
            chip.onclick = () => openOwnerLevel(info.source);
            sourceCell.append(chip);
          } else sourceCell.append(node('span', SOURCE_LABEL[info.source] || '—'));
          row.append(sourceCell);
        }
        tbody.append(row);
      }
    });
  }

  function openOwnerLevel(source) {
    const device = registry.devices.find(d => d.sn === owner.id);
    if (source === 'user' && device && device.user_id) owner = {kind: 'user', id: device.user_id};
    else if (source === 'platform') owner = {kind: 'platform',
                                             id: ((device || {}).profile || {}).platform || ''};
    else if (source === 'server') owner = {kind: 'server', id: ''};
    renderValues();
  }

  function valueInput(def, current, save) {
    let input;
    if (def.type === 'bool') {
      input = node('input'); input.type = 'checkbox'; input.checked = !!current;
      input.onchange = () => save(input.checked);
    } else if (def.type === 'select') {
      input = node('select');
      for (const option of def.options || []) input.append(new Option(String(option), String(option)));
      input.value = current ?? '';
      input.onchange = () => save(input.value);
    } else if (def.type === 'text-list') {
      input = node('input'); input.type = 'text'; input.value = Array.isArray(current) ? current.join(', ') : '';
      input.onchange = () => save(input.value.split(',').map(x => x.trim()).filter(Boolean));
    } else if (def.type === 'number') {
      input = node('input'); input.type = 'number'; input.step = 'any'; input.value = current ?? '';
      input.onchange = () => save(Number(input.value));
    } else {
      input = node('input'); input.type = 'text'; input.value = current ?? '';
      input.onchange = () => save(input.value);
    }
    return input;
  }

  function dialog(def) {
    const dlg = document.createElement('dialog'); dlg.className = 'params-dialog';
    const form = document.createElement('form'); dlg.append(form); document.body.append(dlg);
    const field = (labelText, input) => { const l = node('label', labelText); l.append(input); return l; };
    const key = node('input'); key.value = def ? def.key : ''; key.placeholder = 'user.wifi_hash';
    const scope = node('select'); for (const s of data.scopes) scope.append(new Option(s, s));
    scope.value = def ? def.scope : 'user';
    const type = node('select'); for (const t of data.types) type.append(new Option(t, t));
    type.value = def ? def.type : 'string';
    const category = node('input'); category.value = def ? def.category : '';
    const editable = node('input'); editable.type = 'checkbox'; editable.checked = def ? def.user_editable : false;
    const title = node('input'); title.value = def ? def.title : '';
    const hint = node('input'); hint.value = def ? def.hint : '';
    const options = node('input'); options.value = def && def.options ? JSON.stringify(def.options) : '';
    options.placeholder = 'select: ["a","b"] · number: {"min":0,"max":10}';
    const defv = node('input'); defv.value = def && def.default_value !== null && def.default_value !== undefined ? JSON.stringify(def.default_value) : '';
    defv.placeholder = 'значение по умолчанию (JSON)';
    const save = node('button', 'Сохранить'); save.type = 'submit';
    form.append(field('Ключ', key), field('Область', scope), field('Тип', type),
                field('Категория (группа UI)', category), field('Доступно пользователю (его морда /me)', editable),
                field('Подпись', title), field('Пояснение', hint), field('Опции (JSON)', options),
                field('По умолчанию (JSON)', defv), save);
    form.onsubmit = event => { event.preventDefault(); work(async () => {
      const definition = { key: key.value.trim(), scope: scope.value, type: type.value,
                           category: category.value.trim() || scope.value, user_editable: editable.checked,
                           title: title.value.trim(), hint: hint.value.trim(),
                           options: options.value.trim() ? JSON.parse(options.value) : null,
                           default_value: defv.value.trim() ? JSON.parse(defv.value) : null };
      await api('params', { definition });
      dlg.close(); dlg.remove(); await load(); message('Определение сохранено: ' + definition.key);
    }) };
    dlg.showModal(); dlg.addEventListener('close', () => dlg.remove());
  }

  // Ссылка «Все параметры» из карточки пользователя (users.js) — «Значения» с этим владельцем
  window.paramsOpenOwner = (kind, id) => {
    owner = {kind, id: kind === 'server' ? '' : String(id || '')};
    subtab = 'values';
    if (data) render();
    goTab('params');
  };

  for (const b of document.querySelectorAll('#params-subtabs button'))
    b.onclick = () => { subtab = b.dataset.sub; render(); };
  document.getElementById('params-new').onclick = () => subtab === 'defs' && dialog(null);
  document.getElementById('params-reload').onclick = () => work(load);
  const sync = () => { if (location.hash === '#params') work(load); };
  window.addEventListener('hashchange', sync);
  sync();
})();
