// Раздел «Параметры»: каталог определений (param_defs) + значения server-scope.
// Определения админа: ключ/тип/категория/user_editable/дефолт; значения
// владельцев (user/device) редактируются в карточке пользователя и в /me.
(() => {
  const root = document.getElementById('params-list');
  if (!root) return;
  const node = (tag, text) => { const n = document.createElement(tag); if (text != null) n.textContent = text; return n; };
  let data = null;

  async function api(action, value, method) {
    const response = await fetch('api/' + action, {
      method: method || (value ? 'PUT' : 'GET'),
      headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' },
      body: value ? JSON.stringify(value) : undefined });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || response.statusText);
    return result;
  }

  async function load() {
    data = await api('params');
    render();
  }

  function render() {
    root.replaceChildren();
    const counts = document.getElementById('params-counts');
    const editable = data.defs.filter(d => d.user_editable).length;
    counts.textContent = `Определений: ${data.defs.length} · доступно пользователям: ${editable}`;
    const table = node('table'); table.className = 'dev-table';
    const head = node('tr');
    head.append(node('th', 'Ключ'), node('th', 'Тип'), node('th', 'Область/категория'),
                node('th', 'Пользователю'), node('th', 'Подпись'), node('th', 'Действия'));
    const thead = node('thead'); thead.append(head); table.append(thead);
    const tbody = node('tbody');
    for (const def of data.defs) {
      const row = node('tr');
      const key = node('td'); key.append(node('code', def.key)); row.append(key);
      row.append(node('td', def.type + (def.options ? ` (${JSON.stringify(def.options).slice(0, 40)})` : '')));
      row.append(node('td', `${def.scope} · ${def.category}`));
      row.append(node('td', def.user_editable ? 'да' : 'только админ'));
      row.append(node('td', def.title + (def.hint ? ' — ' + def.hint : '')));
      const actions = node('td');
      const edit = node('button', 'Править'); edit.type = 'button'; edit.className = 'secondary';
      edit.onclick = () => dialog(def);
      const del = node('button', 'Удалить'); del.type = 'button'; del.className = 'secondary danger';
      del.onclick = () => work(async () => {
        if (!confirm(`Удалить параметр ${def.key} вместе со значениями?`)) return;
        await api('params', null, 'DELETE');
        await load(); message('Параметр удалён: ' + def.key);
      });
      actions.append(edit, del); row.append(actions);
      tbody.append(row);
    }
    if (!data.defs.length) {
      const empty = node('tr'); const cell = node('td', 'Каталог пуст (сид создаёт базовые user/device параметры).');
      cell.colSpan = 6; empty.append(cell); tbody.append(empty);
    }
    table.append(tbody); root.append(table);
    // server-scope значения прямо здесь
    const serverDefs = data.defs.filter(d => d.scope === 'server');
    if (serverDefs.length) {
      const box = node('div'); box.className = 'endpoint-tree';
      const h3 = node('h3', 'Серверные значения'); h3.style.margin = '10px 0 4px'; box.append(h3);
      work(async () => {
        const values = (await api('param_values?owner=server')).values;
        for (const def of serverDefs) {
          const row = node('div'); row.className = 'tree-leaf';
          row.append(node('span', def.title + ' (' + def.key + ')'));
          row.append(valueInput(def, (values[def.key] || {}).value, async value => {
            await api('param_values', { owner: 'server', key: def.key, value });
            message('Сохранено: ' + def.key);
          }));
          box.append(row);
        }
      });
      root.append(box);
    }
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
    const dlg = document.createElement('dialog'); dlg.style.maxWidth = '620px';
    const form = document.createElement('form'); dlg.append(form); document.body.append(dlg);
    const field = (labelText, input) => { const l = node('label', labelText); l.append(input); return l; };
    const key = node('input'); key.value = def ? def.key : ''; key.placeholder = 'user.wifi_hash';
    const scope = node('select'); for (const s of ['user', 'device', 'server']) scope.append(new Option(s, s));
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

  document.getElementById('params-new').onclick = () => dialog(null);
  document.getElementById('params-reload').onclick = () => work(load);
  const sync = () => { if (location.hash === '#params') work(load); };
  window.addEventListener('hashchange', sync);
  sync();
})();
