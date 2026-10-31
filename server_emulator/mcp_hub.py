"""MCP-хаб: инструменты модели — встроенные и внешние MCP-серверы из mcp.json.

Встроенные (всегда у модели, исполняются в цикле Runtime.chat сами):
speakers_list — колонки пользователя; speaker_command — push-команда колонке
(каталог actions.CATALOG); db_query — SELECT по снимку данных пользователя
(in-memory SQLite: колонки, пользователь, параметры без секретов, таймеры,
данные сценариев) — модель видит, с чем работает; web_search / web_fetch — веб-поиск
без ключей и загрузка страницы (web_tools.py). Плюс base_context — базовые
сведения в системный промпт: время, пользователь, текущая колонка, все колонки.

Внешние — state/mcp.json в стандартном формате {"mcpServers": {id: {...}}}:
http ({type: "http", url, headers}) — MCP streamable HTTP, каждый вызов — своя
MCP-сессия; stdio ({command, args, env}) — постоянный процесс. Транспорт и протокол —
официальный SDK (пакет mcp). "disabled": true — сервер выключен. Выбор инструментов
внешних серверов — settings.tools {name, server_id, server_tool, description,
inputSchema, enabled, mode}: mode auto — модель вызывает и получает результат
сама, confirm — вызов ждёт ручного подтверждения во вкладке «Проверка».
Контракт: docs/contracts/mcp.md.

Расширения (TASK-032): папка mcp/ включённого расширения добавляет серверы в список
(только чтение, mcp.json не пишется; класс расширения с mcp/ — code): *.json —
описание {"mcpServers": {имя: spec}} или один spec (stdio-команда или URL); имя без
префикса получает <ext-id>.; spec проверяется как запись mcp.json; пользовательская
запись mcp.json с тем же id побеждает; "disabled": true — сервер выключен; некорректное
описание — warning без падения раздела. Слои — extensions/layers.source_layers.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import sqlite3
import time

from . import web_tools

log = logging.getLogger(__name__)

BUILTIN_ID = "alice"
BUILTIN_TITLE = "Колонки Алисы"
MODES = ("auto", "confirm")
CONFIG_NAME = "mcp.json"
# Заготовка: ha-mcp (аддон Home Assistant) — URL с секретным путём из лога аддона
DEFAULT_CONFIG = {"mcpServers": {"homeassistant": {
    "type": "http", "url": "http://IP_HOME_ASSISTANT:9583/private_SECRET_FROM_ADDON_LOG", "disabled": True}}}


# --- mcp.json ----------------------------------------------------------------------

def config_path(runtime):
    return runtime.directory / CONFIG_NAME


def validate_config(value):
    """Проверка mcp.json; возвращает нормализованный dict или ValueError с причиной."""
    if not isinstance(value, dict) or not isinstance(value.get("mcpServers"), dict):
        raise ValueError('mcp.json: нужен объект {"mcpServers": {…}}')
    for key, server in value["mcpServers"].items():
        where = f"mcpServers.{key}: "
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", key) or key == BUILTIN_ID:
            raise ValueError(where + "имя — латиница, цифры, _ . - (до 40), не «alice»")
        if not isinstance(server, dict):
            raise ValueError(where + "нужен объект")
        kind = transport(server)
        if kind == "http":
            if not isinstance(server.get("url"), str) or not re.match(r"https?://", server["url"]):
                raise ValueError(where + "url — http(s)://…")
            if not isinstance(server.get("headers", {}), dict):
                raise ValueError(where + "headers — объект")
        elif kind == "stdio":
            if not isinstance(server.get("command"), str) or not server["command"]:
                raise ValueError(where + "command — строка")
            if not isinstance(server.get("args", []), list) or not isinstance(server.get("env", {}), dict):
                raise ValueError(where + "args — массив, env — объект")
        else:
            raise ValueError(where + "type: http (streamable) или stdio; старый SSE-транспорт не поддержан")
    return value


def transport(server):
    kind = server.get("type") or ("stdio" if server.get("command") else "http" if server.get("url") else "")
    return {"streamable-http": "http", "streamableHttp": "http", "http": "http", "stdio": "stdio"}.get(kind, kind)


def load_config(runtime):
    """mcp.json с диска; нет файла — создаётся (перенос settings.mcp_servers или заготовка)."""
    path = config_path(runtime)
    if not path.exists():
        legacy = {s["id"]: {"type": "http", "url": s["url"],
                            **({"headers": {"Authorization": "Bearer " + s["api_key"]}} if s.get("api_key") else {}),
                            **({"disabled": True} if s.get("enabled") is False else {})}
                  for s in runtime.settings.get("mcp_servers", []) if s.get("id") and s.get("url")}
        save_config(runtime, {"mcpServers": legacy} if legacy else DEFAULT_CONFIG)
    return validate_config(json.loads(path.read_text(encoding="utf-8")))


def save_config(runtime, value):
    validate_config(value)
    path = config_path(runtime)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    for name in list(_STDIO):  # изменённые stdio-серверы перезапустятся при следующем вызове
        _STDIO.pop(name).close()


def extension_servers(runtime):
    """Серверы mcp/ включённых расширений (TASK-032): {id: spec} — *.json файла:
    {"mcpServers": {имя: spec}} или одно описание (stdio-команда или URL); имя без
    префикса получает <ext-id>.; spec проверяется правилами mcp.json (validate_config),
    битое описание — warning, остальные серверы работают; у записи origin
    «extension:<id>». Список только читается: mcp.json не пишется и не мутируется."""
    from .extensions.layers import layer_records, source_layers

    def warn(message):
        log.warning("Расширения (MCP): %s", message)

    manager = getattr(runtime, "extensions", None)
    try:
        layers = source_layers(manager, "mcp")
    except Exception:  # noqa: BLE001 — расширения не должны ломать раздел
        return {}
    result = {}
    for origin, path, value in layer_records(layers, warn=warn):
        ext_id = origin.split(":", 1)[1] if str(origin).startswith("extension:") else path.parent.name
        items = value.get("mcpServers") if isinstance(value, dict) and isinstance(value.get("mcpServers"), dict) \
            else {path.stem: value}
        for name, spec in items.items():
            identifier = str(name) if str(name).startswith(ext_id + ".") else f"{ext_id}.{name}"
            if not isinstance(spec, dict):
                warn(f"{path}: {name}: нужен объект-описание сервера")
                continue
            try:
                validate_config({"mcpServers": {identifier: spec}})
            except ValueError as exc:
                warn(f"{path}: {exc}")
                continue
            result[identifier] = {**spec, "origin": origin}
    return result


def servers(runtime):
    """Все серверы: описания mcp/ включённых расширений + mcp.json — пользовательская
    запись с тем же id побеждает (TASK-032); настройки раздела не мутируются."""
    merged = extension_servers(runtime)
    merged.update(load_config(runtime)["mcpServers"])
    return merged


def server_of(runtime, server_id):
    server = servers(runtime).get(server_id)
    if server is None:
        raise ValueError("MCP-сервер не найден в mcp.json: " + str(server_id))
    return server


# --- транспорт: официальный MCP SDK (пакет mcp) ------------------------------------------

def _plain(result):
    """Результат SDK (pydantic) → dict в форме протокола: inputSchema, isError, nextCursor."""
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


async def _session_call(session, method, params):
    if method == "tools/list":
        return _plain(await session.list_tools(cursor=(params or {}).get("cursor")))
    if method == "tools/call":
        return _plain(await session.call_tool(params["name"], params.get("arguments") or {}))
    raise ValueError("MCP: метод не поддержан: " + method)


def _error(exc):
    """Ошибки SDK (в т.ч. из группы задач anyio) → ValueError с понятным текстом."""
    import httpx
    from mcp.shared.exceptions import McpError
    found = [exc]
    while found:
        item = found.pop()
        if isinstance(item, BaseExceptionGroup):
            found += list(item.exceptions)
        elif isinstance(item, httpx.HTTPStatusError):
            status = item.response.status_code
            hint = ": проверьте URL (секретный путь) и заголовки" if status in (401, 403, 404) else ""
            return ValueError(f"MCP HTTP {status}{hint}")
        elif isinstance(item, McpError):
            return ValueError("MCP: " + str(item.error.message))
        elif isinstance(item, (httpx.TransportError, OSError)):
            return ValueError("MCP: сервер недоступен: " + (str(item) or type(item).__name__))
        elif isinstance(item, ValueError):
            return item
    return ValueError("MCP: " + (str(exc) or type(exc).__name__))


async def _http_request(server, method, params, timeout):
    """Streamable HTTP: каждый вызов — своя MCP-сессия (initialize → запрос → закрытие)."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    headers = {str(k): str(v) for k, v in (server.get("headers") or {}).items()}
    try:
        async with asyncio.timeout(timeout),                 create_mcp_http_client(headers=headers, timeout=httpx.Timeout(timeout)) as http,                 streamable_http_client(server["url"], http_client=http) as (read, write, _),                 ClientSession(read, write) as session:
            await session.initialize()
            return await _session_call(session, method, params)
    except TimeoutError:
        raise ValueError("MCP: сервер не ответил за " + str(timeout) + " с")
    except Exception as exc:
        raise _error(exc) from exc


class _Stdio:
    """stdio: постоянный процесс на сервер. Сессия SDK живёт в своей задаче (anyio требует
    открывать и закрывать её в одной задаче), запросы приходят через очередь."""

    def __init__(self, server):
        self.server, self.task, self.queue, self.lock = server, None, None, asyncio.Lock()

    async def _serve(self, ready):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        params = StdioServerParameters(command=self.server["command"], args=[str(a) for a in self.server.get("args", [])],
                                       env={**os.environ, **{str(k): str(v) for k, v in (self.server.get("env") or {}).items()}})
        try:
            with open(os.devnull, "w") as devnull:
                async with stdio_client(params, errlog=devnull) as (read, write), ClientSession(read, write) as session:
                    await session.initialize()
                    ready.set_result(None)
                    while True:
                        method, arguments, future = await self.queue.get()
                        try:
                            result = await _session_call(session, method, arguments)
                        except Exception as exc:
                            if not future.done():
                                future.set_exception(_error(exc))
                        else:
                            if not future.done():
                                future.set_result(result)
        except Exception as exc:
            if not ready.done():
                ready.set_exception(ValueError("MCP stdio: команда не найдена: " + self.server["command"])
                                    if isinstance(exc, FileNotFoundError) else _error(exc))

    async def request(self, method, params, timeout):
        async with self.lock:
            if self.task is None or self.task.done():
                ready = asyncio.get_running_loop().create_future()
                self.queue = asyncio.Queue()
                self.task = asyncio.create_task(self._serve(ready))
                await asyncio.wait_for(ready, 60)
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((method, params, future))
        return await asyncio.wait_for(future, timeout)

    def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()


_STDIO = {}


async def request(runtime, server, method, params, timeout=30, server_id=None):
    """Один MCP-запрос к серверу из mcp.json (http или stdio): tools/list | tools/call."""
    if transport(server) == "stdio":
        key = server_id or server["command"]
        if key not in _STDIO or _STDIO[key].server != server:
            if key in _STDIO:
                _STDIO.pop(key).close()
            _STDIO[key] = _Stdio(server)
        return await _STDIO[key].request(method, params, timeout)
    return await _http_request(server, method, params, timeout)


async def list_tools(runtime, server, server_id=None):
    """tools/list со всеми страницами."""
    tools, cursor = [], None
    for _ in range(20):
        result = await request(runtime, server, "tools/list", {"cursor": cursor} if cursor else {}, server_id=server_id)
        tools += result.get("tools", [])
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return tools


# --- встроенные инструменты -----------------------------------------------------------

def builtin_tools():
    from .actions import CATALOG
    return [
        {"name": "speakers_list",
         "description": "Колонки Алисы пользователя: sn, название, комната, платформа, в сети ли, текущая ли (с неё говорят).",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "speaker_command",
         "description": "Выполнить команду на колонке Алисы (колонка должна быть в сети). Команды: "
                        + "; ".join(f"{a['id']} — {a['name']}" for a in CATALOG)
                        + ". sn не указан — текущая колонка.",
         "inputSchema": {"type": "object", "properties": {
             "action": {"type": "string", "enum": [a["id"] for a in CATALOG]},
             "sn": {"type": "string", "description": "SN колонки из speakers_list"},
             "level": {"type": "number", "description": "громкость 0..10 для sound_set_level"},
             "frames": {"type": "array", "items": {"type": "string"}, "description": "URL кадров для draw_led_screen"}},
             "required": ["action"]}},
        {"name": "db_query",
         "description": "SQL SELECT (SQLite, только чтение) по данным пользователя. Таблицы: "
                        + "; ".join(f"{t}({', '.join(c)})" for t, c in SNAPSHOT_TABLES.items())
                        + ". Время — локальное ISO. До 200 строк.",
         "inputSchema": {"type": "object", "properties": {
             "sql": {"type": "string", "description": "один SELECT, например SELECT name, room FROM speakers WHERE online=1"}},
             "required": ["sql"]}},
        *web_tools.TOOLS,
    ]


BUILTIN_NAMES = {"speakers_list", "speaker_command", "db_query"} | web_tools.NAMES

SNAPSHOT_TABLES = {
    "speakers": ("sn", "name", "room", "platform", "online", "current", "user_id"),
    "users": ("id", "name", "city", "latitude", "longitude"),
    "params": ("owner", "key", "title", "value"),
    "timers": ("id", "kind", "sn", "label", "duration_s", "created", "due", "status", "time_hhmm", "weekdays"),
    "device_state": ("sn", "key", "value"),
    "scenario_data": ("scenario_id", "scope", "owner", "key", "value", "updated"),
}


def _live(runtime):
    return {s.get("device_sn") for s in runtime.sessions.values() if s.get("send")}


def _speakers(runtime, context):
    from .updates import platform_of
    user_id, live = context.get("user_id"), _live(runtime)
    return [{"sn": d["sn"], "name": d["name"], "room": d.get("room", ""), "platform": platform_of(d),
             "online": d["sn"] in live, "current": d["sn"] == context.get("sn"), "user_id": d.get("user_id")}
            for d in runtime.identities.data["devices"]
            if d.get("user_id") and (user_id is None or d.get("user_id") == user_id)]


def _local(ts, tz=None):
    if not ts:
        return None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz) if tz else None
    except Exception:
        zone = None
    return datetime.datetime.fromtimestamp(float(ts), zone).isoformat(timespec="seconds")


def _timezone(runtime, context):
    for key in ("device.timezone", "user.timezone", "server.timezone"):
        try:
            value = runtime.quasar_params.resolve(key, context.get("user_id"), context.get("sn"))
            if value:
                return str(value)
        except KeyError:
            continue
    return None


def snapshot(runtime, context):
    """In-memory SQLite с данными, видимыми модели: только свой пользователь
    (в голосовой сессии), секреты параметров — '***', служебные таблицы не попадают."""
    tz = _timezone(runtime, context)
    db = sqlite3.connect(":memory:")
    for table, columns in SNAPSHOT_TABLES.items():
        db.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
    speakers = _speakers(runtime, context)
    sns = {s["sn"] for s in speakers}
    db.executemany("INSERT INTO speakers VALUES (?,?,?,?,?,?,?)",
                   [tuple(int(s[c]) if isinstance(s[c], bool) else s[c] for c in SNAPSHOT_TABLES["speakers"]) for s in speakers])
    user_ids = {s["user_id"] for s in speakers} | ({context["user_id"]} if context.get("user_id") else set())
    for user in runtime.identities.data["users"]:
        if context.get("user_id") is None or user["id"] in user_ids:
            location = user.get("location") or {}
            db.execute("INSERT INTO users VALUES (?,?,?,?,?)", (user["id"], user.get("name"), location.get("city"),
                                                                 location.get("latitude"), location.get("longitude")))
    defs = {d["key"]: d for d in runtime.quasar_params.defs()}
    owners = [f"user:{u}" for u in user_ids] + [f"device:{sn}" for sn in sns]
    for owner in owners:
        for key, item in runtime.quasar_params.values(owner).items():
            definition = defs.get(key, {})
            value = "***" if definition.get("type") == "secret" else item.get("value")
            db.execute("INSERT INTO params VALUES (?,?,?,?)", (owner, key, definition.get("title"),
                                                               json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value))
    for device in runtime.identities.data["devices"]:
        if device["sn"] in sns:
            for key, value in (device.get("state") or {}).items():
                if key == "quasar_legacy":
                    continue  # служебный конфиг колонки — не для модели
                db.execute("INSERT INTO device_state VALUES (?,?,?)", (device["sn"], key, json.dumps(value, ensure_ascii=False)[:2000]))
    source = runtime.store.db  # общее соединение хранилища, только чтение
    for row in source.execute("SELECT id,kind,sn,label,duration_s,created_ts,due_ts,status,time_hhmm,weekdays,next_ts FROM timers"):
        if row[2] in sns:
            db.execute("INSERT INTO timers VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (row[0], row[1], row[2], row[3], row[4], _local(row[5], tz), _local(row[10] or row[6], tz), row[7], row[8], row[9]))
    for row in source.execute("SELECT scenario_id,scope,owner,key,value,updated_ts FROM scenario_data"):
        if row[1] == "system" or row[2] in user_ids or row[2] in sns or context.get("user_id") is None:
            db.execute("INSERT INTO scenario_data VALUES (?,?,?,?,?,?)", (*tuple(row)[:4], str(row[4])[:4000], _local(row[5], tz)))
    db.commit()
    return db


def _deny_writes(action, *args):
    return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY


def db_query(runtime, context, sql):
    if not isinstance(sql, str) or not re.match(r"\s*(select|with)\b", sql, re.I) or ";" in sql.strip().rstrip(";"):
        raise ValueError("db_query: один SELECT")
    db = snapshot(runtime, context)
    try:
        db.set_authorizer(_deny_writes)
        cursor = db.execute(sql.strip().rstrip(";"))
        columns = [c[0] for c in cursor.description or []]
        rows = cursor.fetchmany(200)
    except sqlite3.Error as exc:
        raise ValueError("db_query: " + str(exc))
    finally:
        db.close()
    return {"columns": columns, "rows": rows, "truncated": len(rows) == 200}


async def _builtin(runtime, name, arguments, context):
    if name == "speakers_list":
        return _speakers(runtime, context)
    if name == "db_query":
        return db_query(runtime, context, arguments.get("sql"))
    if name in web_tools.NAMES:
        return await web_tools.call(name, arguments, runtime.settings.get("web_tools"))
    if name != "speaker_command":
        raise ValueError("Неизвестный встроенный инструмент: " + name)
    from .actions import send_action
    sn = arguments.get("sn") or context.get("sn")
    if not sn:
        raise ValueError("Не указан sn колонки")
    if sn not in {s["sn"] for s in _speakers(runtime, context)}:
        raise ValueError("Колонка не найдена у пользователя: " + sn)
    extra = {k: arguments[k] for k in ("level", "frames") if k in arguments}
    item = await send_action(runtime, sn, arguments.get("action"), extra)
    return {"sent": True, "sn": sn, "action": arguments.get("action"), "message_id": item["message_id"]}


def base_context(runtime, context):
    """Базовые сведения для системного промпта: время, пользователь, колонки, инструменты."""
    tz = _timezone(runtime, context)
    now = _local(time.time(), tz) or ""
    weekday = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][
        datetime.datetime.fromisoformat(now).weekday()] if now else ""
    lines = [f"Сейчас: {now.replace('T', ' ')} ({weekday}{', ' + tz if tz else ''})."]
    user = next((u for u in runtime.identities.data["users"] if u["id"] == context.get("user_id")), None)
    if user:
        city = (user.get("location") or {}).get("city")
        lines.append(f"Пользователь: {user.get('name')}" + (f", город {city}" if city else "") + ".")
    speakers = _speakers(runtime, context)
    current = next((s for s in speakers if s["current"]), None)
    if current:
        lines.append(f"Говорят с колонки «{current['name']}» (sn {current['sn']}"
                     + (f", комната {current['room']}" if current["room"] else "") + ").")
    if speakers:
        lines.append("Колонки: " + "; ".join(
            f"{s['name']} (sn {s['sn']}{', ' + s['room'] if s['room'] else ''}, {'в сети' if s['online'] else 'не в сети'})"
            for s in speakers) + ".")
    lines.append("Инструменты: speakers_list, speaker_command — команды колонкам; db_query — данные пользователя"
                 " (таймеры, параметры, состояние); web_search, web_fetch — интернет (поиск и текст страницы). Если нужны данные или действие — вызывай инструмент, не выдумывай.")
    return "\n".join(lines)


# --- вызов ------------------------------------------------------------------------------

def tool_name(server_id, name):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", server_id + "__" + name)[:64]


def find_tool(runtime, name):
    if name in BUILTIN_NAMES:
        tool = next(t for t in builtin_tools() if t["name"] == name)
        return {**tool, "server_id": BUILTIN_ID, "server_tool": name, "enabled": True, "mode": "auto", "builtin": True}
    return next((t for t in runtime.settings["tools"] if t["name"] == name), None)


def is_auto(runtime, name):
    tool = find_tool(runtime, name)
    return bool(tool and tool.get("enabled") and tool.get("mode", "auto") == "auto" and tool.get("server_id"))


async def call(runtime, name, arguments, context=None, require_enabled=True):
    """Вызов инструмента → {content:[{type:text,text}], isError}."""
    tool = find_tool(runtime, name)
    if tool is None or (require_enabled and not tool.get("enabled")):
        raise ValueError("Инструмент не найден или выключен: " + str(name))
    if not isinstance(arguments, dict):
        raise ValueError("arguments: нужен объект")
    server_id = tool.get("server_id")
    if not server_id:
        raise ValueError("Заготовка не имеет исполнителя: привяжите инструмент к источнику")
    if server_id == BUILTIN_ID:
        try:
            value = await _builtin(runtime, name, arguments, context or {})
            return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
        except ValueError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    server = server_of(runtime, server_id)
    if server.get("disabled"):
        raise ValueError("MCP-сервер выключен в mcp.json: " + server_id)
    return await request(runtime, server, "tools/call",
                         {"name": tool.get("server_tool", name), "arguments": arguments}, timeout=60, server_id=server_id)


def result_text(result, limit=8000):
    """Текст результата для модели (tool message); длинное — обрезается."""
    parts = []
    for item in (result or {}).get("content", []):
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
        else:
            parts.append(json.dumps(item, ensure_ascii=False)[:500])
    if not parts and (result or {}).get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(parts) or "(пустой результат)"
    if (result or {}).get("isError"):
        text = "Ошибка инструмента: " + text
    return text[:limit] + ("…" if len(text) > limit else "")


def model_tools(runtime):
    """Инструменты для модели (формат OpenAI tools): встроенные всегда + включённые внешние
    из невыключенных серверов mcp.json."""
    try:
        active = {k for k, s in servers(runtime).items() if not s.get("disabled")}
    except (ValueError, OSError):
        active = set()
    # заготовки без server_id (импорт из трафика) — предложения для ручного запуска, как раньше
    chosen = builtin_tools() + [t for t in runtime.settings["tools"]
                                if t.get("enabled") and (not t.get("server_id") or t.get("server_id") in active)]
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""),
        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}} for t in chosen]


def merge_tools(existing, server_id, discovered):
    """Обновить список инструментов сервера: описание/схема — новые, выбор пользователя — прежний."""
    old = {t["name"]: t for t in existing if t.get("server_id") == server_id}
    fresh = []
    for tool in discovered:
        name = tool_name(server_id, tool["name"])
        prev = old.get(name, {})
        fresh.append({"name": name, "server_id": server_id, "server_tool": tool["name"],
                      "description": tool.get("description", ""),
                      "inputSchema": tool.get("inputSchema") or {"type": "object", "properties": {}},
                      "enabled": bool(prev.get("enabled", False)), "mode": prev.get("mode", "auto")})
    return [t for t in existing if t.get("server_id") != server_id] + fresh
