"""Сбер: SaluteSpeech (распознавание и синтез по REST) и GigaChat.

Документация поставщика:
* авторизация: https://developers.sber.ru/docs/ru/salutespeech/api/authentication
  (POST /api/v2/oauth, Basic <ключ авторизации>, RqUID — uuid4, scope=SALUTE_SPEECH_<PERS|B2B|CORP>;
  access_token живёт 30 минут — адаптер обновляет его за минуту до конца срока)
* GigaChat:    https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/post-chat
  (scope=GIGACHAT_API_<PERS|B2B|CORP>; функции модели — поля functions/function_call)
* SaluteSpeech REST: speech:recognize и text:synthesize на smartspeech.sber.ru/rest/v1

Серверы Сбера используют сертификат НУЦ Минцифры, которого нет в хранилище большинства систем:
в «Дополнительно» укажите файл корневого сертификата (CA bundle) или, на свой риск, отключите проверку.
Потоковое распознавание SaluteSpeech — только gRPC (grpcio), поэтому фраза распознаётся целиком.
"""
import asyncio
import hashlib
import json
import ssl
import time
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

from aiohttp import ClientConnectorCertificateError

from ...auth import secret
from . import _speech

TITLE = "Сбер (Online)"
DESCRIPTION = "SaluteSpeech и GigaChat; ключ из Studio (developers.sber.ru). Для физлиц есть бесплатный лимит."
AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
REST_URL = "https://smartspeech.sber.ru/rest/v1"
GIGA_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
KEY = {"key": "api_key", "label": "Ключ авторизации", "type": "password", "required": True,
       "link": "https://developers.sber.ru/studio",
       "help": "Studio → ваш проект → Настройки API → «Получить ключ» (Authorization key, строка Base64)"}
SCOPE = {"key": "scope", "label": "Тип доступа", "type": "select", "required": True, "options": [
    {"value": "PERS", "label": "Физические лица (PERS)"}, {"value": "B2B", "label": "Юрлица, предоплата (B2B)"},
    {"value": "CORP", "label": "Юрлица, постоплата (CORP)"}]}
CERT = [{"key": "ca_bundle", "label": "Сертификаты НУЦ Минцифры (файл .pem / .crt)", "type": "text", "group": "advanced",
         "help": "Путь к корневому сертификату НУЦ Минцифры (russian_trusted_root_ca.crt, с gu.gosuslugi.ru/certificates). Пусто — системное хранилище"},
        {"key": "verify", "label": "Проверка сертификата", "type": "select", "group": "advanced", "options": [
            {"value": "on", "label": "Проверять"}, {"value": "off", "label": "Не проверять (небезопасно)"}]}]
AUTH_FIELD = {"key": "auth_url", "label": "Адрес авторизации", "type": "text", "group": "advanced"}
LANGS = [{"value": "ru-RU", "label": "Русский"}, {"value": "en-US", "label": "English"}, {"value": "kk-KZ", "label": "Қазақша"}]
RATES = ("x-slow", "slow", "medium", "fast", "x-fast")
SERVICES = {
    "asr": {
        "title": "SaluteSpeech — распознавание (Online)",
        "description": "Распознавание речи Сбера (фраза целиком). Платно, у физлиц есть пробный лимит.",
        "fields": [KEY, SCOPE,
                   {"key": "lang", "label": "Язык", "type": "select", "options": LANGS, "group": "advanced"},
                   {"key": "model", "label": "Модель", "type": "select", "group": "advanced", "options": [
                       {"value": "general", "label": "Общая"}, {"value": "media", "label": "Медиа"},
                       {"value": "callcenter", "label": "Колл-центр"}]},
                   *CERT, AUTH_FIELD,
                   {"key": "url", "label": "Адрес сервиса", "type": "text", "group": "advanced"}],
        "defaults": {"url": REST_URL, "auth_url": AUTH_URL, "scope": "PERS", "lang": "ru-RU", "model": "general", "verify": "on"},
    },
    "tts": {
        "title": "SaluteSpeech — синтез (Online)",
        "description": "Синтез речи Сбера: голоса Наталья, Борис, Марфа и другие. Платно, у физлиц есть пробный лимит.",
        "voice_modes": ["preset"],
        "fields": [KEY, SCOPE,
                   {"key": "voice", "label": "Голос", "type": "select", "options": [
                       {"value": "Nec_24000", "label": "Наталья (жен.)"}, {"value": "Bys_24000", "label": "Борис (муж.)"},
                       {"value": "May_24000", "label": "Марфа (жен.)"}, {"value": "Tur_24000", "label": "Тарас (муж.)"},
                       {"value": "Ost_24000", "label": "Александра (жен.)"}, {"value": "Pon_24000", "label": "Сергей (муж.)"}]},
                   {"key": "speed", "label": "Скорость речи", "type": "select", "group": "advanced", "options": [
                       {"value": "x-slow", "label": "Очень медленно"}, {"value": "slow", "label": "Медленно"},
                       {"value": "medium", "label": "Обычная"}, {"value": "fast", "label": "Быстро"},
                       {"value": "x-fast", "label": "Очень быстро"}]},
                   *CERT, AUTH_FIELD,
                   {"key": "url", "label": "Адрес сервиса", "type": "text", "group": "advanced"}],
        "defaults": {"url": REST_URL, "auth_url": AUTH_URL, "scope": "PERS", "voice": "Nec_24000", "speed": "medium", "verify": "on"},
    },
    "llm": {
        "title": "GigaChat (Online Free)",
        "description": "GigaChat Сбера; для физлиц — бесплатный тариф Freemium с лимитом токенов.",
        "fields": [KEY, SCOPE,
                   {"key": "model", "label": "Модель", "type": "select", "options": [
                       {"value": "GigaChat-2", "label": "GigaChat 2 (быстрая)"}, {"value": "GigaChat-2-Pro", "label": "GigaChat 2 Pro"},
                       {"value": "GigaChat-2-Max", "label": "GigaChat 2 Max"}]},
                   {"key": "temperature", "label": "Температура", "type": "number", "step": 0.1, "group": "advanced"},
                   {"key": "max_tokens", "label": "Максимум токенов ответа", "type": "number", "step": 1, "group": "advanced"},
                   *CERT, AUTH_FIELD,
                   {"key": "url", "label": "Адрес сервиса", "type": "text", "group": "advanced"}],
        "defaults": {"url": GIGA_URL, "auth_url": AUTH_URL, "scope": "PERS", "model": "GigaChat-2", "temperature": 0.3,
                     "max_tokens": 512, "verify": "on"},
    },
}
MATCH = {"llm": r"gigachat\.devices\.sberbank\.ru", "asr": r"smartspeech\.sber\.ru", "tts": r"smartspeech\.sber\.ru"}
FAMILY = {"asr": "SALUTE_SPEECH", "tts": "SALUTE_SPEECH", "llm": "GIGACHAT_API"}
VENDOR = {"asr": "SaluteSpeech", "tts": "SaluteSpeech", "llm": "GigaChat"}
_tokens = {}  # sha256(ключ + scope + адрес) → (access_token, срок в секундах time.time())
_contexts = {}  # (путь, mtime) → SSLContext
_locks = {}


def _ssl(config):
    """Параметр ssl запроса: False — не проверять, SSLContext с сертификатом Минцифры, None — по умолчанию."""
    if str(config.get("verify") or "on") == "off":
        return False
    bundle = str(config.get("ca_bundle") or "").strip()
    if not bundle:
        return None
    path = Path(bundle)
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
        if stamp not in _contexts:
            _contexts[stamp] = ssl.create_default_context(cafile=str(path))
        return _contexts[stamp]
    except (OSError, ssl.SSLError):
        raise ValueError("Сбер: не удалось прочитать файл сертификатов — проверьте путь и формат (PEM/CRT)") from None


async def _request(runtime, service, config, method, url, **kwargs):
    try:
        return await _speech.call(runtime, VENDOR[service], method, url, ssl=_ssl(config), **kwargs)
    except ValueError:
        raise
    except ClientConnectorCertificateError:
        raise ValueError("Сбер: сертификат сервера не доверенный — в «Дополнительно» укажите сертификаты НУЦ Минцифры") from None
    except ssl.SSLError:
        raise ValueError("Сбер: ошибка TLS — проверьте сертификаты НУЦ Минцифры в «Дополнительно»") from None


async def token(runtime, service, config, fresh=False):
    """access_token по ключу авторизации (Basic): кешируется до истечения (с запасом минута), секреты не логируются."""
    key = secret(config.get("api_key")).strip()
    if not key:
        raise ValueError("Сбер: укажите ключ авторизации")
    scope = str(config.get("scope") or "PERS")
    if scope not in ("PERS", "B2B", "CORP"):
        raise ValueError("Сбер: тип доступа — PERS, B2B или CORP")
    url = config.get("auth_url") or AUTH_URL
    ident = hashlib.sha256("\0".join((key, scope, url, FAMILY[service])).encode()).hexdigest()
    async with _locks.setdefault(ident, asyncio.Lock()):
        cached = _tokens.get(ident)
        if cached and not fresh and cached[1] - 60 > time.time():
            return cached[0]
        status, raw, _ = await _request(
            runtime, service, config, "POST", url, data={"scope": f"{FAMILY[service]}_{scope}"},
            headers={"Authorization": "Basic " + key, "RqUID": str(uuid.uuid4()), "Accept": "application/json"})
        if status != 200:
            raise _speech.failure(VENDOR[service] + " (получение токена)", status, raw)
        try:
            data = json.loads(raw)
            value = data["access_token"]
            expires = float(data.get("expires_at") or 0)
        except (ValueError, KeyError, TypeError):
            raise ValueError(VENDOR[service] + ": непонятный ответ авторизации") from None
        expires = expires / 1000 if expires > 1e11 else expires  # expires_at — миллисекунды
        _tokens[ident] = (value, expires if expires > time.time() else time.time() + 25 * 60)
        return value


async def _authorized(runtime, service, config, method, url, **kwargs):
    """Запрос с токеном; 401 (токен отозван раньше срока) — одна повторная попытка с новым токеном."""
    base = kwargs.pop("headers", {})
    for attempt in (0, 1):
        headers = {**base, "Authorization": "Bearer " + await token(runtime, service, config, fresh=bool(attempt))}
        result = await _request(runtime, service, config, method, url, headers=headers, **kwargs)
        if result[0] != 401 or attempt:
            return result


# --- SaluteSpeech: распознавание --------------------------------------------------------
async def transcribe(runtime, config, audio, filename):
    if audio[:4] == b"OggS":
        content, body = "audio/ogg;codecs=opus", audio
    else:
        content, body = "audio/x-pcm;bit=16;rate=16000", await _speech.pcm16_from(audio, filename)
    base = (config.get("url") or REST_URL).rstrip("/")
    status, raw, _ = await _authorized(
        runtime, "asr", config, "POST", base + "/speech:recognize", data=body, headers={"Content-Type": content},
        params={"language": config.get("lang") or "ru-RU", "model": config.get("model") or "general"})
    if status != 200:
        raise _speech.failure("SaluteSpeech", status, raw)
    try:
        result = json.loads(raw).get("result") or ""
    except (ValueError, AttributeError):
        raise ValueError("SaluteSpeech: непонятный ответ распознавания") from None
    return " ".join(result if isinstance(result, list) else [str(result)]).strip()


# --- SaluteSpeech: синтез ---------------------------------------------------------------
def _rate(config):
    voice = str(config.get("voice") or "Nec_24000")
    tail = voice.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 24000


def streams(config):
    return True


async def _pcm_chunks(runtime, config, text):
    speed = str(config.get("speed") or "medium")
    if speed in RATES and speed != "medium":
        body, content = f'<speak><prosody rate="{speed}">{escape(text)}</prosody></speak>', "application/ssml"
    else:
        body, content = text, "application/text"
    base = (config.get("url") or REST_URL).rstrip("/")
    status, raw, _ = await _authorized(
        runtime, "tts", config, "POST", base + "/text:synthesize", data=body.encode("utf-8"),
        headers={"Content-Type": content}, params={"format": "pcm16", "voice": str(config.get("voice") or "Nec_24000")})
    if status != 200:
        raise _speech.failure("SaluteSpeech", status, raw)
    yield _speech.strip_wav(raw)


async def synth_pcm(runtime, config, text):
    return await _speech.pcm_float(_pcm_chunks(runtime, config, text)), _rate(config)


async def speech_stream(runtime, config, text):
    async for page in _speech.pcm_pages(_pcm_chunks(runtime, config, text), _rate(config)):
        yield page


async def speech(runtime, config, payload):
    return b"".join([page async for page in speech_stream(runtime, config, payload.get("input", ""))]), "audio/ogg"


# --- GigaChat ---------------------------------------------------------------------------
def to_giga(payload, config):
    """OpenAI chat → GigaChat: tools → functions, tool_calls → function_call, tool → function."""
    names, messages = {}, []
    for message in payload.get("messages", []):
        role, content = message.get("role"), message.get("content") or ""
        if role == "assistant" and message.get("tool_calls"):
            call = message["tool_calls"][0].get("function", {})
            for item in message["tool_calls"]:
                names[item.get("id", "")] = (item.get("function") or {}).get("name", "")
            arguments = call.get("arguments") or "{}"
            messages.append({"role": "assistant", "content": content, "function_call": {
                "name": call.get("name", ""), "arguments": json.loads(arguments) if isinstance(arguments, str) else arguments}})
        elif role == "tool":
            messages.append({"role": "function", "name": names.get(message.get("tool_call_id", ""), ""),
                             "content": json.dumps({"result": str(content)}, ensure_ascii=False)})
        elif role in ("system", "user", "assistant"):
            messages.append({"role": role, "content": content})
    body = {"model": config.get("model") or "GigaChat-2", "messages": messages,
            "temperature": float(payload.get("temperature", config.get("temperature", 0.3)))}
    if config.get("max_tokens") not in (None, ""):
        body["max_tokens"] = int(config["max_tokens"])
    if payload.get("tools"):
        body["functions"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                              "parameters": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                             for t in payload["tools"] if t.get("type") == "function"]
        body["function_call"] = "auto"
    return body


def from_giga(result):
    """GigaChat → OpenAI {"choices": [{"message"}], "usage"}."""
    choice = (result.get("choices") or [{}])[0].get("message") or {}
    message = {"role": "assistant", "content": choice.get("content") or ""}
    call = choice.get("function_call")
    if call and call.get("name"):
        arguments = call.get("arguments", {})
        message["tool_calls"] = [{"id": "call_" + uuid.uuid4().hex[:12], "type": "function", "function": {
            "name": call["name"], "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)}}]
    usage = result.get("usage") or {}
    return {"choices": [{"message": message}],
            "usage": {"prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0)}}


async def complete(runtime, config, payload):
    status, raw, _ = await _authorized(runtime, "llm", config, "POST", config.get("url") or GIGA_URL,
                                       json=to_giga(payload, config), headers={"Content-Type": "application/json"})
    if status != 200:
        raise _speech.failure("GigaChat", status, raw)
    try:
        return from_giga(json.loads(raw))
    except (ValueError, AttributeError):
        raise ValueError("GigaChat: непонятный ответ модели") from None
