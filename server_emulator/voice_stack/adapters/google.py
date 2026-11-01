"""Google: Gemini API (AI Studio) и Google Cloud Text-to-Speech.

  llm  — Gemini через OpenAI-совместимый эндпоинт (чат, инструменты):
         https://ai.google.dev/gemini-api/docs/openai
  asr  — речь → текст: модель gemini-3.5-transcribe (или обычная Gemini с подсказкой), Interactions API
         (аудио целиком в base64, store=false — запрос не сохраняется):
         https://ai.google.dev/gemini-api/docs/transcribe
         https://ai.google.dev/gemini-api/docs/audio
  tts  — Gemini TTS (голос из готового набора, PCM 24 кГц; Interactions API):
         https://ai.google.dev/gemini-api/docs/speech-generation
         или Cloud Text-to-Speech (русские Chirp 3 HD / WaveNet / Standard, WAV PCM 24 кГц):
         https://cloud.google.com/text-to-speech/docs/reference/rest/v1/text/synthesize
         список голосов — https://cloud.google.com/text-to-speech/docs/reference/rest/v1/voices/list

Ключ AI Studio (https://aistudio.google.com/apikey) идёт заголовком x-goog-api-key / Bearer, ключ Cloud TTS —
заголовком X-Goog-Api-Key: ни тот ни другой в адрес запроса и в журналы не попадает.
Бесплатный тариф AI Studio: часть моделей без оплаты в пределах лимитов, запросы на бесплатном тарифе
могут использоваться Google для улучшения продуктов. Cloud TTS — платёжный аккаунт Google Cloud, у голосов
есть ежемесячная бесплатная квота (https://cloud.google.com/text-to-speech/pricing).
"""
import base64

from . import _vendor as vendor

TITLE = "Google (Gemini, Cloud TTS)"
DESCRIPTION = "Gemini (чат, распознавание, голос) — бесплатный тариф AI Studio; Gemini Pro и Cloud TTS — платно."
NAME = "Google"
GEMINI = "https://generativelanguage.googleapis.com/v1beta"
LLM_URL = GEMINI + "/openai/chat/completions"
INTERACTIONS_URL = GEMINI + "/interactions"
CLOUD_URL = "https://texttospeech.googleapis.com/v1"
RATE = 24000
TRANSCRIBE = "gemini-3.5-transcribe"
PROMPT = ("Распознай речь в аудио и верни только дословный текст без пояснений и кавычек. "
          "Если речи нет, верни пустую строку.")

KEY = {"key": "api_key", "label": "Ключ API", "type": "password", "required": True,
       "link": "https://aistudio.google.com/apikey",
       "help": "Google AI Studio → Get API key (для Cloud TTS — ключ из Google Cloud Console → API и сервисы → Учётные данные)"}
URL = {"key": "url", "label": "Адрес API", "type": "text", "group": "advanced",
       "help": "Пусто — официальный адрес Google; нужен для прокси"}
LLM_MODELS = [
    {"value": "gemini-3.8-flash", "label": "Gemini 3.8 Flash (быстрая, бесплатный тариф)"},
    {"value": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite (самая быстрая)"},
    {"value": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro (умная, платно)"},
]
ASR_MODELS = [
    {"value": TRANSCRIBE, "label": "Gemini 3.5 Transcribe (распознавание речи)"},
    {"value": "gemini-3.8-flash", "label": "Gemini 3.8 Flash (с подсказкой)"},
    {"value": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite (с подсказкой)"},
]
TTS_MODELS = [
    {"value": "gemini-3.8-flash-tts", "label": "Gemini 3.8 Flash TTS (выразительная)"},
    {"value": "gemini-3.8-flash-lite-tts", "label": "Gemini 3.8 Flash-Lite TTS (быстрая)"},
]
# 30 готовых голосов Gemini TTS (многоязычные, русский поддерживается): имя, пол
GEMINI_VOICES = [
    ("Kore", "female"), ("Zephyr", "female"), ("Puck", "male"), ("Charon", "male"), ("Fenrir", "male"),
    ("Leda", "female"), ("Orus", "male"), ("Aoede", "female"), ("Callirrhoe", "female"), ("Autonoe", "female"),
    ("Enceladus", "male"), ("Iapetus", "male"), ("Umbriel", "male"), ("Algieba", "male"), ("Despina", "female"),
    ("Erinome", "female"), ("Algenib", "male"), ("Rasalgethi", "male"), ("Laomedeia", "female"),
    ("Achernar", "female"), ("Alnilam", "male"), ("Schedar", "male"), ("Gacrux", "female"),
    ("Pulcherrima", "female"), ("Achird", "male"), ("Zubenelgenubi", "male"), ("Vindemiatrix", "female"),
    ("Sadachbia", "male"), ("Sadaltager", "male"), ("Sulafat", "female"),
]
GENDER = {"female": "женский", "male": "мужской"}
GEMINI_OPTIONS = [vendor.option(name, f"{name} ({GENDER[gender]})", "multi", gender) for name, gender in GEMINI_VOICES]
# Запасной список Cloud TTS (без ключа или при сбое запроса)
CLOUD_OPTIONS = [vendor.option(name, f"{name.split('ru-RU-')[1]} ({GENDER[gender]})", "ru-RU", gender) for name, gender in (
    ("ru-RU-Chirp3-HD-Kore", "female"), ("ru-RU-Chirp3-HD-Aoede", "female"), ("ru-RU-Chirp3-HD-Puck", "male"),
    ("ru-RU-Chirp3-HD-Charon", "male"), ("ru-RU-Wavenet-A", "female"), ("ru-RU-Wavenet-B", "male"),
    ("ru-RU-Standard-A", "female"), ("ru-RU-Standard-B", "male"))]

SERVICES = {
    "llm": {
        "title": "Gemini (Online Free)",
        "description": "Gemini Flash — бесплатный тариф AI Studio (с лимитами); Gemini Pro — платно.",
        "fields": [KEY,
                   {"key": "model", "label": "Модель", "type": "select", "options": LLM_MODELS},
                   {"key": "temperature", "label": "Температура", "type": "number", "step": 0.1, "group": "advanced"},
                   {"key": "reasoning_effort", "label": "Глубина размышления", "type": "select", "group": "advanced",
                    "options": [{"value": "", "label": "По умолчанию модели"}, {"value": "low", "label": "Малая (быстрее)"},
                                {"value": "medium", "label": "Средняя"}, {"value": "high", "label": "Большая (медленнее)"}]},
                   URL],
        "defaults": {"model": "gemini-3.8-flash", "temperature": 0.5, "reasoning_effort": "low"},
        "examples": {
            "Gemini Flash (Online Free)": {"model": "gemini-3.8-flash", "temperature": 0.5, "reasoning_effort": "low"},
            "Gemini Flash-Lite (Online Free)": {"model": "gemini-3.5-flash-lite", "temperature": 0.5},
            "Gemini Pro (Online)": {"model": "gemini-3.1-pro-preview", "temperature": 0.5},
        },
    },
    "asr": {
        "title": "Gemini — распознавание (Online Free)",
        "description": "Распознавание речи Gemini 3.5 Transcribe: бесплатный тариф AI Studio, фраза целиком.",
        "fields": [KEY,
                   {"key": "model", "label": "Модель", "type": "select", "options": ASR_MODELS},
                   {"key": "language", "label": "Язык", "type": "text",
                    "help": "Код языка: ru, en… Пусто — определить автоматически"},
                   {"key": "vocabulary", "label": "Слова-подсказки", "type": "textarea", "group": "advanced",
                    "help": "Имена и термины через запятую — только для Gemini 3.5 Transcribe"},
                   {"key": "prompt", "label": "Подсказка модели", "type": "textarea", "group": "advanced",
                    "help": "Только для обычных моделей Gemini (не Transcribe)"},
                   URL],
        "defaults": {"model": TRANSCRIBE, "language": "ru", "vocabulary": "", "prompt": ""},
        "examples": {
            "Gemini Transcribe (Online Free)": {"model": TRANSCRIBE, "language": "ru"},
            "Gemini Flash (Online Free)": {"model": "gemini-3.8-flash", "language": "ru"},
        },
    },
    "tts": {
        "title": "Gemini TTS и Cloud TTS (Online Free)",
        "description": "Gemini TTS — бесплатный тариф AI Studio; Cloud Text-to-Speech — платно, с ежемесячной квотой.",
        "voice_modes": ["preset"],
        "fields": [KEY,
                   {"key": "engine", "label": "Движок", "type": "select", "options": [
                       {"value": "gemini", "label": "Gemini TTS (AI Studio)"},
                       {"value": "cloud", "label": "Cloud Text-to-Speech"}]},
                   {"key": "model", "label": "Модель", "type": "select", "options": TTS_MODELS, "when": {"engine": "gemini"}},
                   {"key": "voice", "label": "Голос", "type": "select", "options": GEMINI_OPTIONS, "options_from": "voices"},
                   {"key": "style", "label": "Стиль речи", "type": "text", "group": "advanced", "when": {"engine": "gemini"},
                    "help": "Например: спокойно и дружелюбно"},
                   {"key": "speed", "label": "Скорость", "type": "number", "step": 0.05, "group": "advanced",
                    "when": {"engine": "cloud"}, "help": "1 — обычная, от 0.25 до 4"},
                   {"key": "pitch", "label": "Тон (полутоны)", "type": "number", "step": 1, "group": "advanced",
                    "when": {"engine": "cloud"}, "help": "От −20 до 20; у голосов Chirp 3 HD не действует"},
                   URL],
        "defaults": {"engine": "gemini", "model": "gemini-3.8-flash-tts", "voice": "Kore", "style": "",
                     "speed": 1.0, "pitch": 0},
        "examples": {
            "Gemini TTS (Online Free)": {"engine": "gemini", "model": "gemini-3.8-flash-tts", "voice": "Kore"},
            "Cloud TTS Chirp 3 HD (Online)": {"engine": "cloud", "voice": "ru-RU-Chirp3-HD-Kore"},
            "Cloud TTS WaveNet (Online)": {"engine": "cloud", "voice": "ru-RU-Wavenet-A"},
        },
    },
}
MATCH = {"llm": r"generativelanguage\.googleapis\.com", "tts": r"texttospeech\.googleapis\.com"}


def _cloud(config):
    return (config or {}).get("engine") == "cloud"


# --- llm ------------------------------------------------------------------------------------


async def complete(runtime, config, payload):
    key = vendor.api_key(config, NAME)
    body = {**payload, "model": config.get("model") or payload.get("model") or "gemini-3.8-flash"}
    if config.get("temperature") not in (None, ""):
        body["temperature"] = float(config["temperature"])
    if config.get("reasoning_effort") and "reasoning_effort" not in body:
        body["reasoning_effort"] = config["reasoning_effort"]
    return await vendor.call_json(runtime, NAME, "POST", config.get("url") or LLM_URL, key,
                                  headers={"Authorization": "Bearer " + key}, json=body)


# --- asr ------------------------------------------------------------------------------------


def _text_of(result):
    """Текст ответа Interactions API: output_text или model_output-шаги."""
    if isinstance(result.get("output_text"), str):
        return result["output_text"].strip()
    parts = []
    for step in result.get("steps") or []:
        if step.get("type") == "model_output":
            parts += [c.get("text", "") for c in step.get("content") or [] if c.get("type") == "text"]
    return "".join(parts).strip()


def _language_code(language):
    """ru → ru-RU: Transcribe принимает BCP-47."""
    language = (language or "").strip()
    return language if "-" in language or not language else {"ru": "ru-RU", "en": "en-US", "uk": "uk-UA"}.get(language, language)


async def transcribe(runtime, config, audio, filename):
    key = vendor.api_key(config, NAME)
    model = config.get("model") or TRANSCRIBE
    mime = "audio/ogg" if audio[:4] == b"OggS" else "audio/wav"
    content = [{"type": "audio", "data": base64.b64encode(audio).decode("ascii"), "mime_type": mime}]
    body = {"model": model, "store": False}
    if model.startswith("gemini-3.5-transcribe"):
        transcription = {"language_codes": [_language_code(config.get("language"))] if config.get("language") else []}
        words = [w.strip() for w in str(config.get("vocabulary") or "").replace("\n", ",").split(",") if w.strip()]
        if words:
            transcription["custom_vocabulary"] = words[:1000]
        body["generation_config"] = {"transcription_config": transcription}
        body["input"] = content
    else:
        prompt = config.get("prompt") or PROMPT
        if config.get("language"):
            prompt += f" Язык речи: {config['language']}."
        body["input"] = [{"type": "text", "text": prompt}] + content
    result = await vendor.call_json(runtime, NAME, "POST", config.get("url") or INTERACTIONS_URL, key,
                                    headers={"x-goog-api-key": key}, json=body)
    return _text_of(result)


# --- tts ------------------------------------------------------------------------------------


async def _gemini_pcm(runtime, config, text):
    key = vendor.api_key(config, NAME)
    voice = config.get("voice") or "Kore"
    if "-" in voice:
        raise ValueError(f"{NAME}: голос «{voice}» не подходит Gemini TTS — выберите из списка")
    block = {"type": "text", "text": text}
    if config.get("style"):
        block["annotations"] = [{"type": "speech_metadata", "style": str(config["style"])}]
    body = {"model": config.get("model") or "gemini-3.8-flash-tts", "store": False,
            "input": [{"type": "user_input", "content": [block]}],
            "response_format": {"type": "audio", "mime_type": "audio/l16", "sample_rate": RATE},
            "generation_config": {"speech_config": [{"voice": voice}]}}
    result = await vendor.call_json(runtime, NAME, "POST", config.get("url") or INTERACTIONS_URL, key,
                                    headers={"x-goog-api-key": key}, json=body)
    audio = None
    for step in result.get("steps") or []:
        if step.get("type") == "model_output":
            audio = next((c for c in step.get("content") or [] if c.get("type") == "audio" and c.get("data")), audio)
    if audio is None:
        raise ValueError(f"{NAME}: в ответе нет звука")
    data = base64.b64decode(audio["data"])
    if data[:4] == b"RIFF":
        return vendor.wav_pcm(data)
    mime = str(audio.get("mime_type") or "")
    rate = int(mime.split("rate=")[1].split(";")[0]) if "rate=" in mime else RATE
    return data, rate


async def _cloud_pcm(runtime, config, text):
    key = vendor.api_key(config, NAME)
    voice = config.get("voice") or "ru-RU-Chirp3-HD-Kore"
    if "-" not in voice:
        raise ValueError(f"{NAME}: голос «{voice}» не подходит Cloud TTS — выберите из списка")
    audio = {"audioEncoding": "LINEAR16", "sampleRateHertz": RATE}
    if config.get("speed") not in (None, "", 1, 1.0):
        audio["speakingRate"] = max(0.25, min(4.0, float(config["speed"])))
    if config.get("pitch") not in (None, "", 0) and "Chirp" not in voice:
        audio["pitch"] = max(-20.0, min(20.0, float(config["pitch"])))
    body = {"input": {"text": text}, "voice": {"languageCode": "-".join(voice.split("-")[:2]), "name": voice},
            "audioConfig": audio}
    base = (config.get("url") or CLOUD_URL).rstrip("/")
    result = await vendor.call_json(runtime, NAME, "POST", base + "/text:synthesize", key,
                                    headers={"X-Goog-Api-Key": key}, json=body)
    if not result.get("audioContent"):
        raise ValueError(f"{NAME}: в ответе нет звука")
    return vendor.wav_pcm(base64.b64decode(result["audioContent"]))


async def _pcm(runtime, config, text):
    return await (_cloud_pcm if _cloud(config) else _gemini_pcm)(runtime, config, text)


async def speech(runtime, config, payload):
    pcm, rate = await _pcm(runtime, config, payload["input"])
    return await vendor.pcm_to_ogg(pcm, rate), "audio/ogg"


def streams(config):
    return True


async def speech_stream(runtime, config, text):
    # оба API отдают звук целиком — страницы пойдут сразу после ответа
    pcm, rate = await _pcm(runtime, config, text)
    async for page in vendor.pcm_pages(vendor.bytes_chunks(pcm), rate):
        yield page


async def synth_pcm(runtime, config, text):
    """Синтез целиком в float32 PCM (для звукового кеша): → (numpy-массив, частота)."""
    return vendor.pcm_float(*await _pcm(runtime, config, text))


async def voices(runtime, config, refresh=False):
    """Голоса выбранного движка: Gemini — готовый набор, Cloud TTS — русские голоса из API (кеш на сессию)."""
    if not _cloud(config):
        return GEMINI_OPTIONS
    key = vendor.secret((config or {}).get("api_key")).strip()
    if not key:
        return CLOUD_OPTIONS
    base = (config.get("url") or CLOUD_URL).rstrip("/")
    token = key + base
    cached = vendor.voices_cached(NAME, token, refresh or config.get("refresh"))
    if cached is not None:
        return cached
    result = await vendor.call_json(runtime, NAME, "GET", base + "/voices", key,
                                    headers={"X-Goog-Api-Key": key}, params={"languageCode": "ru-RU"})
    items = []
    for item in result.get("voices") or []:
        name = item.get("name", "")
        if not name.startswith("ru-RU-"):
            continue
        gender = {"FEMALE": "female", "MALE": "male"}.get(item.get("ssmlGender"), "")
        items.append(vendor.option(name, f"{name[6:]} ({GENDER.get(gender, 'нейтральный')})", "ru-RU", gender))
    items.sort(key=lambda o: ("Chirp3" not in o["value"], "Wavenet" not in o["value"], o["value"]))
    return vendor.voices_store(NAME, token, items or CLOUD_OPTIONS)
