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
SLOW_REPLY_MS = 3000  # ответ позже — запись «Медленный ответ» в журнал сервера (этап назван)

# этапы хода: (имя, событие начала, событие конца); считаются по первым вхождениям событий
STAGES = (("endpoint", "asr_partial_last", "endpoint"),   # тишина до решения «договорил»
          ("asr", "endpoint", "asr_final"),               # финализация распознавания
          ("scenario", "scenario_start", "scenario_done"),  # выбор и выполнение сценария
          ("model", "model_start", "model_done"),         # ответ модели (нет сценария)
          ("tts", "tts_start", "tts_done"),               # синтез озвучки
          ("send", "tts_done", "tts_audio"))              # отправка звука колонке
WORK_STAGES = ("scenario", "model", "tts", "send")  # «самый долгий» — среди работы сервера, не паузы речи
STAGE_LABELS = {"endpoint": "пауза до конца фразы", "asr": "финал ASR", "scenario": "сценарий",
                "model": "модель", "tts": "синтез TTS", "send": "отправка звука"}


class SpeechTrace:
    """Один проход распознавания/ответа. sink — callable(dict), контекст
    (session/sn/device_id) — общий словарь соединения, живёт с ним."""

    def __init__(self, sink, context=None, run_id=None):
        self.sink = sink
        self.run = run_id or uuid.uuid4().hex[:12]
        self.context = context if context is not None else {}
        self.started = time.monotonic()
        self.counts = {}
        self.marks = {}  # имя события → t_ms первого вхождения (asr_partial_last — последнего)
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
        self.marks.setdefault(name, record["t_ms"])
        if name == "asr_partial":
            self.marks["asr_partial_last"] = record["t_ms"]
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


    def timing(self):
        """Этапы хода в мс: {этап: длительность, ..., "reply": от конца фразы до первого ответа}.
        Этап без обоих событий пропускается. reply — то, что ждёт человек после «договорил»:
        от решения об окончании речи (для текстового хода — от начала) до отправки ответа."""
        marks, result = self.marks, {}
        for name, begin, end in STAGES:
            if begin in marks and end in marks and marks[end] >= marks[begin]:
                result[name] = round(marks[end] - marks[begin], 1)
        answered = [marks[key] for key in ("alice_response", "scenario_handled", "server_action_handled")
                    if key in marks]
        if answered:
            result["reply"] = round(min(answered) - marks.get("endpoint", 0.0), 1)
        return result

    def report(self):
        """Итог хода: событие turn_timing в трассе и (мс этапов, самый долгий этап) для журнала сервера;
        None — этапов нет (служебный ход)."""
        stages = self.timing()
        if "reply" not in stages or not any(name in stages for name in WORK_STAGES):
            return None  # служебные колбэки без сценария, модели и озвучки в журнал не идут
        slowest = max((name for name in stages if name in WORK_STAGES), key=stages.get, default=None)
        self.event("turn_timing", "server", slowest=slowest, **{name + "_ms": ms for name, ms in stages.items()})
        return stages, slowest


def timing_message(stages):
    """Строка журнала: «Ответ через 3.8 с: сценарий 3.7 с, синтез TTS 0.1 с»."""
    parts = [f"{STAGE_LABELS[name]} {ms / 1000:.1f} с" for name, ms in stages.items() if name in STAGE_LABELS]
    head = "Медленный ответ" if stages["reply"] >= SLOW_REPLY_MS else "Ответ"
    return f"{head} через {stages['reply'] / 1000:.1f} с" + (": " + ", ".join(parts) if parts else "")


def reason_name(reason):
    return CLOSE_REASONS.get(reason, str(reason))
