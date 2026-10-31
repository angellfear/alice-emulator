"""Конфиги Quasar: слои common → платформа (merge-patch) → аккаунт → устройство + инлайн-узлы.

Файлы — только содержание:
- state/quasar/common.json — ТЕЛО общего конфига (чистый JSON ответа sync);
- state/quasar/platforms/<platform>.json — чистый merge-patch (RFC 7396);
  платформа = имя файла, по умолчанию — первая по алфавиту.

Слой расширений (TASK-030): common.json и platforms/*.json из quasar/ включённых
расширений — только чтение, под пользовательскими файлами (merge-patch в обратном
порядке приоритета); правка в редакторе сохраняет пользовательскую копию.

Сборка ответа /get_sync_info:

    common.json (тело)
      ← merge-patch платформы
      ← переопределения аккаунта владельца (quasar_overrides пользователя, только config.account_config.*)
      ← переопределения устройства (quasar_overrides: path+source)
      → разрешение инлайн-узлов ($-ключи) → динамика ({{токены}} / db / run)

Инлайн-узлы живут В ТЕЛЕ на месте значения — один $-ключ типа со значением,
плюс опциональные $name/$hint (подпись и пояснение для простого редактора):

    "subscription": {
      "$name": "Подписка", "$hint": "Состояние подписки устройства",
      "$select": "Включена",
      "Включена":  {"enabled": true,  "mode": "subscription", "ttl": 0},
      "Выключена (invalid_subscription_user)": {"enabled": false, "mode": "subscription",
                                                "ttl": 0, "disabled_reason": "invalid_subscription_user"}
    }
    "device_label": {"$string": "Кухня", "$name": "Подпись"}
    "volume":       {"$number": 5, "$name": "Громкость"}
    "alice_pro":    {"$bool": true, "$name": "Alice Pro"}
    "tags":         {"$list": ["a", "b"], "$name": "Теги"}
    "owner":        {"$param": "user.name", "$name": "Владелец"}

Разрешение: $select → узел заменяется значением выбранного варианта (ключи
вариантов = подписи выпадающего списка; вложенность допустима); $string/
$number/$list → значение (строки проходят шаблонизатор {{токены}}); $bool →
true/false; $param → параметр-заглушка (device.sn, device.name, user.name,
session.sn, …; единая система параметров — TODO, функция одна: resolve_param).

Переопределение устройства: {path, source}; source — литерал / {"expr"} /
{"db"} / {"run"} / {"delete"} ИЛИ переключение селектора: {"$select": "…"} —
мерджится в существующий узел (варианты не дублируются).
"""
import copy
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from .params import ParamError

SOURCE_KEYS = ("expr", "db", "run", "delete")
SCRIPTS_DIRNAME = "scripts"
INLINE_KEYS = ("$select", "$string", "$number", "$bool", "$list", "$param")
META_KEYS = ("$name", "$hint")
RUN_TIMEOUT_S = 3
_TOKEN_DOTTED = re.compile(r"\{\{\s*([a-z_][a-z0-9_.]*)\s*\}\}")


class QuasarConfigError(ValueError):
    """Ошибка определения/источника: конфиг невалиден, ответ — 409."""


# --- merge-patch (RFC 7396) -----------------------------------------------------

def merge_patch(target, patch):
    """Рекурсивная мерджа RFC 7396: объекты сливаются, null удаляет ключ."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


# --- контекст и источники значений (динамика) -----------------------------------

def context_for(runtime, device):
    """Токены шаблонов: запросное время + реестр (пользователь/устройство)."""
    now = time.time()
    context = {"now": int(now), "now_ms": int(now * 1000),
               "now_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "uuid": uuid.uuid4().hex,
               "device_id": "", "device_name": "", "platform": "", "user_id": ""}
    if device:
        context.update({"device_id": next(iter(device.get("device_ids", [])), device.get("sn", "")),
                        "device_name": device.get("name", ""),
                        "platform": (device.get("profile") or {}).get("platform", "")})
        user = None
        if device.get("user_id"):
            user = next((u for u in runtime.identities.data["users"] if u["id"] == device["user_id"]), None)
        if user:
            context["user_id"] = user["id"]
            context["user.name"] = user.get("name", "")
            for section in ("location", "fields"):
                for key, value in (user.get(section) or {}).items():
                    context[f"user.{section}.{key}"] = value
        context["device.name"] = device.get("name", "")
        if device.get("room"):
            context["device.room"] = device["room"]
        for key, value in (device.get("fields") or {}).items():
            context[f"device.fields.{key}"] = value
    return context


def resolve_param(name, runtime, device, context):
    """Параметры-заглушки ($param): известные пути из реестра/контекста.
    TODO: единая система параметров (сессии, вычислимые, расширяемые) —
    здесь одна точка расширения, дальше только эта функция."""
    if not isinstance(name, str) or not name:
        raise QuasarConfigError("$param: пустое имя")
    sessions = getattr(runtime, "sessions", {}) or {}
    if name == "device.sn":
        return (device or {}).get("sn", "")
    if name in ("device.id", "device.device_id"):
        return context.get("device_id", "")
    if name == "device.name":
        return context.get("device_name", "")
    if name == "device.room":
        return context.get("device.room", "")
    if name == "device.platform":
        return context.get("platform", "")
    if name == "user.id":
        return context.get("user_id", "")
    if name == "user.name":
        return context.get("user.name", "")
    if name == "session.sn":  # SN устройства активной WS-сессии этого запроса
        sn = (device or {}).get("sn")
        session = next((s for s in sessions.values() if s.get("device_sn") == sn), None)
        return (session or {}).get("device_sn", "")
    if name in context:
        return context[name]
    # Стор параметров (каталог+значения, цепочка device → user → server → default)
    store = getattr(runtime, "quasar_params", None)
    if store is not None:
        try:
            user_id = (device or {}).get("user_id")
            return store.resolve(name, user_id=user_id, sn=(device or {}).get("sn"))
        except KeyError:
            pass
        except ParamError:  # секрет-маска и т.п. — ниже попробуем legacy
            pass
    # legacy: пути реестра (user.location.*, user.fields.*, device.fields.*…)
    if name.startswith(("user.", "device.")):
        try:
            return db_lookup(name, runtime, device)
        except QuasarConfigError:
            pass
    raise QuasarConfigError(f"$param: параметр {name!r} не найден (стор параметров и реестр)")


def db_lookup(path, runtime, device):
    """Значение из БД эмулятора: user.<путь> | device.<путь> (реестр)."""
    root, _, tail = (path or "").partition(".")
    source = None
    if root == "user" and device and device.get("user_id"):
        user = next((u for u in runtime.identities.data["users"] if u["id"] == device["user_id"]), None)
        if user:
            source = {"name": user.get("name"), "location": user.get("location") or {},
                      "fields": user.get("fields") or {}}
    elif root == "device" and device:
        source = {"name": device.get("name"), "room": device.get("room"),
                  "fields": device.get("fields") or {}, "sn": device.get("sn")}
    if source is None:
        raise QuasarConfigError(f"db: путь {path!r} недоступен (нет пользователя/устройства)")
    value = source
    for part in [p for p in tail.split(".") if p]:
        if not isinstance(value, dict) or part not in value:
            raise QuasarConfigError(f"db: путь {path!r} не найден")
        value = value[part]
    return copy.deepcopy(value)


def run_command(command, runtime, device, context):
    """Запуск скрипта/exe из белого списка state/quasar/scripts; stdout → значение."""
    try:
        parts = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        raise QuasarConfigError(f"run: команда не разбирается ({exc})")
    if not parts:
        raise QuasarConfigError("run: пустая команда")
    scripts = runtime.quasar_platforms.scripts_dir.resolve()
    exe = Path(parts[0])
    target = exe.resolve() if exe.is_absolute() else (scripts / exe).resolve()
    if scripts != target and scripts not in target.parents:
        raise QuasarConfigError(f"run: {parts[0]!r} вне белого списка {scripts}")
    if not target.exists():
        raise QuasarConfigError(f"run: {target} не существует")
    environment = dict(os.environ)
    environment.update({"QSN": (device or {}).get("sn", ""), "QDEVICE_ID": context.get("device_id", ""),
                        "QUSER_ID": context.get("user_id", ""), "QPATH": context.get("QPATH", ""),
                        "QCONTEXT": json.dumps(context, ensure_ascii=False)})
    try:
        completed = subprocess.run([str(target)] + parts[1:], capture_output=True, text=True,
                                   timeout=RUN_TIMEOUT_S, env=environment, cwd=str(scripts))
    except subprocess.TimeoutExpired:
        raise QuasarConfigError(f"run: таймаут {RUN_TIMEOUT_S} с ({parts[0]})")
    except OSError as exc:
        raise QuasarConfigError(f"run: {exc}")
    if completed.returncode != 0:
        raise QuasarConfigError(f"run: код {completed.returncode}: {completed.stderr.strip()[:200]}")
    output = completed.stdout.strip()
    try:
        return json.loads(output)
    except ValueError:
        return output


def resolve_source(value, runtime, device, context, trace=None, path=""):
    """Разрешение листа: source-маркеры и строки-шаблоны; остальное как есть."""
    if isinstance(value, str):
        if "{{" in value:
            def substitute(match):
                found = context.get(match.group(1))
                return "" if found is None else str(found)
            if trace is not None:
                used = sorted({m.group(1) for m in _TOKEN_DOTTED.finditer(value)})
                trace[path] = "динамика {{" + "}}, {{".join(used) + "}}"
            return _TOKEN_DOTTED.sub(substitute, value)
        return value
    if isinstance(value, dict) and set(value) == {"expr"}:
        return resolve_source(value["expr"], runtime, device, context, trace, path)
    if isinstance(value, dict) and set(value) == {"db"}:
        result = db_lookup(value["db"], runtime, device)
        if trace is not None:
            trace[path] = f"БД эмулятора: {value['db']}"
        return result
    if isinstance(value, dict) and set(value) == {"run"}:
        result = run_command(value["run"], runtime, device, context)
        if trace is not None:
            trace[path] = f"скрипт: {value['run']}"
        return result
    return value


# --- инлайн-узлы -------------------------------------------------------------------

def inline_type(node):
    """Тип инлайн-узла или None: ровно один $-ключ типа.
    $select: рядом допустимы варианты (любые не-$ ключи) и $name/$hint;
    прочие типы: только $name/$hint рядом."""
    if not isinstance(node, dict):
        return None
    keys = set(node)
    types = keys & set(INLINE_KEYS)
    if not types:
        return None
    if len(types) > 1:
        raise QuasarConfigError(f"инлайн-узел: один $-ключ типа, получено: {sorted(types)}")
    kind = next(iter(types))
    if kind != "$select":
        stray = keys - {kind} - set(META_KEYS)
        if stray:
            raise QuasarConfigError(f"инлайн-узел {kind}: рядом допустимы только $name/$hint, "
                                    f"лишние: {sorted(stray)}")
    return kind


def resolve_inline(node, kind, runtime, device, context, trace=None, path=""):
    if kind == "$select":
        chosen = node[kind]
        option = node.get(chosen) if isinstance(chosen, str) else None
        if option is None:
            raise QuasarConfigError(f"$select: вариант {chosen!r} не определён в узле "
                                    f"«{node.get('$name', '?')}»")
        if trace is not None and path not in trace:  # выбор переопределением важнее
            trace[path] = f"шаблон «{node.get('$name') or path}»: вариант «{chosen}»"
        return resolve_tree(option, runtime, device, context, trace, path)
    if kind == "$param":
        result = resolve_param(node[kind], runtime, device, context)
        if trace is not None:
            trace[path] = f"параметр {node[kind]}"
        return result
    if trace is not None and kind not in ("$select", "$param") and path not in trace:
        trace[path] = f"шаблон «{node.get('$name') or path}»"
    return resolve_tree(node[kind], runtime, device, context, trace, path)  # $string/$number/$list/$bool


def resolve_tree(tree, runtime, device, context, trace=None, path=""):
    """Обход: инлайн-узлы и source-маркеры разрешаются, строки рендерятся."""
    if isinstance(tree, dict):
        kind = inline_type(tree)
        if kind is not None:
            return resolve_inline(tree, kind, runtime, device, context, trace, path)
        marker = set(tree)
        if len(marker) == 1 and next(iter(marker)) in SOURCE_KEYS:
            if "delete" in marker:
                raise QuasarConfigError("delete-маркер допустим только в переопределении")
            return resolve_source(tree, runtime, device, context, trace, path)
        return {k: resolve_tree(v, runtime, device, context, trace,
                                f"{path}.{k}" if path else k) for k, v in tree.items()}
    if isinstance(tree, list):
        return [resolve_tree(item, runtime, device, context, trace,
                             f"{path}.{i}" if path else str(i)) for i, item in enumerate(tree)]
    return resolve_source(tree, runtime, device, context, trace, path)


# --- хранилище: чистые файлы без обёрток -------------------------------------------

class PlatformStore:
    """Слои Quasar-конфига (TASK-030): common.json и platforms/ включённых расширений —
    только чтение, поверх них — пользовательские файлы state/quasar (перекрытие в
    обратном порядке: пользовательское > расширение; между расширениями — позднее).
    extension_dirs — callable → [(ext_id, Path)] каталогов quasar/ расширений, ставит
    runtime (менеджер расширений создаётся позже хранилища)."""

    def __init__(self, directory, extension_dirs=None):
        self.directory = Path(directory)
        self.platforms_dir = self.directory / "platforms"
        self.scripts_dir = self.directory / SCRIPTS_DIRNAME
        self.common_path = self.directory / "common.json"
        self.extension_dirs = extension_dirs

    def _extension_layers(self):
        if self.extension_dirs is None:
            return []
        try:
            return [(ext_id, Path(path)) for ext_id, path in self.extension_dirs()]
        except Exception:  # noqa: BLE001 — расширения не должны ломать конфиг
            return []

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def load_common(self):
        """Тело общего конфига: common.json расширений (merge-patch по порядку) →
        пользовательский common.json (merge-patch поверх). Ни одного слоя — None."""
        body = None
        for _ext_id, path in self._extension_layers():
            item = self._read(path / "common.json")
            if isinstance(item, dict):
                body = merge_patch(body if isinstance(body, dict) else {}, item)
        item = self._read(self.common_path)
        if isinstance(item, dict):
            body = merge_patch(body if isinstance(body, dict) else {}, item)
        return body

    def save_common(self, body):
        if not isinstance(body, dict):
            raise QuasarConfigError("Общий конфиг: объект JSON (тело ответа)")
        self._write(self.common_path, body)
        return body

    def ensure_common(self):
        """Скелет общего конфига, если слоёв нет вовсе (вызывается UI/API, не чтением).
        Пока слой есть (например, только расширение) — пользовательский файл не пишется:
        правка в редакторе сохранит пользовательскую копию поверх слоя расширения."""
        body = self.load_common()
        if body is None:
            body = {"config": {}}
            self.save_common(body)
        return body

    def load(self, platform):
        if platform == "common":
            return self.load_common()
        if not platform or not re.fullmatch(r"[a-z0-9_.-]+", platform):
            return None
        patch = None
        for _ext_id, path in self._extension_layers():  # поздний слой поверх раннего
            item = self._read(path / "platforms" / (platform + ".json"))
            if isinstance(item, dict):
                patch = merge_patch(patch if isinstance(patch, dict) else {}, item)
        item = self._read(self.platforms_dir / (platform + ".json"))  # пользовательский — сильнее
        if isinstance(item, dict):
            patch = merge_patch(patch if isinstance(patch, dict) else {}, item)
        return patch

    def list(self):
        """Платформы: пользовательские файлы + платформы расширений (с тем же именем
        платформы показана пользовательская); первая в списке — по умолчанию."""
        result, names = [], set()
        try:
            paths = sorted(self.platforms_dir.glob("*.json"))
        except OSError:
            paths = []
        for index, path in enumerate(paths):
            patch = self._read(path)
            if isinstance(patch, dict):
                result.append({"platform": path.stem,
                               "default": index == 0,
                               "patch_bytes": len(json.dumps(patch))})
                names.add(path.stem)
        for _ext_id, path in self._extension_layers():
            try:
                files = sorted((path / "platforms").glob("*.json"))
            except OSError:
                continue
            for file in files:
                if file.stem in names:
                    continue
                patch = self._read(file)
                if isinstance(patch, dict):
                    result.append({"platform": file.stem, "default": not result,
                                   "patch_bytes": len(json.dumps(patch))})
                    names.add(file.stem)
        return result

    def save(self, platform, patch):
        if not isinstance(platform, str) or not re.fullmatch(r"[a-z0-9_.-]+", platform) \
                or platform == "common":
            raise QuasarConfigError("platform: строка [a-z0-9_.-], common зарезервировано")
        if not isinstance(patch, dict):
            raise QuasarConfigError("Платформа: чистый merge-patch (объект JSON)")
        self._write(self.platforms_dir / (platform + ".json"), patch)  # пользовательская копия
        return patch

    def remove(self, platform):
        if platform == "common":
            raise QuasarConfigError("Общий конфиг не удаляется")
        target = self.platforms_dir / (platform + ".json")
        existed = target.exists()
        try:
            target.unlink()
        except OSError:
            pass
        return existed

    def platform_for(self, device):
        name = ((device or {}).get("profile") or {}).get("platform") if isinstance(device, dict) else None
        if name and self.load(name) is not None:
            return name
        files = self.list()
        return files[0]["platform"] if files else None

    def resolve_files(self, device):
        """(тело common, патч платформы|None); (None, None) — слоёв нет."""
        body = self.load_common()
        if body is None:
            return None, None
        name = self.platform_for(device)
        return body, (self.load(name) if name else None)


# --- переопределения и композиция --------------------------------------------------

class _Delete:
    __slots__ = ()

    def __repr__(self):
        return "<delete>"


_DELETE = _Delete()


def get_path(tree, dotted):
    node = tree
    for part in [p for p in dotted.split(".") if p]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def deep_set(tree, dotted, value):
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        raise QuasarConfigError("override: пустой путь")
    node = tree
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    if value is _DELETE:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value


def _trace_patch(trace, prefix, patch, label):
    """Отметить пути, которые merge-patch платформы задаёт/удаляет."""
    if not isinstance(patch, dict):
        trace[prefix] = label
        return
    for key, value in patch.items():
        path = f"{prefix}.{key}" if prefix else key
        if value is None:
            trace[path] = label + " (удалено)"
        elif isinstance(value, dict):
            _trace_patch(trace, path, value, label)
        else:
            trace[path] = label


def _override_label(source):
    if isinstance(source, dict) and set(source) == {"delete"}:
        return "переопределение: удаление"
    if isinstance(source, dict) and "$select" in source:
        return f"переопределение: выбор «{source['$select']}»"
    if isinstance(source, dict) and set(source) == {"expr"}:
        return f"переопределение: шаблон {source['expr']}"
    if isinstance(source, dict) and set(source) == {"db"}:
        return f"переопределение: БД {source['db']}"
    if isinstance(source, dict) and set(source) == {"run"}:
        return f"переопределение: скрипт {source['run']}"
    return "переопределение устройства"


POLL_PERIOD_PATH = "config.system_config.syncd.pollPeriodSec"
POLL_PERIOD_RANGE = (60, 900)


def poll_period_warnings(body):
    """Предупреждения о pollPeriodSec: клиент (syncd) зажимает период в [60, 900] с.

    Значение в ответе не меняется — предупреждения получает только редактор."""
    value = get_path(body, POLL_PERIOD_PATH)
    if value is None:
        return []
    low, high = POLL_PERIOD_RANGE
    rng = f"диапазон {low}–{high} с"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [f"pollPeriodSec = {json.dumps(value, ensure_ascii=False)}: не число, "
                f"колонка использует значение по умолчанию ({rng})"]
    if value < low or value > high:
        shown = int(value) if float(value).is_integer() else value
        clamp = low if value < low else high
        return [f"pollPeriodSec = {shown}: колонка ограничит до {clamp} ({rng})"]
    return []


ACCOUNT_CONFIG_PATH = "config.account_config"


def account_override_error(path):
    """Текст ошибки, если путь переопределения аккаунта вне config.account_config.*, иначе None.
    Путь хранится полным (как у устройства); сам узел account_config целиком не заменяется."""
    if not isinstance(path, str) or not path.startswith(ACCOUNT_CONFIG_PATH + ".")             or not path[len(ACCOUNT_CONFIG_PATH) + 1:].strip("."):
        return (f"переопределение аккаунта: путь {path!r} вне {ACCOUNT_CONFIG_PATH}.* — "
                f"аккаунту принадлежит только account_config")
    return None


def validate_account_overrides(items):
    """Переопределения пользователя: список {path, source}, пути — только config.account_config.*.
    Возвращает копию; иначе QuasarConfigError."""
    if not isinstance(items, list):
        raise QuasarConfigError("quasar_overrides: список {path, source}")
    result = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or "source" not in item:
            raise QuasarConfigError("quasar_overrides: элемент — объект {path, source}")
        error = account_override_error(item["path"])
        if error:
            raise QuasarConfigError(error)
        entry = {"path": item["path"].strip("."), "source": copy.deepcopy(item["source"])}
        result.append(entry)
    return result


def account_layer(runtime, device):
    """(переопределения, имя пользователя) владельца устройства; у устройства без владельца
    слоя аккаунта нет — ([], None)."""
    user_id = (device or {}).get("user_id")
    if not user_id:
        return [], None
    user = next((u for u in runtime.identities.data["users"] if u["id"] == user_id), None)
    if not user:
        return [], None
    return list(user.get("quasar_overrides") or []), user.get("name") or user["id"]


def _apply_overrides(body, overrides, runtime, device, context, trace, label_of):
    for override in overrides or []:
        if not isinstance(override, dict) or not isinstance(override.get("path"), str):
            continue
        source = override.get("source")
        if isinstance(source, dict) and set(source) == {"delete"}:
            deep_set(body, override["path"], _DELETE)
            if trace is not None:
                trace[override["path"]] = label_of(source)
            continue
        if isinstance(source, dict) and source and all(k.startswith("$") for k in source)                 and inline_type(source) is not None:
            # переключение инлайн-узла (например {"$select": "…"}): мерджим в
            # существующий узел — варианты не дублируются
            existing = get_path(body, override["path"])
            if isinstance(existing, dict):
                deep_set(body, override["path"], merge_patch(existing, source))
                if trace is not None:
                    trace[override["path"]] = label_of(source)
                continue
        value = resolve_source(source, runtime, device, context)
        deep_set(body, override["path"], copy.deepcopy(value))
        if trace is not None:
            trace[override["path"]] = label_of(source)


def compose(common_body, platform_patch, overrides, runtime, device,
            trace=None, platform_label=None, account_overrides=None, account_name=None):
    """common → merge-patch платформы → переопределения аккаунта → переопределения устройства
    → инлайн-узлы → динамика.

    account_overrides — quasar_overrides владельца (только config.account_config.*; путь вне
    этой части — QuasarConfigError). Устройство сильнее аккаунта.
    trace: dict dotted-путь → подпись происхождения для просмотрщика UI;
    без trace поведение и стоимость прежние."""
    body = copy.deepcopy(common_body or {})
    if platform_patch is not None:
        body = merge_patch(body, copy.deepcopy(platform_patch))
        if trace is not None:
            label = f"платформа {platform_label}" if platform_label else "платформа"
            _trace_patch(trace, "", copy.deepcopy(platform_patch), label)
    context = context_for(runtime, device)
    if account_overrides:
        for item in account_overrides:
            error = account_override_error(item.get("path") if isinstance(item, dict) else None)
            if error:
                raise QuasarConfigError(error)
        owner = f"аккаунт {account_name}" if account_name else "аккаунт"

        def account_label(source):
            detail = _override_label(source)
            return owner if detail == "переопределение устройства" else f"{owner} ({detail.removeprefix('переопределение: ')})"
        _apply_overrides(body, account_overrides, runtime, device, context, trace, account_label)
    _apply_overrides(body, overrides, runtime, device, context, trace, _override_label)
    return resolve_tree(body, runtime, device, context, trace, "")
