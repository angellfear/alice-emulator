// Первичный конфигуратор обязательных параметров (<dialog>): список незаполненных
// (GET api/params_required), поля ввода, «Подтянуть из аккаунта Яндекса» — журнал
// get_sync_info (без входа) + iot-API (нужен вход: токен / cookies / QR; секреты
// остаются в state/, наружу только статус). Заполненное пишется в каталог «Параметры»,
// закреплённые уведомления снимаются следующей сборкой конфига сами.
// Открытие — window.configuratorOpen(): из закреплённого уведомления и из «Параметров».
// API — ui/api/account.py (account.py).
(() => {
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const $ = id => document.getElementById(id);
  let poll = null;  // таймер опроса QR-подтверждения

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

  // ── аккаунт Яндекса ───────────────────────────────────────────────────────────
  function stopPoll() { if (poll) { clearInterval(poll); poll = null; } }

  function renderAccount(status) {
    const actions = $('configurator-account-actions');
    const hint = $('configurator-account-hint');
    actions.replaceChildren();
    $('configurator-login').hidden = true;
    stopPoll();
    if (status.login) {
      hint.textContent = 'Аккаунт: ' + (status.login_name || 'вход выполнен') +
        '. Уведомления и токен — только в state/ эмулятора.';
      const forget = node('button', 'Забыть аккаунт', 'secondary');
      forget.type = 'button';
      forget.onclick = () => work(async () => {
        await api('account', {op: 'logout'});
        await refresh();
      });
      actions.append(forget);
      return;
    }
    hint.textContent = 'Вход не выполнен: импорт из журнала работает и без него.';
    const open = node('button', 'Войти по токену / cookies / QR', 'secondary');
    open.type = 'button';
    open.onclick = () => { $('configurator-login').hidden = false; };
    actions.append(open);
  }

  function loginForms() {
    const box = $('configurator-login');
    box.replaceChildren();
    const token = node('input'); token.placeholder = 'x_token аккаунта';
    const tokenBtn = node('button', 'Войти по токену', 'secondary');
    tokenBtn.type = 'button';
    tokenBtn.onclick = () => work(async () => {
      renderAccount(await api('account', {op: 'login_token', token: token.value.trim()}));
      message('Вход выполнен');
    });
    const cookies = node('textarea'); cookies.rows = 2;
    cookies.placeholder = 'yandexuid=…; Session_id=… или JSON «Copy Cookies»';
    const cookiesBtn = node('button', 'Войти по cookies', 'secondary');
    cookiesBtn.type = 'button';
    cookiesBtn.onclick = () => work(async () => {
      renderAccount(await api('account', {op: 'login_cookies', cookies: cookies.value.trim()}));
      message('Вход выполнен');
    });
    const qrBtn = node('button', 'Получить ссылку QR', 'secondary');
    qrBtn.type = 'button';
    const qrLink = node('p', null, 'hint');
    qrBtn.onclick = () => work(async () => {
      const {link} = await api('account', {op: 'qr_start'});
      qrLink.replaceChildren(node('a', 'Подтвердите вход по ссылке', 'hint'));
      qrLink.firstChild.href = link;
      qrLink.firstChild.target = '_blank';
      qrLink.firstChild.rel = 'noopener';
      stopPoll();
      poll = setInterval(() => work(async () => {
        const state = await api('account', {op: 'qr_poll'});
        if (state.state === 'done') {
          stopPoll();
          renderAccount(state);
          message('Вход выполнен');
        }
      }).catch(() => {}), 2000);
    });
    box.append(node('p', 'Вход по токену', 'hint'), token, tokenBtn,
               node('p', 'Вход по cookies', 'hint'), cookies, cookiesBtn,
               node('p', 'QR-вход: ссылка подтверждается в приложении Яндекса', 'hint'),
               qrBtn, qrLink);
  }

  async function importAccount() {
    const result = $('configurator-import-result');
    result.textContent = 'Подтягиваю…';
    const parts = [];
    const sync = await api('params_import', {source: 'get_sync_info'});
    parts.push('журнал get_sync_info: значений ' + sync.items.length +
               (sync.answers ? ' (ответов колонок: ' + sync.answers + ')' : ''));
    const status = await api('account');
    if (status.login) {
      const iot = await api('params_import', {source: 'iot'});
      parts.push('аккаунт: значений ' + iot.items.length);
    }
    result.textContent = parts.join(' · ') + '. Секреты записаны в каталог без показа.';
    await renderRequired();
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
    renderAccount(await api('account'));
    loginForms();
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
    $('configurator-close').onclick = () => { stopPoll(); $('configurator-dialog').close(); };
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
