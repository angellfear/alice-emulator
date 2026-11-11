"""Падежные формы названий для закрытых списков справочника фраз: «Берлога» →
«берлоге», «берлогу», «берлогой»…; «Рабочий кабинет» → «рабочем кабинете».

Готовое — морфологический словарь pymorphy3 (OpenCorpora): названия устройств и
площадок приходят из данных плагина (Home Assistant), заранее их не перечислить,
поэтому словарь `room` с ручными формами покрывает только общие комнаты.

Форма строится по единственному числу во всех падежах; в названии из нескольких
слов склоняется каждое слово (прилагательное сохраняет род по своему разбору).
Слово, которого нет в словаре (латиница, цифры), остаётся как есть.
"""
from __future__ import annotations

from functools import lru_cache

CASES = ("nomn", "gent", "datv", "accs", "ablt", "loct", "loc2")

_morph = None


def _analyzer():
    global _morph
    if _morph is None:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer(lang="ru")
    return _morph


def _word_form(word, case):
    parse = _analyzer().parse(word)[0]
    if not {"NOUN", "ADJF", "PRTF"} & set(parse.tag.grammemes):
        return word
    form = parse.inflect({case, "sing"}) or (parse.inflect({case}) if case != "loc2" else None)
    return form.word if form else (None if case == "loc2" else word)


@lru_cache(maxsize=4096)
def case_forms(name):
    """Формы названия в нижнем регистре без исходной: кортеж без повторов."""
    words = str(name or "").lower().split()
    if not words:
        return ()
    forms = []
    for case in CASES:
        parts = [_word_form(word, case) for word in words]
        if None in parts:
            continue
        form = " ".join(parts)
        if form != " ".join(words) and form not in forms:
            forms.append(form)
    return tuple(forms)


@lru_cache(maxsize=4096)
def lemma(word):
    """Нормальная форма слова («комедию» → «комедия»); не кириллица — как есть."""
    word = str(word or "").lower()
    if not any("а" <= ch <= "я" or ch == "ё" for ch in word):
        return word
    return _analyzer().parse(word)[0].normal_form
