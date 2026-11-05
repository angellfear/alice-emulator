---
title: "Контракты HTTP (Quasar-бэкенд, OTA, каталог)"
status: stable
date: "2026-10-25"
module: "server_emulator/quasar.py"
---

# Контракты HTTP (Quasar-бэкенд, OTA, каталог)

Реализация: `server_emulator/quasar.py`, `server_emulator/updates.py`, `server_emulator/catalog.py`, диспетчер `server_emulator/runtime.py:dispatch`. Происхождение: захват + v3rt3p/quasar-server (NOTICE в quasar.py). Тесты: `tests/test_quasar_backend.py`, `tests/test_updates.py`, `tests/test_scenario_catalog.py`.

## 1. Quasar HTTP (хост quasar.yandex.net, сработает только для назначенного устройства)

| Endpoint | Запрос | Ответ | Side effect | Тест |
|---|---|---|---|---|
| GET /check_updates | — | `{"hasUpdate":false}` | нет | test_quasar_backend |
| GET /get_sync_info | device_id (query/header) | `{status,config,tags,glagol?,subscription?}` из state.quasar; источник «Напрямую» — ответ Яндекса с заменами regexp устройства (`state.quasar_direct_replace`, сломанный JSON не уходит), см. quasar-config.md | нет; 409 `device_config_not_set` без конфига | test_quasar_backend, test_quasar_direct_replace |
| GET /glagol/device_list | назначенный SN | устройства того же владельца: id/name/platform/networkInfo/config/deviceConfig | нет | test_quasar_backend |
| GET /glagol/token | device_id | HS256 JWT (sub=DeviceId, uid=user, exp 24 ч), ключ в quasar-secret.json | нет | test_quasar_backend |
| POST /glagol/check_token | JWT в теле | `{status,valid}` | нет | test_quasar_backend |
| POST /glagol/v2.0/check_token | JWT | `{status,owner,guest}` | нет | test_quasar_backend |
| POST /update_device_state | JSON networkInfo | `{status:ok}` | state.networkInfo устройства | test_quasar_backend |
| POST /set_device_capability | JSON capabilities | `{status:ok}` | state.reported_capabilities | test_quasar_backend |

Ошибки доступа: устройство без владельца → 403 `device_not_assigned` (кроме /check_updates). DeviceId — только алиас; несуществующее устройство попадает в unassigned и не получает чужой профиль.

## 2. OTA-обновления (updates.py)

Endpoint'ы (по умолчанию, расширяются списком до 100): POST quasar /tv/api/v4/updates (kind=list), GET /check_updates (check), GET /allow_update (allow), GET /confirm_update (confirm).

Поведение на запрос: политика per-SN (allow/deny/custom, override устройств); при любом обращении к upstream запрос помечается `X-Alice-Update-Probe: 1` (защита от петли), ответ ≤8 MiB, все найденные URL прошивок пишутся в updates.sqlite3 (attempts/urls/sightings) без credentials.

- deny → `{updates:[]}` / `{hasUpdate:false}` / `{allow:false}` / `{status:blocked}` (200), оригинал сохраняется в capture.
- custom → сохранённый список updates (URL-валидация, без credentials/fragment).
- allow → ответ реального upstream; при недоступности `update_upstream_unavailable`.
- Loop-защита: повторный вход probe → 508 `update_probe_loop`.

Тесты: test_updates (deny всё же наблюдает URL, custom per-SN, allow сохраняет статус). Ограничение: режима `upstream_observation off` (полный запрет) ещё нет — C02.

## 3. Каталог приложений (rpc.alice.yandex.net)

| Контракт | Поведение | Тест |
|---|---|---|
| POST/GET /gproxy/get_app_catalog | перехватывается при любом маршруте устройства (как OTA); режим каталога из catalog.json: `direct` — ответ Яндекса как есть, `merged` — ответ Яндекса и свои приложения, `own` — только свои. База direct/merged — живой запрос к Яндексу (мост upstream, петлю закрывает резолвер) с кешем последнего удачного ответа (`data/catalog/upstream.bin`); нет ни облака, ни кеша — merged отдаёт только свои, direct — 502. Запись приложения: поле 8 (varint) — versionCode, по нему колонка решает «Обновить»; gzip-каталог >8 MiB отклоняется | test_app_catalog, test_catalog |
| GET /__catalog/icon/{hash} | иконка APK из хранилища каталога | test_catalog |
| GET /__catalog/apk/{hash} | скачивание APK с ограничениями | test_catalog |
| Admin: GET/PUT /api/catalog, POST /api/catalog/apk, PUT/DELETE /api/catalog/apps/{id} | инвентарь/загрузка/правки | test_catalog |

## 4. Fallback маршрутов

Разделы «Обработчики» и «Поведение» удалены (2026-10-25). Порядок обслуживания маршрута: builtin Quasar → каталог захваченных эндпоинтов (state/endpoints, `docs/contracts/endpoints.md`) → локальный честный 404 `{"error":"emulator_handler_not_found"}`. Тест: test_emulator_runtime.

## 5. Общие инварианты

- Клиентский device_id извлекается из query `device_id`, заголовков `X-YaQuasarDeviceId` / `x-uprx-device-id`; IP не участвует в принадлежности.
- Ответы не содержат cookies/private keys владельца; JWT выдаётся только для устройств с назначенным владельцем.
- Standalone и embedded дают одинаковые ответы (один runtime.dispatch); в embedded дополнительно пишется capture-запись с `emulated=true`.

## Обновление 2026-10-24: маршрутизация и идентичность

### /glagol/token — вызывающий ≠ цель
Query `device_id` — ЦЕЛЕВОЕ устройство (токен «для него»); вызывающий определяется заголовками (X-YaQuasarDeviceId > x-uprx-device-id). Ветка: непривязанный вызывающий → 403 caller_not_assigned; неизвестная цель → 404 target_unknown; владелец цели ≠ владелец вызывающего → 403 forbidden. Без заголовков — прежняя семантика «токен для себя». Цель resolve-only (не регистрируется как наблюдение вызывающего). Тесты: tests/test_glagol_token.py.

### Режимы маршрута устройств

Ось «кто отвечает» — у каждого устройства своя: `route_mode` в реестре (identities), устройства
без своего маршрута и неизвестные устройства — всегда `emulate`. Глобального `settings.mode` больше нет (запрет режимов для standalone снят — проход наверх
свой, механизм прохода наверх `upstream/`). Миграция: `mode: emulate` → `emulate`, `mode: proxy` → `proxy` и опция
`record: on` устройствам без своего маршрута, SN из `emulation_excluded` → `route_mode: proxy` (очередь в meta `route_migration`,
применяется и устройствам, подключившимся позже); ключи `mode`/`emulation_excluded` удалены.
Прежний маршрут `record`: `route_mode: record` → `proxy` + `record: on`,
`default_route_mode: record` → `proxy` + `record: on` устройствам без своего маршрута (однократно,
meta `record_migration`); `record` как значение `route_mode` — 400. Прежний
`settings.default_route_mode` ≠ `emulate` однократно записывается как `route_mode` устройствам
реестра без своего маршрута, ключ удаляется из настроек (`PUT /api/settings` его игнорирует).

| Режим | HTTP | WS |
|---|---|---|
| `emulate` (Эмуляция) | эмулятор | эмулятор |
| `hybrid` (Вместе) | эмулятор, где есть обработчик; промах — upstream, промах остаётся в «Без ответа» с отметкой «ушёл в Яндекс» | эмулятор (фаза 1); пересылка отдельных необработанных сообщений — фаза 2 |
| `proxy` (Прозрачно) | upstream | мост upstream |

Решение — `dispatcher.route_mode_for(request) → (sn, device_id, mode)`: заголовки
uprx/quasar → query device_id → реестр (resolve-only); цель строит `upstream.target_for`
(канонический хост из алиасов, https/wss; алиас может задавать полный `http://…` upstream —
тесты). API: `PUT /api/route_mode {sn, mode}` (`mode`: `emulate` | `hybrid` | `proxy`, `null` — снять
свой маршрут, то есть `emulate`) → `{sn, mode, disconnected}` — смена маршрута устройства сразу, без
ожидания колонки. WS устройства закрываются (1012, в фоне; `disconnected` — число закрываемых)
только при переходе в `proxy` или из него: `emulate` ↔ `hybrid` WS не рвёт, HTTP маршрутизируется
заново на каждом запросе. `PUT /api/mode` (маршрут по умолчанию) удалён; `GET /api/identities`
отдаёт у каждого устройства `route_mode`, `record` (поля `default_route_mode` нет). В proxy
устройство регистрируется в реестре при представлении — маршрут виден и сменяем в UI (колонка
«Маршрут» таблицы устройств).

#### Опция устройства «Запись» и журнал трафика

Журнал (`state/traffic.sqlite3`, `GET /api/traffic`) пишется по опции устройства `record`:
`off` (по умолчанию) | `on` | `misses`. `PUT /api/record_mode {sn, mode}` → `{sn, mode}` (400 —
неизвестный режим или SN); соединения не закрываются, опция читается на каждом обмене и кадре.
Устройство без SN не пишется.

| `record` | HTTP | WS |
|---|---|---|
| `off` | ничего | ничего |
| `on` | каждый обмен: ответ эмулятора (`source=emulator`) или Яндекса (`source=upstream`) | `open`/`close`, AAPI-кадры обеих сторон, неизвестные кадры |
| `misses` | запросы без локального обработчика | AAPI-события колонки без обработчика и ответы на них (по `RefMessageId`) |

Локальный обработчик — `Dispatcher.local_handler(host, method, path)` (без побочных эффектов): OTA-эндпоинт,
builtin Quasar (`QuasarBackend.ROUTES`), включённый не-скриптовый эндпоинт, `get_app_catalog`. Запрос с
обработчиком, ушедший в Яндекс в прокси, в `misses` не пишется; промах эмуляции и гибрида — пишется
(`unhandled=1`). WS: `aapi_handlers.has_handler(trigger, settings)` — реестр обработчиков `@aapi_handler`. Звук (бинарный кадр без префикса `AAPI`)
не пишется: в записи `close` — `audio_in_frames`/`audio_in_bytes`/`audio_out_frames`/`audio_out_bytes`.

`GET /api/traffic`: плоский список (`device`, `host`, `source`, `unhandled=1`, `key`, `limit`, `offset`);
`?groups=1[&device_sn=&source=&unhandled=1]` → `{devices: [{device_sn, name, record, groups: [{key,
count, first_ts, last_ts}]}]}`; `?device_sn=&key=[&source=&unhandled=1]` → `{items}` (≤ 10 записей группы);
`?item=<id>` — карточка (тела AAPI — `text_format`); `?export=<id>`; `DELETE [?item=<id>]`. Ключ события:
HTTP — `METHOD host path` без query; WS — `ws → Event.<имя>` (колонка) / `ws ← Event.<имя>` (сервер),
`ws open`, `ws close`, `ws unknown →|←`. На пару (устройство, ключ) хранятся последние 10 записей, `count` —
все случаи.

### H11: HTTP-маршруты эмуляции — отменено

Механизм авторегистрации неизвестных HTTP-маршрутов (`server_emulator/http_routes.py`,
реестр `http_routes.json`, три состояния enabled/transparent/disabled) удалён насовсем
(решение 2026-10-25 — см. `quasar-config.md`): вместо реестра с состояниями —
каталоговые домены с режимами direct/merged/own (задание 074, вкладка «Каталог»).
Прежний текст раздела — в истории git.
