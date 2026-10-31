/* Общий модуль устройств: модель/картинка/статус по SN.
   Используется вкладками «Устройства», «Quasar», «Обновления» и карточками
   DeviceCards (device_cards.js, кабинет /me). */
(() => {
  // ── Модель и картинка устройства по серийному номеру (SN) ──────────
// Формат подтверждён CRC32 (фиктивный тест-вектор C00T10001B6SAK и живые
// SN колонок): [тип:1][возможности:1][ревизия:1][порядковый:6][CRC:4][цвет:1],
// Crockford Base32 (без I L O U), младшие 5 бит первыми. Тип-символ →
// платформа: C=chiron и X=yandexstation_2 подтверждены трафиком колонок
// (?platform= в /get_sync_info), M — mini 2 (заводская утилита), остальные —
// по glagol-токенам. Картинки скачивает device_images.py с веб-версии умного
// дома Яндекса под именами iot_station_*.webp (у Миди — семейство iot_station_2_*).
// Цвет корпуса достоверно известен только для K (чёрный);
// остальным цветовым символам не доверяем — показываем чёрный вариант.
const SN_TYPES = {
  C: { name: 'Яндекс Станция Дуо', platform: 'chiron',
       img: ['iot_station_chiron_black', 'iot_station_chiron_biege', 'iot_station_chiron_green', 'iot_station_chiron_red'] },
  X: { name: 'Яндекс Станция Макс', platform: 'yandexstation_2',
       img: ['iot_station_max_black', 'iot_station_max_beige', 'iot_station_max_green', 'iot_station_max_turquoise'] },
  U: { name: 'Яндекс Станция Миди', platform: 'yandexmidi', img: ['iot_station_2_beige'] },
  L: { name: 'Умная колонка Fiero', platform: 'blackthorn', img: ['iot_station_fiero'] },
  T: { name: 'Яндекс ТВ Станция', platform: 'magritte', img: ['iot_station_magritte'] },
  M: { name: 'Яндекс Станция Мини 2', platform: 'yandexmini_2', img: [] },
  P: { name: 'Яндекс Лайт (Micro)', platform: 'yandexmicro', img: [] },
};
const SN_LEGACY_PREFIXES = [
  ['FF98', { name: 'Яндекс Станция Мини (Gen 1)', platform: 'yandexmini', img: [] }],
];
const SN_COLORS = { K: { label: 'чёрный', file: '_black' } };
const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; table[n] = c >>> 0; }
  return table;
})();
const crc32 = text => {
  let c = 0xFFFFFFFF;
  for (const ch of text) c = CRC_TABLE[(c ^ ch.charCodeAt(0)) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
};
// старые 32-значные SN / MAC-derived ID (FF98… — Мини 1-го поколения)
const legacySn = sn => {
  const byPrefix = SN_LEGACY_PREFIXES.find(([prefix]) => sn.startsWith(prefix));
  if (byPrefix) return { ...byPrefix[1], color: null, colorChar: null, crcOk: null, legacy: true };
  if (sn.length === 32 && SN_TYPES[sn[0]]) return { ...SN_TYPES[sn[0]], color: null, colorChar: null, crcOk: null, legacy: true };
  return null;
};
const parseSn = sn => {
  if (!sn) return null;
  if (sn.length !== 14) return legacySn(sn);
  if ([...sn].some(ch => !CROCKFORD.includes(ch))) return null;
  let stored = 0;
  for (let i = 9; i < 13; i++) stored |= CROCKFORD.indexOf(sn[i]) << (5 * (i - 9));
  const crcOk = (crc32(sn.slice(0, 9) + sn[13]) & 0xFFFFF) === stored;
  const type = SN_TYPES[sn[0]] || { name: 'Неизвестная модель', platform: sn[0], img: [] };
  return { ...type, color: SN_COLORS[sn[13]] || null, colorChar: sn[13], crcOk, legacy: false };
};
// Картинки устройств скачиваются с сайта умного дома по согласию пользователя
// (device_images.py) и могут отсутствовать: тогда показывается простой силуэт по типу
// устройства (img/generic/*.svg, входят в репозиторий).
const SILHOUETTES = {
  speaker: ['chiron', 'blackthorn', 'yandexmicro', 'yandexmini'],
  'speaker-screen': ['yandexstation_2', 'yandexmidi', 'yandexmini_2'],
  tv: ['magritte', 'yandexmodule', 'yandexmodule_2'],
};
// Колонка, колонка с экраном, ТВ; всё остальное (хаб, домофон, неизвестная модель) — «хаб»
const silhouette = platform => 'img/generic/' + (Object.keys(SILHOUETTES).find(kind => SILHOUETTES[kind].includes(platform)) || 'hub') + '.svg';
// Кандидаты картинки: вариант цвета (если цвет подтверждён) → остальные
// варианты платформы; свойство silhouette — путь запасного силуэта
const pictureChain = parsed => {
  if (!parsed) return Object.assign([], { silhouette: silhouette('') });
  const chain = [];
  const color = parsed.colorChar && SN_COLORS[parsed.colorChar];
  if (color) chain.push(...parsed.img.filter(name => name.endsWith(color.file)));
  chain.push(...parsed.img.filter(name => !chain.includes(name)));
  return Object.assign(chain.map(name => 'img/devices/' + name + '.webp'), { silhouette: silhouette(parsed.platform) });
};
// Кандидаты перебираются по onerror: файла нет в каталоге (картинки не скачаны) —
// следующий, в конце силуэт. Битой картинки не остаётся: силуэт — файл репозитория.
const applyPicture = (img, chain, fallback) => {
  const list = [...chain, fallback || chain.silhouette || silhouette('')];
  img.onerror = () => { const next = list.shift(); if (next) img.src = next; else img.onerror = null; };
  img.src = list.shift();
};
const modelTitle = parsed => {
  if (!parsed) return 'Модель по SN не распознана';
  let title = parsed.name + ' · платформа ' + parsed.platform;
  if (parsed.color) title += ' · цвет: ' + parsed.color.label + ' (' + parsed.colorChar + ')';
  else if (parsed.colorChar) title += ' · цвет по SN не расшифрован (' + parsed.colorChar + ')';
  if (parsed.crcOk === true) title += ' · SN ✓ CRC';
  else if (parsed.crcOk === false) title += ' · SN не сходится (CRC)';
  if (parsed.legacy) title += ' · старый формат SN';
  return title;
};

// Справочник платформ (platform → название и картинки): из типов SN + платформы,
// у которых нет картинки по SN, но есть похожая в каталоге приложения
const PLATFORM_EXTRA_PICTURES = {
  yandexmini_2: ['iot_station_mini3_pro_black_clock_led'],
  yandexmicro: ['iot_station_lite2_lilac_no_clock_connection'],
};
const PLATFORMS = {};
for (const type of [...Object.values(SN_TYPES), ...SN_LEGACY_PREFIXES.map(([, t]) => t)])
  PLATFORMS[type.platform] = { name: type.name, img: [...type.img, ...(PLATFORM_EXTRA_PICTURES[type.platform] || [])] };
const platformInfo = platform => PLATFORMS[platform]
  || { name: platform ? 'Платформа ' + platform : 'Платформа не распознана', img: [] };
const platformPictures = platform => platformInfo(platform).img.map(name => 'img/devices/' + name + '.webp');

  window.DeviceKit = { parseSn, pictureChain, applyPicture, modelTitle, silhouette, PLATFORMS, platformInfo, platformPictures };
})();
