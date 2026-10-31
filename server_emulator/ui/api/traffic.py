"""Домен traffic: журнал трафика устройств (TASK-043, TASK-054).

GET /api/traffic                → список обменов (фильтры device/host/source/unhandled=1/key, limit/offset; без тел);
GET /api/traffic?groups=1       → группы событий по устройствам (фильтры device_sn/source/unhandled=1):
                                  [{device_sn, name, record, groups: [{key, count, first_ts, last_ts}]}],
                                  count — все случаи события, записей в группе не больше 10;
GET /api/traffic?device_sn=&key= → записи одной группы (фильтры source/unhandled=1; device_sn пустой — без устройства);
GET /api/traffic?item=<id>      → карточка: заголовки, тела и кадры WS — protobuf/JSON показываются
                                  текстом (есть декодер в эмуляторе), иначе hex/base64;
GET /api/traffic?export=<id>    → JSON-файл обмена; скачивание делает браузер по ссылке API;
DELETE /api/traffic[?item=<id>] → очистка журнала или удаление одной записи.
"""
import base64
import json
from pathlib import Path

from aiohttp import web
from google.protobuf import text_format

from ...protowire import decode_named, to_source
from ...schema import load_type, schemas_ready
from . import route

ROOT = Path(__file__).resolve().parent.parent.parent  # server_emulator/
HEX_LIMIT = 4096
AAPI_TYPES = ("NAlice.NAliceApi.TServerMessage", "NAlice.NAliceApi.TClientMessage")


def present(blob, content_type="", aapi=None):
    """Тело для карточки: JSON и protobuf — текстом, иначе hex (до 4 КиБ) или base64.
    aapi — тип AAPI-сообщения по направлению кадра (клиентское или серверное), если оно известно."""
    if not blob:
        return None
    if blob.startswith(b"AAPI") and schemas_ready():
        payload = blob[4:]
        for name in ((aapi,) if aapi else AAPI_TYPES):
            try:
                message = load_type(ROOT / "schemas", "alice-api.pb", name).FromString(payload)
                return {"encoding": "protobuf", "text": text_format.MessageToString(message)}
            except Exception:  # noqa: BLE001 — не тот тип, пробуем следующий
                continue
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None:
        stripped = text.lstrip()
        if content_type.split(";", 1)[0].strip() == "application/json" or stripped[:1] in ("{", "["):
            try:
                return {"encoding": "json", "text": json.dumps(json.loads(text), ensure_ascii=False, indent=2)}
            except ValueError:
                return {"encoding": "text", "text": text}
        return {"encoding": "text", "text": text}
    try:  # схематичный декодер wire-формата (каталог, прочий protobuf)
        return {"encoding": "protobuf", "text": to_source(decode_named(blob))}
    except Exception:  # noqa: BLE001 — не protobuf
        pass
    if len(blob) <= HEX_LIMIT:
        return {"encoding": "hex", "text": blob.hex()}
    return {"encoding": "base64", "text": base64.b64encode(blob).decode()}


def presented(record):
    """Карточка с телами и кадрами в показываемом виде; сырые bytes наружу не идут."""
    ws = record.get("kind") == "ws"
    for name, direction, aapi in (("request_body", "client_to_server", AAPI_TYPES[1]),
                                  ("response_body", "server_to_client", AAPI_TYPES[0])):
        record[name] = present(record.get(name), aapi=aapi if ws and record.get("direction") == direction else None)
    for frame in record.get("frames") or []:
        frame["data"] = present(frame.get("data"), aapi=AAPI_TYPES[1] if frame.get("direction") == "client_to_server"
                                else AAPI_TYPES[0])
    return record


@route("traffic", methods=("GET", "DELETE"))
async def traffic(request, runtime, app):
    journal = runtime.upstream.journal
    if request.method == "DELETE":
        item = request.query.get("item")
        await journal.flush()  # очистка не должна конкурировать с очередью записи
        journal.forget(None if not item else int(item))
        return web.json_response({"cleared": item or "*"})
    if request.query.get("item"):
        record = journal.item(int(request.query["item"]))
        if record is None:
            raise KeyError("Записи нет в журнале")
        return web.json_response(presented(record))
    if request.query.get("export"):
        record = journal.item(int(request.query["export"]))
        if record is None:
            raise KeyError("Записи нет в журнале")
        return web.json_response(presented(record), headers={
            "Content-Disposition": f'attachment; filename="traffic-{record["id"]}.json"'})
    unhandled = request.query.get("unhandled") == "1"
    source = request.query.get("source") or None
    if request.query.get("groups"):
        devices = {d["sn"]: d for d in runtime.identities.data["devices"]}
        result = journal.groups(device_sn=request.query.get("device_sn"), source=source, unhandled=unhandled)
        for entry in result:
            known = devices.get(entry["device_sn"])
            entry["name"] = known.get("name") if known else None
            entry["record"] = (known.get("record") or "off") if known else None
        return web.json_response({"devices": result})
    if "key" in request.query and "device_sn" in request.query:
        return web.json_response({"items": journal.group_items(request.query["device_sn"], request.query["key"],
                                                              source=source, unhandled=unhandled)})
    filters = {key: request.query[key] for key in ("device", "host", "source") if request.query.get(key)}
    return web.json_response(journal.list(**filters, unhandled=unhandled,
                                          limit=int(request.query.get("limit", 100)),
                                          offset=int(request.query.get("offset", 0))))
