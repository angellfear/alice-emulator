"""R05: валидация сценария формата v2 (docs/contracts/scenario-json-v2.md).

Сценарий декларативен: никаких исполняемых Python/JS, произвольных import/eval.
Операции flow сверяются с закрытым реестром контрактов; payload_raw и сырой
protobuf (compose.raw) допустимы только с "debug": true — для отладки.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

CATEGORIES = {"system", "informers", "device", "smart_home", "timers", "custom"}
TRIGGER_KINDS = {"phrase", "event", "protocol_event", "intent", "schedule", "state_change", "template"}
EVENT_FILTER_KEYS = {"callback", "purpose", "frame"}
BINDING_SOURCES = {"constant", "input", "generated", "slot", "clock", "computed", "state", "media",
                   "user", "device", "animation", "template", "path"}
COMPUTED_FNS = {"timezone_offset_seconds", "slot_value"}
GENERATED_KINDS = {"uuid4_hex", "hex32", "uuid4_dash"}
CLOCK_OPS = {"now_epoch", "now_epoch_ms", "now_local_iso"}
MATCH_MODES = ("contains", "exact", "mask")
VERSION = re.compile(r"^\d+(\.\d+){0,3}$")
FORBIDDEN_KEYS = {"code", "script", "eval", "import", "exec", "python", "javascript", "command"}
TOP_KEYS = {"format_version", "id", "version", "category", "title", "description", "priority",
            "fallback", "enabled", "triggers", "guard", "lexicon", "tables", "slots", "flow",
            "executor", "provenance", "dependencies", "publication", "risk"}
# TASK-019: исполнитель — {"kind": "flow"} (как без поля) или {"kind": "script"};
# код никогда не кладётся внутрь JSON ("script" в FORBIDDEN_KEYS) — он живёт в <id>.py.
# Класс риска (agent/policy.py): scenario_run агента подтверждается для external/config/destructive.
RISKS = {"read", "device", "external", "config", "destructive"}

# Закрытый реестр контрактов операций flow (исполнение — executor).
FLOW_OPS = {
    "resolve": {"required": {"ref"}, "optional": {"slot", "next"}},
    "action": {"required": {"ref"}, "optional": {"arguments", "slot", "next", "on_failure"}},
    "compose": {"required": set(), "optional": {"reply", "raw", "debug", "next"}},
    "ask": {"required": {"question", "await"}, "optional": {"ttl", "next"}},
    "finish": {"required": set(), "optional": {"result"}},
    "error": {"required": set(), "optional": {"message"}},
}
JUMP_KEYS = ("next", "on_failure")
# TASK-027: шаги fetch/extract/load/refresh/store/branch/set/wait_state удалены вместе с кодом
# исполнителя. Логику с данными, извлечением, ветвлением и состоянием пишут в py (SDK,
# docs/contracts/scripts-sdk.md). Имена остаются здесь только ради понятной ошибки валидатора.
LEGACY_OPS = ("fetch", "extract", "load", "refresh", "store", "branch", "set", "wait_state")

REPLY_KEYS = {"envelope", "text", "speech", "speech_parts", "speech_prefix", "card", "directives"}
REPLY_DIRECTIVE_KEYS = {"name", "type", "analytics_type", "is_led_silent", "proto", "schema", "fields",
                        "payload", "payload_raw", "debug", "when"}

CONDITION_COMBINATORS = ("all", "any", "not")
CONDITION_LEAF_OPS = ("exists", "empty", "eq", "ne", "in", "gt", "ge", "lt", "le", "contains")


def trigger_phrases(trigger):
    """Фразы триггера: name и/или phrases[] — слово, предложение, список слов."""
    phrases = []
    if isinstance(trigger.get("name"), str) and trigger["name"]:
        phrases.append(trigger["name"])
    for phrase in trigger.get("phrases") or []:
        if isinstance(phrase, str) and phrase:
            phrases.append(phrase)
    return phrases


def _validate_template_trigger(trigger):
    """TASK-022: шаблон template-триггера проверяется при сохранении — синтаксис
    hassil каждой фразы (ошибка → понятный текст с номером фразы) и разрешимость
    {ссылок} на списки (свои lists триггера или общие lexicons.json)."""
    from ..scenario_engine.templates import TemplateError, check_template
    try:
        check_template(trigger)
    except TemplateError as exc:
        raise PackageError(str(exc)) from exc


class PackageError(ValueError):
    pass


def _reject_code(node, path="pkg"):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_KEYS:
                raise PackageError(f"исполняемое содержимое запрещено: {path}.{key}")
            _reject_code(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_code(item, f"{path}[{index}]")


def legacy_ops(scenario):
    """Удалённые шаги flow сценария (отсортированный список имён op); у py-сценария — пусто."""
    flow = scenario.get("flow") if isinstance(scenario, dict) else None
    if not isinstance(flow, list):
        return []
    return sorted({step["op"] for step in flow
                   if isinstance(step, dict) and step.get("op") in LEGACY_OPS})


def legacy_op_error(step_id, op):
    return PackageError(
        f"шаг {step_id}: шаг {op} больше не поддерживается — перенесите в py (executor script, "
        f"docs/contracts/scripts-sdk.md); в JSON остаются compose / action / ask / finish / error")


def validate_scenario(scenario):
    """Один сценарий v2. Возвращает scenario или бросает PackageError.
    Служебные ключи загрузчика (_origin, _file) игнорируются. Удалённые шаги (LEGACY_OPS)
    отвергаются всегда — исполнитель их больше не знает."""
    if not isinstance(scenario, dict):
        raise PackageError("сценарий должен быть объектом")
    _reject_code(scenario)
    if scenario.get("format_version") != 2:
        raise PackageError("format_version должен быть 2 (docs/contracts/scenario-json-v2.md)")
    unknown = {k for k in scenario if not str(k).startswith("_")} - TOP_KEYS
    if unknown:
        raise PackageError(f"неизвестные ключи сценария {sorted(unknown)}")
    if not re.fullmatch(r"[a-z0-9_.-]{1,64}", scenario.get("id", "")):
        raise PackageError("id: строчные буквы/цифры/._- до 64 символов")
    if not VERSION.fullmatch(scenario.get("version", "")):
        raise PackageError("version: числовые компоненты вида 1.0.0")
    if scenario.get("category", "custom") not in CATEGORIES:
        raise PackageError(f"category ∈ {sorted(CATEGORIES)}")
    if not isinstance(scenario.get("priority", 100), int):
        raise PackageError("priority: целое (меньше — раньше)")
    if not isinstance(scenario.get("fallback", False), bool):
        raise PackageError("fallback: bool")
    if scenario.get("risk", "device") not in RISKS:
        raise PackageError(f"risk ∈ {sorted(RISKS)}")
    triggers = scenario.get("triggers")
    if not isinstance(triggers, list) or not triggers:
        raise PackageError("triggers: непустой массив")
    for trigger in triggers:
        kind = trigger.get("kind") if isinstance(trigger, dict) else None
        if kind not in TRIGGER_KINDS:
            raise PackageError(f"trigger.kind ∈ {sorted(TRIGGER_KINDS)}")
        if kind == "template":
            _validate_template_trigger(trigger)
        elif kind == "phrase":
            if not trigger_phrases(trigger):
                raise PackageError("phrase-триггер: name (слово/предложение) и/или phrases[] (список)")
            if trigger.get("match") is not None and trigger.get("match") not in MATCH_MODES:
                raise PackageError(f"trigger.match ∈ {sorted(MATCH_MODES)}")
        else:
            if not isinstance(trigger.get("name"), str) or not trigger["name"]:
                raise PackageError("trigger.name обязателен")
            extra = set(trigger) - {"kind", "name"} - EVENT_FILTER_KEYS
            if extra:
                raise PackageError(f"trigger: неизвестные ключи {sorted(extra)}")
    if scenario.get("guard") is not None:
        validate_condition(scenario["guard"], "guard")
    for key in ("lexicon", "tables", "slots"):
        if not isinstance(scenario.get(key, {}), dict):
            raise PackageError(f"{key}: объект")
    for key in ("title", "description"):  # строка или {ru, en}
        value = scenario.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if not value.strip():
                raise PackageError(f"{key}: непустая строка или {{ru, en}}")
        elif not (isinstance(value, dict) and value and all(
                k in ("ru", "en") and isinstance(v, str) for k, v in value.items())):
            raise PackageError(f"{key}: строка или объект {{ru: str, en: str}}")
    executor = scenario.get("executor")
    if executor is not None and (not isinstance(executor, dict) or set(executor) != {"kind"}
                                 or executor.get("kind") not in ("flow", "script")):
        raise PackageError('executor: {"kind": "flow"} или {"kind": "script"}')
    if executor is not None and executor.get("kind") == "script":
        # TASK-019: py-сценарий без flow (логика в <id>.py); triggers/guard валидируются
        # как обычно. Наличие .py-файла валидатор не проверяет — это делают API и probe.
        if "flow" in scenario:
            raise PackageError("flow не нужен для executor script")
    else:
        flow = scenario.get("flow")
        if not isinstance(flow, list) or not flow:
            raise PackageError("flow: непустой массив шагов")
        seen = set()
        for step in flow:
            _validate_step(step, set(scenario.get("slots", {})))
            if step["id"] in seen:
                raise PackageError(f"повтор шага id={step['id']}")
            seen.add(step["id"])
        _validate_flow_graph(scenario)
        if not any(step["op"] in ("finish", "error") for step in flow):
            raise PackageError("flow должен завершаться finish/error")
    for dependency in scenario.get("dependencies", []):
        if not isinstance(dependency, dict) or not dependency.get("id") or not VERSION.fullmatch(dependency.get("version", "")):
            raise PackageError("dependency: {id, version}; версия пинится точно")
    publication = scenario.get("publication", {"enabled": False})
    if not isinstance(publication, dict) or not isinstance(publication.get("enabled", False), bool):
        raise PackageError("publication.enabled: bool")
    return scenario


def _validate_reply(reply, path="reply"):
    """Декларативный ответ: тексты — строка/шаблон/binding, директивы — закрытый набор полей."""
    if not isinstance(reply, dict):
        raise PackageError(f"{path}: объект")
    unknown = set(reply) - REPLY_KEYS
    if unknown:
        raise PackageError(f"{path}: неизвестные ключи {sorted(unknown)}")
    if reply.get("envelope", "dialog") not in ("dialog", "service"):
        raise PackageError(f"{path}.envelope ∈ dialog|service")
    for key in ("text", "speech", "speech_prefix"):
        if key in reply and not isinstance(reply[key], (str, dict)):
            raise PackageError(f"{path}.{key}: строка или binding")
        if isinstance(reply.get(key), dict):
            validate_binding(reply[key], f"{path}.{key}")
    if reply.get("card") is not None and not isinstance(reply["card"], bool):
        raise PackageError(f"{path}.card: bool")
    directives = reply.get("directives")
    if directives is not None:
        if not isinstance(directives, list):
            raise PackageError(f"{path}.directives: массив")
        for index, directive in enumerate(directives):
            where = f"{path}.directives[{index}]"
            if not isinstance(directive, dict):
                raise PackageError(f"{where}: объект")
            unknown = set(directive) - REPLY_DIRECTIVE_KEYS
            if unknown:
                raise PackageError(f"{where}: неизвестные ключи {sorted(unknown)}")
            if not isinstance(directive.get("name"), str) or not directive["name"]:
                raise PackageError(f"{where}.name: непустая строка (имя директивы)")
            if directive.get("payload_raw") is not None and directive.get("debug") is not True:
                raise PackageError(f"{where}.payload_raw: только для отладки — нужен \"debug\": true; "
                                   f"в сценариях payload описывается proto+fields")
            if directive.get("fields") is not None and not directive.get("proto"):
                raise PackageError(f"{where}.fields: нужен proto (тип payload)")
            if directive.get("proto") is not None and not isinstance(directive["proto"], str):
                raise PackageError(f"{where}.proto: полное имя типа protobuf")
            if directive.get("when") is not None:
                validate_condition(directive["when"], f"{where}.when")


def _validate_bindings_leaves(node, path):
    """Листья-словари с source — bindings; прочая структура произвольна (Struct)."""
    if isinstance(node, dict):
        if "source" in node:
            validate_binding(node, path)
            return
        for key, value in node.items():
            _validate_bindings_leaves(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _validate_bindings_leaves(item, f"{path}[{index}]")


def validate_condition(condition, path="condition"):
    """Guard-условие: {all|any|not: …} | {exists|eq|ne|in: [binding, значение]}."""
    if not isinstance(condition, dict) or not condition:
        raise PackageError(f"{path}: непустой объект")
    combinators = [k for k in CONDITION_COMBINATORS if k in condition]
    if combinators:
        if len(condition) > 1:
            raise PackageError(f"{path}: комбинатор один, без других ключей: {sorted(condition)}")
        value = condition[combinators[0]]
        if combinators[0] == "not":
            validate_condition(value, f"{path}.not")
            return
        if not isinstance(value, list) or not value:
            raise PackageError(f"{path}.{combinators[0]}: непустой список условий")
        for index, item in enumerate(value):
            validate_condition(item, f"{path}.{combinators[0]}[{index}]")
        return
    ops = [k for k in CONDITION_LEAF_OPS if k in condition]
    if len(ops) != 1 or len(condition) != 1:
        raise PackageError(f"{path}: лист — одна операция из {sorted(CONDITION_LEAF_OPS)}")
    args = condition[ops[0]]
    if not isinstance(args, list) or not args:
        raise PackageError(f"{path}.{ops[0]}: список [binding, значение]")
    for index, item in enumerate(args):
        if isinstance(item, dict) and "source" in item:
            validate_binding(item, f"{path}.{ops[0]}[{index}]")


def _validate_step(step, slots=frozenset()):
    if isinstance(step, dict) and step.get("id") and step.get("op") in LEGACY_OPS:
        raise legacy_op_error(step["id"], step["op"])
    if not isinstance(step, dict) or not step.get("id") or step.get("op") not in FLOW_OPS:
        raise PackageError(f"шаг flow: id и op ∈ {sorted(FLOW_OPS)}")
    contract = FLOW_OPS[step["op"]]
    keys = set(step)
    missing = contract["required"] - keys
    if missing:
        raise PackageError(f"шаг {step['id']} ({step['op']}): не хватает {sorted(missing)}")
    unknown = keys - contract["required"] - contract["optional"] - {"id", "op"}
    if unknown:
        raise PackageError(f"шаг {step['id']}: неизвестные ключи {sorted(unknown)}")
    op = step["op"]
    if op in ("resolve", "action") and (not isinstance(step.get("ref"), str) or not step["ref"]):
        raise PackageError(f"шаг {step['id']}: ref — непустая строка (ID примитива)")
    if op == "action" and not isinstance(step.get("arguments", {}), dict):
        raise PackageError(f"шаг {step['id']}: arguments — объект")
    if op == "compose":
        if ("reply" in step) == ("raw" in step):
            raise PackageError(f"шаг {step['id']}: compose — ровно одно из reply / raw")
        if "raw" in step:
            if step.get("debug") is not True:
                raise PackageError(f"шаг {step['id']}: raw — только для отладки, нужен \"debug\": true")
            raw = step["raw"]
            if not isinstance(raw, dict) or not isinstance(raw.get("fields", {}), dict):
                raise PackageError(f"шаг {step['id']}: raw {{schema, proto, fields}}")
        else:
            _validate_reply(step["reply"], f"{step['id']}.reply")
    if op == "ask":
        question = step.get("question")
        if not isinstance(question, (str, dict)) or not question:
            raise PackageError(f"шаг {step['id']}: question — непустая строка или binding")
        await_slot = step.get("await")
        if not isinstance(await_slot, str) or not await_slot:
            raise PackageError(f"шаг {step['id']}: await — имя слота")


def _validate_flow_graph(scenario):
    """AUD-04: ссылки next/on_failure существуют, все шаги достижимы от начала,
    из каждого достижимого шага есть путь к finish/error (циклы без выхода — ошибка).
    Нет явного next → шаг списка продолжается следующим элементом (fallthrough)."""
    flow = scenario["flow"]
    by_id = {step["id"]: step for step in flow}
    for step in flow:
        for key in JUMP_KEYS:
            target = step.get(key)
            if target is None:
                continue
            if not isinstance(target, str) or target not in by_id:
                raise PackageError(f"шаг {step['id']}: {key} → неизвестный шаг {target!r}")

    def successors(position):
        step = flow[position]
        if step["op"] in ("finish", "error"):
            names = []
        elif step.get("next"):
            names = [step["next"]]
        elif position + 1 < len(flow):
            names = [flow[position + 1]["id"]]  # fallthrough по порядку списка
        else:
            names = []
        if step.get("on_failure"):
            names.append(step["on_failure"])
        return [position_by_id[name] for name in names]

    position_by_id = {step["id"]: position for position, step in enumerate(flow)}
    reachable = set()
    stack = [0]
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        stack.extend(target for target in successors(current) if target not in reachable)
    orphans = sorted(step["id"] for position, step in enumerate(flow) if position not in reachable)
    if orphans:
        raise PackageError(f"недостижимые от начала шаги: {orphans}")
    terminals = {position for position, step in enumerate(flow) if step["op"] in ("finish", "error")}
    if not reachable & terminals:
        raise PackageError("от начала flow не достижим ни один finish/error")
    exits = set(terminals)
    changed = True
    while changed:
        changed = False
        for position in range(len(flow)):
            if position in exits:
                continue
            if set(successors(position)) & exits:
                exits.add(position)
                changed = True
    stuck = sorted(flow[position]["id"] for position in reachable if position not in exits)
    if stuck:
        raise PackageError(f"из шагов {stuck} нет пути к finish/error (цикл без выхода)")


def validate_binding(binding, path="binding"):
    if not isinstance(binding, dict) or binding.get("source") not in BINDING_SOURCES:
        raise PackageError(f"{path}: source ∈ {sorted(BINDING_SOURCES)}")
    source = binding["source"]
    if source == "computed" and binding.get("fn") not in COMPUTED_FNS:
        raise PackageError(f"{path}: fn ∈ {sorted(COMPUTED_FNS)}")
    if source == "generated" and binding.get("kind", "uuid4_hex") not in GENERATED_KINDS:
        raise PackageError(f"{path}: kind ∈ {sorted(GENERATED_KINDS)}")
    if source == "clock" and binding.get("op") not in CLOCK_OPS:
        raise PackageError(f"{path}: op ∈ {sorted(CLOCK_OPS)}")
    if source == "path" and not isinstance(binding.get("path"), str):
        raise PackageError(f"{path}.path: точечный путь контекста")
    if source == "template" and not isinstance(binding.get("value"), str):
        raise PackageError(f"{path}.value: строка с {{токен}}ами")


def validate_package(package, root=None):
    """Пакет-каталог v4 §10: manifest.json, scenarios/, schemas/. Проверяет sha256 схем."""
    root = Path(root) if root else None
    manifest = package.get("manifest")
    if not isinstance(manifest, dict) or manifest.get("format") != "scenario-package" or manifest.get("version") != 1:
        raise PackageError("manifest: format=scenario-package, version=1")
    _reject_code(manifest, "manifest")
    scenarios = package.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise PackageError("пакет без сценариев")
    ids = set()
    for scenario in scenarios:
        validate_scenario(scenario)
        if scenario["id"] in ids:
            raise PackageError(f"дублирующийся сценарий {scenario['id']}")
        ids.add(scenario["id"])
    for schema in package.get("schemas", []):
        entry = schema if isinstance(schema, dict) else {"file": schema}
        if root is not None and entry.get("sha256"):
            name = entry.get("file")
            if not isinstance(name, str) or not name:
                raise PackageError("schemas[].file — имя файла внутри пакета")
            base = (root / "schemas").resolve()
            path = (base / name).resolve()
            if not path.is_relative_to(base):  # AUD-05: границы пакета ДО любого чтения
                raise PackageError(f"схема {name!r} выходит за пределы пакета")
            if not path.is_file():
                raise PackageError(f"схема {name} отсутствует в пакете")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != entry["sha256"]:
                raise PackageError(f"sha256 схемы {name} не совпадает")
    return package
