/* Строка состояния в шапке (TASK-088): значки ASR / TTS / Модель и микросостояние агента —
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
  async function tick() {
    if (document.hidden) return;   // опрос только пока вкладка видима
    try {
      const r = await fetch('api/live', { headers: { 'X-Emulator': '1' } });
      if (r.ok) render(await r.json());
    } catch (e) { /* сервер перезапускается — следующей опрос подхватит */ }
  }
  setInterval(tick, 1500);
  tick();
})();
