// «Настройки → Авторизация в сервисах»: реестр аккаунтов сервисов и копилка
// токенов колонок. Яндекс — вход по QR (ссылка подтверждается в приложении
// Яндекса), несколько аккаунтов, один основной; Spotify — вход через точки login
// поставщика музыки (redirect на панель), тоже несколько аккаунтов. Токены колонок
// собираются сами из обращений колонок к Яндексу — здесь только список SN.
// Секреты остаются в state/, наружу только статусы.
// API — ui/api/service_accounts.py; вход Spotify — providers_login (точки login плагина).
(() => {
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const say = (text, error = false) => notify(text, { error });

  async function api(action, value, method) {
    const response = await fetch('api/' + action, {
      method: method || (value ? 'POST' : 'GET'),
      headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' },
      body: value ? JSON.stringify(value) : undefined });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(result.error || response.statusText);
    return result;
  }

  const state = { accounts: [], columns: [], poll: null };
  const active = () => location.hash === '#service-accounts';
  const stopPoll = () => { if (state.poll) { clearInterval(state.poll); state.poll = null; } };
  window.addEventListener('hashchange', () => { if (!active()) stopPoll(); else load(); });
  if (active()) load();

  async function load() {
    if (!active()) return;
    const root = document.getElementById('service-accounts');
    if (!root) return;
    try {
      const [data, providers] = await Promise.all([api('service-accounts'), api('providers')]);
      state.accounts = data.accounts || [];
      state.columns = data.columns || [];
      render(root, providers);
    } catch (e) {
      root.replaceChildren(node('h2', 'Авторизация в сервисах'), node('p', 'Нет связи: ' + e.message, 'hint'));
    }
  }

  // Поставщик, объявивший вход аккаунта (плагин Spotify): его kind/id нужны точкам
  // providers_login; поставщика нет — расширение не установлено.
  const loginProvider = providers => {
    for (const kind of Object.values(providers?.kinds || {}))
      for (const p of kind.providers || [])
        if (p.login) return { kind: Object.keys(providers.kinds).find(k => providers.kinds[k] === kind), id: p.id, title: p.title };
    return null;
  };

  function render(root, providers) {
    root.replaceChildren(
      node('h2', 'Авторизация в сервисах'),
      node('p', 'Аккаунты сервисов для поставщиков данных. Токены остаются на сервере (state/) — наружу только статусы.', 'hint'),
      serviceCard('yandex', 'Яндекс',
        'Вход по QR: код сканируется приложением Яндекса на телефоне — страница входа не открывается. Аккаунтов может быть несколько — помечьте основной: им пользуется распознавание музыки, когда у колонки нет своего токена.',
        qrEntry),
      serviceCard('spotify', 'Spotify',
        'Вход через страницу Spotify. Поставщик музыки должен быть установлен, у него заданы ключи Web API, а панель открыта по http://127.0.0.1:<порт> — redirect регистрируется в Spotify Dashboard без порта.',
        () => spotifyEntry(providers)));
    const others = state.accounts.filter(a => a.service !== 'yandex' && a.service !== 'spotify');
    if (others.length) {
      const panel = node('div', null, 'panel');
      panel.append(node('h3', 'Прочие сервисы'), ...others.map(a => accountRow(a)));
      root.append(panel);
    }
    root.append(node('h3', 'Токены колонок'),
                node('p', 'Собираются сами из обращений колонок к Яндексу: поставщик музыки берёт токен той колонки, что спросила. Удалённый вернётся при следующем обращении.', 'hint'),
                columnsList());
  }

  // Копилка токенов колонок: строка на колонку — SN, платформа и адрес на момент
  // захвата; «Удалить» убирает токен из копилки (мета в БД состояния тоже).
  function columnsList() {
    if (!state.columns.length) return node('p', 'Пока пусто — появятся сами, как только колонка обратится к Яндексу.', 'hint');
    const box = node('div');
    for (const column of state.columns) {
      const row = node('div', null, 'sa-row');
      const remove = node('button', 'Удалить');
      remove.type = 'button'; remove.className = 'secondary danger';
      remove.onclick = () => api('service-accounts', { op: 'forget', sn: column.sn })
        .then(() => { say('Токен колонки ' + column.sn + ' удалён.'); load() })
        .catch(e => say(e.message, true));
      row.append(node('span', column.sn, 'sa-title'),
                 node('span', [column.platform, column.ip].filter(Boolean).join(' · ') || 'нет данных', 'hint'),
                 remove);
      box.append(row);
    }
    return box;
  }

  function serviceCard(service, title, hint, entry) {
    const box = node('div', null, 'panel');
    box.append(node('h3', title));
    const accounts = state.accounts.filter(a => a.service === service);
    box.append(...accounts.map(a => accountRow(a)),
               node('p', hint, 'hint'), entry(box));
    if (!accounts.length) box.append(node('p', 'Аккаунтов пока нет.', 'hint'));
    return box;
  }

  function accountRow(account) {
    const row = node('div', null, 'sa-row');
    const radio = node('input');
    radio.type = 'radio'; radio.name = 'default-' + account.service;
    radio.checked = !!account.default;
    radio.title = 'Основной аккаунт сервиса: им пользуются поставщики без выбора колонки';
    radio.onchange = () => api('service-accounts', { op: 'default', id: account.id }).then(load).catch(e => { radio.checked = !radio.checked; say(e.message, true) });
    const remove = node('button', 'Удалить');
    remove.type = 'button'; remove.className = 'secondary danger';
    remove.onclick = () => api('service-accounts', { op: 'remove', id: account.id }).then(() => { say('Аккаунт удалён.'); load() }).catch(e => say(e.message, true));
    row.append(radio,
               node('span', account.title || account.id, 'sa-title'),
               node('span', account.login ? '' : 'нет входа', 'hint'),
               remove);
    return row;
  }

  function qrEntry(box) {
    const btn = node('button', 'Войти по QR');
    btn.type = 'button';
    btn.onclick = async () => {
      stopPoll();
      let qr;
      try { qr = (await api('service-accounts', { op: 'qr_start' })).qr; }
      catch (e) { return say(e.message, true) }
      const hint = node('p', 'Отсканируйте код приложением Яндекса на телефоне и подтвердите вход — ждём…', 'hint');
      const image = node('img', null, 'sa-qr');
      image.src = qr; image.alt = 'QR-код входа в Яндекс';
      btn.replaceWith(image, hint);
      state.poll = setInterval(async () => {
        try {
          const result = await api('service-accounts', { op: 'qr_poll' });
          if (result.state !== 'done') return;
          stopPoll();
          say('Аккаунт Яндекса добавлен: ' + (result.title || 'без имени') + (result.default ? ' (основной)' : ''));
          load();
        } catch (e) { stopPoll(); say(e.message, true); load() }
      }, 2000);
    };
    return btn;
  }

  function spotifyEntry(providers) {
    const provider = loginProvider(providers);
    const btn = node('button', 'Войти в Spotify');
    btn.type = 'button';
    if (!provider) { btn.disabled = true; btn.title = 'Поставщик музыки Spotify не установлен'; return btn }
    btn.onclick = async () => {
      stopPoll();
      let url;
      try { url = (await api('providers_login', { kind: provider.kind, id: provider.id, action: 'start' })).url; }
      catch (e) { return say(e.message, true) }
      window.open(url, '_blank', 'noopener');
      const hint = node('p', 'Подтвердите вход в открывшейся вкладке Spotify — ждём…', 'hint');
      btn.replaceWith(hint);
      state.poll = setInterval(async () => {
        try {
          const status = await api('providers_login', { kind: provider.kind, id: provider.id, action: 'status' });
          if (!status.authorized) return;  // страница входа ещё не закрыта
          stopPoll();
          say('Аккаунт Spotify добавлен.');
          load();
        } catch (e) { stopPoll(); say(e.message, true); load() }
      }, 3000);
    };
    return btn;
  }
})();
