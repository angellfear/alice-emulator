---
title: "Умный дом: реестр устройств и директивы колонке"
status: draft
date: "2026-11-14"
module: "server_emulator/smart_home.py"
---

# Умный дом: реестр устройств и директивы колонке

Код: `server_emulator/smart_home.py` (реестр и построители сообщений), `server_emulator/sdk.py`
(SDK расширений), `server_emulator/runtime.py` (`_smart_home_push`, пуш после опознания колонки),
`server_emulator/ui/api/smart_home.py` (вкладка). Тесты: `tests/test_smart_home.py`.

## Обзор

Реестр умного дома — сервис ядра: устройства и комнаты, которые наполняют расширения через SDK.
Колонка показывает их в шторке и виджете «Умный дом»; к `iot.quasar.yandex.ru` она не ходит
(запись 2026-11-09/10: обращений нет) — список приходит uniproxy-пушами, действия карточек
приходят тем же каналом, что и реплика.

## Модель реестра

| Сущность | Поля | Примечание |
|---|---|---|
| устройство | `id`, `name`, `room`, `type`, `capabilities`, `properties`, `online`, `household`, `origin` | id — с префиксом источника (`<origin>:…`); `type` — имя enum `EUserDeviceType` схемы станции; capabilities/properties — списки `{type, value}` |
| комната | `id → {name, origin}` | `set_rooms` заменяет только комнаты источника |
| источник (origin) | id расширения | обновлять/удалять устройство и получать действия может только источник устройства |

- Хранение: `state/smart_home.json` — устройства и комнаты переживают перезапуск;
  обработчики действий в памяти — их ставит расширение на импорте.
- `online` в выдаче — производное: флаг устройства И включённость расширения-источника
  (выключили плагин — устройства «не в сети», остаются в реестре).
- Своих планировщиков, опроса состояния и обращений наружу реестр не делает: состояние
  меняют расширения и действия с колонки.

## Сообщения колонке

Конверт (запись 2026-11-10): `Push.Directives[0]` — `client_action`, `IsLedSilent: true`,
`EndpointId.value = SN`, нагрузка — `PayloadRaw` (protobuf, схема станции):

| Директива | Тип PayloadRaw | Когда |
|---|---|---|
| `set_iot_user_info` | `NAlice.TLocalIoTCapability.TSetIoTUserInfoDirective`: `IoTClientUserInfo` — `Rooms{Id, Name, HouseholdId}`, `Households`, `Devices{Id, Name, Type, ExternalId, RoomId, HouseholdId, StatusInfo{Status: Online/OfflineDeviceState, …}, Endpoint{Id, Status{Status: Online/Offline, UpdatedAt}, AnyCapabilities}}` + устройство-колонки (`ExternalId`/`Endpoint.Id` — `<серийник>.<платформа>`, `Type` — enum платформы, `SkillId: Q`, дом общий с устройствами, Endpoint без умений — форма настоящего кадра 2026-11-14; лаунчер колонки берёт для виджетов умного дома дом, где есть её устройство) | изменение реестра — колонкам, объявившим `LocalIoT` (см. [capabilities.md](capabilities.md): адресаты — по объявлению колонки; `LocalIoT` появился в объявлении — пуш сразу, без изменения реестра; пустой список тоже уходит — карточки снимаются); новой колонке — сразу после опознания, если реестр не пуст и она объявила `LocalIoT`. Идентичная перезапись устройства/комнат списка не пушит: снимок подписки расширения не даёт пачки одинаковых кадров (настоящий сервер шлёт список только по реальному изменению, ~десятки раз за сутки, а не на каждый тик датчика) |
| `set_div_variables` | `NAlice.TDivViewCapability.TSetDivVariablesDirective`: переменная `device_<id, всё кроме букв, цифр и «_» → «_»>_<capability>_state` (`LongValue` 0/1, `VariableType: Local`) | после действия колонки — по каждой изменившейся возможности |
| `sync_iot_user_info` | `NAlice.TLocalIoTCapability.TSyncIoTUserInfoDirective` — только `Name` | после каждого действия колонки — сигнал виджетам перечитать |
| `set_iot_account_settings` | `NAlice.TLocalIoTCapability.TSetIoTAccountSettingsDirective`: `Name` + пустое `IoTAccountSettings` (настроек в директиве нет) | после действия колонки, третьим пушем (запись 2026-11-12) |

Порядок четырёх пушей после действия колонки — `set_div_variables` → `sync_iot_user_info` →
`set_iot_account_settings` → `set_iot_user_info` (запись 2026-11-12, 14:35:34–14:35:36);
`set_iot_user_info` при действии уходит этим же путём, а не слушателем реестра, — иначе
он опережал бы остальные.

`Type` и `CapabilityType` — enum схемы станции; короткие имена возможностей
(`on_off`/`level`/`color`/`battery`) отображаются на `OnOffCapabilityType`/`LevelCapabilityType`/
`ColorCapabilityType`/`BatteryCapabilityType`, неизвестное значение становится
`UnknownDeviceType`/`UnknownCapabilityType` — сборка списка не падает.
`Type` — имена enum, которые несёт настоящий кадр (2026-11-14, журнал «Прозрачного»):
`climate→ThermostatDeviceType`, `media_player→MediaDeviceDeviceType`, `fan→FanDeviceTypeV2`,
`lock→DoorLockDeviceType`, `cover→CurtainDeviceType`; датчик уточняется по `device_class`
сущности (`temperature`/`humidity`→`ClimateSensorDeviceType`, `motion`/`opening`/`moisture` →
одноимённые типы, прочие → `SensorDeviceType`). `ExternalId` — сущность источника без
префикса origin, `Endpoint.Status` — `Online`/`Offline` с `UpdatedAt` (GetCurrentTime),
как в настоящем кадре.
`AnyCapabilities` несёт держатель умения с `RawCapability` и `CapabilityType` вместе —
оба поля заполняются, как в образце 2026-11-10 (журнал 2026-11-13: настоящий кадр —
`OnOffCapabilityType=1`/`LevelCapabilityType=3`/`BatteryCapabilityType=15` рядом с
байтами умения). Колонка строит из `CapabilityType` идентификатор строки умения в
своей БД (первичный ключ — идентификатор и устройство), а карточку — по
`Instance`/`State` внутри байтов: `on_off` → `TOnOffCapability{Meta{SupportedDirectives:
OnOffDirectiveType, Retrievable}, State{On}}`, `level` → `TLevelCapability{…,
Parameters{Instance, Unit, Range}, State{Level}}`, `battery` → `TBatteryCapability{…,
State{Percentage}}`. Держатель без `CapabilityType` даёт всем умениям устройства один
и тот же `UnknownCapabilityType` — конфликт на первичном ключе, и колонка отвергает
весь набор умений целиком (SQLiteConstraintException в её журнале). Инстанс уровня
расширение несёт в умении коротким именем (`brightness`/`open`/`temperature`/`humidity`/… →
`BrightnessInstance`/`CoverInstance`/…, Unit и диапазон — из той же таблицы); громкость
плеера в enum станции не входит — умение идёт без инстанса. Неизвестный тип умения
идёт `CapabilityType`'ом (без байтов).
Переменная div строится по образцу `device_<id>_<capability>_state` (в образце uuid с «_»
вместо «-»; «:» префикса origin и «.» сущности тоже становятся «_») — то же имя у плиток шторки.

## Шторка

Блок умного дома в верхней шторке рисует сервер, а не лаунчер по своей базе. Колонка шлёт
`TextInput` с событием `@@mm_typed_callback` (`Payload.analytics.purpose:
collect_upper_shutter`); ответ — `InputStartAck` и `AliceResponse` с одной директивой
`client_action` `set_upper_shutter`, `IsLedSilent`, `Payload.div2_card`: `string_body` —
div2 JSON, `id {card_id: upper_shutter_key_static_key, card_name: upper_shutter}`,
`card_name: CentaurUpperShutterData` (форма настоящего ответа в журнале).

Карточка (`server_emulator/upper_shutter.py`) собирается из обезличенного шаблона
`upper_shutter.json`, снятого с настоящей карточки: каркас (переменные фильтров
`iot_filter_<Sensors|Climate|Speakers|Vacuum|Light|TV|Scenarios>`, триггеры
`upper_shutter_all_filters_is_off`/`isVertical`), чип фильтра, плитки вкл/выкл в сети
(иконка с вариантом `-on`, переключатель — `set_variable` и фрейм
`iot_device_action_semantic_frame` с id реестра) и не в сети, «Показать все». Чипы — только
категорий, что есть среди плиток; плитки — по комнатам (по имени), без комнаты — в конце,
у них подписи комнаты нет. В шторку идут устройства с `on_off`: лампы (`Light`), телевизоры
(`TV`), вентилятор — без фильтра (виден, когда фильтры сняты); реле и медиаплееры, как в
образце, не показываются. Ссылки `dialog://` кодируются как в образце: компактный JSON,
`quote` без безопасных символов. Колбэк принимает тот же обработчик `iot_device_action`, что
и действия (регистрируется с первым обработчиком действий); устройств шторки нет — обычный
путь (тихая заглушка служебных колбэков).

## Действие с колонки

Кнопка карточки и голос приходят одинаково: `TextInput` с
`typed_semantic_frame.iot_device_action_semantic_frame` — `request_value.device_ids` и
`intent_parameters.capability_instance` («on»/«off»). Порядок ответа (запись 2026-11-09,
записи 120389–120425):

1. `InputStartAck`.
2. `AliceResponse`: пустой `VoiceResponse.OutputSpeech.Text`, директива
   `tts_play_placeholder` (`client_action`, `IsLedSilent`, канал Dialog), `Header.RequestId`
   запроса.
3. Каждое известное устройство — действие его источнику: callback получает
   `{instance, capability, value}`; ответ `{capability: значение}` — реестр обновляет
   `value` возможности, сохраняет и пушает этой колонке по порядку: `set_div_variables`
   (изменения) → `sync_iot_user_info` → `set_iot_account_settings` → `set_iot_user_info`
   (полный список). Неизвестное устройство/источник без обработчика — только
   `sync_iot_user_info`.

Обработчик `TextInput` с именем `iot_device_action` регистрируется в реестре AAPI, только
когда хоть одно расширение поставило `smart_home_on_device_action` (перед обработчиками
реплик: перехватывает фрейм до сценарной маршрутизации, чужой фрейм — `False`, обычный путь).
Пока обработчиков нет, обработчики `TextInput` подчиняются прежним условиям.

## SDK

Функции `server_emulator/sdk.py` (расширения) — детали в docs/contracts/extensions.md:
`smart_home_add_device` / `smart_home_update_device` / `smart_home_remove_device` (только
свои), `smart_home_devices()` (весь реестр), `smart_home_set_rooms`,
`smart_home_on_device_action(cb)`.

## UI

Вкладка «Устройства → Умный дом» — только просмотр: `GET /emulator/api/smart_home` →
`{devices, rooms}`; строки: устройство (название, id, «не в сети»), комната, источник.
Правок реестра из панели нет — их делают расширения.

## Инварианты

- Реестр — единственный источник iot-директив колонке; переменные div — производные от
  состояния реестра.
- Префикс id исключает пересечение устройств разных расширений.
- Любое изменение реестра уходит колонкам, объявившим `LocalIoT` ([capabilities.md](capabilities.md)), включая пустой список.
