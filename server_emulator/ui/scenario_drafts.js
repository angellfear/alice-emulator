(() => {
  // Черновики и предложения агента во вкладке «Сценарии»: агент правит сценарии через
  // черновик (JSON Patch, проверка валидатором, прогон фраз); общий для всех сценарий он
  // не включает сам — предлагает, и включает его администратор здесь.
  // Сервер: server_emulator/agent/scenario_edit.py, API: ui/api/scenario_drafts.py.
  const root = document.getElementById('scenario-drafts');
  if (!root) return;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; };
  const notice = (text, error = false) => { notify(text, { error }); };
  async function api(value) {
    const r = await fetch('api/scenario-drafts', { method: value ? 'POST' : 'GET', headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: value ? JSON.stringify(value) : undefined });
    const v = await r.json(); if (!r.ok) throw Error(v.error || r.statusText); return v;
  }
  async function work(fn) { try { await fn(); } catch (e) { notice(e.message, true); } }
  const STATUS = { draft: 'черновик', proposed: 'ждёт утверждения', active: 'включён' };

  function card(draft) {
    const box = el('div', null, 'mcp-tool' + (draft.status === 'proposed' ? ' on' : ''));
    const text = el('div', null, 'mcp-tool-text');
    const title = draft.scenario.title?.ru || draft.scenario.title || draft.scenario.id;
    text.append(el('b', `${draft.scenario.id} · ${STATUS[draft.status] || draft.status}`),
      el('small', (draft.error ? 'ошибка: ' + draft.error + ' · ' : '') + title + (draft.base_id ? ' · из ' + draft.base_id : '')));
    const json = el('pre', JSON.stringify(draft.scenario, null, 1), 'mcp-result'); json.hidden = true;
    const result = el('pre', null, 'mcp-result'); result.hidden = true;
    const actions = el('div', null, 'actions');
    const show = el('button', 'JSON', 'secondary'), test = el('button', 'Проверить', 'secondary');
    const activate = el('button', 'Включить'), discard = el('button', 'Удалить', 'danger');
    for (const b of [show, test, activate, discard]) b.type = 'button';
    show.onclick = () => { json.hidden = !json.hidden; };
    test.onclick = () => work(async () => {
      const r = await api({ op: 'test', draft_id: draft.draft_id });
      result.hidden = false;
      result.className = 'mcp-result' + (r.ok ? '' : ' bad');
      result.textContent = !r.valid ? 'Ошибка: ' + r.error
        : r.results.map(x => `«${x.phrase}» → ${x.scenario || x.verdict}`).join('\n')
          + (r.stolen.length ? '\nПерехватывает: ' + r.stolen.map(x => `«${x.phrase}» (было ${x.was})`).join(', ') : '');
    });
    activate.disabled = draft.status === 'active' || !!draft.error;
    activate.onclick = () => work(async () => { await api({ op: 'activate', draft_id: draft.draft_id }); notice('Сценарий включён: ' + draft.scenario.id); location.reload(); });
    discard.onclick = () => work(async () => { await api({ op: 'discard', draft_id: draft.draft_id }); await load(); });
    actions.append(show, test, activate, discard);
    box.append(text, actions);
    const wrap = el('div'); wrap.append(box, json, result);
    return wrap;
  }

  async function load() {
    const data = await api();
    root.replaceChildren();
    if (!data.available || !data.drafts.length) return;
    const panel = el('div', null, 'panel');
    const head = el('div', null, 'mem-head');
    head.append(el('h3', 'Черновики и предложения агента'), el('span', String(data.drafts.filter(d => d.status !== 'active').length), 'chip'));
    panel.append(head);
    for (const draft of data.drafts.filter(d => d.status !== 'active')) panel.append(card(draft));
    if (panel.children.length > 1) root.append(panel);
  }

  window.addEventListener('hashchange', () => { if (location.hash === '#scenarios') work(load); });
  if (location.hash === '#scenarios') work(load);
})();
