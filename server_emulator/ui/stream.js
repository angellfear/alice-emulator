/* Стрим-режим: размазывание (расфокус) чувствительных данных по всей панели —
   галочка «Настройки → Сервер → Стрим-режим» (server_config.js). Состояние —
   localStorage (ключ streamMode), как у темы: настройка этого браузера, серверу
   не передаётся, применяется сразу.

   Три механизма:
   1) класс stream-on на <html>. До первой отрисовки его ставит инлайн-скрипт в
      <head> index.html; здесь он только держится синхронно.
   2) обход текстовых узлов: серийники, публичные IPv4, MAC, почта, идентификаторы
      устройств и токены заворачиваются в <span class=stream-mask>. Локальные адреса
      (127.x, LAN, link-local) и порты видны — решение владельца. Правку DOM
      MutationObserver доставляет до отрисовки кадра, поэтому размазывание делается
      в его колбэке синхронно: содержимое не успевает показаться открытым.
   3) значения полей ввода и списков: размазываются только если само значение
      совпало с чувствительным паттерном (class stream-field) — пустые поля, адреса
      моделей, локальные адреса сервера и пути остаются видны. Значение можно
      присвоить без мутации DOM, поэтому, кроме событий input/change и проверки
      после каждой партии правок, работает лёгкий опрос.
   Редакторы не заворачиваются (script/style/textarea/CodeMirror/contenteditable —
   изменился бы редактируемый текст или подсветка). Снятие галочки распаковывает
   span'ы и возвращает всё как было. */
(() => {
  const KEY = 'streamMode';
  const MASK = 'stream-mask';
  const FIELD = 'stream-field';
  // Теги, внутри которых текст не заворачивается: содержимое textarea/script —
  // редактируемый источник, option не стилизуется (размазывается как поле).
  const SKIP_TAGS = ['SCRIPT', 'STYLE', 'TEXTAREA', 'OPTION'];
  const SKIP_SEL = '.stream-mask,.CodeMirror,[contenteditable="true"]';
  const NO_FIELD = ['checkbox', 'radio', 'range', 'file', 'button', 'submit', 'reset', 'image', 'color'];
  let on = false, observer = null, poller = 0, draining = false;
  const pending = [];
  try { on = localStorage.getItem(KEY) === '1'; } catch (e) { /* приватный режим — класс ставит head-скрипт */ }

  // Совпадения заворачиваются целиком. Серийник колонки: 14 символов Crockford
  // Base32 (без I L O U), первый — тип устройства (C X U L T M P, devices_common.js);
  // старые форматы — FF98… и 32-значные (последние снимаются и общим правилом
  // длинных токенов).
  const RX = new RegExp([
    '(?<![\\w.])' + '(?:\\d{1,3}\\.){3}\\d{1,3}' + '(?![\\w.])',                            // IPv4
    '(?<![0-9A-Fa-f:])' + '(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}' + '(?![0-9A-Fa-f:])',      // MAC
    '\\b[\\w.+-]+@[\\w-]+\\.[\\w.-]+\\b',                                                   // почта
    '\\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\\b',    // UUID (id устройств)
    'eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+',                                  // JWT
    '(?<![0-9A-Za-z])FF98[0-9A-Z]{28}(?![0-9A-Za-z])',                                      // старый серийник
    '(?<![0-9A-Za-z])[CXULTMP][0-9A-HJ-NP-TV-Z]{13}(?![0-9A-Za-z])',                        // серийник колонки
    '(?<![0-9A-Za-z_-])[0-9A-Za-z_-]{32,}(?![0-9A-Za-z_-])',                                // токены, хэши, 32-значный серийник
  ].join('|'), 'g');
  // Приватные и служебные IPv4 не маскируются: localhost, LAN, link-local, wildcard,
  // multicast и зарезервированные диапазоны (224.0.0.0–255.255.255.255).
  const PRIVATE_IP = /^(?:0\.|10\.|127\.|169\.254\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.|2(?:2[4-9]|[345]\d)\.)/;

  const skipParent = el => !el || el.closest(SKIP_SEL) || SKIP_TAGS.includes(el.tagName);

  const sensitive = text => {
    RX.lastIndex = 0;
    for (let m; (m = RX.exec(text)); ) if (!PRIVATE_IP.test(m[0])) return true;
    return false;
  };

  const wrapText = node => {
    const parent = node.parentElement;
    if (!parent || skipParent(parent)) return;
    const text = node.nodeValue;
    if (!text || !sensitive(text)) return;
    RX.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = RX.exec(text))) {
      if (m.index > last) frag.append(document.createTextNode(text.slice(last, m.index)));
      if (PRIVATE_IP.test(m[0])) {  // локальный адрес — виден, как обычный текст
        frag.append(document.createTextNode(m[0]));
      } else {
        const span = document.createElement('span');
        span.className = MASK;
        span.textContent = m[0];
        frag.append(span);
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) frag.append(document.createTextNode(text.slice(last)));
    node.replaceWith(frag);
  };

  // Сначала сбор, потом замена: правка DOM во время обхода TreeWalker ломает обход.
  const scan = root => {
    if (!root || !on) return;
    if (root.nodeType === Node.TEXT_NODE) { wrapText(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    if (root.closest(SKIP_SEL)) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode())
      if (!skipParent(n.parentElement)) nodes.push(n);
    for (const n of nodes) wrapText(n);
  };

  const scanFields = () => {
    for (const el of document.querySelectorAll('input, textarea, select')) {
      if (el.tagName === 'INPUT' && NO_FIELD.includes(el.type)) continue;
      const value = el.tagName === 'SELECT'
        ? (el.selectedOptions[0] ? el.selectedOptions[0].textContent : '')
        : el.value;
      el.classList.toggle(FIELD, on && !!value && sensitive(value));
    }
  };

  // Партии правок обрабатываются синхронно в колбэке наблюдателя — до отрисовки
  // кадра (без мигания открытыми данными). Правки, сделанные во время скана,
  // добираются следующими витками цикла.
  const drain = () => {
    if (draining) return;
    draining = true;
    while (pending.length) {
      const batch = pending.splice(0);
      for (const n of batch) scan(n);
      scanFields();
    }
    draining = false;
  };

  const applyClass = () => document.documentElement.classList.toggle('stream-on', on);

  const start = () => {
    applyClass();
    if (observer) { scanFields(); return; }
    scan(document.body);
    scanFields();
    observer = new MutationObserver(records => {
      for (const r of records) {
        if (r.type === 'characterData') pending.push(r.target);
        else for (const n of r.addedNodes) pending.push(n);
      }
      if (pending.length) drain();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    poller = setInterval(scanFields, 700);  // значения, присвоенные без мутации DOM
  };

  const stop = () => {
    clearInterval(poller);
    poller = 0;
    pending.length = 0;
    if (observer) { observer.disconnect(); observer = null; }
    for (const span of document.querySelectorAll('.' + MASK)) {
      const parent = span.parentNode;
      if (!parent) continue;
      parent.replaceChild(document.createTextNode(span.textContent), span);
      parent.normalize();
    }
    applyClass();
    scanFields();  // on=false: классы полей снимаются
  };

  const set = value => {
    on = !!value;
    try { localStorage.setItem(KEY, on ? '1' : '0'); } catch (e) { /* переживёт эту вкладку */ }
    if (on) start(); else stop();
  };

  // значения полей правятся и руками — события дешевле опроса
  document.addEventListener('input', scanFields, true);
  document.addEventListener('change', scanFields, true);

  window.AppStream = { current: () => on, set };
  if (on) start(); else applyClass();
})();
