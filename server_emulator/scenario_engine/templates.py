"""TASK-022: триггеры-шаблоны на hassil — полное совпадение фразы со слотами.

Вид триггера (JSON v2, docs/contracts/scenario-json-v2.md «Триггер template»):

    {"kind": "template",
     "sentences": ["({power}; свет) [(в|на) {room}]"],
     "lists": {"power": {"values": [{"in": "включи", "out": "on"}]}}}

Синтаксис hassil (Home Assistant): (а|б) — альтернативы, […] — необязательное,
{имя} — список (wildcard-список — свободный хвост). Значения совпавших списков
(сущности) и fixed-слоты данных интента становятся slots сценария. Цифры hassil
отдаёт float («5 минут» → 5.0), слова — int («пять» → 5): значения range-списков
приводятся к int по типу списка.

Списки и skip_words собираются из НЕСКОЛЬКИХ источников-словарей (merge_lexicons):
поздний источник приоритетнее — пользовательский перекрывает расширение, расширение —
встроенный. Источник — словарь с разделом lists ({имя: список hassil}) и skip_words
(список) либо asr ({wake_words, trailing_fillers}). Слои (TASK-030): встроенный
(builtin_source — scenarios/lexicons.json) → lists/*.json включённых расширений —
их ставит runtime (set_extension_sources); механизм пользовательских списков пока
не заведён, добавится в список после расширений.

Скорость: шаблоны каталога компилируются один раз в TemplateIndex (Intents.from_dict,
интервал — порядок каталога = приоритет); dialog.py держит индекс в кэше по версии
каталога — перекомпиляция при загрузке или сохранении сценария. Матчинг по фразе
целиком — меньше ложных срабатываний, чем contains; шум (wake-слова, филлеры)
снимают skip_words.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from hassil import Intents, parse_sentence, recognize_all
from hassil.errors import HassilError
from hassil.expression import ListReference, RuleReference

LEXICONS_PATH = Path(__file__).resolve().parent.parent / "scenarios" / "lexicons.json"
LANGUAGE = "ru"
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


class TemplateError(ValueError):
    """Ошибка шаблона template-триггера (синтаксис hassil, ссылки на списки)."""


def clean(text):
    """Нижний регистр, ё→е, пунктуация в пробелы. hassil сам снимает регистр и
    пунктуацию, но «ё» на «е» не отображает — как normalize.clean у phrase-триггеров."""
    lowered = str(text or "").lower().replace("ё", "е")
    return _SPACES.sub(" ", _PUNCT.sub(" ", lowered)).strip()


def builtin_source():
    """Встроенный источник словарей: scenarios/lexicons.json (раздел lists; skip_words —
    asr.wake_words + asr.trailing_fillers). Читается напрямую, без library: источники
    собирает вызывающий (dialog.py), слои расширений добавляет runtime (TASK-030).
    Битый файл — пустой источник (ошибку покажет загрузка библиотеки сценариев)."""
    try:
        return json.loads(LEXICONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# TASK-030: провайдер источников словарей расширений. validate.py (check_template)
# про runtime не знает, поэтому runtime ставит callable при старте — как
# proto_schemas.DIRECTORY. До установки (модульные тесты шаблонов) — только встроенный.
_EXTENSION_SOURCES = None


def set_extension_sources(provider):
    """Провайдер источников списков расширений: callable → [источники-словари]
    (lists/*.json включённых расширений). Ставится runtime один раз при старте."""
    global _EXTENSION_SOURCES
    _EXTENSION_SOURCES = provider


def extension_sources():
    """Источники списков включённых расширений ([] — провайдер не установлен).
    Сбой провайдера — пустой список: расширения не должны ломать валидацию и матчинг."""
    if _EXTENSION_SOURCES is None:
        return []
    try:
        return [source for source in (_EXTENSION_SOURCES() or []) if isinstance(source, dict)]
    except Exception:  # noqa: BLE001
        return []


def merge_lexicons(sources):
    """Слияние источников-словарей → (lists, skip_words).

    Конфликт имён списков — поздний источник перекрывает ранний (пользовательский >
    расширение > встроенный). skip_words объединяются: снятие шума общее для всех
    слоёв. Не-словари и не-списки значений пропускаются (битый слой не мешает другим).
    """
    lists, skip = {}, []
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        for name, values in (source.get("lists") or {}).items():
            if isinstance(values, dict):
                lists[name] = values
        words = source.get("skip_words")
        if words is None:
            words = source.get("asr")  # раздел asr lexicons.json: wake/fillers — тоже skip_words
        if isinstance(words, dict):  # {wake_words, trailing_fillers, skip_words}
            words = list(words.get("wake_words") or []) + list(words.get("trailing_fillers") or []) \
                + list(words.get("skip_words") or [])
        if isinstance(words, list):
            skip.extend(word for word in words if isinstance(word, str) and word.strip())
    seen, skip_words = set(), []
    for word in skip:
        key = clean(word)
        if key and key not in seen:
            seen.add(key)
            skip_words.append(key)
    return lists, skip_words


def _trigger_data(trigger):
    """Данные интента hassil из template-триггера: предложения, свои списки
    (per-intent в hassil — перекрывают общие), fixed-слоты."""
    data = {"sentences": [sentence for sentence in trigger["sentences"]
                          if isinstance(sentence, str) and sentence.strip()]}
    if isinstance(trigger.get("lists"), dict) and trigger["lists"]:
        data["lists"] = trigger["lists"]
    if isinstance(trigger.get("slots"), dict) and trigger["slots"]:
        data["slots"] = trigger["slots"]
    return data


def compile_intents(catalog, sources):
    """Intents hassil из каталога: интент = id сценария, данные = его template-триггеры.

    Порядок интентов = порядок каталога (приоритет): recognize_all отдаёт совпадения
    в этом порядке, первый — победитель. Возвращает (intents, skip_words, numeric)
    или None, если template-триггеров нет. numeric — имена range-списков (числа
    hassil приходят float/int — приводятся к int по типу списка)."""
    lists, skip_words = merge_lexicons(sources)
    numeric = {name for name, values in lists.items() if isinstance(values.get("range"), dict)}
    intents = {}
    for scenario in catalog or []:
        triggers = [t for t in (scenario.get("triggers") or [])
                    if isinstance(t, dict) and t.get("kind") == "template"]
        data = [entry for entry in (_trigger_data(trigger) for trigger in triggers)
                if entry["sentences"]]
        if not data:
            continue
        intents[str(scenario.get("id"))] = {"data": data}
        for trigger in triggers:
            for name, values in (trigger.get("lists") or {}).items():
                if isinstance(values, dict) and isinstance(values.get("range"), dict):
                    numeric.add(name)
    if not intents:
        return None
    payload = {"language": LANGUAGE, "intents": intents, "lists": lists}
    return Intents.from_dict(payload), skip_words, numeric


class TemplateIndex:
    """Скомпилированные шаблоны каталога: match(text) → (scenario_id, slots) | None —
    первый совпавший сценарий (порядок каталога), match_all — все совпадения."""

    def __init__(self, catalog, sources=None):
        compiled = compile_intents(catalog, [builtin_source()] if sources is None else sources)
        self.intents = compiled[0] if compiled else None
        self.skip_words = compiled[1] if compiled else []
        self.numeric = compiled[2] if compiled else set()

    def match(self, text):
        """(scenario_id, slots) первого совпавшего сценария или None."""
        found = self.match_all(text)
        return found[0] if found else None

    def match_all(self, text):
        """[(scenario_id, slots)] в порядке каталога. Ошибка hassil в рантайме (битая
        ссылка на список) — не совпадение, а не падение матчинга: такие шаблоны ловит
        валидатор при сохранении (check_template)."""
        if self.intents is None or not text:
            return []
        try:
            results = list(recognize_all(clean(text), self.intents, skip_words=self.skip_words))
        except HassilError:
            return []
        return [(result.intent.name, self._slots(result)) for result in results]

    def _slots(self, result):
        """Сущности hassil → slots; fixed-слоты данных интента добавляются первыми
        (сущность совпавшего списка сильнее). Числа range-списков — int."""
        slots = dict(result.intent_data.slots or {})
        for entity in result.entities_list:
            value = entity.value
            if entity.name in self.numeric and isinstance(value, float) and value.is_integer():
                value = int(value)
            slots[entity.name] = value
        return slots


def _refs(expression):
    """Имена списков и правил, на которые ссылается шаблон: {имя} — список, <rule> — правило."""
    lists, rules = set(), set()
    stack = [expression]
    while stack:
        node = stack.pop()
        if isinstance(node, ListReference):
            lists.add(node.list_name)
        elif isinstance(node, RuleReference):
            rules.add(node.rule_name)
        for attr in ("items", "expression", "body"):
            children = getattr(node, attr, None)
            if children is None:
                continue
            stack.extend(children if isinstance(children, (list, tuple)) else [children])
    return lists, rules


def check_template(trigger, shared_lists=None):
    """Проверка template-триггера при сохранении: синтаксис hassil каждой фразы
    (ошибка — с номером фразы) и разрешимость {ссылок} — список триггера или общий
    список источников. Бросает TemplateError; валидатор оборачивает в PackageError."""
    extra = set(trigger) - {"kind", "sentences", "lists", "slots"}
    if extra:
        raise TemplateError(f"template-триггер: неизвестные ключи {sorted(extra)}")
    sentences = trigger.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        raise TemplateError("template-триггер: sentences — непустой список шаблонов hassil")
    lists = trigger.get("lists") or {}
    if not isinstance(lists, dict):
        raise TemplateError("template-триггер: lists — объект {имя: список hassil}")
    slots = trigger.get("slots") or {}
    if not isinstance(slots, dict):
        raise TemplateError("template-триггер: slots — объект {имя: значение}")
    if shared_lists is None:
        # TASK-030: сценарий может ссылаться на список расширения — валидация видит
        # те же слои, что и матчинг (встроенный → расширения; провайдера нет — builtin)
        shared_lists = merge_lexicons([builtin_source()] + extension_sources())[0]
    for index, sentence in enumerate(sentences):
        where = f"template-триггер, фраза {index + 1}"
        if not isinstance(sentence, str) or not sentence.strip():
            raise TemplateError(f"{where}: непустая строка — шаблон hassil")
        try:
            parsed = parse_sentence(sentence, keep_text=True)
        except Exception as exc:
            raise TemplateError(f"{where} («{sentence}»): ошибка hassil — {exc}") from exc
        try:
            Intents.from_dict({"language": LANGUAGE,
                               "intents": {"check": {"data": [{"sentences": [sentence],
                                                               "lists": lists, "slots": slots}]}},
                               "lists": shared_lists})
        except Exception as exc:
            raise TemplateError(f"{where} («{sentence}»): ошибка hassil — {exc}") from exc
        list_names, rule_names = _refs(parsed)
        if rule_names:
            raise TemplateError(
                f"{where} («{sentence}»): правила {sorted(rule_names)} не поддерживаются — "
                "в template-триггере только {списки}")
        unknown = sorted(name for name in list_names if name not in lists and name not in shared_lists)
        if unknown:
            raise TemplateError(
                f"{where} («{sentence}»): нет списка {unknown} — объяви в lists триггера "
                "или добавь в общий lexicons.json (раздел lists)")
