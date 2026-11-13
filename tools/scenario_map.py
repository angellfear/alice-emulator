"""Карта сценариев docs/scenario-map.svg и docs/scenario-map/<группа>.svg: python tools/scenario_map.py.

Читает реальные сценарии: встроенную библиотеку (scenario_engine.library), сценарии и справочники
фраз встроенных плагинов (plugins/*), исходники .py (разбор через ast — ответы, уточнения, уступки,
директивы, обращения к поставщикам). Ничего не выдумывает: нет в коде — нет на карте.
Обзор — порядок выбора сценария, поставщики ответа, группы со ссылками; страница группы —
фразы, ветвления диалога и итоговые ответы каждого сценария."""
from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape as esc

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server_emulator import intelligence  # noqa: E402
from server_emulator.scenario_engine import library  # noqa: E402
from server_emulator.scenario_engine.router import trigger_phrases  # noqa: E402
from server_emulator.scenario_packages.validate import PackageError, validate_scenario  # noqa: E402

PKG = ROOT / "server_emulator"
PLUGINS = PKG / "plugins"
DOCS = ROOT / "docs"
OVERVIEW = "scenario-map.svg"
GROUP_DIR = "scenario-map"

SANS = "Segoe UI, IBM Plex Sans, Arial, sans-serif"
MONO = "Consolas, IBM Plex Mono, Menlo, monospace"

# роль цвета: (светлая обводка, светлая заливка, тёмная обводка, тёмная заливка)
ROLES = {
    "blue": ("#2f6fd0", "#e8effb", "#7aa7f0", "#1b2a45"),
    "green": ("#16855a", "#e5f4ec", "#4fc08a", "#16342a"),
    "orange": ("#b2650b", "#fbf1e2", "#e0a050", "#3a2a14"),
    "violet": ("#6a45c9", "#f0ebfb", "#a78bec", "#2a2147"),
    "pink": ("#b8326b", "#fbeaf1", "#e87aa5", "#3d1c2c"),
    "red": ("#c0392b", "#fdecea", "#f0857a", "#42201c"),
    "gray": ("#4a5566", "#eef1f4", "#8b97a8", "#252b34"),
}
GROUP_ROLE = {"system": "gray", "informers": "blue", "device": "green", "smart_home": "violet",
              "timers": "orange", "custom": "gray", "media": "pink", "chat": "blue",
              "games": "green", "knowledge": "violet", "lists": "orange"}

CSS = """
:root{--bg:#ffffff;--fg:#18202b;--muted:#5b6676;--rule:#d5dbe3;--edge:#4a5566;--ask:#b8326b;--decl:#c0392b;--link:#2f6fd0}
@media (prefers-color-scheme: dark){:root{--bg:#10151c;--fg:#e6ebf2;--muted:#9aa6b6;--rule:#2c3644;--edge:#9aa6b6;--ask:#e87aa5;--decl:#f0857a;--link:#7aa7f0}}
.bg{fill:var(--bg)}
.t{fill:var(--fg);font-family:%(sans)s}.m{fill:var(--muted);font-family:%(sans)s}.mo{fill:var(--fg);font-family:%(mono)s}
.b{font-weight:700}.cap{font-weight:700;letter-spacing:1.4px}
.rule{stroke:var(--rule);fill:none}.lk{fill:var(--link);font-family:%(sans)s}
.edge{stroke:var(--edge);fill:none;stroke-width:1.4}.ask{stroke:var(--ask);fill:none;stroke-width:1.4;stroke-dasharray:5 3}
.decl{stroke:var(--decl);fill:none;stroke-width:1.4;stroke-dasharray:2 3}
.mk-edge{fill:var(--edge)}.mk-ask{fill:var(--ask)}.mk-decl{fill:var(--decl)}
.lb-decl{fill:var(--decl);font-weight:700}.lb-ask{fill:var(--ask);font-weight:700}.lb-act{fill:#6a45c9;font-weight:700}
.lb-data{fill:var(--muted);font-weight:700}.lb-say{fill:#16855a;font-weight:700}.lb-dir{fill:#b2650b;font-weight:700}
@media (prefers-color-scheme: dark){.lb-act{fill:#a78bec}.lb-say{fill:#4fc08a}.lb-dir{fill:#e0a050}}
.box{stroke:var(--s);fill:var(--f);stroke-width:1.5}.dash{stroke-dasharray:4 4}
""" % {"sans": SANS, "mono": MONO}
for _name, (_s, _f, _ds, _df) in ROLES.items():
    CSS += f".k-{_name}{{--s:{_s};--f:{_f}}}\n"
CSS += "@media (prefers-color-scheme: dark){" + "".join(
    f".k-{n}{{--s:{ds};--f:{df}}}" for n, (_s, _f, ds, df) in ROLES.items()) + "}\n"

# --- метрики текста: приближение без шрифтов, чтобы раскладка не зависела от машины ---------------
SANS_W, BOLD_W, MONO_W = 0.55, 0.6, 0.6


def text_w(text, size, mono=False, bold=False):
    return len(text) * size * (MONO_W if mono else (BOLD_W if bold else SANS_W))


def wrap(text, width, size, mono=False, bold=False):
    """Строка → список строк по ширине в пикселях (переносы по пробелам, длинные слова режутся)."""
    limit = max(4, int(width / (size * (MONO_W if mono else (BOLD_W if bold else SANS_W)))))
    lines, current = [], ""
    for word in str(text).split():
        while len(word) > limit:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:limit])
            word = word[limit:]
        candidate = word if not current else current + " " + word
        if len(candidate) <= limit:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def clip(text, width, size, mono=False):
    limit = max(4, int(width / (size * (MONO_W if mono else SANS_W))))
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def limit_lines(lines, count):
    if len(lines) <= count:
        return lines
    kept = lines[:count]
    kept[-1] = kept[-1].rstrip(" .,;") + "…"
    return kept


# --- шаблоны hassil: примеры и число вариантов ----------------------------------------------------
class Template:
    """Мини-разбор синтаксиса hassil: (а|б), [необяз.], (а; б) — перестановка, {список}.
    Нужен карте, чтобы показать примеры фраз; сам матчинг сценариев делает hassil."""

    CAP = 400

    def __init__(self, source, lists):
        self.lists, self.tokens, self.pos = lists, re.findall(r"[()\[\]|;]|\{[^}]*\}|<[^>]*>|[^\s()\[\]|;{}<>]+", source), 0
        self.tree = self._alt(())

    def _alt(self, stops):
        branches, current = [], []
        while self.pos < len(self.tokens):
            token = self.tokens[self.pos]
            if token in stops:
                break
            if token == "|":
                branches.append(current)
                current = []
                self.pos += 1
            elif token == ";":
                current.append(("perm_sep",))
                self.pos += 1
            elif token in "([":
                self.pos += 1
                inner = self._alt((")",) if token == "(" else ("]",))
                self.pos += 1
                current.append(("opt", inner) if token == "[" else ("grp", inner))
            elif token.startswith("{"):
                current.append(("list", token[1:-1]))
                self.pos += 1
            elif token.startswith("<"):
                current.append(("word", ""))
                self.pos += 1
            else:
                current.append(("word", token))
                self.pos += 1
        branches.append(current)
        return branches

    def _values(self, name):
        item = self.lists.get(name) or {}
        if item.get("wildcard"):
            return ["…"]
        if "values" in item:
            return [str(v.get("in")) if isinstance(v, dict) else str(v) for v in item["values"]]
        if "range" in item:
            return [str(n) for n in range(int(item["range"]["from"]), min(int(item["range"]["to"]), int(item["range"]["from"]) + 9) + 1)]
        return ["…"]

    def _expand(self, branches):
        out = []
        for seq in branches:
            variants = [""]
            parts = [[]]
            for element in seq:
                if element == ("perm_sep",):
                    parts.append([])
                else:
                    parts[-1].append(element)
            for part in parts:
                for element in part:
                    kind = element[0]
                    if kind == "word":
                        options = [element[1]]
                    elif kind == "list":
                        options = self._values(element[1])
                    elif kind == "opt":
                        options = [""] + self._expand(element[1])
                    else:
                        options = self._expand(element[1])
                    variants = [(a + " " + b).strip() for a in variants for b in options][:self.CAP]
            out.extend(variants)
            if len(out) >= self.CAP:
                break
        return out[:self.CAP]

    def count(self):
        def walk(branches):
            total = 0
            for seq in branches:
                n = 1
                for element in seq:
                    kind = element[0]
                    if kind == "list":
                        n *= len(self._values(element[1]))
                    elif kind == "opt":
                        n *= 1 + walk(element[1])
                    elif kind == "grp":
                        n *= walk(element[1])
                total += n
            return total
        return walk(self.tree)

    def examples(self, k=3):
        found = [re.sub(r"\s+", " ", item.replace("?", " ")).strip() for item in self._expand(self.tree)]
        found = [item for item in dict.fromkeys(found) if item]
        if len(found) <= k:
            return found
        step = max(1, len(found) // k)
        return found[::step][:k]


def template_info(sentence, lists):
    try:
        tpl = Template(sentence, lists)
        return tpl.count(), tpl.examples(2)
    except Exception:  # noqa: BLE001 — шаблон, который карта не разобрала, показывается как есть
        return 1, []


# --- модель ----------------------------------------------------------------------------------------
@dataclass
class Step:
    name: str
    title: str
    doc: str = ""
    kind: str = "call"  # entry | call | answer | flow
    says: list = field(default_factory=list)
    directives: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    uses: list = field(default_factory=list)
    asks: list = field(default_factory=list)  # (вопрос, имя функции продолжения)
    declines: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    card: bool = False
    children: list = field(default_factory=list)  # (Step, "call"|"ask"|"fail")
    also: list = field(default_factory=list)


@dataclass
class Scenario:
    id: str
    title: str
    category: str
    origin: str  # builtin | extension:<id>
    path: str
    priority: int
    fallback: bool
    description: str
    kind: str  # python | flow
    phrases: list
    templates: list  # (шаблон, число вариантов, примеры)
    provides: list
    events: list
    entries: list  # записи справочников фраз плагинов
    guard: str
    root: Step | None
    phrase_total: int = 0
    variant_total: int = 0


ATTR_LABEL = {"music": "поставщик музыки", "player": "плеер колонки", "timers": "таймеры колонки",
              "alarms": "будильники колонки", "screens": "экраны колонки", "volume": "громкость колонки",
              "route_mode": "режим маршрута", "intelligence": "режим «Интеллект»", "data": "память сценария",
              "phrase_entry": "запись справочника фраз", "device": "устройство запроса", "user": "владелец",
              "params": "параметры пользователя", "settings": "настройки"}
MODULE_LABEL = {"music": "поставщик музыки (music.py)", "providers": "реестр поставщиков",
                "station_state": "состояние колонки", "behavior": "стандартные ответы", "clock_directives": "",
                "media_directives": "", "app_directives": "", "web_tools": "веб-инструменты"}
DIRECTIVE_RE = re.compile(r"^[a-z][a-z0-9_@.]+$")
FIRST_SENTENCE = re.compile(r"^(.+?[.!?])(\s|$)", re.S)


def first_sentence(text, limit=230):
    text = " ".join(str(text or "").split())
    match = FIRST_SENTENCE.match(text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[:limit - 1].rstrip() + "…"


def _module_constants(tree):
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                consts[node.targets[0].id] = [value.value]
            elif isinstance(value, ast.Dict):
                values = [v.value for v in value.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                if values:
                    consts[node.targets[0].id] = values
    return consts


def _texts(node, consts):
    """Строки, которые может произнести выражение: константа, f-строка (динамика → «…»),
    константа модуля, значения словаря по ключу, обе ветки условия."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts = [p.value if isinstance(p, ast.Constant) else "…" for p in node.values]
        return ["".join(parts)]
    if isinstance(node, ast.Name):
        return list(consts.get(node.id, []))
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        return list(consts.get(node.value.id, []))
    if isinstance(node, ast.IfExp):
        return _texts(node.body, consts) + _texts(node.orelse, consts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _texts(node.left, consts), _texts(node.right, consts)
        return [a + b for a in left[:1] for b in (right[:1] or ["…"])] if left else []
    return []


def _const(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _dict_directives(node):
    names = []
    for item in ast.walk(node):
        if isinstance(item, ast.Dict):
            for key, value in zip(item.keys, item.values):
                if _const(key) == "name" and _const(value) and DIRECTIVE_RE.match(_const(value)):
                    names.append(_const(value))
    return names


class ModuleIndex:
    """Разбор модуля ядра, на который ссылается сценарий (music.py, clock_directives.py…):
    какие директивы собирает функция (транзитивно по вызовам внутри модуля)."""
    cache: dict = {}

    @classmethod
    def directives(cls, module, function):
        path = PKG / f"{module}.py"
        if not path.is_file():
            return []
        if module not in cls.cache:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            cls.cache[module] = funcs
        funcs = cls.cache[module]
        seen, found, queue = set(), [], [function]
        while queue:
            name = queue.pop()
            if name in seen or name not in funcs:
                continue
            seen.add(name)
            found += _dict_directives(funcs[name])
            for call in ast.walk(funcs[name]):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    queue.append(call.func.id)
        return list(dict.fromkeys(found))


def analyze_python(path):
    """(корневой шаг, число шагов): дерево шагов handle → вызовы и продолжения после вопросов."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    consts = _module_constants(tree)
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    modules = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module in (None, "server_emulator") or (node.level and node.module is None)):
            for alias in node.names:
                modules[alias.asname or alias.name] = alias.name
    steps = {}
    for name, fn in funcs.items():
        step = Step(name=name, title=name, doc=first_sentence(ast.get_docstring(fn) or ""))
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                callee = node.func
                if isinstance(callee, ast.Name):
                    if callee.id in ("say", "card") and node.args:
                        step.says += [t for t in _texts(node.args[0], consts) if t.strip()]
                        step.card = step.card or callee.id == "card"
                    elif callee.id == "directive" and node.args and _const(node.args[0]):
                        step.directives.append(_const(node.args[0]))
                    elif callee.id == "ask" and node.args:
                        question = (_texts(node.args[0], consts) or ["…"])[0]
                        then = next((_const(k.value) for k in node.keywords if k.arg == "then"), None) \
                            or (_const(node.args[1]) if len(node.args) > 1 else None)
                        step.asks.append((question, then or ""))
                    elif callee.id in funcs and callee.id != name:
                        step.calls.append(callee.id)
                elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
                    owner, attr = callee.value.id, callee.attr
                    if owner == "ctx":
                        arg0 = _const(node.args[0]) if node.args else None
                        if attr == "action":
                            detail = next((_const(k.value) for k in node.keywords if k.arg == "action"), None)
                            step.actions.append((arg0 or "…") + (f" · {detail}" if detail else ""))
                        elif attr == "provider":
                            step.uses.append(f"поставщик «{arg0 or '…'}»")
                        elif attr in ("dataset", "dataset_record"):
                            step.uses.append(f"датасет {arg0 or '…'}")
                        elif attr == "no_data_source":
                            step.uses.append("нет поставщика: стандартный ответ «нет источника данных»")
                        elif attr == "fetch_json":
                            step.uses.append("HTTP-запрос данных")
                        elif attr == "send_push":
                            step.actions.append("push колонке")
                        elif attr == "send_directive" and len(node.args) > 1 and _const(node.args[1]):
                            step.directives.append(_const(node.args[1]))
                    elif owner in modules:
                        label = MODULE_LABEL.get(owner, owner)
                        names = ModuleIndex.directives(owner, attr)
                        step.directives += names
                        if label and not names:
                            step.uses.append(f"{label}: {attr}")
                        elif label and attr not in ("start_reply", "step_reply"):
                            step.uses.append(f"{label}: {attr}")
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ctx" \
                    and node.attr in ATTR_LABEL:
                step.uses.append(ATTR_LABEL[node.attr])
            elif isinstance(node, ast.Return) and (node.value is None or (isinstance(node.value, ast.Constant) and node.value.value is None)):
                comment = lines[node.lineno - 1].partition("#")[2].strip() if "#" in lines[node.lineno - 1] else ""
                step.declines.append(comment or "отказ (return None)")
            elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
                step.errors += _texts(node.exc.args[0], consts)
        step.directives = list(dict.fromkeys(step.directives + _dict_directives(fn)))
        step.says = list(dict.fromkeys(step.says))
        step.uses = list(dict.fromkeys(step.uses))
        step.actions = list(dict.fromkeys(step.actions))
        step.declines = list(dict.fromkeys(step.declines))
        step.calls = list(dict.fromkeys(step.calls))
        steps[name] = step
    root = steps.get("handle") or next(iter(steps.values()), None)
    if root is None:
        return None
    root.kind = "entry"
    root.title = "вход: handle"
    return _tree(root, steps)


def _significant(step):
    return bool(step.says or step.directives or step.actions or step.uses or step.asks or step.declines
                or step.errors)


def _tree(root, steps):
    """Дерево шагов: вызовы и продолжения после вопроса; «чистые» построители ответа
    (только речь/директивы, без ветвлений) сливаются в вызывающего."""
    placed = {root.name}

    def build(step):
        for callee_name in step.calls:
            callee = steps.get(callee_name)
            if callee is None:
                continue
            if callee_name in placed:
                step.also.append(callee_name)
                continue
            placed.add(callee_name)
            sub = build(callee)
            pure = not (callee.asks or callee.declines or callee.actions or callee.uses or callee.errors or sub.children)
            if pure:
                step.says = list(dict.fromkeys(step.says + callee.says))
                step.directives = list(dict.fromkeys(step.directives + callee.directives))
                step.card = step.card or callee.card
            elif _significant(callee) or sub.children:
                step.children.append((sub, "call"))
        for _question, then in step.asks:
            target = steps.get(then)
            if target is None or then in placed:
                continue
            placed.add(then)
            target.kind = "answer"
            target.title = f"ответ на вопрос: {then}"
            step.children.append((build(target), "ask"))
        return step

    return build(root)


def count_steps(step):
    return 1 + sum(count_steps(child) for child, _ in step.children) if step else 0


def flow_tree(flow):
    """JSON-сценарий: цепочка шагов action → compose …; error — ветка при сбое действия."""
    steps = []
    by_id = {}
    for item in flow:
        op = item.get("op")
        step = Step(name=str(item.get("id")), title=f"шаг {item.get('id')} · {op}", kind="flow")
        if op == "action":
            args = item.get("arguments") or {}
            detail = args.get("action") or args.get("name") or ""
            step.actions.append(f"{item.get('ref')}" + (f" · {detail}" if detail else ""))
        elif op == "compose":
            reply = item.get("reply") or {}
            speech = reply.get("text") if reply.get("text") is not None else reply.get("speech")
            if isinstance(speech, str) and speech.strip():
                step.says.append(speech)
            step.directives += [d.get("name") for d in reply.get("directives") or [] if isinstance(d, dict) and d.get("name")]
            step.card = bool(reply.get("card"))
            if not step.says and not step.directives:
                step.says.append("(тихий ответ без речи)")
        elif op == "ask":
            step.asks.append((str(item.get("question") or "…"), str(item.get("next") or "")))
        elif op == "error":
            step.errors.append(str(item.get("message") or "ошибка"))
        elif op == "resolve":
            step.uses.append(f"резолвер {item.get('ref')}")
        elif op == "finish":
            continue
        by_id[step.name] = (step, item)
        steps.append((step, item))
    if not steps:
        return None
    root = steps[0][0]
    root.kind = "flow"
    parent = root
    for step, item in steps[1:]:
        if step.errors:
            continue
        parent.children.append((step, "call"))
        parent = step
    for step, item in steps:
        fail = by_id.get(item.get("on_failure"))
        if fail and fail[0] is not step:
            step.children.append((fail[0], "fail"))
    return root


# --- сбор данных -----------------------------------------------------------------------------------
def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def plugin_manifests():
    result = []
    for path in sorted(PLUGINS.glob("*/extension.json")):
        data = _read(path)
        data["_dir"] = path.parent
        result.append(data)
    return result


def plugin_phrase_entries(manifests):
    """Записи справочников фраз плагинов: handler core:<сценарий> → кандидат в сценарий."""
    entries = []
    for manifest in manifests:
        contributes = manifest.get("contributes") or {}
        files = []
        if isinstance(contributes.get("phrases"), str):
            files.append((None, contributes["phrases"]))
        for provider in contributes.get("providers") or []:
            if provider.get("phrases"):
                files.append((provider.get("kind"), provider["phrases"]))
        for kind, rel in files:
            path = manifest["_dir"] / rel
            if not path.is_file():
                continue
            data = _read(path)
            for entry in data.get("entries") or []:
                handler = str(entry.get("handler") or "")
                if handler.startswith("core:"):
                    entries.append({"ext": manifest["id"], "kind": kind, "id": entry.get("id"),
                                    "sentences": list(entry.get("sentences") or []),
                                    "lists": entry.get("lists") or {}, "search": bool(entry.get("search")),
                                    "handler": handler[5:]})
    return entries


def _lists_for(trigger, lexicon_lists):
    merged = dict(lexicon_lists)
    for name, value in (trigger.get("lists") or {}).items():
        if isinstance(value, dict):
            merged[name] = value
    return merged


def build_scenario(item, origin, path, entries, lexicon_lists, category_dir):
    phrases, templates, provides, events = [], [], [], []
    for trigger in item.get("triggers") or []:
        kind = trigger.get("kind")
        if kind == "phrase":
            phrases += trigger_phrases(trigger)
        elif kind == "template":
            lists = _lists_for(trigger, lexicon_lists)
            for sentence in trigger.get("sentences") or []:
                count, examples = template_info(sentence, lists)
                templates.append((sentence, count, examples))
        elif kind == "provide":
            provides.append(str(trigger.get("event")))
        elif kind == "event":
            events.append(str(trigger.get("event") or trigger.get("name") or ""))
    mine = [e for e in entries if e["handler"] == item["id"]]
    for entry in mine:
        lists = _lists_for(entry, lexicon_lists)
        entry["_templates"] = [(s, *template_info(s, lists)) for s in entry["sentences"]]
    script = (item.get("executor") or {}).get("kind") == "script"
    root = None
    if script:
        py = path.with_suffix(".py")
        if py.is_file():
            root = analyze_python(py)
    elif item.get("flow"):
        root = flow_tree(item["flow"])
    title = (item.get("title") or {}).get("ru") or item["id"]
    description = first_sentence(((item.get("description") or {}).get("ru")) or "")
    if not description and root and root.doc:
        description = root.doc
    guard = json.dumps(item["guard"], ensure_ascii=False) if item.get("guard") is not None else ""
    scenario = Scenario(
        id=item["id"], title=title, category=item.get("category") or category_dir, origin=origin,
        path=str(path.relative_to(ROOT)).replace("\\", "/"), priority=int(item.get("priority", 100)),
        fallback=bool(item.get("fallback")), description=description,
        kind="python" if script else "flow", phrases=phrases, templates=templates, provides=provides,
        events=events, entries=mine, guard=guard, root=root)
    scenario.phrase_total = len(phrases) + len(templates) + sum(len(e["sentences"]) for e in mine)
    scenario.variant_total = len(phrases) + sum(c for _s, c, _e in templates) \
        + sum(c for e in mine for _s, c, _x in e["_templates"])
    return scenario


def plugin_scenarios(manifests):
    found = []
    for manifest in manifests:
        base = manifest["_dir"] / "scenarios"
        for path in sorted(base.glob("*/*.json")) if base.is_dir() else []:
            item = _read(path)
            try:
                validate_scenario(item)
            except PackageError:
                continue
            found.append((item, f"extension:{manifest['id']}", path))
    return found


@dataclass
class Data:
    categories: list
    scenarios: list
    manifests: list
    providers: list
    entries: list


def collect():
    manifests = plugin_manifests()
    entries = plugin_phrase_entries(manifests)
    lexicon_lists = (library.LEXICONS.get("lists") or {})
    scenarios = []
    for sid, item in library.SCENARIOS.items():
        path = library.ROOT / item["_file"]
        scenarios.append(build_scenario(item, "builtin", path, entries, lexicon_lists, path.parent.name))
    for item, origin, path in plugin_scenarios(manifests):
        scenarios.append(build_scenario(item, origin, path, entries, lexicon_lists, path.parent.name))
    scenarios.sort(key=lambda s: (s.origin == "builtin", s.fallback, s.priority, s.id))
    providers = []
    for manifest in manifests:
        for provider in (manifest.get("contributes") or {}).get("providers") or []:
            providers.append({"kind": provider.get("kind"), "title": provider.get("title"),
                              "ext": manifest["id"], "name": manifest.get("name") or manifest["id"],
                              "capabilities": provider.get("capabilities") or []})
    return Data(list(library.CATEGORIES), scenarios, manifests, providers, entries)


# --- SVG -------------------------------------------------------------------------------------------
class Svg:
    def __init__(self, width, title, desc):
        self.w, self.title, self.desc, self.parts = width, title, desc, []

    def add(self, markup):
        self.parts.append(markup)

    def rect(self, x, y, w, h, role="gray", rx=8, dashed=False, extra=""):
        self.add(f'<rect class="box k-{role}{" dash" if dashed else ""}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                 f'height="{h:.1f}" rx="{rx}"{extra}/>')

    def text(self, x, y, content, size=12, cls="t", anchor="start", bold=False, mono=False, spacing=None):
        klass = ("mo" if mono else cls) + (" b" if bold else "")
        attrs = f' text-anchor="{anchor}"' if anchor != "start" else ""
        self.add(f'<text class="{klass}" x="{x:.1f}" y="{y:.1f}" font-size="{size}"{attrs}>{esc(str(content))}</text>')

    def labeled(self, x, y, label, label_cls, content, size=11.5, mono=False):
        klass = "mo" if mono else "t"
        self.add(f'<text class="{klass}" x="{x:.1f}" y="{y:.1f}" font-size="{size}">'
                 f'<tspan class="{label_cls}">{esc(label)}</tspan>{esc(" " + content) if content else ""}</text>')

    def line(self, points, cls="edge", marker=None):
        d = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in points)
        end = f' marker-end="url(#{marker})"' if marker else ""
        self.add(f'<path class="{cls}" d="{d}"{end}/>')

    def link(self, href, inner):
        self.add(f'<a href="{esc(href)}" xlink:href="{esc(href)}">{inner}</a>')

    def render(self, height):
        defs = ('<defs>' + "".join(
            f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
            f'<path class="{cls}" d="M0,0 L10,5 L0,10 z"/></marker>'
            for mid, cls in (("m-edge", "mk-edge"), ("m-ask", "mk-ask"), ("m-decl", "mk-decl"))) + '</defs>')
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'viewBox="0 0 {self.w} {height:.0f}" width="{self.w}" height="{height:.0f}" font-family="{SANS}">\n'
                f'<title>{esc(self.title)}</title>\n<desc>{esc(self.desc)}</desc>\n<style>{CSS}</style>\n{defs}\n'
                f'<rect class="bg" width="{self.w}" height="{height:.0f}"/>\n')
        return head + "\n".join(self.parts) + "\n</svg>\n"


def box_block(svg, x, y, w, title, lines, role, size=11.5, min_h=0, dashed=False, mono_lines=()):
    """Блок с заголовком и абзацами; возвращает высоту (рисует только если svg не None)."""
    wrapped = []
    for line in lines:
        wrapped += wrap(line, w - 24, size)
    height = max(min_h, 14 + 18 + len(wrapped) * (size + 4.5) + 10)
    if svg is not None:
        svg.rect(x, y, w, height, role, dashed=dashed)
        svg.text(x + 12, y + 24, title, 13.5, bold=True)
        for i, line in enumerate(wrapped):
            svg.text(x + 12, y + 44 + i * (size + 4.5), line, size, cls="m")
    return height


# --- обзор -----------------------------------------------------------------------------------------
def group_file(category):
    return f"{GROUP_DIR}/{category}.svg"


def render_overview(data):
    W = 1800
    svg = Svg(W, "Карта сценариев эмулятора Алисы",
              "Порядок выбора сценария, поставщики ответа и группы сценариев со ссылками на страницы групп.")
    groups = {}
    for scenario in data.scenarios:
        groups.setdefault(scenario.category, []).append(scenario)
    total_phrases = sum(s.phrase_total for s in data.scenarios)
    svg.text(24, 38, "Карта сценариев эмулятора Алисы", 22, bold=True)
    svg.text(24, 60, f"{len(data.scenarios)} сценариев в {len(groups)} группах · {total_phrases} фраз и шаблонов · "
                     "собрана генератором tools/scenario_map.py из реальных файлов; страницы групп — по ссылкам", 12.5, cls="m")

    # 1. порядок выбора
    y = 96
    svg.text(24, y, "1 · ПОРЯДОК ВЫБОРА СЦЕНАРИЯ", 12, cls="m", bold=True)
    svg.add(f'<line class="rule" x1="24" y1="{y + 10}" x2="{W - 24}" y2="{y + 10}"/>')
    y += 28
    cw, gap = 262, 32
    row1 = [
        ("Реплика", ["VoiceInput или TextInput; ASR даёт текст. Нормализация снимает «Алиса», «пожалуйста», ё, склейки; "
                     "при пустом результате пробуются нечёткие варианты (однобуквенные огрехи ASR)."], "blue"),
        ("Ждёт ответа?", ["Если сценарий задал вопрос (ask), реплика уходит ему — раньше роутера. "
                          "Фраза со словом активации сбрасывает ожидание; TTL истёк — реплика идёт обычным путём."], "pink"),
        ("Кандидаты", ["Два слоя: пользовательские и плагины, затем встроенные. В слое: шаблон hassil (по точности) → "
                       "phrase (точная, вхождение, маска) → записи справочников фраз плагинов (ручной порядок phrase_order)."], "orange"),
        ("Зависимости и guard", ["Нет скрипта или зависимости — кандидат пропущен. Guard (право отказа) не сошёлся — "
                                 "следующий. Одинаковый способ совпадения у двух не встроенных — неоднозначность (tie)."], "violet"),
        ("Исполнение", ["Первый кандидат. Вернул None (уступает) — исполняется следующий; все уступили — реплика "
                        "идёт дальше. ask: вопрос с ShouldListen — колонка слушает ответ без «Алиса»."], "green"),
        ("Ответ", ["Речь (TTS) + директивы колонки + карточка → AliceResponse. HasVoiceResponse поднимается, "
                   "только когда аудио реально синтезировано."], "green"),
    ]
    heights = [box_block(None, 0, 0, cw, t, l, r) for t, l, r in row1]
    row_h = max(heights)
    for i, (title, lines, role) in enumerate(row1):
        x = 24 + i * (cw + gap)
        box_block(svg, x, y, cw, title, lines, role, min_h=row_h)
        if i:
            svg.line([(x - gap + 2, y + 30), (x - 2, y + 30)], marker="m-edge")
    row1_y, row1_h = y, row_h
    # ветка «служебные события» и «если сценария нет»
    y += row_h + 56
    row2 = [
        ("Ступень поиска", ["Записи справочника с search: свободный хвост «включи {X}» — параллельный опрос каталогов "
                            "поставщиков музыки с таймаутом. Нашёл — отвечает media.music; никто — «Не нашла «X»» (режим «Простой») "
                            "или дальше."], "pink"),
        ("«Вместе»", ["Маршрут устройства hybrid: голосовой ход релеится настоящему Яндексу, сценарии музыки "
                      "и «недоступно» уступают сами."], "blue"),
        ("«Без ответа»", ["Фраза без сценария попадает в очередь «Эмулятор → Без ответа» в любом режиме — "
                          "по ней потом пишется сценарий."], "gray"),
    ]
    levels = intelligence.LEVELS
    row2.append(("Режим «Интеллект»", [
        f"{levels[0]}: «{intelligence.NOT_UNDERSTOOD}» (стандартный ответ not_understood), модель не вызывается.",
        f"{levels[1]}: один вызов LLM с историей, без инструментов.",
        f"{levels[2]}: агент LangGraph с инструментами (каталог сценариев: scenario_list/get/probe/run; действия alice_action).",
        "ext:<id>: режим расширения (modes/*.py)."], "pink"))
    row2.append(("Служебные события", [
        "Колбэки колонки (informers.requested, timesync.requested, music.stack_next, callbacks.ack…) — отдельный путь: "
        "первый сценарий с provide-триггером этого события в порядке каталога; нет — тихое подтверждение. "
        "Пользовательский сценарий с тем же событием перекрывает встроенный."], "gray"))
    widths = [cw, cw, cw, cw * 1 + 70, cw + 70]
    heights = [box_block(None, 0, 0, w, t, l, r) for (t, l, r), w in zip(row2, widths)]
    row2_h = max(heights)
    xs, x = [], 24
    for w in widths:
        xs.append(x)
        x += w + gap
    for i, ((title, lines, role), w, x0) in enumerate(zip(row2, widths, xs)):
        box_block(svg, x0, y, w, title, lines, role, min_h=row2_h, dashed=(i == 4))
        if 0 < i < 4:
            svg.line([(x0 - gap + 2, y + 30), (x0 - 2, y + 30)], marker="m-edge")
    exec_x = 24 + 4 * (cw + gap)
    svg.line([(exec_x + cw / 2, row1_y + row1_h), (exec_x + cw / 2, y - 34), (xs[0] + cw / 2, y - 34),
              (xs[0] + cw / 2, y - 2)], cls="decl", marker="m-decl")
    svg.text(xs[0] + cw / 2 + 8, y - 38, "если сценария нет: нет кандидата или все уступили", 11, cls="m")
    y += row2_h + 34

    # 2. приоритет каталога
    svg.text(24, y, "2 · ПРИОРИТЕТ В КАТАЛОГЕ: слой 1 выше слоя 2; внутри — fallback последними, затем priority (меньше — раньше), затем id", 12, cls="m", bold=True)
    svg.add(f'<line class="rule" x1="24" y1="{y + 10}" x2="{W - 24}" y2="{y + 10}"/>')
    y += 28
    lanes = [("слой 1 · плагины и пользовательские", [s for s in data.scenarios if s.origin != "builtin"]),
             ("слой 2 · встроенные", [s for s in data.scenarios if s.origin == "builtin"])]
    for caption, items in lanes:
        svg.text(24, y + 4, caption, 11.5, cls="m", bold=True)
        y += 16
        x = 24
        for scenario in items:
            label = f"{scenario.priority}{'f' if scenario.fallback else ''} {scenario.id}"
            w = text_w(label, 11, mono=True) + 20
            if x + w > W - 24:
                x = 24
                y += 28
            svg.link(group_file(scenario.category), f'<g><rect class="box k-{GROUP_ROLE.get(scenario.category, "gray")}" '
                     f'x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="22" rx="11"/>'
                     f'<text class="mo" x="{x + 10:.1f}" y="{y + 15:.1f}" font-size="11">{esc(label)}</text></g>')
            x += w + 8
        y += 40
    y += 6
    svg.text(24, y - 14, "f — fallback-сценарий (ловит всё, что не взяли другие); цвет — группа; шаблон точнее — раньше при равных слоях", 11, cls="m")

    # 3. поставщики
    y += 14
    svg.text(24, y, "3 · ОТКУДА БЕРЁТСЯ ОТВЕТ: поставщики и плагины", 12, cls="m", bold=True)
    svg.add(f'<line class="rule" x1="24" y1="{y + 10}" x2="{W - 24}" y2="{y + 10}"/>')
    y += 28
    cards = provider_cards(data)
    pw = (W - 48 - 3 * 24) // 4
    heights = [box_block(None, 0, 0, pw, t, l, "violet") for t, l in cards]
    per_row = 4
    for start in range(0, len(cards), per_row):
        chunk = cards[start:start + per_row]
        h = max(heights[start:start + per_row])
        for i, (title, lines) in enumerate(chunk):
            box_block(svg, 24 + i * (pw + 24), y, pw, title, lines, "violet", min_h=h)
        y += h + 20
    y += 10

    # 4. группы
    svg.text(24, y, "4 · ГРУППЫ СЦЕНАРИЕВ (клик по сценарию — страница группы: фразы, ветвления, ответы)", 12, cls="m", bold=True)
    svg.add(f'<line class="rule" x1="24" y1="{y + 10}" x2="{W - 24}" y2="{y + 10}"/>')
    y += 28
    gw = (W - 48 - 2 * 24) // 3
    order = [c["id"] for c in data.categories]
    cells = []
    for category in data.categories:
        cid = category["id"]
        items = groups.get(cid, [])
        lines = []
        cells.append((category, items))
    col_y = [y, y, y]
    for category, items in cells:
        cid = category["id"]
        title = (category.get("title") or {}).get("ru") or cid
        desc = (category.get("description") or {}).get("ru") or ""
        desc_lines = wrap(desc, gw - 24, 11.5)
        h = 24 + 20 + len(desc_lines) * 16 + 14 + max(1, len(items)) * 22 + 14
        slot = min(range(3), key=lambda i: col_y[i])
        x0, y0 = 24 + slot * (gw + 24), col_y[slot]
        role = GROUP_ROLE.get(cid, "gray")
        svg.rect(x0, y0, gw, h, role)
        phrases = sum(s.phrase_total for s in items)
        link_inner = (f'<text class="lk b" x="{x0 + 12}" y="{y0 + 24}" font-size="14">{esc(title)} → {esc(cid)}</text>'
                      if items else f'<text class="t b" x="{x0 + 12}" y="{y0 + 24}" font-size="14">{esc(title)}</text>')
        if items:
            svg.link(group_file(cid), link_inner)
        else:
            svg.add(link_inner)
        svg.text(x0 + gw - 12, y0 + 24, f"{len(items)} сц. · {phrases} фраз", 11.5, cls="m", anchor="end")
        for i, line in enumerate(desc_lines):
            svg.text(x0 + 12, y0 + 46 + i * 16, line, 11.5, cls="m")
        yy = y0 + 46 + len(desc_lines) * 16 + 12
        if not items:
            svg.text(x0 + 12, yy + 14, "встроенных нет: сюда попадают пользовательские сценарии", 11.5, cls="m")
        for scenario in items:
            tag = "" if scenario.origin == "builtin" else " · плагин"
            body = (f'<text class="lk" x="{x0 + 12}" y="{yy + 14}" font-size="12">{esc(scenario.title)}'
                    f'<tspan class="m" font-size="11"> {esc(scenario.id)}{esc(tag)}</tspan></text>')
            svg.link(f"{group_file(cid)}#{scenario.id}", body)
            svg.text(x0 + gw - 12, yy + 14, f"{scenario.phrase_total} фраз", 11, cls="m", anchor="end")
            yy += 22
        col_y[slot] = y0 + h + 18
    y = max(col_y) + 10
    return svg.render(y)


def provider_cards(data):
    cards = []
    kinds = {}
    for provider in data.providers:
        kinds.setdefault(provider["kind"], []).append(provider)
    handlers = {}
    for entry in data.entries:
        handlers.setdefault(entry.get("kind") or "", set()).add(entry["handler"])
    for kind, items in kinds.items():
        names = "; ".join(f"{p['name']} ({p['ext']})" for p in items)
        caps = sorted({c for p in items for c in p["capabilities"]})
        used = ", ".join(sorted(handlers.get(kind) or [])) or "—"
        lines = [f"Поставщик вида «{kind}»: {names}.", f"Фразы ведут в сценарий: {used}.",
                 "Выбранный поставщик вида идёт первым в споре записей (settings.phrase_order)."]
        if caps:
            lines.append("Возможности: " + ", ".join(caps) + ".")
        cards.append((f"{items[0].get('title') or kind}", lines))
    plain = [m for m in data.manifests if not (m.get("contributes") or {}).get("providers")]
    for manifest in plain:
        contributes = manifest.get("contributes") or {}
        mine = sorted({s.id for s in data.scenarios if s.origin == f"extension:{manifest['id']}"})
        lines = [f"Плагин {manifest['id']}: " + (manifest.get("description") or "")[:150].rstrip() + "…"
                 if len(manifest.get("description") or "") > 150 else f"Плагин {manifest['id']}: {manifest.get('description') or ''}"]
        if mine:
            lines.append("Сценарии плагина: " + ", ".join(mine) + ".")
        if contributes.get("actions"):
            lines.append(f"Действия: {contributes['actions'].get('prefix')}.* (вызов из сценария ctx.action).")
        cards.append((manifest.get("name") or manifest["id"], lines))
    cards.append(("Нет поставщика", [
        "Сценарий данных без плагина-источника: «Простой» — стандартный ответ no_data_source "
        "(«Для этого нет источника данных.»); «Модель/Агент» — отказ, фразу забирает модель; «Вместе» — Яндекс."]))
    cards.append(("Колонка сама", [
        "Таймеры, будильники, плеер, громкость, подсветку ведёт колонка. Сценарий отдаёт те же директивы, что "
        "настоящий сервер на эту фразу; состояние берёт из сообщений колонки (ctx.timers, ctx.player, ctx.volume)."]))
    return cards


# --- страница группы -------------------------------------------------------------------------------
COL_PHRASE, COL_LOGIC, COL_OUT, PAD, GAP = 470, 620, 440, 18, 34
PAGE_W = PAD * 2 + COL_PHRASE + COL_LOGIC + COL_OUT + GAP * 2 + 28


def phrase_lines(scenario):
    """Строки колонки «Фразы»: (стиль, текст); стиль phrase|mono|cap|muted."""
    lines = []
    if scenario.phrases:
        joined = " · ".join(f"«{p}»" for p in scenario.phrases)
        lines.append(("cap", "ТОЧНЫЕ ФРАЗЫ"))
        for row in limit_lines(wrap(joined, COL_PHRASE - 8, 11.5), 5):
            lines.append(("phrase", row))
    if scenario.templates:
        count = sum(c for _s, c, _e in scenario.templates)
        lines.append(("cap", f"ШАБЛОНЫ HASSIL · {len(scenario.templates)} шт., ≈{count} вариантов"))
        for sentence, _count, _examples in scenario.templates[:7]:
            lines.append(("mono", clip(sentence, COL_PHRASE - 8, 11, mono=True)))
        if len(scenario.templates) > 7:
            lines.append(("muted", f"… ещё {len(scenario.templates) - 7} шаблонов"))
        samples = [e for _s, _c, ex in scenario.templates for e in ex[:1]][:3]
        if samples:
            for row in limit_lines(wrap("примеры: " + " · ".join(f"«{s}»" for s in samples), COL_PHRASE - 8, 11.5), 2):
                lines.append(("muted", row))
    for entry in scenario.entries:
        ext = entry["ext"].replace("builtin.", "")
        lines.append(("cap", f"СПРАВОЧНИК ПЛАГИНА {ext} · {entry['id']}" + (" · поиск по хвосту" if entry["search"] else "")))
        for sentence, _c, _e in entry["_templates"][:2]:
            lines.append(("mono", clip(sentence, COL_PHRASE - 8, 11, mono=True)))
        if len(entry["sentences"]) > 2:
            lines.append(("muted", f"… ещё {len(entry['sentences']) - 2}"))
    for event in scenario.provides:
        lines.append(("cap", "СЛУЖЕБНОЕ СОБЫТИЕ КОЛОНКИ"))
        lines.append(("mono", event))
    for event in scenario.events:
        lines.append(("cap", "СОБЫТИЕ"))
        lines.append(("mono", event))
    if not lines:
        lines.append(("muted", "триггеров нет: сценарий вызывают плагин или агент"))
    return lines


def step_badges(step, steps_by_name_titles):
    """Строки узла ветвления: (метка, класс метки, текст)."""
    rows = []
    for question, then in step.asks:
        rows.append(("вопрос:", "lb-ask", f"«{question}» — слушает ответ (ShouldListen)" + (f" → {then}" if then else "")))
    for reason in step.declines[:3]:
        rows.append(("уступает:", "lb-decl", reason))
    for action in step.actions:
        rows.append(("действие:", "lb-act", action))
    for use in step.uses[:4]:
        rows.append(("данные:", "lb-data", use))
    for error in step.errors[:2]:
        rows.append(("ошибка:", "lb-decl", error))
    if step.also:
        rows.append(("см. также:", "lb-data", ", ".join(step.also[:3])))
    return rows


def out_lines(step):
    rows = []
    for say_text in step.says[:6]:
        rows.append(("речь:", "lb-say", f"«{say_text}»"))
    if len(step.says) > 6:
        rows.append(("", "lb-say", f"… ещё {len(step.says) - 6} реплик"))
    if step.directives:
        names = ", ".join(step.directives[:10]) + (f" … +{len(step.directives) - 10}" if len(step.directives) > 10 else "")
        rows.append(("директивы:", "lb-dir", names))
    if step.card:
        rows.append(("карточка:", "lb-say", "текстовая карточка в ответе"))
    return rows


def wrap_rows(rows, width, size=11.5):
    """Строки с меткой → список (метка|None, класс, текст) после переноса."""
    result = []
    for label, cls, text in rows:
        full = (label + " " + text).strip()
        wrapped = wrap(full, width, size)
        for i, row in enumerate(limit_lines(wrapped, 4)):
            if i == 0 and label:
                result.append((label, cls, row[len(label):].strip()))
            else:
                result.append(("", cls, row))
    return result


def layout_steps(root):
    """Плоский список (шаг, глубина, родитель, тип ребра) в порядке обхода."""
    flat = []

    def walk(step, depth, parent, edge):
        flat.append((step, depth, parent, edge))
        for child, kind in step.children:
            walk(child, depth + 1, step, kind)

    walk(root, 0, None, "")
    return flat


LH = 15.5


def draw_scenario(svg, scenario, y):
    """Рисует карточку сценария на странице группы, возвращает её высоту."""
    role = GROUP_ROLE.get(scenario.category, "gray")
    x0 = PAD
    inner_w = PAGE_W - 2 * PAD
    x_phr = x0 + 14
    x_log = x_phr + COL_PHRASE + GAP
    x_out = x_log + COL_LOGIC + GAP
    # шапка
    desc = limit_lines(wrap(scenario.description, inner_w - 28, 12), 2) if scenario.description else []
    head_h = 54 + len(desc) * 16
    # фразы
    plines = phrase_lines(scenario)
    phr_h = len(plines) * LH + 8
    # шаги
    flat = layout_steps(scenario.root) if scenario.root else []
    rows = []
    for step, depth, parent, edge in flat:
        indent = min(depth, 4) * 22
        node_w = COL_LOGIC - indent
        title_lines = [step.title]
        doc_lines = limit_lines(wrap(step.doc, node_w - 20, 11), 2) if step.doc and step.kind != "flow" else []
        badges = wrap_rows(step_badges(step, None), node_w - 20)
        node_h = 12 + 16 + len(doc_lines) * 14 + len(badges) * LH + 10
        outs = wrap_rows(out_lines(step), COL_OUT - 24)
        out_h = 12 + len(outs) * LH + 8 if outs else 0
        rows.append(dict(step=step, depth=depth, parent=parent, edge=edge, indent=indent, node_w=node_w,
                         doc=doc_lines, badges=badges, node_h=node_h, outs=outs, out_h=out_h,
                         h=max(node_h, out_h)))
    gap_rows = 12
    logic_h = sum(r["h"] for r in rows) + gap_rows * max(0, len(rows) - 1) if rows else 30
    body_h = max(phr_h + 22, logic_h + 22, 40)
    card_h = head_h + body_h + 14
    # рамка
    svg.add(f'<g id="{esc(scenario.id)}">')
    svg.rect(x0, y, inner_w, card_h, role)
    svg.text(x0 + 14, y + 26, scenario.title, 16, bold=True)
    tx = x0 + 14 + text_w(scenario.title, 16, bold=True) + 12
    svg.text(tx, y + 26, scenario.id, 12.5, mono=True)
    badge = [f"приоритет {scenario.priority}" + (" · fallback" if scenario.fallback else ""),
             "встроенный" if scenario.origin == "builtin" else f"плагин {scenario.origin.split(':', 1)[1]}",
             "логика в коде (.py)" if scenario.kind == "python" else "JSON-сценарий (flow)",
             f"фраз и шаблонов: {scenario.phrase_total}"]
    svg.text(x0 + inner_w - 14, y + 26, " · ".join(badge), 11.5, cls="m", anchor="end")
    for i, row in enumerate(desc):
        svg.text(x0 + 14, y + 46 + i * 16, row, 12, cls="m")
    if scenario.guard:
        svg.labeled(x0 + 14, y + 46 + len(desc) * 16, "guard:", "lb-decl",
                    clip(scenario.guard, inner_w - 80, 11, mono=True), 11, mono=True)
        head_h += 0
    top = y + head_h
    svg.add(f'<line class="rule" x1="{x0 + 10}" y1="{top - 6}" x2="{x0 + inner_w - 10}" y2="{top - 6}"/>')
    # заголовки колонок
    for x, caption in ((x_phr, "ФРАЗЫ"), (x_log, "ЛОГИКА И ВЕТВЛЕНИЯ"), (x_out, "ИТОГОВЫЕ ОТВЕТЫ")):
        svg.text(x, top + 12, caption, 10.5, cls="m", bold=True)
    body_top = top + 22
    for i, (style, content) in enumerate(plines):
        yy = body_top + 12 + i * LH
        if style == "cap":
            svg.text(x_phr, yy, content, 10, cls="m", bold=True)
        elif style == "mono":
            svg.text(x_phr + 6, yy, content, 11, mono=True)
        elif style == "muted":
            svg.text(x_phr + 6, yy, content, 11, cls="m")
        else:
            svg.text(x_phr + 6, yy, content, 11.5)
    # стрелка фразы → логика
    svg.line([(x_phr + COL_PHRASE + 4, body_top + 20), (x_log - 4, body_top + 20)], marker="m-edge")
    # шаги
    if not rows:
        svg.text(x_log, body_top + 14, "логика не разобрана: файл сценария недоступен", 11.5, cls="m")
    y_cursor = body_top
    positions = {}
    for r in rows:
        step = r["step"]
        nx = x_log + r["indent"]
        positions[id(step)] = (nx, y_cursor, r)
        role_node = "red" if step.declines and not (step.says or step.directives or step.actions) else \
            ("pink" if step.kind == "answer" else "gray")
        svg.rect(nx, y_cursor, r["node_w"], r["node_h"], role_node, rx=7, dashed=(step.kind == "answer"))
        svg.text(nx + 10, y_cursor + 18, step.title, 12, bold=True, mono=False)
        yy = y_cursor + 18
        for line in r["doc"]:
            yy += 14
            svg.text(nx + 10, yy, line, 11, cls="m")
        yy += 5
        for label, cls, text in r["badges"]:
            yy += LH
            if label:
                svg.labeled(nx + 10, yy, label, cls, text, 11.5)
            else:
                svg.text(nx + 10, yy, text, 11.5)
        # ответы
        if r["outs"]:
            oy = y_cursor
            svg.rect(x_out, oy, COL_OUT, r["out_h"], "green", rx=7)
            svg.line([(nx + r["node_w"] + 2, y_cursor + 14), (x_out - 3, y_cursor + 14)], marker="m-edge")
            yy = oy + 12
            for label, cls, text in r["outs"]:
                yy += LH
                if label:
                    svg.labeled(x_out + 10, yy - 2, label, cls, text, 11.5)
                else:
                    svg.text(x_out + 10, yy - 2, text, 11.5)
        y_cursor += r["h"] + gap_rows
    # связи дерева
    for r in rows:
        parent = r["parent"]
        if parent is None:
            continue
        px, py, pr = positions[id(parent)]
        cx, cy, cr = positions[id(r["step"])]
        rail = px + 10
        cls, marker = {"ask": ("ask", "m-ask"), "fail": ("decl", "m-decl")}.get(r["edge"], ("edge", "m-edge"))
        svg.line([(rail, py + pr["node_h"]), (rail, cy + 16), (cx - 2, cy + 16)], cls=cls, marker=marker)
    svg.add("</g>")
    return card_h


def render_group(data, category, items):
    title = (category.get("title") or {}).get("ru") or category["id"]
    desc = (category.get("description") or {}).get("ru") or ""
    svg = Svg(PAGE_W, f"Сценарии: {title}", f"Фразы, ветвления диалога и итоговые ответы сценариев группы «{title}».")
    svg.text(PAD, 36, f"Сценарии: {title}", 22, bold=True)
    svg.link(f"../{OVERVIEW}", f'<text class="lk" x="{PAGE_W - PAD}" y="34" font-size="13" text-anchor="end">← обзор карты сценариев</text>')
    svg.text(PAD, 58, desc, 12.5, cls="m")
    legend_y = 82
    svg.labeled(PAD, legend_y, "вопрос:", "lb-ask", "сценарий спрашивает, колонка слушает ответ (штриховая стрелка)", 11.5)
    svg.labeled(PAD + 470, legend_y, "уступает:", "lb-decl", "None — реплика уходит следующему кандидату", 11.5)
    svg.labeled(PAD + 830, legend_y, "действие / данные:", "lb-act", "эффект и источники", 11.5)
    svg.labeled(PAD + 1090, legend_y, "речь / директивы:", "lb-say", "итог для колонки", 11.5)
    y = 104
    for scenario in items:
        y += draw_scenario(svg, scenario, y) + 20
    return svg.render(y)


# --- сборка ----------------------------------------------------------------------------------------
def build():
    """→ (Data, {путь от docs/: текст svg})."""
    data = collect()
    files = {OVERVIEW: render_overview(data)}
    groups = {}
    for scenario in data.scenarios:
        groups.setdefault(scenario.category, []).append(scenario)
    for category in data.categories:
        items = groups.get(category["id"], [])
        if items:
            files[group_file(category["id"])] = render_group(data, category, items)
    return data, files


def main():
    data, files = build()
    (DOCS / GROUP_DIR).mkdir(exist_ok=True)
    for rel, text in files.items():
        (DOCS / rel).write_text(text, encoding="utf-8", newline="\n")
    print(f"сценариев: {len(data.scenarios)}, файлов: {len(files)}")


if __name__ == "__main__":
    main()
