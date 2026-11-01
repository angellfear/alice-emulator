// Раздел «Голос → Медиа» (TASK-071): библиотека музыки и сказок (state/media/<kind>,
// подпапки — плейлисты) и интернет-радио (настройки, «включи радио <имя>»).
// Файлы колонке раздаёт /media/… (Range поддержан). API — ui/api/media.py.
(() => {
  const root = document.getElementById('media');
  if (!root) return;

  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (text, onclick, cls = 'secondary') => { const b = node('button', text, cls); b.type = 'button'; b.onclick = onclick; return b; };
  const notice = (text, error = false) => { notify(text, { error }); };
  async function api(path, method = 'GET', value) {
    const r = await fetch('api/' + path, { method, headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' },
                                           body: value === undefined ? undefined : JSON.stringify(value) });
    const v = await r.json().catch(() => ({})); if (!r.ok) throw Error(v.error || `${r.status} ${r.statusText}`); return v;
  }
  async function work(fn) { try { await fn(); } catch (e) { notice(e.message, true); } }

  const KINDS = [['music', 'Музыка'], ['tales', 'Сказки'], ['sounds', 'Звуки']];
  let kind = 'music';

  // --- библиотека ------------------------------------------------------------
  const fileRow = name => {
    const row = node('div', null, 'ep-row');
    row.append(node('span', name, 'tr-item'));
    row.append(button('Удалить', () => work(async () => {
      await api('media?kind=' + kind + '&name=' + encodeURIComponent(name), 'DELETE');
      notice('Удалено: ' + name);
      await load();
    }, 'secondary danger')), );
    return row;
  };

  const libraryPanel = data => {
    const box = node('div');
    const view = data[kind];
    const bar = node('div', null, 'sc-toolbar');
    const pick = node('input'); pick.type = 'file'; pick.hidden = true;
    pick.onchange = () => work(async () => {
      const file = pick.files[0];
      if (!file) return;
      const r = await fetch('api/media/upload?kind=' + kind, { method: 'POST', headers: { 'X-Emulator': '1', 'X-Filename': encodeURIComponent(file.name) }, body: file });
      const v = await r.json().catch(() => ({}));
      if (!r.ok) throw Error(v.error || r.statusText);
      notice('Загружено: ' + v.saved);
      await load();
    });
    bar.append(node('span', view.count + ' файл(ов) · папки: ' + (view.folders.join(', ') || '—'), 'hint'),
               button('Загрузить файл', () => pick.click()), pick);
    box.append(bar);
    if (!view.count) box.append(node('p', 'Файлов нет. Сложите mp3/ogg/m4a в state/media/' + kind + ' (подпапка — плейлист) или загрузите кнопкой.', 'hint'));
    const folders = new Map();
    for (const file of view.files) {
      const folder = file.name.includes('/') ? file.name.split('/')[0] : '';
      if (!folders.has(folder)) folders.set(folder, []);
      folders.get(folder).push(file.name);
    }
    for (const [folder, names] of folders) {
      const details = node('details', null, 'misses-block');
      const summary = node('summary');
      summary.append(node('b', folder || 'Файлы'), node('span', '×' + names.length, 'count'));
      details.append(summary);
      for (const name of names) details.append(fileRow(name));
      box.append(details);
    }
    return box;
  };

  // --- радио -----------------------------------------------------------------
  const radioPanel = data => {
    const box = node('div');
    const rows = node('div');
    const stations = data.radio || [];
    const render = list => {
      rows.replaceChildren();
      for (const [index, station] of list.entries()) {
        const row = node('div', null, 'ep-row');
        const name = node('input'); name.value = station.name || ''; name.placeholder = 'Название';
        const url = node('input'); url.value = station.url || ''; url.placeholder = 'http://…/stream';
        url.style.flex = '1';
        const remove = button('✕', () => { list.splice(index, 1); render(list); }, 'secondary danger');
        row.append(name, url, remove);
        rows.append(row);
      }
      if (!list.length) rows.append(node('p', 'Станций нет. Колонка понимает «включи радио <имя>».', 'hint'));
    };
    const list = stations.map(s => ({ name: s.name || '', url: s.url || '' }));
    render(list);
    const add = button('Добавить станцию', () => { list.push({ name: '', url: '' }); render(list); });
    const save = button('Сохранить радио', () => work(async () => {
      const value = [...rows.querySelectorAll('.ep-row')].map(row => ({
        name: row.querySelector('input').value.trim(),
        url: row.querySelectorAll('input')[1].value.trim() })).filter(s => s.name && s.url);
      await api('media', 'PUT', { radio: value });
      notice('Список радио сохранён.');
    }));
    save.classList.remove('secondary');
    box.append(node('div', null, 'sc-toolbar'), rows, node('div', null, 'actions'), add, save);
    return box;
  };

  async function load() {
    const data = await api('media');
    const head = node('div', null, 'sc-toolbar');
    head.append(node('h2', 'Медиа'));
    const tabs = node('div', null, 'segmented');
    for (const [value, title] of KINDS) {
      const b = node('button', title); b.type = 'button'; b.className = 'secondary';
      b.setAttribute('role', 'radio'); b.setAttribute('aria-checked', String(kind === value));
      b.onclick = () => { kind = value; load(); };
      tabs.append(b);
    }
    head.append(tabs);
    const note = node('p', 'Файлы из state/media раздаются колонке как «включи музыку / включи сказку» (адрес /media/, перемотка — Range). Радио — потоки из настроек: «включи радио <имя>».', 'hint');
    root.replaceChildren(head, note, libraryPanel(data), node('h3', 'Интернет-радио'), radioPanel(data));
  }

  const sync = () => { if (location.hash === '#media') load().catch(e => root.replaceChildren(node('h2', 'Медиа'), node('p', e.message, 'sc-err'))); };
  window.addEventListener('hashchange', sync);
  sync();
})();
