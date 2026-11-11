"""Конфиги Quasar: встроенный шаблон и источники значений для замен.

Встроенный шаблон (quasar_defaults/): общий дефолт ответа /get_sync_info плюс квирк
платформы — generated_base(platform). Замены поверх шаблона — quasar_rules.py.

Динамика — строки с подстановками {{путь|фильтр}} раскрываются окружением Jinja2
сценарного движка (bindings, строгое подмножество): корни контекста — context_for
(запросное время, устройство и владелец из реестра, каталог «Параметры»); строка
целиком из одной подстановки даёт её значение как есть (число, объект). Маркеры
{"db": "user.…"} и {"run": "скрипт"} — значение из реестра и stdout скрипта из
белого списка state/quasar/scripts. resolve_tree — обход тела: строки с
подстановками и маркеры разрешаются, остальное как есть.
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

from jsonpointer import escape

from .scenario_engine.bindings import BindingError, expand as expand_binding

SOURCE_KEYS = ("db", "run", "delete")
SCRIPTS_DIRNAME = "scripts"
RUN_TIMEOUT_S = 3
_TOKENS = re.compile(r"\{\{\s*(.*?)\s*\}\}")  # подпись происхождения, не разбор


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


def device_platform(device):
    """Платформа базы конфига для устройства: profile.platform → платформа, которую колонка
    сама сообщила при опросе конфига (state.reported_platform, запоминается в get_sync_info)
    → '' (общий дефолт). Первым файлом платформ не угадываем."""
    if not isinstance(device, dict):
        return ''
    explicit = (device.get('profile') or {}).get('platform')
    if explicit:
        return str(explicit)
    return str((device.get('state') or {}).get('reported_platform') or '')


# --- встроенный источник generated (server_emulator/quasar_defaults/) -----------

DEFAULTS_DIR = Path(__file__).parent / "quasar_defaults"
_GENERATED_CACHE: dict[str, dict] = {}
_PLATFORM_FILES: frozenset | None = None


def _platform_file_stems():
    """Имена файлов quasar_defaults/platforms/ (набор собирается один раз): имя платформы
    из реестра подставляется в путь файла только из этого белого списка."""
    global _PLATFORM_FILES
    if _PLATFORM_FILES is None:
        try:
            _PLATFORM_FILES = frozenset(p.stem for p in (DEFAULTS_DIR / "platforms").glob("*.json"))
        except OSError:
            _PLATFORM_FILES = frozenset()
    return _PLATFORM_FILES


def generated_platforms():
    """Имена платформ встроенной базы (файлы quasar_defaults/platforms/) —
    выбор платформы в редакторе слоёв; сгенерированный список отсортирован."""
    return sorted(_platform_file_stems())


def generated_base(platform):
    """Тело built-in источника generated: quasar_defaults/common.json — полный дефолт
    ответа без захвата (собран из исходников quasar-server, см.
    tools/build_quasar_defaults.py) — плюс merge-patch платформенного квирка
    (chiron/magritte; у платформы без файла — общий дефолт). Платформа — только имя
    файла из белого списка platforms/, иначе общий дефолт без патча (../ traversal
    не читает чужой файл). Персональные значения в дефолте несут {{...}}-токены —
    их заполняет динамика compose."""
    key = str(platform or "")
    if key not in _platform_file_stems():
        key = ""  # кэш — только по известным платформам, не по любой строке устройства
    if key not in _GENERATED_CACHE:
        body = json.loads((DEFAULTS_DIR / "common.json").read_text(encoding="utf-8"))
        if key:
            patch = json.loads((DEFAULTS_DIR / "platforms" / (key + ".json"))
                               .read_text(encoding="utf-8"))
            if isinstance(patch, dict):
                body = merge_patch(body, patch)
        _GENERATED_CACHE[key] = body
    return copy.deepcopy(_GENERATED_CACHE[key])


# --- контекст и источники значений (динамика) -----------------------------------

def context_for(runtime, device):
    """Корни подстановок {{…}}: запросное время; device/user — вложенные значения
    реестра; каталог «Параметры» вкладывается в те же корни (цепочка device → user
    → platform → server → default, занятые реестром пути не перезаписываются)."""
    now = time.time()
    context = {"now": int(now), "now_ms": int(now * 1000),
               "now_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "uuid": uuid.uuid4().hex,
               "device_id": "", "device_name": "", "platform": "", "user_id": "",
               "device": {}, "user": {}}
    if device:
        context.update({"device_id": next(iter(device.get("device_ids", [])), device.get("sn", "")),
                        "device_name": device.get("name", ""),
                        "platform": (device.get("profile") or {}).get("platform", "")})
        device_root = {"name": device.get("name", ""), "sn": device.get("sn", ""),
                       "fields": copy.deepcopy(device.get("fields") or {})}
        if device.get("room"):
            device_root["room"] = device["room"]
        context["device"] = device_root
        user = None
        if device.get("user_id"):
            user = next((u for u in runtime.identities.data["users"] if u["id"] == device["user_id"]), None)
        if user:
            context["user_id"] = user["id"]
            context["user"] = {"id": user["id"], "name": user.get("name", ""),
                               "location": copy.deepcopy(user.get("location") or {}),
                               "fields": copy.deepcopy(user.get("fields") or {})}
    params = getattr(runtime, "quasar_params", None)
    if params is not None:
        for definition in params.defs():
            try:
                value = params.resolve(definition["key"], user_id=context["user_id"] or None,
                                       sn=device.get("sn") if device else None,
                                       platform=context["platform"] or None)
            except KeyError:
                continue  # значения нет ни на одном уровне цепочки
            _nest(context, definition["key"], value)
    return context


def _nest(context, key, value):
    """Значение каталога «Параметры» (ключ через точку) — в корни контекста;
    занятые реестром пути не перезаписываются."""
    parts = str(key).split(".")
    node = context
    for part in parts[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            if part in node:
                return  # реестр занял путь не-объектом — параметр не вкладываем
            existing = node[part] = {}
        node = existing
    if parts[-1] not in node:
        node[parts[-1]] = value


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
    scripts = runtime.quasar_rules.scripts_dir.resolve()
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


def resolve_source(value, runtime, device, context, trace=None, path="", render=True):
    """Разрешение листа: строка с {{…}} — шаблон bindings (строка целиком из одной
    подстановки — её значение как есть); маркеры {"db"|"run"} — источники; остальное
    как есть. render=False — сборка без устройства: подстановки и маркеры остаются
    в тексте, подставлять нечего."""
    if not render:
        return value
    if isinstance(value, str):
        if "{{" in value or "{%" in value or "{#" in value:
            if trace is not None:
                used = sorted(set(_TOKENS.findall(value)))
                trace[path] = "динамика {{" + "}}, {{".join(used) + "}}"
            return expand_binding(value, context)
        return value
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


def resolve_tree(tree, runtime, device, context, trace=None, path="", render=True,
                 errors=None, label="шаблон"):
    """Обход: строки с подстановками и маркеры разрешаются, остальное как есть.
    render=False — всё остаётся как есть (сборка без устройства). Ошибка источника:
    errors is None — исключение (проверка правила при применении); иначе запись
    {layer, rule, path, error} в errors и значение как есть — сборка продолжается."""
    if isinstance(tree, dict):
        marker = set(tree)
        if len(marker) == 1 and next(iter(marker)) in SOURCE_KEYS:
            if "delete" in marker:
                raise QuasarConfigError("delete-маркер допустим только в переопределении")
            return _resolve_or_report(tree, runtime, device, context, trace, path, render,
                                      errors, label)
        return {k: resolve_tree(v, runtime, device, context, trace,
                                f"{path}/{escape(str(k))}", render, errors, label)
                for k, v in tree.items()}
    if isinstance(tree, list):
        return [resolve_tree(item, runtime, device, context, trace,
                             f"{path}/{i}", render, errors, label)
                for i, item in enumerate(tree)]
    return _resolve_or_report(tree, runtime, device, context, trace, path, render, errors, label)


def _unfilled_params(value, runtime, device):
    """Пути подстановок строки, у которых в каталоге «Параметры» есть определение,
    но нет значения ни на одном уровне цепочки — незаполненный параметр."""
    if not isinstance(value, str):
        return []
    params = getattr(runtime, "quasar_params", None)
    if params is None:
        return []
    platform = ((device or {}).get("profile") or {}).get("platform") or None
    missing = []
    for token in set(_TOKENS.findall(value)):
        path = token.split("|")[0].strip()
        if not path or not params.def_get(path):
            continue  # нет определения — это не параметр, а ошибка шаблона
        try:
            params.resolve(path, user_id=(device or {}).get("user_id") or None,
                           sn=(device or {}).get("sn") if device else None,
                           platform=platform)
        except KeyError:
            missing.append(path)
    return sorted(missing)


def source_error_text(exc, value, runtime, device):
    """Текст ошибки источника для списка ошибок: незаполненный параметр каталога —
    свой текст (ссылка на вкладку «Параметры»), прочее — как есть."""
    message = str(exc) or type(exc).__name__
    if isinstance(exc, BindingError):
        unfilled = _unfilled_params(value, runtime, device)
        if unfilled:  # параметр каталога без значения — не ошибка шаблона
            return "не заполнен параметр " + ", ".join(unfilled) + " — вкладка «Параметры»"
    return message


def _resolve_or_report(value, runtime, device, context, trace, path, render, errors, label):
    """resolve_source с учётом политики ошибок: собрать и продолжить — или отдать наверх."""
    try:
        return resolve_source(value, runtime, device, context, trace, path, render)
    except (QuasarConfigError, BindingError) as exc:
        if errors is None:
            raise
        errors.append({"layer": label, "rule": path or "(шаблон)", "path": path,
                       "error": source_error_text(exc, value, runtime, device)})
        return value  # значение как есть — в том же виде, что при render=False


# --- пути и предупреждения --------------------------------------------------------

def get_path(tree, dotted):
    node = tree
    for part in [p for p in dotted.split(".") if p]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


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


def count_keys(value):
    """Число листовых значений тела (счётчик размера слоя в UI)."""
    if isinstance(value, dict):
        return sum(count_keys(v) for v in value.values())
    if isinstance(value, list):
        return sum(count_keys(v) for v in value)
    return 1
