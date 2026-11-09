---
title: "Справочник директив: SpeechKitName → proto-сообщение → поля"
status: draft
date: "2026-11-07"
module: "server_emulator/media_directives.py"
---

# Справочник директив: SpeechKitName → proto-сообщение → поля

Нормативная таблица payload'ов директив, которыми эмулятор отвечает колонке
(`server_emulator/media_directives.py`, `server_emulator/clock_directives.py`,
`server_emulator/voice.py`). Источник имён сообщений, полей и номеров —
репозиторий [v3rt3p/quasar-protos](https://github.com/v3rt3p/quasar-protos)
(MIT); в колонке «Проверено» — путь к файлу protos, имя типа из схемы станции
(`server_emulator/schemas/alica-*.pb`) или образец журнала. Непроверенные
позиции помечены явно.

**Правило:** новый payload сначала добавляется в этот справочник; сценарии и
обработчики ссылаются на него. Payload'ы, для которых в protos нет
proto-сообщения, документируются как Struct по образцу журнала.

## Конверт TDirective

Тип: `NAlice.NAliceApi.TDirective` —
`alice/protos/api/alicekit/directives/directive.proto` (этот же конверт в
`Push.Directives` и `TExecuteDirective.Directive`). Брат-определение
`NAlice.NSpeechKit.TDirective` (`alice/megamind/protos/speechkit/directives.proto`)
повторяет поля и добавляет `UniproxyDirectiveMeta=10`.

| № | Поле | Тип | Комментарий |
|---|---|---|---|
| 1 | `Type` | string | всегда `client_action` в ответах колонке |
| 2 | `Name` | string | SpeechKitName (см. таблицу ниже) |
| 3 | `AnalyticsType` | string | тип аналитики (в фиксурах сверяется отдельно) |
| 4 | `IgnoreAnswer` | bool | |
| 5 | `Payload` | google.protobuf.Struct | JSON-payload (текстовая форма) |
| 6 | `MultiroomSessionId` | string | |
| 7 | `RoomDeviceIds` | repeated string | мультирум; нашими устройствами не подтверждён — не заполнять |
| 8 | `IsLedSilent` | bool | тише индикации (у «тихих» ответов `true`) |
| 9 | `EndpointId` | google.protobuf.StringValue | |
| 11 | `IsParallel` | bool | |
| 12 | `TimeBeforeDropMs` | uint32 | |
| 13 | `PersId` | string | |
| 14 | `OnFinish` | TOnFinishEvent | `TypedCallbackRequest=1` (Struct) |
| 15 | `PayloadRaw` | bytes | бинарная форма payload (protobuf-сообщение) |
| 16 | `Id` | string (optional) | |
| 17 | `SourceInfo` | TSourceInfo | |
| 18 | `AttachToMultiroom` | bool | |
| 20 | `DirectiveType` | TCapability.EDirectiveType | |
| 21 | `Channel` | TChannel | `Type=1` — `Dialog` |

**Оговорка Payload/PayloadRaw:** `Payload` (Struct) — для простых
JSON-подобных payload'ов (его шлют наши сценарии и `media_directives.py`);
`PayloadRaw` (bytes) — когда payload — типизированное protobuf-сообщение схемы
станции, которое нельзя потерять в JSON (нестрогие поля, byte-поля). Одно из
двух — `PayloadType` oneof; эмулятор заполняет `PayloadRaw` только там, где
образец журнала содержит бинарный payload (`payload_raw` в фиксурах
`tests/fixtures/directives/`), иначе — `Payload`.

## Таблица «Директива → Payload → поля»

Для каждой строки: сообщение protos с номерами полей; если у эмулятора своя
форма — она указана в «Payload эмулятора».

| Директива (Name) | Payload эмулятора | Сообщение protos и поля | Проверено |
|---|---|---|---|
| `tts_play_placeholder` | Struct `{"channel": "Dialog"}` | proto-сообщения в protos нет — Struct по образцам | образцы журнала (фикстуры `tests/fixtures/directives/*`, все ответы с placeholder) |
| `push_update_config` | Struct по каталогу actions | proto-сообщения нет — Struct | `server_emulator/actions.py` CATALOG; образцы журнала |
| `audio_play` | Struct: `stream{url, type, format, duration_ms?, decode_key?}`, `metadata{title, subtitle, art_image_url?, hide_progress_bar}`, `screen_type: Music`, `background_mode: Ducking`, `set_pause` | proto-сообщения нет — Struct по образцам; поток/ключи — от поставщика | `docs/contracts/player.md`, захват 2026-11-02-music |
| `clear_queue` | без payload | — | образцы журнала (фикстура media-pause.json) |
| `player_pause` | Struct `{"smooth": false}` | — | образцы журнала (media-pause.json) |
| `@@mm_semantic_frame` | Struct (`analytics.purpose`, typed_semantic_frame) | — | образцы журнала (Push с purpose: shoot_reminder / agent_answer — `async_reply.py`) |
| `@@mm_stack_engine_get_next` | без payload (эмулятор на него не отвечает — только от поставщика) | — | образцы журнала (`docs/contracts/player.md`) |
| `@@mm_typed_callback` | Struct (`purpose`, payload) | — | образцы журнала (кнопки экрана плеера) |
| `sound_set_level` | Struct `{new_level, new_percent_level}` | `TSoundSetLevelDirective`: Name=1, NewLevel=2, MultiroomSessionId=3, RoomId=4 (deprecated), LocationInfo=5, NewPercentLevel=6, IsWhisper=7 — `alice/megamind/protos/scenarios/directives/sound_directives/sound_directives.proto` | protos (клон 2026-11-07); фикс. volume-level.json |
| `sound_louder` | Push-действие без payload (из приложения — wire) | `TSoundLouderDirective`: Name=1, MultiroomSessionId=2, RoomId=3, LocationInfo=4 (там же) | protos; образцы журнала |
| `sound_quiter` (опечатка Яндекса) | Push-действие без payload | `TSoundQuiterDirective`: Name=1, MultiroomSessionId=2, RoomId=3, LocationInfo=4 (там же) | protos; образцы журнала |
| `sound_mute` | не отправляется (нет образца) | `TSoundMuteDirective`: Name=1 (там же) | protos |
| `sound_unmute` | не отправляется (нет образца) | `TSoundUnmuteDirective`: Name=1 (там же) | protos |
| `start_bluetooth` / `stop_bluetooth` | Push-действия из каталога | proto-сообщений в protos нет | `server_emulator/actions.py`; образцы журнала |
| `draw_animation_directive` | PayloadRaw по схеме станции | `TDrawAnimationDirective`: Animations=1 [{S3Directory{Bucket=1, Path=2} | BinaryAnimation{Compression=1, Base64EncodedValue=2} | TypedAnimation{Type=1}}], AnimationStopPolicy=2, SpeakingAnimationPolicy=3, AnimationOptions=4, FadeOutAnimations=5 — `alice/protos/endpoint/capabilities/animation/capability.proto` | protos; `NAlice.TAnimationCapability.TDrawAnimationDirective` (схема станции, payload_raw в media-pause.json) |
| `divui_show_view` | PayloadRaw | `TDivUIShowViewDirective`: Name=1, Div2Card=2, Layer=3, InactivityTimeout=4, ActionSpaceId=5, StashInteraction=6, RenderRequestParams=7, OverlayBehaviour=8, GestureInteraction=9, TerminateActions=10, StackInteraction=11 — `alice/protos/endpoint/capabilities/layered_div_ui/capability.proto` | protos; `async_reply.screen_card` |
| `divui_hide_view` | PayloadRaw `{Name}` | `TDivUIHideViewDirective`: Name=1, Layer=2, Identifier=3 (там же) | protos; media-pause.json |
| `draw_led_screen` | не отправляется (нет образца у наших устройств) | в protos SpeechKitName нет — по образцам журнала (x1, колонка с LED-экраном) | образец журнала (device-actions.md); **непроверено в protos** |
| `local_audio_file_play` | не отправляется | `TLocalAudioFilePlayDirective`: Channel=2 (EDirectiveChannel), Looped=3, Name=4, PlayTimes=5, FileName=6, SoundToPlay=7 (enum EStoredSound: UnknownSound=0, CallRinging=1, …, BluetoothPairingMode=5, BootSound=8, …) — `alice/protos/endpoint/capabilities/audio_file_player/capability.proto` | protos (наблюдение: колонка 84 играет свою фразу таймаута — `local_audio_file_play`) |
| `audio_player_rewind` | не отправляется | в protos SpeechKitName нет; близкое — `rewind_amount_ms=3` в `yandex_io/protos/remoting.proto` (remoting-канал, не SpeechKit-директива) | **непроверено**: соответствие `amount_ms`/`type Forward|Backward` protos-сообщению не найдено |
| `set_timezone` | PayloadRaw | `TGeoSyncCapability.TSetTimezoneDirective`: Timezone=1 (TTimezone{TimezoneName=1, OffsetSeconds=2}), Name=2, NextTimezone=4 — `alice/protos/endpoint/capabilities/geo_sync/capability.proto` | protos |
| `set_location` | PayloadRaw | `TGeoSyncCapability.TSetLocationDirective`: Location=1 (TLatLon{Latitude=1, Longitude=2, Accuracy=3}), Name=2, LaasRealLocation=3 (там же) | protos |
| `alarm_add_directive` | PayloadRaw | `TAlarmAddDirective`: Name=1, Alarm=2 — `TAlarm`: AlarmId=1, LocalTime=2 (TTime{Hours=1, Minutes=2, Seconds=3}), CurrentlyPlaying=3, ActivationDays=5 [{Weekday=1 | Date=2{Year=1, Month=2, Day=3}}], Timezone=7, SnoozeTime=12, Disabled=9, Title=10, EditTimestamp=11 — `alice/protos/endpoint/capabilities/alarm/capability.proto`, `alice/protos/data/scenario/alarm/alarm.proto` | protos; timer-set/alarm-set фикстуры |
| `alarm_cancel_directive` | PayloadRaw `{Name: "cancel_alarm_capability_directive", AlarmId}` | `TAlarmCancelDirective`: Name=1, AlarmId=2 (capability alarm) | protos; сценарий `timers.alarm.cancel` (payload по protos, решение владельца; экран списка — по образцам установки/отмены таймера) |
| `alarm_stop_directive` | PayloadRaw `{StopMedia}` | `TAlarmStopDirective`: Name=1, StopMedia=2 (capability alarm) | protos; alarm-stop.json |
| `timer_set_directive` | PayloadRaw | `TTimerSetDirective`: Name=1, Timer=2 — `TTimerDescription`: TimerId=1, StartTimestamp=2, Duration=3, Remaining=4, CurrentlyPlaying=5, Paused=6, TimerType=7, Title=10, Disabled=11 — capability alarm + `alice/protos/data/scenario/timer/timer.proto` | protos; timer-set.json |
| `timer_cancel_directive` | PayloadRaw | `TTimerCancelDirective`: Name=1, TimerId=2 (capability alarm) | protos; timer-cancel.json |
| `timer_stop_directive` | PayloadRaw | `TTimerStopDirective`: Name=1, TimerId=2 (capability alarm) | protos; alarm-stop.json (журнал) |
| `timer_pause_directive` | PayloadRaw `{Name: "pause_timer_capability_directive", TimerId}` | `TTimerPauseDirective`: Name=1, TimerId=2 (capability alarm) | protos; сценарий `timers.timer.pause` (payload по protos, решение владельца) |
| `timer_resume_directive` | PayloadRaw `{Name: "resume_timer_capability_directive", TimerId}` | `TTimerResumeDirective`: Name=1, TimerId=2 (capability alarm) | protos; сценарий `timers.timer.resume` (payload по protos, решение владельца) |
| `local_audio_file_play` | PayloadRaw `{PlayTimes: 1, SoundToPlay}` (enum EStoredSound: AliceIoTSoundsTurnOn=61, AliceIoTSoundsTurnOff=60) | `TLocalAudioFilePlayDirective`: Channel=2 (EDirectiveChannel), Looped=3, Name=4, PlayTimes=5, FileName=6, SoundToPlay=7 (там же) | protos (схема станции); Push-действия панели `sound_turn_on`/`sound_turn_off` (`actions.py`) |
| `audio_player_rewind` | Struct `{"amount_ms": <мс>, "type": "Forward"\|"Backward"}` | protos-сообщения в SpeechKitName нет — Struct по решению владельца 2026-11-07; близкое `rewind_amount_ms=3` в remoting.proto — другой канал | сценарий `device.player.rewind`; **payload не подтверждён журналом** |
| `show_audio_player_screen_directive` | PayloadRaw | `TShowAudioPlayerScreenDirective`: TrackId=1 — `alice/protos/endpoint/capabilities/audio_player_screen/capability.proto` | protos; захват 2026-11-02-music |
| `apply_player_playback_queue_directive` | PayloadRaw | `TApplyMusicPlatformQueueDirective`: MusicPlatformQueue=1 — `TMusicPlatformQueue`: Entities=1 (TPlaybackEntityWithProvider), CurrentEntityIndex=2, MusicPlatformMetaStorage=3 — `alice/protos/endpoint/capabilities/music_platform_queue/capability.proto`, `alice/protos/data/scenario/music/music_platform_queue.proto` | protos; захват 2026-11-02-music |
| `set_audio_player_sequence_state_directive` | PayloadRaw | `TSetSequenceStateDirective`: SequenceState=1 — `TAudioPlayerSequenceState`: Queue=1, History=2, CurrentItem=3, MetaStorage=4, CallbacksStorage=5 — `alice/protos/endpoint/capabilities/audio_player/capability.proto`, `.../audio_player/sequence_state.proto` | protos; захват 2026-11-02-music |
| `vocalize_directive` | не отправляется (нет образца) | `TVocalizeDirective`: Text=1, SynthesisMode=2 (Append=1, Interrupt=2), Voice=3, TtsSource=4 (Server=0, Local=1, Hybrid=2) — `alice/protos/endpoint/capabilities/alice/capability.proto` | protos |
| `complete_streaming_response_directive` | не отправляется (нет образца) | `TCompleteStreamingResponseDirective`: RequestId=1 (capability alice) | protos |
| `stop_conversation_directive` | не отправляется (нет образца) | `TStopConversationDirective`: ClientOptions=1 {DisableJingle=1, DisableSendCancel=2} (capability alice) | protos |
| `set_uniproxy_settings_directive` | не отправляется | `TSetUniProxySettingsDirective`: YexpCookie=2, Cookies=3 [{Name=1, Value=2}], UniProxyUrl=4, VinsUrl=5 (capability alice) | protos |

## Служебные события uniproxy (кратко)

Каталог: `alice/protos/api/alicekit/events/`.

- **`TSyncPush`** (`events/sync_push/sync_push.proto`): PushId=1,
  DeduplicationPushId=2, SyncRequest=3, AnalyticsMetaInfo=4,
  SyncPushClientType=5 (sync=0, sync_iot=1).
- **`TSyncRequest`** (`events/sync_push/sync_request/sync_request.proto`), oneof
  Request: `GetDeviceState=1` | `ExecuteDirective=2` | `ExecuteDirectiveChain=3`;
  вне oneof: GetClientInfoOptions=4.
- **`TExecuteDirective`** (`.../execute_directive/execute_directive.proto`):
  Directive=1 (конверт TDirective), ResponsePolicy=2 — oneof Policy:
  `OnStartedPolicy=1` | `OnFinishedPolicy=2`.
- **`TSyncPushResponse`** (`events/sync_push_response/sync_push_response.proto`):
  полей нет (пустое подтверждение).
- **`TPush`** (`events/push/push.proto`): PushIds=1, DeduplicationPushId=2,
  Directives=3 (repeated TDirective), AnalyticsMetaInfo=4 — доставка директив
  вне диалога (напоминания, отложенный ответ агента).

## Не реализовано

- Отправка `PayloadRaw` остальных описанных здесь сообщений в сценариях — шаги
  добавляются по мере появления образцов журнала (директивы без образца и без
  явного решения владельца не отправляются).
- Payload'ы, внедрённые без образца журнала по решению владельца 2026-11-07
  (`alarm_cancel_directive`, `timer_pause_directive`,
  `timer_resume_directive`, `local_audio_file_play`, `audio_player_rewind`), —
  сверены с protos; фактическое поведение колонки подтверждает владелец на станции.
