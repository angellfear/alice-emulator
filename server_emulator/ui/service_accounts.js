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
      serviceCard('spotify', 'Spotify', spotifyHint(),
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
               typeof hint === 'string' ? node('p', hint, 'hint') : hint, entry(box));
    if (!accounts.length) box.append(node('p', 'Аккаунтов пока нет.', 'hint'));
    return box;
  }

  // Spotify: проигрывание (30-секундные превью) работает без входа и Premium — по ключам
  // Web API у поставщика; кнопка входа только привязывает аккаунт к реестру. Вход делает
  // редирект на один фиксированный локальный адрес — его регистрируют в Spotify Dashboard
  // дословно (API задаёт тем же значением, service_accounts API домен providers).
  const SPOTIFY_REDIRECT = 'https://127.0.0.1/emulator/api/providers_login_callback';

  function spotifyHint() {
    const box = node('div');
    box.append(node('p', 'Проигрывание не требует входа и Premium: колонка играет 30-секундные превью Spotify. Для поиска по реплике задайте ключи Web API (client_id/client_secret) в настройках поставщика музыки.', 'hint'),
               node('p', 'Вход привязывает аккаунт к панели. Личные данные — профиль, плейлисты, лайки, управление плеером — Spotify отдаёт только при Premium у владельца Dashboard-приложения; без него API отвечает отказом, причина сохраняется в строке аккаунта. Вход выполняется в браузере на этой машине и вернётся на локальный адрес: добавьте его в Dashboard Spotify (developer.spotify.com → приложение → Settings → Redirect URIs) и сохраните. Dashboard принимает только https — при возврате браузер покажет предупреждение о самоподписанном сертификате, это ваш сервер: «Перейти на сайт».', 'hint'));
    const uriRow = node('div', null, 'sa-row');
    uriRow.append(node('code', SPOTIFY_REDIRECT));
    const copy = node('button', 'Скопировать');
    copy.type = 'button'; copy.className = 'secondary';
    copy.onclick = () => navigator.clipboard.writeText(SPOTIFY_REDIRECT).then(() => say('Адрес скопирован.'), () => {});
    uriRow.append(copy);
    box.append(uriRow);
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
               node('span', account.note || (account.login ? '' : 'нет входа'), 'hint'),
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
    const row = node('div', null, 'sa-row');
    const provider = loginProvider(providers);
    const btn = node('button', 'Войти в Spotify');
    btn.type = 'button';
    if (!provider) { btn.disabled = true; btn.title = 'Поставщик музыки Spotify не установлен' }
    else btn.onclick = async () => {
      stopPoll();
      let url;
      try { url = (await api('providers_login', { kind: provider.kind, id: provider.id, action: 'start' })).url; }
      catch (e) { return say(e.message, true) }
      window.open(url, '_blank', 'noopener');
      const hint = node('p', 'Подтвердите вход в открывшейся вкладке Spotify — ждём…', 'hint');
      row.replaceWith(hint);
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
    const player = node('button', 'Плеер на колонке');
    player.type = 'button'; player.className = 'secondary';
    player.onclick = stationPlayerDialog;
    row.append(btn, player);
    return row;
  }

  // Диалог «Плеер на колонке»: официальный Spotify APK на колонке с Android —
  // нативный плеер с входом на экране колонки; приложение на телефоне видит её как
  // Connect-устройство (у аккаунта без Premium Connect ограничен Spotify). Управление
  // плеером из панели — Premium у владельца Dashboard-приложения (API плеера закрыт).
  function stationPlayerDialog() {
    const dlg = document.createElement('dialog');
    dlg.className = 'catalog-dialog';
    const form = document.createElement('form');
    form.method = 'dialog';
    dlg.append(form);
    form.append(node('h3', 'Spotify на колонке'));
    const code = text => {
      const c = node('code', text);
      c.style.display = 'block';
      c.style.margin = '4px 0';
      c.style.whiteSpace = 'pre-wrap';
      return c;
    };
    const step = (title, ...parts) => {
      const box = node('p', null, 'hint');
      box.style.margin = '10px 0';
      const b = node('b', title);
      b.style.display = 'block';
      box.append(b, ...parts);
      form.append(box);
    };
    form.append(node('p', 'Колонка с Android становится устройством Spotify Connect: на самой колонке играет нативное приложение Spotify (вход один раз на её экране). Выбор колонки из приложения на телефоне при аккаунте без Premium ограничен Spotify; при Premium колонка работает как обычное Connect-устройство.', 'hint'));
    step('1. Установить Spotify на колонку — на ПК с adb:',
         code('adb connect 192.168.x.x:5555'),
         node('span', 'Официальный бандл com.spotify.music (APKPure, XAPK; адрес колонки — в списке «Токены колонок» ниже; adb-порт закрыт — колонка слушает SSH 22): ', 'hint'),
         code('adb install-multiple base.apk config.arm64_v8a.apk config.ru.apk config.en.apk config.xxhdpi.apk'));
    step('2. Открыть и войти:',
         code('adb shell am start -n com.spotify.music/.MainActivity'),
         node('span', 'На экране часы станции — коснитесь экрана. Войдите в аккаунт Spotify на самой колонке.', 'hint'));
    step('3. Привязать аккаунт в панели:',
         node('span', 'Кнопка «Войти в Spotify». Заранее добавьте redirect-адрес из карточки выше в Dashboard Spotify (только https; при возврате — предупреждение самоподписанного сертификата, жмите «Перейти на сайт»).', 'hint'));
    step('4. Проверить Connect:',
         node('span', 'В приложении Spotify на телефоне включите трек и выберите колонку в списке устройств. Колонки в списке нет — Connect у аккаунта без Premium ограничен Spotify: тогда включайте музыку прямо на экране колонки.', 'hint'));
    form.append(node('p', 'Управление плеером колонки из панели (реплики «включи трек», ваши плейлисты) требует Premium у владельца Dashboard-приложения — без него Spotify закрывает API плеера.', 'hint'));
    const row = node('div', null, 'row');
    const close = node('button', 'Понятно');
    close.type = 'submit';
    row.append(close);
    form.append(row);
    document.body.append(dlg);
    dlg.addEventListener('close', () => dlg.remove());
    dlg.showModal();
  }
})();
