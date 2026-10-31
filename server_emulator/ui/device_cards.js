/* DeviceCards — единая карточка устройства и платформы (docs/ui/device-cards.md).
   Используется кабинетом /me и вкладкой «Обновления»; новые экраны со списками
   колонок/платформ строятся только через неё. Требует devices_common.js (DeviceKit).
   Тема — CSS-переменные --dc-* (у каждой страницы свои значения), стили
   вставляются один раз при загрузке модуля. */
(() => {
  const kit = window.DeviceKit;
  // Картинки лежат рядом со скриптом: страница может жить по другому пути (/me)
  const BASE = new URL('.', document.currentScript ? document.currentScript.src : location.href).href;
  const asset = path => /^(data:|https?:|\/)/.test(path) ? path : BASE + path;

  const ICON = (paths, cls) => `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
  const ICONS = {
    offline: ICON('<path d="M3 3l18 18"/><path d="M8.5 16.5a5 5 0 0 1 7 0"/><path d="M5 12.5a10 10 0 0 1 5-2.7"/><path d="M14.5 9.9A10 10 0 0 1 19 12.5"/><path d="M2 8.8a15 15 0 0 1 4.3-2.6"/><path d="M10.7 5.1A15 15 0 0 1 22 8.8"/><circle cx="12" cy="20" r=".6" fill="currentColor"/>', 'dc-off'),
    all: ICON('<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>', 'dc-glyph'),
    check: ICON('<path d="M5 12.5l4.5 4.5L19 7.5"/>', 'dc-check'),
  };

  const STYLE = `
.dc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--dc-min,220px),1fr));gap:10px}
.dc-strip{display:flex;gap:10px;overflow-x:auto;padding-bottom:6px;scrollbar-width:thin}
.dc-strip>.dc-card{flex:0 0 var(--dc-strip-width,200px)}
.dc-card{position:relative;display:flex;flex-direction:column;justify-content:space-between;gap:6px;min-width:0;
  height:var(--dc-height,134px);padding:10px 12px;text-align:left;font:inherit;color:var(--dc-text,#f1f1f4);
  background:var(--dc-bg,#2c2d37);border:1.5px solid transparent;border-radius:var(--dc-radius,14px);cursor:pointer}
.dc-card:hover{background:var(--dc-bg-hover,#353642)}
.dc-card:focus-visible{outline:2px solid var(--dc-accent,#8b6cff);outline-offset:2px}
.dc-card.selected{border-color:var(--dc-accent,#8b6cff);background:var(--dc-bg-selected,var(--dc-bg-hover,#353642))}
.dc-card .dc-pic{width:var(--dc-pic,56px);height:var(--dc-pic,56px);object-fit:contain;object-position:left top}
.dc-card .dc-glyph{width:var(--dc-pic,56px);height:var(--dc-pic,56px);padding:8px;color:var(--dc-muted,#a3a4b0)}
.dc-card .dc-text{min-width:0}
.dc-card .dc-title{font-size:15px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dc-card .dc-sub{font-size:13px;color:var(--dc-muted,#a3a4b0);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dc-card.dimmed .dc-title,.dc-card.dimmed .dc-pic{opacity:.6}
.dc-card .dc-corner{position:absolute;top:10px;right:10px;display:flex;align-items:center;gap:6px}
.dc-card .dc-off{width:24px;height:24px;color:var(--dc-danger,#ff5c5c)}
.dc-card .dc-check{width:22px;height:22px;padding:3px;border-radius:50%;color:#fff;background:var(--dc-accent,#8b6cff)}
.dc-card .dc-badge{font-size:11.5px;line-height:1;padding:4px 8px;border-radius:999px;white-space:nowrap;
  background:var(--dc-badge-bg,#3a3b47);color:var(--dc-muted,#a3a4b0)}
.dc-card .dc-badge.accent{background:var(--dc-accent-bg,#3b3270);color:var(--dc-accent,#8b6cff)}
.dc-card .dc-badge.danger{background:var(--dc-danger-bg,rgba(255,92,92,.15));color:var(--dc-danger,#ff5c5c)}
.dc-card .dc-badge.ok{background:var(--dc-ok-bg,rgba(85,194,122,.15));color:var(--dc-ok,#55c27a)}`;
  const style = document.createElement('style'); style.textContent = STYLE; document.head.append(style);

  // Связь колонки по реестру: живая WS-сессия → online, HTTP за 10 минут → seen
  function status(device, { sessions = [], activity = {} } = {}) {
    const ids = device.device_ids || [];
    if (sessions.some(s => s.device_sn === device.sn || (s.device_id && ids.includes(s.device_id)))) return 'online';
    const act = activity[device.sn];
    return act && Date.now() / 1000 - act.ts < 600 ? 'seen' : 'offline';
  }

  // Силуэт на случай, когда картинок из APK нет: по платформе (явной или по SN)
  const silhouetteOf = spec => asset(kit.silhouette(spec.platform || (kit.parseSn(spec.sn) || {}).platform || ''));

  function card(spec) {
    const el = document.createElement('button'); el.type = 'button';
    el.className = 'dc-card dc-' + (spec.kind || 'device') + (spec.selected ? ' selected' : '')
      + (spec.dimmed ?? spec.status === 'offline' ? ' dimmed' : '');
    el.setAttribute('aria-pressed', String(!!spec.selected));
    if (spec.hint) el.title = spec.hint;
    if (spec.kind === 'all') el.insertAdjacentHTML('beforeend', ICONS.all);
    else {
      const img = document.createElement('img'); img.className = 'dc-pic'; img.alt = '';
      kit.applyPicture(img, picturesFor(spec).map(asset), silhouetteOf(spec));
      el.append(img);
    }
    const text = document.createElement('div'); text.className = 'dc-text';
    const title = document.createElement('div'); title.className = 'dc-title'; title.textContent = spec.title || '';
    text.append(title);
    if (spec.subtitle) { const sub = document.createElement('div'); sub.className = 'dc-sub'; sub.textContent = spec.subtitle; text.append(sub); }
    el.append(text);
    const corner = document.createElement('div'); corner.className = 'dc-corner';
    if (spec.badge) {
      const badge = document.createElement('span'); badge.className = 'dc-badge ' + (spec.badge.tone || '');
      badge.textContent = spec.badge.text; corner.append(badge);
    }
    if (spec.status === 'offline') corner.insertAdjacentHTML('beforeend', ICONS.offline);
    if (spec.selected) corner.insertAdjacentHTML('beforeend', ICONS.check);
    if (corner.childNodes.length) el.append(corner);
    if (spec.onClick) el.onclick = event => spec.onClick(event);
    return el;
  }

  // Картинка: явный список → по SN (модель и цвет) → по платформе → SVG-заглушка
  function picturesFor(spec) {
    if (spec.pictures) return spec.pictures;
    const chain = spec.sn ? kit.pictureChain(kit.parseSn(spec.sn)) : [];
    for (const path of kit.platformPictures(spec.platform || '')) if (!chain.includes(path)) chain.push(path);
    return chain;
  }

  const devicePlatform = device => device.platform || (device.profile || {}).platform || device.model
    || (kit.parseSn(device.sn) || {}).platform || '';

  // Карточка колонки: название, картинка по SN/платформе, статус связи
  const device = (item, extra = {}) => card({
    kind: 'device', title: item.name || item.sn, sn: item.sn, platform: devicePlatform(item),
    status: item.status, hint: kit.platformInfo(devicePlatform(item)).name + ' · SN ' + item.sn, ...extra });

  // Карточка платформы (класса устройств): название и картинка из справочника DeviceKit
  const platform = (id, extra = {}) => card({
    kind: 'platform', title: kit.platformInfo(id).name, platform: id, hint: id || 'платформа не распознана', ...extra });

  // Карточка «все» — выбор без фильтра (общая политика, все платформы)
  const all = (extra = {}) => card({ kind: 'all', title: 'Все', ...extra });

  function grid(cards, { strip = false } = {}) {
    const box = document.createElement('div'); box.className = strip ? 'dc-strip' : 'dc-grid';
    box.append(...cards); return box;
  }

  // Та же цепочка картинок для произвольного <img> (крупная картинка в диалоге)
  const picture = (img, spec) => kit.applyPicture(img, picturesFor(spec).map(asset), silhouetteOf(spec));

  window.DeviceCards = { card, device, platform, all, grid, status, devicePlatform, picture };
})();
