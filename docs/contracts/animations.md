---
title: "Контракт раздела «Анимации»"
status: stable
module: "server_emulator/animations.py, animation_formats.py, animation_storage.py, ui/api/animations.py"
---

# Контракт раздела «Анимации»

Раздел «Анимации» эмулятора сервера Яндекс Алисы: хранение проектов анимаций, разбор и сборка родных форматов (.led, .scled, GIF), HTTP API для управления проектами и проигрывание на колонке.

## 1. Платформы и дисплеи

Список платформ и их дисплеев берётся из реестра `PLATFORM_DISPLAYS` в `server_emulator/animations.py`.

| Платформа | Дисплеи | Размер | Формат кадров | Примечание |
|---|---|---|---|---|
| yandexstation_2 | frontal (глазки) + ring (кольцо) | 25×16 + 24 | gif / led | GIF по `draw_led_screen`, `.led` для кольца |
| yandexmidi | panel (панель) + oknix (окно) | 15×13 + 5 | led | `.led` текст/бинарник |
| yandexmini_2 | ring (кольцо) + clock (часы) | 6 + 32 | led / scled | `.scled` для часов 7-сегмент |
| yandexstation | ring | 24 | led | |
| yandexmini | ring | 24 | led | |
| chiron | — | — | — | без дисплея, анимаций нет |

API `GET /emulator/api/animations-platforms` возвращает все платформы с их дисплеями:
`{platforms: [{platform, title, displays: [{kind, title, size, fps, format, note}]}]}`.

## 2. Модель проекта анимации

Проект хранится в `state/animations/<platform>/<id>.json`:

```json
{
  "platform": "yandexstation_2",
  "id": "my.ring",
  "title": "Моя анимация",
  "kind": "ring",
  "size": [24],
  "fps": 30,
  "frames": [ ... ]
}
```

- `platform` — идентификатор платформы из реестра.
- `id` — строчные буквы/цифры/._- до 64 символов.
- `title` — непустая строка.
- `kind` — `ring` | `panel` | `frontal` | `clock` | `oknix`.
- `size` — список положительных целых (размер дисплея в пикселях/LED).
- `fps` — положительное число.
- `frames` — список кадров в родном представленииplatformы.

### Представления кадров

**ring / panel / oknix (`.led`)** — каждый кадр: `{colors: [[r,g,b,w], ...], delayMs: N}` где `colors` — массив длиной `size[0]` цветов RGBW, `delayMs` — задержка кадра в мс.

**clock (`.scled`)** — каждый кадр: `{brightnesses: [0..255 x 32], delayMs: N}` — 32 значения яркости сегментов (4 цифры × 7 сегментов + индикаторы) + задержка.

**frontal (GIF)** — каждый кадр: `{media_file: "имя.gif", media_url: "/emulator/api/animations-media?file=имя.gif"}` — ссылка на GIF-файл в `state/animations/media/`.

## 3. Родные форматы

### .led (кольцевой LED-дисплей)

Текстовый формат, строка на кадр:

```
<hex-RGB> <hex-RGB> ... <hex-RGB> <delayMs>
```

где цветов столько, сколько LED в дисплее, а `delayMs` — задержка кадра в миллисекундах.

Макросы (обрабатываются парсером):
- `loop` — метка зацикливания с этой позиции
- `smooth N` — сглаживание между кадрами (коэффициент N)
- `gammacorrection` — гамма-коррекция цветов
- `background` — фоновый кадр
- `name X` — имя анимации
- `no_size_optimization` — отключить оптимизацию размера
- `rotate` / `step1` / `step2` / `fill` / `unfill` — трансформационные макросы

Пример для 3 LED:
```
FF0000 00FF00 0000FF 100
00FF00 FF0000 0000FF 200
loop
```

### .scled (7-сегментные часы Мини 2)

Текстовый формат, строка на кадр:

```
<яркость1> <яркость2> ... <яркость32> <delayMs>
```

где 32 значения яркости (0–255) для сегментов 4 цифр (A–G по каждой) и индикаторов, `delayMs` — задержка.

Строка `loop` — метка зацикливания.

Бинарный формат (соответствует `ScLedPattern::loadFromBinary`):

```
[0][32 байта яркости][2 байта delay big-endian] [1]=loop
```

Каждый блок `0` — один кадр. Блок `1` — маркер зацикливания.

### BinaryAnimation (draw_animation_directive)

Структура из схемы станции `NAlice.TAnimationCapability.TDrawAnimationDirective`:

```protobuf
message TAnimation {
  oneof value {
    TBinaryAnimation binary_animation = 1;
    TS3Directory s3_directory = 2;
    TTypedAnimation typed_animation = 3;
  }
}
message TBinaryAnimation {
  optional string compression = 1;   // "Gzip" или отсутствует
  optional string base64_encoded_value = 2;
}
```

Сборка через существующие схемы эмулятора (`proto_schemas.encode_message` с типом `NAlice.TAnimationCapability.TDrawAnimationDirective`). Бинарные `.scled` данные кодируются gzip (опционально) и base64.

API `POST /emulator/api/animations` с `{id, sn}` отправляет `Push draw_animation` в живую сессию устройства (или мостовое соединение в «Прозрачно»).

## 4. HTTP API

### 4.1 animations — каталог LED-анимаций (существующий)

| Метод | Путь | Параметры | Описание |
|---|---|---|---|
| GET | `/emulator/api/animations` | — | Список анимаций + реестр моделей |
| PUT | `/emulator/api/animations` | `{animation, media_base64?}` | Сохранить анимацию |
| DELETE | `/emulator/api/animations?id=` | `id` | Удалить анимацию |
| POST | `/emulator/api/animations` | `{id, sn}` | Проиграть на устройстве (Push draw_led_screen) |

### 4.2 animations-media — выдача GIF

| Метод | Путь | Параметры | Описание |
|---|---|---|---|
| GET | `/emulator/api/animations-media` | `file` | Выдать GIF-файл каталога |

### 4.3 animations-platforms — платформы и дисплеи

| Метод | Путь | Описание |
|---|---|---|
| GET | `/emulator/api/animations-platforms` | Список платформ с их дисплеями |

### 4.4 animations-projects — CRUD проектов

| Метод | Путь | Описание |
|---|---|---|
| GET | `/emulator/api/animations-projects?platform=` | Список проектов платформы |
| GET | `/emulator/api/animations-projects?platform=&id=` | Проект по id |
| PUT | `/emulator/api/animations-projects` | Тело `{platform, id, title, kind, size, fps, frames}` — создать/заменить |
| DELETE | `/emulator/api/animations-projects?platform=&id=` | Удалить проект |
| POST | `/emulator/api/animations-projects` | То же, что PUT (alias) |

### 4.5 animations-import — импорт файла

| Метод | Путь | Описание |
|---|---|---|
| POST | `/emulator/api/animations-import` | Тело `{platform, id, title, kind, size, fps, media_base64, filename}` — импорт .led/.scled/.gif |

Все write-методы требуют заголовок `X-Emulator: 1` и same-origin ( guard диспетчера `ui/api/__init__.py`).

## 5. Проигрывание на колонке

`POST /emulator/api/animations` с `{id, sn}`:
1. Найти проект/анимацию по id.
2. Проверить, что у устройства SN есть дисплей (способность != "none").
3. Для `frontal` (GIF): Push `draw_led_screen` с `animation_sequence[{frontal_led_image: url}]`.
4. Для остальных (ring/panel/clock/oknix): Push `draw_animation` с `BinaryAnimation` (base64+gzip родного бинарника).

Отправка через `runtime.push_action(sn, action, arguments)` — квитируемая доставка в живую WS-сессию или мостовое соединение («Прозрачно»).

## 6. Границы

- Реагирующие анимации (эквалайзер, LED_USER_CONFIG) — вне этапа 2, не реализованы.
- Каталоги прошивки (встроенные .led/.scled/GIF из Z:/alice/source) не извлекаются в репозиторий — только импорт пользовательских файлов.
- UI-подвкладки не делаются на этой стадии (следующий исполнитель).