/* Звуковой кеш: фразы (стандартные ответы + синтезированные реплики сценариев) с индикатором
   кеша текущей модели синтеза; «Перегенерировать» пересинтезирует всё в фоне — общая полоса
   прогресса сверху, точка у каждой фразы: серая — нет в кеше, жёлтая — синтез, зелёная — готово.
   API — ui/api/sound_cache.py, кеш — server_emulator/tts_cache.py. */
(() => {
  const list = document.getElementById('sound-cache-list');
  if (!list) return;
  const regenerate = document.getElementById('sound-cache-regenerate');
  const engine = document.getElementById('sound-cache-engine');
  const progress = document.getElementById('sound-cache-progress');
  const GROUPS = [['reply', 'Стандартные ответы'], ['scenario', 'Сценарии'], ['agent', 'AI агент'], ['cache', 'Другие озвученные фразы']];
  const TITLE = { cached: 'В кеше текущей модели', missing: 'Нет в кеше', pending: 'В очереди', running: 'Синтез…', error: 'Ошибка синтеза' };
  const make = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n };
  const say = (text, error = false) => { const n = document.getElementById('notice'); if (n) { n.textContent = text; n.className = error ? 'error' : '' } };
  const call = async (action, method = 'GET', value) => {
    const r = await fetch('api/' + action, { method, headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: value === undefined ? undefined : JSON.stringify(value) });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  };
  let timer = null, running = false, playing = null, onlyMissing = false, last = null; // last — последний ответ для повторного рендера при смене фильтра
  const head = list.closest('section')?.querySelector('.sc-head');
  if (head) {
    const missingInfo = make('span', 'sc-missing', 'Без озвучки: 0 из 0'); missingInfo.id = 'sound-cache-missing';
    const onlyMissingBtn = make('button', 'secondary', 'Только без озвучки'); onlyMissingBtn.id = 'sound-cache-missing-only';
    onlyMissingBtn.type = 'button'; onlyMissingBtn.setAttribute('aria-pressed', 'false');
    onlyMissingBtn.onclick = () => { onlyMissing = !onlyMissing; onlyMissingBtn.setAttribute('aria-pressed', String(onlyMissing)); if (last) render(last) };
    head.append(missingInfo, onlyMissingBtn);
  }
  const player = new Audio();
  const ICON = {
    play: '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M4 2.5v11l9-5.5z" fill="currentColor"/></svg>',
    stop: '<svg viewBox="0 0 16 16" width="14" height="14"><rect x="3.5" y="3.5" width="9" height="9" rx="1" fill="currentColor"/></svg>',
    edit: '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M10.5 2.5l3 3L6 13H3v-3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    redo: '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };
  player.onended = player.onerror = () => { playing = null; list.querySelectorAll('.sc-play.on').forEach(b => { b.classList.remove('on'); b.innerHTML = ICON.play }) };
  const icon = (name, title, fn) => { const b = make('button', 'sc-icon sc-' + name); b.type = 'button'; b.title = title; b.setAttribute('aria-label', title); b.innerHTML = ICON[name]; b.onclick = fn; return b };

  function play(entry, button) {
    if (playing === entry.text) { player.pause(); player.onended(); return }
    player.onended();
    player.src = 'api/sound-cache-audio?text=' + encodeURIComponent(entry.text) + '&t=' + Date.now();
    player.play().then(() => { playing = entry.text; button.classList.add('on'); button.innerHTML = ICON.stop })
      .catch(() => say('Не удалось воспроизвести', true));
  }

  function render(data) {
    last = data;
    engine.textContent = data.engine ? 'Синтез: ' + data.engine : 'Текущий синтез не поддерживает кеш (нужен PCM)';
    running = data.job?.state === 'running';
    regenerate.disabled = !data.engine || running || !data.entries.length;
    const missing = data.entries.filter(e => e.status === 'missing').length;
    const missingInfo = document.getElementById('sound-cache-missing');
    if (missingInfo) missingInfo.textContent = 'Без озвучки: ' + missing + ' из ' + data.entries.length;
    const job = data.job;
    progress.hidden = !job;
    if (job) {
      progress.firstElementChild.style.width = Math.round(100 * job.done / Math.max(1, job.total)) + '%';
      progress.title = job.done + ' из ' + job.total + (job.errors ? ', ошибок: ' + job.errors : '');
    }
    const children = [];
    for (const [source, title] of GROUPS) {
      const items = data.entries.filter(e => e.source === source && (!onlyMissing || e.status === 'missing'));
      if (!items.length) continue;
      children.push(make('div', 'sc-heading', title));
      for (const entry of items) {
        const row = make('div', 'sc-row');
        const dot = make('span', 'sc-dot ' + entry.status); dot.title = TITLE[entry.status] || entry.status;
        const listen = icon('play', 'Прослушать', () => play(entry, listen));
        listen.disabled = entry.status !== 'cached';
        if (playing === entry.text) { listen.classList.add('on'); listen.innerHTML = ICON.stop }
        const redo = icon('redo', 'Перегенерировать', async () => {
          try { await call('sound-cache-regenerate', 'POST', { texts: [entry.text] }); await load() } catch (e) { say(e.message, true) }
        });
        redo.disabled = !data.engine || running;
        const text = make('span', 'sc-text', entry.text);
        if (entry.name) text.append(make('small', 'sc-from', entry.name));
        row.append(dot, text, listen, redo);
        if (entry.source === 'reply') row.append(icon('edit', 'Редактировать ответ', () => window.openReplyEditor?.({ name: entry.name, text: entry.text })));
        else if (entry.source === 'scenario') row.append(icon('edit', 'Редактировать сценарий', () => window.openScenarioPhrase?.(entry.id, entry.text)));
        children.push(row);
      }
    }
    if (!children.length) children.push(make('p', 'hint', onlyMissing ? 'Все фразы уже в кеше: без озвучки не осталось.' : 'Фраз пока нет: кеш наполняется при озвучке ответов.'));
    list.replaceChildren(...children);
    if (job?.state === 'done' && job.errors) say('Перегенерация: ошибок ' + job.errors + (job.error ? ' — ' + job.error : ''), true);
  }

  async function load() {
    clearTimeout(timer); timer = null;
    const data = await call('sound-cache');
    render(data);
    if (data.job?.state === 'running' && location.hash === '#replies') timer = setTimeout(() => load().catch(e => say(e.message, true)), 1000);
  }

  document.getElementById('sound-cache-add-reply').onclick = () => window.openReplyEditor?.(null);
  regenerate.onclick = async () => {
    regenerate.disabled = true;
    try { const r = await call('sound-cache-regenerate', 'POST', {}); say('Перегенерация: ' + r.total + ' фраз'); await load() }
    catch (e) { say(e.message, true); regenerate.disabled = false }
  };
  const sync = () => { clearTimeout(timer); if (location.hash === '#replies') load().catch(e => say(e.message, true)) };
  window.addEventListener('hashchange', sync);
  window.addEventListener('replies-changed', sync);
  document.addEventListener('DOMContentLoaded', sync);
  if (document.readyState !== 'loading') sync();
})();
