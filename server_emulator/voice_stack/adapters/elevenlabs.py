"""ElevenLabs: синтез речи (готовые и свои запечённые голоса) и распознавание Scribe.

  TTS     — POST /v1/text-to-speech/{voice_id}/stream?output_format=pcm_24000, заголовок xi-api-key,
            тело {text, model_id, language_code, voice_settings}; ответ — поток PCM 16 бит:
            https://elevenlabs.io/docs/api-reference/text-to-speech/stream
            https://elevenlabs.io/docs/api-reference/text-to-speech/convert
  голоса  — GET /v2/voices (постранично): https://elevenlabs.io/docs/api-reference/voices/search
  запекание голоса — Instant Voice Clone: POST /v1/voices/add (multipart: name, files), ответ {voice_id};
            голос хранится у ElevenLabs и дальше используется по voice_id; удаление — DELETE /v1/voices/{voice_id}:
            https://elevenlabs.io/docs/api-reference/voices/ivc/create
            https://elevenlabs.io/docs/api-reference/voices/delete
  ASR     — POST /v1/speech-to-text (multipart: model_id, file, language_code), ответ {"text"}:
            https://elevenlabs.io/docs/api-reference/speech-to-text/convert

Ключ — только заголовок xi-api-key; в журналы и тексты ошибок не попадает. Есть бесплатный тариф с месячным
лимитом знаков (для коммерческого использования и клонирования голосов нужен платный: Instant Voice Clone
— от тарифа Starter): https://elevenlabs.io/pricing
"""
import hashlib
import re

from aiohttp import FormData

from . import _vendor as vendor

TITLE = "ElevenLabs (Online)"
DESCRIPTION = "ElevenLabs: синтез речи с готовыми и своими голосами и распознавание Scribe. Платно, есть ограниченный бесплатный тариф."
NAME = "ElevenLabs"
BASE = "https://api.elevenlabs.io"
VOICE_ID = re.compile(r"[A-Za-z0-9]{8,64}")
KEY = {"key": "api_key", "label": "Ключ API", "type": "password", "required": True,
       "link": "https://elevenlabs.io/app/settings/api-keys",
       "help": "elevenlabs.io → Developers → API Keys"}
URL = {"key": "url", "label": "Адрес API", "type": "text", "group": "advanced",
       "help": "Пусто — https://api.elevenlabs.io; для региональных серверов укажите свой хост"}
# Запасной список (без ключа или при сбое запроса): стандартные голоса ElevenLabs
OPTIONS = [vendor.option(*item) for item in (
    ("21m00Tcm4TlvDq8ikWAM", "Rachel (женский)", "en", "female"), ("EXAVITQu4vr4xnSDxMzI", "Bella (женский)", "en", "female"),
    ("MF3mGyEYCl7XYWbV9V6O", "Elli (женский)", "en", "female"), ("pNInz6obpgDQGcFmaJgB", "Adam (мужской)", "en", "male"),
    ("ErXwobaYiN019PkySvjV", "Antoni (мужской)", "en", "male"), ("TxGEqnHWrfWFTfGW9XjX", "Josh (мужской)", "en", "male"))]
YES_NO = [{"value": "yes", "label": "Включено"}, {"value": "no", "label": "Выключено"}]

SERVICES = {
    "tts": {
        "title": "ElevenLabs — синтез (Online)",
        "description": "Синтез ElevenLabs: готовые голоса и свой голос по образцу (Instant Voice Clone). Платно, есть бесплатный лимит.",
        "voice_modes": ["preset", "baked"],
        "bake": {"text": False},
        "fields": [KEY,
                   {"key": "model", "label": "Модель", "type": "select", "options": [
                       {"value": "eleven_multilingual_v2", "label": "Multilingual v2 (качество, русский)"},
                       {"value": "eleven_flash_v2_5", "label": "Flash v2.5 (быстрая)"},
                       {"value": "eleven_turbo_v2_5", "label": "Turbo v2.5 (баланс)"}]},
                   {"key": "voice", "label": "Голос", "type": "select", "options": OPTIONS, "options_from": "voices"},
                   {"key": "language", "label": "Язык", "type": "text", "group": "advanced",
                    "help": "Код языка, например ru; учитывается моделями Flash и Turbo"},
                   {"key": "stability", "label": "Стабильность", "type": "number", "step": 0.05, "group": "advanced",
                    "help": "0–1: ниже — эмоциональнее, выше — ровнее"},
                   {"key": "similarity_boost", "label": "Сходство с голосом", "type": "number", "step": 0.05, "group": "advanced",
                    "help": "0–1"},
                   {"key": "style", "label": "Выразительность", "type": "number", "step": 0.05, "group": "advanced",
                    "help": "0–1: больше — ярче, но медленнее"},
                   {"key": "speed", "label": "Скорость", "type": "number", "step": 0.05, "group": "advanced",
                    "help": "1 — обычная, от 0.7 до 1.2"},
                   {"key": "use_speaker_boost", "label": "Усиление сходства", "type": "select", "group": "advanced",
                    "options": YES_NO},
                   {"key": "sample_rate", "label": "Частота звука, Гц", "type": "select", "group": "advanced", "options": [
                       {"value": "16000", "label": "16000"}, {"value": "22050", "label": "22050"},
                       {"value": "24000", "label": "24000"}]},
                   URL],
        "defaults": {"model": "eleven_multilingual_v2", "voice": "21m00Tcm4TlvDq8ikWAM", "language": "ru",
                     "stability": 0.5, "similarity_boost": 0.75, "style": 0, "speed": 1.0,
                     "use_speaker_boost": "yes", "sample_rate": "24000"},
        "examples": {
            "ElevenLabs Multilingual v2 (Online)": {"model": "eleven_multilingual_v2"},
            "ElevenLabs Flash v2.5 (Online)": {"model": "eleven_flash_v2_5"},
        },
    },
    "asr": {
        "title": "ElevenLabs Scribe — распознавание (Online)",
        "description": "Распознавание речи ElevenLabs Scribe (фраза целиком). Платно, есть ограниченный бесплатный лимит.",
        "fields": [KEY,
                   {"key": "model", "label": "Модель", "type": "select", "options": [
                       {"value": "scribe_v2", "label": "Scribe v2"}, {"value": "scribe_v1", "label": "Scribe v1"}]},
                   {"key": "language", "label": "Язык", "type": "text",
                    "help": "Код языка: ru, en… Пусто — определить автоматически"},
                   URL],
        "defaults": {"model": "scribe_v2", "language": "ru"},
        "examples": {"ElevenLabs Scribe (Online)": {"model": "scribe_v2", "language": "ru"}},
    },
}
MATCH = {"asr": r"elevenlabs\.io/v1/speech-to-text", "tts": r"elevenlabs\.io/v1/text-to-speech"}


def _base(config):
    return (config.get("url") or BASE).rstrip("/")


def _voice(config):
    """Голос запроса: запечённый (режим baked) или выбранный из списка."""
    baked = config.get("voice_mode") == "baked"
    voice = str((config.get("baked_voice_id") if baked else config.get("voice")) or "").strip()
    if baked and not voice:
        raise ValueError(f"{NAME}: свой голос ещё не запечён — нажмите «Запечь»")
    if not VOICE_ID.fullmatch(voice):
        raise ValueError(f"{NAME}: выберите голос из списка")
    return voice


def _rate(config):
    return int(config.get("sample_rate") or 24000)


def _number(config, key, low, high, default):
    value = config.get(key)
    return default if value in (None, "") else max(low, min(high, float(value)))


def _request(config, text):
    model = config.get("model") or "eleven_multilingual_v2"
    settings = {"stability": _number(config, "stability", 0, 1, 0.5),
                "similarity_boost": _number(config, "similarity_boost", 0, 1, 0.75),
                "style": _number(config, "style", 0, 1, 0),
                "use_speaker_boost": config.get("use_speaker_boost") != "no",
                "speed": _number(config, "speed", 0.7, 1.2, 1.0)}
    body = {"text": text, "model_id": model, "voice_settings": settings}
    if config.get("language") and ("flash" in model or "turbo" in model):
        body["language_code"] = str(config["language"])
    return body


async def _source(runtime, config, text):
    key = vendor.api_key(config, NAME)
    url = f"{_base(config)}/v1/text-to-speech/{_voice(config)}/stream"
    async for chunk in vendor.stream_chunks(
            runtime, NAME, "POST", url, key, params={"output_format": f"pcm_{_rate(config)}"},
            headers={"xi-api-key": key}, json=_request(config, text)):
        yield chunk


async def speech(runtime, config, payload):
    pcm = await vendor.collect(_source(runtime, config, payload["input"]))
    return await vendor.pcm_to_ogg(pcm, _rate(config)), "audio/ogg"


def streams(config):
    return True


async def speech_stream(runtime, config, text):
    async for page in vendor.pcm_pages(_source(runtime, config, text), _rate(config)):
        yield page


async def synth_pcm(runtime, config, text):
    """Синтез целиком в float32 PCM (для звукового кеша): → (numpy-массив, частота)."""
    return vendor.pcm_float(await vendor.collect(_source(runtime, config, text)), _rate(config))


async def voices(runtime, config, refresh=False):
    """Голоса аккаунта (готовые, свои, из библиотеки) — постранично, кеш на сессию; без ключа — запасной список."""
    key = vendor.secret((config or {}).get("api_key")).strip()
    if not key:
        return OPTIONS
    base = _base(config)
    token = key + base
    cached = vendor.voices_cached(NAME, token, refresh or config.get("refresh"))
    if cached is not None:
        return cached
    items, page = [], None
    for _ in range(10):
        params = {"page_size": 100, "include_total_count": "false"}
        if page:
            params["next_page_token"] = page
        result = await vendor.call_json(runtime, NAME, "GET", base + "/v2/voices", key,
                                        headers={"xi-api-key": key}, params=params)
        for item in result.get("voices") or []:
            if not item.get("voice_id"):
                continue
            labels = item.get("labels") or {}
            gender = str(labels.get("gender") or "").lower()
            note = {"cloned": "свой", "professional": "профессиональный", "generated": "созданный"}.get(item.get("category"), "")
            title = item.get("name") or item["voice_id"]
            items.append(vendor.option(item["voice_id"], f"{title} ({note})" if note else title,
                                       labels.get("language", ""), gender if gender in ("female", "male") else ""))
        page = result.get("next_page_token")
        if not (result.get("has_more") and page):
            break
    return vendor.voices_store(NAME, token, items or OPTIONS)


async def bake_voice(runtime, config, audio, text=""):
    """Запечь голос: образец (WAV) один раз уходит в Instant Voice Clone, возвращается постоянный voice_id."""
    key = vendor.api_key(config, NAME)
    name = "alica-" + hashlib.sha1(audio).hexdigest()[:8]
    form = FormData()
    form.add_field("name", name)
    form.add_field("files", audio, filename="reference.wav", content_type="audio/wav")
    form.add_field("remove_background_noise", "false")
    result = await vendor.call_json(runtime, NAME, "POST", _base(config) + "/v1/voices/add", key,
                                    headers={"xi-api-key": key}, data=form)
    if not result.get("voice_id"):
        raise ValueError(f"{NAME}: в ответе нет идентификатора голоса")
    return {"voice_id": result["voice_id"], "title": name}


async def unbake_voice(runtime, config, voice_id):
    """Удалить запечённый голос у ElevenLabs."""
    key = vendor.api_key(config, NAME)
    if not VOICE_ID.fullmatch(str(voice_id or "")):
        raise ValueError(f"{NAME}: неверный идентификатор голоса")
    await vendor.call(runtime, NAME, "DELETE", f"{_base(config)}/v1/voices/{voice_id}", key, headers={"xi-api-key": key})


# --- asr (Scribe) ----------------------------------------------------------------------------


async def transcribe(runtime, config, audio, filename):
    key = vendor.api_key(config, NAME)
    form = FormData()
    form.add_field("model_id", config.get("model") or "scribe_v2")
    form.add_field("file", audio, filename=filename or "input.wav",
                   content_type="audio/ogg" if audio[:4] == b"OggS" else "audio/wav")
    if config.get("language"):
        form.add_field("language_code", str(config["language"]))
    form.add_field("tag_audio_events", "false")
    result = await vendor.call_json(runtime, NAME, "POST", _base(config) + "/v1/speech-to-text", key,
                                    headers={"xi-api-key": key}, data=form)
    return str(result.get("text") or "").strip()
