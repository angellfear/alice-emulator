# Плеер колонки: директивы и медиа-библиотека (TASK-071)

Разбор по образцу рабочего сервера (журнал 2026-11-01, обмен 155: `AliceResponse` с директивами
плеера; логкат колонки — gstreamer играет по `url=` из директивы через `souphttpsrc`).

## Директива audio_play (главная)

`AliceResponse.Response.Directives` → `Type: client_action`, `Name: audio_play` (плюс
`tts_play_placeholder` добавляет каркас диалогового слоя). Payload — Struct; минимальный набор,
при котором колонка показывает плеер и играет поток:

```json
{"stream": {"url": "http://<адрес эмулятора>/media/music/<файл>", "type": "Track", "format": "MP3",
            "duration_ms": 186580},
 "metadata": {"title": "Самый, самый", "subtitle": "Тутси",
              "art_image_url": "http://<адрес>/media/music/обложка.jpg"},
 "screen_type": "Music", "background_mode": "Ducking", "set_pause": false, "playback_speed": 1.0}
```

- `stream.type` — `Track` для файлов, `Radio` для потоков; `format` — MP3/OGG/OPUS/M4A/AAC/FLAC/WAV
  (колонка определяет формат и сама, typefind).
- `set_pause: true` той же директивой ставит воспроизведение на паузу, `false` — продолжает.
- В образце Яндекса есть ещё `apply_player_playback_queue_directive` и
  `set_audio_player_sequence_state_directive` (protobuf-очередь с провайдером
  MUSIC_PROVIDER_YANDEX_MUSIC) и `show_audio_player_screen_directive` — для своего потока они не
  нужны: очередь ведёт эмулятор (в памяти), экран открывается `screen_type` внутри audio_play.

## Очередь и колбэки

Очередь плеера — в памяти runtime (`media_state[sn]`), сценарии управляют через `ctx.media`
(sdk 1.3.1: `play_library`/`play_radio`/`start`/`step`/`pause`/`resume`/`now_playing`).

Колбэки плеера колонки (маршруты в `aapi_handlers.py`) → сценарий-поставщик `media.playback`
(scenarios/media/playback.json, `kind: provide`):

| Колбэк колонки | Внутреннее событие | Поведение поставщика |
|---|---|---|
| `music_thin_client_on_stopped` | `media.playback_ended` | следующий трек очереди (`audio_play`); очередь кончилась — тихий ответ |
| `@@mm_stack_engine_get_next` | `media.next_requested` | тот же поставщик |
| `music_thin_client_on_started/failed` | `callbacks.ack` | тихое подтверждение |

Фразы: «включи музыку [что-то]», «включи сказку [про …]», «включи радио [имя]» (сценарии
`media.music`/`media.tales`/`media.radio`), «следующий/предыдущий/продолжи/что играет»
(`media.player`), «пауза» (`device.player.pause` — audio_play с `set_pause: true`).

## Библиотека и раздача

- Файлы: `state/media/music/`, `state/media/tales/` — подпапки считаются плейлистами; название —
  имя файла (метаданные тегов не читаются — без лишних зависимостей); обложка — `<имя>.jpg/png/webp`
  или `cover.*` рядом с файлом.
- Раздача — маршрут `/media/<kind>/<путь>` (runtime.py, `web.FileResponse`): Range поддержан
  (перемотка), выход за папку закрыт; адрес колонке — хост её WS-соединения (`ctx.media_base`).
- Радио — `settings.media.radio` `[{name, url}]`, правка во вкладке «Голос → Медиа».
- Загрузка/удаление — вкладка «Голос → Медиа» (`ui/media.js`, API `ui/api/media.py`):
  POST с телом файла и `X-Filename` (до 200 МБ), DELETE по имени.
- Яндекс.Музыка через свой токен — не входит в TASK-071 (отдельное решение владельца).
