"""R06: router — выбор сценария по триггерам с reason code.

Формы phrase-триггера (S01): слово/предложение (`name`), список (`phrases:[]`),
режим `match`: contains (по умолчанию — вхождение подстроки), exact (равенство),
mask (глоб `*`/`?` по нормализованной реплике). Матчинг идёт по нормализованному
тексту (normalize.py), reason объясняет выбор.

Template-триггеры (TASK-022) матчатся не здесь: шаблон сверяется с фразой целиком
в scenario_engine/templates.py (TemplateIndex, нужен скомпилированный каталог),
dialog.py подмешивает совпадения через with_templates — шаблонное совпадение
приоритетнее contains (и exact): полная фраза — самый строгий сигнал.

Continuation (ожидание ask) проверяется диалоговым слоем РАНЬШЕ роутера:
диалог в ожидании продолжает свой сценарий, новый триггер его не перебивает.
`select` возвращает первого кандидата (детерминизм: порядок объявления, точное
совпадение приоритетнее вхождения); `candidates` — упорядоченный список всех
совпавших: диалог прогоняет их через guard (право отказа) и зависимости,
отказавшийся сценарий пропускается — реплика уходит следующему или в LLM.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

MATCH_MODES = ("contains", "exact", "mask")


def trigger_phrases(trigger):
    """Список фраз триггера: name (слово/предложение) или phrases[] (список)."""
    phrases = []
    if isinstance(trigger.get("name"), str) and trigger.get("name"):
        phrases.append(trigger["name"])
    for phrase in trigger.get("phrases") or []:
        if isinstance(phrase, str) and phrase:
            phrases.append(phrase)
    return phrases


EVENT_FILTERS = ("callback", "purpose", "frame")


def match_trigger(trigger, event_name, text, event=None):
    """(matched, reason) одного триггера. Регистронезависимо; mask — по фразе целиком.

    event-триггер: имя служебного события + необязательные фильтры callback
    (тип typed-колбэка), purpose (цель колбэка), frame (семантический фрейм) —
    сверка с полями запроса (input.callback/purpose/frame)."""
    kind = trigger.get("kind")
    if kind in ("event", "protocol_event") and event_name:
        name = (trigger.get("name") or "").strip()
        if name.lower() != event_name.lower():
            return False, None
        fields = event or {}
        used = [key for key in EVENT_FILTERS if trigger.get(key)]
        for key in used:
            if str(fields.get(key) or "") != str(trigger[key]):
                return False, None
        return True, "event:" + name + "".join(f"[{key}={trigger[key]}]" for key in used)
    if kind == "intent" and event_name:
        name = (trigger.get("name") or "").strip()
        return name.lower() == event_name.lower(), f"intent:{name}"
    if kind == "phrase" and text:
        mode = trigger.get("match") or "contains"
        for phrase in trigger_phrases(trigger):
            needle, hay = phrase.lower().replace("ё", "е"), text.lower()  # реплика уже без «ё» (normalize.clean)
            if mode == "exact":
                if needle == hay:
                    return True, "phrase:exact"
            elif mode == "mask":
                if fnmatchcase(hay, needle):
                    return True, "phrase:mask"
            else:
                if needle == hay:
                    return True, "phrase:exact"
                if needle in hay:
                    return True, "phrase:contains"
    return False, None


def candidates(scenarios, event_name=None, text=None, event=None):
    """Упорядоченные кандидаты [(scenario, reason)] — для guard-разрешения.

    Порядок каталога задаёт priority (провайдер сценариев сортирует); здесь —
    точное совпадение раньше вхождения, фильтрованный event-триггер раньше
    голого имени, fallback-сценарии (catch-all) — всегда последними."""
    exact, contains = [], []
    for scenario in scenarios:
        for trigger in scenario.get("triggers", []):
            matched, reason = match_trigger(trigger, event_name, text, event)
            if not matched:
                continue
            bucket = contains if reason == "phrase:contains" else exact
            bucket.append((scenario, reason))
            break  # сценарий попадает в кандидаты один раз, первым своим триггером
    ordered = exact + contains  # точное совпадение где-то дальше выигрывает вхождение
    return sorted(ordered, key=lambda pair: (bool(pair[0].get("fallback")),
                                             "[" not in str(pair[1] or "") and str(pair[1]).startswith("event:")))


def select(scenarios, event_name=None, text=None):
    """(scenario, reason) | (None, reason). Побочных эффектов нет."""
    found = candidates(scenarios, event_name=event_name, text=text)
    if found:
        return found[0]
    return None, "no_match"


def with_templates(found, template_scenarios):
    """Шаблонные совпадения приоритетнее phrase-кандидатов: шаблон сверяется с фразой
    целиком (строже contains/exact), совпавшие сценарии идут первыми в порядке
    каталога, phrase-кандидаты остаются fallback'ом (guard/зависимости диалоговый
    слой разрешает по объединённому списку как раньше).
    template_scenarios — сценарии с совпавшим шаблоном (порядок каталога)."""
    if not template_scenarios:
        return found
    return [(scenario, "template") for scenario in template_scenarios] + list(found)
