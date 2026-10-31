"""system.timesync — служебный колбэк sync_timezone → тихий ответ set_timezone (+ set_location).

Ответ байт-в-байт тот же, что у прежнего JSON-flow: колонка иначе не выходит из
75-секундного цикла повторов. Таймзона: колонка → пользователь → (input.timezone пуст ?
effective : input.timezone); гео — params.effective.location.
"""


def _param(params, *path):
    """Значение params.<путь> или None — как `exists` JSON (пустая строка тоже «есть»)."""
    node = params
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _timezone(ctx):
    params = ctx.params
    for scope in ("device", "user"):
        value = _param(params, scope, "timezone")
        if value is not None:
            return value
    # input.timezone пуст — серверная (effective), иначе заявленная станцией
    return ctx.input_timezone or _param(params, "effective", "timezone")


async def handle(ctx):
    tz = _timezone(ctx)
    directives = [{
        "name": "set_timezone",
        "analytics_type": "set_device_timezone_directive",
        "is_led_silent": True,
        "proto": "NAlice.TGeoSyncCapability.TSetTimezoneDirective",
        "fields": {
            "Timezone": {
                "TimezoneName": tz,
                # смещение считает движок (как у JSON): неизвестная таймзона — ошибка ответа
                "OffsetSeconds": {"source": "computed", "fn": "timezone_offset_seconds",
                                  "args": {"name": tz}},
            },
            "Name": "set_device_timezone_directive",
        },
    }]
    latitude = _param(ctx.params, "effective", "location", "latitude")
    if latitude is not None:  # гео задано — станция запоминает и координаты
        directives.append({
            "name": "set_location",
            "analytics_type": "set_device_location_directive",
            "is_led_silent": True,
            "proto": "NAlice.TGeoSyncCapability.TSetLocationDirective",
            "fields": {
                "Location": {"Latitude": latitude,
                             "Longitude": _param(ctx.params, "effective", "location", "longitude")},
                "Name": "set_device_location_directive",
            },
        })
    return {"envelope": "service", "directives": directives}
