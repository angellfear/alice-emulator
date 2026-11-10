"""Типизированные bindings для recipe (v3 §4) и библиотечные функции.

Binding — декларативное описание источника значения поля:
  constant | input | generated | slot | clock | computed | state | media | user | device
Исполнение произвольного кода исключено: computed-функции берутся из закрытого
реестра LIBRARY, аргументы — плоские литералы. user/device — контекст запроса
(профиль владельца и запись устройства: гео для погоды, комната для света).

template  — реплика целиком в JSON: {"source": "template",
"value": "Таймер на {{slots.seconds|duration}}."} — раскрытие {{путь|фильтр(аргументы)}}
по контексту; фильтры — закрытый реестр FILTERS (форматирование русских
числительных), сами тексты фраз живут в JSON-сценарии.
"""
from __future__ import annotations

import datetime
import functools
import re
import uuid

from jinja2 import StrictUndefined, Undefined, nodes, pass_context
from jinja2.exceptions import (SecurityError, TemplateAssertionError,
                               TemplateSyntaxError, UndefinedError)
from jinja2.sandbox import ImmutableSandboxedEnvironment

SOURCES = {"constant", "input", "generated", "slot", "clock", "computed", "state",
           "user", "device", "animation", "template", "path"}
GENERATED_KINDS = {"uuid4_hex": lambda: uuid.uuid4().hex,
                   "hex32": lambda: uuid.uuid4().hex,
                   "uuid4_dash": lambda: str(uuid.uuid4())}
CLOCK_OPS = {"now_epoch", "now_epoch_ms", "now_local_iso"}


class BindingError(ValueError):
    pass


def _units(value, forms):
    """forms — (род.-мн, род.-ед, им.): («минут», «минуты», «минуту»).
    Возвращает «число + форма»: units(5, …) → «5 минут»."""
    value = int(value)
    form = forms[2] if value % 10 == 1 and value % 100 != 11 else \
        forms[1] if 2 <= value % 10 <= 4 and value % 100 not in (12, 13, 14) else forms[0]
    return f"{value} {form}"


def _time_words(now):
    """Время словами без «Сейчас» и точки: «12 часов» / «12 часов 5 минут».
    Минуты — именительный падеж (показание времени: «21 минута»), в отличие
    от длительности (аккузатив: «таймер на 21 минуту»)."""
    hour = now.hour % 24
    minute = now.minute
    hour_part = _units(hour, ("часов", "часа", "час"))
    if minute == 0:
        return hour_part
    return f"{hour_part} {_units(minute, ('минут', 'минуты', 'минута'))}"


def _format_duration(seconds):
    """Длительность словами: «1 час 30 минут» (без префикса и точки)."""
    seconds = int(seconds)
    if seconds <= 0:
        raise BindingError("duration: длительность должна быть положительной")
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(_units(hours, ("часов", "часа", "час")))
    if minutes:
        parts.append(_units(minutes, ("минут", "минуты", "минуту")))
    if sec or not parts:
        parts.append(_units(sec, ("секунд", "секунды", "секунду")))
    return " ".join(parts)


# --- фильтры шаблонов : закрытый реестр форматирования --------------------

def _filter_plural(value, args, ctx):
    """{{slots.count|plural("минуту","минуты","минут")}} → «5 минут» (формы: 1, 2-4, 5-0)."""
    forms = [form.strip() for form in (args or "").split(",")]
    if len(forms) != 3:
        raise BindingError("plural: три формы через запятую (для 1, для 2-4, для 5-0)")
    return _units(value, (forms[2], forms[1], forms[0]))


def _filter_time_ru(value, args, ctx):
    """{{clock|time_ru}} → «12 часов» / «12 часов 5 минут» (ноль минут опускается)."""
    now = value.get("now") if isinstance(value, dict) else value
    if not isinstance(now, datetime.datetime):
        raise BindingError("time_ru: нужен clock-контекст с now или datetime")
    return _time_words(now)


def _filter_hhmm(value, args, ctx):
    """{{slots.time|hhmm}} → «7:30» (убирает ведущий ноль часа; минуты — две цифры,
    как в прежней alarm_confirm_phrase)."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value))
    if not match:
        raise BindingError("hhmm: ожидается время Ч:ММ")
    return f"{int(match.group(1))}:{int(match.group(2)):02d}"


def _filter_duration(value, args, ctx):
    """{{slots.seconds|duration}} → «1 час 30 минут»."""
    return _format_duration(value)


def _filter_signed(value, args, ctx):
    """{{slots.t|signed}} → «+2» / «-5» / «0» (округление до целого)."""
    number = round(float(value))
    return f"{number:+d}" if number else "0"


def _filter_round(value, args, ctx):
    """{{slots.t|round}} → целое; {{slots.t|round(1)}} → один знак."""
    digits = int(args) if args else 0
    number = round(float(value), digits)
    return str(int(number)) if digits == 0 else str(number)


def _filter_lookup(value, args, ctx):
    """{{slots.code|lookup("icons")}} → tables.icons[value] (таблица — данные сценария).
    {{h.code|lookup("icons", "h.is_day")}} — двухуровневая таблица: сначала ключ по
    пути h.is_day, затем value ("*" — значение по умолчанию на любом уровне)."""
    name, _, sub = (args or "").strip().partition(":")
    table = (ctx.get("tables") or {}).get(name.strip())
    if not isinstance(table, dict):
        raise BindingError(f"lookup: нет таблицы {name!r} в tables сценария")
    if sub:
        outer = str(_lookup(ctx, sub.strip()))
        table = table.get(outer, table.get("*"))
        if not isinstance(table, dict):
            raise BindingError(f"lookup: {outer!r} нет в таблице {name!r}")
    key = str(value)
    if key in table:
        return table[key]
    if "*" in table:
        return table["*"]
    raise BindingError(f"lookup: {key!r} нет в таблице {args!r}")


def _filter_hour_label(value, args, ctx):
    """{{h.time|hour_label}}: ISO «2026-10-27T15:00» → «15:00»; целое n → час
    (clock.hour + n) % 24 в виде «ЧЧ:00» (заглушки без даты)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        base = (ctx.get("clock") or {}).get("hour")
        if base is None:
            raise BindingError("hour_label: нет clock.hour в контексте")
        return f"{(int(base) + int(value)) % 24:02d}:00"
    match = re.search(r"T?(\d{2}):(\d{2})", str(value))
    if not match:
        raise BindingError(f"hour_label: {value!r} — не время")
    return f"{match.group(1)}:{match.group(2)}"


def _filter_div(value, args, ctx):
    """{{slots.level|div(10)}} → целая часть value / N (шкалы: 50 % → 5 из 10)."""
    return str(int(float(value) // float(args or 1)))


def _filter_mul(value, args, ctx):
    """{{params.effective.weather.max_age_hours|mul(3600)}} → часы в секунды."""
    result = float(value) * float(args or 1)
    return int(result) if result.is_integer() else result


def _filter_default(value, args, ctx):
    return args if value in (None, "") else value


def _filter_lower(value, args, ctx):
    return str(value).lower()


def _filter_json(value, args, ctx):
    import json
    return json.dumps(value, ensure_ascii=False)


FILTERS = {"plural": _filter_plural, "time_ru": _filter_time_ru,
           "hhmm": _filter_hhmm, "duration": _filter_duration,
           "signed": _filter_signed, "round": _filter_round, "lookup": _filter_lookup,
           "default": _filter_default, "lower": _filter_lower, "json": _filter_json,
           "hour_label": _filter_hour_label, "div": _filter_div, "mul": _filter_mul}

# --- шаблоны : Jinja2-подмножество с закрытым реестром фильтров -------------
# Разбор и раскрытие — ImmutableSandboxedEnvironment (доступа к атрибутам Python
# у автора сценария нет), ошибки Undefined/Syntax/Security → BindingError.

_ARG_JOINERS = {"plural": ",", "lookup": ":"}


class _PathEnvironment(ImmutableSandboxedEnvironment):
    """Точка в пути — только ключ словаря: getattr словаря берёт значение по
    ключу, а не атрибут Python (items/keys/values/get — ключи контекста, не
    методы dict); у прочих объектов атрибут не читается — undefined, раскрытие
    даёт BindingError."""

    def getattr(self, obj, attribute):
        if isinstance(obj, dict) and attribute in obj:
            return obj[attribute]
        return self.undefined(obj=obj, name=attribute)


_ENV = _PathEnvironment(undefined=StrictUndefined)


def _registry_filter(name, fn, joiner):
    """Обёртка фильтра реестра: аргументы вызова |имя("a","b") собираются в
    строку-аргумент fn(value, args, ctx) — реестр остаётся закрытым."""
    @pass_context
    def run(ctx, value, *args, **kwargs):
        if kwargs:
            raise BindingError(_NO_FILTER_KWARGS)  # вторая линия защиты у статической
        if not joiner and len(args) > 1:
            raise BindingError("фильтр принимает один аргумент")
        if joiner and args:
            raw = joiner.join(args)
        elif args:
            raw = args[0]
        else:
            raw = ""
        return fn(value, raw, ctx)
    run.__name__ = name
    return run


for _name, _fn in FILTERS.items():
    _ENV.filters[_name] = _registry_filter(_name, _fn, _ARG_JOINERS.get(_name))

_ONLY_SUBSTITUTIONS = "в шаблоне допускаются только подстановки {{ }}"
_NO_FILTER_KWARGS = "именованные аргументы фильтра не входят в подмножество шаблонов"
_NO_FILTER_CHAINS = "цепочки фильтров не поддерживаются — один фильтр на подстановку"
_NOT_A_PATH = ("в подстановке — путь контекста и не больше одного фильтра реестра "
               "с позиционными литеральными аргументами")


def _path_text(root, parts):
    return root + "".join(f".{part}" for part in parts)


@functools.lru_cache(maxsize=4096)
def _parsed(source):
    """Кеш по строке исходника: запрет операторов/комментариев, дерево разбора,
    выражение целиком-шаблона и цепочки путей — всё статическое, один раз на
    вариант шаблона. Ошибки не кешируются (lru_cache хранит только успешные)."""
    if "{#" in source or "{%" in source:
        raise BindingError(_ONLY_SUBSTITUTIONS)
    tree = _ENV.parse(source)
    _only_substitutions(tree)
    return source, tree, _whole_inner(source), _static_chains(tree)


def _whole_inner(fixed):
    """Выражение целиком-шаблона (ровно один {{…}} без текста вокруг) или None.
    Скобки внутри строковых литералов вызова фильтра не считаются скобками
    ({{lst|default("}}")}} — целиком-шаблон, тип сохраняется)."""
    stripped = fixed.strip()
    if len(stripped) <= 4 or not (stripped.startswith("{{") and stripped.endswith("}}")):
        return None
    inner = stripped[2:-2]
    quote = None
    index = 0
    while index < len(inner) - 1:
        ch = inner[index]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif (ch == "{" and inner[index + 1] == "{") or (ch == "}" and inner[index + 1] == "}"):
            return None
        index += 1
    return inner.strip()


def _static_chains(tree):
    """Строгий проход дерева подмножества: подстановка — путь контекста
    (корень + части) и не больше одного фильтра реестра с позиционными
    литеральными аргументами. Прочие узлы (арифметика, сравнения, CondExpr,
    вызовы, вычисляемые индексы, литералы-значения) — BindingError.
    Возвращает цепочки путей (корень, части) для проверки контекста."""
    chains = []
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, (nodes.Template, nodes.Output, nodes.TemplateData)):
            stack.extend(node.iter_child_nodes())
        elif isinstance(node, nodes.Filter):
            if node.name not in FILTERS:
                raise BindingError(f"template: неизвестный фильтр {node.name}")
            if node.kwargs or node.dyn_args or node.dyn_kwargs:
                raise BindingError(_NO_FILTER_KWARGS)
            if isinstance(node.node, nodes.Filter):
                raise BindingError(_NO_FILTER_CHAINS)
            if any(not isinstance(arg, nodes.Const) for arg in node.args):
                raise BindingError(_NOT_A_PATH)
            stack.append(node.node)  # значение фильтра — путь; аргументы уже проверены
        elif isinstance(node, (nodes.Getattr, nodes.Getitem)):
            chains.append(_collect_chain(node))
        elif isinstance(node, nodes.Name):
            if node.ctx != "load":
                raise BindingError(_NOT_A_PATH)
            chains.append((node.name, ()))
        else:
            raise BindingError(_NOT_A_PATH)
    return tuple(chains)


def _collect_chain(node):
    """Цепочка Getattr/Getitem → (корень, части). Вычисляемый индекс и литерал
    с атрибутами ({{ ''.__class__ }}) — вне подмножества: песочница
    compile_expression литералы пропускает (undefined_to_none), отвергаем
    статически."""
    parts, current = [], node
    while isinstance(current, (nodes.Getattr, nodes.Getitem)):
        if isinstance(current, nodes.Getattr):
            parts.append(current.attr)
        elif isinstance(current.arg, nodes.Const):
            parts.append(current.arg.value)
        elif isinstance(current.arg, nodes.Neg) and isinstance(current.arg.node, nodes.Const) \
                and isinstance(current.arg.node.value, int):
            parts.append(-current.arg.node.value)
        else:
            raise BindingError(_NOT_A_PATH)
        current = current.node
    if not isinstance(current, nodes.Name):
        raise BindingError("template: доступ к атрибутам литерала запрещён")
    parts.reverse()
    return current.name, tuple(parts)


def _check_chains(chains, ctx):
    """Пути в контексте (chains из кеша): корень в ctx, dict по ключу,
    список по индексу (в т. ч. отрицательном); текст ошибки — полный путь."""
    for root, parts in chains:
        if root not in ctx:
            raise BindingError(f"template: неизвестный корень {root!r}")
        value = ctx[root]
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, (list, tuple)) and isinstance(part, int) \
                    and -len(value) <= part < len(value):
                value = value[part]
            else:
                raise BindingError(f"template: {_path_text(root, parts)} отсутствует в контексте")


@functools.lru_cache(maxsize=4096)
def _whole_expression(inner):
    """Скомпилированное выражение целиком-шаблона. undefined_to_none=False:
    отсутствующее значение не подменяется None — попадает в проверку Undefined."""
    return _ENV.compile_expression(inner, undefined_to_none=False)


@functools.lru_cache(maxsize=4096)
def _mixed_template(fixed):
    """Скомпилированный шаблон смешанного текста."""
    return _ENV.from_string(fixed)


def check_template(source):
    """Синтаксическая проверка шаблона (валидатор, без контекста запроса):
    BindingError на ошибку разбора или узел вне подмножества."""
    try:
        _parsed(str(source or ""))
    except TemplateSyntaxError as exc:
        raise BindingError(f"template: {exc.message}") from None


def _only_substitutions(tree):
    """Операторы и комментарии Jinja2 запрещены (правка через дерево — проверка
    «{#»/«{%» выше): в теле только Output и TemplateData."""
    for node in tree.body:
        if not isinstance(node, (nodes.Output, nodes.TemplateData)):
            raise BindingError(_ONLY_SUBSTITUTIONS)


def expand(value, ctx):
    """Раскрытие значения сценария: binding-словарь → evaluate; строка с {{…}} →
    шаблон (строка целиком из одного токена — значение без приведения к строке,
    compile_expression того же окружения); словари/списки — рекурсивно;
    $json — сериализация поддерева в строку JSON."""
    if isinstance(value, dict):
        if "source" in value and value.get("source") in SOURCES:
            return evaluate(value, ctx)
        if "$each" in value:
            # повтор по данным: {"$each": "slots.hours", "as": "h", "item": {...}}
            # columns: true — источник колоночный ({поле: [..]}, как hourly Open-Meteo):
            # строки собираются по индексу. first/last — шаблоны крайних элементов.
            items = _lookup(ctx, str(value["$each"]))
            if value.get("columns") and isinstance(items, dict):
                width = min((len(v) for v in items.values() if isinstance(v, list)), default=0)
                items = [{key: column[index] for key, column in items.items() if isinstance(column, list)}
                         for index in range(width)]
            if not isinstance(items, (list, tuple)):
                raise BindingError(f"$each: {value['$each']} — не список")
            alias = str(value.get("as") or "item")
            limit = value.get("limit")
            items = list(items)[:int(limit)] if limit is not None else list(items)
            result = []
            for index, item in enumerate(items):
                template = value.get("item")
                if index == 0 and "first" in value:
                    template = value["first"]
                elif index == len(items) - 1 and "last" in value:
                    template = value["last"]
                result.append(expand(template, {**ctx, alias: item, alias + "_index": index}))
            return result
        if set(value) == {"$json"}:
            import json
            return json.dumps(expand(value["$json"], ctx), ensure_ascii=False, separators=(",", ":"))
        return {key: expand(item, ctx) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, ctx) for item in value]
    if isinstance(value, str) and ("{{" in value or "{%" in value or "{#" in value):
        return _expand_template(value, ctx)
    return value


def _lookup(ctx, path):
    """Точечный путь по контексту: словари по ключу, списки по индексу (a.0.b)."""
    parts = path.strip().split(".")
    if parts[0] not in ctx:
        raise BindingError(f"template: неизвестный корень {parts[0]!r}")
    node = ctx[parts[0]]
    for part in parts[1:]:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, (list, tuple)) and part.lstrip("-").isdigit() and -len(node) <= int(part) < len(node):
            node = node[int(part)]
        else:
            raise BindingError(f"template: {path} отсутствует в контексте")
    return node


def _expand_template(value, ctx):
    if not isinstance(value, str):
        raise BindingError("template: value должен быть строкой")
    try:
        source, _, whole, chains = _parsed(value)
    except TemplateSyntaxError as exc:
        raise BindingError(f"template: {exc.message}") from None
    _check_chains(chains, ctx)
    if whole is not None:
        try:
            result = _whole_expression(whole)(ctx)
        except BindingError:
            raise
        except (TemplateSyntaxError, TemplateAssertionError, UndefinedError, SecurityError,
                TypeError) as exc:
            raise BindingError(f"template: {exc.message}") from None
        if isinstance(result, Undefined):  # вторая линия защиты рядом со статической
            raise BindingError("template: значение отсутствует в контексте")
        return result
    try:
        return _mixed_template(source).render(ctx)
    except BindingError:
        raise
    except (TemplateSyntaxError, TemplateAssertionError, UndefinedError, SecurityError,
            TypeError) as exc:
        raise BindingError(f"template: {exc.message}") from None


# Закрытый реестр: расширение — только правкой кода с тестами, не данными пакета.
LIBRARY = {
    "slot_value": lambda args, ctx: _slot_or_state(ctx, _arg(args, ctx, "name")),
    # Голый шаблон музыки («включи {query}») жадный — guard сценария media.music
    # отклоняет не-музыкальные домены (токены по триггерам device/smart_home/timers/
    # system.unavailable и корпусу фраз). Возвращает найденный токен или "".
    "media_other_domain": lambda args, ctx: _media_other_domain(
        (ctx.get("input") or {}).get("text") or ""),
    # Таймзона: args.name или input.timezone запроса (имя из Application.Timezone).
    "timezone_offset_seconds": lambda args, ctx: _tz_offset_seconds(
        (args or {}).get("name") or (ctx.get("input") or {}).get("timezone")),
}


# Не-музыкальные домены для голых фраз музыки (см. LIBRARY media_other_domain).
# Слова сравниваются целиком («светлану» — не «свет»; «шоушенка» — не «шоу»);
# формы слов перечислены явно. «Шоу» — только в составе «утреннее/вечернее шоу»:
# «включи шоу маст гоу он» — песня.
MEDIA_OTHER_DOMAINS = ("таймер", "таймеры", "будильник", "будильники", "свет", "света",
                       "люстра", "люстры", "люстру", "торшер", "торшера", "торшеру",
                       "гирлянда", "гирлянды", "гирлянду", "блютуз", "bluetooth",
                       "громкость", "караоке", "метроном", "радионяня", "радионяню",
                       "субтитры", "субтитру", "сериал", "сериала", "сериалу", "сериале",
                       "серию", "фильм", "фильма", "фильму", "кино", "комедия", "комедии",
                       "мультфильм", "мультфильма", "канал", "канала", "канале", "клиника",
                       "клинику", "клиники", "лавка", "лавку", "утреннее шоу", "вечернее шоу",
                       "кинокомпанию", "розетка", "розетки", "розетку", "чайник", "увлажнитель",
                       "увлажнителя", "кондиционер", "обогреватель", "обогрева", "датчик",
                       "датчики", "замок", "замка", "шторы", "штора", "ворота", "пылесос",
                       "приложение", "приложения", "сценарий", "сценарии", "напоминание",
                       "напоминания", "встреча", "встречи", "встречу", "календарь",
                       "календаре", "календаря", "погода", "погоду", "волшебный светофор",
                       "пауза", "паузу", "дуо 32", "хоккей", "футбол", "спорт", "матч",
                       "матча", "трансляция", "трансляции", "трансляцию", "клип в сети",
                       "новость", "новости", "новостей", "новостях", "новостями")
# Маркеры явного музыкального запроса («включи группу кино») — фраза уходит музыке.
MEDIA_MUSIC_MARKERS = ("группу", "песню", "трек", "альбом", "исполнителя")


def _media_other_domain(text):
    """Не-музыкальный домен в реплике (первый совпавший токен) или пусто."""
    words = re.sub(r"[^\w\s]", " ", str(text or "").lower().replace("ё", "е")).split()
    if any(marker in words for marker in MEDIA_MUSIC_MARKERS):
        return ""
    for token in MEDIA_OTHER_DOMAINS:
        if re.search(r"(?<!\w)" + r"\s+".join(map(re.escape, token.split())) + r"(?!\w)",
                     " ".join(words)):
            return token
    return ""


def _now(ctx):
    now = ctx.get("clock", {}).get("now")
    if not isinstance(now, datetime.datetime):
        raise BindingError("computed/clock: в контексте нет фиксированных часов (clock.now)")
    return now


def _arg(args, ctx, name):
    if name in (args or {}):
        return args[name]
    value = ctx.get("slots", {}).get(name)
    if value is None:
        raise BindingError(f"computed: нет аргумента {name} ни в args, ни в slots")
    return value


def _slot_or_state(ctx, name):
    """Значение из слотов, при отсутствии — из state (set-шаги пишут в state)."""
    if name in ctx.get("slots", {}):
        return str(ctx["slots"][name])
    if name in ctx.get("state", {}):
        return str(ctx["state"][name])
    raise BindingError(f"slot_value: нет слота/состояния {name}")


def _tz_offset_seconds(name):
    """Текущее смещение таймзоны в секундах (для директивы set_timezone
    служебного колбэка синхронизации времени)."""
    tz_name = str(name or '').strip()
    if not tz_name:
        raise BindingError("timezone_offset_seconds: нет имени таймзоны")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return int(datetime.datetime.now(ZoneInfo(tz_name)).utcoffset().total_seconds())
    except ZoneInfoNotFoundError as exc:
        raise BindingError(f"timezone_offset_seconds: неизвестная таймзона {tz_name}") from exc


def evaluate(binding, ctx):
    """Вычисляет значение binding по контексту {input, slots, clock, state, media}."""
    if not isinstance(binding, dict) or "source" not in binding:
        raise BindingError("binding должен быть объектом с source")
    source = binding["source"]
    if source not in SOURCES:
        raise BindingError(f"неизвестный source: {source}")
    if source == "constant":
        return binding.get("value")
    if source == "input":
        key = binding.get("field")
        if key not in ctx.get("input", {}):
            raise BindingError(f"input.{key} отсутствует в контексте запроса")
        return ctx["input"][key]
    if source == "generated":
        kind = binding.get("kind", "uuid4_hex")
        if kind not in GENERATED_KINDS:
            raise BindingError(f"неизвестный kind генератора: {kind}")
        return GENERATED_KINDS[kind]()
    if source == "slot":
        name = binding.get("name")
        if name not in ctx.get("slots", {}):
            raise BindingError(f"slot {name} не извлечён")
        return ctx["slots"][name]
    if source == "clock":
        op = binding.get("op")
        if op not in CLOCK_OPS:
            raise BindingError(f"неизвестная clock-операция: {op}")
        now = _now(ctx)
        if op == "now_epoch":
            return int(now.timestamp())
        if op == "now_epoch_ms":
            return int(now.timestamp() * 1000)
        return now.isoformat()
    if source == "computed":
        fn = binding.get("fn")
        if fn not in LIBRARY:
            raise BindingError(f"функция {fn} не входит в закрытый реестр библиотеки")
        return LIBRARY[fn](expand(binding.get("args"), ctx), ctx)
    if source == "template":
        return _expand_template(binding.get("value"), ctx)
    if source == "path":
        try:
            return _lookup(ctx, str(binding.get("path") or ""))
        except BindingError:
            if "default" in binding:
                return binding["default"]
            raise
    if source == "state":
        key = binding.get("key")
        if key not in ctx.get("state", {}):
            raise BindingError(f"state.{key} отсутствует")
        return ctx["state"][key]
    if source in ("user", "device"):
        key = binding.get("field")
        node = ctx.get(source, {})
        if key is None:
            return node  # весь контекст (гео, имя, комната) — сценарий решает сам
        for part in str(key).split("."):  # точечный путь: fields.city, location.latitude
            if not isinstance(node, dict) or part not in node:
                raise BindingError(f"{source}.{key} отсутствует в контексте запроса")
            node = node[part]
        return node
    if source == "animation":
        # URL анимации из каталога (ctx.animations: id → url) — для frontal_led_image
        name = binding.get("name")
        animations = ctx.get("animations") or {}
        if name not in animations:
            raise BindingError(f"animation.{name} нет в каталоге")
        return animations[name]
    raise BindingError(source)  # unreachable
