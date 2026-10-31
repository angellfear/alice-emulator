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
| GET /get_sync_info | device_id (query/header) | `{status,config,tags,glagol?,subscription?}` из state.quasar | нет; 409 `device_config_not_set` без конфига | test_quasar_backend |
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

### Режимы при встраивании в прокси (временно: заменяется TASK-043/044)

Режимная модель (решение пользователя 2026-10-24): **ровно 2 режима**. Прежний третий режим «частичная эмуляция» (partial) и построенные на нём H04 (HTTP probe-роутинг), H05 (emulation_ws_local connection split) и H06 (turn-bridge) удалены; сохранённые настройки с partial миграцией переводятся в emulate.

- **proxy (Проксирование)**: всё идёт на реальный сервер. Исключения: фильтры «Подмены» и OTA-политика обновлений (запрос уходит на реальный сервер, ответ может подменяться при deny/custom — действует в обоих режимах, см. reports/updates-policy.md).
- **emulate (Эмуляция через прокси)**: всё обслуживается эмулятором локально (HTTP — dispatch; WS — целиком локальный /uni.ws), КРОМЕ устройств из `settings.emulation_excluded` (SN): их запросы и WS-мосты всегда upstream, смена режима их НЕ рвёт. GET /emulator/api/mode возвращает `{mode, modes, excluded}`.
- **Standalone**: всегда emulate, ядро не зависит от прокси; выбрать proxy нельзя.

### H11: HTTP-маршруты эмуляции — три состояния

Неизвестный эмуляции HTTP-маршрут (нет обработчика, builtin и OTA-политики) авторегистрируется в реестре (`server_emulator/http_routes.py`, state-файл `http_routes.json`, ключ — канонический host+method+path) **выключенным**, логируется (счётчик hits/last_ts + recent) и получает локальный 404 `emulator_route_disabled` (запись с emulated=true). Состояния:

- **enabled** — обслуживается локально; без обработчика — локальный честный 404 `emulator_handler_not_found`.
- **transparent** — обмен молча уходит upstream куда шёл, НЕ записываясь в захват (ctx capture_skip; read-once тело запроса переносится с собой). «Подмены» продолжают применяться.
- **disabled** — локальный 404 `emulator_route_disabled`.

Реестр действует только в режиме эмуляции; каталог приложений (`/gproxy/get_app_catalog` при включённом каталоге), OTA-эндпоинты и `/glagol/token` — известные маршруты, гейтом не перехватываются. API: GET/PUT/DELETE `/emulator/api/http_routes`; UI — вкладка «HTTP · Quasar». Тесты: tests/test_http_routes.py, tests/test_mode_enum.py.
