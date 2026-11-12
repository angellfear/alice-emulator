# Плеер колонки: директивы паузы, состояние, поставщик контента

Музыка колонки играет штатным плеером (сценарий `HollywoodMusic` в состоянии колонки).
Своей медиатеки, очереди и избранного у эмулятора нет: контент отдаёт **поставщик
музыки** — встроенный плагин или расширение, объявившее вид `music` в
`contributes.providers` (раздел «Поставщик контента»; встроенные — `builtin.spotify`
и `builtin.yandex-music`, docs/contracts/extensions.md). Поставщика нет — музыкальные
запросы остаются честному ответу/модели («Напрямую»/«Эмуляция») или настоящему
Яндексу («Вместе» — релей голоса).
Эмулятор управляет плеером только там, где есть образец ответа настоящего сервера.

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
  (`ringing.stop` при ringing приоритетнее).

## Перемотка («перемотай на минуту вперёд», «перемотай на 30 секунд», «перемотай назад»)

`audio_player_rewind` — Struct `{"amount_ms": <длительность × 1000>, "type": "Forward" |
"Backward"}`: protos-сообщения нет (справочник `directives.md`), форма — по решению
владельца 2026-11-07, подтверждена записью .85 2026-11-12 13:57:00. Ответ без речи и
карточек: перемотка первой директивой, `tts_play_placeholder` — последней (как в записи).
Собирает сценарий `device.player.rewind` (script); направление — сущность
`rewind_direction` лексикона или слова фразы, без слова о направлении — `Forward`
(запись: «перемотай на 30 секунд» без направления); длительность — слоты шаблона или
разбор фразы; без длительности — уточняющий вопрос (значение по умолчанию не выдумывается).

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
| кнопки экрана плеера (`@@mm_semantic_frame`, `PayloadRaw` = `NAlice.NAliceApi.TSemanticFrameRequestData`: `PlayerContinueSemanticFrame`, `PlayerNextTrackSemanticFrame`, `PlayerPrevTrackSemanticFrame`) | образца нет | `music.touch` — как кнопки виджета |
| кнопки виджета музыки (`@@mm_typed_callback`, `NAlice.TMusicTouchTypedCallback`: `Continue`, `NextTrack`, `PrevTrack`) | образца нет | `music.touch` → сценарий `media.music`: ответы фраз «продолжи», «дальше», «предыдущий» без речи; нечего играть — тихий ack |

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
- **recoverCommit** (quasar-server music/manager.ts, решение владельца 2026-11-07):
  `audio_play` уходит с `callbacks.on_started.payload.music {type: "recoverCommit",
  recoverCommitToken: <uuid>}` (стеки «включи музыку» и «дальше»); колонка при старте
  каждого трека присылает `TextInput` с тем же токеном: `Event.Type=server_action`,
  `Name` пустой, `Payload.music {type: "recoverCommit", recoverCommitToken}` (журнал
  C00NX 2026-11-10; форма `@@mm_semantic_frame` с `music` во фрейме тоже принимается).
  Маршрут `music.recover_commit` → сценарий `media.music`: токен снимается
  (`music.py COMMITS`, 10 минут), ответ тихий. Следующий трек на commit не отдаётся:
  `audio_play` в ответ заменил бы только что начавшийся трек. Соседа заранее не
  запрашиваем — `neighbor` поставщика сдвигает его позицию очереди.
- Фразы «дальше», «следующий трек», «предыдущий», «продолжи» — записи `next`/`prev`/
  `continue` справочника фраз поставщика (`phrases/music.json`): `neighbor(±1)` стеком
  «дальше»; «продолжи» — с места паузы (как «включи музыку» на паузе), иначе текущий
  трек поставщика сначала; дальше нечего — «Дальше ничего нет.» / «Это первый трек.».
- Кнопки виджета музыки в шторке и на главном экране: колонка шлёт `@@mm_typed_callback`
  с `typed_callback_serialized` = `Any(NAlice.TMusicTouchTypedCallback)` и
  `request_params.disable_output_speech`. Поле запроса `music_command` (`continue`/`next`/`prev`)
  разбирается по схеме станции из `Command` (oneof `Value`); прочие команды (`Like`, `Repeat`,
  `Shuffle`…) идут в общий тихий ack. Лайк и пауза колонка выполняет сама.
  Кнопки «плей», «далее», «назад» на экране плеера приходят иначе: `@@mm_semantic_frame`
  без `Payload`, кадр — в `PayloadRaw` (`TSemanticFrameRequestData.TypedSemanticFrame`,
  `Analytics.Purpose` = `player_continue`/`player_next_track`/`player_prev_track`).
  Тот же `music_command` и тот же маршрут `music.touch`.
  Только при треке в плеере колонки (`ctx.player.title`); плеер пуст — сценарий
  отказывается, фраза уходит дальше (в играх «дальше» — ход модели).
- В «Вместе» колбэки контента Яндекса уходят наверх до маршрута
  (`voice.py PLAYER_CALLBACKS`); свой контент поставщика в «Вместе» не запускается —
  сценарии музыки там отказываются (релей голоса).

## Что NOT воспроизводимо (БЛОКЕР — нужна запись)

Фразы «продолжи» и «вперемешку» в журнале либо отсутствуют, либо их ответы содержат
внутренности радио Яндекса (`player_resume`, recovery-параметры стека, лайк-карточки
`divui_patch_view`), которые для стороннего контента не воспроизводимы. Примеры форм
ответов (для будущих записей): x10 — `player_resume`; x5 —
`set_audio_player_sequence_state_directive` (`TSetSequenceStateDirective{RepeatType,
ShuffleSeed}`: RepeatType — «повтор» реализован ниже, ShuffleSeed — «перемешай» нет:
ответ записи .85 2026-11-12 13:57:11 — только облачная `divui_show_view` карта и речь,
перемешивание ведёт радио Яндекса, воспроизводимой формы для очереди поставщика нет);
x17 — `draw_animation` (`animations/player_like`) + like. Директивы без образца не
отправляются. «Следующий/предыдущий» больше не в этом разделе: очередь отдаёт поставщик
(ниже). «На повторе» и «что играет» реализованы по записи .85 2026-11-12 (ниже).
«Лайк» — запись `like` справочника фраз поставщика, только при треке в плеере колонки:
`rate(текущий, True)` поставщика; подтвердил — ответ образца (`music.like_reply`):
`draw_animation_directive` (`animations/player_like`), `set_audio_player_sequence_state_directive`
(`CurrentItem.Track.LikeStatus = LIKE_STATUS_LIKE`), `apply_player_playback_queue_directive`
(только мета `TrackMetaRecords[трек]{TrackMeta{}, LikeStatus}`), `tts_play_placeholder` последним,
без `audio_play`; карточка «Рада, что вы оценили.», речь «Рада что вы оценили.».
`divui_patch_view` и `set_div_variables` образца правят div-карточку плеера облака
(`CardId music_scenario_player_key_<трек>_key`), которую эмулятор не показывает, — в ответ
не входят. Поставщик не подтвердил — отказ словами.
«Дизлайк» — запись `dislike`: `rate(текущий, False)`, затем `neighbor(+1)`; ответ образца
(`music.dislike_reply`) — `draw_animation_directive` (`animations/player_dislike`) и стек «дальше»
на следующий трек (очередь, состояние, `tts_play_placeholder`, `audio_play` с токеном
recoverCommit), речь «Нет проблем, поставила дизлайк.» без карточки; следующего нет — только
анимация и речь. `set_div_variables` образца (div-карточка облака) не входит.
Яндекс Музыка ставит оценку в аккаунте (`account_status` → `users_likes_tracks_add` /
`users_dislikes_tracks_add`), Spotify оценок не поддерживает.
«Поставь на повтор» — запись `repeat` справочника фраз поставщика, только при треке в
плеере колонки: ответ образца (запись .85 2026-11-12 13:57:05) —
`set_audio_player_sequence_state_directive` (`CurrentItem.Track.RepeatType =
REPEAT_TYPE_TRACK`) и `tts_play_placeholder`, речь «Хорошо, буду повторять.» с карточкой.
`set_glagol_metadata` образца (мета волны `user:onyourwave`) и облачная `divui_show_view`
карта в ответ не входят.
«Что играет» — запись `what_playing` (запись .85 2026-11-12 13:57:18): «Это <исполнитель>,
<альбом>, песня "<название>".» — из текущего трека очереди поставщика, иначе из сообщений
колонки о плеере (`ctx.player`); директив, кроме каркаса, в образце нет (облачная карта не
входит).

## Поставщик контента

Контент отдаёт расширение, объявившее вид `music` в `contributes.providers`
(docs/contracts/extensions.md); выбор активного поставщика и его настройки — вкладка
«Поставщики данных» (docs/contracts/providers.md); ядро —
`server_emulator/music.py` (реестр, сборка директорий), сценарий — `media.music`
(`scenarios/media/`). Своей очереди/медиатеки у ядра нет: треки и контекст очереди —
от поставщика, состояние плеера — из запроса колонки (`ctx.player`).

Интерфейс (SDK 2.4.0, `ctx.music` — запись выбранного поставщика или None):

```python
PROVIDER = {"id": "builtin.yandex-music", "title": "Яндекс Музыка", "service": "yandex-music"}
# id — настройка settings["providers"]["music"]; пусто — первый включённый по алфавиту.
# service — признак сервиса вместо id (встроенный и внешний Яндекс.Музыки — оба получают
# токен и ключ меты); без service — совместимость по id.

async def search(ctx, query, kind):      # kind: music | tales | podcast | audiobook; "" — «что-нибудь»
    return {"tracks": [Track, ...], "start": 0, "context": {...}}   # очередь режется до MAX_QUEUE=5

async def neighbor(ctx, track_id, context, step):   # step +1/-1, 0 — текущий; None — дальше нечего
    return Track | None                   # позицию очереди не двигает — её сверяет commit

async def rate(ctx, track_id, like):     # необязательна; None — не поддерживается
    return True | None

async def commit(ctx, track_id):         # необязательна; recoverCommit — подтверждённый старт трека
    return True | None                   # позиция очереди сверяется с играющим; чужой трек — None

# Track = {"id", "title", "artist", "album"?, "duration_ms", "cover_url"?,
#          "stream": {"url", "format": "mp3|aac|flac", "decode_key"?}}
```

- Вызов функций поставщика — `music.search/neighbor/rate/commit(ctx, provider, …)` ядра: они
  ставят коду поставщика его настройки (`ctx.extension`, как у режимов «Интеллекта») и
  приводят результат к контракту (Track без `id`/`title`/`stream.url` отбрасывается).
- Позиция очереди поставщика двигается только подтверждённым стартом трека
  (recoverCommit → `commit`): предзагрузка `@@mm_stack_engine_get_next` и явные
  «дальше/предыдущий» её не сдвигают, иначе «предыдущий» после предзагрузки возвращает
  играющий трек вместо прошлого.
- Маршруты: «Вместе» — сценарии музыки отказываются всегда (Яндекс отвечает сам через
  релей голоса; колбэки контента Яндекса уходят наверх). «Напрямую»/
  «Эмуляция» — поставщик есть: играет он во всех режимах «Интеллекта»; поставщика нет:
  «Простой» — «Для этого нет источника данных.» (`ctx.no_data_source()`), «Модель/
  Агент» — отказ → модель. Голый шаблон («запусти {query}») без поставщика уступает
  следующему кандидату (например, приложению); явные музыкальные фразы («включи
  музыку», «включи песню X», сказки/подкасты) остаются за сценарием. Не-музыкальные
  домены голого шаблона («включи свет», «поставь таймер») отсекает guard сценария
  (привязка media_other_domain, сравнение по нормальной форме слова через pymorphy3):
  «включи светлану лободу» и «включи шоу маст гоу он» — музыка, «включи кино»,
  «включи комедию», «поставь мультики» и «включи утреннее шоу» — нет.
- Поставщик вернул пустой результат с «note» — текст произносится пользователю как
  честный отказ вместо общего «не нашла подходящего».
- «включи музыку» без запроса: плеер колонки (`ctx.player`) на паузе или остановлен, и его
  заголовок совпадает с текущим треком поставщика (`neighbor(step=0)`) — продолжение с
  места по образцу «продолжи»: очередь + состояние + `audio_play` того же трека с
  `stream.offset_ms` = `played_ms`. Иначе — поиск с пустым запросом; поставщик, которому
  нечего играть, спрашивает «Что включить?» (note с «?») — это вопрос уточнения
  (`ShouldListen`), ответ пользователя становится запросом поиска.
- Выбранный поставщик — настройка `settings["providers"]["music"]` (id; пусто — первый
  включённый по алфавиту; прежний ключ `settings["music"]["provider"]` переносится при
  загрузке; выключенное расширение просто не выбирается). Конфликт id в пределах вида или
  битый модуль отключает расширение целиком — как у actions/datasets.
- Захваченный OAuth-токен аккаунта (`captured_oauth`) передаётся только поставщикам
  Яндекс.Музыки — по признаку сервиса `PROVIDER["service"] = "yandex-music"` (без service —
  совместимость по id); другие поставщики получают обычный ExtensionContext без него. Захват
  принимает токен только от опознанной колонки (HTTP — после разрешения устройства,
  WS-handshake — по `x-uprx-device-id`).

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

- «дальше/назад» (колбэк `@@mm_stack_engine_get_next`, фразы — при поставщике) — та
  же тройка 4+5+6 с `tts_play_placeholder`, без экрана и анимации (очередь образца
  перестраивается под новый трек; у поставщика neighbor отдаёт один трек — очередь из
  него).
- Имя провайдера в картах меты — из enum `Alice.Music.MusicProvider`:
  `MUSIC_PROVIDER_YANDEX_MUSIC` для `yandex-music`; другим поставщикам значения в схеме
  нет — `MUSIC_PROVIDER_UNSPECIFIED` (идентификаторы треков/артистов — строки поставщика,
  артист идёт и сущностью, и записью меты). Форма очереди образца от настоящего сервера
  была минимальной (пустая сущность + карта провайдера, 41 байт) — эмулятор собирает
  сущности из результатов поиска по решению владельца: меты нужны
  виджету для названия/обложки.
- Поле `provider_name`, `scenario_meta`, embedded-колбэки `music_thin_client_*` в
  `audio_play` и серверный `server_action` `@@mm_stack_engine_get_next` (recovery радио
  Яндекса) из образца не воспроизводятся честно и не отправляются — колонка шлёт
  `music_thin_client_*`/`@@mm_stack_engine_get_next` сама (проверено пробой).

## Что удалено

`server_emulator/media.py` (медиатека, `Player`, очередь `runtime.media_state`),
вкладка «Голос → Медиа» (`ui/media.js`, `ui/api/media.py`), маршрут раздачи `/media/…`,
`ctx.media` / `ctx.library` / `ctx.media_base` (SDK 2.0.0), прежние сценарии `media.*`
(radio/sounds/player/buttons/playback). Формат Struct-payload `audio_play`
(обмен 155) был верифицирован на живых колонках и остаётся справочным:
`{"stream": {url, type: Track|Radio, format, duration_ms}, "metadata": {title, subtitle,
art_image_url}, "screen_type": "Music", "background_mode": "Ducking", "set_pause": bool,
"playback_speed": 1.0}` — эмулятор отправляет `audio_play` только от поставщика
(раздел «Поставщик контента»).


## Поставщик Яндекс.Музыки и захват OAuth аккаунта

Встроенный поставщик `builtin.yandex-music` (`server_emulator/plugins/yandex-music/`,
`PROVIDER["service"] = "yandex-music"`) работает через библиотеку `yandex-music`
(зависимость эмулятора): `Client.search(type_="track")` и **полные треки** — прямые ссылки
mp3 без decode_key из `tracks_download_info(get_direct_links=True)` (лучший битрейт без
превью). Ссылка get-mp3 отвечает 308 на `strm.yandex.net`, а плеер колонки (gstreamer
souphttpsrc) 308 не проходит, поэтому колонке уходит адрес после перенаправлений (`requests`,
сбой — исходная ссылка). Внешний поставщик Яндекс.Музыки
(если установлен) живёт рядом — другой id, тот же service: токен и ключ меты
(`MUSIC_PROVIDER_YANDEX_MUSIC`) получают оба.

**OAuth-токен аккаунта не вводится** — эмулятор захватывает его из реальных обращений колонки
к Яндексу: заголовок `Authorization: OAuth …` (HTTP-запросы через диспетчер) и
`X-UPRX-OAUTH-TOKEN` (WS-handshake uniproxy). Точки захвата — `dispatcher.handle` и
`runtime.websocket` → `music.note_token(runtime, token)`: кеш ядра (`music.captured_oauth()`)
+ персистентность в БД состояния (meta `yandex_oauth`, выживает перезапуск; гидратация —
`music.load_token(store)` при старте). Токен в журнал не пишется (захват до маскирования);
мусор вместо токена не затирает захват.

Ядро передаёт токен поставщику через ExtensionContext вызова: `music.extension_context`
добавляет системное значение `captured_oauth` — поставщик читает стандартным
`ctx.extension.get("captured_oauth")`. До первого обращения колонки к Яндексу (после
перезапуска) поставщик отвечает честным note «токен ещё не захвачен».
