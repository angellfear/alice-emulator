// Первый запуск: окно «Настройка эмулятора» (<dialog> в index.html) предлагает 1) скачать картинки устройств
// с сайта умного дома Яндекса — только после галочки «ознакомлен, права принадлежат Яндексу»; 2) загрузить
// протокол — libquasar_daemons.so станции или zip с .proto / .pb (разбирается локально). Окно открывается само,
// пока нет протокола, или пока нет картинок и пользователь не закрыл окно в этом браузере; без протокола
// остаётся плашка «Нужна настройка». Состояние и повторная загрузка — в «Дополнительно → Настройка».
// API — ui/api/setup.py: GET api/setup, POST api/setup/protocol (multipart), POST api/setup/images {accept}.
(() => {
  const $ = id => document.getElementById(id);
  const dialog = $('setup-dialog');
  const section = $('setup');
  if (!dialog || !section) return;
  const SKIP = 'setup-images-skip';
  let state = null, busy = false, imagesChanged = false;

  const make = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const say = (text, error = false) => { notify(text, { error }); };
  const stored = key => { try { return localStorage.getItem(key); } catch (e) { return null; } };
  const store = (key, value) => { try { if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (e) { /* нет хранилища */ } };
  const json = async r => {
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  };
  const read = async () => json(await fetch('api/setup', { headers: { 'X-Emulator': '1' } }));
  // XHR ради прогресса отправки: fetch его не отдаёт
  const upload = (file, progress) => new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', 'api/setup/protocol');
    xhr.setRequestHeader('X-Emulator', '1');
    xhr.upload.onprogress = e => { if (e.lengthComputable) progress(e.loaded / e.total); };
    xhr.upload.onload = () => progress(1);
    xhr.onload = () => {
      let value = {};
      try { value = JSON.parse(xhr.responseText); } catch (e) { /* не JSON */ }
      if (xhr.status < 300) resolve(value); else reject(Error(value.error || xhr.statusText || 'Ошибка ' + xhr.status));
    };
    xhr.onerror = () => reject(Error('Нет связи с сервером'));
    xhr.send(form);
  });

  const imagesText = () => 'Картинки устройств: ' + (state ? state.images + ' из ' + state.images_total : '…') + (state && state.images_busy ? ' (загрузка…)' : '');
  const SOURCES = { library: 'из libquasar_daemons.so', zip: 'из zip' };
  const protocolText = () => {
    if (!state) return 'Протокол станции: …';
    if (state.schemas_error) return 'Протокол станции: повреждён — загрузите заново (' + state.schemas_error + ')';
    if (!state.schemas) return 'Протокол станции: не загружен';
    const when = state.protocol_imported ? new Date(state.protocol_imported * 1000).toLocaleDateString() : '';
    return 'Протокол станции: загружен' + [SOURCES[state.protocol_source] || '', when, state.protocol_files ? 'файлов ' + state.protocol_files : '']
      .filter(Boolean).map((part, i) => (i ? ' · ' : ' ') + part).join('');
  };

  function renderSection() {
    const box = make('div', null, 'panel setup-state');
    const protocol = make('div', null, 'row');
    protocol.append(make('span', protocolText()));
    const load = make('button', state && state.schemas ? 'Загрузить заново' : 'Загрузить протокол');
    load.type = 'button'; if (state && state.schemas) load.className = 'secondary';
    load.onclick = () => openDialog();
    protocol.append(load);
    const pictures = make('div', null, 'row');
    pictures.append(make('span', imagesText()));
    const images = make('button', state && state.images ? 'Обновить картинки' : 'Загрузить картинки');
    images.type = 'button'; images.className = 'secondary';
    images.onclick = () => openDialog();
    pictures.append(images);
    box.append(protocol, pictures);
    section.replaceChildren(make('h2', 'Настройка'),
      make('p', 'Протокол Алисы эмулятор разбирает из libquasar_daemons.so станции или zip с .proto / .pb — локально, файл никуда не отправляется. Картинки устройств скачиваются с сайта умного дома Яндекса по вашему согласию; без них показываются силуэты.', 'hint'),
      box);
  }

  function render() {
    $('setup-banner').hidden = !state || state.schemas;
    $('setup-images-state').textContent = imagesText();
    $('setup-protocol-state').textContent = protocolText();
    $('setup-images-load').textContent = state && state.images ? 'Обновить картинки' : 'Загрузить картинки';
    $('setup-images-load').disabled = busy || !$('setup-images-accept').checked;
    renderSection();
  }

  function fail(text) { const n = $('setup-error'); n.textContent = text; n.hidden = !text; }
  function progress(step, fraction) {
    $('setup-progress').hidden = !step;
    $('setup-step').textContent = step || '';
    const bar = $('setup-bar');
    if (fraction == null) bar.removeAttribute('value'); else bar.value = Math.round(fraction * 100);
  }
  function lock(on) {
    busy = on;
    for (const id of ['setup-protocol', 'setup-submit', 'setup-close', 'setup-images-accept']) $(id).disabled = on;
    $('setup-images-load').disabled = on || !$('setup-images-accept').checked;
    for (const button of section.querySelectorAll('button')) button.disabled = on;
  }
  // страница уже собрала картинки и данные без протокола/картинок: перечитываем её целиком, сервер не перезапускается
  function reload(text) {
    try { sessionStorage.setItem('setup-done', text); } catch (e) { /* без хранилища — просто перезагрузка */ }
    say(text);
    location.reload();
  }

  function openDialog() {
    if (busy || dialog.open) return;
    fail(''); progress('');
    $('setup-form').reset();
    render();
    dialog.showModal();
  }

  async function refresh() {
    try { state = await read(); } catch (e) { return; }
    render();
  }

  // картинки — только с галочкой согласия; нет сети — ошибка в окне, интерфейс остаётся с силуэтами
  async function loadImages() {
    if (busy || !$('setup-images-accept').checked) return;
    lock(true);
    fail(''); progress('Скачивание картинок с сайта умного дома…', null);
    try {
      const result = await json(await fetch('api/setup/images', {
        method: 'POST', headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ accept: true }) }));
      state = { ...state, ...result };
      progress('');
      const text = 'Картинки устройств: ' + result.images + ' из ' + result.images_total + '.';
      // протокол ещё не загружен — окно остаётся открытым со следующим шагом; страница перечитается после него
      if (!state.schemas) { imagesChanged = true; say(text + ' Осталось загрузить протокол.'); render(); return; }
      reload(text);
    } catch (e) {
      progress('');
      fail(e.message);
      await refresh();
    } finally {
      lock(false);
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    const file = $('setup-protocol').files[0];
    fail('');
    if (!file) return fail('Выберите libquasar_daemons.so или zip-архив');
    lock(true);
    try {
      progress('Загрузка: ' + file.name + '…', 0);
      const result = await upload(file, f => progress('Загрузка: ' + file.name + ' — ' + Math.round(f * 100) + '%', f));
      progress('Разбор протокола…', null);
      state = { ...state, ...result };
      progress('');
      dialog.close();
      reload('Протокол загружен' + (result.protocol_files ? ': файлов ' + result.protocol_files : '') + '.');
    } catch (e) {
      progress('');
      fail(e.message);
    } finally {
      lock(false);
    }
  }

  $('setup-form').onsubmit = submit;
  $('setup-close').onclick = () => dialog.close();
  $('setup-images-load').onclick = () => loadImages();
  $('setup-images-accept').onchange = () => { $('setup-images-load').disabled = busy || !$('setup-images-accept').checked; };
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  // закрыл окно без картинок — больше не открываем его само ради них (в этом браузере)
  dialog.addEventListener('close', () => {
    if (state && !state.images) store(SKIP, '1');
    // картинки скачаны, а протокол так и не загружен: страница перечитывается, чтобы показать картинки
    if (imagesChanged && !busy) { imagesChanged = false; reload('Картинки устройств: ' + state.images + ' из ' + state.images_total + '.'); }
  });
  $('setup-open').onclick = () => openDialog();
  try { const text = sessionStorage.getItem('setup-done'); if (text) { sessionStorage.removeItem('setup-done'); say(text); } } catch (e) { /* нет хранилища */ }
  render();
  refresh().then(() => {
    if (!state) return;
    if (state.images) store(SKIP, null);
    if (!state.schemas || (!state.images && !stored(SKIP))) openDialog();
  });
})();
