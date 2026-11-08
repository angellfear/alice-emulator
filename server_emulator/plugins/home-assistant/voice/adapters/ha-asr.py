"""Адаптер ASR «Home Assistant»: POST /api/stt/<provider> — HTTP-эндпоинт STT-интеграции
HA (homeassistant/components/stt/__init__.py в исходниках; в публичной документации REST
API его НЕТ — эндпоинт внутренний, см. README). Аудио — сырым телом запроса, метаданные —
заголовок X-Speech-Content: format=wav; codec=pcm; sample_rate=16000; bit_rate=16;
channel=1; language=ru_ru; ответ — JSON {"text", "result"}. Провайдер зависит от
установленной STT-интеграции (Wyoming-Piper даёт сущность stt.piper) — часть URL.

Формат аудио читается из RIFF-заголовка WAV (конвейер эмулятора шлёт WAV 16 кГц);
не-WAV — параметры из полей конфигурации. Настройки — config пресета (SERVICES ниже);
токен: api_key пресета (env:ИМЯ раскрывает sdk.secret, как у встроенного адаптера
openai), пустой дополняется токеном из настроек расширения (аргумент extension,
конвейер передаёт его адаптерам, чья сигнатура его принимает; строки вида
env:ИМЯ в настройках расширения раскрыты ExtensionContext'ом). Сеть — общий aiohttp-
клиент эмулятора (runtime.lifecycle.client_session(), контракт voice-stack.md).
Потоковое распознавание (recognize_stream) и протокол Wyoming не поддержаны —
ограничение описано в README.
"""
import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientTimeout

# Общий модуль расширения — по рецепту контракта (extensions.md, «Настройки в коде
# расширения»): подключение относительно __file__, без правки sys.path.
_SPEC = importlib.util.spec_from_file_location(
    "ha_extension_common", Path(__file__).resolve().parents[2] / "ha_common.py")
ha_common = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ha_common)

TITLE = "Home Assistant (Assist STT)"
DESCRIPTION = "Распознавание через STT-провайдер Home Assistant (POST /api/stt/<провайдер>)."
URL = {"key": "url", "label": "URL STT (…/api/stt/<провайдер>)", "type": "text"}
KEY = {"key": "api_key", "label": "Токен доступа (или env:ИМЯ; пусто — токен расширения)", "type": "password"}
LANGUAGE = {"key": "language", "label": "Язык (например ru_ru)", "type": "text"}
SERVICES = {
    "asr": {
        "fields": [URL, KEY, LANGUAGE,
                   {"key": "sample_rate", "label": "Частота (не-WAV, Гц)", "type": "number", "step": 1000}],
        "defaults": {"url": "http://homeassistant.local:8123/api/stt/stt.piper",
                     "api_key": "", "language": "ru_ru", "sample_rate": 16000},
        "examples": {"Home Assistant + Piper": {
            "url": "http://homeassistant.local:8123/api/stt/stt.piper",
            "api_key": "env:HA_TOKEN", "language": "ru_ru", "sample_rate": 16000}},
    },
}
MATCH = {"asr": r"/api/stt/"}
LIMIT = 16 * 1024 ** 2  # ответ HA больше 16 МиБ — ошибка (как у адаптера openai)


def _wav_format(audio):
    """(sample_rate, bits, channels) из RIFF/WAVE-заголовка или None."""
    data = bytes(audio[:16]) if isinstance(audio, (bytes, bytearray)) else b""
    if len(audio) < 44 or data[:4] != b"RIFF" or bytes(audio[8:12]) != b"WAVE":
        return None
    pos, size = 12, len(audio)
    while pos + 8 <= size:
        chunk = bytes(audio[pos:pos + 4])
        length = int.from_bytes(audio[pos + 4:pos + 8], "little")
        if chunk == b"fmt " and pos + 8 + 16 <= size:
            body = bytes(audio[pos + 8:pos + 24])
            channels = int.from_bytes(body[2:4], "little")
            rate = int.from_bytes(body[4:8], "little")
            bits = int.from_bytes(body[14:16], "little")
            if 1 <= channels <= 8 and 8000 <= rate <= 192000 and bits in (8, 16, 24, 32):
                return rate, bits, channels
            return None
        pos += 8 + length + (length & 1)  # чанки выравниваются на 2 байта
    return None


def speech_content(config, audio):
    """Заголовок X-Speech-Content: формат из WAV или из настроек; все шесть полей
    обязательны для HA (stt/__init__.py: _metadata_from_header)."""
    parsed = _wav_format(audio)
    rate = parsed[0] if parsed else int(config.get("sample_rate") or 16000)
    bits = parsed[1] if parsed else int(config.get("bit_rate") or 16)
    channels = parsed[2] if parsed else int(config.get("channel") or 1)
    language = str(config.get("language") or "ru_ru").strip() or "ru_ru"
    return (f"format=wav; codec=pcm; sample_rate={rate}; "
            f"bit_rate={bits}; channel={channels}; language={language}")


def _token(config, extension):
    """Токен: api_key пресета (env:ИМЯ раскроет sdk.secret), пустой — из настроек
    расширения (extension, там он уже раскрыт ExtensionContext'ом)."""
    token = str(config.get("api_key") or "").strip()
    if not token and extension is not None:
        token = str(extension.get("token") or "").strip()
    return token


async def transcribe(runtime, config, audio, filename, extension=None):
    """Аудио целиком → текст: POST /api/stt/<provider>, ответ {"text", "result"}.

    extension — ExtensionContext расширения, если конвейер его передаёт;
    None — токен берётся только из config пресета (конвейер эмулятора сегодня вызывает
    transcribe без extension — см. README)."""
    url = str(config.get("url") or "").strip()
    if not url or urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("Настройте URL STT вида http://<ha>:8123/api/stt/<провайдер>")
    headers = ha_common.auth_headers(_token(config, extension))
    headers["X-Speech-Content"] = speech_content(config, audio)
    session = ha_common.shared_session(runtime)
    if session is None:
        raise ValueError("Сеть недоступна: нет общего клиента эмулятора (Lifecycle)")
    async with session.post(url, data=bytes(audio), headers=headers,
                            timeout=ClientTimeout(total=90)) as response:
        if response.status >= 400:
            raise ValueError(f"STT: HTTP {response.status} от Home Assistant "
                             f"(проверьте провайдера и токен)")
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > LIMIT:
                raise ValueError("Ответ Home Assistant >16 МиБ")
        try:
            import json
            result = json.loads(bytes(data))
        except ValueError as exc:
            raise ValueError("STT: неожиданный ответ Home Assistant") from exc
    if not isinstance(result, dict):
        raise ValueError("STT: неожиданный ответ Home Assistant")
    if str(result.get("result") or "").lower() not in ("", "success"):
        raise ValueError("STT: Home Assistant не смог распознать аудио")
    return str(result.get("text") or "")
