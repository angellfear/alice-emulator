# Журнал → пробелы обработчиков (2026-11-01 12:46, журнал: traffic.sqlite3)

## HTTP

| Запрос | Источник | Без обработчика | Записей | Ответ (образец) |
|---|---|---|---|---|
| POST quasar.yandex.net/push_subscribe | emulator | да (10) | 10 | 200 application/json 75Б; поля: sign=***, status=ok, ts=1793545598 |
| QUASAR.YANDEX.NET quasar.yandex.net/get_sync_info | upstream | — (0) | 10 | 200 application/json 42018Б; поля: status=ok, tags=[], subscription={'mode': 'subscription',…, config={'system_config': {'***'…, glagol={'security': {'server_ce…, alice_pro_subscription={'enabled': False} |
| QUASAR.YANDEX.NET quasar.yandex.net/push_subscribe | upstream | да (10) | 10 | 200  75Б; поля: sign=***, status=ok, ts=1793545592 |
| POST rpc.alice.yandex.net/gproxy/centaur_get_gallery | emulator | да (5) | 5 | 200 application/x-protobuf 41586Б |
| POST rpc.alice.yandex.net/gproxy/push_events_batch | emulator | — (0) | 4 | 400 application/x-protobuf 155Б; поля: Method=PushEventBatch, Message=Missed expected fields: …, HttpCode=400, DeviceId= |
| QUASAR.YANDEX.NET quasar.yandex.net/set_device_capability | upstream | да (2) | 2 | 429  7Б |
| POST rpc.alice.yandex.net/gproxy/get_teasers | emulator | да (1) | 1 | 404 application/json 38Б; поля: error=emulator_handler_not_fou… |

## AAPI: колонка → эмулятор

| Событие | Раз | Обработчик в реестре |
|---|---|---|
| MatchedUser | 15 | есть |
| CapabilityUpdateState | 14 | есть |
| TextInput | 10 | есть |
| RequestStat | 10 | есть |
| StreamControl | 10 | есть |
| LogSpotter | 6 | есть |
| VoiceInput | 5 | есть |
| SynchronizeStateRequest | 5 | есть |
| Ack | 2 | есть |
| CancelRequest | 1 | есть |

## AAPI: Яндекс → колонка (по ref_message_id)

| Ответ Яндекса | Раз | На что отвечает | Эмулятор шлёт |
|---|---|---|---|
| TtsTimings | 10 | VoiceInput ×10 | **нет** |
| RequestStatAck | 10 | RequestStat ×10 | да |
| InputStartAck | 10 | TextInput ×8, VoiceInput ×2 | да |
| AliceResponse | 10 | TextInput ×8, VoiceInput ×2 | да |
| AsrResult | 10 | VoiceInput ×10 | да |
| StreamControl | 9 | StreamControl ×9 | да |
| LogAck | 6 | LogSpotter ×6 | да |
| SpotterValidation | 5 | VoiceInput ×5 | да |
| SynchronizeStateResponse | 5 | SynchronizeStateRequest ×5 | да |
| TtsSpeak | 4 | VoiceInput ×4 | да |
| Push | 2 | StreamControl ×2 | да |

## Фразы и ответы Алисы

| Фраза (ASR, финальная) | Закрыто сценарием |
|---|---|
| покажи глазки | 2 | device.led.emotion (phrase:contains) |

| Директивы в ответах Алисы | Раз |
|---|---|
| show_audio_player_screen_directive,apply_player_playback_queue_directive,set_aud… | 3 |
| tts_play_placeholder | 3 |
| apply_player_playback_queue_directive,set_audio_player_sequence_state_directive,… | 2 |
| draw_animation_directive,show_audio_player_screen_directive,apply_player_playbac… | 1 |
| divui_show_view,tts_play_placeholder | 1 |
