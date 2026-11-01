# Плеер колонки: директивы и медиа-библиотека

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
- `playback_speed` — скорость воспроизведения («замедли», «поставь скорость 2»); поля позиции
  (перемотка) в образце нет — «на 10 секунд вперед» честно отклоняется.
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

Фразы: «включи музыку [что-то]», «включи сказку [про …]», «включи радио [имя]», «шум дождя»
(сценарии `media.music`/`media.tales`/`media.radio`/`media.sounds`), «следующий/предыдущий/
продолжи/что играет/лайк/начни сначала/другая версия/замедли/зациклить трек» (`media.player`),
«пауза/стоп/замолчи» (`device.player.pause` — audio_play с `set_pause: true`).

## Библиотека и раздача

- Файлы: `state/media/music/`, `state/media/tales/`, `state/media/sounds/` — подпапки считаются
  плейлистами; название — тег файла (ID3v2 у mp3, Vorbis-комментарии у ogg/flac, без зависимостей),
  иначе имя файла; поиск — по тегам и путям с склонением по основе («майкла» найдёт «Майкл»);
  обложка — `<имя>.jpg/png/webp` или `cover.*` рядом с файлом.
- Режимы очереди: вся библиотека без запроса и «вперемешку» играются в случайном порядке;
  «на повторе»/«зациклить трек» — очередь из одного трека; «что-нибудь новенькое» — давно
  не игранное (история в памяти runtime); дизлайки не предлагаются.
- Избранное — `state/media/likes.json` ({пользователь: {kind: {like/dislike: [путь]}}}):
  «нравится» во время песни добавляет трек, «включи мою музыку»/«включи плейлист» играет
  отмеченное вперемешку.
- Раздача — маршрут `/media/<kind>/<путь>` (runtime.py, `web.FileResponse`): Range поддержан
  (перемотка), выход за папку закрыт; адрес колонке — хост её WS-соединения (`ctx.media_base`).
- Радио — `settings.media.radio` `[{name, url}]`, правка во вкладке «Голос → Медиа».
- Загрузка/удаление — вкладка «Голос → Медиа» (`ui/media.js`, API `ui/api/media.py`):
  POST с телом файла и `X-Filename` (до 200 МБ), DELETE по имени.
- Яндекс.Музыка через свой токен — не входит (отдельное решение владельца).
