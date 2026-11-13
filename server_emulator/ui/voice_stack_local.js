/* Голосовой стек: применение источника с окном хода работы и блок «Установлено локально».
   window.voiceApply(service, ref, title) — план (что остановится, что запустится), затем <dialog>
   с шагами применения (GET api/voice-stack-apply?job=); окно закрывается кнопкой после завершения.
   Блок «Установлено локально» — api/voice-stack-installed и api/voice-stack-clean (ui/api/voice_stack.py). */
(() => {
  const make = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n };
  const call = async (action, method = 'GET', value) => {
    const r = await fetch('api/' + action, { method, headers: { 'Content-Type': 'application/json', 'X-Emulator': '1' }, body: value === undefined ? undefined : JSON.stringify(value) });
    const v = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(v.error || r.statusText);
    return v;
  };
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const ICON = { run: '⏳', ok: '✓', warn: '⚠', fail: '✗', wait: '•' };

  // --- применение ----------------------------------------------------------------------------
  const stepLine = step => {
    const line = make('li', 'vs-step ' + step.status);
    line.append(make('span', 'vs-step-icon', ICON[step.status] || '•'));
    const body = make('span', 'vs-step-body');
    body.append(make('span', null, step.title + (step.pid ? ' (PID ' + step.pid + ')' : '')));
    if (step.detail && ['fail', 'warn'].includes(step.status)) body.append(make('small', null, step.detail));
    line.append(body);
    return line;
  };

  const planSteps = plan => [
    ...plan.stop.map(e => ({ status: 'wait', title: 'Остановить: ' + e.title, pid: e.pid })),
    ...(plan.start ? [{ status: 'wait', title: 'Запустить: ' + plan.start.title }] : []),
  ];

  async function showProgress(title, plan, started) {
    const dialog = make('dialog', 'catalog-dialog vs-apply');
    const list = make('ul', 'vs-steps'), summary = make('p', 'vs-apply-summary', 'Составляю план…');
    const close = make('button', null, 'Закрыть'); close.type = 'button'; close.disabled = true;
    const row = make('div', 'row'); row.append(close);
    dialog.append(make('h3', null, 'Применение: ' + title), list, summary, row);
    dialog.addEventListener('cancel', e => e.preventDefault());  // пока идёт работа, окно не закрыть
    document.body.append(dialog); dialog.showModal();
    list.replaceChildren(...planSteps(plan).map(stepLine));
    summary.textContent = 'Выполняется…';
    let job = null, lost = 0;
    const id = (await started).job;
    while (!job || job.state === 'running') {
      await sleep(400);
      try { job = await call('voice-stack-apply?job=' + encodeURIComponent(id)); lost = 0 } catch (e) {
        if (++lost >= 5) { job = { state: 'error', summary: 'Нет связи с сервером: ' + e.message, steps: [] }; break }
        continue;
      }
      if (job.steps.length) list.replaceChildren(...job.steps.map(stepLine));
    }
    summary.textContent = job.summary || (job.state === 'ok' ? 'Применено' : 'Ошибка');
    summary.className = 'vs-apply-summary ' + job.state;
    close.disabled = false; close.focus();
    await new Promise(resolve => { close.onclick = resolve });
    dialog.close(); dialog.remove();
    return job.state !== 'error';
  }

  // Применить источник. Нечего останавливать и запускать — без окна, сразу. Возвращает true при успехе.
  window.voiceApply = async (service, ref, title) => {
    const plan = await call('voice-stack-apply-plan', 'POST', { service, ref });
    let ok;
    if (!plan.stop.length && !plan.start) { await call('voice-stack-apply', 'POST', { service, ref }); ok = true }
    else ok = await showProgress(title, plan, call('voice-stack-apply', 'POST', { service, ref, background: true }));
    window.refreshSettings?.().catch(() => {});
    return ok;
  };

  // --- установлено локально --------------------------------------------------------------------
  const host = document.getElementById('voice-stack-local');
  if (!host) return;
  const NAMES = { asr: 'ASR', tts: 'TTS', llm: 'LLM' };
  const STATUS = { active: 'Используется: применён', running: 'Используется: сервер запущен', installing: 'Идёт установка', needed: 'Нужна установленным пакетам', unused: 'Не используется' };
  const size = bytes => {
    if (bytes == null) return '…';
    if (bytes === 0) return '0 Б';
    if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(1).replace('.', ',') + ' ГБ';
    if (bytes >= 1024 ** 2) return Math.round(bytes / 1024 ** 2) + ' МБ';
    return Math.max(1, Math.round(bytes / 1024)) + ' КБ';
  };
  let data = null, timer = null, busy = false, failure = '';

  function ask(lines, okText) {
    return new Promise(resolve => {
      const dialog = make('dialog', 'catalog-dialog');
      const done = value => { dialog.close(); dialog.remove(); resolve(value) };
      const no = make('button', 'secondary', 'Отмена'); no.type = 'button'; no.onclick = () => done(false);
      const yes = make('button', 'danger', okText); yes.type = 'button'; yes.onclick = () => done(true);
      const row = make('div', 'row'); row.append(no, yes);
      dialog.append(...lines.map(t => make('p', null, t)), row);
      dialog.addEventListener('cancel', e => { e.preventDefault(); done(false) });
      document.body.append(dialog); dialog.showModal(); no.focus();
    });
  }

  async function clean(body, lines) {
    if (busy || !await ask(lines, 'Очистить')) return;
    busy = true; render();
    try {
      const r = await call('voice-stack-clean', 'POST', body);
      notify(r.errors.length ? 'Не удалось удалить: ' + r.errors.map(e => e.id + ' — ' + e.error).join('; ') : 'Очищено: ' + r.removed.length + ', освобождено ' + size(r.freed), { error: !!r.errors.length });
      document.dispatchEvent(new Event('vs-cleaned'));  // карточки пакетов перечитывают состояние
    } catch (e) { notify(e.message, { error: true }) } finally { busy = false; await load() }
  }

  function row(item) {
    const el = make('div', 'vs-inst-row');
    const head = make('div', 'vs-inst-head');
    head.append(make('b', null, item.title), make('span', 'vs-inst-size', size(item.size)));
    const meta = make('div', 'vs-inst-meta');
    const tag = item.kind === 'shared' ? 'общая' : item.service ? NAMES[item.service] : 'без описания';
    meta.append(make('span', 'vs-inst-tag', tag), make('span', 'vs-inst-status ' + item.status, STATUS[item.status] || item.status));
    const button = make('button', 'secondary', 'Очистить'); button.type = 'button';
    button.disabled = !item.removable || busy;
    button.onclick = () => {
      const extra = item.frees.map(d => d + ' (' + size((data.items.find(r => r.id === '_shared/' + d) || {}).size) + ')');
      clean({ ids: [item.id] }, ['Удалить «' + item.title + '» (' + size(item.size) + ') с диска?' + (extra.length ? ' Вместе с ним уйдут ставшие ненужными зависимости: ' + extra.join(', ') + '.' : ''),
        'Параметры запуска сохранятся; пакет можно установить снова.']);
    };
    el.append(head, meta, button);
    return el;
  }

  function render() {
    const section = make('section', 'vs-card vs-installed');
    section.append(make('h3', null, 'Установлено локально'));
    if (!data) { section.append(make('p', 'vs-state', failure ? 'Не удалось загрузить: ' + failure : 'Считаю…')); host.replaceChildren(section); return }
    section.append(make('p', 'vs-desc', 'Очистить можно то, что не применено в настройках и не запущено. Общие зависимости удаляются вместе с последним пакетом, которому они нужны; нужные оставшимся пакетам остаются. Параметры запуска сохраняются.'));
    if (!data.items.length) { section.append(make('p', 'vs-state', 'Ничего не установлено')); host.replaceChildren(section); return }
    const total = make('div', 'vs-inst-total');
    total.append(make('b', null, 'Всего: ' + size(data.total)), make('span', null, 'можно освободить: ' + size(data.reclaimable)));
    const all = make('button', 'secondary', 'Очистить всё неиспользуемое'); all.type = 'button';
    all.disabled = busy || !data.reclaim_ids.length;
    all.onclick = () => clean({ all_unused: true }, ['Удалить всё неиспользуемое (' + size(data.reclaimable) + '): ' + data.reclaim_ids.map(i => i.replace('_shared/', '')).join(', ') + '?', 'Применённое и запущенное не затрагивается.']);
    section.append(total, ...data.items.map(row), all);
    host.replaceChildren(section);
  }

  async function load() {
    clearTimeout(timer); timer = null;
    try { data = await call('voice-stack-installed'); failure = '' } catch (e) { failure = e.message }
    render();
    if (data?.computing && location.hash === '#voice-stack') timer = setTimeout(() => load(), 2000);
  }

  const sync = () => { if (location.hash === '#voice-stack') load() };
  window.addEventListener('hashchange', sync);
  document.addEventListener('vs-changed', () => load());
  document.addEventListener('DOMContentLoaded', sync);
  if (document.readyState !== 'loading') sync();
})();
