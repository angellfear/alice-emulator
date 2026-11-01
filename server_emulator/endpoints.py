"""Каталог захваченных HTTP-эндпоинтов: переигрывание реальных ответов облака.

Один JSON-файл на эндпоинт, по разделам как библиотека сценариев:
state/endpoints/<category>/<id>.json (category: quasar, gproxy, egress…;
плоские файлы старого формата при загрузке переносятся в свой раздел). Файлы — источник правды (можно править при остановленном
сервере); битые файлы пропускаются. Наполнение — tools/endpoints_recover.py
из capture-базы; сам эмулятор о базе не знает (прокси — отдельный компонент).

Слой расширений (TASK-030): endpoints/*.json включённых расширений — только
чтение, файлы остаются в каталоге расширения (script-обработчик — .py рядом с
JSON, тоже оттуда). Правка записи расширения (upsert/переключатель) создаёт
пользовательскую копию в state/endpoints; пользовательская запись с тем же id
перекрывает запись расширения (в приоритете find/public), удаление пользовательской
копии возвращает запись расширения. Порядок обслуживания в dispatch: builtin
Quasar → ЭНДПОИНТЫ → 404. Эндпоинт не затеняет builtin (stateful-маршруты).

Два источника тела: body_base64 — статичный захват; template — динамический
JSON-шаблон (строки-листья могут нести {{плейсхолдеры}} контекста запроса —
device_id, client_ip, now и пр.). При наличии template тело генерируется
на каждом запросе: захваченные ответы не стареют там, где есть динамика.
"""
import json
import os
import re
import time
import uuid
from pathlib import Path

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
# Режим ответа: static — body_base64, template — JSON с {{токенами}},
# script — обработчик <id>.py рядом с JSON (server_emulator/endpoint_scripts.py).
MODES = ("static", "template", "script")

# Плейсхолдеры контекста запроса: {{имя}} в строках-листьях шаблона.
TOKENS = ("device_id", "platform", "client_ip", "host", "method", "path",
          "now", "now_ms", "now_iso", "uuid")
_TOKEN_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

# Раздел по хосту, если у записи нет category.
HOST_CATEGORIES = {"quasar.yandex.net": "quasar", "rpc.alice.yandex.net": "gproxy",
                   "egress.yandex.net": "egress"}
_CATEGORY_RE = re.compile(r"^[a-z0-9_.-]+$")


def category_of(item):
    return item.get("category") or HOST_CATEGORIES.get(item["host"].lower(), "other")


def system_entries(quasar_routes, update_endpoints):
    """Маршруты, которые обслуживают другие вкладки: видны в каталоге, не правятся
    и не выключаются здесь (owner — вкладка-владелец)."""
    entries = [{"id": "quasar" + path.replace("/", "."), "category": "quasar", "owner": "quasar-http",
                "host": "quasar.yandex.net", "method": method, "path": path, "enabled": True,
                "title": method + " " + path, "readonly": True,
                "notes": "Встроенная логика Квазара: настраивается во вкладке «Квазар»."}
               for method, path in sorted(quasar_routes, key=lambda r: r[1])]
    entries += [{"id": "updates." + e["id"], "category": "updates", "owner": "updates",
                 "host": e["host"], "method": e["method"], "path": e["path"],
                 "enabled": e.get("enabled", True), "title": e["method"] + " " + e["path"],
                 "readonly": True,
                 "notes": "OTA (" + e.get("kind", "") + "): настраивается во вкладке «Обновления»."}
                for e in update_endpoints]
    return entries


# Сигнал dispatch → прокси: маршрут «прозрачен», обмен уходит upstream без записи.
# Несёт уже прочитанное dispatch тело запроса (read-once). После удаления
# http_routes (2026-10-25) производится только catalog/endpoints-маршрутами,
# класс сохранён как протокол для встроенного прокси.
class TransparentRoute:
    __slots__ = ("body",)

    def __init__(self, body=b""):
        self.body = body


# ── Встроенные ответы без эндпоинтов (TASK-070) ─────────────────────────────
# Телеметрия и проверки связи, на которые у эмулятора нет данных: короткий ответ
# «успех» (образцов ответов в журнале нет — см. docs/contracts/http.md), чтобы
# колонка не видела ошибок и промахи не засорялись. Пользовательский эндпоинт
# перекрывает встроенный (dispatch ищет каталог раньше).
BUILTIN_SILENCERS = (
    {"host": "clck.yandex.net", "methods": ("POST",), "prefix": "/", "status": 200, "body": b"",
     "note": "метрики колонки — принять и выбросить"},
    {"host": "report.appmetrica.yandex.net", "methods": ("POST",), "prefix": "/", "status": 200, "body": b"",
     "note": "AppMetrica — принять и выбросить"},
    {"host": "rosenberg.appmetrica.yandex.net", "methods": ("POST",), "prefix": "/", "status": 200, "body": b"",
     "note": "геометрика — принять и выбросить"},
    {"host": "scbh.yandex.net", "methods": ("GET", "HEAD"), "path": "/generate_204", "status": 204, "body": b"",
     "note": "проверка доступности интернета"},
    {"host": "soft.export.yandex.ru", "methods": ("GET", "HEAD"), "path": "/status.xml", "status": 200,
     "body": b"<status>ok</status>", "content_type": "text/xml", "note": "проверка доступности"},
    {"host": "rpc.alice.yandex.net", "methods": ("POST",), "path": "/gproxy/get_auth_suggests", "status": 200,
     "body": b"{}", "note": "подсказки входа — пустой список"},
    {"host": "rpc.alice.yandex.net", "methods": ("POST",), "path": "/gproxy/get_teasers", "status": 200,
     "body": b"{}", "note": "тизеры экрана — пустой список"},
    {"host": "keyboard.yandex.net", "methods": ("GET",), "path": "/experiments", "status": 200,
     "body": b"{}", "note": "эксперименты клавиатуры — пустой список"},
    {"host": "yandex.ru", "methods": ("GET",), "path": "/ads/v1/startup", "status": 200,
     "body": b"{}", "note": "реклама при старте — пустой ответ"},
)


def find_builtin(host, method, path):
    """Встроенный ответ для запроса без эндпоинта: {status, body, headers, note} или None."""
    host = (host or "").lower()
    path = path or "/"
    for item in BUILTIN_SILENCERS:
        if item["host"] != host or method not in item["methods"]:
            continue
        if ("path" in item and item["path"] != path) \
                or ("prefix" in item and not path.startswith(item["prefix"])):
            continue
        body = item.get("body", b"")
        return {"status": item["status"], "body": body,
                "headers": {"Content-Type": item.get("content_type", "application/json" if body == b"{}" else "text/plain")},
                "note": item["note"]}
    return None


def builtin_covers(host, method, path):
    """Обслуживается ли запрос встроенным ответом (включая обложки аватаров, TASK-070):
    такие запросы не попадают в «HTTP без эндпоинта»."""
    if find_builtin(host, method, path) is not None:
        return True
    return (host or "").lower() == "avatars.yandex.net" and method in ("GET", "HEAD") \
        and (path or "").startswith(("/get-music-content/", "/get-video-content/"))


def builtin_entries():
    """Строки каталога для UI (источник «встроенный», только чтение): перекрывается
    своим эндпоинтом — защиты URL, как у Квазара/Обновлений, у них нет."""
    entries = []
    for index, item in enumerate(BUILTIN_SILENCERS):
        entries.append({"id": f"builtin.{index}", "host": item["host"], "method": item["methods"][0],
                        "path": item.get("path") or item.get("prefix") or "/",
                        "methods_extra": item["methods"][1:],
                        "category": "builtin", "enabled": True, "readonly": True, "owner": "endpoints",
                        "title": item["note"],
                        "notes": "Встроенный ответ (TASK-070): перекрывается своим эндпоинтом с тем же URL."})
    entries.append({"id": "builtin.avatars", "host": "avatars.yandex.net", "method": "GET",
                    "path": "/get-music-content/…", "category": "builtin", "enabled": True,
                    "readonly": True, "owner": "endpoints",
                    "title": "обложки музыки — пересылка с кешем на диске",
                    "notes": "Пересылается настоящему серверу (TASK-070), ответ кешируется в state/cache/avatars; "
                             "без сети — кеш, затем 404."})
    return entries


def render_template(template, context):
    """Рекурсивный рендер шаблона: строки заменяют {{токены}}, остальное как есть."""
    if isinstance(template, str):
        def substitute(match):
            return str(context.get(match.group(1), match.group(0)))
        return _TOKEN_RE.sub(substitute, template)
    if isinstance(template, list):
        return [render_template(item, context) for item in template]
    if isinstance(template, dict):
        return {key: render_template(value, context) for key, value in template.items()}
    return template


def request_context(request_values):
    """Контекст плейсхолдеров из параметров запроса (без обращений к сети)."""
    now = time.time()
    context = {"now": int(now), "now_ms": int(now * 1000),
               "now_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "uuid": uuid.uuid4().hex}
    context.update({k: v for k, v in (request_values or {}).items() if v})
    return context


class EndpointCatalog:
    def __init__(self, directory, extension_dirs=None):
        self.directory = Path(directory)
        self.extension_dirs = extension_dirs  # callable → [(ext_id, Path)], TASK-030
        self.endpoints = []
        self._load()

    def _extensions(self):
        """Слои endpoints/ включённых расширений: [(origin, Path)] (callable из runtime —
        extensions/layers.source_layers). Сбой — слой пуст, каталог работает."""
        if self.extension_dirs is None:
            return []
        try:
            return [(origin, Path(path)) for origin, path in self.extension_dirs()]
        except Exception:  # noqa: BLE001 — расширения не должны ломать каталог
            return []

    def _load(self):
        self.endpoints = []
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.directory.glob("*.json")) + sorted(self.directory.glob("*/*.json")):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue  # битый файл пропускаем, остальные работают
                valid, error = self.validate(item)
                if not valid:
                    continue
                if path.parent == self.directory:  # старый плоский формат → раздел
                    item.setdefault("category", category_of(item))
                    item.pop("_group", None)
                    self._save(item)
                    path.unlink(missing_ok=True)
                else:
                    item["category"] = path.parent.name
                self.endpoints.append(item)
        except OSError:
            pass  # каталог недоступен — каталог пуст, эмуляция работает
        # Слой расширений (TASK-030): только чтение. Пользовательская запись с тем
        # же id уже в списке — запись расширения перекрыта, не дублируется.
        for origin, path in self._extensions():
            try:
                files = sorted(path.glob("*.json"))
            except OSError:
                continue
            for file in files:
                try:
                    item = json.loads(file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue  # битый файл пропускаем
                valid, _error = self.validate(item)
                if not valid:
                    continue
                if any(e["id"] == item["id"] for e in self.endpoints):
                    continue
                self.endpoints.append({**item, "_origin": origin, "_file": str(file),
                                       "enabled": item.get("enabled", True)})

    def reload(self):
        """Перечитать файлы: каталог — источник правды на диске (утилита восстановления
        пишет файлы в живой сервер; переключатели тоже персистятся файлами;
        включение/выключение расширений пересобирает слой расширений)."""
        self._load()

    @staticmethod
    def validate(item):
        """Валидация определения; возвращает (ok, ошибка|None)."""
        if not isinstance(item, dict):
            return False, "эндпоинт: объект JSON"
        if not isinstance(item.get("id"), str) or not item["id"]:
            return False, "id: непустая строка"
        for key in ("host", "method", "path"):
            if not isinstance(item.get(key), str) or not item[key]:
                return False, key + ": непустая строка"
        if item["method"] not in METHODS:
            return False, "method: " + ", ".join(METHODS)
        if not item["path"].startswith("/"):
            return False, "path: должен начинаться с /"
        if not isinstance(item.get("status"), int) or not 100 <= item["status"] <= 599:
            return False, "status: код 100–599"
        if not isinstance(item.get("body_base64"), str):
            return False, "body_base64: строка base64 (пустая = ответ без тела)"
        if item.get("category") is not None and (not isinstance(item["category"], str)
                                                  or not _CATEGORY_RE.match(item["category"])):
            return False, "category: латиница, цифры, _ . -"
        if item.get("template") is not None \
                and not isinstance(item["template"], (dict, list)):
            return False, "template: объект или массив JSON (динамическое тело)"
        if item.get("mode", "static") not in MODES:
            return False, "mode: " + ", ".join(MODES)
        return True, None

    def serve(self, item, context):
        """Тело ответа: template (динамика) приоритетнее захваченного body_base64."""
        if item.get("template") is not None:
            body = json.dumps(render_template(item["template"], context),
                              ensure_ascii=False).encode()
        else:
            import base64
            body = base64.b64decode(item.get("body_base64") or "")
        return int(item["status"]), body, item.get("content_type") or "application/json"

    def _file(self, item):
        return self.directory / category_of(item) / (item["id"] + ".json")

    def script_path(self, item):
        if item.get("_file"):  # запись расширения: обработчик рядом с её JSON (только чтение)
            return Path(item["_file"]).with_suffix(".py")
        return self._file(item).with_suffix(".py")

    def read_script(self, identifier):
        item = next((e for e in self.endpoints if e["id"] == identifier), None)
        if item is None:
            raise ValueError("Эндпоинт не найден")
        path = self.script_path(item)
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def write_script(self, item, code):
        """Код обработчика: синтаксис проверяется до записи, битый файл не сохраняется.
        Обработчик записи расширения не перезаписывается — правка живёт в пользовательской
        копии (upsert переносит код туда сам)."""
        if item.get("_file"):
            raise ValueError("Обработчик расширения только для чтения — "
                             "правка создаёт пользовательскую копию")
        compile(code, item["id"] + ".py", "exec")  # SyntaxError → ValueError у вызывающего
        target = self.script_path(item)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".py.tmp")
        temporary.write_text(code, encoding="utf-8")
        os.replace(temporary, target)

    def _save(self, item):
        target = self._file(item)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(item, ensure_ascii=False, indent=2)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)

    def find(self, host, method, path):
        """Точное совпадение host+method+path (query в path не входит)."""
        host = (host or "").lower()
        return next((e for e in self.endpoints
                     if e["host"].lower() == host and e["method"] == method
                     and e["path"] == path), None)

    def upsert(self, item, system=(), script=None):
        """system — маршруты других вкладок: их URL JSON-эндпоинтом не перекрыть
        (dispatch всё равно отдаст их раньше). Правка записи расширения (тот же id)
        создаёт пользовательскую копию: код обработчика расширения переезжает в
        пользовательский файл, файлы расширения не меняются."""
        valid, error = self.validate(item)
        if valid:
            owner = next((s for s in system if s["host"].lower() == item["host"].lower()
                          and s["method"] == item["method"] and s["path"] == item["path"]), None)
            if owner:
                raise ValueError("Этот URL обслуживает вкладка «"
                                 + {"quasar-http": "Квазар", "updates": "Обновления"}.get(owner["owner"], owner["owner"])
                                 + "» — JSON-эндпоинт не нужен")
        if not valid:
            raise ValueError("Эндпоинт: " + error)
        if item.get("readonly"):
            raise ValueError("Эндпоинт обслуживается другой вкладкой — правка здесь недоступна")
        clash = next((e for e in self.endpoints if e["id"] != item["id"]
                      and e["host"].lower() == item["host"].lower()
                      and e["method"] == item["method"] and e["path"] == item["path"]), None)
        if clash:
            raise ValueError("Такой URL уже у эндпоинта " + clash["id"])
        item.setdefault("enabled", True)
        item["category"] = category_of(item)
        item.pop("_group", None)
        item.setdefault("title", item["method"] + " " + item["path"])
        previous = next((e for e in self.endpoints if e["id"] == item["id"]), None)
        from_extension = previous is not None and bool(previous.get("_origin"))
        if script is not None:
            try:
                self.write_script(item, script)
            except SyntaxError as exc:
                raise ValueError(f"Скрипт: строка {exc.lineno}: {exc.msg}") from None
        elif from_extension and self.script_path(previous).is_file():
            # копия при правке: код обработчика живёт в пользовательском файле
            self.write_script(item, self.script_path(previous).read_text(encoding="utf-8"))
        elif previous is not None and category_of(previous) != item["category"] \
                and self.script_path(previous).is_file():
            self.write_script(item, self.script_path(previous).read_text(encoding="utf-8"))
        if item.get("mode") == "script" and not self.script_path(item).is_file():
            raise ValueError("Режим «скрипт»: нет кода обработчика")
        self.endpoints = [e for e in self.endpoints if e["id"] != item["id"]] + [item]
        self._save(item)
        if previous is not None and not from_extension and category_of(previous) != item["category"]:
            self._file(previous).unlink(missing_ok=True)  # переезд в другой раздел
            self.script_path(previous).unlink(missing_ok=True)
        return item

    def toggle(self, identifier, enabled):
        item = next((e for e in self.endpoints if e["id"] == identifier), None)
        if item is None:
            raise ValueError("Эндпоинт не найден")
        if item.get("_origin"):  # запись расширения только для чтения: правка — пользовательская копия
            item = self.upsert({k: v for k, v in item.items() if not str(k).startswith("_")})
        item["enabled"] = bool(enabled)
        self._save(item)
        return item

    def remove(self, identifier):
        item = next((e for e in self.endpoints if e["id"] == identifier), None)
        if item is None:
            return None
        if item.get("_origin"):
            raise ValueError("Эндпоинт расширения только для чтения — "
                             "правка создаёт пользовательскую копию")
        self.endpoints.remove(item)
        for path in (self._file(item), self.script_path(item)):
            path.unlink(missing_ok=True)
        return item

    # ── Зафиксированные промахи: запросы, ответа на которые нет (404) ──────
    MISS_LIMIT = 200
    MISS_SAMPLE = 4096

    @property
    def _misses_file(self):
        return self.directory.parent / "endpoints_misses.json"

    def misses(self):
        if not hasattr(self, "_misses"):
            try:
                self._misses = json.loads(self._misses_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._misses = {}
        return self._misses

    def _save_misses(self):
        try:
            temporary = self._misses_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._misses, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporary, self._misses_file)
        except OSError:
            pass

    # Заголовки, которые не переносятся в повтор запроса (hop-by-hop и транспорт).
    SKIP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "keep-alive",
                    "upgrade", "accept-encoding"}

    def note_miss(self, host, method, path, query="", body=b"", content_type="", headers=(), forwarded=None):
        """Запомнить запрос без обработчика: из него UI создаёт эндпоинт (+).
        forwarded (TASK-044, гибрид): 'upstream' — промах ушёл в Яндекс, 'error: …' —
        попытка не удалась; отметка показывается в «Без ответа»."""
        import base64
        misses = self.misses()
        key = method + " " + host.lower() + path
        now = time.time()
        item = misses.get(key) or {"host": host.lower(), "method": method, "path": path,
                                   "count": 0, "first_ts": now}
        item.update(count=item["count"] + 1, last_ts=now, query=query or "",
                    request_base64=base64.b64encode((body or b"")[:self.MISS_SAMPLE]).decode(),
                    request_size=len(body or b""), content_type=content_type or "",
                    headers=[[k, v] for k, v in headers if k.lower() not in self.SKIP_HEADERS])
        if forwarded:
            item["forwarded"] = forwarded
        misses[key] = item
        if len(misses) > self.MISS_LIMIT:  # вытесняем самые старые
            for old in sorted(misses, key=lambda k: misses[k]["last_ts"])[:len(misses) - self.MISS_LIMIT]:
                del misses[old]
        self._save_misses()

    def forget_miss(self, key=None):
        """Убрать промах из списка (key=None — очистить весь)."""
        misses = self.misses()
        if key is None:
            misses.clear()
        else:
            misses.pop(key, None)
        self._save_misses()

    def public(self, system=()):
        """Каталог для UI: свои записи + расширения (origin — откуда запись; запись
        расширения можно править — сохранится пользовательской копией с тем же id)
        + системные (readonly, owner — вкладка)."""
        def view(item):
            entry = {k: v for k, v in item.items() if not str(k).startswith("_")}
            entry["category"] = category_of(item)
            entry["has_script"] = self.script_path(item).is_file()
            entry["origin"] = item.get("_origin") or "user"  # записи расширений несут _origin
            return entry

        own = [view(e) for e in self.endpoints if not e.get("_origin")]
        extension = [view(e) for e in self.endpoints if e.get("_origin")]
        # Маршрут показываем у того, кто его реально обслуживает (порядок dispatch:
        # обновления → builtin Квазара): дубль /check_updates у Квазара скрыт.
        rank = {"updates": 0, "quasar-http": 1, "endpoints": 2}
        seen, shown = set(), []
        for item in sorted(system, key=lambda e: rank.get(e.get("owner"), 2)):
            key = (item["host"].lower(), item["method"], item["path"])
            if key not in seen:
                seen.add(key)
                shown.append(item)
        builtin = [e for e in builtin_entries()
                   if not any(e["host"].lower() == x["host"].lower() and e["method"] == x["method"]
                              and e["path"] == x["path"] for x in own + extension)]
        items = sorted(own + extension + shown + builtin, key=lambda e: (e["category"], e["host"], e["path"]))
        described = {(e["host"].lower(), e["method"], e["path"]) for e in items}
        misses = [dict(m, key=k) for k, m in self.misses().items()
                  if (m["host"], m["method"], m["path"]) not in described
                  and not builtin_covers(m["host"], m["method"], m["path"])]
        return {"endpoints": items,
                "misses": sorted(misses, key=lambda m: -m["last_ts"]),
                "categories": sorted({e["category"] for e in items}),
                "total": len(self.endpoints),
                "enabled": sum(1 for e in self.endpoints if e.get("enabled"))}
