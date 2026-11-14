// Первичный конфигуратор обязательных параметров (<dialog>): список незаполненных
// (GET api/params_required), поля ввода, сводка привязанных Яндекс-аккаунтов и
// «Подтянуть из аккаунта Яндекса» — журнал get_sync_info (без входа) + iot-API по
// всем входам пользователей сразу. Вход/выход — кнопка «Яндекс» у пользователя на
// вкладке «Пользователи»; секреты остаются в state/, наружу только статус.
// Заполненное пишется в каталог «Параметры», закреплённые уведомления снимаются
// следующей сборкой конфига сами.
// Открытие — window.configuratorOpen(): из закреплённого уведомления и из «Параметров».
// API — ui/api/account.py (account.py).
(() => {
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const $ = id => document.getElementById(id);

  async function api(action, value, method) {
    const response = await fetch('api/' + action, {
      method: method || (value ? 'POST' : 'GET'),
      headers: {'X-Emulator': '1', 'Content-Type': 'application/json'},
      body: value ? JSON.stringify(value) : undefined});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || response.statusText);
    return result;
  }

  function fail(text) {
    const box = $('configurator-error');
    box.textContent = text;
    box.hidden = false;
  }
  const clearError = () => { $('configurator-error').hidden = true; };

  // ── обязательные незаполненные ────────────────────────────────────────────────
  async function renderRequired() {
    const root = $('configurator-required');
    root.replaceChildren();
    const {required} = await api('params_required');
    if (!required.length) {
      root.append(node('p', 'Все обязательные параметры заполнены.', 'hint'));
      return required;
    }
    for (const item of required) {
      const label = node('label', item.title);
      label.append(node('div', item.key + (item.hint ? ' — ' + item.hint : ''), 'hint'));
      const input = item.type === 'number' ? node('input') : node('input');
      if (item.type === 'number') { input.type = 'number'; input.step = 'any'; }
      input.dataset.key = item.key;
      input.dataset.owner = item.missing[0];
      label.append(input);
      root.append(label);
    }
    return required;
  }

  // ── аккаунты Яндекса: сводка по всем пользователям ────────────────────────────
  async function linkedCount() {
    const {users} = await api('identities');
    let linked = 0;
    for (const user of users || []) {
      const status = await api('account?user=' + encodeURIComponent(user.id)).catch(() => null);
      if (status && status.login) linked++;
    }
    return {linked, total: (users || []).length};
  }

  async function renderAccountSummary() {
    const {linked, total} = await linkedCount();
    $('configurator-account-summary').textContent = linked
      ? `Вход выполнен у ${linked} из ${total} пользователей; вход и выход — на вкладке «Пользователи».`
      : 'Входа в Яндекс нет ни у одного пользователя — вход настраивается на вкладке «Пользователи». Импорт из журнала работает и без него.';
  }

  async function importAccount() {
    const result = $('configurator-import-result');
    result.textContent = 'Подтягиваю…';
    const parts = [];
    const sync = await api('params_import', {source: 'get_sync_info'});
    parts.push('журнал get_sync_info: значений ' + sync.items.length +
               (sync.answers ? ' (ответов колонок: ' + sync.answers + ')' : ''));
    const {linked} = await linkedCount();
    if (linked) {
      const iot = await api('params_import', {source: 'iot'});
      parts.push('аккаунт: значений ' + iot.items.length);
    }
    result.textContent = parts.join(' · ') + '. Секреты записаны в каталог без показа.';
    await renderRequired();
    await renderAccountSummary();
  }

  // ── каркас диалога ────────────────────────────────────────────────────────────
  async function work(action) {
    try {
      clearError();
      return await action();
    } catch (e) { fail(String(e.message || e)); return undefined; }
  }

  async function refresh() {
    const required = await renderRequired();
    await renderAccountSummary();
    $('configurator-import-result').textContent = '';
    return required;
  }

  function open() {
    const dlg = $('configurator-dialog');
    if (!dlg || dlg.open) return;
    work(refresh);
    dlg.showModal();
  }

  function bind() {
    if (!$('configurator-dialog')) return;
    $('configurator-open').onclick = open;                 // кнопка из «Параметров»
    $('configurator-close').onclick = () => { $('configurator-dialog').close(); };
    $('configurator-import').onclick = () => work(importAccount);
    $('configurator-form').onsubmit = event => { event.preventDefault(); work(async () => {
      const inputs = [...$('configurator-required').querySelectorAll('input[data-key]')];
      const filled = [];
      for (const input of inputs) {
        if (input.value.trim() === '') continue;
        const value = input.type === 'number' ? Number(input.value) : input.value.trim();
        await api('param_values', {owner: input.dataset.owner, key: input.dataset.key, value}, 'PUT');
        filled.push(input.dataset.key);
      }
      if (filled.length) message('Заполнено: ' + filled.join(', ') +
        '. Уведомления снимутся при следующем опросе колонки.');
      await renderRequired();
    }) };
  }

  window.configuratorOpen = open;
  bind();
})();
