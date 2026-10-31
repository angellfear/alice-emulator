/* Каталог приложений Centaur: свои APK в эмуляторе и при проксировании (v9: фича эмулятора). */
(() => {
  const root = document.getElementById('app-catalog');
  if (!root) return;
  const notice = document.getElementById('notice');
  const say = (text, error) => { notice.textContent = text; notice.className = error ? 'error' : ''; };
  const node = (tag, text) => { const n = document.createElement(tag); if (text != null) n.textContent = text; return n; };
  const sizeText = (v = 0) => v >= 1048576 ? (v / 1048576).toFixed(1) + ' MiB' : v >= 1024 ? (v / 1024).toFixed(1) + ' KiB' : v + ' B';
  let data = null, busy = false, pending = false, drag = false, fileInput = null;

  async function api(path, method = 'GET', value) {
    const headers = { 'X-Emulator': '1' };
    let body;
    if (value !== undefined) {
      if (value instanceof Blob) { headers['Content-Type'] = 'application/vnd.android.package-archive'; body = value; }
      else { headers['Content-Type'] = 'application/json'; body = JSON.stringify(value); }
    }
    const r = await fetch('api/catalog' + (path || ''), { method, headers, body });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  }
  async function run(fn) {
    if (busy) { pending = true; return; }  // переход на вкладку во время операции — перечитать после неё
    busy = true;
    root.querySelectorAll('button').forEach(b => (b.disabled = true));
    try { await fn(); }
    catch (e) { say(e.message, true); if (!data) failed(e.message); }
    finally {
      busy = false; root.querySelectorAll('button').forEach(b => (b.disabled = false));
      if (pending) { pending = false; sync(); }
    }
  }
  // До первого ответа и при его ошибке секция не пустая: заголовок + состояние в самой секции
  const shell = (...rest) => root.replaceChildren(node('h2', 'Каталог приложений'), ...rest);
  function failed(message) {
    const retry = node('button', 'Повторить'); retry.type = 'button'; retry.className = 'secondary';
    retry.onclick = () => run(load);
    shell(node('p', 'Не удалось загрузить каталог: ' + message), retry);
  }

  const iconOf = (app, small) => {
    const box = node('span'); box.className = 'icon' + (small ? ' small' : '');
    if (app.icon) { const img = node('img'); img.src = app.icon; img.alt = ''; img.loading = 'lazy'; img.onerror = () => img.remove(); box.append(img); }
    else box.textContent = '⊞';
    return box;
  };

  function render() {
    root.replaceChildren(node('h2', 'Каталог приложений'), node('p', 'Свои APK в каталоге Centaur — работают в эмуляции и при проксировании. APK разбирается автоматически: название, версия и иконка подтягиваются из манифеста.'));
    const head = node('div'); head.className = 'catalog-head';
    const toggle = node('button'); toggle.type = 'button'; toggle.className = 'catalog-switch' + (data.enabled ? ' enabled' : '');
    toggle.setAttribute('role', 'switch'); toggle.setAttribute('aria-checked', String(!!data.enabled));
    toggle.append(node('i'), node('span', data.enabled ? 'Встраивание включено' : 'Встраивание выключено'));
    toggle.onclick = () => run(async () => { const target = !data.enabled; await api('', 'PUT', { enabled: target }); await load(); say('Встраивание в каталог Centaur ' + (target ? 'включено' : 'выключено') + '.'); });
    head.append(toggle); root.append(head);

    fileInput = node('input'); fileInput.type = 'file'; fileInput.accept = '.apk'; fileInput.multiple = true; fileInput.hidden = true;
    fileInput.setAttribute('aria-label', 'Выбрать APK');
    fileInput.onchange = () => upload(fileInput.files);
    const drop = node('button'); drop.type = 'button'; drop.className = 'catalog-drop';
    drop.append(node('strong', busy ? 'Разбираем APK…' : 'Перетащите APK сюда'), node('span', busy ? drop._progress || '' : 'или нажмите, чтобы выбрать · название, версии и иконка заполнятся автоматически'));
    drop.onclick = () => fileInput.click();
    drop.ondragover = e => { e.preventDefault(); drag = true; drop.classList.add('dragging'); };
    drop.ondragleave = () => { drag = false; drop.classList.remove('dragging'); };
    drop.ondrop = e => { e.preventDefault(); drag = false; drop.classList.remove('dragging'); upload(e.dataTransfer.files); };
    root.append(fileInput, drop);

    const mine = node('div'); mine.className = 'panel';
    const mineHead = node('h3', 'Мои приложения'); mine.append(mineHead);
    const grid = node('div'); grid.className = 'catalog-grid';
    for (const app of data.apps) {
      const card = node('button'); card.type = 'button'; card.className = 'catalog-card'; card.title = app.description || app.name;
      card.append(iconOf(app));
      const info = node('span'); info.className = 'info';
      const name = node('b', app.name); const meta = node('small', app.version + ' · ' + sizeText(app.size)); const pkg = node('small', app.package);
      info.append(name, meta, pkg); card.append(info, node('span', '✎'));
      card.onclick = () => editDialog(app);
      grid.append(card);
    }
    if (!data.apps.length) grid.append(node('p', 'Добавьте APK — его карточка появится здесь.'));
    mine.append(grid); root.append(mine);

    const observed = node('div'); observed.className = 'panel';
    observed.append(node('h3', 'Каталог из собранного трафика'), node('p', 'Приложения из последнего захваченного ответа get_app_catalog (доступно, когда эмулятор встроен в прокси с журналом).'));
    if (data.observed && data.observed.length) {
      for (const group of data.observed) {
        observed.append(node('h4', (group.title || 'Раздел') + ' · ' + (group.apps || []).length));
        const og = node('div'); og.className = 'catalog-grid';
        for (const app of group.apps || []) {
          const a = node('a'); a.className = 'catalog-card'; a.href = app.apk_url || '#'; a.title = app.description || app.name;
          if (!app.apk_url) a.removeAttribute('href');
          a.append(iconOf(app, true));
          const info = node('span'); info.className = 'info';
          info.append(node('b', app.name), node('small', app.version || ''));
          a.append(info);
          og.append(a);
        }
        observed.append(og);
      }
    } else observed.append(node('p', 'Пока нет сохранённого ответа get_app_catalog.'));
    root.append(observed);
  }

  async function upload(files) {
    if (!files || !files.length) return;
    const apks = [...files];
    if (apks.some(f => !f.name.toLowerCase().endsWith('.apk'))) { say('Выберите APK-файлы', true); return; }
    await run(async () => {
      for (let i = 0; i < apks.length; i++) { say(`Разбираем ${i + 1} / ${apks.length} · ${apks[i].name}`); await api('/apk', 'POST', apks[i]); }
      await load();
      say('Загружено APK: ' + apks.length + '. Карточки обновлены.');
      if (fileInput) fileInput.value = '';
    });
  }

  function editDialog(app) {
    const dialog = node('dialog'); dialog.className = 'catalog-dialog';
    const form = node('form');
    const head = node('h3', 'Редактировать приложение'); form.append(head);
    const summary = node('div'); summary.className = 'catalog-card'; summary.style.cursor = 'default';
    summary.append(iconOf(app));
    const sinfo = node('span'); sinfo.className = 'info'; sinfo.append(node('b', app.name), node('small', sizeText(app.size) + ' · APK'));
    summary.append(sinfo); form.append(summary);
    const fields = {};
    for (const [key, title, type] of [['name', 'Название', 'text'], ['package', 'Package', 'text'], ['version', 'Версия', 'text'], ['version_code', 'VersionCode', 'number'], ['icon', 'Иконка · URL', 'text']]) {
      const label = node('label', title); const input = node('input'); input.type = type; input.value = app[key] ?? '';
      if (key === 'name') input.required = true;
      label.append(input); form.append(label); fields[key] = input;
    }
    const dlabel = node('label', 'Описание'); const dtext = node('textarea'); dtext.rows = 3; dtext.value = app.description || '';
    dlabel.append(dtext); form.append(dlabel);
    const row = node('div'); row.className = 'row';
    const del = node('button', 'Удалить'); del.type = 'button'; del.className = 'secondary';
    del.onclick = () => run(async () => { await api('/apps/' + app.id, 'DELETE'); dialog.close(); dialog.remove(); await load(); say('Приложение удалено из каталога.'); });
    const dl = node('a', 'Скачать APK'); dl.className = 'button secondary'; dl.href = '/__catalog/apk/' + app.sha256;
    const save = node('button', 'Сохранить'); save.type = 'submit';
    row.append(del, dl, save); form.append(row);
    form.onsubmit = e => {
      e.preventDefault();
      run(async () => {
        await api('/apps/' + app.id, 'PUT', { name: fields.name.value, package: fields.package.value, version: fields.version.value, version_code: Number(fields.version_code.value), icon: fields.icon.value, description: dtext.value });
        dialog.close(); dialog.remove(); await load(); say('Приложение сохранено.');
      });
    };
    dialog.append(form);
    dialog.addEventListener('cancel', () => dialog.remove());
    document.body.append(dialog); dialog.showModal();
  }

  async function load() { data = await api(''); render(); }
  function sync() {
    if (location.hash !== '#app-catalog') return;
    if (!data && !busy) shell(node('p', 'Загрузка…'));
    run(async () => { await load(); say('Готово'); });
  }
  window.addEventListener('hashchange', sync);
  sync();
})();
