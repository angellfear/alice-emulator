"""Адаптер TTS «Home Assistant»: POST /api/tts_get_url → {"url", "path"} → GET
/api/tts_proxy/<token> (эндпоинт документирован на странице интеграции TTS HA;
сам файл отдаётся без авторизации — токен зашит в URL). message берётся из
OpenAI Speech payload (payload["input"], как конвейер эмулятора вызывает адаптер:
audio_buffer.synthesize); engine_id/language/voice — поля конфигурации (voice
уходит в options.voice).

Настройки — config пресета (SERVICES ниже); токен: api_key пресета (env:ИМЯ раскрывает
sdk.secret, как у встроенного адаптера openai), пустой дополняется токеном из
настроек расширения (аргумент extension, конвейер передаёт его адаптерам,
чья сигнатура его принимает; строки вида env:ИМЯ в настройках расширения раскрыты
ExtensionContext'ом). Сеть — общий aiohttp-клиент эмулятора
(runtime.lifecycle.client_session(), контракт voice-stack.md).

Потоковый синтез (speech_stream) не поддержан — REST отдаёт готовый файл целиком;
synth_pcm нет, поэтому файловый кеш TTS с этим адаптером не работает (звуковой кеш
требует PCM-движок) — ограничения описаны в README.
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

TITLE = "Home Assistant (TTS)"
DESCRIPTION = "Синтез через Home Assistant (POST /api/tts_get_url → GET /api/tts_proxy/…)."
URL = {"key": "url", "label": "URL TTS (…/api/tts_get_url)", "type": "text"}
KEY = {"key": "api_key", "label": "Токен доступа (или env:ИМЯ; пусто — токен расширения)", "type": "password"}
ENGINE = {"key": "engine_id", "label": "Движок TTS (entity id, необязательно)", "type": "text"}
LANGUAGE = {"key": "language", "label": "Язык (необязательно)", "type": "text"}
VOICE = {"key": "voice", "label": "Голос (options.voice, необязательно)", "type": "text"}
SERVICES = {
    "tts": {
        "fields": [URL, KEY, ENGINE, LANGUAGE, VOICE],
        "defaults": {"url": "http://homeassistant.local:8123/api/tts_get_url",
                     "api_key": "", "engine_id": "", "language": "ru", "voice": ""},
        "examples": {"Home Assistant + Piper": {
            "url": "http://homeassistant.local:8123/api/tts_get_url",
            "api_key": "env:HA_TOKEN", "engine_id": "tts.piper", "language": "ru", "voice": ""}},
    },
}
MATCH = {"tts": r"/api/tts_get_url"}
LIMIT = 16 * 1024 ** 2  # ответ HA больше 16 МиБ — ошибка (как у адаптера openai)


def _token(config, extension):
    """Токен: api_key пресета (env:ИМЯ раскроет sdk.secret), пустой — из настроек
    расширения (extension, там он уже раскрыт ExtensionContext'ом)."""
    token = str(config.get("api_key") or "").strip()
    if not token and extension is not None:
        token = str(extension.get("token") or "").strip()
    return token


async def speech(runtime, config, payload, extension=None):
    """Текст → аудио: POST /api/tts_get_url, затем GET url из ответа → (байты, тип).

    extension — ExtensionContext расширения, если конвейер его передаёт;
    None — токен берётся только из config пресета (конвейер эмулятора сегодня вызывает
    speech без extension — см. README)."""
    url = str(config.get("url") or "").strip()
    if not url or urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("Настройте URL TTS вида http://<ha>:8123/api/tts_get_url")
    text = str((payload or {}).get("input") or (payload or {}).get("text") or "").strip()
    if not text:
        raise ValueError("TTS: пустой текст синтеза")
    body = {"message": text}
    engine = str(config.get("engine_id") or "").strip()
    if engine:
        body["engine_id"] = engine
    language = str(config.get("language") or "").strip()
    if language:
        body["language"] = language
    voice = str(config.get("voice") or "").strip()
    if voice:
        body["options"] = {"voice": voice}
    session = ha_common.shared_session(runtime)
    if session is None:
        raise ValueError("Сеть недоступна: нет общего клиента эмулятора (Lifecycle)")
    headers = ha_common.auth_headers(_token(config, extension))
    async with session.post(url, json=body, headers=headers,
                            timeout=ClientTimeout(total=90)) as response:
        if response.status >= 400:
            raise ValueError(f"TTS: HTTP {response.status} от Home Assistant "
                             f"(проверьте движок и токен)")
        import json
        data = await response.json(content_type=None)
    if not isinstance(data, dict) or not (data.get("url") or data.get("path")):
        raise ValueError("TTS: Home Assistant не вернул URL аудио")
    target = str(data.get("url") or "").strip()
    if not target:  # только path — относительный, от базы tts_get_url
        split = urlsplit(url)
        target = f"{split.scheme}://{split.netloc}{data['path']}"
    async with session.get(target, headers=headers,
                           timeout=ClientTimeout(total=90)) as audio:
        if audio.status >= 400:
            raise ValueError(f"TTS: HTTP {audio.status} при получении аудио")
        data_bytes = bytearray()
        async for chunk in audio.content.iter_chunked(65536):
            data_bytes.extend(chunk)
            if len(data_bytes) > LIMIT:
                raise ValueError("Ответ Home Assistant >16 МиБ")
        content_type = audio.headers.get("Content-Type", "audio/mpeg")
    return bytes(data_bytes), content_type


def streams(config):
    """Потокового синтеза нет: REST HA отдаёт файл целиком (Ogg-страницы собирает конвейер)."""
    return False
