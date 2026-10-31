// Раздел «Эмулятор → Без ответа»: всё, на что у эмулятора не нашлось обработчика — фразы без сценария
// (в любом режиме «Интеллект»), служебные колбэки @@mm_* без protocol_event-сценария, WS AAPI-сообщения
// без ответа и HTTP-запросы без эндпоинта. Из записи создаётся обработчик: сценарий
// (window.newScenarioFromPhrase / newScenarioFromEvent, app.js) или эндпоинт (window.openEndpointFromMiss).
// API — ui/api/misses.py.
(() => {
  const root = document.getElementById('misses');
  if (!root) return;

  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (text, onclick, cls = 'secondary') => { const b = node('button', text, cls); b.type = 'button'; b.onclick = onclick; return b; };
  const when = ts => ts ? new Date(ts * 1000).toLocaleString('ru-RU') : '';
  const notice = (text, error = false) => { notify(text, { error }); };
  async function api(path, method = 'GET') {
    const r = await fetch('api/' + path, { method, headers: { 'X-Emulator': '1' } });
    const v = await r.json().catch(() => ({})); if (!r.ok) throw Error(v.error || `${r.status} ${r.statusText}`); return v;
  }
  async function work(fn) { try { await fn(); } catch (e) { notice(e.message, true); } }
  const forget = (kind, key) => work(async () => { await api(`misses?kind=${kind}&key=${encodeURIComponent(key)}`, 'DELETE'); await load(); });

  function section(title, kind, items, empty, line) {
    const box = node('div', null, 'ep-misses');
    const head = node('div', null, 'sc-group-head');
    head.append(node('span', title, 'sc-title'), node('span', String(items.length), 'count'));
    if (items.length) head.append(button('Очистить', () => forget(kind, '*')));
    box.append(head);
    if (!items.length) box.append(node('p', empty, 'hint'));
    for (const item of items) {
      const row = node('div', null, 'ep-row');
      const hits = node('span', '×' + item.count, 'count'); hits.title = 'Последний: ' + when(item.last_ts);
      const drop = button('✕', () => forget(kind, item.key), 'secondary ep-drop'); drop.title = 'Убрать из списка';
      row.append(...line(item), hits, drop);
      box.append(row);
    }
    return box;
  }

  function phraseLine(m) {
    const add = button('+', () => window.newScenarioFromPhrase?.(m.examples[0] || m.phrase), 'ep-add'); add.title = 'Создать сценарий';
    const text = node('span', m.examples[0] || m.phrase, 'ep-url');
    if (m.sns?.length) text.title = 'Колонки: ' + m.sns.join(', ');
    return [add, text];
  }

  function callbackLine(m) {
    const add = button('+', () => window.newScenarioFromEvent?.(m.key), 'ep-add'); add.title = 'Создать сценарий на этот колбэк';
    const name = node('span', m.key, 'ep-url'); name.title = m.examples[0] || '';
    return [add, name];
  }

  function httpLine(m) {
    const add = button('+', () => window.openEndpointFromMiss?.(m), 'ep-add'); add.title = 'Создать эндпоинт';
    const url = node('span', null, 'ep-url'); url.append(node('b', m.method), document.createTextNode(' ' + m.host + m.path));
    if (m.forwarded) {  // гибрид (TASK-044): промах ушёл в Яндекс, локального обработчика можно не создавать
      const chip = node('span', m.forwarded === 'upstream' ? '→ Яндекс' : '⚠ Яндекс недоступен', 'chip');
      chip.title = m.forwarded === 'upstream' ? 'Гибрид: запрос переслан наверх. Открыть «Журнал» (запись — опция «Запись» устройства)' : ('Пересылка не удалась: ' + m.forwarded);
      if (m.forwarded === 'upstream') { chip.style.cursor = 'pointer'; chip.onclick = () => window.openTraffic?.({ unhandled: true }); }
      url.append(chip);
    }
    return [add, url];
  }

  async function load() {
    const data = await api('misses');
    const head = node('div', null, 'sc-toolbar');
    head.append(node('h2', 'Без ответа'), button('Обновить', () => work(load)));
    root.replaceChildren(head,
      section('Фразы без сценария', 'phrase', data.phrases, 'Все фразы взяли сценарии.', phraseLine),
      section('Колбэки без сценария', 'callback', data.callbacks, 'Все служебные колбэки взяли сценарии.', callbackLine),
      section('HTTP без эндпоинта', 'http', data.http, 'Запросов без обработчика пока не было.', httpLine));
  }

  // Ошибка загрузки — в самой секции: пустой раздел не должен выглядеть как «промахов нет»
  const failed = e => { const head = node('div', null, 'sc-toolbar'); head.append(node('h2', 'Без ответа'), button('Повторить', () => work(load)));
    root.replaceChildren(head, node('p', 'Не удалось загрузить: ' + e.message, 'hint')); };
  const sync = () => { if (location.hash === '#misses') load().catch(failed); };
  window.addEventListener('hashchange', sync);
  sync();
})();
