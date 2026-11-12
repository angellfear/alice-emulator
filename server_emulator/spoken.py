"""Текст для синтеза речи и пределы ожидания синтеза."""
import re

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
# «семь целых ноль десятых»), знаки — таблица замен, RU/EN по языку ответа.
# Применяется только в тракте TTS: текст карточки и журнал остаются как есть.

_TAG = re.compile(r"(<[^>]+>)")  # группа захвата — теги остаются в split-частях
_NUMBER = re.compile(r"(?:-|−)?\d+(?:[.,]\d+)?")
_SIGNS = {
    ">": ("больше", "greater than"),
    "<": ("меньше", "less than"),
    "#": ("решётка", "hash"),
    "№": ("номер", "number"),
    "$": ("долларов", "dollars"),
    "€": ("евро", "euros"),
    "₽": ("рублей", "rubles"),
    "%": ("процентов", "percent"),
    "°": ("градусов", "degrees"),
    "&": ("и", "and"),
    "+": ("плюс", "plus"),
    "=": ("равно", "equals"),
}


def _number_words(match, lang="ru"):
    value = match.group(0)
    negative = value[0] in "-−"
    digits = value.lstrip("-−")
    number = float(digits.replace(",", ".")) if any(c in digits for c in ",.") else int(digits)
    return num2words(-number if negative else number, lang=lang)


def spoken_words(text, lang="ru"):
    """Числа и знаки словами (галочка «Знаки и числа произносить словами»):
    num2words превращает числа (в т.ч. отрицательные и дробные), знаки — таблица
    _SIGNS. Разметка <speaker …>/<[domain …]> не трогается — числа внутри тегов
    остаются: движки читают параметры разметки сами."""
    lang = lang if lang in ("ru", "en") else "ru"
    result = []
    for i, part in enumerate(_TAG.split(text or "")):
        if i % 2:  # сам тег — без изменений
            result.append(part)
            continue
        part = _NUMBER.sub(lambda m: _number_words(m, lang), part)
        part = "".join(f" {_SIGNS[ch][0 if lang == 'ru' else 1]} " if ch in _SIGNS else ch for ch in part)
        result.append(part)
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
