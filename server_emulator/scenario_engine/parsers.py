"""Парсеры текста py-сценариев (SDK: ctx.parse_*, find_in) — примитивы без словарей: все слова приходят из данных.

Каждый парсер: (text, lexicon, **options) → значение | None. lexicon — данные
сценария или общий словарь (scenarios/lexicons.json): какие слова что значат,
решает JSON; здесь только механика поиска и сложения чисел.
"""
from __future__ import annotations

import re

_WORD = re.compile(r"[a-zа-яё0-9]+(?:[.,:][0-9]+)?", re.IGNORECASE)


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "").lower().replace("ё", "е")).strip()


def _tokens(text):
    return _WORD.findall(_norm(text))


def _contains_phrase(text, phrase):
    """Фраза словаря входит в текст целыми словами (префикс слова — если фраза кончается на *)."""
    phrase = _norm(phrase)
    if phrase.endswith("*"):
        return re.search(r"(?<![а-яa-z0-9])" + re.escape(phrase[:-1]), text) is not None
    return re.search(r"(?<![а-яa-z0-9])" + re.escape(phrase) + r"(?![а-яa-z0-9])", text) is not None


def parse_lexicon(text, lexicon, **_):
    """Словарь {фраза: значение} (или список фраз): самая длинная найденная фраза → значение."""
    text = _norm(text)
    items = lexicon.items() if isinstance(lexicon, dict) else ((p, p) for p in lexicon or [])
    best = None
    for phrase, value in items:
        if phrase and _contains_phrase(text, phrase) and (best is None or len(phrase) > len(best[0])):
            best = (phrase, value)
    return None if best is None else best[1]


def parse_list(text, lexicon, items=None, field="name", fields=None, contains_only=False, **_):
    """Элемент списка (options.items, напр. {{device.apps}} или {{home.devices}}), чьё
    поле (field или любое из fields; значение-список — любой элемент) входит в текст
    целыми словами, либо текст-остаток входит в него (если не contains_only).
    lexicon — слова-команды, которые снимаются с начала. Побеждает самое длинное имя."""
    text = _norm(text)
    for word in sorted(lexicon or [], key=len, reverse=True) if isinstance(lexicon, list) else []:
        if text.startswith(_norm(word) + " "):
            text = text[len(_norm(word)):].strip()
            break
    best = None
    for item in items or []:
        names = []
        for key in (fields or [field]):
            value = item.get(key) if isinstance(item, dict) else item
            names += value if isinstance(value, list) else [value]
        for name in names:
            title = _norm(name)
            if not title:
                continue
            hit = _contains_phrase(text, title) or (not contains_only and text and text in title)
            if hit and (best is None or len(title) > best[0]):
                best = (len(title), item)
    return None if best is None else best[1]


def parse_rest(text, lexicon, **_):
    """Остаток фразы после слова-команды из словаря (список), иначе None."""
    text = _norm(text)
    for word in sorted(lexicon or [], key=len, reverse=True):
        word = _norm(word)
        if text.startswith(word + " "):
            rest = text[len(word):].strip()
            return rest or None
    return None


def _numbers(lexicon):
    return (lexicon or {}).get("numbers") or {}


def parse_number(text, lexicon, minimum=None, maximum=None, **_):
    """Число из цифр или слов (lexicon.numbers: {слово: значение}); «двадцать пять» = 25."""
    numbers = _numbers(lexicon)
    pending = None
    for token in _tokens(text):
        if re.fullmatch(r"\d+(?:[.,]\d+)?", token):
            pending = float(token.replace(",", "."))
            break
        if token in numbers:
            value = numbers[token]
            pending = (pending or 0) + value if pending is not None and pending >= 20 and value < 10 else value
        elif pending is not None:
            break
    if pending is None:
        return None
    value = int(pending) if float(pending).is_integer() else pending
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        return None
    return value


def parse_duration(text, lexicon, **_):
    """Длительность в секундах: lexicon.units {слово: секунд}, lexicon.numbers, lexicon.fixed
    {фраза: секунд} («полчаса»). Число без единицы — секунды."""
    norm = _norm(text)
    for phrase, seconds in ((lexicon or {}).get("fixed") or {}).items():
        if _contains_phrase(norm, phrase):
            return int(seconds)
    units, numbers = (lexicon or {}).get("units") or {}, _numbers(lexicon)
    total, matched, pending = 0.0, False, None
    for token in _tokens(norm):
        if token in units:
            total += (1 if pending is None else pending) * units[token]
            pending, matched = None, True
        elif token in numbers:
            value = numbers[token]
            pending = (pending or 0) + value if pending is not None and pending >= 20 and value < 10 else value
        elif re.fullmatch(r"\d+(?:[.,]\d+)?", token):
            pending = float(token.replace(",", "."))
    if pending is not None and not matched:
        total, matched = total + pending, True
    total = int(round(total))
    return total if matched and total > 0 else None


def parse_time(text, lexicon, **_):
    """Время «ЧЧ:ММ»: цифрами (8:30, 09.15, 7 30) или словами (lexicon.numbers — часы,
    lexicon.minutes — минуты словами)."""
    norm = _norm(text)
    match = re.search(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)", norm)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        numbers, minutes_words = _numbers(lexicon), (lexicon or {}).get("minutes") or {}
        hour = minute = None
        tokens = _tokens(norm)
        for index, token in enumerate(tokens):
            if hour is None:
                if token.isdigit():
                    hour = int(token)
                elif token in numbers:
                    hour = numbers[token]
                continue
            pair = " ".join(tokens[index:index + 2])
            if token.isdigit() and int(token) < 60:
                minute = int(token)
            elif pair in minutes_words:
                minute = minutes_words[pair]
            elif token in minutes_words:
                minute = minutes_words[token]
            break
        if hour is None:
            return None
        minute = minute or 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


PARSERS = {"lexicon": parse_lexicon, "list": parse_list, "rest": parse_rest,
           "number": parse_number, "duration": parse_duration, "time": parse_time}
