"""Яндекс: SpeechKit (распознавание и синтез по REST v1) и YandexGPT (OpenAI-совместимый чат).

Документация поставщика:
* синтез:        https://aistudio.yandex.ru/docs/en/speechkit/tts/request   (POST tts:synthesize, form-urlencoded)
* голоса:        https://aistudio.yandex.ru/docs/en/speechkit/tts/voices
* распознавание: https://aistudio.yandex.ru/docs/en/speechkit/stt/request   (синхронное, до 30 с и 1 МБ)
* YandexGPT:     https://aistudio.yandex.ru/docs/en/ai-studio/text-generation/api-ref/TextGeneration/completion.html

Авторизация — заголовок «Authorization: Api-Key <ключ>» (ключ сервисного аккаунта), для чата — «Bearer»
и «OpenAI-Project: <каталог>» (OpenAI-совместимый интерфейс). Потоковое распознавание SpeechKit в Яндексе
доступно только по gRPC (требует grpcio), поэтому здесь распознаётся фраза целиком — для голосового
помощника этого достаточно: записанная реплика короче 30 секунд.
"""
import json

from ...auth import secret
from . import _speech

TITLE = "Яндекс (Online)"
DESCRIPTION = "Yandex SpeechKit и YandexGPT: платные, оплата по тарифам Yandex Cloud."
KEY_LINK = "https://yandex.cloud/ru/docs/iam/operations/authentication/manage-api-keys"
KEY = {"key": "api_key", "label": "API-ключ", "type": "password", "required": True, "link": KEY_LINK,
       "help": "Консоль Yandex Cloud → Сервисные аккаунты → API-ключ (права ai.speechkit-stt.user / ai.speechkit-tts.user / ai.languageModels.user)"}
FOLDER = {"key": "folder_id", "label": "Идентификатор каталога",
          "type": "text", "help": "Консоль Yandex Cloud → название каталога → значок копирования. Для ключа сервисного аккаунта не обязателен"}
FOLDER_REQUIRED = {**FOLDER, "required": True, "help": "Консоль Yandex Cloud → название каталога → значок копирования; нужен для адреса модели"}
LANGS = [{"value": "ru-RU", "label": "Русский"}, {"value": "en-US", "label": "English"},
         {"value": "kk-KZ", "label": "Қазақша"}, {"value": "uz-UZ", "label": "O'zbek"}]
STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
LLM_URL = "https://llm.api.cloud.yandex.net/v1/chat/completions"
# голос → допустимые эмоции/роли (https://aistudio.yandex.ru/docs/en/speechkit/tts/voices)
ROLES = {"ermil": ("neutral", "good"), "jane": ("neutral", "good", "evil"), "omazh": ("neutral", "evil"),
         "zahar": ("neutral", "good"), "marina": ("neutral", "whisper", "friendly")}
SERVICES = {
    "asr": {
        "title": "Yandex SpeechKit — распознавание (Online)",
        "description": "Распознавание речи Яндекса (фраза целиком, до 30 с). Платно.",
        "fields": [KEY, FOLDER,
                   {"key": "lang", "label": "Язык", "type": "select", "options": LANGS, "group": "advanced"},
                   {"key": "topic", "label": "Модель распознавания", "type": "select", "group": "advanced", "options": [
                       {"value": "general", "label": "Общая"}, {"value": "general:rc", "label": "Общая (новая, RC)"}]},
                   {"key": "url", "label": "Адрес сервиса", "type": "text", "group": "advanced"}],
        "defaults": {"url": STT_URL, "lang": "ru-RU", "topic": "general", "folder_id": ""},
    },
    "tts": {
        "title": "Yandex SpeechKit — синтез (Online)",
        "description": "Синтез речи Яндекса: голоса Филипп, Ермил, Джейн и другие. Платно.",
        "voice_modes": ["preset"],
        "fields": [KEY, FOLDER,
                   {"key": "voice", "label": "Голос", "type": "select", "options": [
                       {"value": "filipp", "label": "Филипп (муж.)"}, {"value": "ermil", "label": "Ермил (муж.)"},
                       {"value": "zahar", "label": "Захар (муж.)"}, {"value": "madi_ru", "label": "Мади (муж.)"},
                       {"value": "jane", "label": "Джейн (жен.)"}, {"value": "omazh", "label": "Омаж (жен.)"},
                       {"value": "marina", "label": "Марина (жен.)"}, {"value": "alena", "label": "Алёна (жен., старый голос)"}]},
                   {"key": "emotion", "label": "Эмоция / роль", "type": "select", "options": [
                       {"value": "", "label": "По умолчанию"}, {"value": "neutral", "label": "Нейтрально"},
                       {"value": "good", "label": "Радостно"}, {"value": "evil", "label": "Недовольно"},
                       {"value": "friendly", "label": "Дружелюбно"}, {"value": "whisper", "label": "Шёпотом"}]},
                   {"key": "speed", "label": "Скорость речи", "type": "number", "step": 0.1, "group": "advanced"},
                   {"key": "sample_rate", "label": "Частота звука, Гц", "type": "select", "group": "advanced",
                    "options": [{"value": "48000", "label": "48000"}, {"value": "16000", "label": "16000"}, {"value": "8000", "label": "8000"}]},
                   {"key": "url", "label": "Адрес сервиса", "type": "text", "group": "advanced"}],
        "defaults": {"url": TTS_URL, "voice": "filipp", "emotion": "", "speed": 1.0, "sample_rate": "48000", "folder_id": ""},
    },
    "llm": {
        "title": "YandexGPT (Online)",
        "description": "YandexGPT через OpenAI-совместимый интерфейс Yandex AI Studio. Платно.",
        "fields": [KEY, FOLDER_REQUIRED,
                   {"key": "model", "label": "Модель", "type": "select", "options": [
                       {"value": "yandexgpt-lite/latest", "label": "YandexGPT Lite (быстрая)"},
                       {"value": "yandexgpt/latest", "label": "YandexGPT Pro"},
                       {"value": "yandexgpt/rc", "label": "YandexGPT Pro (RC)"}]},
                   {"key": "temperature", "label": "Температура", "type": "number", "step": 0.1, "group": "advanced"},
                   {"key": "max_tokens", "label": "Максимум токенов ответа", "type": "number", "step": 1, "group": "advanced"},
                   {"key": "url", "label": "Адрес сервиса", "type": "text", "group": "advanced"}],
        "defaults": {"url": LLM_URL, "model": "yandexgpt-lite/latest", "temperature": 0.3, "max_tokens": 512, "folder_id": ""},
    },
}
MATCH = {"llm": r"llm\.api\.cloud\.yandex\.net|ai\.api\.cloud\.yandex\.net",
         "asr": r"stt\.api\.cloud\.yandex\.net", "tts": r"tts\.api\.cloud\.yandex\.net"}


def _key(config):
    key = secret(config.get("api_key")).strip()
    if not key:
        raise ValueError("Яндекс: укажите API-ключ")
    return key


def _folder(config, required=False):
    folder = str(config.get("folder_id") or "").strip()
    if required and not folder:
        raise ValueError("Яндекс: укажите идентификатор каталога")
    return folder


# --- распознавание ----------------------------------------------------------------------
async def transcribe(runtime, config, audio, filename):
    """Фраза целиком (≤30 с, ≤1 МБ): Ogg Opus отправляется как есть, WAV — как PCM 16 кГц."""
    key = _key(config)
    if audio[:4] == b"OggS":
        params, body = {"format": "oggopus"}, audio
    else:
        body = await _speech.pcm16_from(audio, filename)
        params = {"format": "lpcm", "sampleRateHertz": "16000"}
    if len(body) > 1024 ** 2:
        raise ValueError("Яндекс: запись длиннее лимита распознавания (30 секунд, 1 МБ)")
    params.update(lang=config.get("lang") or "ru-RU", topic=config.get("topic") or "general")
    if _folder(config):
        params["folderId"] = _folder(config)
    status, raw, _ = await _speech.call(runtime, "Яндекс SpeechKit", "POST", config.get("url") or STT_URL,
                                        params=params, data=body, headers={"Authorization": "Api-Key " + key})
    if status != 200:
        raise _speech.failure("Яндекс SpeechKit", status, raw)
    try:
        return str(json.loads(raw).get("result") or "").strip()
    except (ValueError, AttributeError):
        raise ValueError("Яндекс SpeechKit: непонятный ответ распознавания") from None


# --- синтез -----------------------------------------------------------------------------
def _form(config, text):
    voice = str(config.get("voice") or "filipp")
    form = {"text": text, "lang": "ru-RU", "voice": voice, "format": "lpcm",
            "sampleRateHertz": str(config.get("sample_rate") or 48000)}
    emotion = str(config.get("emotion") or "")
    if emotion and emotion in ROLES.get(voice, ()):  # голос без этой роли — без эмоции, а не ошибка 400
        form["emotion"] = emotion
    speed = config.get("speed")
    if speed not in (None, "") and float(speed) != 1.0:
        form["speed"] = str(min(3.0, max(0.1, float(speed))))
    if _folder(config):
        form["folderId"] = _folder(config)
    return form


def streams(config):
    return True


async def _pcm_chunks(runtime, config, text):
    key = _key(config)
    response = await _speech.stream_call(runtime, "Яндекс SpeechKit", "POST", config.get("url") or TTS_URL,
                                         data=_form(config, text), headers={"Authorization": "Api-Key " + key})
    try:
        async for chunk in _speech.pcm_source(response):
            yield chunk
    finally:
        response.release()


async def synth_pcm(runtime, config, text):
    """Синтез целиком в float32 PCM (звуковой кеш): → (numpy-массив, частота)."""
    return await _speech.pcm_float(_pcm_chunks(runtime, config, text)), int(config.get("sample_rate") or 48000)


async def speech_stream(runtime, config, text):
    async for page in _speech.pcm_pages(_pcm_chunks(runtime, config, text), int(config.get("sample_rate") or 48000)):
        yield page


async def speech(runtime, config, payload):
    pages = [page async for page in speech_stream(runtime, config, payload.get("input", ""))]
    return b"".join(pages), "audio/ogg"


# --- YandexGPT --------------------------------------------------------------------------
def _model_uri(config):
    model = str(config.get("model") or "yandexgpt-lite/latest").strip()
    return model if model.startswith(("gpt://", "ds://")) else f"gpt://{_folder(config, True)}/{model}"


async def complete(runtime, config, payload):
    key = _key(config)
    body = {k: v for k, v in payload.items() if k not in ("model",)}
    body["model"] = _model_uri(config)
    if config.get("max_tokens") not in (None, ""):
        body.setdefault("max_tokens", int(config["max_tokens"]))
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if _folder(config):
        headers["OpenAI-Project"] = _folder(config)
    status, raw, _ = await _speech.call(runtime, "YandexGPT", "POST", config.get("url") or LLM_URL, json=body, headers=headers)
    if status != 200:
        raise _speech.failure("YandexGPT", status, raw)
    try:
        return json.loads(raw)
    except ValueError:
        raise ValueError("YandexGPT: непонятный ответ модели") from None
