/* Голосовой стек: карточка на сервис (ASR / TTS / LLM).
   Список — пресеты (адаптер + настройки) и пакеты (установка сервера на этот ПК); под ним сразу
   настройки выбранного. «Добавить» → выбор адаптера → поля → новый пресет.
   Поля описывает адаптер/пакет: password (+ link «Получить ключ», help), required, group: advanced
   («Дополнительно»), options_from: voices (список голосов поставщика), when. Голос TTS — режимы
   voice_modes: preset / baked / clone (переключатель только из объявленных).
   Адаптеры — server_emulator/voice_stack/adapters/*.py, пакеты — voice_stack/packages/*.json,
   API — ui/api/voice_stack.py. */
(() => {
  const host = document.getElementById('voice-stack-cards');
  if (!host) return;
  const llmHost = document.getElementById('llm-cards');  // 'Модель (LLM)' живёт в подвкладке «Интеллект → Модель»
  const hosts = () => [host, llmHost].filter(Boolean);

  const SERVICES = [['asr', 'Распознавание (ASR)'], ['tts', 'Синтез (TTS)'], ['llm', 'Модель (LLM)']];
  const STATE = { absent: 'Не установлен', installing: 'Установка', running: 'Работает', starting: 'Запускается…', stopped: 'Остановлен', error: 'Ошибка установки', unsupported: 'Не поддерживается на этой ОС' };
  const MODES = { preset: 'Встроенный голос', baked: 'Свой голос (запечённый)', clone: 'Клонирование по эталону' };
  const KEEP = '__KEEP__';
  const make = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n };
  const say = (text, error = false) => { notify(text, { error }); };
  const api = async (action, method = 'GET', value) => {
    const r = await fetch('api/' + action, { method, headers: { 'Content-Type': 'application/json', 'X-Emulator': '1' }, body: value === undefined ? undefined : JSON.stringify(value) });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    // TASK-088: применение источника, правка применённого пресета и параметров пакета пишут
    // общие настройки на сервере — перечитываем их, иначе сохранение другой вкладки пришлёт
    // устаревшую копию (сервер сверит ревизию, но форма edits потеряются)
    if (method !== 'GET' && ['voice-stack-apply', 'voice-stack-preset', 'voice-stack-package'].includes(action.split('?')[0]))
      window.refreshSettings?.().catch(() => {});
    return v;
  };

  let data = null, poll = null, busy = false;
  const chosen = {};  // service → "p:<пресет>" | "k:<пакет>"
  const adding = {};  // service → id адаптера в режиме «Добавить» (null — обычный режим)
  const drafts = {};  // ключ формы → {title, values}: введённое, но не сохранённое
  const openAdv = new Set();  // ключи форм с раскрытым «Дополнительно» (только в этой странице)
  const voiceLists = {};  // ключ формы + поле → список голосов, полученный у поставщика
  const engineSeen = {};  // ключ формы + поле → движок (engine), для которого показан список голосов

  async function work(fn) {
    if (busy) return; busy = true;
    hosts().forEach(h => h.querySelectorAll('button,select,input,textarea').forEach(b => b.disabled = true));
    try { await fn() } catch (e) { say(e.message, true) } finally { busy = false; render(true) }
  }
  const button = (text, cls, fn) => { const b = make('button', cls, text); b.type = 'button'; b.onclick = () => work(fn); return b };

  // окно подтверждения в странице (не confirm())
  function ask(text, okText, danger = true) {
    return new Promise(resolve => {
      const dialog = make('dialog', 'catalog-dialog');
      const done = value => { dialog.close(); dialog.remove(); resolve(value) };
      const no = make('button', 'secondary', 'Отмена'); no.type = 'button'; no.onclick = () => done(false);
      const yes = make('button', danger ? 'danger' : null, okText); yes.type = 'button'; yes.onclick = () => done(true);
      const row = make('div', 'row'); row.append(no, yes);
      dialog.append(make('p', null, text), row);
      dialog.addEventListener('cancel', e => { e.preventDefault(); done(false) });
      document.body.append(dialog); dialog.showModal(); no.focus();
    });
  }

  const refs = s => [...data[s].presets.map(p => 'p:' + p.id), ...data[s].packages.map(p => 'k:' + p.id)];

  const statusKey = d => JSON.stringify(SERVICES.map(([s]) => d[s].packages.map(p => [p.id, p.state.status, p.state.outdated, p.bake?.baked])));

  async function load(background = false) {
    clearTimeout(poll); poll = null;
    const before = data && statusKey(data);
    data = await api('voice-stack');
    for (const [s] of SERVICES) if (!refs(s).includes(chosen[s])) chosen[s] = data[s].active || refs(s)[0] || '';
    if (!background || before !== statusKey(data)) render(true);
    else hosts().forEach(h => h.querySelectorAll('.vs-local[data-pkg]').forEach(box => {  // опрос: только блок статуса, форма не трогается
      const [s, id] = box.dataset.pkg.split('/'), pkg = data[s].packages.find(p => p.id === id);
      if (pkg) box.replaceWith(statusBox(s, pkg));
    }));
    const pending = SERVICES.some(([s]) => data[s].packages.some(p => ['installing', 'starting'].includes(p.state.status)));
    if (pending && ['#voice-stack', '#llm'].includes(location.hash)) poll = setTimeout(() => load(true).catch(e => say(e.message, true)), 1500);
  }

  function draft(key, init) { return drafts[key] || (drafts[key] = init()) }

  // --- поля ----------------------------------------------------------------------------------
  // ctx: {key, service, adapter, id, pkg, files, modes, bake, persist} — владелец формы (пресет, пакет или «Добавить»)
  const isEmpty = (spec, values, ctx) => {
    if (spec.type === 'file') return !ctx.files?.[spec.key] || !!ctx.files[spec.key].error;
    const v = values[spec.key];
    return v === undefined || v === null || String(v).trim() === '';
  };
  const shown = (f, values) => Object.entries(f.when || {}).every(([k, v]) => String(values[k]) === String(v));
  const missing = (fields, values, ctx) => fields.filter(f => f.required && shown(f, values) && isEmpty(f, values, ctx));
  function checkRequired(fields, values, ctx, d) {
    const miss = missing(fields, values, ctx);
    if (!miss.length) return;
    if (d) d.touched = true;
    throw Error('Заполните: ' + miss.map(f => f.label).join(', '));
  }

  function labelRow(spec) {
    const row = make('span', 'vs-label');
    row.append(make('span', null, spec.label + (spec.required ? ' *' : '')));
    if (spec.link) {
      const a = make('a', 'vs-link', 'Получить ключ'); a.href = spec.link; a.target = '_blank'; a.rel = 'noopener noreferrer';
      row.append(a);
    }
    return row;
  }

  // список голосов поставщика: select + «Обновить список»; список зависит от движка (поле engine) —
  // при его смене список запрашивается заново (тихо: без ключа остаётся список по умолчанию)
  function voiceSelect(spec, values, ctx, el) {
    const cacheKey = ctx.key + '/' + spec.key;
    const fill = options => {
      el.replaceChildren();
      const list = (options || []).map(o => typeof o === 'object' ? o : { value: o, label: o });
      if (values[spec.key] && !list.some(o => String(o.value) === String(values[spec.key]))) list.unshift({ value: values[spec.key], label: String(values[spec.key]) });
      for (const o of list) el.append(new Option(o.label, o.value));
      el.value = values[spec.key] ?? '';
    };
    const request = async refresh => {
      const r = await api('voice-stack-voices', 'POST', { service: ctx.service, adapter: ctx.adapter, id: ctx.id || undefined, values, refresh });
      voiceLists[cacheKey] = r.options; fill(r.options);
      return r;
    };
    fill(voiceLists[cacheKey] || spec.options);
    const seen = engineSeen[cacheKey]; engineSeen[cacheKey] = values.engine;
    if (seen !== undefined && seen !== values.engine) request(false).then(() => render(true)).catch(() => {});
    const refresh = make('button', 'secondary vs-refresh', 'Обновить список'); refresh.type = 'button';
    refresh.onclick = async () => {
      refresh.disabled = true;
      try {
        const r = await request(true);
        if (r.error) say(r.error, true); else say('Голосов: ' + r.options.length);
      } catch (e) { say(e.message, true) } finally { refresh.disabled = false }
    };
    return refresh;
  }

  function input(spec, values, ctx = {}) {
    const label = make('label', 'vs-field');
    label.append(labelRow(spec));
    let el, extra = null;
    if (spec.type === 'select') {
      el = make('select');
      if (spec.options_from === 'voices') extra = voiceSelect(spec, values, ctx, el);
      else for (const o of spec.options || []) el.append(typeof o === 'object' ? new Option(o.label, o.value) : new Option(o, o));
    } else if (spec.type === 'textarea') {
      el = make('textarea'); el.rows = 2;
    } else {
      el = make('input'); el.type = spec.type || 'text';
      if (spec.type === 'number') el.step = spec.step || 'any';
      if (spec.type === 'password') el.autocomplete = 'off';
    }
    el.dataset.key = spec.key;
    const initial = values[spec.key];
    if (spec.type === 'password' && initial === KEEP) { el.value = ''; el.placeholder = 'сохранён — оставьте пустым' }
    else if (spec.options_from !== 'voices') el.value = initial ?? '';
    const mark = () => label.classList.toggle('vs-missing', !!ctx.touched?.() && !!spec.required && isEmpty(spec, values, ctx));
    el.oninput = () => { values[spec.key] = spec.type === 'password' && !el.value && initial === KEEP ? KEEP : el.value; mark() };
    el.onchange = () => { el.oninput(); if (spec.type === 'select') render(true) };  // поля с when зависят от выбора
    mark();
    if (extra) { const row = make('div', 'vs-select-row'); row.append(el, extra); label.append(row) } else label.append(el);
    if (spec.help) label.append(make('small', 'vs-help', spec.help));
    return label;
  }

  // поле-файл (эталон WAV): загрузка с проверкой, прослушивание, удаление; один файл на поле.
  // ctx.fileUrl(key) — адрес API файла владельца (пакет или сохранённый пресет); нет — файл загрузить пока нельзя
  function fileInput(spec, ctx, values) {
    const box = make('div', 'vs-field vs-file'), info = ctx.files?.[spec.key];
    box.append(labelRow(spec));
    if (!ctx.fileUrl) { box.append(make('small', 'vs-help', 'Эталон можно загрузить после создания пресета')); return box }
    const action = ctx.fileUrl(spec.key), url = 'api/' + action;
    const picker = make('input'); picker.type = 'file'; picker.accept = spec.accept || '.wav'; picker.hidden = true;
    picker.onchange = () => work(async () => {
      const file = picker.files[0]; if (!file) return;
      if (!file.name.toLowerCase().endsWith('.wav')) throw Error('Нужен файл .wav');
      if (file.size > 15 * 1024 * 1024) throw Error('Файл больше 15 МБ');
      const bytes = new Uint8Array(await file.arrayBuffer());
      const tag = i => String.fromCharCode(...bytes.slice(i, i + 4));
      if (tag(0) !== 'RIFF' || tag(8) !== 'WAVE') throw Error('Это не WAV (нет заголовка RIFF/WAVE)');
      const r = await fetch(url, { method: 'POST', headers: { 'X-Emulator': '1', 'Content-Type': 'audio/wav' }, body: bytes });
      const v = await r.json().catch(() => ({})); if (!r.ok) throw Error(v.error || r.statusText);
      say('Загружен: ' + file.name + ' · ' + v.seconds + ' с'); await load();
    });
    const row = make('div', 'vs-file-row');
    if (info && !info.error) {
      row.append(make('span', 'vs-file-name', info.name + ' · ' + info.seconds + ' с'));
      const audio = make('audio'); audio.controls = true; audio.preload = 'none';
      audio.src = url + '&v=' + encodeURIComponent(info.name + info.seconds);
      box.append(row, audio);
      if (spec.transcribe_to && values && ctx.transcribeUrl) row.append(button('Распознать текст', 'secondary', async () => {
        const r = await api(ctx.transcribeUrl(spec.key), 'POST');
        values[spec.transcribe_to] = r.text;
        const target = box.closest('.vs-fields')?.querySelector('[data-key="' + spec.transcribe_to + '"]');
        if (target) target.value = r.text;
        say('Текст эталона распознан — проверьте и сохраните настройки');
      }));
      row.append(Object.assign(make('button', 'secondary', 'Заменить'), { type: 'button', onclick: () => picker.click() }),
        button('Удалить', 'danger', async () => { await api(action, 'DELETE'); await load() }));
    } else {
      if (info?.error) row.append(make('span', 'error', info.error));
      row.append(Object.assign(make('button', 'secondary', 'Загрузить WAV'), { type: 'button', onclick: () => picker.click() }));
      box.append(row);
    }
    box.append(picker);
    return box;
  }

  // «Свой голос (запечённый)» поставщика: эталон уходит один раз, в пресете остаётся voice_id
  function bakeBlock(ctx, d) {
    const box = make('div', 'vs-field vs-bake'), values = d.values, spec = ctx.bake || {};
    const title = values.baked_voice_title || values.baked_voice_id;
    const status = make('div', 'vs-bake-status ' + (values.baked_voice_id ? 'on' : 'off'));
    status.append(make('span', 'vs-dot'), make('span', null, values.baked_voice_id ? 'Запечён: ' + title : 'Не запечён'));
    box.append(status);
    const payload = () => ({ service: ctx.service, adapter: ctx.adapter, id: ctx.id || undefined, values });
    if (values.baked_voice_id) {
      const row = make('div', 'vs-actions');
      row.append(button('Удалить голос', 'danger', async () => {
        if (!await ask('Удалить запечённый голос «' + title + '»?', 'Удалить')) return;
        await api('voice-stack-unbake', 'POST', payload());
        values.baked_voice_id = ''; values.baked_voice_title = '';
        if (ctx.persist) await ctx.persist(); say('Голос удалён');
      }));
      box.append(row);
      return box;
    }
    if (spec.available === false) { box.append(make('small', 'vs-help', 'Этот поставщик не умеет запекать голос')); return box }
    const picker = make('input'); picker.type = 'file'; picker.accept = '.wav'; picker.hidden = true;
    const name = make('span', 'vs-file-name', d.bakeFile ? d.bakeFile.name : 'Эталон не выбран');
    picker.onchange = () => { d.bakeFile = picker.files[0] || null; name.textContent = d.bakeFile ? d.bakeFile.name : 'Эталон не выбран' };
    const pick = make('button', 'secondary', 'Выбрать WAV'); pick.type = 'button'; pick.onclick = () => picker.click();
    const row = make('div', 'vs-file-row'); row.append(name, pick, picker); box.append(row);
    if (spec.text !== false) {
      const label = make('label', 'vs-field', 'Текст эталонной записи'), text = make('textarea'); text.rows = 2;
      text.value = d.bakeText || ''; text.oninput = () => { d.bakeText = text.value };
      label.append(text); box.append(label);
    }
    const actions = make('div', 'vs-actions');
    actions.append(button('Запечь', null, async () => {
      if (!d.bakeFile) throw Error('Выберите WAV с эталонной записью');
      const form = new FormData();
      form.append('meta', JSON.stringify({ ...payload(), text: d.bakeText || '' }));
      form.append('audio', d.bakeFile, d.bakeFile.name);
      const r = await fetch('api/voice-stack-bake', { method: 'POST', headers: { 'X-Emulator': '1' }, body: form });
      const v = await r.json().catch(() => ({})); if (!r.ok) throw Error(v.error || r.statusText);
      values.baked_voice_id = v.voice_id; values.baked_voice_title = v.title; d.bakeFile = null; d.bakeText = '';
      if (ctx.persist) await ctx.persist(); say('Голос запечён: ' + v.title);
    }));
    box.append(actions);
    return box;
  }

  // «Запечь» эталон пакета (OmniVoice): сервер запекает при запуске, статус — по файлу эмбеддинга
  function packageBake(s, pkg, d, saveOptions) {
    const box = make('div', 'vs-field vs-bake'), spec = pkg.bake, baked = spec.baked;
    const status = make('div', 'vs-bake-status ' + (baked ? 'on' : 'off'));
    status.append(make('span', 'vs-dot'), make('span', null, baked ? 'Запечён: ' + spec.name : 'Не запечён'));
    const row = make('div', 'vs-actions');
    const ready = pkg.files?.[spec.audio] && ['running', 'stopped', 'starting'].includes(pkg.state.status);
    const bake = button(baked ? 'Запечь заново' : 'Запечь', null, async () => {
      await saveOptions(); await api('voice-stack-bake', 'POST', { package: pkg.id });
      say('Запекание: сервер перезапускается'); await load();
    });
    if (!ready) { bake.disabled = true; bake.title = pkg.files?.[spec.audio] ? 'Сначала установите пакет' : 'Сначала загрузите эталон' }
    row.append(bake);
    if (baked) row.append(button('Удалить голос', 'danger', async () => {
      if (!await ask('Удалить эталон и запечённый голос?', 'Удалить')) return;
      await api('voice-stack-unbake', 'POST', { package: pkg.id }); await load(); say('Голос удалён');
    }));
    box.append(status, row);
    return box;
  }

  const defaultMode = (modes, values) => { if (!modes.includes(values.voice_mode)) values.voice_mode = modes[0] };

  // форма: переключатель режима голоса, основные поля, «Дополнительно» (свёрнуто)
  function form(fields, values, ctx) {
    const box = make('div', 'vs-fields'), modes = ctx.modes || [];
    if (modes.length) defaultMode(modes, values);
    if (modes.length > 1) {
      const group = make('div', 'vs-modes'); group.setAttribute('role', 'group'); group.setAttribute('aria-label', 'Голос');
      for (const m of modes) {
        const b = make('button', 'secondary', MODES[m] || m); b.type = 'button'; b.setAttribute('aria-pressed', String(values.voice_mode === m));
        b.onclick = () => { values.voice_mode = m; render(true) };
        group.append(b);
      }
      box.append(group);
    }
    else if (modes.length === 1 && modes[0] !== 'preset') box.append(make('div', 'vs-mode-name', MODES[modes[0]]));  // единственный режим — подпись
    ctx.touched = () => !!drafts[ctx.key]?.touched;
    const visible = fields.filter(f => shown(f, values));
    const one = f => f.type === 'file' ? fileInput(f, ctx, values) : input(f, values, ctx);
    visible.filter(f => f.group !== 'advanced').forEach(f => box.append(one(f)));
    if (values.voice_mode === 'baked' && ctx.bakeBlock) box.append(ctx.bakeBlock());
    const advanced = visible.filter(f => f.group === 'advanced');
    if (advanced.length) {
      const details = make('details', 'vs-adv'), summary = make('summary', null, 'Дополнительно');
      details.open = openAdv.has(ctx.key);
      details.addEventListener('toggle', () => { details.open ? openAdv.add(ctx.key) : openAdv.delete(ctx.key) });
      const inner = make('div', 'vs-fields'); advanced.forEach(f => inner.append(one(f)));
      details.append(summary, inner); box.append(details);
    }
    return box;
  }
  const top = (el, actions) => el.querySelector('.vs-bar').after(actions);  // кнопки действий — сразу под списком

  // --- «Добавить»: адаптер → поля → новый пресет -----------------------------------------
  function addView(s, el) {
    const kinds = data[s].adapters, adapter = kinds.find(a => a.id === adding[s]) || kinds[0];
    const kind = make('select', 'vs-select');
    kinds.forEach(a => kind.append(new Option(a.title, a.id)));
    kind.value = adapter.id;
    kind.onchange = () => { adding[s] = kind.value; render(true) };
    const label = make('label', 'vs-field', 'Адаптер'); label.append(kind); el.append(label);
    if (adapter.description) el.append(make('p', 'vs-desc', adapter.description));

    const key = s + '/new/' + adapter.id;
    const d = draft(key, () => ({ title: adapter.title, values: { ...adapter.defaults } }));
    const examples = Object.keys(adapter.examples || {});
    if (examples.length) {
      const pick = make('select');
      pick.append(new Option('—', ''), ...examples.map(n => new Option(n, n)));
      pick.onchange = () => { if (!pick.value) return; Object.assign(d.values, adapter.examples[pick.value]); d.title = pick.value; render(true) };
      const l = make('label', 'vs-field', 'Заполнить как'); l.append(pick); el.append(l);
    }
    const ctx = { key, service: s, adapter: adapter.id, modes: adapter.voice_modes, bake: adapter.bake, files: {} };
    ctx.bakeBlock = () => bakeBlock(ctx, d);
    el.append(input({ key: 'title', label: 'Название' }, d), form(adapter.fields, d.values, ctx));
    const actions = make('div', 'vs-actions');
    actions.append(button('Создать', null, async () => {
      checkRequired(adapter.fields, d.values, ctx, d);
      const r = await api('voice-stack-preset', 'POST', { service: s, adapter: adapter.id, title: d.title, values: d.values });
      delete drafts[key]; adding[s] = null; chosen[s] = 'p:' + r.id; await load(); say('Пресет создан: ' + d.title);
    }));
    top(el, actions);
  }

  // --- пресет ------------------------------------------------------------------------------
  function presetView(s, preset, el) {
    const ref = 'p:' + preset.id, active = ref === data[s].active;
    const adapter = data[s].adapters.find(a => a.id === preset.adapter);
    if (!adapter) { el.append(make('p', 'error', 'Адаптер «' + preset.adapter + '» не найден')); return }
    el.append(make('p', 'vs-desc', adapter.title + (adapter.description ? ' — ' + adapter.description : '')));
    const key = s + '/' + ref;
    const d = draft(key, () => ({ title: preset.title, values: Object.fromEntries([...adapter.fields.map(f => [f.key, preset.values[f.key] ?? adapter.defaults[f.key] ?? '']),
      ...['voice_mode', 'baked_voice_id', 'baked_voice_title'].map(k => [k, preset.values[k] ?? ''])]) }));
    const save = async () => { await api('voice-stack-preset', 'POST', { service: s, id: preset.id, adapter: preset.adapter, title: d.title, values: d.values }); delete drafts[key] };
    const ctx = { key, service: s, adapter: adapter.id, id: preset.id, modes: adapter.voice_modes, bake: adapter.bake, files: preset.files || {},
      fileUrl: preset.readonly ? null : k => 'voice-stack-preset-file?service=' + s + '&id=' + encodeURIComponent(preset.id) + '&key=' + k,
      persist: async () => { await save(); await load() } };
    ctx.bakeBlock = () => bakeBlock(ctx, d);
    el.append(input({ key: 'title', label: 'Название' }, d), form(adapter.fields, d.values, ctx));
    const check = () => checkRequired(adapter.fields, d.values, ctx, d);
    const actions = make('div', 'vs-actions');
    if (!active) actions.append(button('Применить', null, async () => { check(); await save(); await api('voice-stack-apply', 'POST', { service: s, ref }); await load(); say('Применён: ' + d.title) }));
    actions.append(button('Сохранить', active ? null : 'secondary', async () => { check(); await save(); await load(); say('Сохранено: ' + d.title) }));
    if (s === 'tts' && adapter.test) actions.append(testButton(ref, 'Проверить голос'));
    if (!active) actions.append(button('Удалить', 'danger', async () => {
      if (!await ask('Удалить пресет «' + preset.title + '»?', 'Удалить')) return;
      await api('voice-stack-preset?service=' + s + '&id=' + encodeURIComponent(preset.id), 'DELETE');
      delete drafts[key]; chosen[s] = ''; await load();
    }));
    top(el, actions);
  }

  // --- пакет -------------------------------------------------------------------------------
  // блок установки/статуса пакета; фоновый опрос заменяет только его (форма и списки не трогаются)
  // Проба голоса: синтез фиксированной фразы источником без звукового кеша
  const tester = new Audio();
  function testButton(ref, text) {
    const b = make('button', text ? 'secondary' : 'sc-icon vs-test', text || null); b.type = 'button'; b.title = 'Проверить голос'; b.setAttribute('aria-label', 'Проверить голос');
    if (!text) b.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M4 2.5v11l9-5.5z" fill="currentColor"/></svg>';
    b.onclick = async () => {
      b.disabled = true; b.classList.add('busy');
      try {
        const r = await fetch('api/voice-stack-test', { method: 'POST', headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ ref }) });
        if (!r.ok) throw Error((await r.json().catch(() => ({}))).error || r.statusText);
        URL.revokeObjectURL(tester.src); tester.src = URL.createObjectURL(await r.blob()); await tester.play();
      } catch (e) { say(e.message, true) } finally { b.disabled = false; b.classList.remove('busy') }
    };
    return b;
  }

  function statusBox(s, pkg) {
    const ref = 'k:' + pkg.id, active = ref === data[s].active, st = pkg.state, key = s + '/' + ref;
    const saveOptions = async () => { if (drafts[key]) { await api('voice-stack-package', 'POST', { id: pkg.id, values: drafts[key].values }); delete drafts[key] } };
    const box = make('div', 'vs-local ' + st.status), head = make('div', 'vs-local-head');
    head.append(make('span', 'vs-dot'), make('b', null, STATE[st.status] || st.status));
    if (s === 'tts' && st.status === 'running') head.append(testButton(ref));
    if (st.voice) head.append(make('span', 'vs-voice', st.voice));
    head.append(make('span', 'vs-port', '127.0.0.1:' + pkg.values.port));
    box.append(head);
    if (st.status === 'installing') {
      const bar = make('div', 'progress-bar'), fill = make('div', 'progress-fill');
      fill.style.width = Math.round((st.progress || 0) * 100) + '%'; bar.append(fill);
      box.append(bar, make('div', 'vs-stage', st.stage || ''));
    }
    if (st.error) box.append(make('pre', 'vs-log', st.error));
    if (['stopped', 'starting'].includes(st.status) && st.log) box.append(make('pre', 'vs-log', st.log));
    const row = make('div', 'vs-actions');
    if (['absent', 'error'].includes(st.status)) {
      row.append(button(st.status === 'error' ? 'Повторить установку' : 'Установить', null, async () => { await saveOptions(); await api('voice-stack-install', 'POST', { id: pkg.id }); say('Установка: ' + pkg.title); await load() }));
    } else if (!['installing', 'unsupported'].includes(st.status)) {
      if (st.outdated?.length) row.append(button('Переустановить', null, async () => { await saveOptions(); await api('voice-stack-install', 'POST', { id: pkg.id }); await load() }));
      const live = ['running', 'starting'].includes(st.status);
      row.append(button(live ? 'Перезапустить' : 'Запустить', 'secondary', async () => { await api('voice-stack-restart', 'POST', { id: pkg.id }); await load() }));
      if (live) row.append(button('Остановить', 'secondary', async () => {
        if (active && !await ask(pkg.title + ' применён — без сервера голос не будет работать. Остановить?', 'Остановить')) return;
        await api('voice-stack-stop', 'POST', { id: pkg.id }); await load(); say('Остановлен: ' + pkg.title);
      }));
      const remove = button('Удалить установку', 'danger', async () => {
        if (!await ask('Удалить установку ' + pkg.title + ' (окружение и модели)? Настройки останутся.', 'Удалить')) return;
        await api('voice-stack-uninstall', 'POST', { id: pkg.id }); await load(); say('Установка удалена: ' + pkg.title);
      });
      if (live) { remove.disabled = true; remove.title = 'Сначала остановите сервер' }
      row.append(remove);
    }
    if (row.childElementCount) box.append(row);
    box.dataset.pkg = s + '/' + pkg.id;
    return box;
  }

  function packageView(s, pkg, el) {
    const ref = 'k:' + pkg.id, active = ref === data[s].active, st = pkg.state;
    if (pkg.description) el.append(make('p', 'vs-desc', pkg.description));
    const req = pkg.requirements || {};
    const need = [req.gpu ? 'видеокарта' + (req.vram_gb ? ' ' + req.vram_gb + ' ГБ' : '') : 'без видеокарты', req.disk_gb ? req.disk_gb + ' ГБ на диске' : ''].filter(Boolean).join(' · ');
    if (need) el.append(make('p', 'vs-req', need + (pkg.note ? ' · ' + pkg.note : '')));

    const key = s + '/' + ref, d = draft(key, () => ({ values: { ...pkg.values } }));
    const saveOptions = async () => { if (drafts[key]) { await api('voice-stack-package', 'POST', { id: pkg.id, values: d.values }); delete drafts[key] } };
    const ctx = { key, service: s, adapter: pkg.id, modes: pkg.voice_modes, files: pkg.files || {},
      fileUrl: k => 'voice-stack-file?id=' + encodeURIComponent(pkg.id) + '&key=' + k,
      transcribeUrl: k => 'voice-stack-transcribe?id=' + encodeURIComponent(pkg.id) + '&key=' + k };
    if (pkg.bake) ctx.bakeBlock = () => packageBake(s, pkg, d, saveOptions);
    el.append(statusBox(s, pkg), form(pkg.fields, d.values, ctx));

    const check = () => checkRequired(pkg.fields, d.values, ctx, d);
    const actions = make('div', 'vs-actions');
    if (!active && ['running', 'stopped', 'starting'].includes(st.status))
      actions.append(button('Применить', null, async () => { check(); await saveOptions(); await api('voice-stack-apply', 'POST', { service: s, ref }); await load(); say('Применён: ' + pkg.title) }));
    actions.append(button('Сохранить настройки', 'secondary', async () => { check(); await saveOptions(); await load(); say('Настройки сохранены: ' + pkg.title) }));
    top(el, actions);
  }

  function card(s, title) {
    const block = data[s], el = make('section', 'vs-card');
    el.append(make('h3', null, title));
    const bar = make('div', 'vs-bar'), select = make('select', 'vs-select');
    const group = (label, list) => { if (!list.length) return; const g = make('optgroup'); g.label = label; list.forEach(([v, t]) => g.append(new Option(t + (v === block.active ? '  ✓' : ''), v))); select.append(g) };
    group('Пресеты', block.presets.map(p => ['p:' + p.id, p.title]));
    group('Пакеты', block.packages.map(p => ['k:' + p.id, p.title]));
    select.value = chosen[s];
    select.disabled = !!adding[s];
    select.onchange = () => { chosen[s] = select.value; render(true) };
    const add = make('button', 'secondary vs-add', adding[s] ? 'Отмена' : 'Добавить'); add.type = 'button';
    add.onclick = () => { adding[s] = adding[s] ? null : (block.adapters[0]?.id || null); render(true) };
    bar.append(select, add);
    el.append(bar);

    if (adding[s]) addView(s, el);
    else {
      const [kind, id] = [chosen[s].slice(0, 1), chosen[s].slice(2)];
      const preset = kind === 'p' && block.presets.find(p => p.id === id);
      const pkg = kind === 'k' && block.packages.find(p => p.id === id);
      if (preset) presetView(s, preset, el); else if (pkg) packageView(s, pkg, el);
    }
    return el;
  }

  function render(force = false) {
    if (!data) return;
    if (!force && poll && hosts().some(h => h.contains(document.activeElement))) return;  // не сбивать ввод фоновым обновлением
    host.replaceChildren(...SERVICES.filter(([s]) => s !== 'llm').map(([s, t]) => card(s, t)));
    llmHost?.replaceChildren(card('llm', 'Модель (LLM)'));
    if (busy) hosts().forEach(h => h.querySelectorAll('button,select,input,textarea').forEach(b => b.disabled = true));
  }

  const sync = () => { clearTimeout(poll); poll = null; if (['#voice-stack', '#llm'].includes(location.hash)) work(load) };
  window.addEventListener('hashchange', sync);
  document.addEventListener('DOMContentLoaded', sync);
  if (document.readyState !== 'loading') sync();
})();
