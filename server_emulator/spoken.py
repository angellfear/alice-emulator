"""Текст для синтеза речи и пределы ожидания синтеза."""
import re

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
