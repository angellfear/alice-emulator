// «Дополнительно → Сервер» (TASK-042): адрес, порты, SSL, сертификат — state/server.json. Сохранение
// проверяет сертификат и порты; применение — перезапуском сервера (под треем — его механизмом, без трея —
// сервер перезапускает себя сам). После перезапуска страница переходит на новый адрес. API — ui/api/server_config.py.
(() => {
  const section = document.getElementById('server');
  if (!section) return;
  let state = null, busy = false;
  const FIELDS = ['host', 'port', 'ssl', 'cert', 'key', 'http_port'];

  const make = (tag, props = {}, ...children) => { const n = Object.assign(document.createElement(tag), props); n.append(...children); return n; };
  const call = async (method, body) => {
    const r = await fetch('api/server', { method, headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) });
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

  function field(label, input, chooser) {
    const row = make('label', {}, label);
    if (chooser) {
      // браузер не отдаёт путь к файлу — выбор только подставляет имя, основное — ввод пути
      const pick = make('input', { type: 'file', hidden: true });
      pick.onchange = () => { if (pick.files[0]) input.value = pick.files[0].name; };
      const button = make('button', { type: 'button', className: 'secondary' }, 'Файл…');
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
      cert: make('input', { value: c.cert, spellcheck: false, placeholder: 'C:/certs/server.crt' }),
      key: make('input', { value: c.key, spellcheck: false, placeholder: 'пусто — ключ в файле сертификата' }),
      http_port: make('input', { type: 'number', min: 0, max: 65535, value: c.http_port }),
    };
    const sslRow = make('label', { className: 'srv-check' }, inputs.ssl, make('span', {}, 'SSL на основном порту'));
    const certBox = make('div', { className: 'srv-cert' }, field('Сертификат', inputs.cert, true), field('Ключ', inputs.key, true));
    const syncSsl = () => { certBox.hidden = !inputs.ssl.checked; };
    inputs.ssl.onchange = syncSsl; syncSsl();
    const error = make('p', { className: 'sc-err', hidden: true, role: 'alert' });
    const save = make('button', { type: 'submit' }, 'Сохранить и перезапустить');
    const listeners = (state.listeners || []).map(l => (l.ok ? '' : '✕ ') + l.scheme + '://' + l.host + ':' + l.port + (l.ok ? '' : ' — ' + l.error)).join(' · ');
    const version = state.version ? 'Версия: ' + state.version.version + (state.version.commit ? ' · ' + state.version.commit : '') + (state.version.dirty ? ' (есть правки)' : '') : '';
    const form = make('form', { className: 'panel srv-form' },
      field('Адрес', inputs.host), field('Порт', inputs.port), sslRow, certBox, field('HTTP-порт (0 — выключен)', inputs.http_port),
      error, make('div', { className: 'actions' }, save));
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      error.hidden = true;
      const value = Object.fromEntries(FIELDS.map(k => [k, k === 'ssl' ? inputs.ssl.checked : ['port', 'http_port'].includes(k) ? Number(inputs[k].value) : inputs[k].value.trim()]));
      busy = true; save.disabled = true;
      try {
        const saved = await call('PUT', value);
        const target = saved.url + '/emulator/';
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
      make('p', { className: 'hint' }, 'Колонки подключаются к основному порту 443 по TLS и к HTTP-порту 80 (quasar.local); сертификат должен быть тем, которому доверяют колонки. Изменения применяются перезапуском сервера; если с новыми настройками он не поднимется, вернутся прежние.'),
      make('p', { className: 'hint' }, [version, 'Сейчас: ' + (listeners || '—') + (notes.length ? ' ' + notes.join(' ') : '')].filter(Boolean).join(' · ')),
      form, selfUpdatePanel());
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
      try { state.self_update = (await apiSettings()).self_update; } catch (e) { /* блок просто не покажется */ }
      render();
    } catch (e) { section.replaceChildren(make('h2', {}, 'Сервер'), make('p', { className: 'sc-err' }, e.message)); }
  }
  load();
})();
