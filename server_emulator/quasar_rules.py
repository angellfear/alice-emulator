"""Замены Quasar-конфига: правила поверх встроенного шаблона.

Сборка ответа /get_sync_info (режим «Генерируемый», compose_config):

    встроенный шаблон (quasar_defaults: общий дефолт + особенности платформы)
      → общие замены → замены платформы      (расширения, затем поставляемые ⊕ правки state)
      → замены аккаунта → локальный адрес → замены устройства   (только state)
      → динамика ({{…}} в строках, маркеры db/run) → итог

Замены каждого слоя применяются сверху вниз: нижняя сильнее. Детали слоёв, хранения,
подмены каталога поставляемого набора и динамики — docs/contracts/quasar-config.md.

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
              (строгий Jinja2-шаблон bindings; строка из одной подстановки — её значение
              как есть), маркеры {"db": "user.…"} и {"run": "скрипт"}
    delete    удалить ключ

Отключённое правило (enabled: false) хранится, но не применяется. Ошибка источника
(подстановка не разрешилась, db-путь не найден, скрипт упал) не прерывает сборку:
правило пропускается — колонка получает значение нижнего слоя, ошибка попадает в
уведомление UI и журнал и снимается следующей сборкой без ошибок. Незаполненный
параметр каталога «Параметры» — тот же пропуск, текст «не заполнен параметр …».
Проверка правила при сохранении — pydantic-модель RuleModel (битый шаблон не сохраняется).
"""
import copy
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from jsonpointer import JsonPointer, JsonPointerException, escape
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import server_config
from .quasar_config import (ACCOUNT_CONFIG_PATH, QuasarConfigError, SCRIPTS_DIRNAME,
                            context_for, generated_base, resolve_tree, source_error_text)
from .scenario_engine.bindings import BindingError, check_template

RULE_KINDS = ("select", "delete")
SINGLE = "Значение"
PLATFORM_NAME = re.compile(r"[a-z0-9_.-]+")
NAME_LIMIT = 120
_MISSING = object()
QUASAR_RULES_DIR = Path(__file__).parent / "quasar_defaults" / "rules"  # поставляемый набор (в пакете)
# флаги строки редактора (shipped/modified/removed/base) — представление, не данные правила
VIEW_KEYS = ("shipped", "modified", "removed", "base")


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


def _check_template(value):
    """Синтаксис подстановок {{…}} в значении состояния (строгое подмножество bindings,
    без контекста — он зависит от устройства); битый шаблон — ошибка сохранения."""
    if isinstance(value, str):
        if "{{" in value or "{%" in value or "{#" in value:
            try:
                check_template(value)
            except BindingError as exc:
                raise ValueError(str(exc)) from None
    elif isinstance(value, dict):
        for item in value.values():
            _check_template(item)
    elif isinstance(value, list):
        for item in value:
            _check_template(item)


class RuleModel(BaseModel):
    """Замена Quasar-конфига (pydantic v2): проверка при сохранении — 4xx с полем."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_rule_id)
    name: str = ""
    path: str
    kind: str
    value: Any = None
    options: dict[str, Any] | None = None
    enabled: bool = True

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, value):
        return new_rule_id() if not value else str(value)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value):
        name = "" if value is None else str(value).strip()
        if len(name) > NAME_LIMIT:
            raise ValueError(f"имя длиннее {NAME_LIMIT} символов")
        return name

    @field_validator("path")
    @classmethod
    def _path(cls, value):
        pointer_parts(value)  # QuasarConfigError — подкласс ValueError
        return value

    @field_validator("kind")
    @classmethod
    def _kind(cls, value):
        if value not in RULE_KINDS:
            raise ValueError(f"вид {value!r} — один из {', '.join(RULE_KINDS)}")
        return value

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled(cls, value):
        return value is not False

    @model_validator(mode="after")
    def _states(self):
        if self.kind == "select":
            options = self.options
            if not isinstance(options, dict) or not options:
                raise ValueError("нужно хотя бы одно состояние")
            if any(not str(key).strip() for key in options):
                raise ValueError("у состояния пустое имя")
            if self.value not in options:
                raise ValueError(f"состояние {self.value!r} не из списка")
            for option in options.values():
                _check_template(option)
        else:
            self.value = None
            self.options = None
        return self


def validate_rule(item):
    """Нормализованная копия правила; иначе QuasarConfigError с полем."""
    if not isinstance(item, dict):
        raise QuasarConfigError("замена: объект {name, path, kind, value}")
    item = {key: value for key, value in item.items() if key not in VIEW_KEYS}
    try:
        model = RuleModel.model_validate(item)
    except ValidationError as exc:
        fields = "; ".join(".".join(str(part) for part in error["loc"]) + ": "
                           + error["msg"].removeprefix("Value error, ")
                           for error in exc.errors())
        raise QuasarConfigError(f"замена: {fields}") from None
    rule = copy.deepcopy(model.model_dump(exclude_none=True))
    if model.kind == "delete":
        rule["value"] = None
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


def rule_value(rule):
    """Значение текущего состояния; подстановки и маркеры разрешит общий проход resolve_tree."""
    options, value = rule.get("options") or {}, rule.get("value")
    if value not in options:
        raise QuasarConfigError(f"замена «{rule.get('name') or rule['path']}»: состояние {value!r} не из списка")
    return copy.deepcopy(options[value])


def apply_rules(body, rules, label, runtime, device, context, trace=None, render=True, errors=None):
    """Применить включённые правила слоя к телу (на месте). Правило с ошибкой источника
    (подстановка не разрешилась, db-путь не найден, скрипт упал) пропускается: колонка
    получает значение нижнего слоя, ошибка попадает в errors — уведомление UI и журнал,
    снимается следующей сборкой без ошибок."""
    for rule in rules:
        if rule.get("enabled") is False:
            continue
        parts = pointer_parts(rule["path"])
        key = pointer_of(parts)
        if rule["kind"] == "delete":
            _delete_at(body, parts)
            if trace is not None:
                trace[key] = f"{label}: {rule.get('name') or rule['path']} (удалено)"
            continue
        sub = {} if trace is not None else None
        raw = rule_value(rule)
        try:
            value = resolve_tree(raw, runtime, device, context, sub, key, render)
        except (QuasarConfigError, BindingError) as exc:
            if errors is not None:
                errors.append({"layer": label, "rule": rule.get("name") or rule["path"],
                               "path": key,
                               "error": source_error_text(exc, raw, runtime, device)})
            continue
        _set_at(body, parts, value)
        if trace is not None:
            trace[key] = f"{label}: {rule.get('name') or rule['path']}"
            trace.update(sub)  # динамика/БД/скрипт подписывают свои пути вместо метки слоя


# --- хранилище -------------------------------------------------------------------

class RuleStore:
    """Замены: поставляемый набор в пакете (quasar_defaults/rules/) + правки в
    rules/common.json, rules/platforms/<платформа>.json (state) + слои расширений
    (только чтение). extension_dirs — callable → [(ext_id, Path)] каталогов quasar/
    расширений."""

    def __init__(self, directory, extension_dirs=None, shipped_dir=None):
        self.directory = Path(directory)
        self.rules_dir = self.directory / "rules"
        self.platforms_dir = self.rules_dir / "platforms"
        self.scripts_dir = self.directory / SCRIPTS_DIRNAME
        self.extension_dirs = extension_dirs
        # поставляемый набор: None — слоя нет; runtime передаёт каталог пакета
        # (ALICA_QUASAR_RULES_DIR подменяет его для тестов)
        self.shipped_dir = Path(shipped_dir) if shipped_dir else None

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

    # слои расширений (только чтение)
    def _extension_layers(self):
        if self.extension_dirs is None:
            return []
        try:
            return [(ext_id, Path(path)) for ext_id, path in self.extension_dirs()]
        except Exception:  # noqa: BLE001 — расширения не должны ломать конфиг
            return []

    def extension_rules(self, layer):
        """[(ext_id, правила)] слоя из расширений (rules/…)."""
        result = []
        for ext_id, path in self._extension_layers():
            name = ("common.json" if layer == "common"
                    else str(Path("platforms") / (layer + ".json")))
            item = self._read(path / "rules" / name)
            try:
                if isinstance(item, dict) and isinstance(item.get("rules"), list):
                    result.append((ext_id, validate_rules(item["rules"])))
            except QuasarConfigError:
                continue  # битый слой расширения не ломает ответ колонке
        return result

    # поставляемый набор + правки state
    def load(self, layer):
        """Правки state слоя ('common' или имя платформы); нет файла — []."""
        if layer != "common":
            self.check_platform(layer)
        item = self._read(self._file(layer))
        if isinstance(item, dict) and isinstance(item.get("rules"), list):
            return validate_rules(item["rules"])
        return []

    def shipped(self, layer):
        """Поставляемые правила слоя из пакета; набор не задан, нет файла или битый — []."""
        if self.shipped_dir is None:
            return []
        if layer != "common":
            self.check_platform(layer)
        item = self._read(self._shipped_file(layer))
        if isinstance(item, dict) and isinstance(item.get("rules"), list):
            try:
                return validate_rules(item["rules"])
            except QuasarConfigError:
                return []  # битый поставляемый слой не ломает ответ колонке
        return []

    def _shipped_file(self, layer):
        if layer == "common":
            return self.shipped_dir / "common.json"
        return self.shipped_dir / "platforms" / (layer + ".json")

    def layer_rules(self, layer):
        """Поставляемое ⊕ правки state по id правила. Возвращает (для сборки, для
        редактора): список сборки — без снятых правил и без флагов; список редактора
        несёт флаги shipped/modified, записи снятых правил (removed) и исходное
        поставляемое правило (base) для «сбросить к поставляемому»."""
        shipped, state = self.shipped(layer), self.load(layer)
        state_by_id = {rule["id"]: rule for rule in state}
        effective, view = [], []
        for rule in shipped:
            edit = state_by_id.pop(rule["id"], None)
            if edit is None or edit == rule:
                effective.append(rule)
                view.append({**rule, "shipped": True, "modified": False})
            elif edit["kind"] == "delete":  # снятое поставляемое: маркер, не правка узла
                view.append({**rule, "shipped": True, "modified": True, "removed": True,
                             "base": rule})
            else:
                effective.append(edit)
                view.append({**edit, "shipped": True, "modified": True, "base": rule})
        for rule in state:
            if rule["id"] in state_by_id:  # не совпало с поставляемым — новое правило
                effective.append(rule)
                view.append(rule)
        return effective, view

    def save(self, layer, rules):
        """Сохранить слой целиком как правки поверх поставляемого набора: правило,
        совпадающее с поставляемым, не хранится (не дубликат); отсутствующее в списке
        поставляемое — delete-маркер с его id (набор в пакете не трогается)."""
        if layer != "common":
            self.check_platform(layer)
        rules = validate_rules(rules)
        shipped = {rule["id"]: rule for rule in self.shipped(layer)}
        overlay, seen = [], set()
        for rule in rules:
            seen.add(rule["id"])
            base = shipped.get(rule["id"])
            if base is None or rule != base:
                overlay.append(rule)
        for rule_id, base in shipped.items():
            if rule_id not in seen:
                overlay.append({"id": rule_id, "name": base["name"], "path": base["path"],
                                "kind": "delete", "value": None, "enabled": True})
        self._write(self._file(layer), {"rules": overlay})
        return rules

    def platforms(self):
        """Имена платформ с заменами: поставляемые + правки state + расширения."""
        names = set()
        if self.platforms_dir.is_dir():
            names.update(p.stem for p in self.platforms_dir.glob("*.json"))
        if self.shipped_dir is not None:
            shipped_platforms = self.shipped_dir / "platforms"
            if shipped_platforms.is_dir():
                names.update(p.stem for p in shipped_platforms.glob("*.json"))
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

    def layers(self, platform):
        """[(подпись, правила)] в порядке применения: общие (расширения → поставляемое ⊕
        правки) → платформы (расширения → поставляемое ⊕ правки)."""
        result = [(f"общие замены ({ext_id})", rules) for ext_id, rules in self.extension_rules("common")]
        result.append(("общие замены", self.layer_rules("common")[0]))
        if platform and PLATFORM_NAME.fullmatch(platform) and platform != "common":
            result += [(f"замены {platform} ({ext_id})", rules)
                       for ext_id, rules in self.extension_rules(platform)]
            result.append((f"замены {platform}", self.layer_rules(platform)[0]))
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
               "ws://{domain}/v2/subscribe/websocket"),
              # gproxy: arpc_client применяет адрес живьём; пути /gproxy/* на локальном домене
              # эмулятор обслуживает как rpc.alice.yandex.net (dispatcher.LOCAL_DOMAIN_PATHS)
              ("arpc_server_url", "/config/system_config/arpc_client/server_url", "http://{domain}"))


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
                   with_account=True, errors=None):
    """Итог: встроенный шаблон платформы → общие замены → замены платформы → аккаунт →
    локальный адрес → устройство → динамика. render=False — без устройства: подстановки
    {{ключ}} и маркеры db/run остаются как есть. Ошибка источника не прерывает сборку:
    правило пропускается (нижний слой), ошибки — в errors и в состоянии устройства
    (уведомление UI и журнал, снимаются следующей сборкой без ошибок)."""
    store = runtime.quasar_rules
    body = generated_base(platform)
    context = context_for(runtime, device)
    if platform and not context.get("platform"):
        context["platform"] = platform
    collected = errors if errors is not None else []
    for label, rules in store.layers(platform):
        apply_rules(body, rules, label, runtime, device, context, trace, render, collected)
    if with_account and device is not None:
        items, owner = account_rules(runtime, device)
        if items:
            for rule in items:
                if not rule["path"].startswith("/config/account_config/"):
                    raise QuasarConfigError(f"замена аккаунта: путь {rule['path']!r} вне /config/account_config")
            apply_rules(body, items, f"аккаунт {owner}", runtime, device, context, trace, render, collected)
    domain = local_domain(runtime) if device is not None else ""
    local = local_rules(device, domain)
    if local:
        apply_rules(body, local, f"локальный адрес {domain}",
                    runtime, device, context, trace, render, collected)
    if device_rules:
        apply_rules(body, device_rules, "устройство",
                    runtime, device, context, trace, render, collected)
    body = resolve_tree(body, runtime, device, context, trace, "", render, collected)
    if device is not None and render:
        _record_errors(runtime, device, collected)
    return body


def _record_errors(runtime, device, errors):
    """Ошибки последней сборки конфига устройства: состояние для уведомления UI
    (api/live, снимается следующей сборкой без ошибок) и запись в журнал сервера."""
    sn = (device or {}).get("sn")
    if not sn:
        return
    state = getattr(runtime, "quasar_errors", None)
    if not isinstance(state, dict):
        return  # подменный runtime без состояния ошибок (модульные тесты)
    if errors:
        state[sn] = copy.deepcopy(errors)
        log = getattr(runtime, "server_log", None)
        if log is not None:
            for item in errors:
                log.log("warning", "quasar",
                        f"конфиг колонки: {item['layer']} «{item['rule']}»: {item['error']}",
                        device_sn=sn)
    else:
        state.pop(sn, None)


def validate_account_rules(items):
    """Переопределения аккаунта: правила текущего формата с путями только
    /config/account_config/*. Возвращает копию; иначе QuasarConfigError."""
    result = []
    for rule in validate_rules(items):
        if not rule["path"].startswith("/config/account_config/") \
                or not rule["path"][len("/config/account_config/"):].strip("/"):
            raise QuasarConfigError(f"переопределение аккаунта: путь {rule['path']!r} вне "
                                    f"{ACCOUNT_CONFIG_PATH}/* — аккаунту принадлежит только account_config")
        result.append(rule)
    return result
