// Промахи эмулятора — свёрнутые блоки в своих разделах (TASK-061, вкладки «Без ответа» нет):
// «Сценарии» — фразы без сценария, «HTTP» — запросы без эндпоинта (с чипом «→ Яндекс» гибрида),
// «WS» — AAPI без ответа и колбэки без обработчика (TASK-058). Из записи создаётся обработчик:
// сценарий (window.newScenarioFromPhrase / newScenarioFromEvent, app.js) или эндпоинт
// (window.openEndpointFromMiss). Компонента одна: раздел передаёт контейнер и виды промахов —
// window.renderMisses(container, kinds). API — ui/api/misses.py (один маршрут на все виды).
(() => {
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (text, onclick, cls = 'secondary') => { const b = node('button', text, cls); b.type = 'button'; b.onclick = onclick; return b; };
  const when = ts => ts ? new Date(ts * 1000).toLocaleString('ru-RU') : '';
  async function api(path, method = 'GET') {
    const r = await fetch('api/' + path, { method, headers: { 'X-Emulator': '1' } });
    const v = await r.json().catch(() => ({})); if (!r.ok) throw Error(v.error || `${r.status} ${r.statusText}`); return v;
  }
  async function work(fn) { try { await fn(); } catch (e) { notify(e.message, { error: true }); } }
  const forget = (kind, key, refresh) => work(async () => { await api(`misses?kind=${kind}&key=${encodeURIComponent(key)}`, 'DELETE'); await refresh(); });

  function phraseLine(m, refresh) {
    const add = button('+', () => window.newScenarioFromPhrase?.(m.examples[0] || m.phrase), 'ep-add'); add.title = 'Создать сценарий';
    const text = node('span', m.examples[0] || m.phrase, 'ep-url');
    if (m.sns?.length) text.title = 'Колонки: ' + m.sns.join(', ');
    return [add, text];
  }

  function callbackLine(m, refresh) {
    const add = button('+', () => window.newScenarioFromEvent?.(m.key), 'ep-add'); add.title = 'Создать сценарий на этот колбэк';
    const name = node('span', m.key, 'ep-url'); name.title = m.examples[0] || '';
    return [add, name];
  }

  function aapiLine(m, refresh) {  // обработчика из UI не создать — нужен код (voice.py / расширение TASK-059); образец — в подсказке
    const name = node('span', m.key, 'ep-url'); name.title = m.examples[0] || '';
    return [node('span', null, 'ep-add'), name];
  }

  function httpLine(m, refresh) {
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

  const KINDS = {
    phrase:   {key: 'phrases',  title: 'Фразы без сценария',   empty: 'Все фразы взяли сценарии.',                   line: phraseLine},
    callback: {key: 'callbacks', title: 'Колбэки без сценария', empty: 'Все служебные колбэки взяли сценарии.',        line: callbackLine},
    aapi:     {key: 'aapi',     title: 'AAPI без ответа',      empty: 'Все сообщения колонок обработаны.',            line: aapiLine},
    http:     {key: 'http',     title: 'HTTP без эндпоинта',   empty: 'Запросов без обработчика пока не было.',       line: httpLine},
  };

  function block(kind, items, refresh) {
    const meta = KINDS[kind];
    const box = node('details', null, 'ep-misses misses-block');
    const summary = node('summary', null, 'sc-group-head');
    summary.append(node('span', meta.title, 'sc-title'), node('span', String(items.length), 'count'));
    if (items.length) summary.append(button('Очистить', (e) => { e.stopPropagation(); forget(kind, '*', refresh); }));
    box.append(summary);
    if (!items.length) box.append(node('p', meta.empty, 'hint'));
    for (const item of items) {
      const row = node('div', null, 'ep-row');
      const hits = node('span', '×' + item.count, 'count'); hits.title = 'Последний: ' + when(item.last_ts);
      const drop = button('✕', () => forget(kind, item.key, refresh), 'secondary ep-drop'); drop.title = 'Убрать из списка';
      row.append(...meta.line(item, refresh), hits, drop);
      box.append(row);
    }
    return box;
  }

  // Блоки видов промахов в контейнер раздела; ошибка загрузки — внутри контейнера,
  // чтобы пустой блок не выглядел как «промахов нет»
  window.renderMisses = async (container, kinds) => {
    if (!container) return;
    const data = await api('misses');
    container.replaceChildren();
    for (const kind of kinds) {
      const meta = KINDS[kind];
      if (!meta) continue;
      container.append(block(kind, data[meta.key] || [], () => window.renderMisses(container, kinds)));
    }
  };
})();
