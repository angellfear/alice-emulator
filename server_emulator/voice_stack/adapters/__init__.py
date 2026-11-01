"""Адаптеры голосового стека — код, через который конвейер говорит с ASR / TTS / LLM.

Адаптер — модуль Python: встроенные лежат здесь, свои — в state/voice_stack/adapters/<id>.py
(подхватываются без рестарта, по mtime). Имя файла — id адаптера, его пишет пресет:
settings[stt|tts|model]["adapter"] = "<id>". Расширения добавляют слой
voice/adapters/*.py — id с префиксом «<ext-id>.<файл>» (встроенные не перекрываются);
загрузка по mtime и выключение вместе с расширением — как у своих адаптеров.

    TITLE = "Anthropic (Claude)"
    DESCRIPTION = "..."
    SERVICES = {                       # что адаптер умеет и какие поля у пресета
        "llm": {"fields": [{"key": "url", "label": "URL", "type": "text"}, ...],
                "defaults": {"url": "...", "model": "..."},
                "examples": {"Groq": {"url": "...", "model": "..."}}},   # быстрое заполнение
    }
    MATCH = {"llm": r"api\\.anthropic\\.com"}  # узнать адаптер по URL старых настроек (необязательно)

    async def complete(runtime, config, payload) -> dict          # llm: OpenAI chat → {"choices":[{"message"}],"usage"}
    async def transcribe(runtime, config, audio, filename) -> str # asr: аудио целиком → текст
    async def recognize_stream(runtime, config, chunks, fmt, on_partial) -> {"text"}  # asr потоком (необязательно)
    async def speech(runtime, config, payload) -> (bytes, content_type)  # tts: OpenAI Speech payload → Ogg Opus
    def speech_stream(runtime, config, text)   # tts потоком: async-генератор Ogg-страниц (необязательно)
    def streams(config) -> bool                # tts: использовать speech_stream для этой конфигурации

runtime даёт lifecycle.client_session() (общий aiohttp-клиент) и всё состояние эмулятора;
config — копия settings[stt|tts|model] с полями пресета; api_key — как есть (env:VAR раскрывает auth.secret).
"""
import importlib
import importlib.util
import logging
import pkgutil
from pathlib import Path

log = logging.getLogger(__name__)

BUILTIN = Path(__file__).resolve().parent
KIND_SERVICE = {"stt": "asr", "tts": "tts", "model": "llm"}
_user_dir = None
_extension_dirs = None  # callable → [(origin, Path)] — слои voice/adapters расширений
_cache = {}  # path → (mtime_ns, module)


def configure(user_dir, extension_dirs=None):
    """Папка своих адаптеров (state/voice_stack/adapters) и слои адаптеров включённых
    расширений (callable → [(origin, Path)] — extensions/layers.source_layers)."""
    global _user_dir, _extension_dirs
    _user_dir = Path(user_dir)
    _extension_dirs = extension_dirs


def _extension_modules():
    """[(id, путь)] адаптеров включённых расширений: «<ext-id>.<файл>»; сбой слоёв —
    слой пуст, список адаптеров работает."""
    if _extension_dirs is None:
        return []
    try:
        layers = _extension_dirs() or []
    except Exception:  # noqa: BLE001 — расширения не должны ломать список адаптеров
        return []
    found = []
    for origin, path in layers:
        ext_id = str(origin).split(":", 1)[-1]
        for file in sorted(Path(path).glob("*.py")):
            found.append((f"{ext_id}.{file.stem}", file))
    return found


def _load_file(path):
    stat = path.stat()
    cached = _cache.get(path)
    if cached and cached[0] == stat.st_mtime_ns:
        return cached[1]
    spec = importlib.util.spec_from_file_location("voice_adapter_" + path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _cache[path] = (stat.st_mtime_ns, module)
    return module


def available():
    """{id: модуль} — встроенные, адаптеры расширений («<ext-id>.<файл>») и свои
    (свой с тем же id заменяет встроенный; подхватываются без рестарта, по mtime)."""
    result = {}
    for info in pkgutil.iter_modules([str(BUILTIN)]):
        result[info.name] = importlib.import_module(__name__ + "." + info.name)
    for identifier, path in _extension_modules():
        try:
            result[identifier] = _load_file(path)
        except Exception as exc:  # noqa: BLE001 — битый адаптер расширения не валит список
            log.warning("Адаптер расширения %s не загружен: %s", path.name, exc)
    if _user_dir and _user_dir.is_dir():
        for path in sorted(_user_dir.glob("*.py")):
            try:
                result[path.stem] = _load_file(path)
            except Exception as exc:  # noqa: BLE001 — битый свой адаптер не валит список
                log.warning("Адаптер %s не загружен: %s", path.name, exc)
    return {k: v for k, v in result.items() if isinstance(getattr(v, "SERVICES", None), dict)}


def get(identifier):
    module = available().get(identifier)
    if module is None:
        raise ValueError(f"Адаптер «{identifier}» не найден")
    return module


def resolve(kind, config):
    """Адаптер для settings[kind]: явный adapter, иначе по URL (WebSocket ASR) или OpenAI-совместимый."""
    identifier = (config or {}).get("adapter")
    if not identifier:
        url = (config or {}).get("url", "")
        identifier = "stream" if kind == "stt" and url.endswith("/asr/stream") else "openai"
    module = get(identifier)
    if KIND_SERVICE[kind] not in module.SERVICES:
        raise ValueError(f"Адаптер «{identifier}» не умеет {KIND_SERVICE[kind]}")
    return module
