"""Блок умного дома в верхней шторке колонки: div2-карточка set_upper_shutter.

Шторку рисует не лаунчер по своей базе, а сервер: колонка шлёт TextInput с событием
@@mm_typed_callback (Payload.analytics.purpose = collect_upper_shutter), сервер
отвечает InputStartAck и AliceResponse с одной директивой client_action
set_upper_shutter (Payload.div2_card: string_body — div2 JSON, id {card_id
upper_shutter_key_static_key, card_name upper_shutter}, card_name
CentaurUpperShutterData; IsLedSilent) — форма настоящего ответа из журнала.

Карточка собирается по реестру умного дома из обезличенного шаблона
upper_shutter.json (каркас, чип фильтра, плитки вкл/выкл и со значением свойства
в сети и не в сети, «Показать все»): чипы — только присутствующих категорий, плитки — группами по
комнатам по имени, без комнаты — в конце, затем «Показать все». Переменная состояния
плитки — device_<id>_on_off_state (variable_key), та же, что в set_div_variables;
значение свойства — строка в property_<id>_<инстанс>_devices.properties.float_value
(«--», если значения нет). Датчик, термостат и пылесос показываются, только когда у
них есть значение с образцом (PROPERTIES).
Ссылки dialog:// в шаблоне хранятся раскрытыми ({"$dialog": [директивы]}) и
кодируются при сборке, как в образце: компактный JSON, quote без безопасных символов.

Колонки (фильтр «Колонки») — другие колонки того же пользователя из реестра
колонок эмулятора (smart_home.stations): в сети — есть живая сессия; кнопка плитки —
phone_calls_open_device_call_ui с DeviceInfo, как в образце.

Колбэк принимает обработчик умного дома (smart_home.py, вместе с действиями
устройств); устройств шторки нет — обычный путь (заглушка колбэков).
"""
import copy
import json
import re
import urllib.parse
import uuid
from pathlib import Path

TEMPLATE_FILE = Path(__file__).parent / "upper_shutter.json"
PURPOSE = "collect_upper_shutter"
CALLBACK_EVENT = "@@mm_typed_callback"

# Категории чипов в порядке образца: фильтр → подпись.
FILTERS = (("Sensors", "Датчики"), ("Climate", "Климат"), ("Speakers", "Колонки"),
           ("Vacuum", "Пылесосы"), ("Light", "Свет"), ("TV", "ТВ"), ("Scenarios", "Сценарии"))

# Тип устройства с вкл/выкл → (фильтр или None, иконка) — по образцу: лампы — «Свет»,
# телевизоры — «ТВ», вентилятор без фильтра (виден, только когда фильтры сняты).
ON_OFF_TYPES = {"LightDeviceType": ("Light", "lamp-other"),
                "LightLampDeviceType": ("Light", "table-lamp"),
                "TvDeviceDeviceType": ("TV", "tv"),
                "FanDeviceType": (None, "uso"),
                "FanDeviceTypeV2": (None, "uso")}

# Тип устройства со значением свойства → (фильтр, иконка, есть ли вкл/выкл): датчики —
# «Датчики», термостат — «Климат», пылесос — «Пылесосы»; у датчика переменной
# вкл/выкл в образце нет.
VALUE_TYPES = {"ClimateSensorDeviceType": ("Sensors", "sensor-climate", False),
               "SensorDeviceType": ("Sensors", "sensor", False),
               "ThermostatDeviceType": ("Climate", "thermostat", True),
               "VacuumCleanerDeviceType": ("Vacuum", "vaccleaner", True)}

# Инстанс значения реестра → (инстанс переменной, иконка свойства, единица) — только
# те, что есть в образце; значение без образца плитку не даёт.
PROPERTIES = {"temperature": ("temperature", "temperature", "°C"),
              "humidity": ("humidity", "humidity", "%"),
              "battery": ("battery_level", "battery-level", "%"),
              "voltage": ("voltage", "voltage", "В"),
              "power": ("power", "power", "Вт")}

# Платформа колонки → иконка шторки (smart-display-res/iot/devices). Цвет корпуса
# эмулятору неизвестен: чёрный, где он есть в образце, иначе единственный из образца;
# платформа без образца — иконка Станции Макс.
STATION_ICONS = {"chiron": "station-Duo-max-black", "yandexstation_2": "station-max-black",
                 "yandexmidi": "station-2-blue", "yandexmini": "station-mini",
                 "yandexmicro": "station-light-purple"}
STATION_ICON_DEFAULT = "station-max-black"

_template = None


def variable_key(device_id):
    """Часть имени div-переменной из id устройства: всё, кроме букв, цифр и «_», → «_»
    (в образце uuid с «_» вместо «-»; у id реестра ещё «:» и «.»)."""
    return re.sub(r"\W", "_", str(device_id), flags=re.ASCII)


def _load_template():
    global _template
    if _template is None:
        _template = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
    return _template


def _dialog_url(directives):
    return "dialog://?directives=" + urllib.parse.quote(
        json.dumps(directives, ensure_ascii=False, separators=(",", ":")), safe="")


def _render(node, values):
    """Копия узла шаблона: @@KEY@@ в строках → values[KEY], {"$dialog": …} → ссылка dialog://."""
    if isinstance(node, dict):
        if set(node) == {"$dialog"}:
            return _dialog_url(_render(node["$dialog"], values))
        return {key: _render(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [_render(value, values) for value in node]
    if isinstance(node, str) and "@@" in node:
        for key, value in values.items():
            node = node.replace(f"@@{key}@@", value)
    return node


def _on_off(device):
    """Значение умения on_off устройства (None — умения нет)."""
    for cap in device.get("capabilities") or []:
        if str(cap.get("type") or "") == "on_off":
            return bool(cap.get("value"))
    return None


def _property(device):
    """(инстанс, иконка, единица, значение-строка) первого значения устройства из образца:
    уровень с инстансом или батарея; None — значения нет."""
    for cap in device.get("capabilities") or []:
        kind = str(cap.get("type") or "")
        known = PROPERTIES.get(kind if kind == "battery" else
                               str(cap.get("instance") or "") if kind == "level" else "")
        if known is None:
            continue
        value = cap.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(int(value)) if float(value).is_integer() else str(float(value))
        else:
            text = "--"
        return (*known, text)
    return None


def _tiles(registry, stations=()):
    """[(комната, устройство, плитка)] — устройства и колонки шторки в порядке показа;
    плитка — {filter, icon, state (вкл/выкл или None), property (или None), station?}."""
    rooms = registry.rooms()
    result = []
    for device in registry.devices():
        kind = str(device.get("type") or "")
        state = _on_off(device)
        if kind in ON_OFF_TYPES and state is not None:
            flt, icon = ON_OFF_TYPES[kind]
            tile = {"filter": flt, "icon": icon, "state": state, "property": None}
        elif kind in VALUE_TYPES and (prop := _property(device)) is not None:
            flt, icon, switchable = VALUE_TYPES[kind]
            tile = {"filter": flt, "icon": icon, "property": prop,
                    "state": bool(state) if switchable else None}
        else:
            continue
        result.append((rooms.get(str(device.get("room") or ""), ""), device, tile))
    for station in stations:
        tile = {"filter": "Speakers", "state": None, "property": None, "station": True,
                "icon": STATION_ICONS.get(station["platform"], STATION_ICON_DEFAULT)}
        result.append((station["room"], station, tile))
    result.sort(key=lambda item: (not item[0], item[0].lower(), item[1]["name"].lower()))
    return result


def _template_name(device, tile):
    """Шаблон плитки: вкл/выкл, датчик или устройство со значением; в сети и не в сети.
    Устройство со значением в сети (образца нет) — форма датчика в сети."""
    online = bool(device.get("online"))
    if tile.get("station"):
        return "tile_station" if online else "tile_station_offline"
    if tile["property"] is None:
        return "tile_on_off" if online else "tile_on_off_offline"
    if online:
        return "tile_property"
    return "tile_device_property_offline" if tile["state"] is not None else "tile_property_offline"


def card(registry, stations=()):
    """div2 JSON карточки шторки по реестру и колонкам пользователя (smart_home.stations);
    None — показывать нечего."""
    from .smart_home import DEFAULT_HOUSEHOLD
    tiles = _tiles(registry, stations)
    if not tiles:
        return None
    template = _load_template()
    body = copy.deepcopy(template["card"])
    root = body["card"]["states"][0]["div"]
    filters_gallery, devices_gallery = root["items"][0]["items"][:2]
    present = {tile["filter"] for _room, _device, tile in tiles}
    filters_gallery["items"] = [_render(template["filter_chip"], {"FILTER": key, "LABEL": label})
                                for key, label in FILTERS if key in present]
    variables = body["card"]["variables"]
    items = []
    for room, device, tile in tiles:
        var = variable_key(device["id"])
        flt = tile["filter"]
        values = {"VAR": var, "ID": str(device["id"]), "NAME": device["name"], "ROOM": room,
                  "ICON": tile["icon"], "SN": str(device.get("sn") or ""),
                  "HOUSEHOLD": DEFAULT_HOUSEHOLD["name"],
                  "FILTER": (f"iot_filter_{flt} == 'on' || upper_shutter_all_filters_is_off"
                             if flt else "false || upper_shutter_all_filters_is_off")}
        if tile["state"] is not None:
            variables.append({"type": "boolean", "name": f"device_{var}_on_off_state",
                              "value": tile["state"]})
        if tile["property"] is not None:
            instance, icon, unit, text = tile["property"]
            values.update({"INSTANCE": instance, "PICON": icon, "UNIT": unit})
            variables.append({"type": "string", "value": text,
                              "name": f"property_{var}_{instance}_devices.properties.float_value"})
        items.append(_render(template[_template_name(device, tile)], values))
    items.append(_render(template["tile_show_all"], {}))
    devices_gallery["items"] = items
    return json.dumps(_render(body, {}), ensure_ascii=False, separators=(",", ":"))


def is_request(incoming):
    """TextInput — колбэк сбора шторки (@@mm_typed_callback, purpose collect_upper_shutter)."""
    event = incoming.Event.TextInput.Request.Event
    if event.Name != CALLBACK_EVENT:
        return False
    analytics = event.Payload.fields.get("analytics")
    if analytics is None or analytics.WhichOneof("kind") != "struct_value":
        return False
    purpose = analytics.struct_value.fields.get("purpose")
    return purpose is not None and purpose.string_value == PURPOSE


async def on_text_input(conversation, incoming, trigger):
    """Колбэк шторки: InputStartAck + AliceResponse с set_upper_shutter. Не колбэк
    шторки или реестр без устройств шторки — обычный путь."""
    runtime = getattr(conversation, "runtime", None)
    registry = getattr(runtime, "smart_home", None)
    if registry is None or not is_request(incoming):
        return False
    from .smart_home import stations
    body = card(registry, stations(runtime, conversation.trace_context.get("sn")))
    if body is None:
        return False
    await conversation.message(conversation.event(incoming, "InputStartAck"))
    output = conversation.event(incoming, "AliceResponse")
    response = output.Event.AliceResponse
    response.Header.RequestId = getattr(incoming.Event.TextInput.Header, "RequestId", "") or ""
    response.Header.ResponseId = uuid.uuid4().hex
    response.VoiceResponse.SetInParent()
    directive = response.Response.Directives.add()
    directive.Type = "client_action"
    directive.Name = "set_upper_shutter"
    directive.IsLedSilent = True
    directive.Payload.update({"div2_card": {
        "string_body": body,
        "id": {"card_id": "upper_shutter_key_static_key", "card_name": "upper_shutter"},
        "card_name": "CentaurUpperShutterData"}})
    await conversation.message(output)
    return True
