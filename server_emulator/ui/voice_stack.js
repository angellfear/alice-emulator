/* Голосовой стек: карточка на сервис (ASR / TTS / LLM).
   Список — пресеты (адаптер + настройки) и пакеты (установка сервера на этот ПК); под ним сразу
   настройки выбранного. Над карточками — метр микрофона: уровни «Вход» и «В ASR» до/после
   выравнивания (api/voice-levels), полоса истории и колёса предусиления/нормализации.
   «Добавить» → выбор адаптера → поля → новый пресет.
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
    // применение источника, правка применённого пресета и параметров пакета пишут
    // общие настройки на сервере — перечитываем их, иначе сохранение другой вкладки пришлёт
    // устаревшую копию (сервер сверит ревизию, но форма edits потеряются)
    if (method !== 'GET' && ['voice-stack-apply', 'voice-stack-preset', 'voice-stack-package'].includes(action.split('?')[0]))
      window.refreshSettings?.().catch(() => {});
    return v;
  };

  let data = null, poll = null, busy = false, loadError = null;
  const chosen = {};  // service → "p:<пресет>" | "k:<пакет>"
  const adding = {};  // service → id адаптера в режиме «Добавить» (null — обычный режим)
  const drafts = {};  // ключ формы → {title, values}: введённое, но не сохранённое
  const openAdv = new Set();  // ключи форм с раскрытым «Дополнительно» (только в этой странице)
  const voiceLists = {};  // ключ формы + поле → список голосов, полученный у поставщика
  const engineSeen = {};  // ключ формы + поле → движок (engine), для которого показан список голосов

  async function work(fn) {
    if (busy) return; busy = true;
    hosts().forEach(h => h.querySelectorAll('button,select,input,textarea').forEach(b => b.disabled = true));
    try { await fn(); loadError = null } catch (e) { if (!data) loadError = e.message; say(e.message, true) } finally { busy = false; render(true) }
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
    if (!cfg || !background) { try { cfg = await api('settings') } catch (e) { cfg = { voice_stack: {} } } }  // режимы вывода; без настроек — флаги скрыты
    data = await api('voice-stack');
    for (const [s] of SERVICES) if (!refs(s).includes(chosen[s])) chosen[s] = data[s].active || refs(s)[0] || '';
    if (!background) document.dispatchEvent(new Event('vs-changed'));  // блок «Установлено локально»
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
    if (!active) actions.append(button('Применить', null, async () => { check(); await save(); const ok = await voiceApply(s, ref, d.title); await load(); if (ok) say('Применён: ' + d.title) }));
    actions.append(button('Сохранить', active ? null : 'secondary', async () => { check(); await save(); await load(); say('Сохранено: ' + d.title) }));
    if (s === 'tts' && adapter.test) actions.append(testButton(ref, 'Проверить голос'));
    if (adapter.login) actions.append(button('Войти', 'secondary', () => loginDialog(adapter)));
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

  // вход модели по подписке (код устройства): сервер запускает login отдельным процессом,
  // диалог показывает ссылку и код из его вывода и ждёт завершения. Перерисовка — только
  // при изменении вывода: переписывание текста каждую секунду сбивало выделение кода
  function loginDialog(adapter) {
    let timer = null, shown = null;
    const dialog = make('dialog', 'catalog-dialog');
    const close = () => { clearTimeout(timer); dialog.close(); dialog.remove() };
    const state = make('p', null, 'Запускаю вход…');
    const link = make('a'); link.target = '_blank'; link.rel = 'noopener';
    const linkWrap = make('p');
    const code = make('p', 'vs-login-code');
    const copy = make('button', 'secondary', 'Копировать код'); copy.type = 'button';
    copy.onclick = async () => {
      if (!code.textContent) return;
      try { await navigator.clipboard.writeText(code.textContent); say('Код скопирован') }
      catch { say('Не удалось скопировать — выделите код мышью', true) }
    };
    const codeWrap = make('div', 'row'); codeWrap.style.display = 'none'; codeWrap.append(code, copy);
    const log = make('pre', 'vs-log plain');
    const row = make('div', 'row');
    const closeBtn = make('button', 'secondary', 'Закрыть'); closeBtn.type = 'button'; closeBtn.onclick = close;
    const cancelBtn = make('button', 'danger', 'Отменить вход'); cancelBtn.type = 'button';
    cancelBtn.onclick = async () => {
      clearTimeout(timer);
      try { await api('voice-stack-login', 'DELETE'); say('Вход отменён') } catch (e) { say(e.message, true) }
      close();
    };
    const render = r => {
      const out = r.output || [];
      const next = {
        running: !!r.running, exit: r.exit ?? null,
        url: out.map(l => l.match(/^1\) Visit (\S+)/)?.[1]).find(Boolean) || '',
        userCode: out.map(l => l.match(/^2\) Enter code: (.+)$/)?.[1]).find(Boolean) || '',
        cooldown: r.cooldown || 0,
        log: out.slice(-6).join('\n'),
      };
      const key = JSON.stringify(next);
      if (key === shown) return;
      shown = key;
      if (next.running) {
        const wait = Math.floor(next.cooldown / 60) + ':' + String(next.cooldown % 60).padStart(2, '0');
        state.textContent = next.userCode ? 'Откройте ссылку в браузере и введите код:'
          : next.cooldown ? `OpenAI ограничил частоту: новый код через ${wait} — прошлая попытка была недавно`
          : 'Ждём код устройства…';
        if (next.url) { link.href = next.url; link.textContent = next.url; linkWrap.replaceChildren(link) }
        if (code.textContent !== next.userCode) code.textContent = next.userCode;  // перезапись сбивала выделение
        codeWrap.style.display = next.userCode ? '' : 'none';
        if (log.textContent !== next.log) log.textContent = next.log;
        row.replaceChildren(cancelBtn, closeBtn);
      } else if (next.exit === 0) {
        state.textContent = 'Готово: вход сохранён. Можно закрыть.';
        linkWrap.replaceChildren(); codeWrap.style.display = 'none';
        log.textContent = out.slice(-4).join('\n');
        row.replaceChildren(closeBtn);
      } else if (next.exit != null) {
        state.textContent = 'Вход не удался — журнал:';
        linkWrap.replaceChildren(); codeWrap.style.display = 'none';
        log.textContent = out.slice(-8).join('\n');
        row.replaceChildren(closeBtn);
      }
    };
    const poll = async () => {
      try {
        const r = await api('voice-stack-login');
        render(r);
        if (r.running) timer = setTimeout(poll, 1000);
        else if (r.exit === 0) { await load(); say('Вход выполнен: ' + adapter.title) }
      } catch (e) { state.textContent = 'Состояние входа недоступно'; log.textContent = e.message }
    };
    dialog.append(make('h3', null, 'Вход: ' + adapter.title), state, linkWrap, codeWrap, log, row);
    dialog.addEventListener('cancel', e => { e.preventDefault(); close() });
    document.body.append(dialog); dialog.showModal();
    api('voice-stack-login', 'POST', { service: adapter.id }).then(poll)
      .catch(e => { state.textContent = 'Не удалось запустить вход'; log.textContent = e.message; row.replaceChildren(closeBtn) });
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
    // журнал: при запуске — прогресс обычным цветом; красным — только при сбое (crashed);
    // штатная остановка — без журнала
    if (st.status === 'starting' && st.log) box.append(make('pre', 'vs-log plain', st.log));
    else if (st.crashed && st.log) box.append(make('pre', 'vs-log', st.log));
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
      actions.append(button('Применить', null, async () => { check(); await saveOptions(); const ok = await voiceApply(s, ref, pkg.title); await load(); if (ok) say('Применён: ' + pkg.title) }));
    actions.append(button('Сохранить настройки', 'secondary', async () => { check(); await saveOptions(); await load(); say('Настройки сохранены: ' + pkg.title) }));
    top(el, actions);
  }

  // --- режимы вывода ответа (карточка TTS): «Озвучивать» и «Карточка с текстом» -------------
  // settings["voice_stack"].speak/card; озвучка выключена — карточка включена принудительно
  // (ответа совсем без текста нет). Сохранение — полная форма с _rev, 409 → перечитать и повторить.
  let cfg = null;  // общие настройки эмулятора: читаются при первой отрисовке, пишутся переключателями
  const outputBlock = () => {
    const box = make('div', 'vs-output');
    const stack = cfg.voice_stack || {};  // свежий state может не знать ключей
    const row = make('div', 'vs-flags');
    const speakLabel = make('label', 'vs-flag'), speakBox = make('input');
    speakBox.type = 'checkbox'; speakBox.checked = stack.speak !== false;
    speakLabel.append(speakBox, make('span', null, 'Озвучивать'));
    const cardLabel = make('label', 'vs-flag'), cardBox = make('input');
    cardBox.type = 'checkbox';
    const syncCard = () => {
      cardBox.checked = !speakBox.checked || (cfg.voice_stack || {}).card !== false;
      cardBox.disabled = !speakBox.checked;
    };
    syncCard();
    cardLabel.append(cardBox, make('span', null, 'Карточка с текстом'));
    const wordsLabel = make('label', 'vs-flag'), wordsBox = make('input');
    wordsBox.type = 'checkbox'; wordsBox.checked = stack.speak_words === true;
    wordsLabel.append(wordsBox, make('span', null, 'Знаки и числа словами'));
    row.append(speakLabel, cardLabel, wordsLabel);
    const save = () => work(async () => {
      for (let tries = 0;; tries++) {
        const next = structuredClone(cfg);
        next.voice_stack = { ...(next.voice_stack || {}), speak: speakBox.checked, card: cardBox.checked, speak_words: wordsBox.checked };
        next._rev = cfg._rev;
        const r = await fetch('api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Emulator': '1' }, body: JSON.stringify(next) });
        const v = await r.json().catch(() => ({}));
        if (r.status === 409 && tries < 1) { cfg = await api('settings'); continue; }  // форма устарела — база перечитана, повтор
        if (!r.ok) throw Error(v.error || r.statusText);
        cfg = v; break;
      }
      syncCard(); window.refreshSettings?.().catch(() => {}); say('Сохранено');
    });
    speakBox.onchange = () => { syncCard(); save(); };
    cardBox.onchange = () => save();
    wordsBox.onchange = () => save();
    box.append(row, make('small', 'vs-help', 'Без озвучки синтез не вызывается — ответ приходит карточкой, как беззвучные ответы настоящего сервера. «Знаки и числа словами» меняет только звучание: цифры и знаки прочитываются словами (3,5 — «три целых пять десятых», −5° — «минус пять градусов»), карточка и журнал показывают оригинал.'));
    return box;
  };

  // --- Метр микрофона: живые уровни до и после выравнивания (api/voice-levels). ------------
  // «Вход» — микрофон колонки как есть, «В ASR» — после предусиления и выравнивания
  // (то, что слышит распознавание). Уровни снимает transcode у любого источника ASR,
  // идущего через выравнивание. У каждой колонки свой метр и свои колёса (профиль
  // stt.profiles[sn]): селектор выбирает колонку, метр сам переключается на говорящую
  // (после ручного выбора — пауза 15 с, чтобы рассмотреть выбранную), «Автонастройка»
  // измеряет микрофон и пишет профиль сама, колёса правятся через «Применить»/«Отмена».
  // Шкала −60…0 dBFS, полоса истории — последние окна (RMS по 100 мс). Опрос 10 Гц,
  // пока вкладка видима; чтение сервер не изменяет.
  const meter = { refs: null, timer: null, hold: {}, clip: false, sn: null, pinUntil: 0,
                  tuning: false, holdStatus: null, names: null, namesAt: 0,
                  lastT: {}, columnsSignature: null };
  const METER_MIN = -60;  // низ шкалы, dBFS
  const pct = db => Math.max(0, Math.min(100, (db - METER_MIN) / -METER_MIN * 100));
  const fmtDb = v => (v > 0 ? '+' : '') + Number(v).toFixed(1).replace(/\.0$/, '');
  const columnLabel = sn => (meter.names || {})[sn] || sn;

  // Имена привязанных устройств (identities) — для подписей селектора; кеш на 30 с,
  // обновление в фоне: опрос метра не должен ждать лишнего GET.
  function meterNames() {
    if (meter.names && Date.now() - meter.namesAt < 30000) return;
    meter.namesAt = Date.now();
    api('identities').then(d => {
      const names = {};
      (d.devices || []).forEach(dev => { if (dev.sn) names[dev.sn] = dev.name || dev.sn });
      meter.names = names;
    }).catch(() => {});
  }

  function meterBar(label) {
    const row = make('div', 'vs-meter-row');
    row.append(make('span', 'vs-meter-label', label));
    const track = make('div', 'vs-meter-track');
    const fill = make('div', 'vs-meter-fill');
    const peak = make('div', 'vs-meter-peak');
    track.append(fill, peak);
    const read = make('span', 'vs-meter-read', '−∞ dBFS');
    row.append(track, read);
    return { row, fill, peak, read };
  }

  function meterSet(bar, rms, peak, slot, now) {
    bar.fill.style.width = (rms == null ? 0 : pct(rms)) + '%';
    bar.fill.className = 'vs-meter-fill' + (peak != null && peak > -6 ? ' hot' : peak != null && peak > -18 ? ' warm' : '');
    bar.read.textContent = rms == null ? '−∞ dBFS' : rms.toFixed(1) + ' dBFS';
    const hold = meter.hold[slot] || (meter.hold[slot] = { db: METER_MIN, until: 0, t: now });
    if (peak != null && peak >= hold.db) { hold.db = peak; hold.until = now + 1500 }  // удержание 1,5 с
    else if (now > hold.until) hold.db = Math.max(rms ?? METER_MIN, hold.db - 0.03 * (now - hold.t));  // спад 30 дБ/с
    hold.t = now;
    bar.peak.style.left = pct(hold.db) + '%';
    bar.peak.style.display = hold.db > METER_MIN + 1 ? '' : 'none';
  }

  function meterStatus(v, outRms) {
    if (!v.active || (outRms != null && outRms <= -90)) return 'Микрофон молчит — скажите колонке что-нибудь, и метр покажет уровень речи.';
    if (v.out?.peak != null && v.out.peak > -3) return 'Сигнал упирается в ограничение — уменьшите предусиление.';
    if ((v.gain ?? 0) > 45) return 'Микрофон очень тихий: усиление на потолке. Добавьте предусиления или говорите ближе к колонке.';
    return 'Речь ' + (outRms == null ? '—' : outRms.toFixed(1) + ' dBFS') + ' в ASR: норма −30…−12 dBFS, пик около −6.';
  }

  function meterHistory(canvas) {
    const draw = v => {
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth || 0, h = canvas.clientHeight || 44;
      if (!w) return;
      if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr) }
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const css = getComputedStyle(canvas);
      const ok = css.getPropertyValue('--ok').trim() || '#8fe0a8';
      const warn = css.getPropertyValue('--warning').trim() || '#e0c88f';
      const hot = css.getPropertyValue('--danger').trim() || '#e08f8f';
      const dim = 'rgba(127,127,127,.45)';
      const yTarget = Math.round(h * (100 - pct(-6)) / 100) + .5;  // пунктир — цель нормализации −6 dBFS
      ctx.strokeStyle = dim; ctx.setLineDash([3, 3]); ctx.beginPath();
      ctx.moveTo(0, yTarget); ctx.lineTo(w, yTarget); ctx.stroke(); ctx.setLineDash([]);
      const slot = 4, tail = v.history.slice(-Math.floor(w / slot));  // справа — свежие окна
      tail.forEach((e, i) => {
        const x = w - (tail.length - i) * slot;
        if (e.o != null) {
          const height = Math.max(1, Math.round(h * pct(e.o) / 100));
          ctx.fillStyle = e.op != null && e.op > -6 ? hot : e.op != null && e.op > -18 ? warn : ok;
          ctx.fillRect(x, h - height, 2, height);
        }
        if (e.i != null) {
          const height = Math.max(1, Math.round(h * pct(e.i) / 100));
          ctx.fillStyle = dim;
          ctx.fillRect(x + 2, h - height, 1, height);
        }
        if (e.oc > 0 || e.ic > 0) { ctx.fillStyle = hot; ctx.fillRect(x, 0, 3, 2) }
      });
    };
    return draw;
  }

  // Колёса выравнивания выбранной колонки — профиль stt.profiles[sn], без колонки —
  // общие stt.*. Правки копятся в черновик и применяются «Применить», «Отмена» возвращает
  // сохранённое — автосохранения нет. Перестраиваются при смене колонки (черновик сбрасывается).
  // Видны, только когда активный STT идёт через выравнивание (batch/эмуляция потока);
  // у облачных потоковых своя нормализация в пресете.
  function meterControlsBox() {
    const box = make('div', 'vs-flags');
    const stt = () => (cfg?.stt || {});
    const prof = () => meter.sn ? ((stt().profiles || {})[meter.sn] || {}) : {};
    const value = (key, def) => { const n = Number(prof()[key] ?? stt()[key]); return Number.isFinite(n) ? n : def };
    const draft = {};  // правки колёс до «Применить»: {pre_gain_db: …, level_target_dbfs: …}
    let apply, cancel;
    const dirty = () => Object.keys(draft).length > 0;
    const sync = () => { if (apply && cancel) apply.disabled = cancel.disabled = !dirty() };
    const knob = (key, title, hint, min, max, def) => {
      const wrap = make('label', 'vs-meter-knob'), input = make('input');
      input.type = 'range'; input.min = String(min); input.max = String(max); input.step = '1'; input.title = hint;
      input.value = draft[key] ?? value(key, def);
      const text = make('span', null, `${title}: ${input.value}`);
      input.oninput = () => { text.textContent = `${title}: ${input.value}` };
      input.onchange = () => { draft[key] = Number(input.value); sync() };
      wrap.append(text, input);
      return wrap;
    };
    const fill = (reset) => {
      if (reset) for (const key of Object.keys(draft)) delete draft[key];  // смена колонки — черновик чужой
      box.replaceChildren();
      const actions = make('div', 'vs-meter-actions');
      const tune = make('button', 'secondary', 'Автонастройка');
      tune.type = 'button';
      tune.title = 'Колонка слушает вас около 8 секунд: скажите «Алиса» и произнесите пару фраз обычным голосом — предусиление и уровень речи подберутся сами.';
      tune.onclick = async () => {
        if (meter.tuning || !meter.sn) return;
        meter.tuning = true; tune.disabled = true;
        meter.holdStatus = `Настройка «${columnLabel(meter.sn)}»: скажите «Алиса» и произнесите пару фраз обычным голосом…`;
        try {
          const r = await api('voice-autotune', 'POST', { sn: meter.sn });
          say(`Настроено: предусиление ${fmtDb(r.pre_gain_db)} дБ, пик речи ${r.level_target_dbfs} dBFS` + (r.note ? `. ${r.note}` : ''));
          cfg = await api('settings').catch(() => cfg);
          fill(true);
          meter.holdStatus = `Готово: речь ${r.speech_dbfs?.toFixed(1) ?? '—'} dBFS, шум ${r.noise_dbfs?.toFixed(1) ?? '—'} dBFS — сохранено в профиль колонки.`;
          setTimeout(() => { meter.holdStatus = null }, 8000);
        } catch (e) {
          say(e.message, true);
          meter.holdStatus = 'Автонастройка не удалась: ' + e.message;
          setTimeout(() => { meter.holdStatus = null }, 10000);
        }
        meter.tuning = false; tune.disabled = false;
        meter.pinUntil = performance.now() + 15000;  // настройка завершена: колонка не сменится, пока смотрят результат
      };
      apply = make('button', 'secondary', 'Применить');
      apply.type = 'button';
      apply.title = 'Записать правки колёс в профиль колонки (без колонки — в общие настройки).';
      apply.onclick = async () => {
        if (!dirty()) return;
        const patch = { ...draft };
        for (const key of Object.keys(draft)) delete draft[key];
        sync();
        if (await saveSttProfile(meter.sn, patch, () => fill(true))) fill();  // сбой: показано сохранённое, черновик сброшен
      };
      cancel = make('button', 'secondary', 'Отмена');
      cancel.type = 'button';
      cancel.title = 'Вернуть колёса к сохранённым значениям.';
      cancel.onclick = () => {
        if (!dirty()) return;
        fill(true);
      };
      actions.append(tune, apply, cancel);
      box.append(actions,
        knob('pre_gain_db', 'Предусиление, дБ',
          'Дополнительное усиление для совсем тихого микрофона колонки; выравнивание при этом остаётся главным.', -12, 24, 0),
        knob('level_target_dbfs', 'Пик речи, dBFS',
          'До какого уровня держать вашу речь (−6 — стандарт; тише — больше запас от перегруза).', -18, -3, -6),
        make('small', 'vs-help',
          '«Автонастройка» сама измерит микрофон колонки по вашей речи и впишет оба колеса в её профиль. Вручную: выравнивание держит речь на выбранном уровне (усиление видно в шапке метра), предусиление — только для совсем глухого микрофона; правки применяются «Применить», у облачных потоковых источников своя нормализация в пресете.'));
      sync();
    };
    fill();
    return { box, fill };
  }

  function meterBlock() {
    const box = make('section', 'vs-card vs-meter');
    const head = make('div', 'vs-meter-head');
    const gain = make('span', 'vs-meter-gain', '…');
    gain.title = 'Итоговое усиление выравнивания — появится после первого звука колонки.';
    const led = make('button', 'vs-meter-led');
    led.type = 'button';
    led.title = 'Ограничение сигнала (клип): в звуке были сэмплы выше 0 dBFS — уменьшите предусиление. Клик — сбросить отметку.';
    led.hidden = true;  // виден только при перегрузе — красная точка
    led.setAttribute('aria-label', 'Ограничение сигнала');
    led.onclick = () => { meter.clip = false; led.hidden = true };
    head.append(make('h3', null, 'Уровень микрофона'), gain, led);
    // колонка: селектор (имя устройства или sn); метр сам переключается на говорящую
    const device = make('select', 'vs-meter-device');
    device.title = 'Чей микрофон показывать и какую колонку настраивать. Метр переключается на говорящую колонку сам; после ручного выбора на 15 секунд остаётся на ней.';
    device.onchange = () => {
      meter.sn = device.value || null;
      meter.pinUntil = performance.now() + 15000;  // ручной выбор: не выдёргивать сразу
      meterPoll();
      meter.refs.fillControls?.(true);  // колёса и черновик — уже другой колонки
    };
    const devices = make('div', 'vs-meter-devices');
    devices.append(device);
    const bars = { in: meterBar('Вход'), out: meterBar('В ASR') };
    const scale = make('div', 'vs-meter-row vs-meter-scale-row');
    const scaleRow = make('div', 'vs-meter-scale');
    for (let db = METER_MIN; db <= 0; db += 12) scaleRow.append(make('span', null, String(db)));
    scale.append(make('span'), scaleRow, make('span'));
    const history = make('canvas', 'vs-meter-history');
    history.setAttribute('aria-hidden', 'true');
    const status = make('small', 'vs-help', 'Микрофон молчит.');
    box.append(head, devices, bars.in.row, bars.out.row, scale, history, status,
      make('small', 'vs-help', 'Вход — микрофон колонки как есть, «В ASR» — после предусиления и выравнивания. Шкала −60…0 dBFS, пунктир — цель нормализации; в полосе цветные столбики — ASR, серые — вход.'));
    const controls = data?.asr?.streaming === false ? meterControlsBox() : null;
    if (controls) box.append(controls.box);
    meter.refs = { bars, gain, led, status, device, row: bars.in.row, historyDraw: meterHistory(history),
                   fillControls: controls ? controls.fill : null };
    return box;
  }

  async function meterPoll() {
    if (!meter.refs?.row.isConnected) { meter.refs = null; meterSync(); return }  // вкладка перерисована — старый метр мёртв
    meterNames();
    try { meterDraw(await api('voice-levels' + (meter.sn ? '?sn=' + encodeURIComponent(meter.sn) : ''))) }
    catch (e) { meter.refs.status.textContent = 'Метр: нет связи — ' + e.message }
  }

  function meterColumns(refs, columns, shown) {
    // селектор: колонки с данными хаба + привязанные устройства + показанная сейчас;
    // перестраивается только при изменении списка (каждый опрос DOM не трогаем)
    const wanted = new Map();
    for (const c of columns || []) wanted.set(c.sn, null);
    Object.keys(meter.names || {}).forEach(sn => { if (!wanted.has(sn)) wanted.set(sn, null) });
    if (shown) wanted.set(shown, null);
    const signature = [...wanted.keys()].join('\n') + '|' + shown;
    if (signature === meter.columnsSignature) return;
    meter.columnsSignature = signature;
    refs.device.replaceChildren();
    if (!shown) refs.device.append(new Option('— выберите колонку —', ''));
    for (const sn of wanted.keys()) refs.device.append(new Option(columnLabel(sn), sn));
    refs.device.value = shown || '';
  }

  function meterDraw(v) {
    const refs = meter.refs; if (!refs) return;
    // смена колонки (выбор или авто): отметка клипа прежней колонки не обязана гореть
    if (meter.sn !== meter.shown) {
      meter.shown = meter.sn;
      meter.clip = false;
      refs.led.hidden = true;
    }
    // без выбора сервер отвечает колонкой со звуком (или последней говорившей) — принять её
    if (!meter.sn && v.sn) meter.sn = v.sn;
    // метр следует за говорящей колонкой: во время настройки и 15 с после ручного
    // выбора или настройки — нет
    if (!meter.tuning && performance.now() >= meter.pinUntil) {
      const speaking = (v.columns || []).filter(c => c.active);
      if (speaking.length && !speaking.some(c => c.sn === meter.sn)) {
        meter.sn = speaking.sort((a, b) => b.last_sound - a.last_sound)[0].sn;
        meterPoll();  // уровни выбранной колонки придут следующим опросом
        refs.fillControls?.(true);  // колёса и черновик — уже другой колонки
      }
    }
    meterColumns(refs, v.columns, meter.sn);
    if (refs.device.value !== (meter.sn || '')) refs.device.value = meter.sn || '';
    const now = performance.now();
    const last = v.history[v.history.length - 1];
    const recent = last && v.now - last.t >= -0.05 && v.now - last.t <= 0.5;
    const pick = (cur, key) => cur != null ? cur : recent ? last[key] ?? null : null;  // пустое окно — последнее закрытое, если оно свежее
    const outRms = pick(v.out.rms, 'o');
    meterSet(refs.bars.in, pick(v.in.rms, 'i'), pick(v.in.peak, 'ip'), 'in', now);
    meterSet(refs.bars.out, outRms, pick(v.out.peak, 'op'), 'out', now);
    refs.gain.textContent = v.gain == null ? 'усиление —'
      : 'усиление ' + (v.gain > 0 ? '+' : '') + v.gain.toFixed(1) + ' дБ';
    // клип — и в текущем окне, и в закрытых с прошлого опроса (окно могло запечататься
    // между опросами); отметка держится до клика по LED
    const fresh = v.history.filter(e => !meter.lastT[v.sn] || e.t > meter.lastT[v.sn]);
    if (last) meter.lastT[v.sn] = last.t;
    if ((v.in.clip || 0) + (v.out.clip || 0) > 0 || fresh.some(e => e.ic > 0 || e.oc > 0)) {
      meter.clip = true;
      refs.led.hidden = false;
    }
    refs.historyDraw(v);
    refs.status.textContent = meter.holdStatus || meterStatus(v, outRms);
  }

  function meterSync() {
    const wanted = meter.refs?.row.isConnected
      && location.hash === '#voice-stack' && document.visibilityState === 'visible';
    if (wanted && !meter.timer) { meter.timer = setInterval(meterPoll, 100); meterPoll() }
    else if (!wanted && meter.timer) { clearInterval(meter.timer); meter.timer = null }
  }
  document.addEventListener('visibilitychange', meterSync);

  // Настройки stt (метр микрофона и «Эмуляция потока») пишутся точечно: свежие общие
  // настройки, патч stt, PUT — как в «Режимах вывода ответа»; ошибка возвращает поле.
  async function saveStt(patch, revert) {
    try {
      const fresh = await api('settings');
      fresh.stt = { ...(fresh.stt || {}), ...patch };
      cfg = await api('settings', 'PUT', fresh);
    } catch (e) { revert?.(); say(e.message, true) }
  }

  // Колёса выбранной колонки — в её профиль (stt.profiles[sn]): прочие ключи профиля
  // и профили других колонок не трогаются; без выбранной колонки — общие настройки.
  // Возвращает true, если запись прошла.
  async function saveSttProfile(sn, patch, revert) {
    if (!sn) return saveStt(patch, revert);
    try {
      const fresh = await api('settings');
      const profiles = { ...((fresh.stt || {}).profiles || {}) };
      profiles[sn] = { ...(profiles[sn] || {}), ...patch };
      fresh.stt = { ...(fresh.stt || {}), profiles };
      cfg = await api('settings', 'PUT', fresh);
      return true;
    } catch (e) { revert?.(); say(e.message, true); return false }
  }

  // «Эмуляция потокового режима» (settings["stt"]): pseudo_stream — галочка,
  // pseudo_partial_pause / pseudo_end_pause — паузы детектора (видны при включённой
  // галочке); выравнивание уровня микрофона живёт в метре над карточками. Batch-STT
  // не отдаёт частичные результаты — детектор пауз (Silero VAD) распознаёт накопленное
  // по ходу речи, колонка закрывает микрофон в конце фразы, а не по тайм-ауту. Виден
  // только когда активный STT не умеет настоящий поток (streaming === false).
  function pseudoBlock() {
    const box = make('div', 'vs-output');
    const stt = () => (cfg?.stt || {});
    const label = make('label', 'vs-flag'), check = make('input');
    check.type = 'checkbox'; check.checked = stt().pseudo_stream === 'yes';
    label.append(check, make('span', null, 'Эмуляция потокового режима'));
    const pauses = make('div', 'vs-flags');
    const num = (key, title, min, max) => {
      const wrap = make('label', 'vs-flag'), input = make('input');
      input.type = 'number'; input.step = '0.1'; input.min = min; input.max = max;
      input.value = stt()[key] ?? '';
      wrap.append(input, make('span', null, title));
      input.onchange = () => {
        const value = Number(input.value);
        if (input.value.trim() === '' || Number.isNaN(value)) { input.value = stt()[key] ?? ''; return }  // пустое и мусор — вернуться к сохранённому
        saveStt({ [key]: value }, () => { input.value = stt()[key] ?? '' });
      };
      return wrap;
    };
    pauses.append(num('pseudo_partial_pause', 'Пауза между словами, с', 0.2, 2),
                  num('pseudo_end_pause', 'Конец фразы, с', 0.5, 5));
    const sync = () => { pauses.style.display = check.checked ? '' : 'none' };
    check.onchange = () => { sync(); saveStt({ pseudo_stream: check.checked ? 'yes' : 'no' }, () => { check.checked = !check.checked; sync() }) };
    sync();
    box.append(label, pauses, make('small', 'vs-help', 'Пакетный движок распознаёт по паузам в речи: текст приходит по ходу, колонка закрывает микрофон в конце фразы, а не по тайм-ауту. Выключено — распознавание целиком после закрытия микрофона. Уровень микрофона и его усиление — в метре над карточками.'));
    return box;
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
    if (s === 'tts' && cfg) el.append(outputBlock());

    if (adding[s]) addView(s, el);
    else {
      const [kind, id] = [chosen[s].slice(0, 1), chosen[s].slice(2)];
      const preset = kind === 'p' && block.presets.find(p => p.id === id);
      const pkg = kind === 'k' && block.packages.find(p => p.id === id);
      if (preset) presetView(s, preset, el); else if (pkg) packageView(s, pkg, el);
    }
    if (s === 'asr' && block.streaming === false) el.append(pseudoBlock());
    return el;
  }

  function render(force = false) {
    if (!data) {  // до первого ответа и при сбое загрузки — состояние вместо пустой карточки
      const box = make('p', 'vs-state', loadError ? 'Не удалось загрузить: ' + loadError : 'Загружаю голосовой стек…');
      if (loadError) box.append(button('Повторить', 'secondary', load));
      hosts().forEach(h => h.replaceChildren(box));
      meterSync();
      return;
    }
    if (!force && poll && hosts().some(h => h.contains(document.activeElement))) return;  // не сбивать ввод фоновым обновлением
    host.replaceChildren(meterBlock(), ...SERVICES.filter(([s]) => s !== 'llm').map(([s, t]) => card(s, t)));
    llmHost?.replaceChildren(card('llm', 'Модель (LLM)'));
    if (busy) hosts().forEach(h => h.querySelectorAll('button,select,input,textarea').forEach(b => b.disabled = true));
    meterSync();
  }

  const sync = () => { clearTimeout(poll); poll = null;
    if (['#voice-stack', '#llm'].includes(location.hash)) { render(); work(load) } };
  window.addEventListener('hashchange', sync);
  document.addEventListener('vs-cleaned', () => work(load));
  document.addEventListener('DOMContentLoaded', sync);
  if (document.readyState !== 'loading') sync();
})();
