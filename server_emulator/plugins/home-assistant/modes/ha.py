"""Режим «Интеллекта» «Home Assistant»: фраза без сценария уходит
conversation-агенту Home Assistant — REST POST /api/conversation/process с заголовком
Authorization: Bearer <токен> и телом {"text", "language", "conversation_id", "agent_id"}
(эндпоинт документирован в REST API HA; набор полей сверён с homeassistant/components/
conversation/http.py — обязателен только text). Ответ — текст из
response.speech.plain.speech. conversation_id — диалог колонки (по SN: контекст беседы
HA живёт между запросами одной колонки); agent_id (id агента Assist) и язык — настройки
расширения. HA недоступен, таймаут (10 с) или HTTP-ошибка — понятная фраза вместо
сбоя: падающий режим и так откатывает «Интеллект» на «Простой», но это безликое
«не умею», а не объяснение (intelligence.py).

Настройки код режима читает штатно через ExtensionContext: эмулятор ставит
контекст расширения в ctx.extension (intelligence.answer), строки вида `env:ИМЯ` уже
раскрыты; правка настроек во вкладке «Расширений» видна следующему вызову без
рестарта. Своего HTTP-клиента у контекста сценария нет (SDK не отдаёт Runtime) — режим
ходит в HA одноразовой aiohttp-сессией на вызов (ha_common.post_json).

Формат MODE и answer — tests/test_extension_modes.py эмулятора; полный id режима —
"builtin.home-assistant.ha" (settings.intelligence = "ext:builtin.home-assistant.ha").
preset — набор «Всё через Home Assistant» (кнопка в карточке расширения): пресеты
ASR/TTS расширения + этот режим применяются одним кликом.
"""
import importlib.util
from pathlib import Path

# Общий модуль расширения — по рецепту контракта (extensions.md, «Настройки в коде
# расширения»): подключение относительно __file__, без правки sys.path.
_SPEC = importlib.util.spec_from_file_location(
    "ha_extension_common", Path(__file__).resolve().parents[1] / "ha_common.py")
ha_common = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ha_common)

MODE = {
    "id": "ha",
    "title": "Home Assistant",
    "needs": ["network"],
    "tabs": [["ha", "Диалог"], ["cfg", "Настройки"]],
    "preset": {"asr": "p:builtin.home-assistant.ha-asr", "tts": "p:builtin.home-assistant.ha-tts"},
}

NO_SETTINGS = "Настройки расширения «Home Assistant» недоступны."
NO_URL = "Не задан адрес Home Assistant — заполните настройки расширения «Home Assistant»."
UNAVAILABLE = "Home Assistant недоступен. Проверьте адрес и токен в настройках расширения."
EMPTY_REPLY = "Home Assistant ответил без текста."


async def answer(ctx, text, history):
    """Ответ режима: реплика conversation-агента HA или понятная фраза сбоя."""
    ext = getattr(ctx, "extension", None)  # ExtensionContext: ставит intelligence.answer
    if ext is None:
        return {"text": NO_SETTINGS, "tool_calls": [], "usage": {}}
    base = str(ext.get("url") or "").strip().rstrip("/")
    if not base:
        return {"text": NO_URL, "tool_calls": [], "usage": {}}
    payload = {"text": str(text or ""), "language": str(ext.get("language") or "ru")}
    if ctx.sn:  # диалог колонки: контекст беседы HA ведётся по conversation_id
        payload["conversation_id"] = "alice-" + str(ctx.sn)
    agent = str(ext.get("agent_id") or "").strip()
    if agent:
        payload["agent_id"] = agent
    try:
        status, data = await ha_common.post_json(
            base + "/api/conversation/process", payload, ext.get("token"))
    except Exception:  # noqa: BLE001 — сеть/таймаут: понятная фраза, а не сбой режима
        return {"text": UNAVAILABLE, "tool_calls": [], "usage": {}}
    if status != 200 or not isinstance(data, dict):
        return {"text": UNAVAILABLE, "tool_calls": [], "usage": {}}
    speech = ((data.get("response") or {}).get("speech") or {}).get("plain") or {}
    reply = str(speech.get("speech") or "").strip()
    return {"text": reply or EMPTY_REPLY, "tool_calls": [], "usage": {}}
