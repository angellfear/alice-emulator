---
title: "Контракты действий над устройством"
status: stable
date: "2026-10-23"
module: "server_emulator/runtime.py"
---

# Контракты действий над устройством

Два канала доставки: **Push-директивы** (вне диалога, `TServerMessage.Event.Push`) и **директивы внутри AliceResponse** (в ответе на запрос). Разделение delivery (доставлено) / execution (исполнено) обязательно: клиентский `Ack` подтверждает только доставку (runtime.py:375–379), фактическое исполнение подтверждается callback/state (не реализовано — E01).

## 1. Push-действия эмулятора (actions.py CATALOG, отправка из «Эмулятор → WS → Отправка»)

| Действие | Параметры | Wire | Fixture/тест |
|---|---|---|---|
| sound_louder | — | Push.Directives[]: Name=действие, Type=client_action, IsLedSilent=true | test_quasar_backend (push+ack) |
| sound_quiter | — | 同上 (имя сохранено как есть — исторический контракт) | test_quasar_backend |
| sound_set_level | level 0..10 | Payload(Struct): new_level, new_percent_level=level*10 | test_quasar_backend |
| start_bluetooth / stop_bluetooth | — | 同上 | test_quasar_backend |
| push_update_config | — | 同上 | test_quasar_backend |

Каркас Push-сообщения: Header.MessageId (новый), Header.Ack (ms), Push.PushIds[]+DeduplicationPushId (новые UUID), Timings.SendingTime. Отправка возможна только в активную WS-сессию устройства с назначенным владельцем (runtime.py:462); чужой SN/чужая сессия → ошибка. Повтор Ack-подтверждённого MessageId не создаёт дубль действия.

## 2. Директивы внутри AliceResponse (наблюдается в захвате)

| Директива | Назначение | Evidence |
|---|---|---|
| sound_set_level | установка громкости по команде («громкость N») | fixture volume-set: AliceResponse.Response.Directives[] перед TtsSpeak ch=102 |
| divui_show_view | показать DivUI-карточку на экране | fixture screen-divui (+ Payload.div2_card.string_body — JSON-карточка) |
| tts_play_placeholder | заглушка TTS-фразы | text-basic, screen-divui |
| set_timezone, set_location, set_upper_shutter | контекст/занавес камеры | captures (не реализованы) |
| audio_play, apply_player_playback_queue_directive, set_audio_player_sequence_state_directive | медиа-очередь | captures (не реализованы) |
| update_all_informers_directive, update_informer_directive, draw_animation_directive, rotate_teasers_directive | инфоценры/анимация экрана | captures (не реализованы) |
| iot_sync_endpoints_directive | IoT | captures (не реализован) |

Полный `TDirective`: Type, Name, AnalyticsType, IgnoreAnswer, Payload(Struct), PayloadRaw(bytes), MultiroomSessionId, RoomDeviceIds, IsLedSilent, EndpointId, IsParallel, TimeBeforeDropMs, PersId, OnFinish, Id, SourceInfo, DirectiveType, Channel (descriptor). Multiroom/Endpoint поля не подтверждаются нашими устройствами — не заполнять без evidence.

## 3. Push-директивы реального сервера (наблюдение; эмулятором не поддержаны)

| Директива | Type | Счётчик в захвате | Судьба |
|---|---|---|---|
| notify | — | 114 | не реализована (обычный Push-шаблон можно импортировать как handler) |
| set_iot_user_info, set_iot_account_settings, sync_iot_user_info, sync_iot_scenarios_directive | client_action | 92/92/4/33 | вне скоупа (IoT-утюги/лампы) — не обещать |
| set_div_variables | client_action | 10 | кандидат на E03 после divui_show_view |
| @@mm_typed_callback, @@mm_semantic_frame | server_action | 15/3 | внутренние Мегамозг-каналы; изучить при импортере (v4 §9) |
| push_update_config | client_action | 23 | уже в каталоге §1 |

## 4. Карточки

В захвате все карточки — `simple_text` (Type + Text + CardId; CardId меняется между повторами одинакового ответа — генерируется сервером, класс New identifier по v3 §4). DivUI-контент идёт директивой divui_show_view, не карточкой. Эмулятор генерирует simple_text (voice.py:197); другие типы — E03.

## 5. Нереализованное и границы

- Execution-подтверждение (callback/state после действия) — нет; UI показывает `execution: unconfirmed` (test_quasar_backend это закрепляет).
- Capability/firmware-гейт действий — нет (E01): Push уходит в любую активную сессию устройства.
- Запуск Activity/deeplink — исследование TSendAndroidAppIntentDirective в v4 §6/§13; в трафике не наблюдался.
- Цель действия из model tool-call не принимается автоматически (runtime.py:225 — tool calls только предложения).
