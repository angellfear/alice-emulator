"""Замены Quasar-конфига: правила поверх встроенного шаблона.

Сборка ответа /get_sync_info (режим «Генерируемый»):

    встроенный шаблон (quasar_defaults: общий + квирк платформы)
      → общие замены → замены платформы → замены аккаунта → локальный адрес
      → замены устройства → динамика ({{токены}} в строках, маркеры db/run) → итог

Локальный адрес: колонке в «Эмуляции» и «Вместе» голосовой канал уходит на
ws://<local_domain>/uni.ws (server.json local_domain, по умолчанию alica.lan — имя в DNS
сети на IP сервера), без TLS и без подмены *.yandex.ru. Пустой local_domain — слоя нет.
Замена устройства этот адрес перебивает.

Замена — правило:

    {"id": "…", "name": "Подписка", "path": "/subscription", "kind": "select",
     "value": "Включена", "options": {"Включена": {…}, "Выключена": {…}}, "enabled": true}

path — JSON Pointer (RFC 6901): ключи с точкой (com.yandex.io.sdk) адресуются без
экранирования точек. Виды (kind):

    select    состояния узла: options — имя → значение, value — имя текущего.
              Одно состояние — значение узла, два — переключатель вкл/выкл (первое —
              «вкл»), больше — выбор. Значение — любой JSON; в строках подстановки {{…}}
              (токены запроса и параметры; строка из одной подстановки — её значение
              как есть), маркеры {"db": "user.…"} и {"run": "скрипт"}
    delete    удалить ключ

Прежние виды принимаются и переводятся в состояния: bool → «Вкл»/«Выкл»; value,
template, param («{{ключ}}»), db ({"db": …}), script ({"run": …}) — одно состояние.

Отключённое правило (enabled: false) хранится, но не применяется.

Хранение: state/quasar/rules/common.json и rules/platforms/<платформа>.json —
{"rules": [...]}. Замены устройства — state.quasar_overrides устройства, аккаунта —
quasar_overrides пользователя (там же принимается прежний формат {path, source} с
путём через точку — он переводится в правило при чтении). Расширения дают свои
правила (quasar/rules/…) или прежние тела (quasar/common.json, platforms/*.json) —
только чтение, применяются раньше пользовательских.
"""
import copy
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from jsonpointer import JsonPointer, JsonPointerException, escape

from . import server_config
from .quasar_config import (INLINE_KEYS, QuasarConfigError, SCRIPTS_DIRNAME, context_for,
                            generated_base, generated_platforms, merge_patch,
                            resolve_tree)

RULE_KINDS = ("select", "delete")
LEGACY_KINDS = ("value", "bool", "param", "template", "db", "script")
ON, OFF, SINGLE = "Вкл", "Выкл", "Значение"
PLATFORM_NAME = re.compile(r"[a-z0-9_.-]+")
NAME_LIMIT = 120
_MISSING = object()


# --- пути ------------------------------------------------------------------------

def pointer_parts(path):
    """Части JSON Pointer; пустой путь (весь документ) не допускается."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise QuasarConfigError(f"путь {path!r}: JSON Pointer, начинается с «/»")
    try:
        parts = JsonPointer(path).parts
    except JsonPointerException as exc:
        raise QuasarConfigError(f"путь {path!r}: {exc}")
    if not parts or any(part == "" for part in parts):
        raise QuasarConfigError(f"путь {path!r}: пустой сегмент")
    return parts


def pointer_of(parts):
    return "/" + "/".join(escape(str(part)) for part in parts)


def dotted_to_pointer(dotted):
    """Прежний путь через точку (ключи без точек) → JSON Pointer."""
    parts = [p for p in str(dotted or "").split(".") if p]
    if not parts:
        raise QuasarConfigError("путь: пустой")
    return pointer_of(parts)


def trace_key(parts):
    """Ключ происхождения для просмотрщика UI (путь через точку, как строки JSON)."""
    return ".".join(str(part) for part in parts)


def get_at(tree, parts):
    node = tree
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _set_at(tree, parts, value):
    node = tree
    for part in parts[:-1]:
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
            continue
        if not isinstance(node, dict):
            raise QuasarConfigError(f"путь /{'/'.join(parts)}: «{part}» — не объект")
        if not isinstance(node.get(part), (dict, list)):
            node[part] = {}
        node = node[part]
    last = parts[-1]
    if isinstance(node, list):
        if last == "-":
            node.append(value)
        elif last.isdigit() and int(last) < len(node):
            node[int(last)] = value
        else:
            raise QuasarConfigError(f"путь /{'/'.join(parts)}: индекс вне списка")
    elif isinstance(node, dict):
        node[last] = value
    else:
        raise QuasarConfigError(f"путь /{'/'.join(parts)}: родитель — не объект")


def _delete_at(tree, parts):
    parent = get_at(tree, parts[:-1]) if len(parts) > 1 else tree
    last = parts[-1]
    if isinstance(parent, dict):
        parent.pop(last, None)
    elif isinstance(parent, list) and last.isdigit() and int(last) < len(parent):
        parent.pop(int(last))


# --- правила ---------------------------------------------------------------------

def new_rule_id():
    return uuid.uuid4().hex[:10]


def normalize_kind(item):
    """Прежние виды → состояния. Копия, если менялось."""
    kind = item.get("kind") if isinstance(item, dict) else None
    if kind not in LEGACY_KINDS:
        return item
    item = dict(item)
    value = item.get("value")
    label = item.get("name") or item.get("path")
    if kind == "bool":
        if not isinstance(value, bool):
            raise QuasarConfigError(f"замена «{label}»: флаг — true или false")
        item.update(kind="select", options={ON: True, OFF: False}, value=ON if value else OFF)
        return item
    if kind in ("param", "db", "script"):
        if not isinstance(value, str) or not value.strip():
            raise QuasarConfigError(f"замена «{label}»: пустое значение")
        value = value.strip()
        value = {"param": "{{" + value + "}}", "db": {"db": value}, "script": {"run": value}}[kind]
    item.update(kind="select", options={SINGLE: copy.deepcopy(value)}, value=SINGLE)
    return item


def validate_rule(item):
    """Нормализованная копия правила; иначе QuasarConfigError с понятным текстом."""
    if not isinstance(item, dict):
        raise QuasarConfigError("замена: объект {name, path, kind, value}")
    item = normalize_kind(item)
    kind = item.get("kind")
    if kind not in RULE_KINDS:
        raise QuasarConfigError(f"замена: вид {kind!r} — один из {', '.join(RULE_KINDS)}")
    path = item.get("path")
    pointer_parts(path)
    name = item.get("name")
    name = str(name).strip() if name is not None else ""
    if len(name) > NAME_LIMIT:
        raise QuasarConfigError(f"замена: имя длиннее {NAME_LIMIT} символов")
    rule = {"id": str(item.get("id") or new_rule_id()), "name": name, "path": path,
            "kind": kind, "enabled": item.get("enabled", True) is not False}
    value = copy.deepcopy(item.get("value"))
    label = name or path
    if kind == "select":
        options = item.get("options")
        if not isinstance(options, dict) or not options:
            raise QuasarConfigError(f"замена «{label}»: нужно хотя бы одно состояние")
        if any(not str(key).strip() for key in options):
            raise QuasarConfigError(f"замена «{label}»: у состояния пустое имя")
        if value not in options:
            raise QuasarConfigError(f"замена «{label}»: состояние {value!r} не из списка")
        rule["options"] = copy.deepcopy(options)
    else:
        value = None
    rule["value"] = value
    return rule


def validate_rules(items):
    if not isinstance(items, list):
        raise QuasarConfigError("замены: список правил")
    rules, seen = [], set()
    for index, item in enumerate(items, 1):
        try:
            rule = validate_rule(item)
        except QuasarConfigError as exc:
            raise QuasarConfigError(f"Строка {index}: {exc}")
        while rule["id"] in seen:
            rule["id"] = new_rule_id()
        seen.add(rule["id"])
        rules.append(rule)
    return rules


def legacy_override_to_rule(item, select_options=None):
    return normalize_kind(_legacy_override_to_rule(item, select_options))


def _legacy_override_to_rule(item, select_options=None):
    """Прежнее переопределение {path (через точку), source} → правило.
    select_options — pointer → варианты выбора нижних слоёв (для {"$select": …})."""
    path = dotted_to_pointer(item.get("path"))
    source = item.get("source")
    name = item.get("path").rsplit(".", 1)[-1]
    if isinstance(source, dict) and set(source) == {"delete"}:
        return {"name": name, "path": path, "kind": "delete", "value": None}
    if isinstance(source, dict) and len(source) == 1:
        (key, value), = source.items()
        if key in ("expr", "db", "run"):
            return {"name": name, "path": path,
                    "kind": {"expr": "template", "db": "db", "run": "script"}[key], "value": value}
    if isinstance(source, dict) and "$select" in source and not (set(source) - set(INLINE_KEYS) - {"$name", "$hint"}):
        options = (select_options or {}).get(path)
        if options and source["$select"] in options:
            return {"name": name, "path": path, "kind": "select", "value": source["$select"],
                    "options": copy.deepcopy(options)}
    if isinstance(source, dict) and set(source) & set(INLINE_KEYS):
        rule = inline_to_rule(source, path, name)
        if rule is not None:
            return rule
    return {"name": name, "path": path, "kind": "bool" if isinstance(source, bool) else "value",
            "value": copy.deepcopy(source)}


def inline_to_rule(node, path, fallback_name):
    rule = _inline_to_rule(node, path, fallback_name)
    return normalize_kind(rule) if rule else rule


def _inline_to_rule(node, path, fallback_name):
    """Инлайн-узел прежнего тела ($select/$bool/$param/…) → правило."""
    kinds = set(node) & set(INLINE_KEYS)
    if len(kinds) != 1:
        return None
    kind = next(iter(kinds))
    name = node.get("$name") or fallback_name
    value = node[kind]
    if kind == "$select":
        options = {k: copy.deepcopy(v) for k, v in node.items() if not k.startswith("$")}
        return {"name": name, "path": path, "kind": "select", "value": value, "options": options}
    if kind == "$bool":
        return {"name": name, "path": path, "kind": "bool", "value": bool(value)}
    if kind == "$param":
        return {"name": name, "path": path, "kind": "param", "value": value}
    if kind == "$string" and isinstance(value, str) and "{{" in value:
        return {"name": name, "path": path, "kind": "template", "value": value}
    return {"name": name, "path": path, "kind": "value", "value": copy.deepcopy(value)}


def rules_as_list(items, select_options=None):
    """Правила слоя: новый формат как есть, прежний {path, source} — переводится."""
    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if "kind" in item:
            try:
                result.append(normalize_kind(item))
            except QuasarConfigError:
                continue  # битая запись не ломает сборку остальных
        elif isinstance(item.get("path"), str) and "source" in item:
            result.append(legacy_override_to_rule(item, select_options))
    return result


def rule_value(rule):
    """Значение текущего состояния; подстановки и маркеры разрешит общий проход resolve_tree."""
    options, value = rule.get("options") or {}, rule.get("value")
    if value not in options:
        raise QuasarConfigError(f"замена «{rule.get('name') or rule['path']}»: состояние {value!r} не из списка")
    return copy.deepcopy(options[value])


def apply_rules(body, rules, label, runtime, device, context, trace=None, render=True):
    """Применить включённые правила слоя к телу (на месте)."""
    for rule in rules:
        if rule.get("enabled") is False:
            continue
        parts = pointer_parts(rule["path"])
        if rule["kind"] == "delete":
            _delete_at(body, parts)
        else:
            _set_at(body, parts, rule_value(rule))
        if trace is not None:
            trace[trace_key(parts)] = f"{label}: {rule.get('name') or rule['path']}" \
                + (" (удалено)" if rule["kind"] == "delete" else "")


def select_options_of(rules):
    return {r["path"]: r["options"] for r in rules if r.get("kind") == "select" and r.get("options")}


# --- перенос прежних тел в правила --------------------------------------------------

def _has_inline(node):
    if isinstance(node, dict):
        if set(node) & set(INLINE_KEYS):
            return True
        return any(_has_inline(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_inline(v) for v in node)
    return False


def body_to_rules(body, references):
    """Прежнее тело слоя (merge-patch поверх нижних слоёв, с инлайн-узлами) → правила,
    дающие тот же итог поверх КАЖДОГО из references (тела нижних слоёв по платформам):
    правило создаётся там, где значение отличается хотя бы от одной ссылки."""
    rules = []

    def walk(node, refs, parts):
        path = pointer_of(parts)
        name = str(parts[-1])
        if isinstance(node, dict) and set(node) & set(INLINE_KEYS):
            rule = inline_to_rule(node, path, name)
            if rule is not None:
                rules.append(rule)
            return
        if isinstance(node, dict) and node and (
                all(isinstance(r, dict) for r in refs)
                or (all(r is _MISSING for r in refs) and _has_inline(node))):
            for key, value in node.items():
                child_refs = [r.get(key, _MISSING) if isinstance(r, dict) else _MISSING for r in refs]
                if value is None:
                    if any(r is not _MISSING for r in child_refs):
                        rules.append({"name": str(key), "path": pointer_of(parts + [key]),
                                      "kind": "delete", "value": None})
                    continue
                walk(value, child_refs, parts + [key])
            return
        if node == {} and any(isinstance(r, dict) for r in refs):
            return  # пустой объект при слиянии ничего не менял
        if any(r is _MISSING or r != node for r in refs):
            kind = "template" if isinstance(node, str) and "{{" in node \
                else "bool" if isinstance(node, bool) else "value"
            rules.append({"name": name, "path": path, "kind": kind, "value": copy.deepcopy(node)})

    if isinstance(body, dict):
        for key, value in body.items():
            refs = [r.get(key, _MISSING) if isinstance(r, dict) else _MISSING for r in references]
            if value is None:
                if any(r is not _MISSING for r in refs):
                    rules.append({"name": str(key), "path": pointer_of([key]), "kind": "delete", "value": None})
                continue
            walk(value, refs, [key])
    return validate_rules(rules)


def _plain_apply(body, rules):
    """Применение правил без устройства и без динамики (для расчёта ссылок переноса)."""
    result = copy.deepcopy(body)
    for rule in rules:
        if rule.get("enabled") is False:
            continue
        parts = pointer_parts(rule["path"])
        if rule["kind"] == "delete":
            _delete_at(result, parts)
        elif rule["kind"] == "select":
            _set_at(result, parts, copy.deepcopy(rule["options"][rule["value"]]))
        else:
            _set_at(result, parts, copy.deepcopy(rule["value"]))
    return result


# --- хранилище -------------------------------------------------------------------

class RuleStore:
    """Замены: rules/common.json, rules/platforms/<платформа>.json (пользовательские) +
    слои расширений (только чтение). Прежние тела state/quasar/common.json и
    platforms/*.json один раз переносятся в правила, оригиналы — в legacy-<дата>/.
    extension_dirs — callable → [(ext_id, Path)] каталогов quasar/ расширений."""

    def __init__(self, directory, extension_dirs=None):
        self.directory = Path(directory)
        self.rules_dir = self.directory / "rules"
        self.platforms_dir = self.rules_dir / "platforms"
        self.scripts_dir = self.directory / SCRIPTS_DIRNAME
        self.extension_dirs = extension_dirs
        self._migrated = False

    # файлы
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

    def _file(self, layer):
        if layer == "common":
            return self.rules_dir / "common.json"
        return self.platforms_dir / (layer + ".json")

    @staticmethod
    def check_platform(name):
        if not isinstance(name, str) or not PLATFORM_NAME.fullmatch(name) or name == "common":
            raise QuasarConfigError("платформа: строка [a-z0-9_.-], common зарезервировано")
        return name

    # перенос прежних тел
    def migrate(self):
        """Однократный перенос прежних тел в правила. Возвращает отчёт или None."""
        if self._migrated:
            return None
        self._migrated = True
        legacy_common = self.directory / "common.json"
        legacy_platforms = sorted((self.directory / "platforms").glob("*.json")) \
            if (self.directory / "platforms").is_dir() else []
        if self.rules_dir.exists() or (not legacy_common.exists() and not legacy_platforms):
            return None
        common_body = self._read(legacy_common)
        references = [generated_base(name) for name in [""] + generated_platforms()]
        common_rules = body_to_rules(common_body, references) if isinstance(common_body, dict) else []
        self._write(self._file("common"), {"rules": common_rules})
        report = {"common": len(common_rules), "platforms": {}}
        for path in legacy_platforms:
            patch = self._read(path)
            if not isinstance(patch, dict) or not PLATFORM_NAME.fullmatch(path.stem):
                continue
            # null в файле платформы колонке не доходил (merge-patch в пустой объект его
            # отбрасывал) — перенос сохраняет действующий итог, а не написанное
            patch = merge_patch({}, patch)
            reference = _plain_apply(generated_base(path.stem), common_rules)
            rules = body_to_rules(patch, [reference])
            self._write(self._file(path.stem), {"rules": rules})
            report["platforms"][path.stem] = len(rules)
        backup = self.directory / ("legacy-" + time.strftime("%Y%m%d-%H%M%S"))
        backup.mkdir(parents=True, exist_ok=True)
        if legacy_common.exists():
            shutil.move(str(legacy_common), str(backup / "common.json"))
        if legacy_platforms:
            shutil.move(str(self.directory / "platforms"), str(backup / "platforms"))
        return report

    # слои расширений (только чтение)
    def _extension_layers(self):
        if self.extension_dirs is None:
            return []
        try:
            return [(ext_id, Path(path)) for ext_id, path in self.extension_dirs()]
        except Exception:  # noqa: BLE001 — расширения не должны ломать конфиг
            return []

    def extension_rules(self, layer):
        """[(ext_id, правила)] слоя из расширений: rules/… или прежнее тело (переводится)."""
        result = []
        for ext_id, path in self._extension_layers():
            if layer == "common":
                ruled, body_file = path / "rules" / "common.json", path / "common.json"
                references = [generated_base(name) for name in [""] + generated_platforms()]
            else:
                ruled = path / "rules" / "platforms" / (layer + ".json")
                body_file = path / "platforms" / (layer + ".json")
                references = [generated_base(layer)]
            item = self._read(ruled)
            try:
                if isinstance(item, dict) and isinstance(item.get("rules"), list):
                    result.append((ext_id, validate_rules(item["rules"])))
                    continue
                body = self._read(body_file)
                if isinstance(body, dict):
                    if layer != "common":  # как при переносе: null платформы не действовал
                        body = merge_patch({}, body)
                    result.append((ext_id, body_to_rules(body, references)))
            except QuasarConfigError:
                continue  # битый слой расширения не ломает ответ колонке
        return result

    # пользовательские правила
    def load(self, layer):
        """Пользовательские правила слоя ('common' или имя платформы); нет файла — []."""
        self.migrate()
        if layer != "common":
            self.check_platform(layer)
        item = self._read(self._file(layer))
        if isinstance(item, dict) and isinstance(item.get("rules"), list):
            return rules_as_list(item["rules"])
        return []

    def save(self, layer, rules):
        self.migrate()
        if layer != "common":
            self.check_platform(layer)
        rules = validate_rules(rules)
        self._write(self._file(layer), {"rules": rules})
        return rules

    def platforms(self):
        """Имена платформ с заменами: пользовательские файлы + расширения."""
        self.migrate()
        names = set()
        if self.platforms_dir.is_dir():
            names.update(p.stem for p in self.platforms_dir.glob("*.json"))
        for _ext_id, path in self._extension_layers():
            for folder in (path / "rules" / "platforms", path / "platforms"):
                if folder.is_dir():
                    names.update(p.stem for p in folder.glob("*.json"))
        return sorted(n for n in names if PLATFORM_NAME.fullmatch(n))

    def has_platform(self, name):
        return name in self.platforms()

    def create_platform(self, name):
        self.check_platform(name)
        if self._file(name).exists():
            raise QuasarConfigError(f"платформа {name} уже есть")
        self.save(name, [])

    def rename_platform(self, old, new):
        self.check_platform(old)
        self.check_platform(new)
        source, target = self._file(old), self._file(new)
        if not source.exists():
            raise QuasarConfigError(f"платформа {old} не найдена")
        if target.exists():
            raise QuasarConfigError(f"платформа {new} уже есть")
        os.replace(source, target)

    def remove_platform(self, name):
        self.check_platform(name)
        target = self._file(name)
        if not target.exists():
            return False
        target.unlink()
        return True

    def is_empty(self):
        """Нет ни одного слоя замен (ни пользовательского, ни расширений, ни прежних тел)."""
        self.migrate()
        return (not self.rules_dir.exists()
                and not any(self.extension_rules("common"))
                and not self.platforms())

    def layers(self, platform):
        """[(подпись, правила)] в порядке применения: общие (расширения → свои) →
        платформы (расширения → свои)."""
        result = [(f"общие замены ({ext_id})", rules) for ext_id, rules in self.extension_rules("common")]
        result.append(("общие замены", self.load("common")))
        if platform and PLATFORM_NAME.fullmatch(platform) and platform != "common":
            result += [(f"замены {platform} ({ext_id})", rules)
                       for ext_id, rules in self.extension_rules(platform)]
            result.append((f"замены {platform}", self.load(platform)))
        return result


# --- сборка ---------------------------------------------------------------------

def account_rules(runtime, device):
    """(правила, имя) аккаунта владельца устройства; без владельца — ([], None)."""
    user_id = (device or {}).get("user_id")
    if not user_id:
        return [], None
    user = next((u for u in runtime.identities.data["users"] if u["id"] == user_id), None)
    if not user:
        return [], None
    return list(user.get("quasar_overrides") or []), user.get("name") or user["id"]


LOCAL_MODES = ("emulate", "hybrid")
LOCAL_URLS = (("uniProxyUrl", "/config/system_config/uniProxyUrl", "ws://{domain}/uni.ws"),
              # пуш-канал xiva: pushd переподключается на новый адрес живьём, эмулятор мостит
              # его к push.yandex.ru (dispatcher, XIVA_PATH)
              ("xivaSubscribeUrl", "/config/system_config/pushd/xivaSubscribeUrl",
               "ws://{domain}/v2/subscribe/websocket"))


def local_domain(runtime):
    """Локальный домен из server.json (читается на каждую сборку — без перезапуска); пусто — ""."""
    return server_config.load(runtime.directory).get("local_domain") or ""


def local_rules(device, domain):
    """Замены «локальный адрес» для устройства в «Эмуляции»/«Вместе»; пустой домен — []."""
    if not domain or device is None or (device.get("route_mode") or "emulate") not in LOCAL_MODES:
        return []
    return [{"name": name, "path": path, "kind": "select", "enabled": True,
             "options": {SINGLE: url.format(domain=domain)}, "value": SINGLE}
            for name, path, url in LOCAL_URLS]


def compose_config(runtime, platform, device=None, device_rules=None, trace=None, render=True,
                   with_account=True):
    """Итог: встроенный шаблон платформы → общие замены → замены платформы → аккаунт →
    локальный адрес → устройство → динамика. render=False — без устройства: параметры остаются {{ключ}},
    db/run — маркерами, {{токены}} в строках — как есть."""
    store = runtime.quasar_rules
    body = generated_base(platform)
    context = context_for(runtime, device)
    if platform and not context.get("platform"):
        context["platform"] = platform
    options = {}
    for label, rules in store.layers(platform):
        rules = rules_as_list(rules, options)
        apply_rules(body, rules, label, runtime, device, context, trace, render)
        options.update(select_options_of(rules))
    if with_account and device is not None:
        items, owner = account_rules(runtime, device)
        if items:
            rules = rules_as_list(items, options)
            for rule in rules:
                if not rule["path"].startswith("/config/account_config/"):
                    raise QuasarConfigError(f"замена аккаунта: путь {rule['path']!r} вне /config/account_config")
            apply_rules(body, rules, f"аккаунт {owner}", runtime, device, context, trace, render)
            options.update(select_options_of(rules))
    domain = local_domain(runtime) if device is not None else ""
    local = local_rules(device, domain)
    if local:
        apply_rules(body, rules_as_list(local), f"локальный адрес {domain}",
                    runtime, device, context, trace, render)
    if device_rules:
        apply_rules(body, rules_as_list(device_rules, options), "устройство",
                    runtime, device, context, trace, render)
    return resolve_tree(body, runtime, device, context, trace, "", render)


def layer_select_options(runtime, platform):
    """Варианты выбора нижних слоёв (для перевода прежних {"$select": …} устройства)."""
    options = {}
    for _label, rules in runtime.quasar_rules.layers(platform):
        options.update(select_options_of(rules_as_list(rules, options)))
    return options
