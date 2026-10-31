/* Голосовой стек: карточка на сервис (ASR / TTS / LLM).
   Список — пресеты (адаптер + настройки) и пакеты (установка сервера на этот ПК); под ним сразу
   настройки выбранного. «Добавить» → выбор адаптера → поля → новый пресет.
   Адаптеры — server_emulator/voice_stack/adapters/*.py, пакеты — voice_stack/packages/*.json,
   API — ui/api/voice_stack.py. */
(() => {
  const host = document.getElementById('voice-stack-cards');
  if (!host) return;
  const llmHost = document.getElementById('llm-cards');  // 'Модель (LLM)' живёт в подвкладке «Интеллект → Модель»
  const hosts = () => [host, llmHost].filter(Boolean);

  const SERVICES = [['asr', 'Распознавание (ASR)'], ['tts', 'Синтез (TTS)'], ['llm', 'Модель (LLM)']];
  const STATE = { absent: 'Не установлен', installing: 'Установка', running: 'Работает', starting: 'Запускается…', stopped: 'Остановлен', error: 'Ошибка установки', unsupported: 'Не поддерживается на этой ОС' };
  const KEEP = '__KEEP__';
  const make = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n };
  const say = (text, error = false) => { const n = document.getElementById('notice'); if (n) { n.textContent = text; n.className = error ? 'error' : '' } };
  const api = async (action, method = 'GET', value) => {
    const r = await fetch('api/' + action, { method, headers: { 'Content-Type': 'application/json', 'X-Emulator': '1' }, body: value === undefined ? undefined : JSON.stringify(value) });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  };

  let data = null, poll = null, busy = false;
  const chosen = {};  // service → "p:<пресет>" | "k:<пакет>"
  const adding = {};  // service → id адаптера в режиме «Добавить» (null — обычный режим)
  const drafts = {};  // ключ формы → {title, values}: введённое, но не сохранённое

  async function work(fn) {
    if (busy) return; busy = true;
    hosts().forEach(h => h.querySelectorAll('button,select,input,textarea').forEach(b => b.disabled = true));
    try { await fn() } catch (e) { say(e.message, true) } finally { busy = false; render(true) }
  }
  const button = (text, cls, fn) => { const b = make('button', cls, text); b.type = 'button'; b.onclick = () => work(fn); return b };

  const refs = s => [...data[s].presets.map(p => 'p:' + p.id), ...data[s].packages.map(p => 'k:' + p.id)];

  const statusKey = d => JSON.stringify(SERVICES.map(([s]) => d[s].packages.map(p => [p.id, p.state.status, p.state.outdated])));

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

  // поле-файл пакета (эталон WAV): загрузка с проверкой, прослушивание, удаление; один файл на поле
  function fileInput(spec, pkg, values) {
    const box = make('div', 'vs-field vs-file'), info = pkg.files?.[spec.key];
    const pt = spec.format === 'pt', ext = pt ? '.pt' : '.wav';
    const url = 'api/voice-stack-file?id=' + encodeURIComponent(pkg.id) + '&key=' + spec.key;
    box.append(make('span', null, spec.label));
    const picker = make('input'); picker.type = 'file'; picker.accept = spec.accept || ext; picker.hidden = true;
    picker.onchange = () => work(async () => {
      const file = picker.files[0]; if (!file) return;
      if (!file.name.toLowerCase().endsWith(ext)) throw Error('Нужен файл ' + ext);
      if (file.size > 15 * 1024 * 1024) throw Error('Файл больше 15 МБ');
      const bytes = new Uint8Array(await file.arrayBuffer());
      const tag = i => String.fromCharCode(...bytes.slice(i, i + 4));
      if (!pt && (tag(0) !== 'RIFF' || tag(8) !== 'WAVE')) throw Error('Это не WAV (нет заголовка RIFF/WAVE)');
      const r = await fetch(url, { method: 'POST', headers: { 'X-Emulator': '1', 'Content-Type': pt ? 'application/octet-stream' : 'audio/wav' }, body: bytes });
      const v = await r.json().catch(() => ({})); if (!r.ok) throw Error(v.error || r.statusText);
      say('Загружен: ' + file.name + (pt ? '' : ' · ' + v.seconds + ' с')); await load();
    });
    const row = make('div', 'vs-file-row');
    if (info && !info.error) {
      row.append(make('span', 'vs-file-name', info.name + ' · ' + (pt ? info.kb + ' КБ' : info.seconds + ' с')));
      if (!pt) {
        const audio = make('audio'); audio.controls = true; audio.preload = 'none';
        audio.src = url + '&v=' + encodeURIComponent(info.name + info.seconds);
        box.append(row, audio);
      } else box.append(row);
      if (spec.transcribe_to && values) row.append(button('Распознать текст', 'secondary', async () => {
        const r = await api('voice-stack-transcribe?id=' + encodeURIComponent(pkg.id) + '&key=' + spec.key, 'POST');
        values[spec.transcribe_to] = r.text;
        const target = box.closest('.vs-fields')?.querySelector('[data-key="' + spec.transcribe_to + '"]');
        if (target) target.value = r.text;
        say('Текст эталона распознан — проверьте и сохраните настройки');
      }));
      row.append(Object.assign(make('button', 'secondary', 'Заменить'), { type: 'button', onclick: () => picker.click() }),
        button('Удалить', 'danger', async () => { await api(url, 'DELETE'); await load() }));
    } else {
      if (info?.error) row.append(make('span', 'error', info.error));
      row.append(Object.assign(make('button', 'secondary', pt ? 'Загрузить .pt' : 'Загрузить WAV'), { type: 'button', onclick: () => picker.click() }));
      box.append(row);
    }
    box.append(picker);
    return box;
  }

  function input(spec, values) {
    const label = make('label', 'vs-field', spec.label);
    let el;
    if (spec.type === 'select') {
      el = make('select');
      for (const o of spec.options || []) el.append(typeof o === 'object' ? new Option(o.label, o.value) : new Option(o, o));
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
    else el.value = initial ?? '';
    el.oninput = () => { values[spec.key] = spec.type === 'password' && !el.value && initial === KEEP ? KEEP : el.value };
    el.onchange = () => { el.oninput(); if (spec.type === 'select') render(true) };  // поля с when зависят от выбора
    label.append(el);
    return label;
  }
  const shown = (f, values) => Object.entries(f.when || {}).every(([k, v]) => String(values[k]) === String(v));
  const form = (fields, values, pkg) => { const box = make('div', 'vs-fields'); fields.filter(f => shown(f, values)).forEach(f => box.append(f.type === 'file' && pkg ? fileInput(f, pkg, values) : input(f, values))); return box };
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
    el.append(input({ key: 'title', label: 'Название' }, d), form(adapter.fields, d.values));
    const actions = make('div', 'vs-actions');
    actions.append(button('Создать', null, async () => {
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
    const d = draft(key, () => ({ title: preset.title, values: Object.fromEntries(adapter.fields.map(f => [f.key, preset.values[f.key] ?? adapter.defaults[f.key] ?? ''])) }));
    el.append(input({ key: 'title', label: 'Название' }, d), form(adapter.fields, d.values));
    const save = async () => { await api('voice-stack-preset', 'POST', { service: s, id: preset.id, adapter: preset.adapter, title: d.title, values: d.values }); delete drafts[key] };
    const actions = make('div', 'vs-actions');
    if (!active) actions.append(button('Применить', null, async () => { await save(); await api('voice-stack-apply', 'POST', { service: s, ref }); await load(); say('Применён: ' + d.title) }));
    actions.append(button('Сохранить', active ? null : 'secondary', async () => { await save(); await load(); say('Сохранено: ' + d.title) }));
    if (!active) actions.append(button('Удалить', 'danger', async () => {
      if (!confirm('Удалить пресет «' + preset.title + '»?')) return;
      await api('voice-stack-preset?service=' + s + '&id=' + encodeURIComponent(preset.id), 'DELETE');
      delete drafts[key]; chosen[s] = ''; await load();
    }));
    top(el, actions);
  }

  // --- пакет -------------------------------------------------------------------------------
  // блок установки/статуса пакета; фоновый опрос заменяет только его (форма и списки не трогаются)
  // Проба голоса: синтез фиксированной фразы источником без звукового кеша
  const tester = new Audio();
  function testButton(ref) {
    const b = make('button', 'sc-icon vs-test'); b.type = 'button'; b.title = 'Проверить голос'; b.setAttribute('aria-label', 'Проверить голос');
    b.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M4 2.5v11l9-5.5z" fill="currentColor"/></svg>';
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
        if (active && !confirm(pkg.title + ' применён — без сервера голос не будет работать. Остановить?')) return;
        await api('voice-stack-stop', 'POST', { id: pkg.id }); await load(); say('Остановлен: ' + pkg.title);
      }));
      const remove = button('Удалить установку', 'danger', async () => {
        if (!confirm('Удалить установку ' + pkg.title + ' (окружение и модели)? Настройки останутся.')) return;
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
    el.append(statusBox(s, pkg), form(pkg.fields, d.values, pkg));

    const actions = make('div', 'vs-actions');
    if (!active && ['running', 'stopped', 'starting'].includes(st.status))
      actions.append(button('Применить', null, async () => { await saveOptions(); await api('voice-stack-apply', 'POST', { service: s, ref }); await load(); say('Применён: ' + pkg.title) }));
    actions.append(button('Сохранить настройки', 'secondary', async () => { await saveOptions(); await load(); say('Настройки сохранены: ' + pkg.title) }));
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
