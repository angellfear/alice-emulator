---
title: "Контракты действий над устройством"
status: stable
date: "2026-10-23"
module: "server_emulator/runtime.py"
---

# Контракты действий над устройством

Два канала доставки: **Push-директивы** (вне диалога, `TServerMessage.Event.Push`) и **директивы внутри AliceResponse** (в ответе на запрос). Разделение delivery (доставлено) / execution (исполнено) обязательно: клиентский `Ack` подтверждает только доставку (runtime.py), фактическое исполнение подтверждается callback/state (не реализовано — E01).

## 1. Push-действия эмулятора (actions.py CATALOG, отправка из «Эмулятор → WS → Отправка»)

| Действие | Параметры | Wire | Fixture/тест |
|---|---|---|---|
| sound_louder | — | Push.Directives[]: Name=действие, Type=client_action, IsLedSilent=true | test_quasar_backend (push+ack) |
| sound_quiter | — | 同上 (имя сохранено как есть — исторический контракт) | test_quasar_backend |
| sound_set_level | level 0..10 | Payload(Struct): new_level, new_percent_level=level*10 | test_quasar_backend |
| start_bluetooth / stop_bluetooth | — | 同上 | test_quasar_backend |
| push_update_config | — | Push.Directives[]: Name=действие, Type=client_action, без IsLedSilent; AnalyticsMetaInfo.RequestId (образец настоящего сервера) | test_push_actions |

Каркас Push-сообщения: Header.MessageId (новый), Header.Ack (ms), Push.PushIds[]+DeduplicationPushId (новые UUID), Timings.SendingTime. Отправка возможна только в активную WS-сессию устройства с назначенным владельцем (runtime.py); чужой SN/чужая сессия → ошибка. Повтор Ack-подтверждённого MessageId не создаёт дубль действия.

## 2. Директивы внутри AliceResponse (наблюдается в захвате)

| Директива | Назначение | Evidence |
|---|---|---|
| sound_set_level | установка громкости голосом («громкость N/два/на 50») — **тихий** ответ: payload `new_level` + `new_percent_level = level·100/MaxLevel` (шкала колонки, cap 23), без речи и карточек (`IsLedSilent=true`), `tts_play_placeholder` второй | volume-level (tests/fixtures/directives, журнал 2026-10-24 00:24:56/23:07:45, 10-29 22:14:24), tests/test_time_scenarios.py + tests/test_simple_commands.py; шаги «громче/тише» — ответного образца фраз нет, проводной Push из приложения (sound_louder/sound_quiter, журнал 10-23…29) |
| divui_show_view | показать DivUI-карточку на экране | fixture screen-divui (+ Payload.div2_card.string_body — JSON-карточка) |
| tts_play_placeholder | заглушка TTS-фразы | text-basic, screen-divui |
| set_timezone, set_location, set_upper_shutter | контекст/занавес камеры | captures (не реализованы) |
| audio_play, apply_player_playback_queue_directive, set_audio_player_sequence_state_directive | медиа-очередь Яндекса (контент и URL потока) | captures (не реализованы: своей очереди у эмулятора нет) |
| update_all_informers_directive, update_informer_directive, rotate_teasers_directive | инфоценры/ротация тизеров | captures (не реализованы) |
| divui_hide_view, draw_animation_directive (S3 animations/player_pause), player_pause, clear_queue | пауза штатного плеера («стоп/пауза», без речи) | media-pause (tests/fixtures/directives), tests/test_media_scenarios.py; payload_raw по схеме станции |
| iot_sync_endpoints_directive | IoT | captures (не реализован) |
| open_screen_timer_info_directive, open_screen_timer_list_directive, open_screen_alarm_list_directive, close_screen_timer_active_directive, close_screen_alarm_active_directive | экраны «Часов» (ClockAppCapability, payload_raw по схеме станции) | timer-set / timer-list / timer-cancel / alarm-set / alarm-stop, tests/fixtures/directives |
| timer_set_directive, timer_cancel_directive, timer_stop_directive, alarm_add_directive, alarm_stop_directive | таймеры/будильники (AlarmCapability, payload_raw; id 8-8-8-8 hex) | там же; сверка — tests/test_time_scenarios.py |
| divui_show_view (Div2Card YouTubeWebviewData) | **запуск ютуба** на станции с экраном: тихий ответ — tts_play_placeholder + webview-карта m.youtube.com (youtube_inject, voice_controls), InactivityTimeout Infinity, TerminateActions → divui_hide_view по статическому CardId `youtube_key_static_key`; свежий uuid — log_id карты и request_id webview (одно значение) | запись .85 «Прозрачно» 2026-11-10 (обмен 126042), форма — server_emulator/app_cards/youtube.json; сценарий device.app.open + app_directives.py |
| divui_show_view (Name=default_div_text_card) | **отказ открывать сайты и приложения**: «открой часы» — divui-карта `default_div_text_card` (StringBody с репликой и visibility_actions «Открой часы», OverlayBehaviour Hide, TerminateActions) + та же реплика в речи и simple_text-карточке | запись .85 «Прозрачно» 2026-11-12 14:28:04, форма — server_emulator/app_cards/clock_refusal.json; сценарий device.app.open + app_directives.not_here_directive |
| launch_app | **запуск приложения по пакету**: PayloadRaw `TLaunchAppDirective{PackageToLaunch, IntentAction MAIN, Analytics.AppLaunch{PackageName, VisibleName}}` (VisibleName — имя из InstalledAppsList колонки), речью — `<speaker audio='open_sound.opus'>`, карточек нет; на проводе launch_app до tts_play_placeholder | запись .85 «Прозрачно» 2026-11-12 14:28:31 («запусти телемост»); сценарий device.app.open + app_directives.launch_app_directive; поля — docs/contracts/directives.md |
| open_screen_alarm_list_directive (без payload) | **экран списка будильников**: «открой будильник» — директива с пустым PayloadRaw (в отличие от `{ForceAutoClose}` в set/cancel); если у колонки нет будильников (состояние из её сообщений) — реплика «У вас нет установленных будильников.», иначе ответ тихий | запись .85 «Прозрачно» 2026-11-12 14:28:11 (колонка без будильников); сценарий device.app.open |
| go_forward, go_backward, go_home | **листание экранов колонки**: «ещё» → go_forward, «назад» → go_backward, «главный экран» → go_home; client_action, IsLedSilent, Payload — пустой Struct, PayloadRaw пустой, AnalyticsType нет; tts_play_placeholder первой | запись .85 «Прозрачно» 2026-11-12 14:26:49 / 14:27:19 / 14:27:29 / 14:27:59; сценарии device.screen.navigation, device.home |
| accessibility_press_button_by_id_directive | **не реализовано**: второй способ открыть приложение у настоящего сервера — нажатие кнопки на экране колонки по id: тихий ответ (`VoiceResponse` пуст) с `accessibility_press_button_by_id_directive` + `tts_play_placeholder`, PayloadRaw `TAccessibilityPressButtonByIdDirective` (схема `…/accessibility_voice_control/capability.proto`) — только `ButtonId=2` («EDc=» → id 55; `Name`/`Reason` не заполнены). id берутся с экрана колонки, образцов, откуда сервер их знает, нет — реализовать нечем | запись .85 «Прозрачно» 2026-11-12 14:10:34 («открой любимые пазлы», обмен 167931); механизм рядом с `launch_app` — docs/contracts/scenarios.md, строка device.app.open |
| phone_calls_make_call | **не реализовано**: звонок голосом — `TMakeCallDirective` (`…/phone_calls/capability.proto`): `Number`, `CallId`, `AccountId`, хост `alice-ats.yandex.net`, данные вызываемой колонки + речь «Звоню "<имя>".». Нужен ATS/телефония, которой у эмулятора нет | запись .85 «Прозрачно» 2026-11-12 14:35:47 (TextInput `@@mm_typed_callback`, product_scenario «Speaker calls») |

Полный `TDirective`: Type, Name, AnalyticsType, IgnoreAnswer, Payload(Struct), PayloadRaw(bytes), MultiroomSessionId, RoomDeviceIds, IsLedSilent, EndpointId, IsParallel, TimeBeforeDropMs, PersId, OnFinish, Id, SourceInfo, DirectiveType, Channel (descriptor). Multiroom/Endpoint поля не подтверждаются нашими устройствами — не заполнять без evidence.

## 3. Push-директивы реального сервера (наблюдение)

| Директива | Type | Счётчик в захвате | Судьба |
|---|---|---|---|
| notify | — | 114 | не реализована (обычный Push-шаблон можно импортировать как handler) |
| sync_iot_scenarios_directive | client_action | 33 | не реализована (сценарии умного дома колонка хранит сама) |
| set_iot_user_info, set_iot_account_settings, sync_iot_user_info, set_div_variables | client_action | 92/4/92/10 | реализованы реестром умного дома — порядок и поля: docs/contracts/smart-home.md |
| @@mm_typed_callback, @@mm_semantic_frame | server_action | 15/3 | внутренние Мегамозг-каналы; изучить при импортере (v4 §9) |
| push_update_config | client_action | 23 | уже в каталоге §1 |

## 4. Карточки

В захвате все карточки — `simple_text` (Type + Text + CardId; CardId меняется между повторами одинакового ответа — генерируется сервером, класс New identifier по v3 §4). DivUI-контент идёт директивой divui_show_view, не карточкой. Эмулятор генерирует simple_text (voice.py); другие типы — E03.

## 5. Нереализованное и границы

- Execution-подтверждение (callback/state после действия) — нет; UI показывает `execution: unconfirmed` (test_quasar_backend это закрепляет).
- Capability/firmware-гейт действий — нет (E01): Push уходит в любую активную сессию устройства.
- Запуск Activity/deeplink — исследование TSendAndroidAppIntentDirective в v4 §6/§13; в трафике не наблюдался.
- Цель действия из model tool-call не принимается автоматически (runtime.py — tool calls только предложения).
