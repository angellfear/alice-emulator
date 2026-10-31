"""Домен traffic: журнал трафика прохода наверх (TASK-043).

GET /api/traffic                → список обменов (фильтры device/host/source, limit/offset; без тел);
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


def present(blob, content_type=""):
    """Тело для карточки: JSON и protobuf — текстом, иначе hex (до 4 КиБ) или base64."""
    if not blob:
        return None
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
    if blob.startswith(b"AAPI") and schemas_ready():
        payload = blob[4:]
        for name in AAPI_TYPES:
            try:
                message = load_type(ROOT / "schemas", "alice-api.pb", name).FromString(payload)
                return {"encoding": "protobuf", "text": text_format.MessageToString(message)}
            except Exception:  # noqa: BLE001 — не тот тип, пробуем следующий
                continue
    try:  # схематичный декодер wire-формата (каталог, прочий protobuf)
        return {"encoding": "protobuf", "text": to_source(decode_named(blob))}
    except Exception:  # noqa: BLE001 — не protobuf
        pass
    if len(blob) <= HEX_LIMIT:
        return {"encoding": "hex", "text": blob.hex()}
    return {"encoding": "base64", "text": base64.b64encode(blob).decode()}


def presented(record):
    """Карточка с телами и кадрами в показываемом виде; сырые bytes наружу не идут."""
    for name in ("request_body", "response_body"):
        record[name] = present(record.get(name))
    for frame in record.get("frames") or []:
        frame["data"] = present(frame.get("data"))
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
    filters = {key: request.query[key] for key in ("device", "host", "source") if request.query.get(key)}
    return web.json_response(journal.list(**filters,
                                          limit=int(request.query.get("limit", 100)),
                                          offset=int(request.query.get("offset", 0))))
