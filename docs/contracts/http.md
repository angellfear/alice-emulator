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
| POST/GET /gproxy/get_app_catalog | при включённом каталоге: если своего ответа нет — берётся наблюдаемый protobuf-каталог (без credentials), патчится под адрес эмулятора (media/WS URL), content-encoding снимается корректно; gzip-каталог >8 MiB отклоняется | test_catalog, test_quasar_backend |
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

### Режимы маршрута устройств (TASK-044)

Ось «кто отвечает» — у каждого устройства своя: `route_mode` в реестре (identities), устройства
без своего маршрута и неизвестные устройства — всегда `emulate` (TASK-064: маршрута по умолчанию
нет). Глобального `settings.mode` больше нет (запрет режимов для standalone снят — проход наверх
свой, TASK-043). Миграция: `mode: emulate` → `emulate`, `mode: proxy` → `proxy` и опция
`record: on` устройствам без своего маршрута, SN из `emulation_excluded` → `route_mode: proxy` (очередь в meta `route_migration`,
применяется и устройствам, подключившимся позже); ключи `mode`/`emulation_excluded` удалены.
Прежний маршрут `record` (TASK-054): `route_mode: record` → `proxy` + `record: on`,
`default_route_mode: record` → `proxy` + `record: on` устройствам без своего маршрута (однократно,
meta `record_migration`); `record` как значение `route_mode` — 400. Прежний
`settings.default_route_mode` ≠ `emulate` однократно записывается как `route_mode` устройствам
реестра без своего маршрута, ключ удаляется из настроек (`PUT /api/settings` его игнорирует).

| Режим | HTTP | WS |
|---|---|---|
| `emulate` (Эмуляция) | эмулятор | эмулятор |
| `hybrid` (Гибрид) | эмулятор, где есть обработчик; промах — upstream, промах остаётся в «Без ответа» с отметкой «ушёл в Яндекс» | эмулятор (фаза 1); пересылка отдельных необработанных сообщений — фаза 2 |
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

#### Опция устройства «Запись» и журнал трафика (TASK-054)

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

### H11: HTTP-маршруты эмуляции — три состояния

Неизвестный эмуляции HTTP-маршрут (нет обработчика, builtin и OTA-политики) авторегистрируется в реестре (`server_emulator/http_routes.py`, state-файл `http_routes.json`, ключ — канонический host+method+path) **выключенным**, логируется (счётчик hits/last_ts + recent) и получает локальный 404 `emulator_route_disabled` (запись с emulated=true). Состояния:

- **enabled** — обслуживается локально; без обработчика — локальный честный 404 `emulator_handler_not_found`.
- **transparent** — обмен молча уходит upstream куда шёл, НЕ записываясь в захват (ctx capture_skip; read-once тело запроса переносится с собой). «Подмены» продолжают применяться.
- **disabled** — локальный 404 `emulator_route_disabled`.

Реестр действует только в режиме эмуляции; каталог приложений (`/gproxy/get_app_catalog` при включённом каталоге), OTA-эндпоинты и `/glagol/token` — известные маршруты, гейтом не перехватываются. API: GET/PUT/DELETE `/emulator/api/http_routes`; UI — вкладка «HTTP · Quasar». Тесты: tests/test_http_routes.py, tests/test_mode_enum.py.

## Обновление 2026-11-01: кабинет пользователя `/me` (TASK-062)

Реализация: `server_emulator/ui/api/me.py`, страница `server_emulator/ui/me.html`, вход — `server_emulator/auth.py`. Тесты: `tests/test_me_cabinet.py`.

| Маршрут | Запрос | Ответ | Доступ |
|---|---|---|---|
| GET /me | — | страница кабинета (`me.html`; также `<prefix>/me`) | без входа |
| GET /api/me/params[?user=<id>] | — | `{user, user_name, params, devices, users?}`: `params` — определения области `user` с `user_editable` и значением владельца; `devices` — устройства пользователя `{sn, name, room, model, platform, status (online/seen/offline), last_seen, params}` (определения области `device`); `users` — только админу | сессия; пользователь — свой дом (`identities_user`), `?user=` игнорируется; админ и открытый режим — дом из `?user=` (иначе свой/первый) |
| PUT /api/me/params | `{key, value, owner?}` (`owner` — `user:<id>` или `device:<sn>`, по умолчанию свой) | `{owner, key, value}` | только `user_editable` своих владельцев; 403 `not_editable` / `bad_owner` / `not_your_device`, 400 — ошибка значения; нужен `X-Emulator: 1` |
| PUT /api/me/devices | `{sn, name?, room?}` | `{sn, name, room}` | своё устройство (иначе 403 `not_your_device`): реестр и параметры `device.name` / `device.room` |
| POST /emulator/api/me_link | `{user}` | `{url}` | только админ |

Ошибки доступа: нет сессии при включённом входе — 401; вход не привязан к пользователю реестра (или в реестре нет пользователей) — 409 `{"error":"principal_not_linked"}`.

Вход админа в кабинет без логина. Вход включён (`auth.json`) — `me_link` создаёт токен (в памяти, 12 ч, привязан к пользователю) и возвращает `{"url":"/me#t=<токен>"}`; открытый режим — `{"url":"/me?user=<id>"}`. Токен во фрагменте адреса, поэтому не попадает в логи сервера и прокси; страница кладёт его в `sessionStorage`, убирает из адреса и шлёт заголовком `X-Me-Token`. Заголовок действует только на `/api/me/*` и `/api/auth/whoami` (запрос идёт как сессия `role: user` этого пользователя); на остальных путях он игнорируется (401/403 как без сессии). `whoami` с токеном — `{authenticated, name, role: "user", via: "link"}`. Cookie админа не затрагивается. Выход админа (его токены) и `revoke_all` (смена пароля и т. п.) гасят токены кабинета.

## Обновление 2026-11-01: журнал сервера (TASK-068)

Админ-API «Дополнительно → Лог» (`ui/api/server_log.py`, хранилище — `server_log.py`,
`state/server_log.sqlite3`, хранение 3 дня, повторы в пределах часа склеиваются):

| Маршрут | Запрос | Ответ | Доступ |
|---|---|---|---|
| GET /emulator/api/server_log | query: `level` (имя или имена через запятую), `source`, `q` (подстрока сообщения/устройства), `before` (ts — пагинация), `limit` (до 500) | `{items, sources, more}` — записи новые сверху: `ts` (первый случай), `last_ts`, `count`, `level`, `source`, `message`, `device_sn`, `detail` (JSON: хост, путь, код, причина — без заголовков и тел) | только админ при включённом входе |
| DELETE /emulator/api/server_log | — | `{cleared: true}` | только админ; нужен `X-Emulator: 1` |

## Обновление 2026-11-01: перехват OTA в «Прозрачно» (TASK-066)

Обновления прошивки (эндпоинты `updates.py`: `/tv/api/v4/updates`, `/check_updates`,
`/allow_update`, `/confirm_update` и правила расширений) перехватываются **при любом маршруте
устройства**, включая `proxy` («Прозрачно»): в `Dispatcher.handle` ветка proxy перед прямым
проходом спрашивает `runtime.updates.match(canonical_host, path, method)` — совпало → ответ по
политике «Устройства → Обновления» (запретить/разрешить/свой ответ), апстрим не вызывается.
Запрос оригинала для каталога URL выполняется мимо маршрута (прямая сессия) с заголовком
`X-Alice-Update-Probe: 1`; ответ на такой маркер — 508 (петля закрыта). Журнал — как в эмуляции
(`journal_emulated` по опции «Запись»). Остальной трафик «Прозрачно» не меняется.

## Обновление 2026-11-01: встроенные ответы без эндпоинтов (TASK-070)

Промахи рабочего сервера («HTTP без эндпоинта») обслуживаются встроенными ответами
(`endpoints.py: BUILTIN_SILENCERS`, вызов — `Dispatcher.builtin_answer` в fallback'е, после
каталога эндпоинтов). Пользовательский эндпоинт с тем же URL перекрывает встроенный (защиты URL,
как у Квазара/Обновлений, нет). В «HTTP без эндпоинта» и в журнал трафика («Запись») они не
пишутся (шум); в каталоге видны в группе «Встроенные ответы».

| Хост и путь | Ответ |
|---|---|
| `POST clck.yandex.net/*` (quasar_metrics/tv/quasar/smart_display_errors) | 200, пустое тело |
| `POST report.appmetrica.yandex.net/report`, `POST rosenberg.appmetrica.yandex.net/location` | 200, пустое тело |
| `HEAD/GET scbh.yandex.net/generate_204` | 204 без тела |
| `GET egress.yandex.net/` | 200 text/plain — адрес клиента, с которого пришёл запрос (семантика «твой исходящий IP») |
| `GET soft.export.yandex.ru/status.xml` | 200 `text/xml` `<status>ok</status>` |
| `POST rpc.alice.yandex.net/gproxy/get_auth_suggests`, `get_teasers` | 200 `{}` |
| `GET keyboard.yandex.net/experiments` | 200 `{}` |
| `GET yandex.ru/ads/v1/startup` | 200 `{}` |
| `GET avatars.yandex.net/get-music-content/…`, `get-video-content/…` | пересылка настоящему серверу (цель — `upstream.target_for_host`, алиасы учитываются), ответ кешируется в `state/cache/avatars` (лимит 200 МБ, срок 7 дней, вытеснение самого старого); без сети — кеш (даже протухший), затем 404; Range-запросы проходят насквозь без кеша |

Форматы, для которых в журнале не нашлось образцов (метрики, status.xml, `{}`) — безопасный
минимум «успех»; если колонке потребуется что-то другое, ответ видно в «Прозрачно» и
перекрывается своим эндпоинтом. Запросы браузера к самой панели (`favicon.ico` — отдаётся
`tools/assets/alica.ico`; `/`, `/emulator/*`, `/me` — маршруты панели) — не промахи.

## Обновление 2026-11-01: пробелы из журнала (TASK-072)

По отчёту `tools/journal_gaps.py` (журнал «Запись» → `reports/journal-gaps-<дата>.md`) закрыты
оставшиеся HTTP-промахи колонки — там же, где встроенные ответы TASK-070 (перекрываются своими
эндпоинтами, в промахи и «Запись» не пишутся):

| Запрос | Ответ |
|---|---|
| `POST quasar.yandex.net/push_subscribe` | 200 по образцу Яндекса: `{"sign": <случайный hex>, "status": "ok", "ts": <сейчас>}` (динамический, `Dispatcher.builtin_answer`); push-канал эмулятор не ведёт |
| `POST rpc.alice.yandex.net/gproxy/centaur_get_gallery` | 200 `application/x-protobuf`, пустое тело — персональная галерея без аккаунта недоступна (в «Прозрачно» галерея работает как раньше) |
| `POST quasar.yandex.net/set_device_capability` | 200 `{"status":"ok"}` — принято; образца успеха у Яндекса нет (в журнале только 403/429), состояние возможностей колонки приходит и по WS и хранится с TASK-069 |
