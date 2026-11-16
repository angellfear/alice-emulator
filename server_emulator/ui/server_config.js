// «Дополнительно → Сервер»: адрес, порты, SSL, сертификат — state/server.json. Сохранение
// проверяет сертификат и порты; применение — перезапуском сервера (под треем — его механизмом, без трея —
// сервер перезапускает себя сам). После перезапуска страница переходит на новый адрес. API — ui/api/server_config.py.
(() => {
  const section = document.getElementById('server');
  if (!section) return;
  let state = null, busy = false;
  const FIELDS = ['host', 'port', 'ssl', 'cert', 'key', 'http_port', 'front', 'front_dns', 'local_domain', 'speech_dir'];

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
      local_domain: make('input', { value: c.local_domain || '', spellcheck: false, placeholder: 'пусто — конфиг колонок не меняется' }),
      speech_dir: make('input', { value: c.speech_dir || '', spellcheck: false, placeholder: 'пусто — data/speech внутри проекта' }),
    };
    const sslRow = make('label', { className: 'srv-check' }, inputs.ssl, make('span', {}, 'SSL на основном порту'));
    const uploaded = s => notify('Загружено: ' + s.path + '. Нажмите «Сохранить и перезапустить», чтобы применить.');
    const serveBox = serveDomainsPanel();
    const certBox = make('div', { className: 'srv-cert' },
      field('Сертификат', inputs.cert, { kind: 'cert', done: s => {
        if (s.has_key) inputs.key.value = '';  // ключ лежит в том же файле
        uploaded(s);
      } }),
      field('Ключ', inputs.key, { kind: 'key', done: uploaded }), serveBox);
    const frontRow = make('label', { className: 'srv-check' }, inputs.front,
      make('span', {}, 'Фронт TLS (SNI)', make('small', { className: 'hint' },
        ' — режимы для доменов вне сертификата выбираются ниже')));
    const dnsRow = field('DNS туннелей (через запятую)', inputs.front_dns);
    const policyBox = frontPolicyBox(serveBox);
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
    const speechDirRow = field('Папка голосовых пакетов', inputs.speech_dir);
    speechDirRow.append(make('small', { className: 'hint' },
      ' — venv и модели ASR/TTS/LLM-пакетов; относительный путь — от корня проекта. Смена не переносит и не удаляет файлы по прежнему пути.'));
    const form = make('form', { className: 'panel srv-form' },
      field('Адрес', inputs.host), field('Порт', inputs.port), field('Локальный домен', inputs.local_domain), sslRow, certBox,
      field('HTTP-порт (0 — выключен)', inputs.http_port), frontRow, dnsRow, policyBox, speechDirRow,
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
      (() => {
        const note = make('details', { className: 'hint' });
        note.append(make('summary', null, 'Сертификат и порты: колонки доверяют сертификату основного порта; изменения применяются перезапуском.'));
        note.append(make('p', { className: 'hint' }, 'Колонки подключаются к основному порту 443 по TLS и к HTTP-порту 80 (quasar.yandex.net); сертификат должен быть тем, которому доверяют колонки. Путь к файлу указывается на компьютере с эмулятором либо файл загружается кнопкой — он сохранится в папке данных (certs/). Если с новыми настройками сервер не поднимется, вернутся прежние.'));
        return note;
      })(),
      make('p', { className: 'hint' }, [version, 'Сейчас: ' + (listeners || '—') + (notes.length ? ' ' + notes.join(' ') : '')].filter(Boolean).join(' · ')),
      form, selfUpdatePanel(), journalPanel(), protocolPanel(), appearancePanel());
  }

  // Обслуживаемые домены (serve_domains из GET api/server, покрытие — SAN текущего сертификата):
  // обязательные всегда включены, дополнительные выключаются, пользовательский список — textarea.
  // Правка применяется без перезапуска через api/server-front. Старый сервер без поля — блок не показывается.
  function serveDomainsPanel() {
    const list = state.serve_domains;
    if (!Array.isArray(list)) return make('div', { hidden: true });
    const box = make('div', { className: 'srv-serve' });
    const rows = list.map(d => {
      const input = make('input', { type: 'checkbox', checked: d.enabled, disabled: d.mandatory });
      const mark = make('span', { className: 'srv-serve-mark ' + (d.covered ? 'ok' : 'err') }, d.covered ? '✓' : '✕');
      const row = make('label', { className: 'srv-check srv-serve-row' }, input, mark,
        make('span', {}, d.host, d.covered ? null : make('small', { className: 'hint sc-err' }, ' — нет в сертификате')));
      return { data: d, input, row };
    });
    const mandatory = rows.filter(r => r.data.mandatory), optional = rows.filter(r => !r.data.mandatory);
    const extraArea = make('textarea', { rows: 4, spellcheck: false, placeholder: 'avatars.yandex.net' });
    const extras = make('div', { className: 'srv-observed' });
    function renderExtras() {
      const fp = state.front_policy || {};
      const coverage = new Map((fp.extra || []).map(e => [e.host, e.covered]));
      extras.replaceChildren(...(fp.serve_extra || []).map(host => make('div', { className: 'srv-observed-row' },
        make('span', { className: coverage.get(host) === false ? 'sc-err' : '' },
          (coverage.has(host) ? (coverage.get(host) ? '✓ ' : '✕ ') : '') + host,
          coverage.get(host) === false ? make('small', { className: 'hint' }, ' — нет в сертификате') : null))));
    }
    async function refreshFront() {
      state.front_policy = await callFront('GET');
      renderExtras();
    }
    renderExtras();  // state.front_policy уже загружен в load()
    const error = make('p', { className: 'sc-err', hidden: true, role: 'alert' });
    const apply = make('button', { type: 'button' }, 'Применить');
    apply.onclick = async () => {
      if (busy) return;
      busy = true; apply.disabled = true; error.hidden = true;
      try {
        await callFront('PUT', { serve_off: optional.filter(r => !r.input.checked).map(r => r.data.host),
                                 serve_extra: extraArea.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean) });
        await refreshFront();
        notify('Применено без перезапуска');
      } catch (e) { error.textContent = e.message; error.hidden = false; }
      finally { busy = false; apply.disabled = false; }
    };
    // Проверка DNS: системный резолвер машины с сервером → все ли обслуживаемые домены
    // указывают на этот компьютер (эндпоинт api/server-dns-check). Read-only.
    const dns = make('div', { className: 'srv-observed' });
    const dnsBtn = make('button', { type: 'button', className: 'secondary' }, 'Проверить DNS');
    dnsBtn.onclick = async () => {
      if (busy) return;
      busy = true; dnsBtn.disabled = true; error.hidden = true;
      dns.replaceChildren(make('p', { className: 'hint' }, 'Проверяю…'));
      try {
        const r = await fetch('api/server-dns-check', { method: 'POST', headers: { 'X-Emulator': '1' } });
        const v = await r.json().catch(() => ({}));
        if (!r.ok) throw Error(v.error || r.statusText);
        const good = v.items.filter(i => i.ok).length;
        dns.replaceChildren(
          make('p', { className: 'hint' }, 'Системный DNS машины с сервером; адреса сервера: '
            + v.local.join(', ') + '. На эмулятор указывают ' + good + ' из ' + v.items.length + '.'),
          ...v.items.map(i => make('div', { className: 'srv-observed-row' },
            make('span', { className: i.ok ? 'srv-serve-mark ok' : 'sc-err' },
              (i.ok ? '✓ ' : '✕ ') + i.host + ' → '
              + (i.ips.length ? i.ips.join(', ') + (i.ok ? '' : ' (мимо)') : 'не разрешается')))));
      } catch (e) { error.textContent = e.message; error.hidden = false; dns.replaceChildren(); }
      finally { busy = false; dnsBtn.disabled = false; }
    };
    box.append(
      make('p', { className: 'hint' }, 'Какие домены фронт отдаёт эмулятору, а не туннелирует настоящему Яндексу. Правка применяется без перезапуска; домен без покрытия сертификатом соединением колонки не обслуживается.'),
      ...mandatory.map(r => r.row), ...optional.map(r => r.row),
      make('label', {}, 'Пользовательский список (1 домен на строку, без *)', extraArea),
      extras, error, make('div', { className: 'actions' }, dnsBtn, apply), dns);
    return Object.assign(box, { addExtra: host => {
      const lines = extraArea.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
      if (!lines.includes(host)) lines.push(host);
      extraArea.value = lines.join('\n');
    } });
  }

  // Политика для необслуживаемых доменов: два режима и список замеченных с кнопкой «Добавить» —
  // домен уходит в пользовательский список обслуживаемых (serveBox). Применяется сразу,
  // без перезапуска сервера — фронт читает server.json на каждое соединение. API — api/server-front;
  // API нет (или старый сервер) — блок не показывается.
  function frontPolicyBox(serveBox) {
    const fp = state.front_policy;
    if (!fp) return make('div', { hidden: true });
    const modes = [['tunnel', 'Пропускать прозрачно', 'слепой туннель к настоящему серверу'],
                   ['block', 'Блокировать', 'отклонять все домены, которых нет в сертификате']];
    const radio = {};
    const rows = modes.map(([value, label, hint]) => {
      const input = make('input', { type: 'radio', name: 'front-uncovered', value: value,
                                    checked: (fp.front_uncovered || 'tunnel') === value });
      radio[value] = input;
      return make('label', { className: 'srv-check' }, input,
        make('span', {}, label, make('small', { className: 'hint' }, ' — ' + hint)));
    });
    const observed = make('div', { className: 'srv-observed' });
    function renderObserved() {
      const seen = fp.observed || [];
      observed.replaceChildren(...(seen.length
        ? seen.map(item => make('div', { className: 'srv-observed-row' },
            make('span', {}, item.host + ' ', make('small', { className: 'hint' }, '×' + item.count)),
            make('button', { type: 'button', className: 'secondary', onclick: () => {
              if (serveBox) serveBox.addExtra(item.host);
              notify('Добавлено в пользовательский список обслуживаемых — не забудьте «Применить»');
            } }, 'Добавить')))
        : [make('p', { className: 'hint' }, 'Домены, которые фронт не обслуживал после запуска сервера, появятся здесь.')]));
    }
    renderObserved();
    const error = make('p', { className: 'sc-err', hidden: true, role: 'alert' });
    const apply = make('button', { type: 'button' }, 'Применить');
    apply.onclick = async () => {
      if (busy) return;
      busy = true; apply.disabled = true; error.hidden = true;
      try {
        const mode = modes.find(([value]) => radio[value].checked)[0];
        const saved = await callFront('PUT', { front_uncovered: mode });
        fp.front_uncovered = saved.front_uncovered;
        notify('Применено без перезапуска');
      } catch (e) { error.textContent = e.message; error.hidden = false; }
      finally { busy = false; apply.disabled = false; }
    };
    return make('div', { className: 'panel' },
      make('h3', {}, 'Домены вне сертификата'),
      make('p', { className: 'hint' }, 'Как фронт поступает с доменами, которые не обслуживает. Применяется сразу, без перезапуска сервера; уже открытые соединения не затрагиваются.'),
      ...rows, observed, error, make('div', { className: 'actions' }, apply));
  }

  // Обновление: настройки проверки релизов — настройки эмулятора, а не server.json,
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

  // Журнал трафика: сколько записей хранить на пару «устройство + тип сообщения». Настройка
  // upstream.journal.group_limit (GET/PUT api/settings), применяется сразу, без перезапуска.
  function journalPanel() {
    const journal = state.journal || {};
    const input = make('input', { type: 'number', min: '1', step: '1', value: String(journal.group_limit || 200) });
    const asrInput = make('input', { type: 'number', min: '1', step: '1', value: String(journal.asr_group_limit || 1000) });
    const error = make('p', { className: 'sc-err', hidden: true, role: 'alert' });
    const save = make('button', { type: 'submit' }, 'Сохранить');
    const form = make('form', { className: 'panel' },
      make('h3', {}, 'Журнал трафика'),
      make('p', { className: 'hint' }, 'Старые записи сверх лимита удаляются при поступлении новых; счётчик всех случаев хранится отдельно. Результаты распознавания речи идут десятками на реплику — у них свой лимит.'),
      make('label', {}, 'Записей на тип сообщения', input),
      make('label', {}, 'Записей на результаты распознавания речи', asrInput),
      error, make('div', { className: 'actions' }, save));
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      error.hidden = true;
      busy = true; save.disabled = true;
      try {
        const value = Math.floor(Number(input.value)), asrValue = Math.floor(Number(asrInput.value));
        if (!(value >= 1) || !(asrValue >= 1)) throw new Error('Нужно целое число от 1');
        const settings = await apiSettings();
        settings.upstream = { ...settings.upstream, journal: { ...(settings.upstream || {}).journal, group_limit: value, asr_group_limit: asrValue } };
        await apiSettings('PUT', settings);
        state.journal = settings.upstream.journal;
        notify('Настройки журнала сохранены');
      } catch (e) { error.textContent = e.message; error.hidden = false; }
      finally { busy = false; save.disabled = false; }
    };
    return form;
  }

  // Alice Pro: служебные поля EffectiveSettings/TrialState в каждом AliceResponse — как у
  // quasar-server (settings.alice2_settings, читается живьём, выключено по умолчанию).
  function protocolPanel() {
    const check = make('input', { type: 'checkbox', checked: !!state.alice2_settings });
    const error = make('p', { className: 'sc-err', hidden: true, role: 'alert' });
    const save = make('button', { type: 'submit' }, 'Сохранить');
    const form = make('form', { className: 'panel' },
      make('h3', {}, 'Протокол'),
      make('label', { className: 'srv-check' }, check,
        make('span', {}, 'Alice Pro: поля EffectiveSettings/TrialState в каждом ответе')),
      make('p', { className: 'hint' }, 'Выключено по умолчанию — станции без Alice Pro получают ответы прежнего формата. Применяется сразу, без перезапуска.'),
      error, make('div', { className: 'actions' }, save));
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      error.hidden = true;
      busy = true; save.disabled = true;
      try {
        const settings = await apiSettings();
        settings.alice2_settings = check.checked;
        await apiSettings('PUT', settings);
        state.alice2_settings = check.checked;
        notify('Настройка протокола сохранена');
      } catch (e) { error.textContent = e.message; error.hidden = false; }
      finally { busy = false; save.disabled = false; }
    };
    return form;
  }

  // Оформление: тема панели (theme.js, localStorage) — тот же выбор, что переключатель
  // в шапке; радио здесь только синхронизируется с ним (делегирование событий в theme.js),
  // своего состояния и обращения к серверу нет.
  function appearancePanel() {
    const current = window.AppTheme ? window.AppTheme.current() : 'auto';
    const options = [['light', 'Светлая'], ['dark', 'Тёмная'], ['auto', 'Системная (авто)']];
    const rows = options.map(([value, label]) => {
      const input = make('input', { type: 'radio', name: 'appearance-theme', checked: value === current });
      input.dataset.themeChoice = value;
      return make('label', { className: 'srv-check' }, input, make('span', {}, label));
    });
    return make('div', { className: 'panel' },
      make('h3', {}, 'Оформление'),
      make('p', { className: 'hint' }, 'Тема панели — тот же выбор, что переключатель в правом верхнем углу шапки.'),
      ...rows);
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
      try { const s = await apiSettings(); state.alice2_settings = s.alice2_settings; state.journal = (s.upstream || {}).journal; } catch (e) { /* блок просто не покажется */ }
      render();
    } catch (e) { section.replaceChildren(make('h2', {}, 'Сервер'), make('p', { className: 'sc-err' }, e.message)); }
  }
  load();
})();
