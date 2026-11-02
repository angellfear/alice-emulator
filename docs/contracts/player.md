# Плеер колонки: директивы паузы и состояние

Музыка колонки играет штатным плеером (сценарий `HollywoodMusic` в состоянии колонки).
С 087 своей медиатеки, очереди и избранного у эмулятора нет: музыкальные запросы
(«включи …») в гибридном маршруте, где WS целиком эмулируется, остаются без обработчика —
их отвечает настоящий сервер Яндекса (в proxy-режиме — тоже он). Эмулятор управляет
плеером только там, где есть образец ответа настоящего сервера.

Разбор по образцам журнала-захвата (2026-10-21…2026-10-27); обезличенный образец —
`tests/fixtures/directives/media-pause.json`.

## Пауза/стоп («стоп», «пауза», «подожди», «замолчи», «ладно, стоп»)

Доминирующая форма, x20 из 23 медио-остановов журнала (2026-10-21 20:35:28,
2026-10-22 12:00:59, 2026-10-26 16:52:41 и др.): тихий ответ без речи и карточек,
`tts_play_placeholder` первой директивой (сервер не дополнял его — дубликат не добавляется):

| # | `Name` | тип payload | `AnalyticsType` |
|---|---|---|---|
| 1 | `tts_play_placeholder` | Struct `{"channel": "Dialog"}` | `tts_play_placeholder` |
| 2 | `divui_hide_view` | `NAlice.TLayeredDivUICapability.TDivUIHideViewDirective` `{Name}` | `divui_hide_view` |
| 3 | `draw_animation_directive` | `NAlice.TAnimationCapability.TDrawAnimationDirective` `{Animations[{S3Directory{Bucket: "https://quasar.s3.yandex.net", Path: "animations/player_pause"}}], Name}` | `draw_animation_directive` |
| 4 | `player_pause` | Struct `{"smooth": false}` | `general_conversation_player_pause` |
| 5 | `clear_queue` | без payload | `general_conversation_player_pause` |

Все `Type: client_action`, `IsLedSilent: true`. Собирает `server_emulator/media_directives.py`,
сценарий — `device.player.pause` (script).

- Форма **не зависит от состояния плеера**: typed-«стоп» на Idle-колонке тоже получала
  полную форму x20 (2026-10-23 16:52:41 и ещё 4 запроса, `(StateType=Idle)` → x20).
- Минорные варианты в журнале: x2 без `player_pause` (`placeholder + divui_hide_view +
  draw_animation + clear_queue`, Idle-плеер) и останов с `draw_led_screen_directive`
  (x1, колонка с LED-экраном). Не реализуем: выбор между ними по образцам не выводится,
  основная форма покрывает 20/23.
- «хватит» на играющей колонке без сигнала таймера/будильника — та же пауза
  (`ringing.stop` при ringing приоритетнее, см. `tasks/review/079-time-scenarios.md`).

## Состояние плеера в запросе (capability 46)

Колонка приносит состояние плеера в КАЖДОМ `TextInput` (замер журнала: cap 46 — 4174×,
92 — 4173×, 95 — 4172× из 4174): `Request.EnvironmentStateRaw.Endpoints[].AnyCapabilities[]`
`CapabilityType=46` (`AudioPlayerCapabilityType`) → `NAlice.TAudioPlayerCapability`:

- `State.StateType` — enum `EStateType`: 0 Unknown, 1 Idle, 2 Playing, 3 Paused,
  4 Buffering, 5 Stopped, 6 Finished;
- `State.Title` / `State.SubTitle` — что играет;
- `State.CommonPlayerState` — `IsPlaying`, `PlayedMs`, `DurationMs`,
  `ScenarioMeta` (`owner: "music"`, `@scenario_name: "HollywoodMusic"`,
  `__musicmeta_id`, `playback_context` — внутренние идентификаторы очереди Яндекса).

Разбирает `server_emulator/station_state.py` (SDK 2.0.0: `ctx.player` —
`{state, playing, title, subtitle, played_ms, duration_ms}`). Своей копии состояния у
эмулятора нет: что колонка не сообщает — того нет.

## Колбэки `HollywoodMusic`

Колбэки приходят служебным `TextInput` (`Event.Type=server_action`, `Text` пустой,
`Name` — имя колбэка). Ответ настоящего сервера — всегда **тихий**: пустой
`OutputSpeech.Text`, без карточек, только `tts_play_placeholder {channel: "Dialog"}`
(замер паринга: `music_thin_client_on_stopped` x134 → placeholder, x1 → голос;
`on_started` x112 → placeholder; `@@mm_stack_engine_get_next` x4 → placeholder).
Эмулятор отвечает на них `callbacks.ack` (маршруты в `aapi_handlers.py`,
сценарий `system.service_ack`) — та же форма.

| Колбэк колонки | Ответ настоящего сервера | Эмулятор |
|---|---|---|
| `music_thin_client_on_started` | x112: только `tts_play_placeholder` | `callbacks.ack` |
| `music_thin_client_on_stopped` | x134: только `tts_play_placeholder` | `callbacks.ack` |
| `@@mm_stack_engine_get_next` | x92: очередь Яндекса (см. ниже), x4: только placeholder | `callbacks.ack` |
| кнопки экрана плеера (`@@mm_typed_callback`, `purpose=player_*`) | — | `callbacks.ack` |

- `music_thin_client_on_paused` / `on_resumed` / `on_progress` в журнале **не встречались**
  (проход по всем блокам: только on_started/on_stopped/on_finished/on_failed/recovery).
- `@@mm_stack_engine_get_next` x92 сервер отвечал полной формой продолжения очереди:
  `apply_player_playback_queue_directive` (`TApplyMusicPlatformQueueDirective` — сущности
  треков, `MusicPlatformMetaStorage`, провайдер `MUSIC_PROVIDER_YANDEX_MUSIC`) +
  `set_audio_player_sequence_state_directive` + `tts_play_placeholder` + `audio_play`
  (URL потока Яндекса) + сам `@@mm_stack_engine_get_next`. Повторить её эмулятор не может:
  контент и URL очереди принадлежат Яндекс.Музыке — **БЛОКЕР, нужна запись** (в proxy
  эти колбэки уходят настоящему серверу).

## Что NOT воспроизводимо (БЛОКЕР — нужна запись)

Фразы «следующий/предыдущий трек», «продолжи», «на повторе», «вперемешку», «нравится»,
перемотка в журнале либо отсутствуют («что играет», «продолжить» — 0 запросов), либо их
ответы содержат контент и URL очереди Яндекса (`apply_player_playback_queue_directive`,
`audio_play` со ссылкой на поток), которые эмулятор взять неоткуда. Примеры форм
ответов (для будущих записей): x132 — очередь + `audio_play`; x10 — `player_resume`;
x5 — `set_audio_player_sequence_state_directive` (повтор/перемешивание:
`TSetSequenceStateDirective{RepeatType, ShuffleSeed}`); x17 — `draw_animation`
(`animations/player_like`) + like. Директивы без образца не отправляются.

## Что удалено (было в 071/072)

`server_emulator/media.py` (медиатека, `Player`, очередь `runtime.media_state`),
вкладка «Голос → Медиа» (`ui/media.js`, `ui/api/media.py`), маршрут раздачи `/media/…`,
`ctx.media` / `ctx.library` / `ctx.media_base` (SDK 2.0.0), сценарии `media.*`
(music/tales/radio/sounds/player/buttons/playback). Формат Struct-payload `audio_play`
(обмен 155) был верифицирован на живых колонках и остаётся справочным:
`{"stream": {url, type: Track|Radio, format, duration_ms}, "metadata": {title, subtitle,
art_image_url}, "screen_type": "Music", "background_mode": "Ducking", "set_pause": bool,
"playback_speed": 1.0}` — сам эмулятор `audio_play` с 087 не отправляет.
