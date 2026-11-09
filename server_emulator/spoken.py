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
