---
title: "Контракт AAPI (Uniproxy WS)"
status: stable
date: "2026-11-17"
module: "server_emulator/voice.py"
---

# Контракт AAPI (Uniproxy WS)

Источник истины: восстановленный descriptor `schemas/alice-api.pb` (sha256 фиксируется в каждом fixture), эталонные обмены `tests/fixtures/emulator/`, реализация `server_emulator/runtime.py`, `voice.py`, `legacy_replay.py`. Тесты: `tests/test_voice_pipeline.py`, `tests/test_emulator_and_pcm.py`, `tests/test_emulator_fixtures.py`, `tests/test_emulator_identity.py`.

## 1. Фрейминг

| Кадр | Формат | Evidence |
|---|---|---|
| Управляющий | `b"AAPI"` + сериализованный protobuf `TClientMessage` / `TServerMessage` | все fixtures; runtime.py |
| Аудио канала | 4 байта big-endian ID канала + payload (Ogg/Opus-страницы или PCM по `Format` запроса) | voice-clock (вх. канал 1), volume-set (вых. канал 102); voice.py receive_audio |

Каналы: входной объявляется в `VoiceInput.Header.StreamId`; исходящий TTS-канал сервер выбирает сам. В реальном трафике наблюдался вых. канал 102 (volume-set) и закрытие вх. канала 1 сервером (voice-clock); эмулятор генерирует чётные каналы с 2 (voice.py). Политика выделения каналов сервером не подтверждена — оба варианта валидны для клиентов.

Ограничения эмулятора на входное аудио: ≤90 с на канал, суммарно ≤16 MiB, ≤4 одновременных канала (voice.py).

## 1.1 QuasarMessage — конверт другой шины, в этом канале не участвует

Кадры AAPI — `TClientMessage`/`TServerMessage`; в alice-api.pb нет ни одной ссылки на
quasar_proto. `QuasarMessage` (`yandex_io/protos/quasar_proto.proto`) — конверт внутренней шины
прошивки (демон ↔ сервисы станции), на эмулируемый провод он не выходит. Формат зависит от
прошивки: в части версий сообщение каждого типа лежит в своём поле (`<type_id>: <inner_bytes>`),
в других — общий конверт `payload` (поле 252) + `type` (поле 253, enum `QuasarMessage.Type`, ~142
значения). В комплектном дампе станции (`schemas/centaur-*.pb`) уже вторая форма. Эмулятор
QuasarMessage не кодирует и не декодирует — ни один его путь от формата конверта не зависит,
поэтому смена прошивки колонки ничего здесь не ломает. Если когда-нибудь понадобится разбирать
устройствные дампы (IPC, метрика), конверт брать из схемы самой станции, поддержав обе формы;
номера полей в коде не фиксировать.

## 2. Закрытие потока — enum `CloseReason` (из descriptor, не магические числа)

| Значение | Имя | Использование |
|---|---|---|
| 0 | OK | нормальное завершение TTS-канала (voice.py close_stream по умолчанию) |
| 1 | INTERNAL_ERROR | превышение лимитов входа, ошибка STT (voice.py) |
| 2 | STOP_RECORDING | не используется эмулятором; наблюдается от клиента |
| 3 | AUDIO_SOURCE_STOPPED | не используется эмулятором |
| 4 | CANCEL | отмена/новый запрос вытесняет старый (voice.py) |
| 5 | NO_SPEECH | без аудио — пустой финал и реплика; со звуком и без потокового ASR — буфер распознаётся как реплика (voice.py) |

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
| MatchedUser | биометрия говорящего: классификация голоса (`Request.Event.BiometryClassification`: Scores ClassName/Confidence/Tag, Simple) → `conversation.speaker` и `sessions[<соединение>]['speaker']`; в контексте сценариев — `speaker`, агенту — kwargs `speaker`. Облако не отвечает — эмулятор тоже | voice-clock (MatchedUser) | test_aapi_handlers |
| CapabilityUpdateState | состояние возможностей колонки: `CapabilityType` (enum, напр. AccessibilityVoiceControlType) + `AnyCapability.RawCapability` → последнее состояние каждого типа в `sessions[…]['capabilities']` (память, без БД) и `conversation.capabilities`; в контексте сценариев — `device.capabilities` (preview читаемых строк дампа). Облако не отвечает | voice-clock (CapabilityUpdateState) | test_aapi_handlers |
| ContinueRequest | колонка просит продолжить ответ (`RequestMessageId`). Поведение облака не разобрано (образца нет): ответа нет, событие — в журнал сервера (source ws, предупреждение склеивается); полное поведение — БЛОКЕР | — | test_aapi_handlers |
| SpotterEnd | валидация слова-активации: ответ `SpotterValidation` (Result=1) при `VoiceInput.EnableSpotterValidation`; на подачу аудио не влияет — кадры канала идут непрерывно с VoiceInput и до SpotterEnd тоже (в образце voice-clock первые 46 кадров, ~3,8 КБ, приходят раньше него) и всё доходят до ASR; слово-активация снимается из распознанного текста (`scenario_engine.normalize.strip_wake_prefix`) — как у облака | voice-clock | test_online_voice (спотер) |
| Ping | наблюдаются, обработчиков нет — игнорируются без ошибки | voice-clock | replay-тест держит соединение |
| MusicInput, AsrRecognize, TtsGenerate, DeferredTtsGenerate, DataProxyInput, FileUploadRequest, TtsUpdateState, RecordInput, ClientSubscriptionState, TranslateInput, AudioTranscriptionInputRequest, FinishRequest | не наблюдались в captures; обработчиков нет | — | — |
| SyncPushResponse | ответ на НАШ SyncPush{GetDeviceState} — ожидающему `runtime.fetch_state` по сессии (ветка oneof `Response` == GetDeviceStateResponse; квитанции ExecuteDirective* ожидающего не будят); событие обработано всегда — не промах; в «Вместе» наверх не релеится | — | test_fetch_state, test_ws_hybrid |

## 4. События сервера (TServerMessage.Event, 25 по descriptor)

| Событие | Кто шлёт | Fixture | Эмулятор генерирует |
|---|---|---|---|
| InputStartAck | на TextInput/VoiceInput | text-basic, voice-clock | да (voice.py) |
| AsrResult | частичные и финальное распознавание | voice-clock (26 partial + final) | да (voice.py) |
| AliceResponse | ответ с Cards/Directives | text-basic, volume-set, voice-clock, screen-divui | да (simple_text + OutputSpeech) |
| DeferredAliceResponse | отложенный ответ | screen-divui ×2 | нет (не реализовано) |
| TtsSpeak | объявление TTS-канала (`Format=audio/opus`) | volume-set, screen-divui | да (voice.py) |
| TtsTimings | метрики TTS | volume-set ×3, screen-divui | нет |
| Push | директивы вне диалога | text-basic, cancel-request (notify) | да (actions.py push_message) |
| GoAway | сервер требует переподключение | reconnect-goaway | нет |
| SynchronizeStateResponse / RequestStatAck | ответы на запросы | sync-basic, cancel-request | да (legacy_replay.control_response) |
| SyncPush | опрос состояния колонки (fetch_state): `SyncRequest{GetDeviceState}` — см. uniproxy.md §9 | — | да (runtime.sync_push_message → runtime.fetch_state) |
| LogAck | подтверждение LogSpotter | logspotter-ack | да (runtime.py) |
| SpotterValidation | валидация фразы активации | screen-divui (наблюдение) | да (voice.py, при EnableSpotterValidation) |
| EventException, InvalidAuth, ServerRequestReady, Ping, AsrMusicResult, DataProxyRequest, FileUploadResponse, SpeechControl, ServerSubscriptionState, AudioTranscriptionInputResponse, ChunkAck | не наблюдались/не генерируются | — | — |

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

## Реестр обработчиков и колбэки

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

## Карточка и NO_SPEECH

- Карточка диалога рендерится колонкой при заполненном каркасе (см. docs/contracts/scenarios.md §6): CardId (новый идентификатор на карточку), Header.SequenceNumber, DialogId="", директива tts_play_placeholder{channel:"Dialog"} — восстановлено из реальных ответов (fixtures voice-clock/volume-set).
- AsrResult: текст экрана колонки — AggregatedRecognition.RecognizedText (Recognition[].Normalized недостаточно); пустые паршлы не отправляются.
- NO_SPEECH-протокол: см. scenarios.md §7.

## Ответ частями (DeferredAliceResponse)

- Перехват screen-divui: облако шлёт `AliceResponse` без текста (`HasVoiceResponse`,
  `tts_play_placeholder`), затем `DeferredAliceResponse {BaseResponse{Text, ShouldListen},
  ResponsePartialNum, IsLast, RequestId}` и только потом `TtsSpeak` + аудио. Эмулятор этот
  порядок не воспроизводит — отложенный ответ доставляет `Push` (см. ниже).
  Поведение колонки при многих частях и звуке до `IsLast` на живом устройстве не проверено.

## Сбои конвейера видны пользователю

- Сбой распознавания, сценария или модели (HTTP-ошибка провайдера, таймаут, нет связи, не
  настроен): входной канал закрывается `INTERNAL_ERROR`, затем уходит обычный `AliceResponse` —
  карточка `simple_text` «Не получилось …: <причина>. Подробности — в журнале эмулятора.» и
  голос, если TTS работает (`voice.failure_text`, трасса `failure_reply`). Без этого колонка
  играла свою стандартную фразу и не говорила, что сломалось.
- Обрыв потокового распознавания посреди фразы (сервер ASR умер или соединение с ним
  оборвалось) — тот же путь этапа `asr`: канал снимается, задача распознавания отдаётся
  `run()`, и её ошибка уходит в обычную реплику сбоя (`voice._asr_failed`). Исключение из
  `receive_audio` обработчик сообщения колонки не поднимает: оно закрыло бы WS колонки кодом
  1011, и реплика осталась бы без ответа. В журнал сервера (source `ws`) — «Потоковое
  распознавание прервалось: …».
- Сбой TTS: ответ уходит без голоса, а к карточке ответа добавляется вторая карточка
  «Озвучка недоступна: <причина>». Причина пишется и в журнал сервера (source `ws`,
  «Озвучка не удалась: …», `voice_response.report_tts_failure`) — без этой строки сбой синтеза
  виден только в трассе и `runtime.recent`, а молчание колонки объяснить нечем.

## Колонка не уходит в свой таймаут

- Колонка 84 (logcat `quark-native`): без ответа ~17 с после конца речи она играет свою
  фразу о таймауте (`local_audio_file_play`), даже если ответ сервера придёт позже.
- Синтез ждёт первый звук не дольше `tts_wait_s` (настройка верхнего уровня, по умолчанию 6 с):
  дольше — ответ уходит карточкой без голоса и «Озвучка недоступна: сервис не ответил вовремя».
  Локальный TTS-сервер (`tools/speech_common.tts_app`) прогревается одним синтезом до открытия
  порта: холодный движок на CUDA тратит на первый запрос секунды, и без прогрева первая
  несохранённая в кеше фраза после запуска упиралась в это ожидание.
- Ход измеряется по этапам (`SpeechTrace.timing`): пауза до конца фразы (`asr_partial` →
  `endpoint`), финал ASR, сценарий (`scenario_start` → `scenario_done`), модель (`model_start` →
  `model_done`), синтез (`tts_start` → `tts_done`), отправка звука (`tts_done` → `tts_audio`) и
  `reply` — от конца фразы до отправки ответа. Итог — событие `turn_timing` в трассе
  (`api/speech`, поля `<этап>_ms`, `slowest` — самый долгий этап работы сервера) и строка
  журнала сервера (source `ws`): `info` «Ответ через N с: …», `warning` «Медленный ответ…» при
  `reply` от 3 с (`speech_trace.SLOW_REPLY_MS`). Служебные колбэки без сценария, модели и
  озвучки в журнал не попадают.
- Карточка ответа на колонке — потоковая (`centaur_streamed_alice_answer`), текст с
  `ProgressivePrinting` печатается по ходу звука: при сбое модели и TTS сразу карточка выходит
  пустой. Поэтому `TtsSpeak` уходит только с первым звуком (без звука канала нет), а после
  сбоя синтеза карточки идут без `ProgressivePrinting` — текст целиком.
- Звуковой кеш, раздел «AI агент» (`tts_cache.agent_phrases`, source `agent`): озвучиваемые
  фразы сбоев — все сочетания этапа (распознавание, модель, команда, запрос) и причины
  (`voice.failure_phrases`). Голос сбоя — без кода HTTP (`failure_speech`), карточка —
  с кодом (`failure_text`).
- Ключ прошивки `vinsRequestTimeout` (рядом с `sendCancelAfterLastDeferred`) не выставляется:
  единица и место в конфиге не подтверждены.

- Колонка ждёт `TtsSpeak` после раннего ответа не дольше ~10 с (logcat: `tts_play_placeholder`
  → `onTtsPlayerError` через 10 с, затем IDLE) — синтез до Push (`OPEN_TTS_WAIT_S`, 20 с)
  укладывается в это окно. Отменённый колонкой запрос пишется в
  журнал агента со статусом `cancelled`.

## Отложенный ответ модели через Push

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
  «Ответ уже недоступен.».
- Трасса: `async_wait`, `async_wait_reply`, `async_push` / `async_push_failed`, `async_answer`.
- Проверено на колонке 84: Push со своим purpose принят — колонка присылает TextInput с
  server_action, ответ звучит. Карточка `simple_text` при этом не видна: экран колонка
  открывает сама только при голосовой активации. Поэтому ответ по Push несёт ещё
  `divui_show_view` (`async_reply.screen_card`, `TDivUIShowViewDirective`, слой Dialog) с
  карточкой ответа (шаблон `agent_answer`, ниже) — как облако в ответе на напоминание.
- Звук отложенного ответа синтезируется до Push (`Conversation._prerender`, ожидание до
  `OPEN_TTS_WAIT_S`): на запрос колонки ответ уходит сразу. Не вышло — синтез по запросу, как
  обычно. Фразы раздела «AI агент» звукового кеша досинтезируются в фоне при старте (через
  20 с, после autostart TTS) и при сохранении настроек (`TtsCache.warm`, только недостающие).
- Проверено на колонке 84 (2026-10-30, задание «по расписанию» → Push): колонка выполнила
  `alice_request.reminders_on_shoot_semantic_frame`, прислала TextInput, ответ с
  `divui_show_view` показан (`StackedLayerManager show agent_answer`, снимок экрана — текст
  ответа на тёмном фоне), голос прозвучал (`onAliceStateChanged: SPEAKING`). Карточка не
  закрывается сразу после речи.

## Отложенный ответ: продолжение сессии (continue_session)

Второй вариант доставки (`settings["agent"]["defer_mode"]`, по умолчанию `push`, читается
живьём; механика — quasar-server input-handler.ts, решение владельца 2026-11-07):

- Модель не ответила за `agent.async_reply_s` — вместо короткого ответа и Push колонке уходит
  `AliceResponse` с одной директивой `tts_play_placeholder {channel: Dialog}` и `OnFinish`
  (поле 14 `TDirective`, `TOnFinishEvent.TypedCallbackRequest=1` — `google.protobuf.Struct`)
  `{typed_semantic_frame: {continue_session_stage1_semantic_frame: {}}}`. Речи и карточки нет.
- Колонка после проигрывания placeholder сама присылает `TextInput @@mm_semantic_frame` с
  `typed_semantic_frame.continue_session_stage2_semantic_frame.dialog_id` — сервер отдаёт
  продолжение: готовый ответ модели обычным `AliceResponse` с голосом и карточкой.
- Ответ модели ждёт stage2 не дольше `agent.async_reply_s` (ключ — DialogId запроса, в
  фрейме колонка возвращает его же); не готова или ключ устарел — «Ответ уже недоступен.»
  как у Push-варианта. Готовый ответ хранится до 10 мин (`async_reply.CONTINUES`).
- Пуш-режим не меняется: `defer_mode: push` — прежняя механика раздела выше. Неизвестное
  значение настройки — push.
- Трасса: `continue_stage1`, `continue_stage2`, `continue_ready`, `continue_answer`.
- Проверено на станции: не подтверждено (кадры по quasar-server; живое поведение
  подтверждает владелец).

## Отложенный ответ: дополнительные свойства

- Озвучки по фразам (ранний `AliceResponse`, `DeferredAliceResponse` частями, белый шум,
  фразы «пока думаю») в эмуляторе нет: медленную модель закрывает отложенный ответ через Push.
- Ответ модели (обычный и по Push) ставит `VoiceResponse.ShouldListen`, если ждёт
  подтверждения действия (`tool_calls`) или заканчивается «?».
- Реплики заданий (`delivery: speak`) тоже синтезируются до Push (`Conversation._prerender`).
- Карточка ответа — шаблон `agent_answer` (`app_cards/agent_answer.json`, раскладка экрана
  навыка): шапка «Алиса», прокручиваемый вертикальный gallery с картинкой и текстом, внизу
  кнопки-подсказки (`dialog://text_command?query=…`), крестик закрытия. Картинка — первая
  markdown-картинка ответа `![…](https://…)`, из текста она убирается; нет картинки — нет
  элемента. Кнопки «Да»/«Нет» — когда ответ ждёт подтверждения действия (`tool_calls`).
  Проверено на колонке .202: длинный текст прокручивается, картинка показывается.
- Проверено на колонке .84 (2026-10-30): после ответа по Push с вопросом колонка сама переходит
  в `LISTENING` (без «Алиса»), без речи закрывает микрофон через ~5 с (`NoSpeechDetectedError`).
- Реплика задания попадает и в голосовой тред `voice:<SN>` (agent.md, «Треды»): ответ на вопрос
  задания понятен агенту.

## Биометрия и возможности колонки

Промахи рабочего сервера (`MatchedUser` 91, `CapabilityUpdateState` 49, `ContinueRequest` 1) получили
встроенные обработчики в `voice.py` (реестр `aapi_handlers`, видны в «WS → Входящие»):

- **MatchedUser** — колонка сообщает классификацию голоса к текущему запросу
  (`Request.Event.BiometryClassification`: оценки `Scores` {ClassName → Confidence} по тегам
  children/gender, упрощённо `Simple` {Tag → ClassName} — Tag 'children' → adult/child,
  Tag 'gender' → male/female). Сохраняется: `conversation.speaker`,
  `runtime.sessions[<соединение>]['speaker']` — и оценки, и Simple, и удобные поля поверх:
  `child`/`adult` (bool из Simple.children), `gender` ('male'|'female'|None из Simple.gender).
  Сценариям — `speaker` в контексте
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

## Кнопки экрана плеера

Колбэки `@@mm_typed_callback` с `purpose=player_*` (кнопки экрана плеера: next/previous/pause/
play/liked/unliked/dislike/repeat/shuffle/speed) — тихое подтверждение (`callbacks.ack`,
как в образцах журнала); без аккаунта Яндекса состояние кнопки показывает колонка.
Тихое подтверждение — и у `music_thin_client_on_started/on_stopped` и
`@@mm_stack_engine_get_next` без поставщика (`docs/contracts/player.md`).

## Маршрут «Вместе»: промах релеится голосом

- На промахе сценария в `hybrid` ветка `upstream/hybrid.py` получает **исходный голосовой ход
  колонки**: кадр `VoiceInput` + бинарные аудиокадры (с 4-байтовым каналом-префиксом, как на
  проводе) + `StreamControl`; если колонкин `Close(STOP_RECORDING)` ещё не пришёл (эмулятор
  сам решил endpoint по VAD), в конец хода синтезируется `Close{Reason: 2}`. Кадры — как есть,
  без перекодировки; StreamId колонки для ветки — её первый поток.
- Колонке из ветки идёт только цепочка ответа: `AliceResponse` / `DeferredAliceResponse` /
  `TtsSpeak` / `TtsTimings` (по `RefMessageId` ветки) + бинарный TTS-аудио. Протокольные кадры
  ветки (`InputStartAck`, `AsrResult`, `SpotterValidation`, `StreamControl` ветки) колонке не
  пересылаются — её голосовой ход уже отработал эмулятор, чужие StreamId сломали бы канал.
- Релеи протокольных ответов колонки ветке: `StreamControl` с StreamId, который ветка сама
  упоминала, и `Ack` с MessageId кадра ветки; `RequestStat` не релеится (ранний статотчёт
  мегамайнда принимает за конец хода и отменяет отложенный ответ).
- Текстовая пересылка (TextInput из VoiceInput) удалена как мёртвая: мегамайнд не отвечает
  содержанием на текст станции (у станции нет такого потока; единственный образец
  TextInput в захватах — бардж-ин «стоп»).
