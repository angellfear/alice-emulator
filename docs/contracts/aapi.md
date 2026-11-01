---
title: "Контракт AAPI (Uniproxy WS)"
status: stable
date: "2026-10-30"
module: "server_emulator/voice.py"
---

# Контракт AAPI (Uniproxy WS)

Источник истины: восстановленный descriptor `schemas/alice-api.pb` (sha256 фиксируется в каждом fixture), эталонные обмены `tests/fixtures/emulator/`, реализация `server_emulator/runtime.py`, `voice.py`, `legacy_replay.py`. Тесты: `tests/test_voice_pipeline.py`, `tests/test_emulator_and_pcm.py`, `tests/test_emulator_fixtures.py`, `tests/test_emulator_identity.py`.

## 1. Фрейминг

| Кадр | Формат | Evidence |
|---|---|---|
| Управляющий | `b"AAPI"` + сериализованный protobuf `TClientMessage` / `TServerMessage` | все fixtures; runtime.py:370–373 |
| Аудио канала | 4 байта big-endian ID канала + payload (Ogg/Opus-страницы или PCM по `Format` запроса) | voice-clock (вх. канал 1), volume-set (вых. канал 102); voice.py:93 receive_audio |

Каналы: входной объявляется в `VoiceInput.Header.StreamId`; исходящий TTS-канал сервер выбирает сам. В реальном трафике наблюдался вых. канал 102 (volume-set) и закрытие вх. канала 1 сервером (voice-clock); эмулятор генерирует чётные каналы с 2 (voice.py:57). Политика выделения каналов сервером не подтверждена — оба варианта валидны для клиентов.

Ограничения эмулятора на входное аудио: ≤90 с на канал, суммарно ≤16 MiB, ≤4 одновременных канала (voice.py:100,132).

## 2. Закрытие потока — enum `CloseReason` (из descriptor, не магические числа)

| Значение | Имя | Использование |
|---|---|---|
| 0 | OK | нормальное завершение TTS-канала (voice.py close_stream по умолчанию) |
| 1 | INTERNAL_ERROR | превышение лимитов входа, ошибка STT (voice.py:102,219) |
| 2 | STOP_RECORDING | не используется эмулятором; наблюдается от клиента |
| 3 | AUDIO_SOURCE_STOPPED | не используется эмулятором |
| 4 | CANCEL | отмена/новый запрос вытесняет старый (voice.py:85,110) |
| 5 | NO_SPEECH | вход без аудио игнорируется (voice.py:145) |

`StreamControl` несёт `StreamId`, `Close.Reason`, `SpotterEnd`, `LazyTTS` (descriptor).

## 3. События клиента (TClientMessage.Event, 25 по descriptor)

| Событие | Эмулятор | Fixture | Тест |
|---|---|---|---|
| SynchronizeStateRequest | идентификация DeviceId (смена в соединении → close 1008); ответ SynchronizeStateResponse | sync-basic, text-basic, logspotter-ack | test_emulator_identity, test_emulator_and_pcm |
| TextInput | при `aapi_model`: InputStartAck → модель → AliceResponse; иначе игнор | text-basic, volume-set | test_voice_pipeline |
| VoiceInput | при `aapi_voice`: InputStartAck + открытие канала; иначе игнор | voice-clock, screen-divui | test_voice_pipeline |
| StreamControl.Close | завершение фразы → ASR→LLM→TTS конвейер | voice-clock | test_voice_pipeline |
| CancelRequest | отмена задач и каналов (close CANCEL); ответ после отмены не подавается эмулятором как новый | cancel-request | test_voice_pipeline (cancel) |
| Ack | `delivery=acknowledged` в action_log | volume-set, text-basic | test_quasar_backend |
| LogSpotter | ответ LogAck с RefMessageId | logspotter-ack | replay-тест; отдельного unit-теста нет |
| RequestStat | ответ RequestStatAck | cancel-request | test_emulator_and_pcm |
| MatchedUser | биометрия говорящего (TASK-069): классификация голоса (`Request.Event.BiometryClassification`: Scores ClassName/Confidence/Tag, Simple) → `conversation.speaker` и `sessions[<соединение>]['speaker']`; в контексте сценариев — `speaker`, агенту — kwargs `speaker`. Облако не отвечает — эмулятор тоже | voice-clock (MatchedUser) | test_aapi_handlers |
| CapabilityUpdateState | состояние возможностей колонки (TASK-069): `CapabilityType` (enum, напр. AccessibilityVoiceControlType) + `AnyCapability.RawCapability` → последнее состояние каждого типа в `sessions[…]['capabilities']` (память, без БД) и `conversation.capabilities`; в контексте сценариев — `device.capabilities` (preview читаемых строк дампа). Облако не отвечает | voice-clock (CapabilityUpdateState) | test_aapi_handlers |
| ContinueRequest | колонка просит продолжить ответ (`RequestMessageId`). Поведение облака не разобрано (образца нет): ответа нет, событие — в журнал сервера (source ws, предупреждение склеивается); полное поведение — БЛОКЕР задания 069 | — | test_aapi_handlers |
| SpotterEnd / Ping | наблюдаются, обработчиков нет — игнорируются без ошибки | voice-clock | replay-тест держит соединение |
| MusicInput, SyncPushResponse, AsrRecognize, TtsGenerate, DeferredTtsGenerate, DataProxyInput, FileUploadRequest, TtsUpdateState, RecordInput, ClientSubscriptionState, TranslateInput, AudioTranscriptionInputRequest, FinishRequest | не наблюдались в captures; обработчиков нет | — | — |

## 4. События сервера (TServerMessage.Event, 25 по descriptor)

| Событие | Кто шлёт | Fixture | Эмулятор генерирует |
|---|---|---|---|
| InputStartAck | на TextInput/VoiceInput | text-basic, voice-clock | да (voice.py:119,135) |
| AsrResult | частичные и финальное распознавание | voice-clock (26 partial + final) | да (voice.py:169) |
| AliceResponse | ответ с Cards/Directives | text-basic, volume-set, voice-clock, screen-divui | да (simple_text + OutputSpeech) |
| DeferredAliceResponse | отложенный ответ | screen-divui ×2 | нет (не реализовано) |
| TtsSpeak | объявление TTS-канала (`Format=audio/opus`) | volume-set, screen-divui | да (voice.py:206) |
| TtsTimings | метрики TTS | volume-set ×3, screen-divui | нет |
| Push | директивы вне диалога | text-basic, cancel-request (notify) | да (actions.py push_message) |
| GoAway | сервер требует переподключение | reconnect-goaway | нет |
| SynchronizeStateResponse / RequestStatAck | ответы на запросы | sync-basic, cancel-request | да (legacy_replay.control_response) |
| LogAck | подтверждение LogSpotter | logspotter-ack | да (runtime.py:396) |
| SpotterValidation | валидация фразы активации | screen-divui (наблюдение) | нет |
| EventException, InvalidAuth, ServerRequestReady, SyncPush, Ping, AsrMusicResult, DataProxyRequest, FileUploadResponse, SpeechControl, ServerSubscriptionState, AudioTranscriptionInputResponse, ChunkAck | не наблюдались/не генерируются | — | — |

## 5. Ключевые структуры (поля из descriptor)

- `Header`: MessageId, RefMessageId, StreamId, Ack, RtLogId, SubRequestId, RetryNumber, SubRequestRef. Ответ всегда несёт RefMessageId = MessageId запроса.
- `TAliceRequestProto` (TextInput/VoiceInput): Header (RequestId, PrevReqId, SequenceNumber, DialogId…), Application, Request (Event.Text для TextInput; Format/ResetSession/VoiceSession…), Firmware, DialogHistoryBlock, Alice2Settings…
- `TAliceResponseProto`: Header (RequestId, ResponseId, DialogId), VoiceResponse.OutputSpeech.Text, Response.Cards[] (Type simple_text + Text + CardId), Response.Directives[] (Name, Type, Payload(Struct), PayloadRaw(bytes)).
- `TPush`: PushIds[], DeduplicationPushId, Directives[], AnalyticsMetaInfo.
- `TAsrResultProto`: ResponseCode, MessagesCount, EndOfUtt, Recognition[] (Normalized, Confidence, Words), AggregatedRecognition.

## 6. Учетные данные в протоколе

`SynchronizeStateRequest` может нести OAuthToken, ApiKey, ICookie, YexpCookie, SaeCookie, MegamindCookies. В fixtures эти поля закрываются `<redacted credential N chars>`; живые значения запрещены (проверка в tests/test_emulator_fixtures.py). Эмулятор их не проверяет и не хранит.

## 7. Тайминги/лимиты эмулятора

WS heartbeat 30 с; max_msg_size 16 MiB; провайдеры model/stt/tts — HTTP total 90 с; MCP 30 с; лимиты аудио из §1. Firmware/capability-гейт ещё не реализован (B05/E01) — статус в coverage.md.

## Обновление 2026-11-01: реестр обработчиков и колбэки (TASK-058)

Реестр — `aapi_handlers.py`: `@aapi_handler(trigger, enabled=…)` объявляет обработчик рядом
с кодом; у записи есть источник (`builtin`, у расширений через SDK — `extension:<id>`),
имя/описание (docstring) и счётчики в памяти (пришло / обработано / время последнего).
`Conversation.receive` перебирает обработчики события; `has_handler` отвечает без выполнения
(опция «Запись» → «Нет обработчиков»). API — `GET /emulator/api/ws_handlers` (реестр + колбэки),
вкладка «Эмулятор → WS → Входящие».

Служебные колбэки (`@@mm_typed_callback`, `@@mm_semantic_frame`, `@@timer_fired`, …) не
исполняют сценарий по `kind: event` напрямую: маршрут (`aapi_handlers.route` — имя события
и фильтры `callback`/`purpose`/`frame`) отображает их во внутреннее событие-запрос данных
(`informers.requested`, `timesync.requested`, `weather.opened`, `agent_say.requested`,
`timer_fired.requested`, прочие — `callbacks.ack`). Ответ колонке собирает сценарий-поставщик
(триггер `kind: provide`, см. scenario-json-v2.md); пользовательский поставщик перекрывает
встроенный, поставщика нет — умолчание колбэка (тихое подтверждение, `Dialog.quiet_ack`).
Колбэк без маршрута и сценария — в `callback_misses` (блок «AAPI без ответа» во вкладке WS).

## Обновление 2026-10-24: карточка и NO_SPEECH

- Карточка диалога рендерится колонкой при заполненном каркасе (см. docs/contracts/scenarios.md §6): CardId (новый идентификатор на карточку), Header.SequenceNumber, DialogId="", директива tts_play_placeholder{channel:"Dialog"} — восстановлено из реальных ответов (fixtures voice-clock/volume-set).
- AsrResult: текст экрана колонки — AggregatedRecognition.RecognizedText (Recognition[].Normalized недостаточно); пустые паршлы не отправляются.
- NO_SPEECH-протокол: см. scenarios.md §7.

## Обновление 2026-10-28: ответ частями (DeferredAliceResponse)

- Перехват screen-divui: облако шлёт `AliceResponse` без текста (`HasVoiceResponse`,
  `tts_play_placeholder`), затем `DeferredAliceResponse {BaseResponse{Text, ShouldListen},
  ResponsePartialNum, IsLast, RequestId}` и только потом `TtsSpeak` + аудио. Эмулятор использует
  этот порядок для озвучки ответа модели по фразам (`agent.stream_reply`, docs/contracts/agent.md).
  Поведение колонки при многих частях и звуке до `IsLast` на живом устройстве не проверено.

## Обновление 2026-10-28: сбои конвейера видны пользователю

- Сбой распознавания, сценария или модели (HTTP-ошибка провайдера, таймаут, нет связи, не
  настроен): входной канал закрывается `INTERNAL_ERROR`, затем уходит обычный `AliceResponse` —
  карточка `simple_text` «Не получилось …: <причина>. Подробности — в журнале эмулятора.» и
  голос, если TTS работает (`voice.failure_text`, трасса `failure_reply`). Без этого колонка
  играла свою стандартную фразу и не говорила, что сломалось.
- Сбой TTS: ответ уходит без голоса, а к карточке ответа добавляется вторая карточка
  «Озвучка недоступна: <причина>».

## Обновление 2026-10-28: колонка не уходит в свой таймаут

- Колонка 84 (logcat `quark-native`): без ответа ~17 с после конца речи она играет свою
  фразу о таймауте (`local_audio_file_play`), даже если ответ сервера придёт позже.
- Синтез ждёт первый звук не дольше `tts_wait_s` (настройка верхнего уровня, по умолчанию 6 с):
  дольше — ответ уходит карточкой без голоса и «Озвучка недоступна: сервис не ответил вовремя».
- В потоковом режиме (`agent.stream_reply`) ранний `AliceResponse` уходит сразу, как запрос
  ушёл в модель, а не с первой фразой. Сбой модели после него — последний
  `DeferredAliceResponse` (`IsLast`) с причиной и голосом, второго `AliceResponse` нет. После
  первого сбоя синтеза остальные фразы не синтезируются, причина — второй картой в `IsLast`.
- Карточка ответа на колонке — потоковая (`centaur_streamed_alice_answer`), текст с
  `ProgressivePrinting` печатается по ходу звука: при сбое модели и TTS сразу карточка вышла
  пустой. Поэтому `TtsSpeak` уходит только с первым звуком (без звука канала нет), а после
  сбоя синтеза карточки идут без `ProgressivePrinting` — текст целиком. Трасса
  `deferred_response` (part, last, text_len, progressive).
- Нет звука дольше `agent.filler_after_s` (по умолчанию 4 с, 0 — выкл., вкладка «AI агент»)
  после раннего ответа — канал открывается и в него идёт белый шум ≈ −54 dBFS шагами 0,1 с,
  пока синтез не даст звук; тот же Opus-поток продолжается речью. Шум подаётся только когда
  колонка доиграла уже отданное (паузы между фразами тоже), готовую речь не сдвигает.
- Фразы «пока думаю» (`agent.think_phrases`, выкл. по умолчанию; `think_every_s` — 5 с;
  `think_words` — список, в UI через запятую): во время шума раз в `think_every_s` вместо
  шума звучит случайная фраза. Синтез — в фоне; пока фраза не готова, идёт шум. В текст
  карточки фразы не попадают.
- Звуковой кеш, раздел «AI агент» (`tts_cache.agent_phrases`, source `agent`): фразы «пока
  думаю» и озвучиваемые фразы сбоев — все сочетания этапа (распознавание, модель, команда,
  запрос) и причины (`voice.failure_phrases`). Голос сбоя — без кода HTTP
  (`failure_speech`), карточка — с кодом (`failure_text`). При PCM-движке эти фразы (и прочие
  фразы кеша) и в потоке берутся из кеша (`reply_stream.phrase_pcm`); синтез из кеша тоже
  ограничен `tts_wait_s`.
- Ключ прошивки `vinsRequestTimeout` (рядом с `sendCancelAfterLastDeferred`) не выставляется:
  единица и место в конфиге не подтверждены.

- Колонка ждёт `TtsSpeak` после раннего ответа не дольше ~10 с (logcat: `tts_play_placeholder`
  → `onTtsPlayerError` через 10 с, затем IDLE) — шум через 4 с укладывается. Когда канал держит
  шум, синтез фразы ждётся до `OPEN_TTS_WAIT_S` (20 с), а не `tts_wait_s`. Фразы «пока думаю»
  включены по умолчанию (один шум ≈ −54 dBFS не слышно). Отменённый колонкой запрос пишется в
  журнал агента со статусом `cancelled`.

## Обновление 2026-10-28: отложенный ответ модели через Push

- Перехват volume-set: облако доставляет сработавшее напоминание сообщением `Push`
  (`Directives: [{Type: server_action, Name: @@mm_semantic_frame, Payload: {analytics:
  {purpose: shoot_reminder, origin_info}, typed_semantic_frame: {...}}}]`); колонка отвечает
  `Ack` и присылает `TextInput` с `Request.Event {Type: server_action, Name, Payload}` — облако
  отвечает обычным `AliceResponse` + `TtsSpeak` и звуком.
- Эмулятор (`async_reply.py`, `agent.async_reply_s`, по умолчанию 3 с, 0 — выкл.): модель не
  ответила за это время — на запрос сразу уходит обычный ответ `agent.async_wait_text`
  («Секунду, думаю.», голос из звукового кеша), модель дорабатывает в фоне. Готовый ответ (или
  причина сбоя) хранится до 10 мин, колонке уходит `Push` той же формы с `purpose:
  agent_answer` и id ответа в `analytics.origin_info`. На её `TextInput` с этим server_action
  ответ модели уходит как обычный (карточка, голос, действия, директивы); id не найден —
  «Ответ уже недоступен.». При включённом отложенном ответе озвучка по фразам не используется.
- Трасса: `async_wait`, `async_wait_reply`, `async_push` / `async_push_failed`, `async_answer`.
- Принимает ли колонка `Push` со своим purpose (а не shoot_reminder) — на живом устройстве не
  проверено.
- Фразы «пока думаю» по умолчанию — короткие фразы, а не слова («Секунду, думаю.», «Хм, дайте
  подумать.» …): синтез с клонированием голоса на одно слово отдаёт почти тишину, и в шуме был
  провал ~1 с. Результат синтеза тише `AUDIBLE_RMS` (0,01) не вставляется (трасса `think_silent`).
- Проверено на колонке 84: Push со своим purpose принят — колонка присылает TextInput с
  server_action, ответ звучит. Карточка `simple_text` при этом не видна: экран колонка
  открывает сама только при голосовой активации. Поэтому ответ по Push несёт ещё
  `divui_show_view` (`async_reply.screen_card`, `TDivUIShowViewDirective`, слой Dialog) с
  простой Div2-карточкой текста — как облако в ответе на напоминание.
- Звук отложенного ответа синтезируется до Push (`Conversation._prerender`, ожидание до
  `OPEN_TTS_WAIT_S`): на запрос колонки ответ уходит сразу. Не вышло — синтез по запросу, как
  обычно. Фразы раздела «AI агент» звукового кеша досинтезируются в фоне при старте (через
  20 с, после autostart TTS) и при сохранении настроек (`TtsCache.warm`, только недостающие).
- Проверено на колонке 84 (2026-10-30, задание «по расписанию» → Push): колонка выполнила
  `alice_request.reminders_on_shoot_semantic_frame`, прислала TextInput, ответ с
  `divui_show_view` показан (`StackedLayerManager show agent_answer`, снимок экрана — текст
  ответа на тёмном фоне), голос прозвучал (`onAliceStateChanged: SPEAKING`). Карточка не
  закрывается сразу после речи.

## Обновление 2026-10-30: озвучка по фразам убрана

- Ранний `AliceResponse`, `DeferredAliceResponse` частями, шум и фразы «пока думаю»
  (`agent.stream_reply`, `filler_after_s`, `think_*`, `reply_stream.py`) убраны: медленную
  модель закрывает отложенный ответ через Push. Разделы выше про них — история.
- Ответ модели (обычный и по Push) ставит `VoiceResponse.ShouldListen`, если ждёт
  подтверждения действия (`tool_calls`) или заканчивается «?».
- Реплики заданий (`delivery: speak`) тоже синтезируются до Push (`Conversation._prerender`).
- Карточка `agent_answer` без прокрутки: длинный ответ (≈800 символов, 12 предложений) обрезан
  сверху и снизу, свайп не прокручивает (колонка .84, 2026-10-30). Пока так; задел — TODO в
  `async_reply.screen_card` (прокручиваемый вертикальный gallery).
- Проверено на колонке .84 (2026-10-30): после ответа по Push с вопросом колонка сама переходит
  в `LISTENING` (без «Алиса»), без речи закрывает микрофон через ~5 с (`NoSpeechDetectedError`).
- Реплика задания попадает и в голосовой тред `voice:<SN>` (agent.md, «Треды»): ответ на вопрос
  задания понятен агенту.

## Обновление 2026-11-01: биометрия и возможности колонки (TASK-069)

Промахи рабочего сервера (`MatchedUser` 91, `CapabilityUpdateState` 49, `ContinueRequest` 1) получили
встроенные обработчики в `voice.py` (реестр `aapi_handlers`, видны в «WS → Входящие»):

- **MatchedUser** — колонка сообщает классификацию голоса к текущему запросу
  (`Request.Event.BiometryClassification`: оценки `Scores` {ClassName → Confidence} по тегам
  children/gender, упрощённо `Simple` {Tag → ClassName}). Сохраняется: `conversation.speaker`,
  `runtime.sessions[<соединение>]['speaker']`. Сценариям — `speaker` в контексте
  (`speaker.scores.adult`, `speaker.simple.gender`), агенту — kwargs `speaker`. Ответа нет:
  в образце журнала («Прозрачно», C00T10001B6SAK, обмены 108–112) облако на событие не отвечает,
  первый ответ (`InputStartAck`) ссылается на VoiceInput.
- **CapabilityUpdateState** — состояние возможностей колонки: `CapabilityType` (enum
  `NAlice.TCapability.ECapabilityType`) и сырой дамп `AnyCapability.RawCapability` (protobuf без
  схемы — например дерево доступности экрана с именами приложения и текстами). Последнее состояние
  каждого типа — в `sessions[…]['capabilities']` (только память: ts, размер, preview читаемых строк;
  БД на каждое событие не пишется) и `conversation.capabilities`; сценариям —
  `device.capabilities[<тип>]` (preview), агенту — сессия устройства. Ответа нет (образец тот же).
- **ContinueRequest** — единственное поле `RequestMessageId`; образца ответа облака в журнале нет
  (вытеснен ротацией). Минимальное поведение: ответа нет, событие считается обработанным (не промах),
  предупреждение в журнал сервера («Дополнительно → Лог», source ws, повторы склеиваются).
  Полное поведение — БЛОКЕР: нужен обмен (устройство в «Прозрачно» с «Записью», воспроизвести
  продолжение долгого ответа).

## Обновление 2026-11-01: кнопки экрана плеера (TASK-072)

Колбэки `@@mm_typed_callback` с `purpose=player_*` (кнопки экрана плеера: next/previous/pause/
play/liked/unliked/dislike/repeat/shuffle/speed) маршрутизируются на сценарий-поставщик
`media.player_button` (scenarios/media/buttons.json): next/previous двигают очередь эмулятора
(TASK-071), pause/play — `audio_play` с `set_pause`, лайки/повтор/шаффл/скорость — тихое
подтверждение (без аккаунта Яндекса состояние кнопки показывает колонка).
