"""Умный дом Home Assistant: сущности HA → реестр умного дома (SDK 2.8.0),
действия с колонки → сервисы HA.

Транспорт — WS API HA (`<url>/api/websocket`, авторизация long-lived access token
из настроек плагина): auth → subscribe_entities. Проволока (websocket_api HA):
подписка подтверждается result (null), данные идут событиями — снимок
`{"event": {"a": {entity_id: {s, a, c, lc, lu}}}}` (s=состояние, a=атрибуты),
дельты `{"event": {"c": {entity_id: {"+": {s, a…}, "-": {"a": [ключи]}}}}}`
("+" частичен, "-" удаляет ключи атрибутов), удаление сущности
`{"event": {"r": [entity_ids]}}` → офлайн. Состояние сущностей — локальный кеш
{state, attributes} на соединение: снимок его заполняет, дельты сливают в него,
устройства реестра считаются от кеша. Комнаты — WS-команды config-реестров HA
(`config/entity_registry/list`: entity_id → area_id;
`config/area_registry/list`: area_id → имя; REST `/api/config/*` с HA 2026.5
отвечает 404): устройство несёт room = area_id, без области в HA или при
недоступности реестра — псевдокомната `unassigned` («Не назначено»),
имена комнат — sdk.smart_home_set_rooms({area_id: имя}). Действие карточки с
колонки → REST POST /api/services/<домен>/<сервис> (entity_id) — новое состояние
возвращается событием WS. Реконнект — бэкофф 1→60 с; настройки читаются свежими
на каждое соединение (смена url/token — на следующем реконнекте).

Сопоставление доменов HA и реестра (type — enum станции, неизвестное значение
реестр сам деградирует в UnknownDeviceType; умения — короткие имена on_off/level):

| HA domain | type | умения | сервис действия |
|---|---|---|---|
| light | LightDeviceType | on_off, level | light.turn_on/turn_off (level → brightness) |
| switch | SwitchDeviceType | on_off | switch.turn_on/turn_off |
| climate | ClimateDeviceType | on_off, level | climate.turn_on/turn_off, level → set_temperature |
| cover | CoverDeviceType | level, on_off | cover.open_cover/close_cover, level → set_cover_position |
| media_player | MediaDeviceType | on_off, level | media_player.media_play/media_pause, level → volume_set |
| vacuum | VacuumCleanerDeviceType | on_off | vacuum.start/stop |
| sensor, binary_sensor | SensorDeviceType | battery | — (только состояние) |
| lock, fan, humidifier | LockDeviceType/FanDeviceType/… | on_off/level | по домену |
| script, scene | — (не устройство) | — | script.turn_on / scene.turn_on |

Запуск — ленивый: клиент стартует при первой активности колонки (наблюдатель
RequestStat) или при действии карточки, и живёт реконнект-циклом; снимок
subscribe_entities при каждом старте восполняет пропущенные изменения. Задача —
spawn sdk.extension_runtime (идемпотентен; выключение расширения останавливает её
через hooks.on_disable → stop). Не настроено (url/token пусты) — клиент стартует
и тихо завершается; смена настроек применяется на следующем старте задачи.

Фикстуры тестов обезличены: токен test-token, адреса 192.0.2.0/24.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

from server_emulator import sdk

log = logging.getLogger(__name__)

EXT_ID = "builtin.home-assistant"
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 60.0
UNASSIGNED_AREA = "unassigned"  # псевдокомната для сущностей без области в HA
ENTITY_DOMAINS = ("light", "switch", "climate", "cover", "media_player",
                  "vacuum", "sensor", "binary_sensor", "lock", "fan", "humidifier")

DOMAIN_MAP = {
    "light": {"type": "LightDeviceType"},
    "switch": {"type": "SwitchDeviceType"},
    "climate": {"type": "ClimateDeviceType"},
    "cover": {"type": "CoverDeviceType"},
    "media_player": {"type": "MediaDeviceType"},
    "vacuum": {"type": "VacuumCleanerDeviceType"},
    "sensor": {"type": "SensorDeviceType"},
    "binary_sensor": {"type": "SensorDeviceType"},
    "lock": {"type": "LockDeviceType"},
    "fan": {"type": "FanDeviceType"},
    "humidifier": {"type": "HumidifierDeviceType"},
}

ACTION_SERVICES = {  # действие карточки → сервис HA по умению
    ("on_off", True): "turn_on", ("on_off", False): "turn_off",
    ("level", None): "set_cover_position",  # домен решает в _service_for
}

# Умения уровня → короткое имя инстанса реестра (колонке сообщает, ЧТО измеряет
# уровень: яркость света, позиция шторы, температура климата); громкость плеера
# в enum станции не входит — умение идёт без инстанса.
LEVEL_INSTANCES = {"light": "brightness", "cover": "open", "climate": "temperature"}


def _capabilities(domain, state, attributes):
    """Умения сущности по домену: [{type, value, instance?}] короткими именами
    реестра; level несёт instance (LEVEL_INSTANCES) — из него сборщик списка
    строит TLevelCapability.Parameters.Instance."""
    caps = []
    if domain in ("light", "switch", "media_player", "fan", "humidifier", "climate"):
        caps.append({"type": "on_off", "value": state == "on"})
    if domain == "light" and isinstance(attributes.get("brightness"), int):
        caps.append({"type": "level", "value": round(attributes["brightness"] / 2.55),
                     "instance": LEVEL_INSTANCES[domain]})
    if domain == "cover":
        position = attributes.get("current_position")
        caps.append({"type": "level", "value": int(position or 0),
                     "instance": LEVEL_INSTANCES[domain]})
        caps.append({"type": "on_off", "value": state == "open"})
    if domain == "media_player" and isinstance(attributes.get("volume_level"), (int, float)):
        caps.append({"type": "level", "value": round(attributes["volume_level"] * 100)})
    if domain == "climate" and isinstance(attributes.get("temperature"), (int, float)):
        caps.append({"type": "level", "value": int(attributes["temperature"]),
                     "instance": LEVEL_INSTANCES[domain]})
    if domain == "sensor" and isinstance(attributes.get("battery_level"), (int, float)):
        caps.append({"type": "battery", "value": int(attributes["battery_level"])})
    return caps


def _device_from_state(entity_id, state, attributes, area):
    """Состояние сущности HA → запись для реестра (SDK 130)."""
    domain = entity_id.split(".", 1)[0]
    mapping = DOMAIN_MAP.get(domain)
    if mapping is None:
        return None
    name = str(attributes.get("friendly_name") or entity_id)
    device = {"id": entity_id, "name": name, "type": mapping["type"],
              "capabilities": _capabilities(domain, state, attributes),
              "online": state not in ("unavailable", "unknown"),
              "room": area or UNASSIGNED_AREA}  # без области в HA — псевдокомната «Не назначено»
    return device


def _service_for(entity_id, action):
    """Действие карточки → (домен, сервис, данные) вызова HA; None — не маппится.
    on_off — родные сервисы домена (cover.open_cover/close_cover, media_player
    media_play/media_pause, vacuum.start/stop, прочие turn_on/turn_off); level —
    яркость света (× 2.55), позиция шторы, громкость плеера, температура климата."""
    domain = entity_id.split(".", 1)[0]
    instance, capability = action.get("instance"), action.get("capability")
    value = action.get("value")
    if capability == "on_off" or instance == "on_off":
        on = bool(value) if value is not None else instance != "off"
        if domain == "cover":
            return domain, "open_cover" if on else "close_cover", {}
        if domain == "media_player":
            return domain, "media_play" if on else "media_pause", {}
        if domain == "vacuum":
            return domain, "start" if on else "stop", {}
        return domain, "turn_on" if on else "turn_off", {}
    if capability == "level" and isinstance(value, (int, float)):
        if domain == "light":
            return domain, "turn_on", {"brightness": round(value * 2.55)}
        if domain == "cover":
            return domain, "set_cover_position", {"position": int(value)}
        if domain == "media_player":
            return domain, "volume_set", {"volume_level": round(value / 100, 2)}
        if domain == "climate":
            return domain, "set_temperature", {"temperature": value}
        return domain, "turn_on", {}
    return None


class _Client:
    """WS-клиент HA: auth → subscribe_entities → события в реестр. Состояние сущностей —
    локальный кеш {state, attributes} на соединение: снимок подписки его заполняет,
    дельты сливают в него (атрибуты в "+" частичны), устройства реестра считаются от
    кеша. Реконнект с бэкоффом; настройки читаются свежими на каждое соединение.
    Остановка — отмена задачи (sdk.extension_runtime.stop)."""

    def __init__(self, handle, ctx):
        self.handle = handle
        self.ctx = ctx
        self._id = 0
        self._cache = {}  # entity_id → {"state": str, "attributes": dict}
        self._areas = {}  # entity_id → area_id (реестр сущностей HA)

    def _next_id(self):
        self._id += 1
        return self._id

    async def _rooms(self, ws):
        """Комнаты HA — WS-команды config-реестров: entity_id → area_id и
        {area_id: имя}; REST `/api/config/*` с версии HA 2026.5 отвечает 404,
        WS-команды есть везде, где есть subscribe_entities. Сбой — пусто
        (устройства без комнат) и предупреждение в журнал."""
        async def command(payload):
            identifier = self._next_id()
            await ws.send_json({"id": identifier, **payload})
            while True:
                message = json.loads((await ws.receive()).data)
                if message.get("id") == identifier and message.get("type") == "result":
                    if not message.get("success", True):
                        raise ValueError(f"HA WS: команда {payload['type']} отклонена")
                    return message.get("result") or []
        try:
            areas = {str(item["area_id"]): str(item.get("name") or item["area_id"])
                     for item in await command({"type": "config/area_registry/list"})
                     if isinstance(item, dict) and item.get("area_id")}
            entities = {str(item["entity_id"]): str(item["area_id"])
                        for item in await command({"type": "config/entity_registry/list"})
                        if isinstance(item, dict)
                        and item.get("entity_id") and item.get("area_id")}
            return entities, areas
        except Exception as exc:  # noqa: BLE001 — комнаты не критичны для реестра
            log.warning("Комнаты HA недоступны (%s) — устройства без комнат", exc)
            return {}, {}

    async def _cycle(self):
        """Одно соединение: auth → подписка → события. Исключения — реконнект снаружи.
        False — расширение не настроено (клиент завершается тихо)."""
        ctx = self.handle.context()
        base = str(ctx.get("url") or "").strip().rstrip("/")
        token = str(ctx.get("token") or "").strip()
        if not token:  # токен — признак настроенности (url в дефолтах манифеста непуст)
            return False  # не настроено — тихо (перезапуск: новый spawn после настройки)
        ws_base = base.replace("http", "ws", 1)
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_base + "/api/websocket") as ws:
                required = json.loads((await ws.receive()).data)  # auth_required
                if required.get("type") != "auth_required":
                    raise ValueError("HA WS: ожидался auth_required")
                await ws.send_json({"type": "auth", "access_token": token})
                verdict = json.loads((await ws.receive()).data)
                if verdict.get("type") != "auth_ok":
                    raise ValueError("HA WS: токен отклонён (auth_invalid)")
                entity_areas, area_names = await self._rooms(ws)
                self._areas = entity_areas
                area_names = {**area_names, UNASSIGNED_AREA: "Не назначено"}
                sdk.smart_home_set_rooms(area_names, self.ctx)
                identifier = self._next_id()
                await ws.send_json({"id": identifier, "type": "subscribe_entities"})
                self._cache = {}  # кеш — на соединение: снимок его восполняет
                while True:
                    message = json.loads((await ws.receive()).data)
                    if message.get("id") != identifier:
                        continue  # ping/pong и ответы чужих команд
                    kind = message.get("type")
                    if kind == "result":
                        continue  # ack подписки (result null — данные идут событиями)
                    if kind != "event":
                        continue
                    event = message.get("event") or {}
                    added = event.get("a")
                    if isinstance(added, dict):  # снимок подписки и новые сущности
                        self._apply_added(added)
                    self._apply_delta(event.get("c") or {})
                    for entity_id in event.get("r") or []:
                        # сущность удалена из HA — в реестре офлайн (имя/комната сохраняются)
                        self._cache.pop(entity_id, None)
                        sdk.smart_home_update_device(entity_id, {"online": False}, self.ctx)

    def _apply_added(self, entities):
        """Снимок подписки и новые сущности: {entity_id: {s, a, …}} → кеш → реестр."""
        for entity_id, item in entities.items():
            if not isinstance(item, dict) or item.get("s") is None:
                continue
            self._cache[entity_id] = {"state": str(item["s"]),
                                      "attributes": dict(item.get("a") or {})}
            self._apply(entity_id)

    def _apply_delta(self, changes):
        """Дельта подписки: {entity_id: {"+": {s, a}, "-": {"a": [ключи]}}} → кеш →
        реестр; атрибуты в "+" частичны — сливаются с кешем, "-" удаляет ключи."""
        for entity_id, change in changes.items():
            if not isinstance(change, dict):
                continue
            added = change.get("+") or {}
            cached = self._cache.get(entity_id)
            if cached is None:
                if added.get("s") is None:  # дельта без состояния до снимка — не собрать
                    continue
                cached = self._cache[entity_id] = {"state": "", "attributes": {}}
            if added.get("s") is not None:
                cached["state"] = str(added["s"])
            attrs = added.get("a")
            if isinstance(attrs, dict):
                cached["attributes"].update(attrs)
            for key in (change.get("-") or {}).get("a") or []:
                cached["attributes"].pop(key, None)
            self._apply(entity_id)

    def _apply(self, entity_id):
        """Устройство реестра из кеша (add_device перезаписывает запись — upsert);
        офлайн — состояние unavailable/unknown."""
        cached = self._cache.get(entity_id)
        if cached is None or not cached["state"]:
            return
        device = _device_from_state(entity_id, cached["state"], cached["attributes"],
                                    self._areas.get(entity_id))
        if device:
            sdk.smart_home_add_device(device, self.ctx)

    async def action(self, device, action):
        """Действие карточки с колонки → REST call_service HA; новое состояние —
        событием WS. device_id приходит с префиксом плагина."""
        entity_id = str(device.get("id") or "").split(":", 1)[-1]
        ctx = self.handle.context()
        base = str(ctx.get("url") or "").strip().rstrip("/")
        token = str(ctx.get("token") or "").strip()
        mapped = _service_for(entity_id, action)
        if mapped is None or not base or not token:
            return None
        domain, service, data = mapped
        payload = {"entity_id": entity_id, **data}
        url = f"{base}/api/services/{domain}/{service}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                    headers={"Authorization": "Bearer " + token}) as response:
                if response.status >= 400:
                    raise RuntimeError(f"HA service {domain}/{service}: HTTP {response.status}")
                return await response.json()


async def _run_forever(handle, started):
    """Реконнект-цикл клиента: бэкофф 1→60 с; отмена задачей хэндла (выключение).
    Не настроено — тихое завершение (перезапуск: spawn после настройки)."""
    client = _Client(handle, handle.context())
    backoff = BACKOFF_START_S
    started.set()
    while True:
        try:
            if not await client._cycle():
                return  # не настроено — тихо
            backoff = BACKOFF_START_S  # соединение закрылось штатно — сразу реконнект
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — сеть/отказ HA: тихий реконнект с бэкоффом
            pass  # у плагина нет своего канала журнала; состояние видно в реестре (online)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX_S)


async def _ensure_started():
    """Ленивый запуск клиента: spawn, если задача ещё не жива (настройки читает
    сам клиент — пусто: тихо завершается)."""
    if _handle.alive:
        return
    started = asyncio.Event()
    await _handle.spawn(_run_forever(_handle, started))
    await asyncio.wait_for(started.wait(), 5)


async def action_handler(device, action):
    """Колбэк действий карточек (smart_home_on_device_action): устройство плагина →
    сервис HA. Клиент поднимается лениво, если задача ещё не жива."""
    await _ensure_started()
    return await _Client(_handle, _handle.context()).action(device, action)


async def _on_column_activity(event, ctx):
    """Наблюдатель активности колонки: ленивый запуск клиента (снимок подписки
    восполняет всё пропущенное; после старта наблюдатель больше ничего не делает)."""
    await _ensure_started()


def setup():
    """Идемпотентная привязка умного дома HA к текущему runtime: колбэк действий +
    наблюдатель активности (ленивый запуск). Вызывается при загрузке модуля
    (окно _LOADING — origin плагина) и доступен тестам напрямую."""
    global _handle
    _handle = sdk.extension_runtime(EXT_ID)
    with sdk.loading_extension(EXT_ID):
        sdk.smart_home_on_device_action(action_handler)
        sdk.on_event("RequestStat")(_on_column_activity)


_handle = None
setup()
