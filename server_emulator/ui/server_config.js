// «Дополнительно → Сервер» (TASK-042): адрес, порты, SSL, сертификат — state/server.json. Сохранение
// проверяет сертификат и порты; применение — перезапуском сервера (под треем — его механизмом, без трея —
// сервер перезапускает себя сам). После перезапуска страница переходит на новый адрес. API — ui/api/server_config.py.
(() => {
  const section = document.getElementById('server');
  if (!section) return;
  let state = null, busy = false;
  const FIELDS = ['host', 'port', 'ssl', 'cert', 'key', 'http_port', 'front', 'front_dns'];

  const make = (tag, props = {}, ...children) => { const n = Object.assign(document.createElement(tag), props); n.append(...children); return n; };
  const call = async (method, body) => {
    const r = await fetch('api/server', { method, headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  };
  const callFront = async (method, body) => {
    const r = await fetch('api/server-front', { method, headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  };
  const apiSettings = async (method, body) => {
    const r = await fetch('api/settings', { method: method || 'GET', headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  };
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  // загрузка PEM в state/certs/ на сервере: в поле подставляется путь, настройки применяются сохранением
  async function uploadCert(kind, file) {
    const form = new FormData();
    form.append('kind', kind);
    form.append('file', file, file.name);
    const r = await fetch('api/server-cert', { method: 'POST', headers: { 'X-Emulator': '1' }, body: form });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  }

  // upload — загрузка файла на сервер (kind: cert | key); ввод пути на компьютере с эмулятором остаётся
  function field(label, input, upload) {
    const row = make('label', {}, label);
    if (upload) {
      const pick = make('input', { type: 'file', hidden: true, accept: '.crt,.pem,.cer,.key' });
      const button = make('button', { type: 'button', className: 'secondary' }, 'Загрузить…');
      pick.onchange = async () => {
        const file = pick.files[0];
        pick.value = '';
        if (!file || button.disabled) return;
        button.disabled = true;
        try {
          const stored = await uploadCert(upload.kind, file);
          input.value = stored.path;
          upload.done(stored);
        } catch (e) { notify(e.message, { error: true }); }
        finally { button.disabled = false; }
      };
      button.onclick = () => pick.click();
      row.append(make('div', { className: 'srv-path' }, input, button, pick));
    } else row.append(input);
    return row;
  }

  function render() {
    if (!state) return;
    const c = state.config, over = new Set(state.overridden || []);
    const inputs = {
      host: make('input', { value: c.host, spellcheck: false }),
      port: make('input', { type: 'number', min: 1, max: 65535, value: c.port }),
      ssl: make('input', { type: 'checkbox', checked: !!c.ssl }),
      cert: make('input', { value: c.cert, spellcheck: false, placeholder: 'certs/server.crt или C:/certs/server.crt' }),
      key: make('input', { value: c.key, spellcheck: false, placeholder: 'пусто — ключ в файле сертификата' }),
      http_port: make('input', { type: 'number', min: 0, max: 65535, value: c.http_port }),
      front: make('input', { type: 'checkbox', checked: c.front !== false }),
      front_dns: make('input', { value: (c.front_dns || []).join(', '), spellcheck: false }),
    };
    const sslRow = make('label', { className: 'srv-check' }, inputs.ssl, make('span', {}, 'SSL на основном порту'));
    const uploaded = s => notify('Загружено: ' + s.path + '. Нажмите «Сохранить и перезапустить», чтобы применить.');
    const certBox = make('div', { className: 'srv-cert' },
      field('Сертификат', inputs.cert, { kind: 'cert', done: s => {
        if (s.has_key) inputs.key.value = '';  // ключ лежит в том же файле
        uploaded(s);
      } }),
      field('Ключ', inputs.key, { kind: 'key', done: uploaded }));
    const frontRow = make('label', { className: 'srv-check' }, inputs.front,
      make('span', {}, 'Фронт TLS (SNI)', make('small', { className: 'hint' },
        ' — режимы для доменов вне сертификата выбираются ниже')));
    const dnsRow = field('DNS туннелей (через запятую)', inputs.front_dns);
    const policyBox = frontPolicyBox();
    const syncSsl = () => {
      certBox.hidden = !inputs.ssl.checked;
      frontRow.hidden = !inputs.ssl.checked;
      dnsRow.hidden = !inputs.ssl.checked;  // DNS нужен только туннелям фронта
      policyBox.hidden = !(inputs.ssl.checked && inputs.front.checked);  // политика — часть блока фронта
    };
    inputs.ssl.onchange = syncSsl; inputs.front.onchange = syncSsl; syncSsl();
    const error = make('p', { className: 'sc-err', hidden: true, role: 'alert' });
    const save = make('button', { type: 'submit' }, 'Сохранить и перезапустить');
    const listeners = (state.listeners || []).map(l => (l.ok ? '' : '✕ ') + l.scheme + '://' + l.host + ':' + l.port + (l.ok ? '' : ' — ' + l.error)).join(' · ');
    const version = state.version ? 'Версия: ' + state.version.version + (state.version.commit ? ' · ' + state.version.commit : '') + (state.version.dirty ? ' (есть правки)' : '') : '';
    const form = make('form', { className: 'panel srv-form' },
      field('Адрес', inputs.host), field('Порт', inputs.port), sslRow, certBox,
      field('HTTP-порт (0 — выключен)', inputs.http_port), frontRow, dnsRow, policyBox,
      error, make('div', { className: 'actions' }, save));
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      error.hidden = true;
      const value = Object.fromEntries(FIELDS.map(k => [k, k === 'ssl' ? inputs.ssl.checked : k === 'front' ? inputs.front.checked : ['port', 'http_port'].includes(k) ? Number(inputs[k].value) : inputs[k].value.trim()]));
      busy = true; save.disabled = true;
      try {
        const saved = await call('PUT', value);
        const target = panelUrl(saved.effective || saved.config);
        save.textContent = 'Перезапуск…';
        await call('POST', { action: 'restart' });
        await waitFor(target, saved.warnings || []);
      } catch (e) {
        error.textContent = e.message; error.hidden = false;
        save.textContent = 'Сохранить и перезапустить';
      } finally { busy = false; save.disabled = false; }
    };
    const notes = [];
    if (over.size) notes.push('Перекрыто командной строкой: ' + [...over].join(', ') + '.');
    if ((state.errors || []).length) notes.push('Ошибки запуска: ' + state.errors.join('; ') + '.');
    section.replaceChildren(make('h2', {}, 'Сервер'),
      make('p', { className: 'hint' }, 'Колонки подключаются к основному порту 443 по TLS и к HTTP-порту 80 (quasar.yandex.net); сертификат должен быть тем, которому доверяют колонки. Путь к файлу указывается на компьютере с эмулятором либо файл загружается кнопкой — он сохранится в папке данных (certs/). Изменения применяются перезапуском сервера; если с новыми настройками он не поднимется, вернутся прежние.'),
      make('p', { className: 'hint' }, [version, 'Сейчас: ' + (listeners || '—') + (notes.length ? ' ' + notes.join(' ') : '')].filter(Boolean).join(' · ')),
      form, selfUpdatePanel());
  }

  // Политика доменов вне сертификата (TASK-093): три режима; для «по листу» — поле (1 домен на строку)
  // и список зафиксированных с кнопкой «Добавить». Применяется сразу, без перезапуска сервера —
  // фронт читает server.json на каждое соединение. API — api/server-front; API нет (или старый
  // сервер) — блок не показывается.
  function frontPolicyBox() {
    const fp = state.front_policy;
    if (!fp) return make('div', { hidden: true });
    const modes = [['tunnel', 'Пропускать прозрачно', 'слепой туннель к настоящему серверу — как сейчас'],
                   ['allowlist', 'Пропускать только по листу', 'туннель доменам из списка, остальные отклонять'],
                   ['block', 'Блокировать', 'отклонять все домены, которых нет в сертификате']];
    const radio = {};
    const rows = modes.map(([value, label, hint]) => {
      const input = make('input', { type: 'radio', name: 'front-uncovered', value: value,
                                    checked: (fp.front_uncovered || 'tunnel') === value });
      radio[value] = input;
      return make('label', { className: 'srv-check' }, input,
        make('span', {}, label, make('small', { className: 'hint' }, ' — ' + hint)));
    });
    const listArea = make('textarea', { rows: 6, spellcheck: false, placeholder: '*.strm.yandex.net',
                                        value: (fp.front_allow || []).join('\n') });
    const observed = make('div', { className: 'srv-observed' });
    function addLine(host) {
      const lines = listArea.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
      if (!lines.includes(host)) lines.push(host);
      listArea.value = lines.join('\n');
    }
    function renderObserved() {
      const seen = fp.observed || [];
      observed.replaceChildren(...(seen.length
        ? seen.map(item => make('div', { className: 'srv-observed-row' },
            make('span', {}, item.host + ' ', make('small', { className: 'hint' }, '×' + item.count)),
            make('button', { type: 'button', className: 'secondary', onclick: () => addLine(item.host) }, 'Добавить')))
        : [make('p', { className: 'hint' }, 'Домены, которые фронт видел после запуска сервера, появятся здесь.')]));
    }
    renderObserved();
    const listBox = make('div', {},
      make('label', {}, 'Домены вне сертификата (1 домен на строку)', listArea), observed);
    const syncMode = () => { listBox.hidden = !radio.allowlist.checked; };
    modes.forEach(([value]) => { radio[value].onchange = syncMode; });
    syncMode();
    const error = make('p', { className: 'sc-err', hidden: true, role: 'alert' });
    const apply = make('button', { type: 'button' }, 'Применить');
    apply.onclick = async () => {
      if (busy) return;
      busy = true; apply.disabled = true; error.hidden = true;
      try {
        const mode = modes.find(([value]) => radio[value].checked)[0];
        const saved = await callFront('PUT', { front_uncovered: mode,
          front_allow: listArea.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean) });
        fp.front_uncovered = saved.front_uncovered;
        fp.front_allow = saved.front_allow;
        notify('Применено без перезапуска');
      } catch (e) { error.textContent = e.message; error.hidden = false; }
      finally { busy = false; apply.disabled = false; }
    };
    return make('div', { className: 'panel' },
      make('h3', {}, 'Домены вне сертификата'),
      make('p', { className: 'hint' }, 'Как фронт поступает с доменами, отсутствующими в сертификате. Применяется сразу, без перезапуска сервера; уже открытые соединения не затрагиваются.'),
      ...rows, listBox, error, make('div', { className: 'actions' }, apply));
  }

  // Обновление (TASK-040): настройки проверки релизов — настройки эмулятора, а не server.json,
  // поэтому сохранение отдельно: GET api/settings, правка блока, PUT. В форме — только галочка
  // автообновления и канал, остальные поля остаются как сохранены (в дефолтах); кнопка-индикатор
  // обновления от галочки не зависит — она всегда ручная.
  function selfUpdatePanel() {
    const su = state.self_update || {};
    const inputs = {
      enabled: make('input', { type: 'checkbox', checked: su.enabled !== false }),
      channel: make('select', {}, make('option', { value: 'stable' }, 'стабильные'), make('option', { value: 'beta' }, 'бета (пререлизы)')),
    };
    inputs.channel.value = su.channel || 'stable';
    const error = make('p', { className: 'sc-err', hidden: true, role: 'alert' });
    const save = make('button', { type: 'submit' }, 'Сохранить');
    const form = make('form', { className: 'panel' },
      make('h3', {}, 'Обновление'),
      make('label', { className: 'srv-check' }, inputs.enabled, make('span', {}, 'Включить автообновление')),
      make('label', {}, 'Канал', inputs.channel),
      error, make('div', { className: 'actions' }, save));
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      error.hidden = true;
      busy = true; save.disabled = true;
      try {
        const settings = await apiSettings();
        settings.self_update = { ...settings.self_update, enabled: inputs.enabled.checked, channel: inputs.channel.value };
        await apiSettings('PUT', settings);
        notify('Настройки обновления сохранены');
      } catch (e) { error.textContent = e.message; error.hidden = false; }
      finally { busy = false; save.disabled = false; }
    };
    return form;
  }

  // адрес панели после перезапуска: сервер слушает все адреса (0.0.0.0 / ::) — остаёмся на хосте, по которому
  // открыта панель (адрес в сети, имя компьютера), меняются только схема и порт; та же схема, если её слушатель остался
  function panelUrl(c) {
    const wildcard = ['0.0.0.0', '::', ''].includes(c.host);
    const host = wildcard ? location.hostname : c.host;
    let scheme, port;
    if (location.protocol === 'http:' && c.http_port) [scheme, port] = ['http', c.http_port];
    else if (location.protocol === 'https:' && c.ssl) [scheme, port] = ['https', c.port];
    else if (c.http_port) [scheme, port] = ['http', c.http_port];
    else [scheme, port] = [c.ssl ? 'https' : 'http', c.port];
    const url = new URL(location.href);
    url.protocol = scheme + ':';
    url.hostname = host.includes(':') && !host.startsWith('[') ? '[' + host + ']' : host;
    url.port = String(port);  // стандартный порт схемы URL убирает сам
    return url.href;
  }

  // ждём сервер на новом адресе: тот же адрес — опрос и перезагрузка, другой — переход (CORS не даёт опросить)
  async function waitFor(target, warnings) {
    const same = new URL(target).origin === location.origin;
    notify('Перезапуск сервера…' + (warnings.length ? ' ' + warnings.join('; ') : ''), { timeout: 15000 });  // рестарт дольше обычного тоста
    await sleep(1500);
    if (!same) { location.href = target; return; }
    for (let i = 0; i < 40; i++) {
      try { const r = await fetch('api/server', { headers: { 'X-Emulator': '1' }, cache: 'no-store' }); if (r.ok) { location.reload(); return; } } catch (e) { /* ещё не поднялся */ }
      await sleep(1000);
    }
    throw Error('Сервер не ответил за 40 секунд — проверьте журнал сервера');
  }

  async function load() {
    try {
      state = await call('GET');
      try { state.front_policy = await callFront('GET'); } catch (e) { /* блок политики не покажется */ }
      try { state.self_update = (await apiSettings()).self_update; } catch (e) { /* блок просто не покажется */ }
      render();
    } catch (e) { section.replaceChildren(make('h2', {}, 'Сервер'), make('p', { className: 'sc-err' }, e.message)); }
  }
  load();
})();
