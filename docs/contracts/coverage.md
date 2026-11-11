---
title: "Инвентаризация контрактов эмулятора"
status: stable
date: "2026-10-23"
module: ""
---

# Инвентаризация контрактов эмулятора — baseline A01

Дата: 2026-10-23. Версия Python 3.13.12, aiohttp 3.13.3, protobuf 7.35.1, pytest 9.0.2.
Baseline тестов: `python -m pytest -q` → **61 passed**.
Стендalone-проверка: `create_app(runtime=Runtime(temp_state))` на свободном порту — status/UI/mode/quasar отвечают, файлы пишутся только во временный state.

Колонки: **observed** (видели в реальном трафике/файлах), **source** (где реализовано), **tested** (автотест), **device-tested** (физическое устройство), **unsupported** (осознанно не поддержано/не реализовано).
Сокращения тестов — файлы в `tests/`.

## 1. HTTP Quasar-бэкенд (`server_emulator/quasar.py`)

| Endpoint | observed | source | tested | device-tested | unsupported |
|---|---|---|---|---|---|
| GET /check_updates | да | quasar.py | test_quasar_backend | нет записи | — |
| GET /get_sync_info | да | quasar.py (требует state.quasar.config) | test_quasar_backend | нет записи | — |
| GET /glagol/device_list | да | quasar.py | test_quasar_backend | нет записи | — |
| GET /glagol/token | да | quasar.py (HS256 JWT, quasar-secret.json) | test_quasar_backend | нет записи | — |
| POST /glagol/check_token | да | quasar.py | test_quasar_backend | нет записи | — |
| POST /glagol/v2.0/check_token | да | quasar.py | test_quasar_backend | нет записи | — |
| POST /update_device_state | да | quasar.py | test_quasar_backend | нет записи | — |
| POST /set_device_capability | да | quasar.py | test_quasar_backend | нет записи | — |

Происхождение контрактов: v3rt3p/quasar-server (см. NOTICE в quasar.py) + собственный захват.

## 2. OTA-обновления (`server_emulator/updates.py`)

| Endpoint | observed | source | tested | device-tested | unsupported |
|---|---|---|---|---|---|
| POST quasar /tv/api/v4/updates (list) | да | updates.py | test_updates | нет записи | — |
| GET /check_updates (check) | да | updates.py | test_updates | нет записи | — |
| GET /allow_update | да | updates.py | test_updates | нет записи | — |
| GET /confirm_update | да | updates.py | test_updates | нет записи | — |
| Политики allow/deny/custom + per-SN | да | updates.py | test_updates (deny/custom/allow) | нет записи | — |
| Сбор URL прошивок (attempts/urls/sightings) | да | updates.py | test_updates | нет записи | — |

Примечание: режим `upstream_observation off` (полный запрет обращения к Яндексу, C02) пока отсутствует — при любой политике, кроме явного deny-ответа, происходит реальное обращение к upstream.

## 3. Каталог приложений (`server_emulator/catalog.py`)

| Контракт | observed | source | tested | device-tested | unsupported |
|---|---|---|---|---|---|
| POST rpc.alice /gproxy/get_app_catalog (патч каталога под эмулятор) | да | runtime.py | test_catalog, test_quasar_backend | нет записи | — |
| GET /__catalog/icon/{hash} | да | catalog.icon | test_catalog | нет записи | — |
| GET /__catalog/apk/{hash} | да | catalog.download | test_catalog | нет записи | — |
| Admin: GET/PUT /api/catalog, POST /api/catalog/apk, PUT/DELETE /api/catalog/apps | да | app.py | test_catalog | нет записи | — |

## 4. Эндпоинты и fallback

| Контракт | observed | source | tested | device-tested | unsupported |
|---|---|---|---|---|---|
| Каталог эндпоинтов `state/endpoints`: find(host, method, path), включённый эндпоинт отвечает | да | endpoints.py | test_endpoints | нет записи | — |
| Серверный эндпоинт-скрипт (`<id>.py` рядом с JSON) | да | endpoint_scripts.py | test_endpoint_scripts | нет записи | — |
| Fallback 404 `emulator_handler_not_found` | да | runtime.py | test_emulator_runtime | нет записи | — |

Хранилище обработчиков из захвата (раздел «Обработчики») удалено — ответы подменяются эндпоинтами (`docs/contracts/endpoints.md`).

## 5. WS AAPI (`server_emulator/runtime.py`, `voice.py`, `legacy_replay.py`)

| Событие/направление | observed | source | tested | device-tested | unsupported |
|---|---|---|---|---|---|
| Фрейминг `AAPI` + protobuf, аудиоканалы 4-байтовым BE-префиксом | да | runtime.py, voice.py | test_capture_analysis, test_voice_pipeline | нет записи | — |
| VoiceInput → InputStartAck → аудио → Close → AsrResult | да | voice.py | test_voice_pipeline (fake STT) | нет записи | реальный ASR — внешний (D01) |
| AliceResponse (simple_text-карточка + OutputSpeech) | да | voice.py | test_voice_pipeline | нет записи | другие типы карточек — нет (E03) |
| TtsSpeak + Ogg/Opus постранично + Close канала | да | voice.py | test_voice_pipeline (fake TTS) | нет записи | Silero на билдере — внешний (D02) |
| TextInput → модель (OpenAI-совместимый chat) | да | voice.py, runtime.py | test_voice_pipeline, test_emulator_runtime | нет записи | — |
| CancelRequest → отмена задач/каналов | да | voice.py | test_voice_pipeline (cancel) | нет записи | — |
| SynchronizeStateRequest: смена DeviceId в соединении → 1008 | да | runtime.py | test_emulator_identity | нет записи | — |
| Ack → delivery=acknowledged для Push | да | runtime.py | test_quasar_backend | нет записи | — |
| LogSpotter → LogAck | да | runtime.py | fixture logspotter-ack + replay-тест (tests/test_emulator_fixtures.py); отдельного unit-теста нет | нет записи | — |
| Fallback: SynchronizeStateResponse, RequestStatAck | да | legacy_replay.py | test_emulator_and_pcm | нет записи | — |
| Push-действия: sound_louder, sound_quiter, sound_set_level, start/stop_bluetooth, push_update_config | да | actions.py CATALOG | test_quasar_backend (push+ack) | нет записи | — |

## 6. Голосовой конвейер (внутреннее)

| Контракт | observed | source | tested | device-tested | unsupported |
|---|---|---|---|---|---|
| PCM s16le → WAV (проверка rate/channels/выравнивания) | да | voice.py | test_emulator_and_pcm | нет записи | — |
| Opus/Ogg вход (input.ogg) | да | voice.py | test_voice_pipeline | нет записи | — |
| Ogg/Opus выход: страницы, OpusHead, EOS | да | voice.py ogg_pages | test_voice_pipeline, test_emulator_and_pcm | нет записи | CRC/continuation — валидатором библиотеки (D02) |
| Провайдеры model/stt/tts по HTTP (OpenAI-совместимые) | да | runtime.py | test_emulator_runtime, test_voice_pipeline | нет записи | — |
| MCP JSON HTTP (initialize/tools) | да | runtime.py | test_emulator_runtime | нет записи | SSE transport не поддержан (явная ошибка) |

## 7. Идентичности и владельцы (`server_emulator/identity.py`)

| Контракт | observed | source | tested | device-tested | unsupported |
|---|---|---|---|---|---|
| DeviceId — алиасы, SN — отдельный ключ; unassigned pending | да | identity.py | test_emulator_identity | нет записи | — |
| Профили user/device поверх defaults; unclaimed без AI-профиля | да | identity.py | test_emulator_identity, test_mode_connections | нет записи | — |
| Смена владельца требует reset_state | да | identity.py | test_emulator_identity | нет записи | — |
| HTTP/WS scope не пересекает пользователей | да | runtime.py | test_emulator_identity | нет записи | — |

Хранение — БД состояния (StateStore, SQLite); группы стереопар и слой аккаунта Quasar — meta-таблица.

## 8. Административный API эмулятора (`mount_ui`, префикс /emulator)

actions: settings, updates, identities, actions, chat, stt, tts, mcp-discover, tool-call, status (+static UI).
Тесты: test_emulator_runtime, test_quasar_backend. Защита: X-Emulator + same-origin на мутациях; вход в панель по паролю с сессиями (`auth.json`; без файла — открытый режим).
Разделение admin/user principals и pairing — отсутствует (B04).

## 9. Offline-replay каркас (`server_emulator/legacy_replay.py`)

`/_emulator/health`, `/_emulator/routes`, catch-all по (host,method,path) с body-hash матчингом, `/uni.ws` WS-каркас. Тесты: test_emulator_and_pcm.

## 10. Схемы

| Схема | observed | source | tested | device-tested | unsupported |
|---|---|---|---|---|---|
| schemas/alice-api.pb (TClientMessage/TServerMessage, восстановленный descriptor) | да | tools/build_alice_schema.py | test_capture_analysis (recovered schemas) | нет записи | отдельные вложенные Any/unknown — R02 |
| schemas/alica-<хеш>.pb(.json) | да | из libquasar_daemons.so или zip с .proto / .pb при первом запуске (protocol_import.py) | test_catalog (nested) | нет записи | — |

## 11. Известные неподдержанные/отложенные области (не баги baseline)

- Реальный ASR на билдере и Silero TTS: внешние сервисы, настройка — D01/D02; в тестах только fakes.
- Импорт Мегамозга не реализован.
- Пользовательский кабинет `/cabinet/api/v1`, SSE, admin/user auth: не реализован (B04, F01).
- `upstream_observation off`: не реализован (C02).
- Device-tested: по репозиторию нет воспроизводимых записей физической проверки; все status-ы выше — CODE_TESTED. Первое DEVICE_VERIFIED фиксировать с SN/firmware/результатом.


## Дополнение 2026-10-24 (342 passed)

Обновлённые/новые зоны контрактов: сценарный движок + каталог + реестр — docs/contracts/scenarios.md (новый); glagol/token caller/target — http.md; карточка/NO_SPEECH — aapi.md. Ключевые файлы→тесты: state_store→test_state_store; device resolver→test_device_resolver; speech trace→test_speech_trace; executor/router/probe→test_executor,test_probe; dialog transport→test_dialog_transport; timers/alarms/light/volume/app/weather→test_timers,test_alarms,test_light,test_simple_commands,test_weather; режимы 2-модели/миграция→test_mode_enum; glagol token→test_glagol_token; трей-менеджер→test_windows_launcher; каталог/реестр/zip/rollback/etag→test_scenario_catalog. Device-tested: по-прежнему нет зафиксированных физический проверок; статусы CODE_TESTED.
