# Плеер колонки: директивы паузы, состояние, поставщик контента

Музыка колонки играет штатным плеером (сценарий `HollywoodMusic` в состоянии колонки).
Своей медиатеки, очереди и избранного у эмулятора нет (087): контент отдаёт **поставщик
музыки** — расширение `music/*.py` (раздел «Поставщик контента», TASK-099; первым будет
Spotify — PLAN-spotify-music). Поставщика нет — музыкальные запросы остаются честному
ответу/модели («Напрямую»/«Эмуляция») или настоящему Яндексу («Вместе» — релей голоса,
098). Эмулятор управляет плеером только там, где есть образец ответа настоящего сервера.

Разбор по образцам журнала-захвата (2026-10-21…2026-11-02); обезличенный образец паузы —
`tests/fixtures/directives/media-pause.json`, полный стек виджета — раздел «Стек директорий
виджета» ниже (захват `state/captures/2026-11-02-music/`).

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
| `@@mm_stack_engine_get_next` | x92: очередь Яндекса (см. ниже), x4: только placeholder | `music.stack_next` → сценарий `media.music`: поставщик есть — следующий трек; нет — сценарий отказывается, умолчание маршрута (тихий ack) |
| кнопки экрана плеера (`@@mm_typed_callback`, `purpose=player_*`) | — | `callbacks.ack` |

- `music_thin_client_on_paused` / `on_resumed` / `on_progress` в журнале **не встречались**
  (проход по всем блокам: только on_started/on_stopped/on_finished/on_failed/recovery).
- `@@mm_stack_engine_get_next` x92 сервер отвечал полной формой продолжения очереди:
  `apply_player_playback_queue_directive` (`TApplyMusicPlatformQueueDirective` — сущности
  треков, `MusicPlatformMetaStorage`, провайдер `MUSIC_PROVIDER_YANDEX_MUSIC`) +
  `set_audio_player_sequence_state_directive` + `tts_play_placeholder` + `audio_play`
  (URL потока Яндекса) + сам `@@mm_stack_engine_get_next` с recovery-параметрами радио.
  Форму очереди/состояния эмулятор теперь собирает от поставщика (раздел «Стек директорий
  виджета»); recovery-параметры радио Яндекса (`@recovery_callback`, `radio.*`) —
  внутренние идентификаторы Яндекса, для стороннего контента не воспроизводимы и не
  отправляются.
- В «Вместе» колбэки контента Яндекса уходят наверх до маршрута (091,
  `voice.py PLAYER_CALLBACKS`); свой контент поставщика в «Вместе» не запускается —
  сценарии музыки там отказываются (релей голоса 098).

## Что NOT воспроизводимо (БЛОКЕР — нужна запись)

Фразы «продолжи», «на повторе», «вперемешку», «нравится», перемотка в журнале либо
отсутствуют («что играет», «продолжить» — 0 запросов), либо их ответы содержат
внутренности радио Яндекса (`player_resume`, recovery-параметры стека, лайк-карточки
`divui_patch_view`), которые для стороннего контента не воспроизводимы. Примеры форм
ответов (для будущих записей): x10 — `player_resume`; x5 —
`set_audio_player_sequence_state_directive` (повтор/перемешивание:
`TSetSequenceStateDirective{RepeatType, ShuffleSeed}`); x17 — `draw_animation`
(`animations/player_like`) + like. Директивы без образца не отправляются.
«Следующий/предыдущий» больше не в этом разделе: очередь отдаёт поставщик (ниже).
Лайк/дизлайк — `rate` поставщика (интерфейс есть, директивы образца — при записи).

## Поставщик контента (TASK-099)

Контент отдаёт расширение с папкой `music/*.py` (docs/contracts/extensions.md); ядро —
`server_emulator/music.py` (реестр, сборка директорий), сценарий — `media.music`
(`scenarios/media/`). Своей очереди/медиатеки у ядра нет: треки и контекст очереди —
от поставщика, состояние плеера — из запроса колонки (`ctx.player`).

Интерфейс (SDK 2.4.0, `ctx.music` — запись выбранного поставщика или None):

```python
PROVIDER = {"id": "spotify", "title": "Spotify"}          # id — настройка settings["music"]["provider"]; пусто — первый включённый

async def search(ctx, query, kind):      # kind: music | tales | podcast | audiobook; "" — «что-нибудь»
    return {"tracks": [Track, ...], "start": 0, "context": {...}}   # очередь режется до MAX_QUEUE=5

async def neighbor(ctx, track_id, context, step):   # step +1/-1; None — дальше нечего
    return Track | None                   # голосом/колбэком колонка id не передаёт — поставщик ведёт очередь сам

async def rate(ctx, track_id, like):     # необязательна; None — не поддерживается
    return True | None

# Track = {"id", "title", "artist", "album"?, "duration_ms", "cover_url"?,
#          "stream": {"url", "format": "mp3|aac|flac", "decode_key"?}}
```

- Вызов функций поставщика — `music.search/neighbor/rate(ctx, provider, …)` ядра: они
  ставят коду поставщика его настройки (`ctx.extension`, как у режимов «Интеллекта») и
  приводят результат к контракту (Track без `id`/`title`/`stream.url` отбрасывается).
- Маршруты: «Вместе» — сценарии музыки отказываются всегда (Яндекс отвечает сам через
  релей голоса 098; колбэки контента Яндекса уходят наверх — 091). «Напрямую»/
  «Эмуляция» — поставщик есть: играет он во всех режимах «Интеллекта»; поставщика нет:
  «Простой» — «Для этого нет источника данных.» (`ctx.no_data_source()`), «Модель/
  Агент» — отказ → модель.
- Выбранный поставщик — настройка `settings["music"]["provider"]` (id; пусто — первый
  включённый по алфавиту; выключенное расширение просто не выбирается). Конфликт id или
  битый модуль отключает расширение целиком — как у actions/datasets.

## Стек директорий виджета (по образцу захвата 2026-11-02-music)

Виджет плеера отражает воспроизведение только при полном стеке директив ответа
(захват `state/captures/2026-11-02-music/`, декодированные PayloadRaw):

| # | `Name` | тип payload | содержание |
|---|---|---|---|
| 1 | `tts_play_placeholder` | Struct `{"channel": "Dialog"}` | каркас ответа |
| 2 | `draw_animation_directive` | proto `TDrawAnimationDirective` | `animations/player_play` (старт; в паузе — `player_pause`) |
| 3 | `show_audio_player_screen_directive` | proto `TShowAudioPlayerScreenDirective` | `{TrackId: "<id>"}` |
| 4 | `apply_player_playback_queue_directive` | proto `NAlice.TMusicPlatformQueueCapability.TApplyMusicPlatformQueueDirective` | очередь: `Entities` (по треку, `PlaybackEntity.TrackEntity.Track{Id, Uuid}`) + `CurrentEntityIndex` + `MusicPlatformMetaStorage.ProviderMeta[<провайдер>].TrackMetaRecords{id → TrackMeta{id, album_id, artist_ids, title, image_url, duration_ms}}` |
| 5 | `set_audio_player_sequence_state_directive` | proto `NAlice.TAudioPlayerCapability.TSetSequenceStateDirective` | `SequenceState{CurrentItem.Track{Id, Uuid}, MetaStorage{ProviderTrackMeta/ProviderArtistMeta/ProviderAlbumMeta[<провайдер>]}}` |
| 6 | `audio_play` | Struct | `stream{url, type: Track, format, duration_ms?, decode_key?}` + `metadata{title, subtitle, art_image_url?, hide_progress_bar}` + `screen_type: Music` + `background_mode: Ducking` |

- «дальше/назад» (колбэк `@@mm_stack_engine_get_next`, фразы — при поставщике 100) — та
  же тройка 4+5+6 с `tts_play_placeholder`, без экрана и анимации (очередь образца
  перестраивается под новый трек; у поставщика neighbor отдаёт один трек — очередь из
  него).
- Имя провайдера в картах меты — из enum `Alice.Music.MusicProvider`:
  `MUSIC_PROVIDER_YANDEX_MUSIC` для `yandex-music`; другим поставщикам значения в схеме
  нет — `MUSIC_PROVIDER_UNSPECIFIED` (идентификаторы треков/артистов — строки поставщика,
  артист идёт и сущностью, и записью меты). Форма очереди образца от настоящего сервера
  была минимальной (пустая сущность + карта провайдера, 41 байт) — эмулятор собирает
  сущности из результатов поиска по решению владельца (PLAN-spotify-music): меты нужны
  виджету для названия/обложки.
- Поле `provider_name`, `scenario_meta`, embedded-колбэки `music_thin_client_*` в
  `audio_play` и серверный `server_action` `@@mm_stack_engine_get_next` (recovery радио
  Яндекса) из образца не воспроизводятся честно и не отправляются — колонка шлёт
  `music_thin_client_*`/`@@mm_stack_engine_get_next` сама (проверено пробой 098).

## Что удалено (было в 071/072)

`server_emulator/media.py` (медиатека, `Player`, очередь `runtime.media_state`),
вкладка «Голос → Медиа» (`ui/media.js`, `ui/api/media.py`), маршрут раздачи `/media/…`,
`ctx.media` / `ctx.library` / `ctx.media_base` (SDK 2.0.0), прежние сценарии `media.*`
(radio/sounds/player/buttons/playback). Формат Struct-payload `audio_play`
(обмен 155) был верифицирован на живых колонках и остаётся справочным:
`{"stream": {url, type: Track|Radio, format, duration_ms}, "metadata": {title, subtitle,
art_image_url}, "screen_type": "Music", "background_mode": "Ducking", "set_pause": bool,
"playback_speed": 1.0}` — с 099 эмулятор отправляет `audio_play` только от поставщика
(раздел «Поставщик контента»).
