"""Нормализация ASR-текста перед матчингом триггеров (S01).

Локальный ASR (t-one) отдаёт финалы вида «сколькобудетде» (склеенные слова),
«алиса, покажи глазки» (wake-слово), «вдключи сказки» (однобуквенный огрех).
Роутер триггеров работает по substring — без нормализации такие реплики мажут.

Пайплайн: очистка (регистр/ё/пунктуация) → снятие wake-слов → разглей
склеенных токенов по лексикону триггеров → нечёткая правка однобуквенных
огрехов (уникальный кандидат — сразу, неоднозначные — вариантами фразы для
повторного матчинга, normalize_variants). Нормализация только для выбора
сценария: в ctx.input.text остаётся исходная реплика (парсеры длительностей
и слотов работают по сырой фразе).
"""
from __future__ import annotations

import re

from .library import LEXICONS

_ASR = LEXICONS.get("asr") or {}
WAKE_WORDS = tuple(_ASR.get("wake_words") or ())
TRAILING_FILLERS = tuple(_ASR.get("trailing_fillers") or ())
# Служебные слова команд — расширяют лексикон разглея сверх слов триггеров
# (данные — scenarios/lexicons.json, раздел asr).
COMMON_WORDS = tuple(_ASR.get("common_words") or ())
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def clean(text):
    """Нижний регистр, ё→е, пунктуация в пробелы, схлопнутые пробелы."""
    lowered = (text or "").lower().replace("ё", "е")
    return _SPACES.sub(" ", _PUNCT.sub(" ", lowered)).strip()


def strip_wake(text):
    """«алиса покажи …» → «покажи …»; филлеры в конце тоже снимаются."""
    words = text.split(" ")
    while words and words[0] in WAKE_WORDS:
        words = words[1:]
    while words and words[-1] in TRAILING_FILLERS:
        words = words[:-1]
    return " ".join(words)


def _segments(token, lexicon):
    """Разбивка токена на слова лексикона.

    Сначала полный word-break (DP, минимум кусков) — покрывает точные склейки
    вроде «поставьтаймер». Если полного разбора нет (ASR-хвост вроде «де» в
    «сколькобудетде» не входит в лексикон) — жадный проход слева: слово
    лексикона максимальной длины, иначе неизвестный кусок до следующей позиции,
    где начинается слово лексикона.
    """
    words = _full_break(token, lexicon)
    if words is not None:
        return words
    return _greedy_break(token, lexicon)


def _full_break(token, lexicon):
    best = [0] + [None] * len(token)  # позиция → минимальное число кусков
    cuts = [None] * (len(token) + 1)
    for end in range(1, len(token) + 1):
        for start in range(0, end):
            if best[start] is None:
                continue
            if token[start:end] in lexicon:
                cost = best[start] + 1
                if best[end] is None or cost < best[end]:
                    best[end] = cost
                    cuts[end] = start
    if best[len(token)] is None:
        return None
    words, position = [], len(token)
    while position > 0:
        start = cuts[position]
        words.append(token[start:position])
        position = start
    return words[::-1]


def _greedy_break(token, lexicon):
    out, position = [], 0
    while position < len(token):
        matched = None
        for end in range(len(token), position, -1):  # длиннейшее слово лексикона
            if token[position:end] in lexicon:
                matched = end
                break
        if matched is not None:
            out.append(token[position:matched])
            position = matched
            continue
        end = position + 1  # неизвестный кусок до слова лексикона впереди
        while end < len(token) and not _starts_word(token, end, lexicon):
            end += 1
        out.append(token[position:end])
        position = end
    return out


def _starts_word(token, position, lexicon):
    return any(token[position:end] in lexicon
               for end in range(position + 1, min(len(token), position + 24) + 1))


def deglue(text, lexicon):
    """Склеенные токены разваливаются на слова лексикона.

    Тронуты только токены, которых нет в лексиконе целиком и которые
    разбираются минимум на ДВА слова лексикона — разбор с одним словарным
    куском («полчаса» → «пол | час | а») это огрех, а не склейка: слова
    с вложенным словом лексикона остаются целыми.
    """
    out = []
    for token in text.split(" "):
        if token and token not in lexicon and len(token) >= 6:
            words = _segments(token, lexicon)
            if sum(1 for word in words if word in lexicon) >= 2:
                out.extend(words)
                continue
        out.append(token)
    return " ".join(out)


def lexicon_for(scenarios):
    """Слова всех phrase-триггеров каталога + служебные слова команд."""
    words = set(COMMON_WORDS)
    for scenario in scenarios or []:
        for trigger in scenario.get("triggers", []) or []:
            if trigger.get("kind") != "phrase":
                continue
            for phrase in [trigger.get("name") or ""] + list(trigger.get("phrases") or []):
                for word in clean(phrase if isinstance(phrase, str) else "").split(" "):
                    if word:
                        words.add(word)
    return words


def _levenshtein_at_most_1(a, b):
    """Расстояние Левенштейна ≤ 1 (замена/вставка/удаление) без полного DP."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a  # a короче: одна вставка в b
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        diff += 1
        if diff > 1:
            return False
        j += 1
    return diff + (len(b) - j) <= 1


FUZZY_MIN_LEN = 5  # короткие слова ( «на», «час») чинить опасно — ложные замены


def fuzzy_candidates(token, lexicon):
    """Слова лексикона на расстоянии ≤1 («вдключи» → включи/выключи)."""
    if len(token) < FUZZY_MIN_LEN:
        return []
    return [word for word in lexicon
            if abs(len(word) - len(token)) <= 1 and _levenshtein_at_most_1(token, word)]


def _tokens(text):
    return text.split(" ") if text else []


def normalize_text(text, lexicon):
    """Полный пайплайн для матчинга: clean → wake → deglue → fuzzy (уникальная замена)."""
    cleaned = strip_wake(clean(text))
    if not cleaned:
        return cleaned
    tokens = _tokens(deglue(cleaned, lexicon))
    out = []
    for token in tokens:
        if token not in lexicon:
            candidates = fuzzy_candidates(token, lexicon)
            if len(candidates) == 1:  # однозначная однобуквенная правка
                token = candidates[0]
        out.append(token)
    return " ".join(out)


def normalize_variants(text, lexicon, limit=4):
    """Нечёткие варианты реплики для повторного матчинга.

    Уникальные правки normalize_text уже применил; здесь — токены с НЕСКОЛЬКИМИ
    кандидатами («вдключи» → и «включи», и «выключи»): каждый кандидат даёт
    вариант фразы. Роутер пробует варианты, пока один не поддержан."""
    cleaned = strip_wake(clean(text))
    if not cleaned:
        return []
    variants, base = [], _tokens(deglue(cleaned, lexicon))
    for index, token in enumerate(base):
        if token in lexicon or len(token) < FUZZY_MIN_LEN:
            continue
        for candidate in fuzzy_candidates(token, lexicon):
            variant = " ".join(base[:index] + [candidate] + base[index + 1:])
            if variant != " ".join(base) and variant not in variants:
                variants.append(variant)
                if len(variants) >= limit:
                    return variants
    return variants
