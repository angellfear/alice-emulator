"""Датасет ha.lights: лампы Home Assistant для сценария света и закрытого списка
справочника фраз (GET /api/states → сущности light.*).

Ключи записей: "rooms" — названия устройств для списка {ha_room} файла фраз,
"devices" — [{"name", "entity_id", "entity_ids"}] для сценария (имя → сущности). Название:
площадка сущности (POST /api/template с area_name(entity_id) — русская «Спальня»
из настроек HA; REST вместо WS: документирован, один запрос на все сущности, без
второго протокола и keepalive), нет площадки — friendly_name, нет и его — хвост
entity_id. Одинаковые имена свёрнуты в одну запись со всеми сущностями (entity_ids):
закрытый список без дублей, команда идёт на все лампы площадки. Модульный кеш CACHE_TTL секунд объединяет обновление обоих
ключей в один обход HA.

Сбой HA — исключение: refresh_dataset оставляет старую запись (справочник и
сценарь работают по кешу), причину пишет журнал Executor. Настройки — свой
ExtensionContext (sdk.dataset_context, контракт extensions.md «Настройки в коде
расширения»). Сеть — ha_common (одноразовая aiohttp-сессия: датасеты Runtime
не получают).
"""
import importlib.util
import json
import time
from pathlib import Path

from server_emulator.sdk import dataset_context

# Общий модуль расширения — по рецепту контракта (extensions.md, «Настройки в коде
# расширения»): подключение относительно __file__, без правки sys.path.
_SPEC = importlib.util.spec_from_file_location(
    "ha_extension_common", Path(__file__).resolve().parents[1] / "ha_common.py")
ha_common = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ha_common)

CACHE_TTL = 10.0  # общий обход HA для ключей rooms/devices, с; кеш сбрасывается при
# смене адреса/токена (правка настроек) — тесты и живая правка не видят чужой кеш
_cache = {"key": None, "ts": 0.0, "devices": None}


async def _area_names(base, token, ids):
    """{entity_id: название площадки} через REST /api/template (area_name): строка
    на сущность, пустая строка — площадки нет. Сбой шаблона (старый HA, права) —
    {} : названия читаются из friendly_name."""
    template = "{% for id in " + json.dumps(ids, ensure_ascii=False) + " %}{{ area_name(id) }}\n{% endfor %}"
    try:
        status, data = await ha_common.post_json(base + "/api/template", {"template": template}, token)
    except Exception:  # noqa: BLE001 — резерв на friendly_name
        return {}
    if status >= 400 or not isinstance(data, dict):
        return {}
    lines = str(data.get("result") or "").splitlines()
    return {eid: line.strip() for eid, line in zip(ids, lines) if line.strip()}


async def _fetch(base, token):
    """Лампы HA: [{"name", "entity_id", "entity_ids"}] (имена уникальны, порядок — за HA)."""
    status, states = await ha_common.get_json(base + "/api/states", token)
    if status >= 400 or not isinstance(states, list):
        raise RuntimeError(f"ha.lights: Home Assistant вернул HTTP {status}")
    lamps = [s for s in states if str(s.get("entity_id") or "").startswith("light.")
             and str(s.get("state") or "") != "unavailable"]
    ids = [str(s["entity_id"]) for s in lamps]
    areas = await _area_names(base, token, ids) if ids else {}
    devices, by_name = [], {}
    for state in lamps:
        entity_id = str(state["entity_id"])
        friendly = str((state.get("attributes") or {}).get("friendly_name") or "").strip()
        name = areas.get(entity_id) or friendly or entity_id.split(".", 1)[-1].replace("_", " ")
        if name in by_name:
            by_name[name]["entity_ids"].append(entity_id)  # вторая лампа той же площадки
        else:
            by_name[name] = {"name": name, "entity_id": entity_id, "entity_ids": [entity_id]}
            devices.append(by_name[name])
    return devices


async def lights(key, args):
    """Значение записи датасета: ключ "rooms" — названия, иначе — устройства."""
    ext = dataset_context("ha.lights")  # ExtensionContext владельца датасета
    if ext is None:
        raise RuntimeError("ha.lights: нет настроек расширения — датасет вызывается "
                           "из реестра расширения (sdk.dataset_context)")
    base = str(ext.get("url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("ha.lights: не задан адрес Home Assistant (настройки расширения)")
    now = time.time()
    cache_key = (base, str(ext.get("token") or ""))
    if _cache["devices"] is None or _cache["key"] != cache_key or now - _cache["ts"] >= CACHE_TTL:
        devices = await _fetch(base, ext.get("token"))
        _cache.update(key=cache_key, ts=now, devices=devices)
    devices = _cache["devices"]
    if str(key) == "rooms":
        return [device["name"] for device in devices]
    return devices


DATASETS = {"ha.lights": lights}
