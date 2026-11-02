---
title: "Голосовой стек"
status: stable
date: "2026-11-01"
module: "server_emulator/voice_stack/"
---

# Голосовой стек

## 1. Обзор
Для каждого сервиса (asr / tts / llm) активный источник выбирается в `settings["voice_stack"][service]` — это либо **пресет** `p:<id>`, либо **пакет** `k:<id>`. Применение (`apply`) записывает конфигурацию в `settings[stt|tts|model]` с ключом `"adapter"`. Конвейер (`Runtime.provider`, `recognize_stream`, `speech_stream`, `asr_streaming`, `tts_streaming`) вызывает адаптер, указанный в этом ключе. Профили устройств продолжают работать — они переопределяют те же настройки.

`apply` меняет общие настройки, поэтому UI голосовой вкладки перечитывает их после применения (TASK-088), а полное сохранение устаревшей копии формы сервер отклоняет по ревизии: `GET /api/settings` добавляет поле `_rev`, сохранение с чужой `_rev` отвечает 409 (форма перечитывает базу и сохраняет заново; запросы без `_rev` — внешние клиенты — сохраняются как раньше). Смена режима «Интеллект» — отдельный `PUT /api/intelligence`, сохраняющий только ключ `intelligence`: прежний полный PUT откатывал выбранный источник ASR и распознавание замолкало. Готовность источников показывает строка состояния в шапке панели — `GET /api/live` (см. `docs/architecture.md`, раздел «Переключение режима „Интеллект“ и строка состояния»).

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
        "title": "YandexGPT (Online)",          # необязательно: название сервиса вместо TITLE (адаптер на несколько сервисов)
        "description": "...",                    # необязательно: описание сервиса вместо DESCRIPTION
        "fields": [{"key": "api_key", "type": "password"}, {"key": "model", "type": "select", "options": [...]}],
        "defaults": {"url": "https://api.anthropic.com/v1/messages", "model": "claude-haiku-5-5", "temperature": 0.3},
        "examples": {"Groq": {"url": "https://api.groq.com/openai/v1/chat/completions", "model": "llama-3.3-70b-versatile"}}
    },
    "tts": {"voice_modes": ["preset", "baked"], "bake": {"seconds": [5, 120], "text": False}, "fields": [...], "defaults": {...}}
}
MATCH = {"llm": r"api\.anthropic\.com"}  # автоопределение по URL старых настроек

async def complete(runtime, config, payload) -> dict          # LLM: OpenAI chat → {"choices":[{"message"}],"usage"}
async def transcribe(runtime, config, audio, filename) -> str  # ASR: аудио целиком → текст
async def recognize_stream(runtime, config, chunks, fmt, on_partial) -> {"text"}  # ASR потоком (опц.)
async def speech(runtime, config, payload) -> (bytes, content_type)  # TTS: OpenAI Speech payload → Ogg Opus
def speech_stream(runtime, config, text) -> AsyncGenerator[bytes, None]  # TTS потоком (опц.)
def streams(config) -> bool                 # TTS: использовать speech_stream для этой конфигурации
async def synth_pcm(runtime, config, text) -> (np.ndarray float32, rate)   # TTS: PCM целиком для звукового кеша (необязательно)
async def voices(runtime, config) -> [{"value", "label", "lang", "gender"}]   # список голосов поставщика (необязательно)
async def bake_voice(runtime, config, audio, text) -> {"voice_id", "title"}   # запечь голос (необязательно)
async def unbake_voice(runtime, config, voice_id) -> None                      # удалить запечённый голос (необязательно)
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

**Встроенные адаптеры:** см. таблицу «Поставщики» ниже.

### Поля адаптеров и пакетов
Поле — `key`, `label`, `type` (`text` · `password` · `number` · `select` · `textarea` · `file`), `options`, `when` и метаданные для UI:

| Ключ поля | Значение | Как показывает UI |
|---|---|---|
| `"type": "password"` | токен, ключ, секрет | поле-пароль; значение не возвращается в API открытым (маска `__KEEP__`, пусто = оставить прежний), `env:VAR` работает |
| `"help"` | одна строка: где взять | подсказка под полем — только у полей, где без неё не обойтись |
| `"link"` | URL страницы получения ключа | ссылка «Получить ключ» рядом с названием поля |
| `"group": "advanced"` | редкий параметр | поле под кнопкой «Дополнительно» (свёрнуто по умолчанию, состояние хранится только в открытой странице) |
| `"required": true` | без значения не работает | звёздочка у названия; «Создать» / «Сохранить» / «Применить» подсвечивают пустое и не продолжают |
| `"options_from": "voices"` | список голосов с API поставщика | select с кнопкой «Обновить список»; адаптер даёт `voices()`; ошибка или адаптер без `voices()` — список `options` по умолчанию + сообщение |
| `"options_from": "gpus"` | видеокарты NVIDIA | как раньше (пакеты) |

Если список зависит от поля `engine` (Google TTS, Microsoft TTS), при смене движка UI запрашивает голоса заново. «Обновить список» передаёт адаптеру `config["refresh"] = True` (адаптеры кешируют список на 10 минут; `voices(runtime, config, refresh=False)`). Поле с `when` и `required` обязательно только пока видно: скрытые условием поля не подсвечиваются и не проверяются.

Метаданные проверяет `validate_fields` (неизвестные `group` и `options_from` отвергаются). У пакетов (`voice_stack/packages/*.json`) те же ключи: порт, потоки, приоритет, чекпойнт, шаги — `group: advanced`.

### Голос TTS: режимы
`"voice_modes"` у сервиса адаптера (`SERVICES["tts"]`) или у пакета — подмножество, в порядке показа:

| Режим | Подпись в UI | Смысл | Поля |
|---|---|---|---|
| `preset` | Встроенный голос | голос поставщика/модели | select голоса (`options` или `options_from: voices`), стиль/эмоция |
| `baked` | Свой голос (запечённый) | эталон обрабатывается один раз и хранится: у поставщика — созданный `voice_id`, локально — файл эмбеддинга (`.pt`); синтез потом без эталона | загрузка эталона и текст, «Запечь», статус «Запечён / Не запечён», «Удалить голос» |
| `clone` | Клонирование по эталону | эталон подставляется в каждый запрос | поле-файл эталона + текст |

Переключатель показывается, если режимов больше одного; единственный `baked`/`clone` — подписью над полями. Текущий режим — значение `voice_mode` (пресеты и параметры пакета; служебные `voice_mode`, `baked_voice_id`, `baked_voice_title` принимаются без объявления в `fields`). Поля режима — обычные поля с `"when": {"voice_mode": "clone"}`. Постоянный `voice_id` (ElevenLabs и т. п.) — это `baked`, не `clone`.

**Запекание у поставщика (пресет).** `SERVICES["tts"]["bake"]` — необязательные правила: `seconds` (допустимая длина эталона, по умолчанию 2–30 с) и `text: false` (текст эталона не нужен). `POST voice-stack-bake` (multipart: `meta` — `{service, adapter, values, id?, text}` и WAV `audio`) вызывает `bake_voice` и возвращает `{voice_id, title}`; UI кладёт их в `baked_voice_id` / `baked_voice_title` пресета и сохраняет. Эталон нигде не хранится. `POST voice-stack-unbake` вызывает `unbake_voice` (если есть) — UI очищает значения. Адаптер получает `config["voice_mode"]` и `config["baked_voice_id"]`.

**Клонирование (пресет).** Поле-файл адаптера (`type: file`, `when: {voice_mode: clone}`) загружается в сохранённый пресет: `POST voice-stack-preset-file?service=&id=&key=` (WAV, как у пакетов), хранится в `state/voice_stack/presets/<service>/<id>/<ключ>.wav`, путь попадает в `config[<ключ>]` при применении; наружу API путь не отдаёт — только `files`. Удаление пресета удаляет и эталон.

**Запекание у пакета.** `"bake": {"audio": "<поле-файл>", "text": "<поле текста>", "mode": "baked", "transcribe": true}`: сервер пакета запекает эталон при запуске (OmniVoice: `<эталон>.<sha1(WAV + текст)[:12]>.pt` рядом с WAV). «Запечь» = сохранить параметры, распознать пустой текст текущим ASR (кроме `"transcribe": false` — модели текст не нужен, как XTTS-v2), перезапустить сервер (`POST voice-stack-bake {package}`); статус — наличие файла эмбеддинга с актуальным хешем (`bake.baked`, `bake.name` в `GET voice-stack`). «Удалить голос» удаляет эталон и его `.pt` (`POST voice-stack-unbake {package}`). Вне режима `bake.mode` эталон серверу не передаётся (`OV_REF_AUDIO` пуст).
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
  "models": [{"url": "https://.../{voice}.tar.bz2"}],   // .tar.bz2 / .tar.gz / .zip распаковываются (внутри — папка модели)
  "reinstall_on": ["device", "voice"]
}
```
Шаг установки — `{"pip": [...], "args": [...]}` (pip в venv пакета) или `{"run": [...], "title": "..."}`. Плейсхолдеры: параметры запуска, `{dir}` — папка установки, `{python}` — python venv. Параметры запуска хранятся в `state/voice_stack/packages/<id>.json` — доступны до установки и переживают удаление. venv: `data/speech/<id>/venv`. Живой `/health` на порту = «запущен» (переживает рестарт панели).

**Поля параметров запуска:** `"when": {"device": "cuda"}` — поле видно только при этом значении другого поля; `"options_from": "gpus"` — список видеокарт NVIDIA из `nvidia-smi` (значение — индекс, уходит в `CUDA_VISIBLE_DEVICES`). Ключ `priority: "low"` запускает сервер с фоновым приоритетом (Windows `BELOW_NORMAL_PRIORITY_CLASS`, Linux `nice 10`); `threads` ограничивает потоки процессора. Сборка под CUDA работает и на процессоре — переключение cuda→cpu переустановки не требует. PID сервера пишется в `data/speech/<id>/server.pid`: «Перезапустить» останавливает и процесс, запущенный прошлым экземпляром панели.

**Поле-файл** (`"type": "file"`, F5 — эталон голоса): загрузка `POST voice-stack-file?id=&key=` телом WAV; сервер проверяет RIFF/WAVE, PCM или float, 1–2 канала, 8–96 кГц, 2–30 с, ≤15 МБ. Один файл на поле: `state/voice_stack/packages/<id>/<key>.wav` (замена перезаписывает), путь пишется в параметр; `GET` — прослушать, `DELETE` — удалить. Загрузка в применённый пакет перезапускает сервер.

## Поставщики
Метка: `(Online Free)` — есть бесплатный тариф, `(Online)` — платно. Токены и ключи — поля-пароли со ссылкой «Получить ключ»; `*` — обязательное поле. Названия сервисов берутся из `SERVICES[<сервис>]["title"]`. Локальные пакеты — в разделе «Пакеты» выше (и в таблице комплектации).

| Адаптер | Сервис | Метка | Основные поля | Дополнительно | Режимы голоса |
|---|---|---|---|---|---|
| `openai` | llm / asr / tts | любой сервер с OpenAI API; в «Заполнить как» — OpenAI (Online), Groq (Online Free), DeepSeek (Online), OpenRouter (Online Free / Online), Gemini OpenAI-совместимый (Online Free), Ollama, vLLM, speaches | URL*, API-ключ, модель*, у asr — язык, у tts — голос* | температура, уровень рассуждений (llm), формат аудио (tts) | `preset` (tts) |
| `anthropic` | llm | (Online) | API-ключ*, модель | температура, макс. токенов, URL | — |
| `stream` | asr | — | URL, модель, язык | — | — |
| `stub` | llm | — | текст ответа | — | — |
| `google` | llm | Gemini (Online Free) | API-ключ*, модель | температура, уровень рассуждений, URL | — |
| `google` | asr | Gemini (Online Free) | API-ключ*, модель, язык | словарь, подсказка, URL | — |
| `google` | tts | Gemini TTS и Cloud TTS (Online Free; Cloud TTS — Online) | API-ключ*, движок (`engine`: gemini / cloud), модель (gemini), голос (список с API) | стиль (gemini), скорость и высота (cloud), URL | `preset` |
| `microsoft` | tts | Edge TTS и Azure Speech (Online Free) | движок (`engine`: edge без ключа / azure), API-ключ* и регион* (azure), голос (список с API) | стиль и роль (azure), скорость, высота, частота, URL | `preset` |
| `microsoft` | asr | Azure Speech (Online Free) | API-ключ*, регион*, язык | фильтр брани, URL | — |
| `elevenlabs` | tts | ElevenLabs (Online) | API-ключ*, модель, голос (список с API) | язык, стабильность, схожесть, стиль, скорость, усиление диктора, частота, URL | `preset`, `baked` (Instant Voice Clone, текст эталона не нужен) |
| `elevenlabs` | asr | ElevenLabs Scribe (Online) | API-ключ*, модель, язык | URL | — |
| `deepgram` | asr | Deepgram (Online Free) | API-ключ*, модель, язык | умное форматирование, пунктуация, пауза конца фразы, URL | — |
| `yandex` | asr | Yandex SpeechKit (Online) | API-ключ, идентификатор каталога | язык, модель, адрес | — |
| `yandex` | tts | Yandex SpeechKit (Online) | API-ключ, каталог, голос (filipp, ermil, zahar, madi_ru, jane, omazh, marina, alena), эмоция/роль | скорость, частота звука, адрес | `preset` |
| `yandex` | llm | YandexGPT (Online) | API-ключ, каталог*, модель (Lite / Pro / RC) | температура, макс. токенов, адрес | — |
| `sber` | asr | SaluteSpeech (Online) | ключ авторизации, тип доступа (PERS / B2B / CORP) | язык, модель, сертификаты НУЦ Минцифры, проверка сертификата, адреса | — |
| `sber` | tts | SaluteSpeech (Online) | ключ авторизации, тип доступа, голос (Nec, Bys, May, Tur, Ost, Pon) | скорость (SSML), сертификаты, адреса | `preset` |
| `sber` | llm | GigaChat (Online Free) | ключ авторизации, тип доступа, модель (GigaChat-2 / -Pro / -Max) | температура, макс. токенов, сертификаты, адреса | — |
| `tbank` | asr | T-Bank VoiceKit (Online) | API-ключ, секретный ключ | адрес | — |
| `tbank` | tts | T-Bank VoiceKit (Online) | API-ключ, секретный ключ, голос (alyona, maxim, dorofeev) | адрес, частота звука | `preset` |

Облачные ASR здесь распознают фразу целиком (REST): потоковые режимы Яндекса, Сбера и T-Bank доступны только по gRPC (нужен `grpcio`, в зависимости не добавляется). TTS отдаётся сырым PCM и кодируется в Ogg Opus конвейером (`speech_stream`, `synth_pcm` для звукового кеша).

**Яндекс.** SpeechKit REST v1 (`stt:recognize` ≤30 с и ≤1 МБ, `tts:synthesize` lpcm), заголовок `Authorization: Api-Key`; YandexGPT — OpenAI-совместимый интерфейс, модель `gpt://<каталог>/<модель>`, `Bearer` и `OpenAI-Project`. Эмоция отправляется, только если её поддерживает выбранный голос.

**Сбер.** OAuth `POST /api/v2/oauth` (`Basic <ключ>`, `RqUID` — uuid4, `scope=SALUTE_SPEECH_<тип>` или `GIGACHAT_API_<тип>`): `access_token` живёт 30 минут и кешируется в памяти до истечения (за минуту до конца — новый); отозванный раньше срока токен (401) обновляется один раз. Серверы подписаны сертификатом НУЦ Минцифры: в «Дополнительно» указывается путь к корневому сертификату (`ca_bundle`) либо отключается проверка (`verify: off`, небезопасно). GigaChat: инструменты OpenAI ↔ `functions` / `function_call`.

**T-Bank.** JWT HS256 (`kid` — API-ключ, подпись — секретный ключ из base64url, `aud` = `tinkoff.cloud.stt` / `tinkoff.cloud.tts`, срок 10 минут, кеш до истечения), REST `POST /v1/stt:recognize` и `/v1/tts:synthesize` (JSON, base64).

## Звуковой кеш
Раздел «Звуковой кеш» (бывшие «Стандартные ответы»): фразы — включённые стандартные ответы и всё, что уже озвучивалось через кеш (сценарии), индекс `state/tts_cache/index.json`. Кеш работает, если синтез отдаёт PCM (`Runtime.get_tts()` → `voice_stack.PcmTTS`, адаптер с `synth_pcm` и `pcm_f32le`/`wav`); ключ файла — движок + URL + модель + голос + текст. Стандартные ответы озвучиваются через кеш. `POST sound-cache-regenerate` пересинтезирует все фразы текущей моделью в фоне; `GET sound-cache` — `{engine, entries[{text, source, status}], job{total, done, errors, state}}`, статус фразы: missing (серый) · pending/running (жёлтый) · cached (зелёный) · error.
Фразы сценариев (без `{{переменных}}`) попадают в список сами; у записи `name`/`id` источника (`builtin` — встроенный сценарий), иконка правки открывает сценарий в редакторе с выделенной фразой (`window.openScenarioPhrase`), у стандартного ответа — модальный редактор (`window.openReplyEditor`, ui/replies.js). Реплика всегда озвучивается одной фразой (куски не склеиваются): фраза без переменных — из кеша целиком, с подставленными значениями — один запрос синтеза мимо кеша. Статические фразы, ушедшие из сценариев/ответов, удаляются из кеша при сохранении настроек; старые записи значений (`dynamic` в индексе) удаляются там же и при смене `settings.tts` (`TtsCache.drop_dynamic`).

**F5-TTS на Windows:** torchaudio ≥2.9 читает аудио через torchcodec (нужны DLL FFmpeg); `tools/f5_tts_server.py` подменяет `torchaudio.load` чтением через soundfile — эталон должен быть WAV/FLAC.

**Статусы:** `absent` / `installing` / `running` / `starting` / `stopped` / `error` / `unsupported`. «Переустановить» доступно, когда изменились ключи из `reinstall_on`.

**Комплектация:**
| id | сервис | адаптер | порт | режимы голоса | примечание |
|---|---|---|---|---|---|
| sherpa-t-one | asr | stream | 5013 | — | WebSocket /asr/stream |
| piper | tts | openai | 5014 | `preset` | голоса irina/denis/dmitri/ruslan |
| f5-tts | tts | openai | 8777 | `clone` | PyTorch, клонирование голоса; чекпойнт по умолчанию `hf://Misha24-10/F5-TTS_RUSSIAN/...`, env `F5_*` |
| omnivoice | tts | openai | 8778 | `preset` (описание голоса), `baked` | PyTorch, запечённый эталон `.pt` |
| gigaam | asr | openai | 5015 | — | GigaAM v3 (MIT) на onnx-asr, варианты e2e-rnnt / e2e-ctc / rnnt / ctc; CPU int8 ~226 МБ, длинное аудио режется Silero VAD |
| parakeet | asr | openai | 5016 | — | NVIDIA Parakeet-tdt-0.6b-v3 (CC-BY-4.0) на onnx-asr; GPU по умолчанию |
| faster-whisper | asr | openai | 5017 | — | Whisper large-v3-turbo / large-v3 / medium / small (MIT) на CTranslate2; на CPU — int8, медленно |
| vosk | asr | openai | 5018 | — | Vosk small-ru-0.22 / ru-0.42 (Apache-2.0), только CPU; качество ниже |
| silero | tts | openai | 5019 | `preset` | Silero v5 ru: aidar, baya, kseniya, xenia, eugene; лицензия CC BY-NC 4.0 (некоммерческая) |
| espeech | tts | openai | 5020 | `clone` | ESpeech TTS-1 (F5-TTS, Apache-2.0) + RUAccent; эталон в каждом запросе |
| qwen-tts | tts | openai | 5021 | `preset` (описание голоса), `baked` | Qwen3-TTS 12Hz 1.7B Base / VoiceDesign (Apache-2.0), GPU |
| xtts | tts | openai | 5022 | `baked` | Coqui XTTS-v2, латенты говорящего хранятся; лицензия CPML (некоммерческая), torch 2.8 |

## 5. API
Эндпоинты из `server_emulator/ui/api/voice_stack.py`:

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/voice-stack` | `{asr|tts|llm: {presets, packages, adapters, active}}` |
| POST | `/voice-stack-preset` | `{service, adapter, title, values, id?}` создать/обновить пресет |
| DELETE | `/voice-stack-preset?service=&id=` | удалить пресет |
| POST | `/voice-stack-apply` | `{service, ref: "p:<пресет>\|k:<пакет>"}` → применить (пишет settings) |
| GET | `/live` | строка состояния шапки (домен `live.py`, TASK-088): `{intelligence, services: {asr\|tts\|llm: {status: ok\|warn\|err\|off, hint, source, kind, ref}}}, agent: {state, tool, error}` |
| POST | `/voice-stack-package` | `{id, values}` сохранить параметры запуска пакета |
| POST | `/voice-stack-install` | `{id}` фоновая установка (прогресс в GET) |
| POST | `/voice-stack-transcribe` | `?id=&key=` текст загруженного эталона WAV текущим ASR → `{text}` (кнопка «Распознать текст»; поле с `transcribe_to`) |
| POST | `/voice-stack-test` | `{ref}` проба синтеза «Привет, это проверка голоса» без кеша → WAV (кнопка ▶ у статуса «Работает» TTS-пакета) |
| POST | `/voice-stack-uninstall` | `{id}` удалить установку, если сервер не запущен (иначе отказ; параметры остаются) |
| POST | `/voice-stack-restart` | `{id}` перезапуск сервера пакета |
| POST | `/voice-stack-voices` | `{service, adapter, values, id?, refresh?}` → `{options: [{value, label, lang?, gender?}], error?}`; сбой — `options` по умолчанию + `error` (кнопка «Обновить список») |
| POST | `/voice-stack-bake` | multipart `meta` + `audio` (WAV) → `{voice_id, title}`; JSON `{package}` — запечь эталон пакета (перезапуск сервера) |
| POST | `/voice-stack-unbake` | `{service, adapter, values, id?}` — удалить запечённый голос поставщика; `{package}` — голос пакета |
| POST/GET/DELETE | `/voice-stack-preset-file` | `?service=&id=&key=` эталон клонирования пресета (WAV): загрузить / прослушать / удалить |
### Эталон голоса и запекание (OmniVoice)
Поле-файл (`type: file`) меняется только загрузкой/удалением файла; форма путь не перезаписывает. Новый файл не перезапускает сервер сразу — пакет помечается, и «Сохранить настройки» перезапускает работающий сервер. Если у поля есть `transcribe_to`, а текст пуст, при сохранении он распознаётся текущим ASR. Сервер OmniVoice запекает эталон один раз (`create_voice_clone_prompt` → `<поле>.<хеш>.pt` рядом с WAV; новый эталон или текст — новое запекание) и синтезирует с `voice_clone_prompt`; `instruct` применяется только без эталона, шаги — `num_step`. Удаление эталона удаляет и его `.pt`. Режим голоса виден в статусе пакета (`state.voice` из `/health`).
