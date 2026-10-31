"""v9 §2: структурированная трассировка речевого тракта.

Единый run_id проходит весь проход (VoiceInput → аудио → ASR → модель → TTS →
канал → Close). Направление события фиксируется явно (c2s/s2c/server), чтобы
клиентское NO_SPEECH не выдавалось за решение сервера и наоборот. Трасса —
наблюдение, не управление: ошибки тракта в неё записываются, поведение не меняют.
"""
import time
import uuid

# Имена enum CloseReason из docs/contracts/aapi.md
CLOSE_REASONS = {0: "OK", 1: "INTERNAL_ERROR", 2: "STOP_RECORDING",
                 3: "AUDIO_SOURCE_STOPPED", 4: "CANCEL", 5: "NO_SPEECH"}
DIRECTIONS = ("c2s", "s2c", "server")
SPEECH_LOG_LIMIT = 300


class SpeechTrace:
    """Один проход распознавания/ответа. sink — callable(dict), контекст
    (session/sn/device_id) — общий словарь соединения, живёт с ним."""

    def __init__(self, sink, context=None, run_id=None):
        self.sink = sink
        self.run = run_id or uuid.uuid4().hex[:12]
        self.context = context if context is not None else {}
        self.started = time.monotonic()
        self.counts = {}
        self._first_partial_emitted = False

    def event(self, name, direction, **fields):
        if direction not in DIRECTIONS:
            raise ValueError("направление трассы: c2s/s2c/server")
        record = {"run": self.run, "name": name, "direction": direction,
                  "t_ms": round((time.monotonic() - self.started) * 1000, 1)}
        for key, value in self.context.items():
            if value is not None:
                record[key] = value
        record.update(fields)
        self.sink(record)
        return record

    def bump(self, counter, amount=1):
        self.counts[counter] = self.counts.get(counter, 0) + amount

    def snapshot(self):
        """Счётчики прохода (аудио-байты и пр.) для терминальных событий."""
        return dict(self.counts)

    def asr_partial(self, text):
        first = not self._first_partial_emitted
        self._first_partial_emitted = True
        self.event("asr_partial", "s2c", first=first, text_len=len(text))


def reason_name(reason):
    return CLOSE_REASONS.get(reason, str(reason))
