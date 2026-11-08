"""Конфиги Quasar: встроенный шаблон и источники значений для замен.

Встроенный шаблон (quasar_defaults/): общий дефолт ответа /get_sync_info плюс квирк
платформы — generated_base(platform). Замены поверх шаблона — quasar_rules.py.

Источники значений: context_for — {{токены}} строк (время, устройство, владелец);
resolve_param — параметры (device.sn, user.name, каталог «Параметры», пути реестра);
db_lookup — значение из реестра эмулятора; run_command — stdout скрипта из белого
списка state/quasar/scripts. resolve_tree — финальный проход по телу: строки с
{{токенами}}, маркеры {"expr"|"db"|"run"} и инлайн-узлы прежнего формата тел
расширений ($select/$bool/$param/$string/$number/$list с $name/$hint) — внутри
вариантов выбора и значений, перенесённых из прежних тел.
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
            return store.resolve(name, user_id=user_id, sn=(device or {}).get("sn"),
                                 platform=context.get("platform") or None)
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
    """Разрешение листа: source-маркеры и строки-шаблоны; остальное как есть.
    render=False — сборка без устройства (просмотр слоёв): {{токены}} и
    source-маркеры (expr/db/run) остаются в тексте, подставлять нечего."""
    if not render:
        return value
    if isinstance(value, str):
        if "{{" in value:
            # токен вне контекста запроса — параметр (каталог «Параметры», пути реестра)
            def lookup(name, strict):
                if context.get(name) is not None:
                    return context[name]
                try:
                    return resolve_param(name, runtime, device, context)
                except QuasarConfigError:
                    if strict:
                        raise
                    return None

            def substitute(match):
                found = lookup(match.group(1), False)
                return "" if found is None else str(found)
            if trace is not None:
                used = sorted({m.group(1) for m in _TOKEN_DOTTED.finditer(value)})
                trace[path] = "динамика {{" + "}}, {{".join(used) + "}}"
            whole = _TOKEN_DOTTED.fullmatch(value.strip())
            if whole:
                # строка из одной подстановки — её значение как есть (число, объект);
                # параметра нет — ошибка, а не пустая строка в ответе колонке
                found = lookup(whole.group(1), whole.group(1) not in context)
                return "" if found is None else found
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


def resolve_inline(node, kind, runtime, device, context, trace=None, path="", render=True):
    if kind == "$select":
        chosen = node[kind]
        option = node.get(chosen) if isinstance(chosen, str) else None
        if option is None:
            raise QuasarConfigError(f"$select: вариант {chosen!r} не определён в узле "
                                    f"«{node.get('$name', '?')}»")
        if trace is not None and path not in trace:  # выбор переопределением важнее
            trace[path] = f"шаблон «{node.get('$name') or path}»: вариант «{chosen}»"
        return resolve_tree(option, runtime, device, context, trace, path, render)
    if kind == "$param":
        result = resolve_param(node[kind], runtime, device, context)
        if trace is not None:
            trace[path] = f"параметр {node[kind]}"
        return result
    if trace is not None and kind not in ("$select", "$param") and path not in trace:
        trace[path] = f"шаблон «{node.get('$name') or path}»"
    return resolve_tree(node[kind], runtime, device, context, trace, path, render)  # $string/$number/$list/$bool


def resolve_tree(tree, runtime, device, context, trace=None, path="", render=True):
    """Обход: инлайн-узлы и source-маркеры разрешаются, строки рендерятся.
    render=False — строки и маркеры остаются как есть (сборка без устройства)."""
    if isinstance(tree, dict):
        kind = inline_type(tree)
        if kind is not None:
            return resolve_inline(tree, kind, runtime, device, context, trace, path, render)
        marker = set(tree)
        if len(marker) == 1 and next(iter(marker)) in SOURCE_KEYS:
            if "delete" in marker:
                raise QuasarConfigError("delete-маркер допустим только в переопределении")
            return resolve_source(tree, runtime, device, context, trace, path, render)
        return {k: resolve_tree(v, runtime, device, context, trace,
                                f"{path}.{k}" if path else k, render) for k, v in tree.items()}
    if isinstance(tree, list):
        return [resolve_tree(item, runtime, device, context, trace,
                             f"{path}.{i}" if path else str(i), render) for i, item in enumerate(tree)]
    return resolve_source(tree, runtime, device, context, trace, path, render)


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


def count_keys(value):
    """Число листовых значений тела (счётчик размера слоя в UI)."""
    if isinstance(value, dict):
        return sum(count_keys(v) for v in value.values())
    if isinstance(value, list):
        return sum(count_keys(v) for v in value)
    return 1
