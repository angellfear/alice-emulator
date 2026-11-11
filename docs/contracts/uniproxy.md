# Yandex uniproxy — протокол сессии колонки (наблюдения и эксперименты)

Истина для поведения настоящего `wss://uniproxy.alice.yandex.net/uni.ws`. Всё ниже проверено
на живой сессии (журнал `state/traffic.sqlite3` и зонд, 2026-11-04…06).
Дополняется по мере наблюдений; противоречит старым записям — править здесь.

## 1. Фрейминг

- Кадры WS — бинарные. AAPI-кадр: `b"AAPI" + TClientMessage` (колонка → сервер) /
  `b"AAPI" + TServerMessage` (сервер → колонка); схемы — `server_emulator/schemas/alice-api.pb`
  (`NAlice.NAliceApi.TClientMessage` / `TServerMessage`).
- Бинарный кадр аудио: `struct.pack(">I", StreamId)` + аудиоданные (Opus в Ogg для
  `Format: audio/opus`). Префикс — номер аудиопотока из `VoiceInput.Header.StreamId`.
- Имена полей в проводе отсутствуют (только номера тегов) — искать кадр подстрокой имени
  поля (`b"SynchronizeStateRequest" in raw`) бессмысленно: разбор только через дескриптор.

## 2. Авторизация сессии

- Первым кадром колонка шлёт `SynchronizeStateRequest` (внутри `OAuthToken`, `Application.DeviceId`).
- **Повтор этого кадра открывает параллельную сессию от имени колонки** (так работает ветка
  «Вместе», `upstream/hybrid.py`). Минимальных заголовков рукопожатия достаточно — авторизация
  в кадре sync, не в заголовках (зонд подключается с одним `User-Agent`).
- Сервер отвечает `SynchronizeStateResponse` (SessionId) и шлёт `Push {notify}`.
- Sync-кадр колонки есть в журнале (source=emulator, клиентские кадры; ротация 3 дня) — зонд
  сохраняет его во временную папку, в git не класть (OAuthToken внутри).

## 3. Голосовой ход (VoiceInput → ответ)

Порядок на проводе (образцы: source=emulator, кадры колонки):

1. `VoiceInput` — открывает ход: `Header.StreamId` = номер аудиопотока; `VoiceInput.Format`
   (`audio/opus` у колонки), `Request.ActivationType: spotter`, `EnableSpotterValidation`
   (см. §4), `Request` целиком несёт `DeviceStateRaw`/`EnvironmentStateRaw`/`AdditionalOptions`
   (OAuthToken)/`UniproxyOptions` — сессию ASR авторизуют они; `Event.Type = 1`.
2. Бинарные аудиокадры (префикс StreamId) — стриминговый ASR слушает поток в **реальном
   времени**: 3 с аудио, отправленные за 0.1 с, дают пустой endpoint (зонд, 2026-11-06).
3. Кадров `StreamControl{Start}` от колонки **нет** — поток объявляет сам VoiceInput.
4. `Close` колонки — только **после** ответа (`Reason: 0 OK` после проигрывания; `4 CANCEL` при
   отмене). Endpoint записи решает сервер (сервер шлёт колонке `StreamControl{Close,
   STOP_RECORDING}` — код 2), колонка стримит до этого момента.
5. `Close` **требует `MessageId`** (uuid) — без него сервер отвечает
   `EventException: "StreamControl without 'messageId'"` и рвёт ASR по таймауту
   (`asr-server error: Timeout`, scope=WS_ADAPTER_OUT).

## 4. SpotterValidation

- `EnableSpotterValidation = true` в VoiceInput → сервер ждёт в аудио споттер-фразу («Алиса»)
  и её валидацию: без неё ASR завершается пустым результатом (зонд 2026-11-06).
- Для синтетических фраз (TTS без «Алисы») — `EnableSpotterValidation = False`, тогда весь
  поток распознаётся как речь сразу.

## 5. Цепочка ответа

`InputStartAck` → (`SpotterValidation`, если включён) → `AsrResult` партиалы →
`AliceResponse` (директивы, обычно первой `tts_play_placeholder`) → `DeferredAliceResponse`
(прогрессивно: `ResponsePartialNum` 0…N, `IsLast: true` на последней, текст в
`BaseResponse.Text`, карточки в `BaseResponse.Cards`) → `TtsSpeak` → бинарные кадры TTS-аудио
(Opus, без AAPI-префикса).

- Финальный `AsrResult` с `EndOfUtt: true` бывает **пустым** (`AggregatedRecognition.RecognizedText`
  пуст) — это маркер endpoint, не распознавание; текст брать из последнего непустого
  партиала или `Recognition[].Value/Normalized`.
- Пустой финал без партиалов — ASR не услышала ничего (проверить формат/пейсинг/споттер).

## 6. TextInput — мёртв для контента

Мегамайнд не отвечает содержанием на пересланный `TextInput` станции: факты/музыка/новости —
голая заглушка `tts_play_placeholder {v: false}` без продолжения. Единственный образец
TextInput от станции в захватах — бардж-ин «стоп» (на него сервер отвечает минимумом:
`set_intonation_spotting` + пустой placeholder). Текстовая пересылка промахов удалена;
промах релеится голосом.

## 7. RequestStat и отложенный ответ

`RequestStat` колонка шлёт **по завершении хода** (в образце — через ~11 с после заглушки).
Ранний `RequestStat` (сразу после заглушки) мегамайнда принимает за конец хода и отменяет ещё
не доставленный отложенный ответ (сессия 01:52) — релей RequestStat запрещён
(`hybrid.relay`).

## 8. Сессии и лимиты

- Несколько параллельных сессий с одним sync-кадром допустимы (ветка «Вместе» + зонд).
- `Push {notify}` после sync — рутинное уведомление, повторяется в каждой сессии.
- Протокольные кадры ветки (`InputStartAck`, `AsrResult`, `SpotterValidation`) второй сессии
  не должны попадать в первую (чужой StreamId ломает состояние канала колонки) — `hybrid._on_frame`
  фильтрует по событию.

## 9. SyncPush: опрос состояния колонки (fetch_state)

Сервер опрашивает колонку о её состоянии, не дожидаясь её же запросов (поля — quasar-protos
`alice/protos/api/alicekit/events/sync_push/**`, сборка по коду quasar-server):

- Сервер → колонка: `TServerMessage{Event{Header{MessageId: uuid, Ack: now_ms}, SyncPush{
  PushId=1: uuid, DeduplicationPushId=2: uuid, SyncRequest=3{GetDeviceState=1{
  GetDeviceStateOptions=1{}, GetEnvironmentStateOptions=2{}}}}}}` — пустые опции остаются
  в кадре; сборка — `runtime.sync_push_message`.
- Колонка → сервер: `Event.SyncPushResponse{SyncResponse{GetDeviceStateResponse{
  DeviceState, EnvironmentState}}}`; `SyncResponse` — oneof `Response`, прочие ветки
  (квитанции ExecuteDirective*) — другой канал push.
- Обработчик `_on_sync_push_response` (`runtime.py`) отдаёт ответ ожидающему
  `runtime.fetch_state(sn)` по сессии устройства. Событие всегда обработано — это ответ
  на НАШ push: в «Без ответа» не попадает, в «Вместе» наверх не релеится (релей
  пересылает только StreamControl и Ack).
- `runtime.fetch_state(sn, timeout=5.0)`: отправка в живую WS-сессию, ожидание не дольше
  5 с (повторный опрос устройства отменяет предыдущее ожидание), разбор —
  `station_state.fetch_state_result`: будильники/таймеры/плеер — из `EnvironmentState`
  (те же возможности, что в запросах колонки; у таймеров — remaining на момент опроса),
  громкость — `DeviceState.SoundLevel/SoundMaxLevel/SoundMuted`, имя ассистента — spotter
  sync-конфига (`config.account_config.spotter`: alisa→Алиса, yandex→Яндекс, yasmina→Ясмина).
- Итог сохраняется в `devices.state.fetch_state_last` = `{ts, alarms, timers, volume,
  player, assistant_name}`. Частота ограничена настройкой
  `settings["fetch_state"]["min_interval_s"]` (секунды, по умолчанию 10, 0 — без лимита):
  чаще — 429 из API (`FetchStateThrottled` для программных вызовов — тихий отказ); нет
  ответа вовремя — 504 (`FetchStateTimeout`); ожидание вытеснено новым опросом —
  409 (`FetchStateSuperseded`); нет живой сессии — 400.
- API: `POST /emulator/api/fetch_state {sn}`. UI — вкладка «Устройства»: кнопка «Опросить»
  открывает карточку «Состояние (опрос)» с последними данными и повторным опросом.
- Тесты: `tests/test_fetch_state.py`; релей — `tests/test_ws_hybrid.py`.

## 9a. Push-действия: Push и квитируемый SyncPush{ExecuteDirective}

Два канала доставки действий панели/MCP (`server_emulator/actions.py`, `runtime.push_action`):

- **Push** (`Event.Push{Directives:[TDirective]}`) — без квитанции: `execution: unconfirmed`
  до ответа колонки. Косметика и push_tts.
- **SyncPush{ExecuteDirective}** (`Event.SyncPush{PushId, DeduplicationPushId, SyncRequest=
  {ExecuteDirective={Directive (TDirective), ResponsePolicy{OnFinishedPolicy}}}}`) — важные
  действия: колонка отвечает `SyncPushResponse{SyncResponse{ExecuteDirectiveResponse{
  ExecutionStatus: oneof TExecutionStarted=1 | TExecutionCompleted=2 | TExecutionFailed=3}}}`
  (поля — quasar-protos `sync_push/**`, сборка — `actions.execute_message`). Квитанция ждётся
  не дольше 5 с (`EXECUTE_TIMEOUT_S`): статус `started`/`completed`/`failed` — в action_log и
  в ответ API-действия; нет ответа вовремя — `unconfirmed`. Обработчик `_on_sync_push_response`
  маршрутизирует по ветке `SyncResponse` (GetDeviceState → fetch_state, ExecuteDirective →
  квитанция); на устройстве одно ждущее действие, новый опрос/действие отменяет прежнее
  ожидание. Корреляция кадра — `RefMessageId` заголовка (тело ответа идентификатор push не
  несёт); живого обмена квитанциями на станции не снято — поведение при реальной колонке
  подтверждает владелец.

| Действие | Канал | Примечание |
|---|---|---|
| push_update_config | Push | по образцу настоящего сервера: директива без payload и IsLedSilent, `AnalyticsMetaInfo.RequestId`; колонка сама запрашивает `get_sync_info` |
| player_pause {—} | ExecuteDirective | директива `player_pause{smooth:false}` (образец media-pause) |
| clear_queue {—} | ExecuteDirective | директива `clear_queue` |
| sound_louder / sound_quiter / sound_set_level | Push | косметика |
| start_bluetooth / stop_bluetooth | Push | косметика |
| draw_led_screen {frames} | Push | LED-анимация |
| push_tts {text} | Push | директива `@@mm_semantic_frame` (server_action) с payload
  `{typed_semantic_frame: {tts_semantic_frame: {text}}}` — колонка возвращает тот же фрейм
  TextInput'ом, сервер отвечает репликой с этим текстом через голосовой конвейер: маршрут
  колбэка `tts.speak` (frame=tts_semantic_frame), поставщик — сценарий `system.push_tts`
  (текст фрейма — `input.frame_text` в `service_event_fields`); у реплики синтез TTS и
  `HasVoiceResponse` как у фразового пути |

## 10. Зонд фраз-блокеров

Один прогон — одна фраза: TTS/WAV → Opus → sync-кадр → VoiceInput (реальный кадр колонки,
`EnableSpotterValidation=false`, свежий MessageId) → аудио в реальном темпе → ответ.
Проверка — партиалы AsrResult; результаты — в `state/captures/2026-11-06-probe/<фраза>/`
(`answer.json`, `frames.bin`, аудио). Кадры-источники: sync и VoiceInput — из журнала
(source=emulator), до ротации.

Ответы сервера на фразы-блокеры (голос — синтез SAPI Irina, 2026-11-06; интент-маршрутизация
подтверждена директивами):

| фраза | директивы ответа |
|---|---|
| выключись через 30 минут | `timer_set_directive` |
| выключи музыку в полночь | `timer_set_directive` |
| удали будильник на 7 утра | `open_screen_alarm_list_directive` |
| перенеси будильник на 8 часов | `open_screen_alarm_list_directive` |
| отложи будильник | `open_screen_alarm_list_directive` |
| поменяй мелодию будильника | `tts_play_placeholder` |
| будильник по будням в 7 утра | `open_screen_alarm_list_directive` + `alarm_add_directive` |
| напомни через 10 минут проверить плиту | `divui_show_view` + `update_informer_directive` |
| какие встречи сегодня | `tts_play_placeholder` |

Полные payload (поля protobuf) — `state/captures/2026-11-06-probe/<фраза>/frames.bin`
(парсинг — `server_emulator.schema.load_type`, TServerMessage) и `answer.json`.
Блокеры (сон-таймер, будильник удалить/перенос/отложить/мелодия/будни, напоминания,
календарь) — сняты: образцы ответов получены.

## 11. Захват OAuth аккаунта

OAuth-токен аккаунта приходит в каждом сеансе: `SynchronizeStateRequest.OAuthToken` (sync),
заголовок `X-UPRX-OAUTH-TOKEN` (WS-handshake), `Authorization: OAuth …` (HTTP к quasar и др.).
Эмулятор захватывает его из реальных обращений колонки (`music.note_token`: dispatcher.handle +
runtime.websocket) и хранит в БД состояния (meta `yandex_oauth`) — поставщик Яндекс.Музыки
использует его для Web API. До первого обращения колонки поставщик честно отказывается.
Подробно — `docs/contracts/player.md`, раздел «Поставщик Яндекс.Музыки».

## 12. Чего НЕ проверено

- Поведение колонки при `DeferredAliceResponse` со звуком до `IsLast` (см. aapi.md).
- Мультирум, `AttachToMultiroom`, звонки.
- Потоки `ConnectionData` в `SynchronizeStateResponse`.
- SyncPush{GetDeviceState} на живой колонке: сборка по коду quasar-server, ответ — по
  схеме; живой обмен не снят.
