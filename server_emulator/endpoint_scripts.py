"""Обработчики эндпоинтов кодом: <id>.py рядом с <id>.json (режим "script").

Контракт файла-обработчика:

    from server_emulator.endpoint_scripts import reply

    async def handle(request, emulator):        # или def handle(request) — синхронно
        platform = request.headers.get("X-YaQuasarPlatform", "")
        return {"platform": platform}            # dict/list → JSON 200

Возврат: dict/list — JSON; bytes/str — тело с content_type из JSON-файла;
reply(body, status=, content_type=, headers=) — полный контроль; None — ответ
из JSON (статичный/шаблон), т.е. скрипт только для побочных эффектов.
request: method, host, path, query (dict), headers (без учёта регистра),
body (bytes), text(), json(), client_ip, device_id, device (запись реестра|None).
emulator — живой Runtime (параметры, реестр, хранилище). Модуль перечитывается
при изменении файла (движок scripting.ScriptRunner); исключение → 500 с текстом
ошибки, сервер продолжает работу.
"""
import json

from .scripting import ScriptError, ScriptRunner as _Engine, failure, TIMEOUT  # noqa: F401 — публичные имена


STUB = '''"""Обработчик эндпоинта: вызывается на каждый запрос (см. server_emulator/endpoint_scripts.py)."""
from server_emulator.endpoint_scripts import reply


async def handle(request, emulator):
    return {
        "platform": request.headers.get("X-YaQuasarPlatform", ""),
        "device_id": request.device_id,
    }
'''


class Reply:
    __slots__ = ("body", "status", "content_type", "headers")

    def __init__(self, body=b"", status=200, content_type=None, headers=None):
        self.body, self.status, self.content_type, self.headers = body, status, content_type, dict(headers or {})


def reply(body=b"", status=200, content_type=None, headers=None):
    """Ответ с явным статусом/типом/заголовками; body — bytes, str, dict или list."""
    return Reply(body, status, content_type, headers)


class ScriptRequest:
    def __init__(self, method, host, path, query, headers, body, client_ip="", device_id="", device=None):
        self.method, self.host, self.path = method, host, path
        self.query, self.headers, self.body = query, headers, body
        self.client_ip, self.device_id, self.device = client_ip, device_id, device

    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body or b"null")


class ScriptRunner(_Engine):
    """Эндпоинтный вид скриптов: результат → Reply; None — ответ JSON-файла."""

    def __init__(self):
        super().__init__(prefix="endpoint_script")

    async def run(self, path, request, emulator):
        """→ Reply | None (None — отдать ответ JSON-файла)."""
        result = await super().run(path, request, emulator)
        if result is None or isinstance(result, Reply):
            return result
        return Reply(result)


def encode(result, default_type):
    """Reply → (status, bytes, content_type, headers)."""
    body, content_type = result.body, result.content_type
    if isinstance(body, (dict, list)):
        body, content_type = json.dumps(body, ensure_ascii=False).encode(), content_type or "application/json"
    elif isinstance(body, str):
        body = body.encode()
    elif body is None:
        body = b""
    return int(result.status), bytes(body), content_type or default_type, result.headers
