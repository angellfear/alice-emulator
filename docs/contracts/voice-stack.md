---
title: "Голосовой стек"
status: stable
date: "2026-10-30"
module: "server_emulator/voice_stack/"
---

# Голосовой стек

## 1. Обзор
Для каждого сервиса (asr / tts / llm) активный источник выбирается в `settings["voice_stack"][service]` — это либо **пресет** `p:<id>`, либо **пакет** `k:<id>`. Применение (`apply`) записывает конфигурацию в `settings[stt|tts|model]` с ключом `"adapter"`. Конвейер (`Runtime.provider`, `recognize_stream`, `speech_stream`, `asr_streaming`, `tts_streaming`) вызывает адаптер, указанный в этом ключе. Профили устройств продолжают работать — они переопределяют те же настройки.

## 2. Пресеты
**Файл:** `state/voice_stack/presets/<service>/<id>.json`
```json
{
  "id": "my-whisper",
  "service": "asr",
  "title": "Whisper local",
  "adapter": "openai",
  "values": { "url": "http://127.0.0.1:5013/v1/audio/transcriptions", "model": "base" }
}
```
UI «Добавить» → выбрать адаптер → «Заполнить как» (examples из адаптера) → создать. API-ключ маскируется значением `__KEEP__` (подставляется прежнее значение при сохранении). Миграция «Текущие настройки» создаёт пресет из имеющихся `settings[stt|tts|model]`, если источник не выбран.

## 3. Адаптеры — код
**Контракт модуля** (встроенные в `server_emulator/voice_stack/adapters/`, свои — в `server_emulator/state/voice_stack/adapters/<id>.py`, подхватываются без рестарта по mtime):
```python
TITLE = "Anthropic (Claude)"
DESCRIPTION = "..."
SERVICES = {
    "llm": {
        "fields": [{"key": "api_key", "type": "password"}, {"key": "model", "type": "select", "options": [...]}],
        "defaults": {"url": "https://api.anthropic.com/v1/messages", "model": "claude-haiku-5-5", "temperature": 0.3},
        "examples": {"Groq": {"url": "https://api.groq.com/openai/v1/chat/completions", "model": "llama-3.3-70b-versatile"}}
    }
}
MATCH = {"llm": r"api\.anthropic\.com"}  # автоопределение по URL старых настроек

async def complete(runtime, config, payload) -> dict          # LLM: OpenAI chat → {"choices":[{"message"}],"usage"}
async def transcribe(runtime, config, audio, filename) -> str  # ASR: аудио целиком → текст
async def recognize_stream(runtime, config, chunks, fmt, on_partial) -> {"text"}  # ASR потоком (опц.)
async def speech(runtime, config, payload) -> (bytes, content_type)  # TTS: OpenAI Speech payload → Ogg Opus
def speech_stream(runtime, config, text) -> AsyncGenerator[bytes, None]  # TTS потоком (опц.)
def streams(config) -> bool                 # TTS: использовать speech_stream для этой конфигурации
```
`runtime.lifecycle.client_session()` — общий `aiohttp` клиент; `config` — копия настроек с полями пресета; `api_key` как есть (`env:VAR` раскрывает `auth.secret`).

**Пример кастомного LLM адаптера** (`server_emulator/state/voice_stack/adapters/myllm.py`):
```python
TITLE = "My LLM"
DESCRIPTION = "OpenAI-совместимый"
SERVICES = {"llm": {"fields": [{"key":"url","type":"text"},{"key":"model","type":"text"},{"key":"api_key","type":"password"}], "defaults":{"url":"https://api.example.com/v1/chat/completions","model":"my-model"}}}
MATCH = {"llm": r"api\.example\.com"}

async def complete(runtime, config, payload):
    async with runtime.lifecycle.client_session().post(config["url"], json=payload, headers={"Authorization": f"Bearer {config['api_key']}"}) as r:
        return await r.json()
```

**Встроенные адаптеры:**
- `openai` — llm / asr / tts (OpenAI-совместимый)
- `anthropic` — llm (Messages API, инструменты в обе стороны)
- `stream` — asr (WebSocket `/asr/stream`)
- `stub` — llm (возвращает `stub_text`)

## 4. Пакеты
**JSON формат** (`voice_stack/packages/<id>.json`): все ключи из docstring модуля.
```json
{
  "id": "piper", "service": "tts", "title": "Piper", "order": 10,
  "requirements": {"gpu": false, "vram_gb": 0, "disk_gb": 0.2},
  "adapter": "openai",
  "config": {"url": "http://127.0.0.1:{port}/v1/audio/speech", "voice": "{voice}"},
  "fields": [{"key":"voice","type":"select","options":[...]},{"key":"device","type":"select","options":[{"value":"cpu"},{"value":"cuda"}]}],
  "defaults": {"device": "cpu", "port": 5014, "voice": "irina"},
  "server": {"script": "tools/builder_tts.py", "health": "/health", "env": {"TTS_THREADS": "{threads}"}},
  "platforms": {"windows": {"cpu": [{"pip": ["sherpa-onnx==1.13.8", "numpy", "aiohttp"]}], "cuda": [{"pip": ["sherpa-onnx==1.13.8+cuda", "numpy", "aiohttp"], "args": ["-f", "https://k2-fsa.github.io/sherpa/onnx/cuda.html"]}]}, "linux": {...}},
  "notes": {"linux": "..."},
  "models": [{"url": "https://.../{voice}.tar.bz2"}],
  "reinstall_on": ["device", "voice"]
}
```
Шаг установки — `{"pip": [...], "args": [...]}` (pip в venv пакета) или `{"run": [...], "title": "..."}`. Плейсхолдеры: параметры запуска, `{dir}` — папка установки, `{python}` — python venv. Параметры запуска хранятся в `state/voice_stack/packages/<id>.json` — доступны до установки и переживают удаление. venv: `data/speech/<id>/venv`. Живой `/health` на порту = «запущен» (переживает рестарт панели).

**Поля параметров запуска:** `"when": {"device": "cuda"}` — поле видно только при этом значении другого поля; `"options_from": "gpus"` — список видеокарт NVIDIA из `nvidia-smi` (значение — индекс, уходит в `CUDA_VISIBLE_DEVICES`). Ключ `priority: "low"` запускает сервер с фоновым приоритетом (Windows `BELOW_NORMAL_PRIORITY_CLASS`, Linux `nice 10`); `threads` ограничивает потоки процессора. Сборка под CUDA работает и на процессоре — переключение cuda→cpu переустановки не требует. PID сервера пишется в `data/speech/<id>/server.pid`: «Перезапустить» останавливает и процесс, запущенный прошлым экземпляром панели.

**Поле-файл** (`"type": "file"`, F5 — эталон голоса): загрузка `POST voice-stack-file?id=&key=` телом WAV; сервер проверяет RIFF/WAVE, PCM или float, 1–2 канала, 8–96 кГц, 2–30 с, ≤15 МБ. Один файл на поле: `state/voice_stack/packages/<id>/<key>.wav` (замена перезаписывает), путь пишется в параметр; `GET` — прослушать, `DELETE` — удалить. Загрузка в применённый пакет перезапускает сервер.

## Звуковой кеш
Раздел «Звуковой кеш» (бывшие «Стандартные ответы»): фразы — включённые стандартные ответы и всё, что уже озвучивалось через кеш (сценарии), индекс `state/tts_cache/index.json`. Кеш работает, если синтез отдаёт PCM (`Runtime.get_tts()` → `voice_stack.PcmTTS`, адаптер с `synth_pcm` и `pcm_f32le`/`wav`); ключ файла — движок + URL + модель + голос + текст. Стандартные ответы озвучиваются через кеш. `POST sound-cache-regenerate` пересинтезирует все фразы текущей моделью в фоне; `GET sound-cache` — `{engine, entries[{text, source, status}], job{total, done, errors, state}}`, статус фразы: missing (серый) · pending/running (жёлтый) · cached (зелёный) · error.
Фразы сценариев (без `{{переменных}}`) попадают в список сами; у записи `name`/`id` источника (`builtin` — встроенный сценарий), иконка правки открывает сценарий в редакторе с выделенной фразой (`window.openScenarioPhrase`), у стандартного ответа — модальный редактор (`window.openReplyEditor`, ui/replies.js). Реплика всегда озвучивается одной фразой (куски не склеиваются): фраза без переменных — из кеша целиком, с подставленными значениями — один запрос синтеза мимо кеша. Статические фразы, ушедшие из сценариев/ответов, удаляются из кеша при сохранении настроек; старые записи значений (`dynamic` в индексе) удаляются там же и при смене `settings.tts` (`TtsCache.drop_dynamic`).

**F5-TTS на Windows:** torchaudio ≥2.9 читает аудио через torchcodec (нужны DLL FFmpeg); `tools/f5_tts_server.py` подменяет `torchaudio.load` чтением через soundfile — эталон должен быть WAV/FLAC.

**Статусы:** `absent` / `installing` / `running` / `starting` / `stopped` / `error` / `unsupported`. «Переустановить» доступно, когда изменились ключи из `reinstall_on`.

**Комплектация:**
| id | сервис | адаптер | порт | примечание |
|---|---|---|---|---|
| sherpa-t-one | asr | stream | 5013 | WebSocket /asr/stream |
| piper | tts | openai | 5014 | голоса irina/denis/dmitri/ruslan |
| f5-tts | tts | openai | 8777 | PyTorch, клонирование голоса; чекпойнт по умолчанию `hf://Misha24-10/F5-TTS_RUSSIAN/...`, env `F5_*` |

## 5. API
Эндпоинты из `server_emulator/ui/api/voice_stack.py`:

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/voice-stack` | `{asr|tts|llm: {presets, packages, adapters, active}}` |
| POST | `/voice-stack-preset` | `{service, adapter, title, values, id?}` создать/обновить пресет |
| DELETE | `/voice-stack-preset?service=&id=` | удалить пресет |
| POST | `/voice-stack-apply` | `{service, ref: "p:<пресет>\|k:<пакет>"}` → применить (пишет settings) |
| POST | `/voice-stack-package` | `{id, values}` сохранить параметры запуска пакета |
| POST | `/voice-stack-install` | `{id}` фоновая установка (прогресс в GET) |
| POST | `/voice-stack-transcribe` | `?id=&key=` текст загруженного эталона WAV текущим ASR → `{text}` (кнопка «Распознать текст»; поле с `transcribe_to`) |
| POST | `/voice-stack-test` | `{ref}` проба синтеза «Привет, это проверка голоса» без кеша → WAV (кнопка ▶ у статуса «Работает» TTS-пакета) |
| POST | `/voice-stack-uninstall` | `{id}` удалить установку, если сервер не запущен (иначе отказ; параметры остаются) |
| POST | `/voice-stack-restart` | `{id}` перезапуск сервера пакета |
### Эталон голоса и запекание (OmniVoice)
Поле-файл (`type: file`) меняется только загрузкой/удалением файла; форма путь не перезаписывает. Новый файл не перезапускает сервер сразу — пакет помечается, и «Сохранить настройки» перезапускает работающий сервер. Если у поля есть `transcribe_to`, а текст пуст, при сохранении он распознаётся текущим ASR. Сервер OmniVoice запекает эталон один раз (`create_voice_clone_prompt` → `<поле>.<хеш>.pt` рядом с WAV; новый эталон или текст — новое запекание) и синтезирует с `voice_clone_prompt`; `instruct` применяется только без эталона, шаги — `num_step`. Удаление эталона удаляет и его `.pt`. Режим голоса виден в статусе пакета (`state.voice` из `/health`).
