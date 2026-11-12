/* Строка состояния в шапке: значки ASR / TTS / Модель и микросостояние агента —
   слева от переключателя «Простой / Модель / AI агент». Источник — GET api/live: здоровье
   локальных пакетов и адресов (voice_stack) плюс микросостояние хода и сбои сервисов
   (runtime: события SpeechTrace и вызовы инструментов). Опрос раз в 1.5 с, пока вкладка
   видима. На элементе один title, подписей нет; клик по значку — goTab на карточку источника. */
(() => {
  const host = document.getElementById('live-status');
  if (!host) return;
  const NAMES = { asr: 'Распознавание', tts: 'Синтез', llm: 'Модель' };
  const TABS = { asr: 'voice-stack', tts: 'voice-stack', llm: 'llm' };   // карточки вкладок «Голос» / «Модель»
  const DOT = { ok: 'готов', warn: 'запускается', err: 'ошибка', off: 'не выбран' };
  const AGENT = { idle: 'Ожидает', listening: 'Слушает', thinking: 'Думает', tool: 'Вызывает инструмент', responding: 'Отвечает', error: 'Ошибка' };
  let last = '';
  function render(data) {
    const key = JSON.stringify(data);
    if (key === last) return;  // без изменений — не перерисовываем, tooltip не мигает на опросе
    last = key;
    host.hidden = false;
    const parts = ['asr', 'tts', 'llm'].map(s => {
      const info = (data.services || {})[s] || {};
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'live-dot ' + (info.status || 'off');
      b.setAttribute('aria-label', NAMES[s]);
      b.title = NAMES[s] + ' — ' + (DOT[info.status] || 'нет данных') +
        (info.source ? ' · ' + info.source : '') + (info.hint ? ': ' + info.hint : '');
      b.onclick = () => window.goTab && window.goTab(TABS[s]);
      return b;
    });
    const agent = data.agent || {};
    const text = agent.state === 'tool' ? AGENT.tool + ' ' + (agent.tool || '…')
      : agent.state === 'error' && agent.error ? AGENT.error + ': ' + agent.error : AGENT[agent.state] || AGENT.idle;
    const chip = document.createElement('span');
    chip.className = 'live-agent ' + (agent.state || 'idle');
    chip.textContent = text;
    chip.title = 'AI агент — ' + text;
    chip.hidden = data.intelligence !== 'agent';   // микросостояние показываем только в режиме «AI агент»
    host.replaceChildren(...parts, chip);
  }
  // Ошибки сборки конфига колонок: закреплённое уведомление на колонку (notify.pin —
  // без крестика и автоскрытия), снимается следующей сборкой без ошибок (unpin).
  // Полный список — вкладка «Quasar», панель устройства.
  const quasarPinned = new Set();
  function quasarNotify(data) {
    const active = new Set();
    for (const item of (data.quasar_errors || [])) {
      const first = (item.errors || [])[0];
      if (!first) continue;
      active.add(item.sn);
      const count = (item.errors || []).length;
      const unfilled = String(first.error || '').includes('не заполнен параметр');
      window.notify.pin('quasar:' + item.sn,
        'Конфиг колонки · ' + (item.name || item.sn) + ' — ошибок: ' + count + ' · «'
        + first.rule + '»: ' + String(first.error).slice(0, 160)
        + (count > 1 ? ' · полный список — вкладка «Quasar»' : ''),
        { error: true,
          // незаполненный обязательный параметр — клик открывает первичный конфигуратор
          onclick: unfilled && window.configuratorOpen ? window.configuratorOpen : undefined });
    }
    for (const sn of [...quasarPinned])
      if (!active.has(sn)) { window.notify.unpin('quasar:' + sn); quasarPinned.delete(sn); }
    for (const sn of active) quasarPinned.add(sn);
  }
  async function tick() {
    if (document.hidden) return;   // опрос только пока вкладка видима
    try {
      const r = await fetch('api/live', { headers: { 'X-Emulator': '1' } });
      if (r.ok) { const data = await r.json(); render(data); quasarNotify(data); }
    } catch (e) { /* сервер перезапускается — следующей опрос подхватит */ }
  }
  setInterval(tick, 1500);
  tick();
})();
