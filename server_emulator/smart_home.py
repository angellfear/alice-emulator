"""Реестр устройств умного дома: сервис ядра + построители директив колонке.

Наполняют расширения через SDK (sdk.py: smart_home_add_device/…): устройство
{id, name, room, type, capabilities, properties, online, origin} — origin это id
расширения, добавившего устройство, id реестра получает префикс «<origin>:»
(плагин даёт стабильное имя). Комнаты {id: name} тоже с origin. Устройства и
комнаты переживают перезапуск (state/smart_home.json); online — производное:
флаг плагина И включённость расширения (выключили плагин — устройства «не в сети»).

Действие с колонки (TextInput с фреймом iot_device_action_semantic_frame) идёт
только владельцу-расширению (callback, sdk.smart_home_on_device_action): плагин
возвращает новое состояние — реестр обновляется, колонке по порядку уходят четыре Push:
set_div_variables (переменные device_<id>_<capability>_state) → sync_iot_user_info →
set_iot_account_settings (настройки пустые) → set_iot_user_info (полный список) —
как в записи 2026-11-12. Обработчик TextInput
регистрируется, когда хоть одно расширение поставило обработчик действий
(aapi_handlers.ensure_builtin — перед обработчиками voice), и возвращается False на
чужих фреймах. Список устройств колонке — Push set_iot_user_info
(TSetIoTUserInfoDirective.PayloadRaw = IoTClientUserInfo с Rooms/Households/Devices —
образец tasks/samples/2026-11-10-iot-rename.txt): при изменении реестра — всем живым
сессиям, новой колонке — сразу после опознания (действие карточки пушит список само,
слушателя на это время не зовём).

Своих планировщиков/очередей нет; хранение — JSON-файл, как у прочих состояний.
"""
import copy
import json
import logging
import time
import uuid
from pathlib import Path

from . import aapi_handlers, upper_shutter
from .scenario_engine.proto_build import build, encode

log = logging.getLogger(__name__)

SCHEMAS_DIR = Path(__file__).parent / "schemas"

# Файл состояния: устройства и комнаты плагинов переживают перезапуск.
STATE_FILE = "smart_home.json"

DEFAULT_HOUSEHOLD = {"id": "emulator-home", "name": "Дом"}

# Ключи устройства, которые обновляет update_device (id/origin не меняются).
DEVICE_KEYS = ("name", "room", "type", "capabilities", "properties", "online", "household")

# Фрейм действия кнопки карточки/голоса (образец 2026-11-09, записи 120389–120425):
# TextInput @@mm_semantic_frame → request_value.device_ids + intent_parameters.
IOT_ACTION_FRAME = "iot_device_action_semantic_frame"


class SmartHomeError(ValueError):
    """Неверное устройство/действие (текст — для UI)."""


def _struct_member(payload, key):
    """Вложенный Struct из Payload события или None."""
    value = payload.fields.get(key) if payload is not None else None
    return value.struct_value if value is not None and value.WhichOneof('kind') == 'struct_value' else None


def _string_member(struct, key):
    value = struct.fields.get(key) if struct is not None else None
    return value.string_value if value is not None and value.WhichOneof('kind') == 'string_value' else ''


def _device_ids(frame_body):
    """device_ids из request_value фрейма действия."""
    request = _struct_member(frame_body, 'request_value')
    value = request.fields.get('device_ids') if request is not None else None
    if value is None or value.WhichOneof('kind') != 'list_value':
        return []
    return [item.string_value for item in value.list_value.values
            if item.WhichOneof('kind') == 'string_value']


def _intent(frame_body):
    """intent_parameters фрейма: рядом с request_value (голос, карточка ответа) или внутри
    него (плитка шторки)."""
    return (_struct_member(frame_body, 'intent_parameters')
            or _struct_member(_struct_member(frame_body, 'request_value'), 'intent_parameters'))


def _capability_instance(frame_body):
    """capability_instance из intent_parameters («on»/«off»/…)."""
    return _string_member(_intent(frame_body), 'capability_instance')


def _capability_value(frame_body, instance):
    """Значение вкл/выкл: capability_value.bool_value (кнопка плитки шторки: instance
    всегда «on», значение — здесь), иначе по instance («off» — выкл). None — не задано."""
    value = _struct_member(_intent(frame_body), 'capability_value')
    flag = value.fields.get('bool_value') if value is not None else None
    if flag is not None and flag.WhichOneof('kind') == 'bool_value':
        return flag.bool_value
    return instance.lower() != 'off' if instance else None


IOT_HANDLER_NAME = 'iot_device_action'


async def _on_iot_device_action(conversation, incoming, trigger):
    """Действие умного дома с колонки: реестр пуст — обычный путь; иначе ответ колонке
    как у настоящего сервера (AliceResponse без голоса + tts_play_placeholder),
    действие — плагину-владельцу, а колонке по порядку четыре Push: set_div_variables,
    sync_iot_user_info, set_iot_account_settings, set_iot_user_info (запись 2026-11-12).
    Регистрируется при первом обработчике действий (on_device_action → ensure_builtin,
    перед обработчиками voice) — перехватывает фрейм до сценарной маршрутизации;
    voice.py при этом не меняется."""
    runtime = getattr(conversation, 'runtime', None)
    registry = getattr(runtime, 'smart_home', None)
    if registry is None or not registry.has_actions():
        return False
    if upper_shutter.is_request(incoming):  # колбэк шторки — карточка по реестру
        return await upper_shutter.on_text_input(conversation, incoming, trigger)
    request = incoming.Event.TextInput.Request
    event_obj = request.Event
    frame = _struct_member(event_obj.Payload, 'typed_semantic_frame')
    if frame is None or IOT_ACTION_FRAME not in frame.fields:
        return False
    body = next(iter(frame.fields.values()), None)
    body = body.struct_value if body is not None and body.WhichOneof('kind') == 'struct_value' else None
    # плитка шторки кладёт request_value в request (образец карточки set_upper_shutter),
    # голос и карточка ответа — прямо во фрейм
    body = _struct_member(body, 'request') or body
    device_ids = _device_ids(body)
    instance = _capability_instance(body)
    value = _capability_value(body, instance)
    device_sn = conversation.trace_context.get('sn') or ''
    await conversation.message(conversation.event(incoming, 'InputStartAck'))
    # ответ колонке: пустой голос + одна tts_play_placeholder (запись 2026-11-12)
    output = conversation.event(incoming, 'AliceResponse')
    response = output.Event.AliceResponse
    response.Header.RequestId = getattr(incoming.Event.TextInput.Header, 'RequestId', '') or ''
    response.Header.ResponseId = uuid.uuid4().hex
    response.VoiceResponse.OutputSpeech.Text = ''
    directive = response.Response.Directives.add()
    directive.Type = 'client_action'
    directive.Name = 'tts_play_placeholder'
    directive.IsLedSilent = True
    directive.Payload['channel'] = 'Dialog'
    await conversation.message(output)
    if not device_sn:
        return True
    server_type = conversation.server_type
    changed = {}
    for device_id in device_ids:
        action = {'instance': instance, 'capability': 'on_off',
                  'value': value}
        try:
            # notify=False: список колонке уходит ниже, в порядке образца,
            # а не слушателем реестра
            result = await registry.handle_action(device_id, action, notify=False)
        except SmartHomeError as exc:
            log.warning('Действие умного дома %s: %s', device_id, exc)
            continue
        if result:
            changed[device_id] = result
    if changed:
        await conversation.send(b'AAPI' + iot_push(server_type, device_sn, 'set_div_variables',
                                                   div_variables_raw(changed)).SerializeToString())
    await conversation.send(b'AAPI' + iot_push(server_type, device_sn, 'sync_iot_user_info',
                                               sync_iot_user_info_raw()).SerializeToString())
    if changed:
        await conversation.send(b'AAPI' + iot_push(server_type, device_sn,
                                                   'set_iot_account_settings',
                                                   account_settings_raw()).SerializeToString())
        await conversation.send(b'AAPI' + iot_push(server_type, device_sn, 'set_iot_user_info',
                                                   user_info_raw(registry,
                                                                 station=station_info(runtime, device_sn))).SerializeToString())
    return True


class SmartHomeRegistry:
    """Реестр: устройства и комнаты по источникам (origin), действия — владельцу."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.path = runtime.directory / STATE_FILE
        self._devices = {}    # id → {id, name, room, type, capabilities, properties, online, origin}
        self._rooms = {}      # id → {name, origin}
        self._callbacks = {}  # origin → async callable(device, action) — владелец устройств
        self._listeners = []  # callable() — реестр изменился (колонкам уходит set_iot_user_info)
        self._load()

    # --- хранение ---------------------------------------------------------------
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in data.get("devices") or []:
            if isinstance(item, dict) and item.get("id") and item.get("origin"):
                self._devices[str(item["id"])] = item
        for key, value in (data.get("rooms") or {}).items():
            if isinstance(value, dict) and value.get("name"):
                self._rooms[str(key)] = {"name": str(value["name"]),
                                         "origin": str(value.get("origin") or "")}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"devices": list(self._devices.values()),
             "rooms": {key: {"name": room["name"], "origin": room["origin"]}
                       for key, room in self._rooms.items()}},
            ensure_ascii=False, indent=1), encoding="utf-8")

    def _changed(self, notify=True):
        """Реестр изменился: сохранение + слушатели (пуш колонкам) — сбой слушателя
        не ломает запись. notify=False — только сохранение (пуш делает вызывающий)."""
        try:
            self._save()
        except OSError as exc:
            log.warning("Реестр умного дома не сохранён: %s", exc)
        if not notify:
            return
        for listener in self._listeners:
            try:
                listener()
            except Exception as exc:  # noqa: BLE001 — слушатель не должен ронять запись
                log.warning("Слушатель реестра умного дома: %s", exc)

    def subscribe(self, callback):
        """Слушатель изменений реестра (без аргументов) — ставит runtime при старте."""
        self._listeners.append(callback)

    # --- источник (origin) --------------------------------------------------------
    def origin_online(self, origin):
        """Расширение-источник включено (иначе его устройства «не в сети»)."""
        manager = getattr(self.runtime, "extensions", None)
        if manager is None:
            return True
        try:
            return str(origin) in (manager.loaded or {})
        except Exception:  # noqa: BLE001 — нет менеджера (модульные тесты) — источник доступен
            return True

    # --- устройства ---------------------------------------------------------------
    @staticmethod
    def _normalize(device, origin):
        if not isinstance(device, dict):
            raise SmartHomeError("устройство: объект")
        device_id = str(device.get("id") or "").strip()
        name = str(device.get("name") or "").strip()
        if not device_id or not name:
            raise SmartHomeError("устройство: нужны id и name")
        if ":" in device_id and not device_id.startswith(origin + ":"):
            raise SmartHomeError(f"id устройства с чужим префиксом: {device_id}")
        if not device_id.startswith(origin + ":"):
            device_id = f"{origin}:{device_id}"
        result = {"id": device_id, "name": name, "origin": str(origin)}
        for key in ("room", "type"):
            if device.get(key):
                result[key] = str(device[key])
        for key in ("capabilities", "properties"):
            value = device.get(key)
            if value is not None:
                if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
                    raise SmartHomeError(f"{key}: список объектов")
                result[key] = value
        result["online"] = bool(device.get("online", True))
        if device.get("household"):
            result["household"] = str(device["household"])
        return result

    def add_device(self, device, origin):
        item = self._normalize(device, origin)
        if self._devices.get(item["id"]) == item:
            return copy.deepcopy(item)  # запись не изменилась — без сохранения и пуша
        self._devices[item["id"]] = item
        self._changed()
        return copy.deepcopy(item)

    def update_device(self, device_id, patch, origin):
        device_id, item = self._own(str(device_id), origin)
        changed = False
        for key, value in dict(patch or {}).items():
            if key not in DEVICE_KEYS:
                raise SmartHomeError(f"поле {key!r} обновлять нельзя")
            if key in ("capabilities", "properties"):
                if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
                    raise SmartHomeError(f"{key}: список объектов")
            elif key == "online":
                value = bool(value)
            else:
                value = str(value) if value is not None else ""
            if item.get(key) != value:
                item[key] = value
                changed = True
        if changed:
            self._changed()
        return copy.deepcopy(item)

    def remove_device(self, device_id, origin):
        device_id, _ = self._own(str(device_id), origin)
        del self._devices[device_id]
        self._changed()

    def _own(self, device_id, origin):
        item = self._devices.get(device_id if device_id.startswith(origin + ":")
                                 else f"{origin}:{device_id}")
        if item is None or item["origin"] != str(origin):
            raise SmartHomeError(f"устройство {device_id} не найдено у источника {origin}")
        return item["id"], item

    def devices(self, origin=None):
        """Устройства реестра (копия; origin — только одного источника); online — итог."""
        result = []
        for item in self._devices.values():
            if origin and item["origin"] != str(origin):
                continue
            device = copy.deepcopy(item)
            device["online"] = bool(device["online"]) and self.origin_online(item["origin"])
            result.append(device)
        return sorted(result, key=lambda d: d["id"])

    def get(self, device_id):
        return copy.deepcopy(self._devices.get(str(device_id)))

    # --- комнаты -----------------------------------------------------------------
    def set_rooms(self, rooms, origin):
        """Комнаты источника {id: name} — заменяют его предыдущие, чужие не трогают."""
        if not isinstance(rooms, dict):
            raise SmartHomeError("комнаты: {id: name}")
        fresh = {str(key): {"name": str(name or "").strip(), "origin": str(origin)}
                 for key, name in rooms.items()}
        fresh = {key: room for key, room in fresh.items() if room["name"]}
        old = {key: room for key, room in self._rooms.items() if room["origin"] == str(origin)}
        if old == fresh:
            return  # комнаты не изменились — без сохранения и пуша
        for key in old:
            del self._rooms[key]
        self._rooms.update(fresh)
        self._changed()

    def rooms(self):
        return {key: room["name"] for key, room in sorted(self._rooms.items())}

    # --- действия -----------------------------------------------------------------
    def on_device_action(self, origin, callback):
        """Обработчик действия с колонки по своим устройствам (async (device, action)).
        С первым обработчиком регистрирует фичу в реестре AAPI (TextInput)."""
        self._callbacks[str(origin)] = callback
        aapi_handlers.ensure_builtin(
            'TextInput', _on_iot_device_action, name=IOT_HANDLER_NAME,
            description='Умный дом с колонки: фрейм iot_device_action_semantic_frame '
                        '(кнопка карточки или шторки, голос) → плагину-владельцу; колбэк '
                        'collect_upper_shutter → карточка шторки set_upper_shutter')

    def has_actions(self):
        """Есть ли обработчики действий (фича используется хоть одним источником)."""
        return bool(self._callbacks)

    async def handle_action(self, device_id, action, notify=True):
        """Действие колонки → владельцу. Возвращает изменения состояния
        {capability: value} или None (владелец не подтвердил).
        notify=False — слушателя реестра (пуш списка всем живым сессиям) не звать:
        вызывающий отправит список сам, в порядке образца."""
        item = self._devices.get(str(device_id))
        if item is None:
            raise SmartHomeError(f"устройство {device_id} не найдено")
        callback = self._callbacks.get(item["origin"])
        if callback is None:
            raise SmartHomeError(f"у источника {item['origin']} нет обработчика действий")
        result = await callback(copy.deepcopy(item), dict(action or {}))
        if isinstance(result, dict) and result:
            capabilities = item.setdefault("capabilities", [])
            for capability, value in result.items():
                for entry in capabilities:
                    if entry.get("type") == capability:
                        entry["value"] = value
            self._changed(notify)
            return result
        return None


# --- построители сообщений колонке (Push) -----------------------------------------

USER_INFO_PROTO = "NAlice.TLocalIoTCapability.TSetIoTUserInfoDirective"

# Короткие имена возможностей плагина → enum ECapabilityType схемы станции
# (образец: Type/CapabilityType — enum, значения LightDeviceType/OnOffCapabilityType/…).
CAPABILITY_TYPES = {"on_off": "OnOffCapabilityType", "level": "LevelCapabilityType",
                    "color": "ColorCapabilityType", "battery": "BatteryCapabilityType"}

# Умение уровня → сообщение TLevelCapability.Parameters: короткое имя инстанса
# (его даёт расширение вместе с умением) → enum Instance, единица и диапазон
# (образец: яркость света — BrightnessInstance, PercentUnit, 1..100).
LEVEL_INSTANCES = {
    "brightness": ("BrightnessInstance", "PercentUnit", 1, 100),
    "open": ("CoverInstance", "PercentUnit", 0, 100),
    "temperature": ("TemperatureInstance", "TemperatureCelsiusUnit", None, None),
    "humidity": ("HumidityInstance", "PercentUnit", 0, 100),
    "pressure": ("PressureInstance", None, None, None),
    "illuminance": ("IlluminanceInstance", None, None, None),
    "voltage": ("VoltageInstance", None, None, None),
    "amperage": ("AmperageInstance", None, None, None),
    "power": ("PowerInstance", None, None, None),
    "co2": ("CO2Instance", None, None, None),
}

# Поддерживаемые директивы умения (Meta.SupportedDirectives, образец).
CAPABILITY_DIRECTIVES = {"on_off": ["OnOffDirectiveType"],
                         "level": ["SetAbsoluteLevelDirectiveType"],
                         "battery": []}

_VALID_ENUMS: dict = {}


def _enum_names(kind):
    """Имена enum схемы станции для полей Type/CapabilityType (None — схема не читается,
    значения идут как есть). Нужно, чтобы неизвестное значение плагина не роняло
    сборку всего списка: оно заменяется Unknown…Type."""
    if not _VALID_ENUMS:
        from .scenario_engine.proto_build import message_class
        try:
            devices = (message_class(SCHEMAS_DIR, USER_INFO_PROTO, "centaur").DESCRIPTOR
                       .fields_by_name["IoTClientUserInfo"].message_type
                       .fields_by_name["Devices"].message_type)
            _VALID_ENUMS.update({
                "type": set(devices.fields_by_name["Type"].enum_type.values_by_name),
                "capability": set(devices.fields_by_name["Endpoint"].message_type
                                  .fields_by_name["AnyCapabilities"].message_type
                                  .fields_by_name["CapabilityType"].enum_type.values_by_name),
            })
        except Exception as exc:  # noqa: BLE001 — нет схемы — не чиним, encode сообщит
            log.warning("Схема enum умного дома не загружена: %s", exc)
            return None
    return _VALID_ENUMS.get(kind)


def _capability_raw(entry):
    """Payload умения AnyCapabilities.RawCapability — типизированное сообщение схемы
    (образец 2026-11-10: AnyCapabilities несёт RawCapability, не голый CapabilityType):
    on_off → TOnOffCapability (State.On), level → TLevelCapability (Parameters.Instance
    по короткому имени из расширения, Unit/Range, State.Level), battery →
    TBatteryCapability (State.Percentage). Meta — поддерживаемые директивы +
    Retrievable. None — тип неизвестен (умение идёт CapabilityType'ом)."""
    kind = str(entry.get("type") or "")
    value = entry.get("value")
    if kind not in CAPABILITY_DIRECTIVES:
        return None
    meta = {"SupportedDirectives": CAPABILITY_DIRECTIVES[kind], "Retrievable": True}
    if kind == "on_off":
        return encode(SCHEMAS_DIR, "NAlice.TOnOffCapability",
                      {"Meta": meta, "State": {"On": bool(value)}}, "centaur")
    if kind == "level":
        params = {}
        instance = LEVEL_INSTANCES.get(str(entry.get("instance") or ""))
        if instance:
            name, unit, lo, hi = instance
            params["Instance"] = name
            if unit:
                params["Unit"] = unit
            if lo is not None:
                params["Range"] = {"Min": lo, "Max": hi, "Precision": 1}
        return encode(SCHEMAS_DIR, "NAlice.TLevelCapability",
                      {"Meta": meta, "Parameters": params,
                       "State": {"Level": float(value or 0)}}, "centaur")
    return encode(SCHEMAS_DIR, "NAlice.TBatteryCapability",
                  {"Meta": meta, "State": {"Percentage": float(value or 0)}}, "centaur")


# Платформа колонки → enum Type устройства-колонки (образец настоящего кадра 2026-11-14:
# chiron → YandexStationChironDeviceType, yandexstation_2 → YandexStation2DeviceType).
STATION_TYPES = {"chiron": "YandexStationChironDeviceType",
                 "yandexstation_2": "YandexStation2DeviceType",
                 "yandexstation": "YandexStationDeviceType",
                 "yandexmini": "YandexStationMiniDeviceType",
                 "yandexmini_2": "YandexStationMini2DeviceType",
                 "yandexmicro": "YandexStationMicroDeviceType",
                 "yandexmidi": "YandexStationMidiDeviceType",
                 "yandexmodule": "YandexModuleDeviceType",
                 "yandexmodule_2": "YandexModule2DeviceType",
                 "magritte": "YandexTvMagritteDeviceType",
                 "malevich": "YandexTvMalevichDeviceType",
                 "monet": "YandexTvMonetDeviceType",
                 "cucumber": "YandexStationCucumberDeviceType"}


def station_info(runtime, sn):
    """{sn, name, platform} колонки для устройства-колонки в кадре (None — колонки нет
    в реестре устройств: тогда кадр без неё, как раньше)."""
    device = None
    identities = getattr(runtime, "identities", None)
    if identities is not None and sn:
        device = next((d for d in identities.data["devices"] if d.get("sn") == str(sn)), None)
    if device is None:
        return None
    from .quasar_config import device_platform
    return {"sn": str(sn), "name": str(device.get("name") or sn),
            "platform": device_platform(device)}


def user_info_raw(registry, station=None):
    """PayloadRaw директивы set_iot_user_info: TSetIoTUserInfoDirective c
    IoTClientUserInfo (Rooms/Households/Devices) — поля по образцу 2026-11-10.
    station — устройство-колонки в том же доме (форма настоящего кадра): лаунчер
    колонки берёт для виджетов умного дома дом, где есть её устройство, поэтому без
    него список в шторке пуст при принятых устройствах.
    Умения вписываются в готовое сообщение напрямую (ParseDict портит bytes-поля:
    он ждёт их в base64-строках). Держатель умения несёт RawCapability и
    CapabilityType вместе (оба поля в образце): колонка строит из CapabilityType
    идентификатор строки умения, и держатель без него у всех умений одного
    устройства даёт одинаковый UnknownCapabilityType — на первичном ключе
    (id, device_smart_home_id) это конфликт, и колонка отвергает весь набор."""
    rooms = registry._rooms
    devices, households = [], {DEFAULT_HOUSEHOLD["id"]: DEFAULT_HOUSEHOLD["name"]}
    capability_lists = []
    now = int(time.time())
    type_names, cap_names = _enum_names("type"), _enum_names("capability")
    for item in registry.devices():
        household = item.get("household") or DEFAULT_HOUSEHOLD["id"]
        households.setdefault(household, DEFAULT_HOUSEHOLD["name"])
        capabilities = []
        for cap in (item.get("capabilities") or []):
            kind = str(cap.get("type") or "")
            if not kind:
                continue
            raw = _capability_raw(cap)
            if raw is not None:
                capabilities.append((raw, CAPABILITY_TYPES[kind]))
                continue
            name = CAPABILITY_TYPES.get(kind, kind if kind.endswith("CapabilityType")
                                        else "UnknownCapabilityType")
            if cap_names is not None and name not in cap_names:
                name = "UnknownCapabilityType"
            capabilities.append(name)
        capability_lists.append(capabilities)
        device_type = str(item.get("type") or "UnknownDeviceType")
        if type_names is not None and device_type not in type_names:
            device_type = "UnknownDeviceType"
        devices.append({
            "Id": item["id"],
            "Name": item["name"],
            "Type": device_type,
            # сущность источника без префикса (образец настоящего кадра: ExternalId = entity_id)
            "ExternalId": str(item["id"]).split(":", 1)[-1],
            "RoomId": item.get("room") or "",
            "HouseholdId": household,
            "StatusInfo": {"Status": "OnlineDeviceState" if item["online"] else "OfflineDeviceState",
                           "Updated": now, "Changed": now},
            # статус конечной точки (образец: Endpoint.Status — Online/Offline + UpdatedAt;
            # UpdatedAt проставляется после сборки — GetCurrentTime, как Timings в конверте)
            "Endpoint": {"Id": item["id"],
                         "Status": {"Status": "Online" if item["online"] else "Offline"}},
        })
    if station:
        endpoint_id = "{}.{}".format(station["sn"], station.get("platform") or "").rstrip(".")
        devices.append({
            # форма настоящего кадра (2026-11-14): Id — постоянный uuid по серийнику,
            # ExternalId/Endpoint.Id — «<серийник>.<платформа>», SkillId «Q», Endpoint
            # без умений; дом — общий с устройствами реестра
            "Id": str(uuid.uuid5(uuid.NAMESPACE_URL, "emulator-station:" + station["sn"])),
            "Name": str(station.get("name") or station["sn"]),
            "Type": STATION_TYPES.get(str(station.get("platform") or ""),
                                      "SmartSpeakerDeviceType"),
            "ExternalId": endpoint_id,
            "SkillId": "Q",
            "HouseholdId": DEFAULT_HOUSEHOLD["id"],
            "StatusInfo": {"Status": "OnlineDeviceState", "Updated": now, "Changed": now},
            "Endpoint": {"Id": endpoint_id, "Status": {"Status": "Online"}},
        })
    message = build(SCHEMAS_DIR, USER_INFO_PROTO,
                    {"Name": "set_iot_user_info",
                     "IoTClientUserInfo": {
                         "Rooms": [{"Id": key, "Name": room["name"],
                                    "HouseholdId": DEFAULT_HOUSEHOLD["id"]}
                                   for key, room in sorted(rooms.items())],
                         "Households": [{"Id": key, "Name": name}
                                        for key, name in sorted(households.items())],
                         "Devices": devices}}, "centaur")
    for index, capabilities in enumerate(capability_lists):
        endpoint = message.IoTClientUserInfo.Devices[index].Endpoint
        for entry in capabilities:
            cap = endpoint.AnyCapabilities.add()
            if isinstance(entry, tuple):
                cap.RawCapability = entry[0]
                cap.CapabilityType = entry[1]
            else:
                cap.CapabilityType = entry
    for device in message.IoTClientUserInfo.Devices:
        device.Endpoint.Status.UpdatedAt.GetCurrentTime()
    return message.SerializeToString()


def div_variables_raw(changes):
    """PayloadRaw set_div_variables: TSetDivVariablesDirective — переменные
    device_<upper_shutter.variable_key(id)>_<capability>_state (значение LongValue,
    VariableType Local) — те же имена, что у плиток шторки."""
    variables = [{"VariableName": "device_{}_{}_state".format(
                       upper_shutter.variable_key(device_id), capability),
                  "LongValue": int(bool(value)), "VariableType": "Local"}
                 for device_id, change in dict(changes or {}).items()
                 for capability, value in dict(change).items()]
    return encode(SCHEMAS_DIR, "NAlice.TDivViewCapability.TSetDivVariablesDirective",
                  {"SetVariable": variables}, "centaur")


def sync_iot_user_info_raw():
    """PayloadRaw sync_iot_user_info — только Name (сигнал без нагрузки)."""
    return encode(SCHEMAS_DIR, "NAlice.TLocalIoTCapability.TSyncIoTUserInfoDirective",
                  {"Name": "sync_iot_user_info"}, "centaur")


def account_settings_raw():
    """PayloadRaw set_iot_account_settings: Name + пустое TIoTAccountSettings
    (запись 2026-11-12 14:35:36 — настроек в директиве нет)."""
    return encode(SCHEMAS_DIR, "NAlice.TLocalIoTCapability.TSetIoTAccountSettingsDirective",
                  {"Name": "set_iot_account_settings", "IoTAccountSettings": {}}, "centaur")


def iot_push(server_type, sn, name, raw):
    """Push одной директивой колонке: client_action, IsLedSilent, EndpointId {value: SN},
    PayloadRaw = raw — конверт по образцу 2026-11-10 (set_iot_user_info / sync_iot_user_info /
    set_div_variables)."""
    output = server_type()
    output.Event.Header.MessageId = uuid.uuid4().hex
    output.Event.Header.Ack = int(time.time() * 1000)
    push = output.Event.Push
    push.DeduplicationPushId = "notificator-internal-" + uuid.uuid4().hex
    push.PushIds.append(uuid.uuid4().hex)
    directive = push.Directives.add()
    directive.Type = "client_action"
    directive.Name = name
    directive.AnalyticsType = name
    directive.IsLedSilent = True
    directive.EndpointId.value = str(sn)
    if raw:
        directive.PayloadRaw = bytes(raw)
    push.AnalyticsMetaInfo.RequestId = uuid.uuid4().hex
    output.Timings.SendingTime.seconds = int(time.time())
    return output
