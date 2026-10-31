(() => {
  // Задания по расписанию: агент (prompt) или сценарий без модели — по cron, интервалу или разово;
  // результат говорит колонка, а если она не в сети или тихие часы — ждёт во входящих до
  // следующего обращения. Сервер: server_emulator/agent/routines.py, API: ui/api/routines.py,
  // контракт: docs/contracts/routines.md.
  const root = document.getElementById('routines');
  let data = null, editing = null;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; };
  const notice = (text, error = false) => { notify(text, { error }); };
  async function api(value) {
    const r = await fetch('api/routines', { method: value ? 'POST' : 'GET', headers: { 'X-Emulator': '1', 'Content-Type': 'application/json' }, body: value ? JSON.stringify(value) : undefined });
    const v = await r.json(); if (!r.ok) throw Error(v.error || r.statusText); return v;
  }
  async function work(fn) { try { await fn(); } catch (e) { notice(e.message, true); } }
  async function load() { data = await api(); render(); }
  const when = ts => ts ? new Date(ts * 1000).toLocaleString() : '—';
  const SCHEDULE = { cron: 'cron', interval: 'интервал, с', once: 'разово' };
  const DELIVERY = { speak: 'сказать на колонке', inbox: 'во входящие', silent: 'молча' };
  const field = (label, input) => { const box = el('label', null, 'mem-field'); box.append(el('span', label), input); return box; };
  const select = (options, value) => { const s = el('select'); for (const [v, t] of options) { const o = el('option', t); o.value = v; s.append(o); } s.value = value; return s; };
  const input = (value, placeholder) => { const i = el('input'); i.value = value ?? ''; if (placeholder) i.placeholder = placeholder; return i; };

  function form(routine) {
    const r = routine || { kind: 'agent', schedule: { kind: 'cron', expr: '0 8 * * *' }, delivery: 'speak', enabled: true,
      owner: data.users[0]?.id, tz: 'Europe/Moscow', max_runs_per_day: 24 };
    const box = el('div', null, 'panel');
    box.append(el('h3', routine ? 'Задание · ' + routine.title : 'Новое задание'));
    const title = input(r.title, 'Утренняя сводка');
    const owner = select(data.users.map(u => [u.id, u.name]), r.owner);
    const sn = select([['', 'первая колонка владельца'], ...data.devices.map(d => [d.sn, d.name])], r.sn || '');
    const kind = select([['agent', 'агент'], ['scenario', 'сценарий без модели']], r.kind);
    const prompt = el('textarea'); prompt.rows = 3; prompt.value = r.prompt || ''; prompt.placeholder = 'Расскажи погоду и что у меня сегодня в календаре';
    const scenario = select(data.scenarios.map(s => [s.id, s.title]), r.scenario_id || data.scenarios[0]?.id);
    const text = input(r.text, 'какая погода');
    const scheduleKind = select(Object.entries(SCHEDULE), r.schedule.kind), expr = input(r.schedule.expr, '0 8 * * 1-5');
    const tz = input(r.tz), delivery = select(Object.entries(DELIVERY), r.delivery);
    const quietFrom = input(r.quiet?.from, '22:00'), quietTo = input(r.quiet?.to, '08:00');
    const limit = input(r.max_runs_per_day); limit.type = 'number';
    const agentPart = el('div'), scenarioPart = el('div', null, 'mem-grid');
    agentPart.append(field('Что сделать агенту', prompt));
    scenarioPart.append(field('Сценарий', scenario), field('Фраза для сценария', text));
    const sync = () => { agentPart.hidden = kind.value !== 'agent'; scenarioPart.hidden = kind.value !== 'scenario'; };
    kind.onchange = sync; sync();
    const grid = el('div', null, 'mem-grid');
    grid.append(field('Название', title), field('Владелец', owner), field('Колонка', sn), field('Что запускать', kind),
      field('Расписание', scheduleKind), field('Выражение', expr), field('Таймзона', tz), field('Доставка', delivery),
      field('Тихие часы с', quietFrom), field('по', quietTo), field('Запусков в сутки, не больше', limit));
    const save = el('button', 'Сохранить'), cancel = el('button', 'Отмена', 'secondary');
    save.type = cancel.type = 'button';
    save.onclick = () => work(async () => {
      const value = { title: title.value.trim(), owner: owner.value, sn: sn.value || null, kind: kind.value,
        schedule: { kind: scheduleKind.value, expr: expr.value.trim() }, tz: tz.value.trim(), delivery: delivery.value,
        quiet: quietFrom.value && quietTo.value ? { from: quietFrom.value, to: quietTo.value } : null,
        max_runs_per_day: +limit.value || 24 };
      if (routine) value.id = routine.id;
      if (kind.value === 'agent') value.prompt = prompt.value.trim(); else { value.scenario_id = scenario.value; value.text = text.value.trim(); }
      await api({ op: 'save', routine: value }); editing = null; notice('Задание сохранено'); await load();
    });
    cancel.onclick = () => { editing = null; render(); };
    const actions = el('div', null, 'actions'); actions.append(save, cancel);
    box.append(grid, agentPart, scenarioPart, actions);
    return box;
  }

  function row(r) {
    const line = el('div', null, 'mcp-tool' + (r.enabled ? ' on' : ''));
    const check = el('input'); check.type = 'checkbox'; check.checked = r.enabled; check.setAttribute('aria-label', 'Включено ' + r.title);
    check.onchange = () => work(async () => { await api({ op: 'save', routine: { id: r.id, enabled: check.checked } }); await load(); });
    const text = el('div', null, 'mcp-tool-text');
    const what = r.kind === 'agent' ? r.prompt : 'сценарий ' + (r.scenario_id || r.text);
    text.append(el('b', r.title), el('small', `${SCHEDULE[r.schedule.kind]} ${r.schedule.expr} · ${DELIVERY[r.delivery]} · далее ${when(r.next_run)} · ${what}`));
    const buttons = el('div', null, 'actions');
    const run = el('button', 'Запустить', 'secondary'), edit = el('button', 'Изменить', 'secondary'), del = el('button', 'Удалить', 'danger');
    run.type = edit.type = del.type = 'button';
    run.onclick = () => work(async () => { const res = await api({ op: 'run', id: r.id }); notice(r.title + ': ' + (res.result?.status || '—')); await load(); });
    edit.onclick = () => { editing = r; render(); };
    del.onclick = () => work(async () => { if (!confirm('Удалить задание «' + r.title + '»?')) return; await api({ op: 'delete', id: r.id }); await load(); });
    buttons.append(run, edit, del);
    line.append(check, text, buttons);
    return line;
  }

  function render() {
    root.replaceChildren(el('h2', 'Задания · по расписанию'),
      el('p', 'Агент или сценарий запускаются по cron, интервалу или разово. Итог говорит колонка; если она не в сети или идут тихие часы — сообщение ждёт во входящих и агент передаст его при следующем обращении. Пропущенные во время простоя запуски не догоняются. Создавать задания может и сам агент — с подтверждением.'));
    if (!data.available) { root.append(el('p', 'Агент не установлен: pip install -e ".[agent]"', 'hint')); return; }
    const toolbar = el('div', null, 'sc-toolbar');
    const add = el('button', 'Новое задание'); add.type = 'button'; add.disabled = !data.users.length;
    add.onclick = () => { editing = 'new'; render(); };
    toolbar.append(add); root.append(toolbar);
    if (editing) root.append(form(editing === 'new' ? null : editing));
    const list = el('div', null, 'panel'), rows = el('div', null, 'mcp-tools');
    for (const r of data.routines) rows.append(row(r));
    if (!data.routines.length) rows.append(el('p', data.users.length ? 'Заданий нет.' : 'Сначала добавьте пользователя реестра.', 'hint'));
    list.append(rows); root.append(list);
    const names = Object.fromEntries(data.routines.map(r => [r.id, r.title]));
    const runs = el('div', null, 'panel'); runs.append(el('h3', 'Журнал запусков'));
    const log = el('div', null, 'log-console'); log.setAttribute('role', 'log');
    for (const r of data.runs) log.append(el('div', `${when(r.ts)}  ${names[r.routine_id] || r.routine_id}  ${r.status}${r.delivered ? ' → ' + r.delivered : ''}  ${r.error || r.text || ''}`, r.error ? 'mem-bad' : null));
    if (!data.runs.length) log.append(el('div', '— запусков пока не было', 'log-console-empty'));
    runs.append(log); root.append(runs);
    const inbox = el('div', null, 'panel'); inbox.append(el('h3', 'Входящие'));
    const items = el('div', null, 'mem-list');
    for (const i of data.inbox) { const r = el('div', null, 'mem-row'); r.append(el('time', when(i.ts)), el('span', (i.delivered ? '' : '● ') + i.title + ': ' + i.text)); items.append(r); }
    if (!data.inbox.length) items.append(el('p', 'Пусто.', 'hint'));
    inbox.append(items); root.append(inbox);
  }

  window.addEventListener('hashchange', () => { if (location.hash === '#routines') work(load); });
  if (location.hash === '#routines') work(load);
})();
