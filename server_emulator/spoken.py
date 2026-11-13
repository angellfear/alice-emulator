"""Текст для синтеза речи и пределы ожидания синтеза."""
import re
from functools import lru_cache

from num2words import num2words

EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️‍]")
TTS_WAIT_S = 6  # первый звук синтеза дольше — ответ без голоса (колонка ждёт ответ ~17 с)
OPEN_TTS_WAIT_S = 20  # синтез без спешки: отложенный ответ готовится до Push, колонка не ждёт


def speakable(text):
    """Текст для синтеза: без тегов, эмодзи и разметки Markdown (маркеры списков, **, #)."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = EMOJI.sub("", text)
    text = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)]|#+)\s+", "", text)
    text = re.sub(r"[*_`#]+", "", text).strip()
    text = re.sub(r"(?<=[^\s.!?…:,;])[ \t]*\n\s*", ". ", text)  # строка без точки — пауза
    return re.sub(r"\s+", " ", text).strip()


def tts_wait(runtime):
    return float(runtime.settings.get("tts_wait_s") or TTS_WAIT_S)


def voice_output(runtime):
    """Режимы вывода ответа (карточка «Синтез (TTS)»): (озвучивать, карточка с текстом)
    из settings["voice_stack"] (ключи speak/card). Ответа совсем без текста нет —
    озвучка выключена делает карточку принудительно включённой."""
    stack = runtime.settings.get("voice_stack") or {}
    speak = bool(stack.get("speak", True))
    return speak, True if not speak else bool(stack.get("card", True))


# ── знаки и числа словами (галочка «Знаки и числа произносить словами») ──────
# Многие TTS-движки не читают цифры 0–9 и знаки. Числа превращает num2words
# (LGPL; дробь — только при наличии дробной части, «7.0» иначе звучит как
# «семь целых ноль десятых»), единицы согласует с числом pymorphy3 («21 градус»,
# «22 градуса», «25 градусов»), знаки — таблица _SIGNS, RU/EN по языку ответа.
# Применяется только в тракте TTS: текст карточки и журнал остаются как есть.

_TAG = re.compile(r"(<[^>]+>)")  # группа захвата — теги остаются в split-частях
_SPACES = re.compile("[    ]")
_INVISIBLE = re.compile("[­​-‏⁠﻿]")
_QUOTES = re.compile('[«»„“”"]')
# эмодзи и декор не озвучиваются; «→» исключена — у неё есть слово в _SIGNS
_DECOR = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿■-◿←-↑↓-⇿"
    "⌀-⏿•·©®™️‍]")

# Единицы после числа: регэксп -> (существительное RU, хвост RU, EN ед., EN мн., хвост EN).
# Существительное согласуется с числом (pymorphy3), хвост добавляется как есть.
_UNITS = (
    (r"°\s?[CС]", ("градус", "Цельсия", "degree", "degrees", "Celsius")),
    (r"°\s?F", ("градус", "по Фаренгейту", "degree", "degrees", "Fahrenheit")),
    ("°", ("градус", "", "degree", "degrees", "")),
    ("%", ("процент", "", "percent", "percent", "")),
    ("‰", ("промилле", "", "per mille", "per mille", "")),
    ("′", ("минута", "", "minute", "minutes", "")),
    ("″", ("секунда", "", "second", "seconds", "")),
    ("км²", ("километр", "в квадрате", "kilometer", "kilometers", "squared")),
    ("м²", ("метр", "в квадрате", "meter", "meters", "squared")),
    ("м³", ("метр", "в кубе", "meter", "meters", "cubed")),
    (r"мм\s?рт\.\s?ст\.", ("миллиметр", "ртутного столба", "millimeter", "millimeters", "of mercury")),
    ("м/с", ("метр", "в секунду", "meter", "meters", "per second")),
    ("км/ч", ("километр", "в час", "kilometer", "kilometers", "per hour")),
    ("гПа", ("гектопаскаль", "", "hectopascal", "hectopascals", "")),
    ("мм", ("миллиметр", "", "millimeter", "millimeters", "")),
    ("см", ("сантиметр", "", "centimeter", "centimeters", "")),
    ("км", ("километр", "", "kilometer", "kilometers", "")),
    ("кг", ("килограмм", "", "kilogram", "kilograms", "")),
    (r"\$", ("доллар", "", "dollar", "dollars", "")),
    ("€", ("евро", "", "euro", "euros", "")),
    ("£", ("фунт", "стерлингов", "pound", "pounds", "")),
    ("₽", ("рубль", "", "ruble", "rubles", "")),
    (r"руб\.?", ("рубль", "", "ruble", "rubles", "")),
)
_UNIT_RE = "|".join(f"(?:{pattern})" for pattern, _ in _UNITS)
_QUANTITY = re.compile(
    rf"(?P<pre>[$€£₽])?(?P<sign>(?<!\w)[-−+])?(?P<int>\d+)(?:[.,](?P<frac>\d+))?"
    rf"(?:\s?(?P<unit>{_UNIT_RE})(?!\w))?")
# диапазон целых: «10–15», «+2…+5», «10–15°C» -> «от десяти до пятнадцати градусов Цельсия»
_RANGE = re.compile(
    rf"(?<![\w.,])(?P<a>[-−+]?\d+)(?:\s?[°%])?\s*[–—…]\s*(?P<b>[-−+]?\d+)(?![.,]?\d)"
    rf"(?:\s?(?P<unit>{_UNIT_RE})(?!\w))?")
_TIME = re.compile(r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_THOUSANDS = re.compile(r"(?<![\d.,])\d{1,3}(?:[   ]\d{3})+(?!\d)")
_DOTTED = re.compile(r"\d+(?:\.\d+){2,}")  # даты 13.11.2026, версии: части по отдельности
_SLASH_DIGITS = re.compile(r"(?<=\d)\s?/\s?(?=\d)")
_FRACTIONS = {
    "½": ("одна вторая", "one half"), "⅓": ("одна треть", "one third"),
    "⅔": ("две трети", "two thirds"), "¼": ("одна четвёртая", "one quarter"),
    "¾": ("три четвёртых", "three quarters"),
}
_FRACTION = re.compile("(\\d)?([" + "".join(_FRACTIONS) + "])")
_POWERS = {"²": ("в квадрате", "squared"), "³": ("в кубе", "cubed")}
_BARE_UNITS = (  # те же единицы без числа: «скорость в км/ч»
    (re.compile(r"(?<!\w)мм\s?рт\.\s?ст\."), ("миллиметры ртутного столба", "millimeters of mercury")),
    (re.compile(r"(?<!\w)км/ч(?!\w)"), ("километры в час", "kilometers per hour")),
    (re.compile(r"(?<!\w)м/с(?!\w)"), ("метры в секунду", "meters per second")),
)
# Единственная таблица одиночных знаков: символ -> (RU, EN); подставляется словом в пробелах.
# Знаки при числе (°, %, валюты, ′ ″) читаются через _UNITS; здесь — их запасной вид без числа.
_SIGNS = {
    ">": ("больше", "greater than"),
    "<": ("меньше", "less than"),
    "≥": ("больше или равно", "greater than or equal to"),
    "≤": ("меньше или равно", "less than or equal to"),
    "≠": ("не равно", "not equal to"),
    "=": ("равно", "equals"),
    "≈": ("примерно", "approximately"),
    "~": ("примерно", "approximately"),
    "±": ("плюс-минус", "plus or minus"),
    "+": ("плюс", "plus"),
    "−": ("минус", "minus"),
    "×": ("умножить на", "times"),
    "÷": ("разделить на", "divided by"),
    "^": ("в степени", "to the power of"),
    "→": ("стрелка", "arrow"),
    "#": ("решётка", "hash"),
    "№": ("номер", "number"),
    "§": ("параграф", "section"),
    "@": ("собака", "at"),
    "&": ("и", "and"),
    "*": ("звёздочка", "star"),
    "/": ("слэш", "slash"),
    "\\": ("обратный слэш", "backslash"),
    "|": (",", ","),  # вертикальная черта — пауза
    "_": ("", ""),  # подчёркивание — пробел
    "$": ("долларов", "dollars"),
    "€": ("евро", "euros"),
    "£": ("фунтов", "pounds"),
    "₽": ("рублей", "rubles"),
    "%": ("процентов", "percent"),
    "‰": ("промилле", "per mille"),
    "°": ("градусов", "degrees"),
    "′": ("минут", "minutes"),
    "″": ("секунд", "seconds"),
}
_SIGN_WORDS = {"-": ("минус", "minus"), "−": ("минус", "minus"), "+": ("плюс", "plus")}


@lru_cache(maxsize=1)
def _morph():
    from pymorphy3 import MorphAnalyzer
    return MorphAnalyzer()


@lru_cache(maxsize=None)
def _gender(lemma):
    return {"masc": "m", "femn": "f", "neut": "n"}.get(_morph().parse(lemma)[0].tag.gender, "m")


def _unit_spec(text):
    for pattern, spec in _UNITS:
        if re.fullmatch(pattern, text):
            return spec
    return None


def _noun(spec, number, is_int, lang, genitive=False):
    """Единица, согласованная с числом (RU — pymorphy3, EN — ед./мн.). genitive — после «от/до»:
    единица в родительном падеже (оканчивается на 1, кроме 11, — ед., иначе мн.)."""
    lemma, tail, one, many, tail_en = spec
    if lang == "en":
        return " ".join(w for w in (one if is_int and number == 1 else many, tail_en) if w)
    parsed = _morph().parse(lemma)[0]
    if genitive:
        single = number % 10 == 1 and number % 100 != 11
        form = parsed.inflect({"gent", "sing" if single else "plur"})
        word = form.word if form else lemma
    else:
        word = parsed.make_agree_with_number(number if is_int else 2).word
    return " ".join(w for w in (word, tail) if w)


def _num(number, lang, gender="m", genitive=False):
    if lang == "en":
        return num2words(number, lang="en")
    kwargs = {"gender": gender}
    if genitive:
        kwargs["case"] = "genitive"
    return num2words(number, lang="ru", **kwargs)


def _signed(sign, words, lang):
    return f"{_SIGN_WORDS[sign][0 if lang == 'ru' else 1]} {words}" if sign else words


def _noun_gender(number, is_int, rest, lang):
    """Род числительного 1/2 по следующему слову: «1 минута» -> «одна», «2 окна» -> «два»
    остаётся мужским, «21 окно» -> «одно»."""
    if lang != "ru" or not is_int or number % 10 not in (1, 2) or number % 100 in (11, 12):
        return "m"
    word = re.match(r"\s([а-яё]+)", rest, re.IGNORECASE)
    if not word:
        return "m"
    parsed = _morph().parse(word.group(1).lower())[0]
    if "NOUN" not in parsed.tag:
        return "m"
    gender = _gender(parsed.normal_form)
    return gender if gender == "f" or (gender == "n" and number % 10 == 1) else "m"


def _quantity(match, lang):
    int_s, frac_s = match.group("int"), match.group("frac")
    is_int = frac_s is None
    number = int(int_s) if is_int else float(f"{int_s}.{frac_s}")
    unit = match.group("unit") or match.group("pre")
    spec = _unit_spec(unit) if unit else None
    gender = _gender(spec[0]) if spec else _noun_gender(number, is_int, match.string[match.end():], lang)
    words = _signed(match.group("sign"), _num(number, lang, gender), lang)
    if spec:
        words = f"{words} {_noun(spec, number, is_int, lang)}"
    return words


def _range(match, lang):
    spec = _unit_spec(match.group("unit")) if match.group("unit") else None
    gender = _gender(spec[0]) if spec else "m"
    parts = []
    for name in ("a", "b"):
        raw = match.group(name)
        number = int(raw.lstrip("-−+"))
        sign = raw[0] if raw[0] in "-−+" else None
        parts.append((number, _signed(sign, _num(number, lang, gender, genitive=True), lang)))
    text = f"from {parts[0][1]} to {parts[1][1]}" if lang == "en" else f"от {parts[0][1]} до {parts[1][1]}"
    if spec:
        text += " " + _noun(spec, parts[1][0], True, lang, genitive=True)
    return text


def _time(match, lang):
    hours, minutes = int(match.group(1)), match.group(2)
    words = num2words(hours, lang=lang)
    if minutes == "00":
        if lang == "en":
            return f"{words} o'clock"
        return f"{words} {_morph().parse('час')[0].make_agree_with_number(hours).word}"
    zero = ("oh " if lang == "en" else "ноль ") if minutes[0] == "0" else ""
    return f"{words} {zero}{num2words(int(minutes), lang=lang)}"


def _prepare(part, lang):
    index = 0 if lang == "ru" else 1
    part = _INVISIBLE.sub("", part)
    part = _THOUSANDS.sub(lambda m: re.sub(r"\D", "", m.group(0)), part)
    part = _SPACES.sub(" ", part)
    part = _QUOTES.sub("", part)
    part = _DECOR.sub("", part)
    part = _DOTTED.sub(lambda m: m.group(0).replace(".", " "), part)
    part = _SLASH_DIGITS.sub(" дробь " if lang == "ru" else " over ", part)
    part = _FRACTION.sub(
        lambda m: (f"{m.group(1)} {'и' if lang == 'ru' else 'and'} " if m.group(1) else "") + _FRACTIONS[m.group(2)][index],
        part)
    part = _TIME.sub(lambda m: _time(m, lang), part)
    part = _RANGE.sub(lambda m: _range(m, lang), part)
    part = _QUANTITY.sub(lambda m: _quantity(m, lang), part)
    for pattern, words in _BARE_UNITS:
        part = pattern.sub(words[index], part)
    part = re.sub("[²³]", lambda m: " " + _POWERS[m.group(0)][index], part)
    return "".join(f" {_SIGNS[ch][index]} " if ch in _SIGNS else ch for ch in part)


def spoken_words(text, lang="ru"):
    """Числа и знаки словами (галочка «Знаки и числа произносить словами»):
    num2words превращает числа (отрицательные, дробные, диапазоны, время), pymorphy3
    согласует единицы с числом, знаки — таблица _SIGNS; эмодзи и декор удаляются.
    Разметка <speaker …>/<[domain …]> не трогается — числа внутри тегов
    остаются: движки читают параметры разметки сами."""
    lang = lang if lang in ("ru", "en") else "ru"
    result = [part if i % 2 else _prepare(part, lang) for i, part in enumerate(_TAG.split(text or ""))]
    joined = re.sub(r"\s+", " ", "".join(result)).strip()
    return re.sub(r"\s+([,.!?;:…])", r"\1", joined)  # пробел перед пунктуацией — от замен знаков


def tts_text(runtime, text):
    """Текст для синтеза с учётом галочки «Знаки и числа произносить словами»
    (voice_stack.speak_words): выключена — текст без изменений."""
    stack = (getattr(runtime, "settings", None) or {}).get("voice_stack") or {}
    if stack.get("speak_words"):
        return spoken_words(text)
    return text


# ── граница первого предложения стримингового ответа ─────────────────────────
# Минимальная длина фразы для предпрогрева: короче — синтез отдельно не имеет
# смысла (обычный путь озвучит реплику целиком).
FIRST_SENTENCE_MIN = 20
_ABBREV = ("т.е.", "т.д.", "т.п.", "др.", "пр.", "им.", "г.", "ул.", "стр.", "рис.",
           "млн", "млрд", "руб", "у.", "м.")
_TERMINATORS = "!?"


def first_sentence_end(text, start=0, min_length=FIRST_SENTENCE_MIN):
    """Индекс конца первого предложения в text (после start) или None.

    Границы: . ! ? … и переводы строки, за которыми пробел или конец текста.
    Не режет: десятичные числа (3.5), сокращения (т.е. — точка без пробела),
    адреса (точка между буквами)."""
    buffer = text[start:]
    for i, ch in enumerate(buffer):
        if ch == chr(10):
            return start + i + 1
        if ch in "!?":
            return start + i + 1
        if ch == "…":
            return start + i + 1
        if ch == ".":
            nxt = buffer[i + 1:i + 2]
            word = buffer[max(0, buffer.rfind(" ", 0, i) + 1):i + 1].lower()
            if word in _ABBREV:
                continue
            if i + 1 < min_length:
                continue  # короткая фраза в начале — отдельный синтез не имеет смысла
            return start + i + 1
    return None
