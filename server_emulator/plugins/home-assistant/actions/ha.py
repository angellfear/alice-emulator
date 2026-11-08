"""Действие ha.call_service: POST {url}/api/services/<domain>/<service>
в Home Assistant (Bearer-токен, тело — данные вызова; эндпоинт документирован в
REST API HA).

Сигнатура — контракт реестра Executor: async fn(arguments, execution). Настройки код
действия читает штатно через ExtensionContext: эмулятор ставит контекст
расширения-владельца в execution.extension на время вызова (Executor._action для
JSON-шагов, SDK ctx.action) и сбрасывает после; строки вида `env:ИМЯ` уже раскрыты.
Имя «ha.call_service» — под префиксом contributes.actions.prefix («ha», он же
пространство датасета ha.lights) и объявлено в permissions.actions манифеста;
сверку ведёт manager.action_prefix (runtime.rebuild_extension_registries). Вне
вызова сценарием (execution.extension пуст) действие честно отказывается —
настроек у него нет.

Аргументы шага action (или ctx.action):
  {"domain": "light", "service": "turn_on",
   "entity_id": "light.kitchen",          # необязательно; можно список сущностей
   "data": {"brightness_pct": 50}}        # необязательно — остальные данные вызова
Тело POST: {"entity_id": …, **data} — пустые entity_id/data в тело не попадают
(область/устройство можно передать через data: {"area_id": …} / {"device_id": …}).
Возврат — разобранный JSON ответа HA (список изменённых состояний) — значение
в slot шага. Ошибка сети/HTTP — исключение: шаг action уходит в on_failure
или errored (scenario_engine/executor.py). Сеть — ha_common (одноразовая
aiohttp-сессия, как у режима: действия Runtime не получают).
"""
import importlib.util
from pathlib import Path

# Общий модуль расширения — по рецепту контракта (extensions.md, «Настройки в коде
# расширения»): подключение относительно __file__, без правки sys.path.
_SPEC = importlib.util.spec_from_file_location(
    "ha_extension_common", Path(__file__).resolve().parents[1] / "ha_common.py")
ha_common = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ha_common)


async def call_service(arguments, execution):
    """Вызов сервиса HA: возврат — JSON ответа (изменённые состояния), сбой — исключение."""
    args = arguments if isinstance(arguments, dict) else {}
    domain = str(args.get("domain") or "").strip()
    service = str(args.get("service") or "").strip()
    if not domain or not service:
        raise ValueError("ha.call_service: нужны domain и service (например light / turn_on)")
    ext = getattr(execution, "extension", None)  # ExtensionContext владельца
    if ext is None:
        raise RuntimeError("ha.call_service: нет настроек расширения — действие вызывается "
                           "через сценарий (execution.extension не задан)")
    body = {}
    entity = args.get("entity_id")
    if isinstance(entity, list):  # несколько сущностей — HA принимает список
        entity = [str(e).strip() for e in entity if str(e).strip()]
    else:
        entity = str(entity or "").strip()
    if entity:
        body["entity_id"] = entity
    data = args.get("data")
    if isinstance(data, dict):
        body.update(data)
    base = str(ext.get("url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("ha.call_service: не задан адрес Home Assistant (настройки расширения)")
    status, payload = await ha_common.post_json(
        base + f"/api/services/{domain}/{service}", body, ext.get("token"))
    if status >= 400:
        raise RuntimeError(f"ha.call_service: Home Assistant вернул HTTP {status}")
    return payload


ACTIONS = {"ha.call_service": call_service}
