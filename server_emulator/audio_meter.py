"""Живой измеритель уровней микрофона перед ASR — данные для метра вкладки
«Голосовой стек». Панель показывает «Вход» — микрофон колонки как есть — и
«В ASR» — после предусиления и выравнивания, то, что слышит распознавание.

Уровни снимает transcode (audio_stream.py) в точке выравнивания: сюда пишутся
RMS и пик каждого куска до и после audio_gain.AutoGain. Окно 100 мс: RMS —
среднеквадратический уровень (громкость речи), пик — максимум |сэмпла| (запас
до ограничения). Единица — dBFS: 0 — полный масштаб, тишина −∞; в JSON тише
SILENCE_DB не бывает. Сэмплы |x| ≥ CLIP считаются ограничением сигнала
(выход AutoGain смягчается tanh, но перегруз виден и там).

LEVELS — реестр процесса (как providers.PROVIDERS): аудиотракт не знает ни
runtime, ни API и пишет в него; домен ui/api/voice_stack.py отдаёт snapshot.
Несколько одновременных потоков ASR пишут в одно окно — метр показывает самый
свежий звук, а не сумму микрофонов. История — последние окна (HISTORY_S) для
полосы уровней в UI; окно без кусков не пишется — пауза в звуке видна как
разрыв. Чтение (snapshot) состояние не меняет: опросы UI данные не съедают.
"""
import collections
import math
import time

import numpy as np

WINDOW_S = 0.1      # окно измерения: RMS и пик за ~100 мс
HISTORY_S = 10      # глубина истории для полосы уровней UI, секунд
ACTIVE_S = 0.3      # звук свежее этого — метр «живой», иначе рисуется тишина
SILENCE_DB = -100.0  # в JSON тише этого не бывает (dBFS −∞)
SOUND_PEAK = 0.001  # пик выше этого — окно считается звуком (−60 dBFS)
CLIP = 0.99         # |сэмпл| выше — ограничение сигнала


def to_db(value):
    """Линейный уровень → dBFS (0 — полный масштаб); тише SILENCE_DB не бывает."""
    return max(SILENCE_DB, round(20 * math.log10(value), 1)) if value > 0 else SILENCE_DB


def _stats(x):
    """(сумма квадратов, сэмплов, пик, сэмплов ограничения) по куску."""
    if len(x) == 0:
        return 0.0, 0, 0.0, 0
    x = np.asarray(x, dtype=np.float32)
    absolute = np.abs(x)
    return (float(np.dot(x.astype(np.float64), x.astype(np.float64))), int(len(x)),
            float(absolute.max()), int(np.count_nonzero(absolute >= CLIP)))


class LevelMeter:
    """Окна уровней и их история: observe_* пишет, snapshot читает (для UI)."""

    def __init__(self, window=WINDOW_S, history_s=HISTORY_S, active_s=ACTIVE_S):
        self.window = window
        self.active_s = active_s
        self.history = collections.deque(maxlen=max(1, round(history_s / window)))
        self.gain_db = None  # текущее усиление выравнивания (дБ); None — выключено
        self.last_sound = 0.0  # monotonic последнего куска со звуком
        self._start()

    def _start(self):
        self._t0 = time.monotonic()
        self._in = [0.0, 0, 0.0, 0]  # сумма квадратов, сэмплов, пик, ограничение
        self._out = [0.0, 0, 0.0, 0]
        self._gain = None

    def _seal(self):
        """Заполненное или запаздывающее окно — в историю; новое начинается сразу.
        Окно без кусков не пишется: пауза в звуке остаётся разрывом истории."""
        if time.monotonic() - self._t0 < self.window:
            return
        entry = {"t": round(self._t0, 3), "g": self._gain}
        for key, acc in (("i", self._in), ("o", self._out)):
            sum_sq, count, peak, clip = acc
            entry[key] = to_db(math.sqrt(sum_sq / count)) if count else None
            entry[key + "p"] = to_db(peak) if peak > 0 else None
            entry[key + "c"] = clip
        self.history.append(entry)
        self._start()

    def observe_in(self, x):
        """Кусок микрофона до усиления (вызывает transcode)."""
        self._seal()
        sum_sq, count, peak, clip = _stats(x)
        self._in[0] += sum_sq
        self._in[1] += count
        self._in[2] = max(self._in[2], peak)
        self._in[3] += clip
        if peak >= SOUND_PEAK:
            self.last_sound = time.monotonic()

    def observe_out(self, x, gain_db=None):
        """Кусок после усиления — то, что получает ASR. gain_db — итоговое усиление."""
        self._seal()
        self.gain_db = None if gain_db is None else round(float(gain_db), 1)
        self._gain = self.gain_db
        sum_sq, count, peak, clip = _stats(x)
        self._out[0] += sum_sq
        self._out[1] += count
        self._out[2] = max(self._out[2], peak)
        self._out[3] += clip

    def snapshot(self):
        """Текущее окно и история: in/out — {rms, peak, clip} в dBFS, gain — усиление
        выравнивания, active — звук свежий; история — окна {t, i/o (RMS), ip/op (пик),
        ic/oc (ограничение), g (усиление)}. Окно с несколькими кусками — RMS по всей
        суммарной энергии, пик — максимум."""
        now = time.monotonic()

        def level(acc):
            sum_sq, count, peak, clip = acc
            return {"rms": to_db(math.sqrt(sum_sq / count)) if count else None,
                    "peak": to_db(peak) if peak > 0 else None, "clip": clip}

        return {"now": round(now, 3),
                "active": now - self.last_sound <= self.active_s,
                "gain": self.gain_db,
                "in": level(self._in), "out": level(self._out),
                "history": list(self.history)}


LEVELS = LevelMeter()  # реестр процесса: transcode пишет, ui/api/voice_stack.py читает
