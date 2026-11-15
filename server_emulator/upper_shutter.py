"""Блок умного дома в верхней шторке колонки: div2-карточка set_upper_shutter.

Шторку рисует не лаунчер по своей базе, а сервер: колонка шлёт TextInput с событием
@@mm_typed_callback (Payload.analytics.purpose = collect_upper_shutter), сервер
отвечает InputStartAck и AliceResponse с одной директивой client_action
set_upper_shutter (Payload.div2_card: string_body — div2 JSON, id {card_id
upper_shutter_key_static_key, card_name upper_shutter}, card_name
CentaurUpperShutterData; IsLedSilent) — форма настоящего ответа из журнала.

Карточка собирается по реестру умного дома из обезличенного шаблона
upper_shutter.json (каркас, чип фильтра, плитки вкл/выкл в сети и не в сети,
«Показать все»): чипы — только присутствующих категорий, плитки — группами по
комнатам по имени, без комнаты — в конце, затем «Показать все». Переменная состояния
плитки — device_<id>_on_off_state (variable_key), та же, что в set_div_variables.
Ссылки dialog:// в шаблоне хранятся раскрытыми ({"$dialog": [директивы]}) и
кодируются при сборке, как в образце: компактный JSON, quote без безопасных символов.

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


def _tiles(registry):
    """[(комната, фильтр, устройство, состояние)] — устройства шторки в порядке показа."""
    rooms = registry.rooms()
    result = []
    for device in registry.devices():
        kind = ON_OFF_TYPES.get(str(device.get("type") or ""))
        state = _on_off(device)
        if kind is None or state is None:
            continue
        result.append((rooms.get(str(device.get("room") or ""), ""), kind, device, state))
    result.sort(key=lambda item: (not item[0], item[0].lower(), item[2]["name"].lower()))
    return result


def card(registry):
    """div2 JSON карточки шторки по реестру; None — показывать нечего."""
    tiles = _tiles(registry)
    if not tiles:
        return None
    template = _load_template()
    body = copy.deepcopy(template["card"])
    root = body["card"]["states"][0]["div"]
    filters_gallery, devices_gallery = root["items"][0]["items"][:2]
    present = {kind[0] for _room, kind, _device, _state in tiles}
    filters_gallery["items"] = [_render(template["filter_chip"], {"FILTER": key, "LABEL": label})
                                for key, label in FILTERS if key in present]
    variables = body["card"]["variables"]
    items = []
    for room, (flt, icon), device, state in tiles:
        var = variable_key(device["id"])
        visibility = (f"iot_filter_{flt} == 'on' || upper_shutter_all_filters_is_off" if flt
                      else "false || upper_shutter_all_filters_is_off")
        tile = template["tile_on_off" if device.get("online") else "tile_on_off_offline"]
        items.append(_render(tile, {"VAR": var, "ID": str(device["id"]), "NAME": device["name"],
                                    "ROOM": room, "ICON": icon, "FILTER": visibility}))
        variables.append({"type": "boolean", "name": f"device_{var}_on_off_state", "value": state})
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
    registry = getattr(getattr(conversation, "runtime", None), "smart_home", None)
    if registry is None or not is_request(incoming):
        return False
    body = card(registry)
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
