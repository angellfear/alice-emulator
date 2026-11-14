---
title: "Эндпоинты — каталог захваченных HTTP-ответов"
status: draft
date: "2026-10-25"
module: "server_emulator/runtime.py"
---

# Эндпоинты — каталог захваченных HTTP-ответов

Раздел «Эндпоинты» — по образцу каталога сценариев: восстановление эндпоинтов, которые
в эмуляции отдают 40*, чтобы колонки получали всё нужное без реального облака.
Критерий: в режиме эмуляции периодический трафик колонок не получает
404/409 на эндпоинты, имеющие захваченный реальный ответ.

## Проблема

Эмулятор отвечает локально только на часть маршрутов (builtin Quasar, обработчики,
OTA-обновления, каталог приложений). Остальное — честный 404 (`emulator_route_disabled`
/ `emulator_handler_not_found`). Инвентаризация живого трафика (traffic.sqlite3,
эмулированные 40*, сутки):

- rpc.alice.yandex.net `/gproxy/*`: get_tv_categories (590), push_events_batch (299),
  centaur_get_gallery (93), get_teasers, get_gallery, get_galleries, get_screen_hints,
  watch_list_check, get_search_suggests, get_tv_promo_phrases, get_tv_channels;
- quasar.yandex.net: `/push_subscribe` (1092 суммарно), `/tv/api/v4/settings` (33),
  `/tv/api/v1/media`, `/register`;
- egress.yandex.net `/?size=10` (226) — периодика каждые ~2 минуты с каждой колонки.

Отдельный класс — 409 `device_config_not_set` на `/get_sync_info` (устроенному нужен
реальный `state.quasar.config`): устранён — без слоёв и legacy builtin собирает ответ из
встроенных дефолтов `server_emulator/quasar_defaults/` (источник generated,
`docs/contracts/quasar-config.md`).

В capture-базе уже есть реальные 200-ответы облака почти на всё это (proxy-окна):
egress — 106, tv/api/v4/settings — 72, gproxy-семейство — 1–54, push_subscribe — 31,
set_device_capability — 3. Ресурс для восстановления существует.

## Архитектура

### Принцип

Каталог = «переигрывание захваченного»: один JSON-файл на эндпоинт в
`state/endpoints/<id>.json` (как сценарии в `state/scenarios/`). Эмулятор остаётся
отдельным компонентом: он читает только файлы каталога и не знает о traffic.sqlite3.
Наполнение каталога — утилитой `tools/endpoints_recover.py` (запускается человеком),
которая извлекает реальные ответы из capture-базы и пишет файлы. Это зеркально
разделению «прокси — отдельный компонент».

### Модель данных (файл эндпоинта)

```json
{
  "id": "rpc.alice.yandex.net.gproxy.get_tv_categories",
  "title": "POST /gproxy/get_tv_categories",
  "_group": "rpc.alice.yandex.net",
  "enabled": true,
  "host": "rpc.alice.yandex.net",
  "method": "POST",
  "path": "/gproxy/get_tv_categories",
  "status": 200,
  "content_type": "application/json",
  "body_base64": "...",
  "captured_ts": 1792873244.5,
  "source": "traffic.sqlite3",
  "notes": ""
}
```

- `host` — канонический (aliases уже разворачиваются до сравнения в dispatch).
- `path` — только путь, без query: `/push_subscribe?device_id=…` матчится как
  `/push_subscribe` (ответы облака на семейство device-agnostic).
- `body_base64` — байты ответа как есть (gzip не распаковываем: сохраняем
  Content-Encoding в `content_type`-соседстве только при необходимости; реальные
  capture-ответы хранятся уже распакованными рекордером — фиксируем что есть).
- `_group` — плоская группа каталога (по хосту), как `_group` сценариев.

### Порядок обслуживания (dispatch)

```
HTTP-обработчики (пользовательские, scoped)
  → builtin Quasar (stateful: token/sync/device_list/check_token/register-семантика)
  → ЭНДПОИНТЫ (каталог, enabled, host+method+path)
  → 404 fallback
```

Ключевые решения:
- Эндпоинты не затеняют builtin: `/get_sync_info` 409 не «лечится» реплеем —
  конфиги сидируются в состояние устройств (ниже). Builtin-ответ (включая 403/409)
  всегда приоритетнее каталога.
- Если ответ захвачен и включён — эндпоинт обслуживает запрос локально, до
  честного 404 дело не доходит.
- Выключенный (enabled=false) эндпоинт полностью прозрачен для dispatch —
  поведение ровно как до его создания.
- Неизвестный маршрут без обработчика/builtin/эндпоинта — честный 404
  `emulator_handler_not_found`; прозрачно пустить конкретный маршрут можно только
  режимом прокси («Прозрачно»).

### Модули

1. `server_emulator/endpoints.py` — `EndpointCatalog(directory)`:
   загрузка/валидация файлов, `find(host, method, path)`, `toggle`, `upsert`,
   `remove`, `public()` (список + сводка). Битый файл пропускается (как сценарии).
2. `server_emulator/runtime.py`:
   - `self.endpoints = EndpointCatalog(self.directory / "endpoints")`;
   - dispatch: ветка после builtin (перед 404);
   - admin-API action `endpoints` (в списке разрешённых): GET — каталог,
     PUT `{id, enabled}` — переключатель, PUT `{endpoint}` — upsert,
     DELETE `?id=` — удаление.
3. `server_emulator/ui/`:
   - index.html: кнопка «Эндпоинты» + секция;
   - `endpoints.js`: группы-хосты, галочки (эндпоинт + группа), таблица
     (метод/путь, статус, размер, время захвата), JSON-редактор, удаление;
     поллинга нет — по кнопке «Обновить».
   - app.js: регистрация таба.
4. `tools/endpoints_recover.py` — восстановление из traffic.sqlite3:
   - режим `--all`: для каждого маршрута, который хотя бы раз получал эмулированный
     40*, ищет последний реальный (не эмулированный) 200-ответ; если найден —
     пишет `state/endpoints/<id>.json`;
   - режим `--host H --method M --path P`: точечное извлечение;
   - тело достаётся из payloads/blocks (zstd-блоки), content-type — из заголовков
     ответа capture-события;
   - `--sync-configs`: для каждого известного эмулятору устройства достаёт последний
     реальный ответ `/get_sync_info` и заливает `state.quasar.config` (config/tags/
     glagol/subscription) через admin-API `identities` (operation=state) — персональный
     захваченный конфиг вместо встроенного дефолта для legacy-пути.

### Конфиги sync: персональный ответ из захвата

Реальный ответ `/get_sync_info` содержит `config` (device_config и пр.), `tags`,
`glagol`, `subscription`, `alice_pro_subscription` — именно их ждёт builtin
(quasar.py: get_sync_info). Утилита заливает их в `state.quasar` устройства;
персональные ключи из capture (если есть) вычищаются (оставляем структуру).
Без legacy-конфига устройство получает встроенный источник generated
(см. `docs/contracts/quasar-config.md`).

Восстановлено из захватов 19 эндпоинтов (16 маршрутов + device_list/check_token/
set_device_capability после смягчения builtin: неопределимый вызывающий и protobuf-тело
capability уступают каталогу). Подписка в состоянии устройств включена вручную:
реальное облако для этого аккаунта отдаёт invalid_subscription_user — колонки и на
облаке говорили бы «Продлите подписку». Без ответа остаются gproxy/get_tv_promo_phrases
и gproxy/get_search_suggests — косметика ТВ-меню (4 запроса/сутки), облако отклоняет
крафтовые повторы (нужна живая сессия станции); захватятся естественным трафиком в
следующем proxy-окне — тогда `endpoints_recover.py --all` доберёт их.

## OAuth и Passport: oauth.yandex.ru, mobileproxy.passport.yandex.net

Режим-зависимые builtin-заглушки авторизации и профиля аккаунта (`oauth_stubs.py`); решение —
сразу после `route_mode_for`, до каталога эндпоинтов (см. `http.md`, «Режимы маршрута устройств»):

- `settings["oauth"]["mode"]`: `auto` (по умолчанию) — emulate/hybrid → заглушки, proxy → туннель
  наверх; `emulate` — всегда заглушки; `upstream` («напрямую») — всегда туннель. Читается живьём;
  карточка «OAuth / Passport» — «Дополнительно → Сервер». Туннель — механизм «Напрямую»
  (upstream-мост, петлезащита резолвера); сломанный JSON наверху при статусе 200 — честная
  502 колонке, мусор не отправляется.
- `POST /token` и `POST /1/token` (urlencoded): `grant_type=authorization_code` →
  `{access_token: "VAT."+sha256('access'+code+device_id), expires_in: 315360000,
  refresh_token: "VRT."+sha256('refresh'+code+device_id), token_type: "bearer"}`;
  `grant_type=x-token` → access_token эхом + `{uid, expires_in, token_type}`; иной grant_type →
  400 `{"error": "unsupported_grant_type"}` (RFC 6749 §5.2). Токены детерминированные,
  нигде не хранятся и не проверяются — как в образце (quasar-server, oauth.yandex.ru.ts);
  префиксы VAT./VRT. — маркер заглушки: `music.note_token` их не захватывает, настоящий
  токен аккаунта в кеше поставщика музыки не затирается. `uid` — число: SDK паспорта
  колонки разбирает его как long, строковый идентификатор роняет разбор ответа — сессия
  аккаунта не восстанавливается, и после перезагрузки колонка остаётся на сплэше загрузки.
  Если через «Подтянуть из аккаунта» (`account.AccountSession`) привязан настоящий
  Яндекс-аккаунт — uid настоящий (из `short_info` при входе); иначе детерминированный
  синтетический по id пользователя реестра (sha256, первые 4 байта).
- `POST /1/bundle/oauth/token_by_sessionid` → `{access_token: "VAT."+hex(32), expires_in,
  status: "ok", token_type: "bearer"}`.
- `GET /1/bundle/account/short_info` → каркас полей из образца, персональное — из реестра:
  владелец устройства, иначе первый владелец users; uid/master_uid и uid в master_members —
  тот же uid, что и в `/token` (настоящий привязанного аккаунта или синтетический),
  display_name/имя/логин — из users.
- `GET /1/am/sso/config.json`, `/1/am/config.json`, `/1/badges-specification.json` — статика
  из образца (`oauth_static/`, MIT — NOTICE.md).

## Встроенные ответы по образцу: register, OTA v3, телеметрия

Образец — код quasar-server; ответы встроенные (пользовательский эндпоинт перекрывает, кроме
отмеченного), в «HTTP без эндпоинтов» не попадают:

| Host | Endpoint | Ответ |
|---|---|---|
| quasar.yandex.net | `POST /register?platform=&firmware_version=` | `{hasCriticalUpdate: <из политик OTA>, status: "ok"}` — custom с записями → true, deny/allow/пустой custom → false |
| quasar.yandex.net | `POST /tv/api/v3/updates` | `{bundles: [], updates: []}` |
| quasar.yandex.net | `GET /tv/api/v3/ota` (заголовок x-yaquasarplatform) | `{}` или объект прошивки **только** из каталога прошивок эмулятора (`updates.firmware_for`, с учётом политики: deny → `{}`, custom → первый свой объект, allow → свежая запись каталога по платформе) |
| rpc.alice.yandex.net | `POST /gproxy/push_events_batch` | 200, пусто, `application/x-protobuf` |
| clck.yandex.net | `POST /quasar_metrics/write/batch` | 200, тело `0x0A 0x02 0x4F 0x4B` (protobuf field1="OK"), `application/x-protobuf` |
| clck.yandex.net | `POST /quasar_metrics/write/json/batch`, `/smart_display_errors/write/json/batch`, `/quasar/write/json/batch` | массив `[{"ResponseStatus": "OK"}] × N` (N = длина присланного массива; не массив → 500) — диспетчер |
| report.appmetrica.yandex.net | `POST /report` | `{"status": "accepted"}` (прочие пути appmetrica — прежнее пустое «принято») |
| scbh.yandex.net | `GET /generate_204` | 204 + `X-Start-Time: <микросекунды строкой>` |
| startup.mobile.yandex.net | `GET /analytics/startup` | `{device_id: {hash: <10 цифр>}, query_hosts: {list: {9 доменов, как у образца}}, uuid: {value: <hex16>}}` — домены яндексовые (до них колонка добирается DNS-алиасами владельца), кроме `report`/`diagnostic`: для «Эмуляции» и «Вместе» — `http://<local_domain>` (приём `/report` на локальном домене); hash/uuid одноразовые |

Порядок прежний: пользовательский эндпоинт перекрывает builtin, builtin отвечает раньше
честного 404. Автозаполнение из промахов (`tools/endpoints_recover.py`) сохраняет только
реальные ответы со статусом 2xx — отказ наверху (≥400) не образец ответа. Уже сохранённые
такие записи (source `miss`, статус ≥400) при поиске пропускаются: колонке уходит
builtin/дальнейшая цепочка, запись остаётся на диске и в UI — решение за владельцем.

## glagol: токен и сертификаты устройств

- `GET /glagol/token?device_id&platform` — HS256-токен целевого устройства (v9 §8: только
  устройства одного владельца), claims: `{plt: platform (из query), sub: device_id,
  uid: владелец, iss: 'quasar-backend', aud: 'glagol', iat/exp: 24 ч}`. Проверка
  `/glagol/check_token` plt не смотрит (нестрогая — отсутствие/несовпадение не ломает).
- `glagol.security` — сертификат локального glagol-сервера колонки (glagold, P2P
  мультирум/тандем): RSA 2048, самоподписанный X.509 (serialNumber '01', CN=localhost,
  O=Yandex, C=RU, срок 30 лет, SHA-256; образец — generateGlagolSecurity() quasar-server).
  Отдаётся в `/get_sync_info` поверх собранного конфига и у каждого устройства
  `/glagol/device_list`. Единственная точка чтения — `quasar.glagol_security`:
  параметры пользователя (device.glagol_cert / device.glagol_key) сильнее автогена;
  не задано ни то ни другое — пара генерируется (cryptography) и запоминается в
  `state.glagol_security` при регистрации устройства или первом опросе (кто раньше);
  повторные вызовы не перегенерируют. Задан ровно один параметр или пустой — полей нет
  (пара неполна). Приватный ключ — секрет: в API/UI (`GET identities`, образец `quasar_source` generate) — маска,
  в журнал не пишется; тесты — генерация на лету, ключей в фикстурах нет.

## Редактор и динамика

- Тело эндпоинта — два источника: `body_base64` (статичный захват) и `template`
  (JSON-шаблон; строки-листья несут `{{плейсхолдеры}}` контекста запроса:
  device_id, platform, client_ip, host, method, path, now, now_ms, now_iso, uuid).
  При наличии template тело генерируется на каждом запросе (endpoints.serve).
- UI-редактор эндпоинта: визуальный режим (включён/название/метод/статус/хост/путь/
  content-type; источник тела — захват или шаблон; дерево JSON-тела: булевы —
  чекбоксы, числа — number, строки — text с {{токенами}}; «переложить захват
  в шаблон») и режим «JSON (расширенный)» с полным определением.
- Вкладка «HTTP · Quasar»: поверх JSON-конфига SN — переключатели подписки
  (subscription.enabled + снятие disabled_reason), Alice Pro и поле tags;
  правят тот же JSON, сохранение общей кнопкой.

## Разделы, системные записи, затенение

- Файлы по разделам, как библиотека сценариев: `state/endpoints/<category>/<id>.json`.
  Раздел — поле `category` (латиница, цифры, `_ . -`); без него раздел берётся по хосту:
  `quasar.yandex.net` → `quasar`, `rpc.alice.yandex.net` → `gproxy`, `egress.yandex.net` → `egress`,
  иначе `other`. Плоские файлы старого формата при загрузке переносятся в свой раздел (`_group` удаляется).
- URL (host/method/path) редактируется в UI. Два эндпоинта с одним URL недопустимы (ValueError).
  Смена раздела переносит файл.
- GET `api/endpoints` дополнительно отдаёт системные записи (`readonly: true`, `owner` — вкладка-владелец):
  builtin-маршруты Квазара (`owner: quasar-http`, например `/get_sync_info` — конфиг из вкладки «Квазар»)
  и OTA-эндпоинты (`owner: updates`, раздел `updates`). Они не правятся и не выключаются здесь;
  в UI вместо кнопок ссылка на вкладку-владельца.
- URL, который обслуживает вкладка «Квазар» или «Обновления», JSON-эндпоинтом не сохраняется (ValueError);
  системный маршрут показывается один раз — у того, кто его реально обслуживает (порядок dispatch: обновления → Квазар).
- Ответ: `{endpoints, categories, total, enabled}`; `total`/`enabled` считают только свои записи.

## Зафиксированные запросы и редактор-окно

- Запросы, на которые нет ответа (404 в dispatch), запоминаются в `state/endpoints_misses.json`: ключ `"<METHOD> <host><path>"`, поля host, method, path, count, first_ts, last_ts, query, request_base64 (первые 4 КБ тела), request_size, content_type. Хранится не более 200 записей, старые вытесняются. Каталог приложений (`/gproxy/get_app_catalog`) не фиксируется.
- GET `api/endpoints` отдаёт `misses` — только те, для которых ещё нет описанного эндпоинта. DELETE `api/endpoints?miss=<ключ>` убирает запись, `?miss=*` очищает список.
- UI раздела: сверху «Зафиксированы без ответа» с кнопкой «+» (создать JSON-эндпоинт из запроса); затем свёрнутые разделы своих JSON-эндпоинтов (кнопки «Редактировать», «Отключить», «Удалить»); в конце — маршруты вкладок «Квазар» и «Обновления» (только просмотр, владелец указан в шапке раздела).
- Редактор — модальное окно: метод/хост/путь, статус, Content-Type, раздел, тело в редакторе кода. Режим «Ответ как есть» сохраняет текст как body_base64, режим «Шаблон с {{токенами}}» — как template (текущий JSON становится шаблоном, токены вставляются в позицию курсора). Двоичные тела не редактируются текстом, заменяются файлом.

## «С оригинала» — только режим прокси

- Кнопка «С оригинала» в окне редактора запрашивает текущий метод/хост/путь у настоящего сервера и подставляет статус, Content-Type и тело ответа в форму (сохранение — обычной кнопкой «Сохранить»).
- Кнопка и API запроса к оригиналу в эмулятор не входят: их подключает внешний код через `runtime.ui_extensions` (см. ниже); в самостоятельном эмуляторе кнопки нет.
- Точки расширения эмулятора (без кода прокси): `runtime.ui_extensions` — список скриптов, которые UI загружает из `api/status` (`ui_extensions`); редактор эндпоинтов публикует API через событие `endpoint-editor-ready` и `window.endpointEditor()`: `actions` (ряд кнопок окна), `request()` (метод, хост, путь, query, заголовки и тело зафиксированного запроса), `apply({status, content_type, body_base64})`.
- Зафиксированные запросы теперь хранят заголовки (кроме hop-by-hop: Host, Content-Length, Connection и т.п.) — они уходят на оригинал вместе с query и телом, поэтому эндпоинт, созданный по «+», запрашивается у оригинала с заголовками устройства. Для эндпоинта без зафиксированного запроса заголовков устройства нет — оригинал может ответить 400/401.

## Обработчики кодом — режим «скрипт»

- поле `mode` эндпоинта: `static` (тело `body_base64`), `template` (JSON с `{{токенами}}`) и `script` — код в `<id>.py` рядом с `<id>.json` в той же папке раздела `state/endpoints/<раздел>/`;
- в режиме `script` сервер на каждый запрос вызывает функцию `handle` модуля `<id>.py`;
- сигнатура `handle(request)` или `async def handle(request, emulator)`; поля `request`: `method`, `host`, `path`, `query` (dict), `headers` — без учёта регистра, `body` (bytes), `text()`, `json()`, `client_ip`, `device_id`, `device` (запись реестра или `None`); `emulator` — живой `Runtime` (параметры, реестр, хранилище);
- что можно вернуть: `dict`/`list` → JSON, `bytes`/`str` → тело с `content_type` из JSON-файла, `reply(body, status=, content_type=, headers=)` — полный контроль, `None` — ответ из JSON (static/template);
- пример обработчика, отвечающего по заголовку `X-YaQuasarPlatform`:

```python
from server_emulator.endpoint_scripts import reply


async def handle(request, emulator):
    platform = request.headers.get("X-YaQuasarPlatform", "")
    return reply({"platform": platform, "device_id": request.device_id})
```

- модуль перечитывается при изменении файла `<id>.py`; таймаут async-обработчика 10 с; исключение → `500 {"error":"endpoint_script","detail":...}` и запись в журнал ошибок, сервер работает дальше;
- API: `GET api/endpoints?script=<id>` → `{code, stub}`; `PUT api/endpoints` `{endpoint, script}` — код проверяется на синтаксис до записи; переезд в другой раздел переносит `.py`, удаление эндпоинта удаляет `.py`;
- безопасность: код исполняется в процессе сервера с полными правами — правка доступна только через защищённое админ-API (`X-Emulator`, авторизация).
- protobuf → скрипт: у двоичного тела с типом `*protobuf*`/`grpc` в редакторе кнопка «Разбор» — `POST api/endpoints-protobuf` `{id, body_base64, content_type}` → `{code}`: скрипт с `MESSAGE` (разбор без схемы, `server_emulator/protowire.py`: `(номер поля, значение)`, вложенные сообщения — списки, `fixed32`/`fixed64`; вложенный `google.protobuf.Struct` — `Struct({ключ: значение})` с настоящими ключами: это единственный тип в ответах `/gproxy/*` с известной схемой — дескрипторы прошивки `schemas/` их типов не содержат, прокси тоже разбирает их без схемы), `handle` отдаёт `encode(MESSAGE)`; разбор проверяется побайтно (`encode(decode(x)) == x`), иначе ошибка. Редактор переходит в режим скрипта, тело остаётся запасным ответом;
- схемы protobuf: кнопка «Импорт proto» (вкладка «Эндпоинты») — zip с файлами `.proto` (пути каталогами или сплющенные `a__b__c.proto` → `a/b/c.proto`, как в `a311_research/gproxy_research/proto_extracted.zip`), после выбора — режим: «Обновить» (файлы архива заменяют одноимённые, остальные остаются) или «Заменить» (набор только из архива). `POST api/endpoints-proto-import?mode=update|replace&name=` с телом-архивом (до 64 МБ) → `server_emulator/proto_schemas.py` собирает их protoc из grpcio-tools (`google/protobuf/*` — свои у protoc; у полей proto2 без метки возвращается `required`; отсутствующие импорты — пустые заглушки; файлы, которые не собираются, отбрасываются по одному проходу protoc с причиной) и пишет `state/proto/quasar.pb` (FileDescriptorSet, формат `schemas/*.pb`) + `quasar.json` (источники, отброшенные файлы и заглушки, привязки методов gproxy к типам ответов). `GET` — сведения и имена типов. Общая папка-обёртка архива («proto_final/…») срезается, если без неё находится больше импортов; файлы с теми же типами, что у другого (архив из нескольких источников), — дубликаты: вместо них `import public` на оставленный; копии `package google.protobuf` пропускаются; импорт, отличающийся от файла только регистром («common.proto» → `Common.proto`), переписывается на имя файла (на Windows заглушка затёрла бы файл); у полей в `oneof` снимаются метки; protoc останавливается на первом файле с ошибкой, поэтому следующий проход продолжается с него. Для `proto_final.zip` (1383 файла, ~25 с): собрано 803, дубликатов ~445, не собрались 143 (в основном типы из файлов, которых нет в архиве);
- «Разбор» со схемами: тип — выбранный в поле рядом или найденный по пути (`/gproxy/<метод>` → известная таблица или `T<Метод>Response`); скрипт — `MESSAGE` по именам полей (protobuf JSON), `handle` отдаёт `encode_message(тип, MESSAGE)`. Тело, не совпавшее со схемой по смыслу или длине (неизвестные поля; порядок ключей map не важен), разбирается без схемы с пояснением;
- тело с объявленным текстовым типом (JSON, `text/*`) показывается в редакторе как текст, даже с управляющими байтами; эвристика «двоичное» — только когда тип не указан.