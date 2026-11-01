"""T-Bank VoiceKit: распознавание и синтез речи по REST (JSON, base64).

Документация и примеры поставщика: https://github.com/Tinkoff/voicekit-examples
(recognize_rest.py: POST /v1/stt:recognize; авторизация — JWT HS256, заголовок kid = API-ключ,
подпись секретным ключом (base64url), iss=test_issuer, sub=test_user, aud=tinkoff.cloud.stt|tinkoff.cloud.tts,
срок — 10 минут; токен кешируется до истечения). Потоковый режим VoiceKit — только gRPC (grpcio),
поэтому фраза распознаётся целиком; синтез отдаётся целиком и режется на страницы Ogg Opus.
"""
import base64
import hashlib
import hmac
import json
import time

from ...auth import secret
from . import _speech

TITLE = "T-Bank VoiceKit (Online)"
DESCRIPTION = "Распознавание и синтез речи T-Bank VoiceKit. Платно, есть пробный лимит."
ENDPOINT = "https://api.tinkoff.ai"
KEY_LINK = "https://www.tbank.ru/business/voicekit/"
API_KEY = {"key": "api_key", "label": "API-ключ", "type": "password", "required": True, "link": KEY_LINK,
           "help": "Личный кабинет VoiceKit → ключи доступа → API key"}
SECRET_KEY = {"key": "secret_key", "label": "Секретный ключ", "type": "password", "required": True, "link": KEY_LINK,
              "help": "Личный кабинет VoiceKit → ключи доступа → Secret key (показывается один раз)"}
ENDPOINT_FIELD = {"key": "endpoint", "label": "Адрес сервиса", "type": "text", "group": "advanced"}
SERVICES = {
    "asr": {
        "title": "T-Bank VoiceKit — распознавание (Online)",
        "description": "Распознавание речи T-Bank VoiceKit (фраза целиком). Платно.",
        "fields": [API_KEY, SECRET_KEY, ENDPOINT_FIELD],
        "defaults": {"endpoint": ENDPOINT},
    },
    "tts": {
        "title": "T-Bank VoiceKit — синтез (Online)",
        "description": "Синтез речи T-Bank VoiceKit: голоса Алёна, Максим и другие. Платно.",
        "voice_modes": ["preset"],
        "fields": [API_KEY, SECRET_KEY,
                   {"key": "voice", "label": "Голос", "type": "select", "options": [
                       {"value": "alyona", "label": "Алёна (жен.)"}, {"value": "maxim", "label": "Максим (муж.)"},
                       {"value": "dorofeev", "label": "Дорофеев (муж.)"}]},
                   ENDPOINT_FIELD,
                   {"key": "sample_rate", "label": "Частота звука, Гц", "type": "select", "group": "advanced", "options": [
                       {"value": "24000", "label": "24000"}, {"value": "48000", "label": "48000"},
                       {"value": "16000", "label": "16000"}, {"value": "8000", "label": "8000"}]}],
        "defaults": {"endpoint": ENDPOINT, "voice": "alyona", "sample_rate": "24000"},
    },
}
MATCH = {"asr": r"api\.tinkoff\.ai", "tts": r"api\.tinkoff\.ai"}
_tokens = {}  # sha256(api + secret + aud) → (JWT, exp)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(api_key, secret_key, audience, now=None, lifetime=600):
    """HS256 JWT VoiceKit; подписывается base64url-декодированным секретным ключом."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": api_key}, separators=(",", ":")).encode())
    exp = int((now or time.time()) + lifetime)
    payload = _b64(json.dumps({"iss": "test_issuer", "sub": "test_user", "aud": audience, "exp": exp},
                              separators=(",", ":")).encode())
    try:
        standard = secret_key.replace("-", "+").replace("_", "/")
        key = base64.b64decode(standard + "=" * (-len(standard) % 4), validate=True)
    except ValueError:
        raise ValueError("T-Bank: секретный ключ должен быть строкой Base64 из кабинета VoiceKit") from None
    return f"{header}.{payload}.{_b64(hmac.new(key, f'{header}.{payload}'.encode(), hashlib.sha256).digest())}", exp


def token(config, audience):
    api_key, secret_key = secret(config.get("api_key")).strip(), secret(config.get("secret_key")).strip()
    if not api_key or not secret_key:
        raise ValueError("T-Bank: укажите API-ключ и секретный ключ")
    ident = hashlib.sha256("\0".join((api_key, secret_key, audience)).encode()).hexdigest()
    cached = _tokens.get(ident)
    if cached and cached[1] - 60 > time.time():
        return cached[0]
    _tokens[ident] = jwt(api_key, secret_key, audience)
    return _tokens[ident][0]


def _url(config, path):
    return (config.get("endpoint") or ENDPOINT).rstrip("/") + path


async def _post(runtime, config, audience, path, body):
    status, raw, _ = await _speech.call(
        runtime, "T-Bank VoiceKit", "POST", _url(config, path), json=body,
        headers={"Authorization": "Bearer " + token(config, audience)})
    if status != 200:
        raise _speech.failure("T-Bank VoiceKit", status, raw)
    try:
        return json.loads(raw)
    except ValueError:
        raise ValueError("T-Bank VoiceKit: непонятный ответ сервиса") from None


async def transcribe(runtime, config, audio, filename):
    """Фраза целиком: звук устройства → PCM 16 кГц → /v1/stt:recognize (LINEAR16, base64)."""
    pcm = await _speech.pcm16_from(audio, filename)
    result = await _post(runtime, config, "tinkoff.cloud.stt", "/v1/stt:recognize", {
        "config": {"encoding": "LINEAR16", "sample_rate_hertz": 16000, "num_channels": 1},
        "audio": {"content": base64.b64encode(pcm).decode()}})
    parts = []
    for item in result.get("results") or []:
        alternatives = item.get("alternatives") or [{}]
        parts.append(str(alternatives[0].get("transcript") or "").strip())
    return " ".join(p for p in parts if p)


def streams(config):
    return True


def _rate(config):
    return int(config.get("sample_rate") or 24000)


async def _pcm_chunks(runtime, config, text):
    result = await _post(runtime, config, "tinkoff.cloud.tts", "/v1/tts:synthesize", {
        "input": {"text": text}, "voice": {"name": str(config.get("voice") or "alyona")},
        "audio_config": {"audio_encoding": "LINEAR16", "sample_rate_hertz": _rate(config)}})
    try:
        yield _speech.strip_wav(base64.b64decode(result["audio_content"]))
    except (KeyError, ValueError, TypeError):
        raise ValueError("T-Bank VoiceKit: в ответе нет звука") from None


async def synth_pcm(runtime, config, text):
    return await _speech.pcm_float(_pcm_chunks(runtime, config, text)), _rate(config)


async def speech_stream(runtime, config, text):
    async for page in _speech.pcm_pages(_pcm_chunks(runtime, config, text), _rate(config)):
        yield page


async def speech(runtime, config, payload):
    return b"".join([page async for page in speech_stream(runtime, config, payload.get("input", ""))]), "audio/ogg"
