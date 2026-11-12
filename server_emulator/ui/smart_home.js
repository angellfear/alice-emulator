/* Вкладка «Умный дом»: реестр устройств из расширений — просмотр (изменяют расширения SDK).
   API — ui/api/smart_home.py: {devices: [{id, name, room, origin, online, …}], rooms: {id: имя}}. */
(() => {
  const list = document.getElementById('smart-home-list');
  if (!list) return;
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n };
  const render = data => {
    const rooms = data.rooms || {}, devices = data.devices || [];
    if (!devices.length) { list.replaceChildren(el('p', 'hint', 'Устройств нет.')); return; }
    const table = el('table', 'dev-table');
    const head = el('tr');
    for (const title of ['Устройство', 'Комната', 'Источник']) head.append(el('th', null, title));
    const thead = el('thead'); thead.append(head);
    const body = el('tbody');
    for (const d of devices) {
      const name = el('td');
      name.append(el('b', null, d.name || d.id));
      const extra = [d.id];
      if (!d.online) extra.push('не в сети');
      name.append(el('small', null, extra.join(' · ')));
      const tr = el('tr');
      tr.append(name, el('td', null, rooms[d.room] || d.room || '—'), el('td', null, d.origin || '—'));
      body.append(tr);
    }
    table.append(thead, body);
    list.replaceChildren(table);
  };
  const load = async () => {
    try {
      const r = await fetch('api/smart_home', { headers: { 'X-Emulator': '1' } });
      const v = await r.json().catch(() => ({}));
      if (!r.ok) throw Error(v.error || r.statusText);
      render(v);
    } catch (e) { list.replaceChildren(el('p', 'hint', 'Реестр не загрузился: ' + e.message)); }
  };
  window.addEventListener('hashchange', () => { if (location.hash === '#smart-home') load(); });
  if (location.hash === '#smart-home') load();
})();
