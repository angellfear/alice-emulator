"""Автоматическая регулировка уровня звука колонки перед распознаванием.

Микрофон колонки отдаёт речь очень тихо (пик около −50 dBFS), для ASR это
пониженная точность. Усиление следует за пиковой огибающей: растёт, пока
реплика тихая, падает мгновенно при громком кадре. Готовых потоковых AGC с
колёсами под Windows нет (webrtc-noise-gain собирается из исходников), поэтому
регулировка своя — около тридцати строк numpy.

Колёса (настройки stt, читает asr_pseudo_stream.gain_config): pre_gain_db —
предусиление микрофона до огибающей (тихий микрофон ниже порога тишины
NOISE_PEAK поднимается до него), target — целевой пик огибающей (линейный,
пересчитан из dBFS). Итоговое усиление последнего кадра — gain_db (дБ), его
читает метр audio_meter."""
import math

import numpy as np

RATE = 8000                    # частота потока в ASR по умолчанию (T-one)
TARGET_PEAK = 0.5              # целевой пик огибающей (−6 dBFS)
MAX_GAIN = 300.0               # потолок усиления (~+50 dB): тишина не раздувается до шума
RELEASE = 0.5 ** (1 / 300)     # огибающая убывает вдвое за 3 с
NOISE_PEAK = 0.0005            # ниже этого пика кадр — тишина, огибающую не поднимает


class AutoGain:
    """Потоковая нормализация float32-кадров: process(bytes) → bytes той же длины.
    pre_gain_db — предусиление до огибающей, target — целевой пик (линейный)."""

    def __init__(self, rate=RATE, pre_gain_db=0.0, target=TARGET_PEAK):
        self.frame = rate // 100  # кадр 10 мс: RELEASE отсчитан в кадрах по 10 мс
        self.pre = 10.0 ** (float(pre_gain_db) / 20)
        self.target = float(target)
        self.level = 0.0
        self.tail = np.empty(0, np.float32)
        self.gain_db = 0.0  # усиление последнего кадра (предусиление + огибающая), дБ

    def process(self, data):
        samples = np.concatenate((self.tail, np.frombuffer(data, dtype="<f4")))
        whole = len(samples) // self.frame * self.frame
        self.tail = samples[whole:].copy()
        out = np.empty(whole, np.float32)
        for start in range(0, whole, self.frame):
            frame = samples[start:start + self.frame] * self.pre
            peak = float(np.abs(frame).max())
            self.level = max(peak if peak >= NOISE_PEAK else 0.0, self.level * RELEASE)
            gain = min(MAX_GAIN, self.target / self.level) if self.level else 1.0
            out[start:start + self.frame] = frame * gain
            self.gain_db = round(20 * math.log10(gain * self.pre), 1)
        return np.tanh(out).astype("<f4").tobytes()

    def flush(self):
        """Хвост, не набравший кадра, — без усиления вместе с последним коэффициентом."""
        tail, self.tail = self.tail, np.empty(0, np.float32)
        return tail.astype("<f4").tobytes()
