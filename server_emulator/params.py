"""Параметры: каталог определений (param_defs) + значения владельцев
(param_values). docs/contracts/params-db.md.

Параметр = определение (ключ/тип/роль/подпись) + значение у владельца.
Каркас данных (scope → разделы category):
  пользовательские: user (profile — о пользователе, service — служебное),
                    device (info — о колонке, service — служебное);
  системные:        platform (info, service — по платформе: 'platform:chiron'),
                    server (common — общее, service — служебное).
Владелец: 'user:<id>' | 'device:<sn>' | 'platform:<name>' | 'server'.
Разрешение — цепочка device → user → platform → server → default_value по
ключу без префикса области (location.latitude колонки перекрывает гео дома).
Роль: user_editable=1 — пользователь может править своё значение в кабинете
(/me); остальное — только админ.
Секреты (type=secret) маскируются "__KEEP__" при чтении (как api_key),
запись "__KEEP__" сохраняет прежнее значение.

Расширения (TASK-032): папка params/ включённого расширения добавляет определения
поверх каталога БД (только чтение, в БД не копируются): *.json — определение
{key, scope, type, title, …}; key получает namespace «<scope>.<ext-id>.<имя>» —
префикс области первым (правила validate_def), префикс расширения вторым; поэтому
правка определения расширения через API (def_upsert) создаёт пользовательскую копию
в БД, и пользовательское определение с тем же key побеждает; выключение расширения
убирает определения; некорректное определение — warning без падения раздела. Слои —
extensions/layers; callable ставит runtime (конструктор ниже).
"""
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

PARAM_TYPES = ("string", "number", "bool", "select", "text-list", "secret")
SCOPES = ("user", "device", "platform", "server")
# Разделы внутри области: первый — «информация», второй — служебные нужды.
CATEGORIES = {"user": ("profile", "service"), "device": ("info", "service"),
              "platform": ("info", "service"), "server": ("common", "service")}
LEGACY_CATEGORIES = {"user": "profile", "device": "info", "glagol": "service", "server": "common"}
LEGACY_KEY_CATEGORIES = {"user.wifi_hash": "service"}
# Пути, общие для областей (итог цепочки params.effective): ближний владелец
# перекрывает дальнего. Имена/псевдонимы у пользователя и колонки — разные сущности.
EFFECTIVE_PATHS = ("location.", "timezone", "weather.")
KEEP = "__KEEP__"


class ParamError(ValueError):
    """Неверное определение/значение параметра."""


def _json(value):
    return json.dumps(value, ensure_ascii=False)


class ParamStore:
    def __init__(self, db, extension_dirs=None):
        self.db = db  # соединение StateStore (sqlite3, FK включены)
        self.extension_dirs = extension_dirs  # callable → [(origin, Path)] — слои params/, TASK-032

    # --- слой расширений (TASK-032) ---------------------------------------------
    def _extension_layers(self):
        """Слои params/ включённых расширений: [(origin, Path)] (callable из runtime —
        extensions/layers.source_layers). Слой не задан или сбой — пусто, раздел работает."""
        if self.extension_dirs is None:
            return []
        try:
            return [(origin, Path(path)) for origin, path in self.extension_dirs()]
        except Exception:  # noqa: BLE001 — расширения не должны ломать раздел
            return []

    def extension_defs(self):
        """Определения параметров расширений: {key: определение} — *.json файла
        (validate_def, как у пользовательских); key получает namespace
        «<scope>.<ext-id>.<имя>» (префикс области первым — правила validate_def, тогда
        правка через def_upsert создаёт пользовательскую копию; <ext-id> — вторым);
        некорректное определение — warning, раздел работает. Поздний слой перекрывает
        ранний; в БД определения не копируются."""
        from .extensions.layers import layer_records

        def warn(message):
            log.warning("Расширения (параметры): %s", message)

        result = {}
        for origin, path, value in layer_records(self._extension_layers(), warn=warn):
            ext_id = origin.split(":", 1)[1] if str(origin).startswith("extension:") else ""
            if not isinstance(value, dict):
                warn(f"{path}: нужен JSON-объект с определением параметра")
                continue
            value = dict(value)
            scope = value.get("scope")
            prefix = f"{scope}." if scope in SCOPES else ""
            key = str(value.get("key") or path.stem)
            tail = key[len(prefix):] if prefix and key.startswith(prefix) else key
            if ext_id and not tail.startswith(ext_id + "."):
                tail = f"{ext_id}.{tail}"
            value["key"] = prefix + tail
            try:
                item = self.validate_def(value)
            except ParamError as exc:
                warn(f"{path}: {exc}")
                continue
            item["origin"] = origin
            result[item["key"]] = item
        return result

    # --- определения ---------------------------------------------------------
    def defs(self, editable_only=False, scopes=None):
        rows = self.db.execute("SELECT * FROM param_defs").fetchall()
        items = []
        for row in rows:
            item = self._def_row(row)
            if editable_only and not item["user_editable"]:
                continue
            if scopes and item["scope"] not in scopes:
                continue
            items.append(item)
        extension = self.extension_defs()  # записи расширений поверх БД (TASK-032)
        known = {item["key"] for item in items}
        items += [item for key, item in extension.items() if key not in known
                  and (not editable_only or item["user_editable"])
                  and (not scopes or item["scope"] in scopes)]
        return sorted(items, key=lambda x: x["key"])

    def def_get(self, key):
        row = self.db.execute("SELECT * FROM param_defs WHERE key=?", (key,)).fetchone()
        if row:
            return self._def_row(row)  # пользовательское определение БД побеждает
        return self.extension_defs().get(key)

    def validate_def(self, item):
        """Проверка определения → нормализованный dict (без записи в БД); определения
        расширений проходят её же (их namespace «<scope>.<ext-id>.<имя>» соблюдает
        префикс области)."""
        key = item.get("key") if isinstance(item, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_.-]+", key):
            raise ParamError("key: строка [a-z0-9_.-], например user.wifi_hash")
        scope, ptype = item.get("scope"), item.get("type")
        if scope not in SCOPES:
            raise ParamError("scope: " + ", ".join(SCOPES))
        if ptype not in PARAM_TYPES:
            raise ParamError("type: " + ", ".join(PARAM_TYPES))
        if scope == "user" and not key.startswith("user."):
            raise ParamError("ключ scope=user должен начинаться с user.")
        if scope == "server" and not key.startswith("server."):
            raise ParamError("ключ scope=server должен начинаться с server.")
        if scope == "device" and not key.startswith("device."):
            raise ParamError("ключ scope=device должен начинаться с device.")
        if scope == "platform" and not key.startswith("platform."):
            raise ParamError("ключ scope=platform должен начинаться с platform.")
        category = item.get("category") or CATEGORIES[scope][0]
        if category not in CATEGORIES[scope]:
            raise ParamError(f"category для {scope}: " + ", ".join(CATEGORIES[scope]))
        options = item.get("options")
        if ptype == "select":
            if not isinstance(options, list) or not options or not all(str(o) for o in options):
                raise ParamError("select: options — непустой список вариантов")
            options = [str(o) for o in options]
        elif ptype == "number" and options is not None:
            if not isinstance(options, dict) or not {"min", "max"} & set(options):
                raise ParamError("number: options = {min?, max?}")
        else:
            options = None
        default = item.get("default_value")
        if default is not None:
            default = self.validate_value(key, ptype, default, options, mask_allowed=False)
        return {"key": key, "scope": scope, "category": category,
                "user_editable": bool(item.get("user_editable")), "type": ptype,
                "title": str(item.get("title") or key), "hint": str(item.get("hint") or ""),
                "options": options, "default_value": default}

    def def_upsert(self, item):
        normalized = self.validate_def(item)
        key, scope, category = normalized["key"], normalized["scope"], normalized["category"]
        options, default = normalized["options"], normalized["default_value"]
        with self.db:
            self.db.execute(
                "INSERT INTO param_defs(key,scope,category,user_editable,type,title,hint,options,default_value) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET scope=excluded.scope,"
                "category=excluded.category,user_editable=excluded.user_editable,type=excluded.type,"
                "title=excluded.title,hint=excluded.hint,options=excluded.options,"
                "default_value=excluded.default_value",
                (key, scope, category, int(normalized["user_editable"]),
                 normalized["type"], normalized["title"], normalized["hint"],
                 _json(options) if options is not None else None,
                 _json(default) if default is not None else None))
        return self.def_get(key)

    def def_remove(self, key):
        with self.db:
            self.db.execute("DELETE FROM param_values WHERE key=?", (key,))
            self.db.execute("DELETE FROM param_defs WHERE key=?", (key,))
        return True

    @staticmethod
    def _def_row(row):
        keys = ("key", "scope", "category", "user_editable", "type", "title", "hint", "options", "default_value")
        item = {k: row[i] for i, k in enumerate(keys)}
        item["user_editable"] = bool(item["user_editable"])
        item["options"] = json.loads(item["options"]) if item["options"] else None
        item["default_value"] = json.loads(item["default_value"]) if item["default_value"] else None
        if item["type"] == "secret" and item["default_value"]:
            item["default_value"] = ""  # дефолт секрета не показываем
        return item

    # --- значения --------------------------------------------------------------
    def validate_value(self, key, ptype, value, options, mask_allowed=True):
        if ptype == "secret":
            if not isinstance(value, str):
                raise ParamError(f"{key}: секрет — строка")
            if value == KEEP and mask_allowed:
                return KEEP
            return value
        if ptype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ParamError(f"{key}: число")
            if isinstance(options, dict):
                lo, hi = options.get("min"), options.get("max")
                if lo is not None and value < lo or hi is not None and value > hi:
                    raise ParamError(f"{key}: вне [{lo}, {hi}]")
            return value
        if ptype == "bool":
            if not isinstance(value, bool):
                raise ParamError(f"{key}: булево")
            return value
        if ptype == "select":
            if value not in (options or []):
                raise ParamError(f"{key}: допустимо {options}")
            return value
        if ptype == "text-list":
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ParamError(f"{key}: список строк")
            return value
        if not isinstance(value, str):
            raise ParamError(f"{key}: строка")
        return value

    def set_value(self, owner, key, value):
        definition = self.def_get(key)
        if definition is None:
            raise ParamError(f"параметр {key!r} не определён (каталог → раздел «Параметры»)")
        if not isinstance(owner, str) or ":" not in owner and owner != "server":
            raise ParamError("owner: 'user:<id>' | 'device:<sn>' | 'platform:<name>' | 'server'")
        value = self.validate_value(key, definition["type"], value, definition["options"])
        if definition["type"] == "secret" and value == KEEP:
            return self.values(owner).get(key)  # маска: сохранить прежнее
        with self.db:
            self.db.execute("INSERT INTO param_values(owner,key,value,updated_ts) VALUES(?,?,?,?) "
                            "ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value,"
                            "updated_ts=excluded.updated_ts", (owner, key, _json(value), time.time()))
        return value

    def values(self, owner):
        rows = self.db.execute("SELECT key,value,updated_ts FROM param_values WHERE owner=?",
                               (owner,)).fetchall()
        return {r[0]: {"value": self._mask(r[0], json.loads(r[1])), "updated_ts": r[2]} for r in rows}

    def raw_value(self, owner, key):
        row = self.db.execute("SELECT value FROM param_values WHERE owner=? AND key=?",
                              (owner, key)).fetchone()
        return json.loads(row[0]) if row else None

    def _mask(self, key, value):
        definition = self.def_get(key)
        if definition and definition["type"] == "secret" and value:
            return KEEP
        return value

    # --- разрешение $param ------------------------------------------------------
    def resolve(self, key, user_id=None, sn=None, platform=None):
        """Цепочка device:<sn> → user:<id> → platform:<p> → server → default_value.
        KeyError — значения нет ни на одном уровне."""
        for owner in [o for o in (f"device:{sn}" if sn else None,
                                  f"user:{user_id}" if user_id else None,
                                  f"platform:{platform}" if platform else None,
                                  "server") if o]:
            value = self.raw_value(owner, key)
            if value is not None:
                return value
        definition = self.def_get(key)
        if definition and definition.get("default_value") is not None:
            return definition["default_value"]
        raise KeyError(key)

    def resolve_view(self, sn=None, user_id=None, platform=None):
        """Итог цепочки по всем определениям с уровнем-источником (TASK-060, UI «Итог/Откуда»):
        {key: {"value": итог, "source": "device|user|platform|server|default"}}; секреты маской.
        Первый непустой уровень цепочки device → user → platform → server перекрывает младшие,
        значения нет ни на одном — default_value определения."""
        chain = [("device", f"device:{sn}" if sn else None),
                 ("user", f"user:{user_id}" if user_id else None),
                 ("platform", f"platform:{platform}" if platform else None),
                 ("server", "server")]
        result = {}
        for definition in self.defs():
            key, source, value = definition["key"], "default", definition.get("default_value")
            for scope, owner in chain:
                if owner is None:
                    continue
                raw = self.raw_value(owner, key)
                if raw is not None:  # raw_value уже разбирает JSON
                    value, source = raw, scope
                    break
            result[key] = {"value": self._mask(key, value), "source": source}
        return result

    # --- контекст сценариев -------------------------------------------------------
    def context(self, user_id=None, sn=None, platform=None):
        """Значения для сценариев: params.<scope>.<путь> и params.effective.<путь>
        (итог цепочки device → user → platform → server → default по общим путям
        EFFECTIVE_PATHS: location.*, timezone; путь = ключ без префикса области).
        Секреты в контекст не попадают."""
        defs = {d["key"]: d for d in self.defs() if d["type"] != "secret"}
        shared = {key.split(".", 1)[1] for key in defs
                  if key.split(".", 1)[1].startswith(EFFECTIVE_PATHS)}
        owners = [("device", f"device:{sn}" if sn else None), ("user", f"user:{user_id}" if user_id else None),
                  ("platform", f"platform:{platform}" if platform else None), ("server", "server")]
        result = {scope: {} for scope in SCOPES}
        effective = {}
        for definition in defs.values():
            path = definition["key"].split(".", 1)[1]
            if definition.get("default_value") is not None and path in shared:
                _put(effective, path, definition["default_value"])
        for scope, owner in reversed(owners):  # сервер → … → колонка: ближний перекрывает
            if owner is None:
                continue
            for key, value in self.db.execute("SELECT key, value FROM param_values WHERE owner=?", (owner,)):
                if key not in defs:
                    continue
                value = json.loads(value)
                path = key.split(".", 1)[1]
                _put(result[scope], path, value)
                if path in shared:
                    _put(effective, path, value)
        result["effective"] = effective
        return result

    # --- сид --------------------------------------------------------------------
    SEED_DEFS = (
        # пользователь: о пользователе
        {"key": "user.name", "scope": "user", "category": "profile", "user_editable": True,
         "type": "string", "title": "Как зовут", "hint": "Имя для обращения в ответах сценариев"},
        {"key": "user.location.latitude", "scope": "user", "category": "profile", "user_editable": True,
         "type": "number", "title": "Широта дома", "hint": "Координаты дома (гео сценариев/погоды)"},
        {"key": "user.location.longitude", "scope": "user", "category": "profile", "user_editable": True,
         "type": "number", "title": "Долгота дома", "hint": "Координаты дома (гео сценариев/погоды)"},
        {"key": "user.location.city", "scope": "user", "category": "profile", "user_editable": True,
         "type": "string", "title": "Город", "hint": "Именительный падеж: «Москва»"},
        {"key": "user.location.city_in", "scope": "user", "category": "profile", "user_editable": True,
         "type": "string", "title": "Город (где)", "hint": "Предложный падеж для фраз: «Москве»"},
        {"key": "user.timezone", "scope": "user", "category": "profile", "user_editable": True,
         "type": "string", "title": "Часовой пояс", "hint": "IANA, например Europe/Moscow"},
        # пользователь: служебное
        {"key": "user.wifi_hash", "scope": "user", "category": "service", "user_editable": True,
         "type": "string", "title": "Wi-Fi hash", "hint": "Хеш домашней сети — общий для устройств пользователя"},
        # колонка: о колонке
        {"key": "device.name", "scope": "device", "category": "info", "user_editable": True,
         "type": "string", "title": "Имя колонки", "hint": "Отображаемое имя колонки"},
        {"key": "device.aliases", "scope": "device", "category": "info", "user_editable": True,
         "type": "text-list", "title": "Как ещё называть колонку",
         "hint": "Имена для команд между колонками: «дуо 32», «на кухне»"},
        {"key": "device.room", "scope": "device", "category": "info", "user_editable": True,
         "type": "string", "title": "Комната", "hint": "Размещение колонки (контекст света)"},
        {"key": "device.location.latitude", "scope": "device", "category": "info", "user_editable": True,
         "type": "number", "title": "Широта колонки", "hint": "Если колонка не дома — перекрывает гео пользователя"},
        {"key": "device.location.longitude", "scope": "device", "category": "info", "user_editable": True,
         "type": "number", "title": "Долгота колонки", "hint": "Если колонка не дома — перекрывает гео пользователя"},
        {"key": "device.location.city", "scope": "device", "category": "info", "user_editable": True,
         "type": "string", "title": "Город колонки", "hint": "Перекрывает город пользователя"},
        {"key": "device.location.city_in", "scope": "device", "category": "info", "user_editable": True,
         "type": "string", "title": "Город колонки (где)", "hint": "Предложный падеж"},
        {"key": "device.timezone", "scope": "device", "category": "info", "user_editable": True,
         "type": "string", "title": "Часовой пояс колонки", "hint": "IANA; перекрывает пояс пользователя"},
        # платформа: о платформе
        {"key": "platform.title", "scope": "platform", "category": "info", "user_editable": False,
         "type": "string", "title": "Название платформы", "hint": "chiron → «Станция Дуо Макс»"},
        {"key": "platform.display", "scope": "platform", "category": "info", "user_editable": False,
         "type": "select", "options": ["none", "seven_segment", "emotions", "screen"],
         "title": "Экран", "hint": "Что умеет показывать платформа"},
        # сервер: общее
        {"key": "server.timezone", "scope": "server", "category": "common", "user_editable": False,
         "type": "string", "title": "Часовой пояс по умолчанию", "hint": "IANA", "default_value": "Europe/Moscow"},
        {"key": "server.weather.url", "scope": "server", "category": "common", "user_editable": False,
         "type": "string", "title": "Провайдер погоды (URL)", "hint": "Open-Meteo-совместимый",
         "default_value": "https://api.open-meteo.com/v1/forecast"},
        {"key": "server.weather.max_age_hours", "scope": "server", "category": "common", "user_editable": False,
         "type": "select", "options": ["1", "2", "3", "6"], "title": "Погода: свежесть данных, ч",
         "hint": "Запись погоды в БД младше — озвучивается сразу; старше — пинок плагина", "default_value": "2"},
        {"key": "server.weather.refresh_timeout", "scope": "server", "category": "common", "user_editable": False,
         "type": "number", "title": "Погода: ожидание плагина, с",
         "hint": "Не дождались — озвучиваем старые данные с пометкой, сколько им времени", "default_value": 8},
    )

    def seed(self, identities_data):
        """Каталог + перенос значений из legacy-полей реестра (location/fields).
        Идемпотентно: определения не перезаписывают изменённое, значения не трогает."""
        existing = {d["key"]: d for d in self.defs()}
        for definition in self.SEED_DEFS:
            if definition["key"] not in existing:
                self.def_upsert(dict(definition))
        for item in existing.values():  # миграция разделов прежнего каталога
            if item["category"] not in CATEGORIES.get(item["scope"], ()):
                category = LEGACY_KEY_CATEGORIES.get(item["key"]) or LEGACY_CATEGORIES.get(
                    item["category"], CATEGORIES[item["scope"]][-1])
                self.def_upsert({**item, "category": category})
        for user in identities_data.get("users", []):
            owner = f"user:{user['id']}"
            known = {k for k in self.values(owner)}
            location = user.get("location") or {}
            if "user.location.latitude" not in known and "latitude" in location:
                self.set_value(owner, "user.location.latitude", location["latitude"])
            if "user.location.longitude" not in known and "longitude" in location:
                self.set_value(owner, "user.location.longitude", location["longitude"])
            if "user.name" not in known and user.get("name"):
                self.set_value(owner, "user.name", user["name"])
            for field, value in (user.get("fields") or {}).items():
                key = f"user.{field}"
                if key not in known and self.def_get(key):
                    self.set_value(owner, key, value)
        for device in identities_data.get("devices", []):
            owner = f"device:{device['sn']}"
            known = {k for k in self.values(owner)}
            if "device.name" not in known:
                self.set_value(owner, "device.name", device.get("name", device["sn"]))
            if device.get("room") and "device.room" not in known:
                self.set_value(owner, "device.room", device["room"])


def _put(tree, path, value):
    """Точечный путь → вложенные словари (location.latitude → {location: {latitude}})."""
    node = tree
    parts = path.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[parts[-1]] = value
