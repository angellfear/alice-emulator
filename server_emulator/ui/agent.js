(() => {
  // Вкладка «AI агент»: включение агента (выключен — прежний цикл модели без тредов, памяти
  // и заданий), лимиты, PII-фильтр, вебхук событий, предложения сценариев и журнал прогонов.
  // Сервер: server_emulator/agent/ops.py, API: ui/api/agent_ops.py, контракт: docs/contracts/agent.md.
  const root = document.getElementById('agent');
  let data = null;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; };
  const notice = text => { document.getElementById('notice').textContent = text; };
  async function api(value) {
    const r = await fetch('api/agent-ops', { method: value ? 'POST' : 'GET', headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: value ? JSON.stringify(value) : undefined });
    const v = await r.json(); if (!r.ok) throw Error(v.error || r.statusText); return v;
  }
  async function work(fn) { try { await fn(); } catch (e) { notice(e.message); } }
  async function load() { data = await api(); render(); }
  const when = ts => new Date(ts * 1000).toLocaleString('ru-RU');
  const field = (label, input) => { const box = el('label', null, 'mem-field'); box.append(el('span', label), input); return box; };
  const input = (value, type = 'text') => { const i = el('input'); i.type = type; i.value = value ?? ''; return i; };
  const check = (label, checked) => { const i = el('input'); i.type = 'checkbox'; i.checked = !!checked; const box = el('label', null, 'mem-check'); box.append(i, el('span', label)); return [box, i]; };
  const PII = { credit_card: 'Номера карт', secret: 'Ключи, токены, пароли', email: 'Email', ip: 'IP-адреса', mac_address: 'MAC-адреса', url: 'Ссылки' };

  function settingsPanel() {
    const s = data.settings, box = el('div', null, 'panel');
    box.append(el('h3', 'Настройки'));
    const steps = input(s.max_model_calls, 'number'), daily = input(s.daily_model_calls, 'number');
    daily.min = 0; daily.placeholder = '0';
    const cron = input(s.suggest_cron), min = input(s.suggest_min_count, 'number'), token = input(s.webhook_token, 'password');
    cron.placeholder = 'выключено'; token.placeholder = s.webhook_token ? 'Не менять' : 'выключен';
    const grid = el('div', null, 'mem-grid');
    grid.append(field('Шагов модели на ответ', steps), field('Вызовов модели в сутки', daily),
      field('Предложения сценариев · cron', cron), field('Минимум повторов фразы', min), field('Токен вебхука', token));
    const budgets = el('div', null, 'mem-grid'), budgetInputs = {};
    for (const u of data.users) { const i = input(s.budgets[u.id] ?? '', 'number'); i.min = 0; i.placeholder = 'общий'; budgetInputs[u.id] = i; budgets.append(field('Лимит · ' + u.name, i)); }
    const deferAfter = input(s.async_reply_s, 'number'); deferAfter.min = 0; deferAfter.max = 15; deferAfter.step = 0.5;
    const deferText = input(s.async_wait_text);
    const deferGrid = el('div', null, 'mem-grid');
    deferGrid.append(field('Отложенный ответ · через, с (0 — нет)', deferAfter), field('Фраза, пока модель думает', deferText));
    const pii = el('div', null, 'mem-grid'), piiInputs = {};
    for (const [key, label] of Object.entries(PII)) { const [b, i] = check(label, s.pii.includes(key)); piiInputs[key] = i; pii.append(b); }
    const save = el('button', 'Сохранить'); save.type = 'button';
    save.onclick = () => work(async () => {
      const settings = { max_model_calls: +steps.value, daily_model_calls: +daily.value || 0, suggest_cron: cron.value.trim(),
        suggest_min_count: +min.value, budgets: Object.fromEntries(Object.entries(budgetInputs).filter(([, i]) => i.value !== '').map(([k, i]) => [k, +i.value])),
        pii: Object.keys(piiInputs).filter(k => piiInputs[k].checked), async_reply_s: +deferAfter.value || 0,
        async_wait_text: deferText.value.trim(), webhook_token: token.value || (s.webhook_token ? '__KEEP__' : '') };
      await api({ op: 'settings', settings }); notice('Настройки агента сохранены'); await load();
    });
    const actions = el('div', null, 'actions'); actions.append(save);
    box.append(grid, deferGrid);
    if (data.users.length) box.append(el('h3', 'Лимиты пользователей'), budgets);
    box.append(el('h3', 'Скрывать от модели'), pii, actions);
    return box;
  }

  // Ползунок включения: сохраняется сразу; выключенный агент гасит свои разделы
  // (настройки, журнал, «Память», «Задания»). MCP остаётся — им пользуется и прежний цикл.
  function journal() {
    const box = el('div', null, 'panel'), users = Object.fromEntries(data.users.map(u => [u.id, u.name]));
    const limit = u => data.settings.budgets[u] ?? (data.settings.daily_model_calls || '∞');
    const usage = Object.entries(data.usage).map(([u, v]) => `${users[u] || u || 'без пользователя'}: ${v.model_calls}/${limit(u)} вызовов, ${v.tokens} ток.`);
    box.append(el('h3', 'Сегодня'), el('p', usage.join(' · ') || 'Прогонов сегодня не было.', 'hint'));
    const head = el('div', null, 'mem-head'), suggest = el('button', 'Предложить сценарии', 'secondary');
    suggest.type = 'button'; suggest.disabled = !data.misses.length;
    suggest.onclick = () => work(async () => {
      suggest.disabled = true;
      const r = await api({ op: 'suggest' });
      notice(r.status === 'done' ? 'Черновики — во вкладке «Сценарии». ' + (r.text || '') : 'Частых промахов нет.');
      await load();
    });
    head.append(el('h3', 'Промахи сценариев'), suggest);
    const misses = el('div', null, 'mem-list');
    for (const m of data.misses) { const r = el('div', null, 'mem-row'); r.append(el('time', '×' + m.count), el('span', m.examples[0] + (m.tools.length ? ' → ' + m.tools.join(', ') : ''))); misses.append(r); }
    if (!data.misses.length) misses.append(el('p', 'Пусто.', 'hint'));
    const log = el('div', null, 'log-console'); log.setAttribute('role', 'log');
    for (const r of data.runs.slice(0, 100)) log.append(el('div', `${when(r.ts)}  ${r.thread}  ${r.kind}  ${r.status}  ${r.model_calls}×/${r.ms} мс  ${r.text} → ${r.answer}${r.tools.length ? '  [' + r.tools.map(t => t.name).join(', ') + ']' : ''}`, r.status === 'ok' || r.status === 'pending' ? null : 'mem-bad'));
    if (!data.runs.length) log.append(el('div', '— прогонов пока не было', 'log-console-empty'));
    box.append(head, misses, el('h3', 'Прогоны'), log);
    return box;
  }

  function render() {
    const head = el('div', null, 'agent-head'); head.append(el('h2', 'AI агент'));
    root.replaceChildren(head,
      el('p', 'Агент ведёт диалог с памятью, подтверждает рискованные действия, выполняет задания по расписанию и правит сценарии. Лимит вызовов модели считается на пользователя за сутки, 0 — без лимита. Промахи — фразы, которые не взял ни один сценарий: по ним агент готовит черновики сценариев. Вебхук: POST api/agent-event с заголовками X-Emulator: 1 и X-Agent-Token.'));
    if (!data.settings.installed) root.append(el('p', 'Агент не установлен: pip install -e ".[agent]"', 'hint'));
    const body = el('fieldset', null, 'agent-body'); body.disabled = !data.settings.installed;
    body.append(settingsPanel());
    if (data.available) body.append(journal());
    root.append(body);
  }

  window.addEventListener('hashchange', () => { if (location.hash === '#agent') work(load); });
  if (location.hash === '#agent') work(load);
})();
