"""Microsoft: Edge TTS (без ключа) и Azure Speech (синтез и распознавание по REST).

  Azure TTS   — POST <регион>.tts.speech.microsoft.com/cognitiveservices/v1, SSML, заголовок
                Ocp-Apim-Subscription-Key, формат X-Microsoft-OutputFormat raw-*-16bit-mono-pcm:
                https://learn.microsoft.com/azure/ai-services/speech-service/rest-text-to-speech
                список голосов — GET <регион>.tts.speech.microsoft.com/cognitiveservices/voices/list (там же)
  Azure STT   — POST <регион>.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1
                ?language=ru-RU, WAV PCM 16 кГц (до 60 с), ответ {"RecognitionStatus", "DisplayText"}:
                https://learn.microsoft.com/azure/ai-services/speech-service/rest-speech-to-text-short
  Edge TTS    — НЕОФИЦИАЛЬНЫЙ протокол: WebSocket службы «Читать вслух» браузера Edge
                (wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1), те же SSML и
                форматы, что у Azure. Публичного описания нет, служба может измениться или перестать работать;
                ключ не нужен. Реализация — на aiohttp, без сторонних пакетов.

Звук от обоих синтезаторов — PCM 16 бит 24 кГц (Azure — на выбор): конвейер сам кодирует его в Ogg Opus.
Ключ Azure уходит только заголовком запроса и в журналы и тексты ошибок не попадает.
Бесплатный уровень Azure F0: ~0,5 млн символов нейронного синтеза и 5 часов распознавания в месяц
(https://azure.microsoft.com/pricing/details/cognitive-services/speech-services/); уровень S0 — по тарифу.
"""
import asyncio
import datetime
import hashlib
import re
import time
import uuid
from xml.sax.saxutils import escape

from aiohttp import ClientError, WSMsgType, WSServerHandshakeError

from . import _vendor as vendor

TITLE = "Microsoft (Edge TTS, Azure Speech)"
DESCRIPTION = "Edge TTS — бесплатно, без ключа, неофициальный протокол; Azure Speech — уровень F0 бесплатно, S0 платно."
NAME = "Microsoft"
AZURE_TTS = "https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
AZURE_STT = "https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
EDGE_URL = "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1"
EDGE_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"  # gitleaks:allow — публичный токен клиента Edge, одинаковый у всех (не секрет)
EDGE_VERSION = "1-130.0.2849.68"
EDGE_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
RATE = 24000
VOICE = re.compile(r"[A-Za-z]{2,3}-[A-Za-z0-9]{2,4}-[A-Za-z0-9-]+")
TOKEN = re.compile(r"[A-Za-z0-9._-]+")

REGION = {"key": "region", "label": "Регион", "type": "text", "required": True,
          "help": "Регион ресурса Speech в Azure, например westeurope или eastus"}
KEY = {"key": "api_key", "label": "Ключ Azure Speech", "type": "password", "required": True,
       "link": "https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices",
       "help": "Портал Azure → ресурс Speech → Keys and Endpoint → KEY 1"}
EDGE_VOICES = [("ru-RU-SvetlanaNeural", "Светлана", "female"), ("ru-RU-DmitryNeural", "Дмитрий", "male"),
               ("ru-RU-DariyaNeural", "Дарья", "female")]
GENDER = {"female": "женский", "male": "мужской"}
OPTIONS = [vendor.option(name, f"{label} ({GENDER[gender]})", "ru-RU", gender) for name, label, gender in EDGE_VOICES]
LANGUAGES = [{"value": "ru-RU", "label": "Русский"}, {"value": "en-US", "label": "English (US)"},
             {"value": "uk-UA", "label": "Українська"}, {"value": "kk-KZ", "label": "Қазақша"}]

SERVICES = {
    "tts": {
        "title": "Edge TTS и Azure Speech (Online Free)",
        "description": ("Edge — бесплатно и без ключа, но протокол неофициальный и может перестать работать; "
                        "Azure Speech: F0 бесплатно с лимитами, S0 платно."),
        "voice_modes": ["preset"],
        "fields": [{"key": "engine", "label": "Движок", "type": "select", "options": [
                       {"value": "edge", "label": "Edge (без ключа, неофициальный)"},
                       {"value": "azure", "label": "Azure Speech"}]},
                   {**KEY, "when": {"engine": "azure"}},
                   {**REGION, "when": {"engine": "azure"}},
                   {"key": "voice", "label": "Голос", "type": "select", "options": OPTIONS, "options_from": "voices"},
                   {"key": "style", "label": "Стиль", "type": "text", "group": "advanced", "when": {"engine": "azure"},
                    "help": "Если голос поддерживает, например cheerful или friendly"},
                   {"key": "rate", "label": "Скорость, %", "type": "number", "step": 5, "group": "advanced",
                    "help": "0 — обычная, 10 — быстрее на 10 %, −10 — медленнее"},
                   {"key": "pitch", "label": "Тон, Гц", "type": "number", "step": 1, "group": "advanced",
                    "help": "0 — обычный, положительные значения — выше"},
                   {"key": "sample_rate", "label": "Частота звука, Гц", "type": "select", "group": "advanced",
                    "when": {"engine": "azure"}, "options": [
                        {"value": "16000", "label": "16000"}, {"value": "24000", "label": "24000"},
                        {"value": "48000", "label": "48000"}]},
                   {"key": "url", "label": "Адрес сервиса", "type": "text", "group": "advanced",
                    "help": "Пусто — официальный адрес; для Azure с собственным доменом — полный адрес …/cognitiveservices/v1"}],
        "defaults": {"engine": "edge", "voice": "ru-RU-SvetlanaNeural", "region": "westeurope", "style": "",
                     "rate": 0, "pitch": 0, "sample_rate": "24000"},
        "examples": {
            "Edge TTS (Online Free)": {"engine": "edge", "voice": "ru-RU-SvetlanaNeural"},
            "Azure Speech F0 (Online Free)": {"engine": "azure", "voice": "ru-RU-SvetlanaNeural", "region": "westeurope"},
            "Azure Speech S0 (Online)": {"engine": "azure", "voice": "ru-RU-DmitryNeural", "region": "westeurope"},
        },
    },
    "asr": {
        "title": "Azure Speech — распознавание (Online Free)",
        "description": "Распознавание речи Azure (фраза целиком, до 60 с): уровень F0 — 5 часов в месяц бесплатно, S0 — платно.",
        "fields": [KEY, REGION,
                   {"key": "language", "label": "Язык", "type": "select", "options": LANGUAGES},
                   {"key": "profanity", "label": "Нецензурная лексика", "type": "select", "group": "advanced", "options": [
                       {"value": "masked", "label": "Закрывать звёздочками"}, {"value": "removed", "label": "Убирать"},
                       {"value": "raw", "label": "Оставлять"}]},
                   {"key": "url", "label": "Адрес сервиса", "type": "text", "group": "advanced",
                    "help": "Пусто — официальный адрес региона; для Azure с собственным доменом — полный адрес …/cognitiveservices/v1"}],
        "defaults": {"region": "westeurope", "language": "ru-RU", "profanity": "masked"},
        "examples": {"Azure Speech F0 (Online Free)": {"language": "ru-RU", "region": "westeurope"},
                     "Azure Speech S0 (Online)": {"language": "ru-RU", "region": "westeurope"}},
    },
}
MATCH = {"asr": r"stt\.speech\.microsoft\.com", "tts": r"tts\.speech\.microsoft\.com|speech\.platform\.bing\.com"}


def _edge(config):
    return (config or {}).get("engine") != "azure"


def _region(config):
    region = str(config.get("region") or "").strip().lower()
    if not TOKEN.fullmatch(region):
        raise ValueError(f"{NAME}: укажите регион ресурса Speech, например westeurope")
    return region


def _tts_url(config):
    return config.get("url") or AZURE_TTS.format(region=_region(config))


def _rate(config):
    return RATE if _edge(config) else int(config.get("sample_rate") or RATE)


def _ssml(config, text):
    voice = str(config.get("voice") or "ru-RU-SvetlanaNeural")
    if not VOICE.fullmatch(voice):
        raise ValueError(f"{NAME}: неверное имя голоса «{voice[:40]}»")
    inner = escape(text)
    attrs = ""
    rate, pitch = float(config.get("rate") or 0), float(config.get("pitch") or 0)
    if rate:
        attrs += f" rate='{rate:+g}%'"
    if pitch:
        attrs += f" pitch='{pitch:+g}Hz'"
    if attrs:
        inner = f"<prosody{attrs}>{inner}</prosody>"
    style = str(config.get("style") or "").strip()
    if style and not _edge(config):
        if not re.fullmatch(r"[A-Za-z0-9-]+", style):
            raise ValueError(f"{NAME}: стиль — латиница, например cheerful")
        inner = f"<mstts:express-as style='{style}'>{inner}</mstts:express-as>"
    lang = "-".join(voice.split("-")[:2])
    return (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
            f"xmlns:mstts='https://www.w3.org/2001/mstts' xml:lang='{lang}'>"
            f"<voice name='{voice}'>{inner}</voice></speak>")


# --- Azure ---------------------------------------------------------------------------------


async def _azure_source(runtime, config, text):
    key = vendor.api_key(config, NAME)
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
               "X-Microsoft-OutputFormat": f"raw-{_rate(config) // 1000}khz-16bit-mono-pcm",
               "User-Agent": "alice-emulator"}
    async for chunk in vendor.stream_chunks(runtime, NAME, "POST", _tts_url(config), key,
                                            headers=headers, data=_ssml(config, text).encode("utf-8")):
        yield chunk


# --- Edge ----------------------------------------------------------------------------------


def _gec():
    """Sec-MS-GEC: SHA-256 от времени (округлено до 5 минут, в «тиках» Windows) и токена клиента."""
    ticks = time.time() + 11644473600
    ticks -= ticks % 300
    return hashlib.sha256(f"{ticks * 1e7:.0f}{EDGE_TOKEN}".encode("ascii")).hexdigest().upper()


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")


def _edge_address(config):
    if config.get("url"):
        return config["url"]
    return (f"{EDGE_URL}?TrustedClientToken={EDGE_TOKEN}&Sec-MS-GEC={_gec()}&Sec-MS-GEC-Version={EDGE_VERSION}"
            f"&ConnectionId={uuid.uuid4().hex}")


async def _edge_source(runtime, config, text):
    ssml = _ssml(config, text)
    settings = ('{"context":{"synthesis":{"audio":{"metadataoptions":{"sentenceBoundaryEnabled":"false",'
                '"wordBoundaryEnabled":"false"},"outputFormat":"raw-24khz-16bit-mono-pcm"}}}}')
    headers = {"Pragma": "no-cache", "Cache-Control": "no-cache", "User-Agent": EDGE_AGENT,
               "Origin": "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold", "Accept-Language": "en-US,en;q=0.9"}
    got = False
    try:
        async with runtime.lifecycle.client_session().ws_connect(
                _edge_address(config), headers=headers, max_msg_size=0, heartbeat=20) as ws:
            await ws.send_str(f"X-Timestamp:{_stamp()}\r\nContent-Type:application/json; charset=utf-8\r\n"
                              f"Path:speech.config\r\n\r\n{settings}")
            await ws.send_str(f"X-RequestId:{uuid.uuid4().hex}\r\nContent-Type:application/ssml+xml\r\n"
                              f"X-Timestamp:{_stamp()}Z\r\nPath:ssml\r\n\r\n{ssml}")
            while True:
                message = await ws.receive(timeout=30)
                if message.type == WSMsgType.TEXT:
                    if "Path:turn.end" in message.data:
                        break
                elif message.type == WSMsgType.BINARY:
                    data = message.data
                    size = int.from_bytes(data[:2], "big")
                    if _is_audio(data[2:2 + size]) and len(data) > 2 + size:
                        got = True
                        yield data[2 + size:]
                else:  # закрытие или ошибка до turn.end
                    break
    except WSServerHandshakeError as exc:
        reason = "возможно, изменился протокол или неверно время на компьютере" if exc.status in (401, 403) else "сбой службы"
        raise ValueError(f"Edge TTS: служба отклонила подключение (HTTP {exc.status}): {reason}") from None
    except asyncio.TimeoutError:
        raise ValueError("Edge TTS: служба не ответила вовремя") from None
    except ClientError as exc:
        raise ValueError(f"Edge TTS: нет соединения ({type(exc).__name__})") from None
    if not got:
        raise ValueError("Edge TTS: служба не вернула звук — проверьте голос и текст")


def _is_audio(headers):
    """Заголовки бинарного кадра Edge: звук — Path:audio (не audio.metadata)."""
    return any(line.strip() == b"Path:audio" for line in headers.splitlines())


def _source(runtime, config, text):
    return (_edge_source if _edge(config) else _azure_source)(runtime, config, text)


# --- tts -----------------------------------------------------------------------------------


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
    """Edge — три русских голоса; Azure — голоса региона для ru-RU (в том числе многоязычные), кеш на сессию."""
    key = vendor.secret((config or {}).get("api_key")).strip()
    if _edge(config) or not key:
        return OPTIONS
    url = _tts_url(config)
    listing = url[:-len("/v1")] + "/voices/list" if url.endswith("/cognitiveservices/v1") else url
    token = key + listing
    cached = vendor.voices_cached(NAME, token, refresh or config.get("refresh"))
    if cached is not None:
        return cached
    result = await vendor.call_json(runtime, NAME, "GET", listing, key, headers={"Ocp-Apim-Subscription-Key": key})
    items = []
    for item in result if isinstance(result, list) else []:
        locales = [item.get("Locale", "")] + list(item.get("SecondaryLocaleList") or [])
        if "ru-RU" not in locales or not item.get("ShortName"):
            continue
        gender = {"Female": "female", "Male": "male"}.get(item.get("Gender"), "")
        name = item.get("LocalName") or item.get("DisplayName") or item["ShortName"]
        suffix = "" if item.get("Locale") == "ru-RU" else ", многоязычный"
        items.append(vendor.option(item["ShortName"], f"{name} ({GENDER.get(gender, 'нейтральный')}{suffix})", "ru-RU", gender))
    items.sort(key=lambda o: (not o["value"].startswith("ru-RU-"), o["value"]))
    return vendor.voices_store(NAME, token, items or OPTIONS)


# --- asr (Azure) ---------------------------------------------------------------------------


async def transcribe(runtime, config, audio, filename):
    key = vendor.api_key(config, NAME)
    url = config.get("url") or AZURE_STT.format(region=_region(config))
    wav = vendor.pcm_wav(await vendor.input_pcm16(audio, filename), 16000)
    params = {"language": config.get("language") or "ru-RU", "format": "simple",
              "profanity": config.get("profanity") or "masked"}
    result = await vendor.call_json(
        runtime, NAME, "POST", url, key, params=params, data=wav,
        headers={"Ocp-Apim-Subscription-Key": key, "Accept": "application/json",
                 "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000"})
    status = result.get("RecognitionStatus")
    if status == "Success":
        return str(result.get("DisplayText") or "").strip()
    if status in ("NoMatch", "InitialSilenceTimeout", "BabbleTimeout"):
        return ""  # речи нет — конвейер ответит «не расслышала»
    raise ValueError(f"{NAME}: распознавание не удалось ({status or 'без статуса'})")
