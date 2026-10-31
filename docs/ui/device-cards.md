# DeviceCards — карточка устройства и платформы

Единый способ показать колонку или платформу (класс устройств) карточкой:
картинка модели, название, подпись, статус связи, бейдж и выбор. Все экраны,
где пользователь выбирает или просматривает колонки/платформы, строят карточки
только через `DeviceCards` — свою разметку карточек не заводим.

Файл: `server_emulator/ui/device_cards.js`. Зависит от `devices_common.js`
(`DeviceKit`: модель по SN, справочник платформ, цепочка картинок).

Где используется сейчас:

| Экран | Что показывает |
|---|---|
| Кабинет `/me` («Мой дом») | колонки пользователя по комнатам, `grid` |
| Вкладка «Обновления» | полоса платформ и полоса устройств, `grid(…, {strip: true})`; каталог прошивок — `card({kind: 'platform', …})` с версией вместо названия |

## Подключение

```html
<script src="devices_common.js"></script>
<script src="device_cards.js"></script>
```

Со страницы на другом пути (например `/me`) — абсолютными путями
`/emulator/devices_common.js`, `/emulator/device_cards.js`. Картинки модуль
берёт относительно своего `src`, поэтому они находятся с любой страницы.
Стили модуль вставляет сам, один раз.

## API (`window.DeviceCards`)

| Функция | Возвращает | Назначение |
|---|---|---|
| `device(device, extra?)` | `<button>` | карточка колонки из записи реестра (`sn`, `name`, `platform`/`profile.platform`/`model`, `status`) |
| `platform(id, extra?)` | `<button>` | карточка платформы (`chiron`, `yandexstation_2`, …): название и картинка из `DeviceKit.PLATFORMS` |
| `all(extra?)` | `<button>` | карточка «Все» — выбор без фильтра |
| `card(spec)` | `<button>` | базовая карточка; три функции выше — обёртки над ней |
| `grid(cards, {strip})` | `<div>` | сетка (`strip: false`) или горизонтальная полоса с прокруткой (`strip: true`) |
| `status(device, {sessions, activity})` | `'online' \| 'seen' \| 'offline'` | связь колонки по ответу `api/identities`: WS-сессия → `online`, HTTP за 10 минут → `seen` |
| `devicePlatform(device)` | `string` | платформа: `platform` → `profile.platform` → `model` → тип-символ SN |
| `picture(img, {sn, platform, pictures})` | — | та же цепочка картинок для произвольного `<img>` (крупная картинка в диалоге) |

`extra` перекрывает поля `spec`, которые обёртка заполнила сама.

### `spec` базовой карточки

| Поле | Тип | Смысл |
|---|---|---|
| `kind` | `'device' \| 'platform' \| 'all'` | вид карточки; у `all` вместо картинки — значок сетки |
| `title` | string | крупная строка внизу |
| `subtitle` | string? | строка под названием (комната, «3 устр», SN) |
| `sn`, `platform` | string? | откуда брать картинку: по SN (модель и цвет), затем по платформе, затем SVG-заглушка |
| `pictures` | string[]? | явный список картинок (пути от папки `ui/`) вместо вычисления |
| `status` | `'online' \| 'seen' \| 'offline'`? | `offline` — красный значок «нет сети» и приглушённая карточка |
| `dimmed` | bool? | приглушить независимо от статуса |
| `badge` | `{text, tone}`? | бейдж в правом верхнем углу; `tone`: `accent`, `ok`, `danger` или пусто |
| `selected` | bool? | рамка акцентным цветом и галочка; `aria-pressed` |
| `hint` | string? | всплывающая подсказка (`title`) |
| `onClick` | function? | обработчик нажатия |

Правило подписей: на карточке — только то, что отличает её от соседних
(название, комната, число устройств, явная политика). Пояснения к разделу —
один раз в заголовке раздела, не на каждой карточке.

## Тема

Внешний вид задают CSS-переменные; значения по умолчанию — тёмная тема
«Мой дом». Страница переопределяет их на `:root`:

| Переменная | Что |
|---|---|
| `--dc-bg`, `--dc-bg-hover`, `--dc-bg-selected` | фон карточки, при наведении, выбранной |
| `--dc-text`, `--dc-muted` | цвет названия и подписи |
| `--dc-accent`, `--dc-accent-bg` | рамка выбора, галочка, бейдж `accent` |
| `--dc-danger`, `--dc-ok`, `--dc-badge-bg` | значок «нет сети», бейджи |
| `--dc-radius`, `--dc-height`, `--dc-pic` | скругление, высота карточки, размер картинки |
| `--dc-min`, `--dc-strip-width` | минимальная ширина в сетке, ширина в полосе |

Админ-панель задаёт их в `style.css`, кабинет — в `me.html`.

## Пример

```js
const devices = registry.devices.map(d => ({ ...d, status: DeviceCards.status(d, registry) }));
root.append(DeviceCards.grid([
  DeviceCards.all({ title: 'Все платформы', selected: !current, onClick: () => select(null) }),
  ...platforms.map(p => DeviceCards.platform(p, {
    subtitle: count(p) + ' устр', selected: current === p,
    badge: { text: 'Запрет', tone: 'danger' }, onClick: () => select(p) })),
], { strip: true }));
root.append(DeviceCards.grid(devices.map(d => DeviceCards.device(d, {
  subtitle: d.room || 'Укажите комнату', onClick: () => open(d) }))));
```

## Справочник платформ

`DeviceKit.PLATFORMS` (`devices_common.js`) — `platform → {name, img}`: строится
из типов SN и дополняется картинками для платформ без собственной
(`PLATFORM_EXTRA_PICTURES`). Новая платформа добавляется туда же — карточки
подхватят её сами. На сервере платформу устройства определяет
`server_emulator.updates.platform_of` по тем же правилам.
