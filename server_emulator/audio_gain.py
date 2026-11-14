"""Автоматическая регулировка уровня звука колонки перед распознаванием.

Микрофон колонки отдаёт речь очень тихо (пик около −50 dBFS), для ASR это
пониженная точность. Усиление следует за пиковой огибающей: растёт, пока
реплика тихая, падает мгновенно при громком кадре. Готовых потоковых AGC с
колёсами под Windows нет (webrtc-noise-gain собирается из исходников), поэтому
регулировка своя — около тридцати строк numpy."""
import numpy as np

RATE = 8000                    # частота потока в ASR по умолчанию (T-one)
TARGET_PEAK = 0.5              # целевой пик огибающей (−6 dBFS)
MAX_GAIN = 300.0               # потолок усиления (~+50 dB): тишина не раздувается до шума
RELEASE = 0.5 ** (1 / 300)     # огибающая убывает вдвое за 3 с
NOISE_PEAK = 0.0005            # ниже этого пика кадр — тишина, огибающую не поднимает


class AutoGain:
    """Потоковая нормализация float32-кадров: process(bytes) → bytes той же длины."""

    def __init__(self, rate=RATE):
        self.frame = rate // 100  # кадр 10 мс: RELEASE отсчитан в кадрах по 10 мс
        self.level = 0.0
        self.tail = np.empty(0, np.float32)

    def process(self, data):
        samples = np.concatenate((self.tail, np.frombuffer(data, dtype="<f4")))
        whole = len(samples) // self.frame * self.frame
        self.tail = samples[whole:].copy()
        out = np.empty(whole, np.float32)
        for start in range(0, whole, self.frame):
            frame = samples[start:start + self.frame]
            peak = float(np.abs(frame).max())
            self.level = max(peak if peak >= NOISE_PEAK else 0.0, self.level * RELEASE)
            gain = min(MAX_GAIN, TARGET_PEAK / self.level) if self.level else 1.0
            out[start:start + self.frame] = frame * gain
        return np.tanh(out).astype("<f4").tobytes()

    def flush(self):
        """Хвост, не набравший кадра, — без усиления вместе с последним коэффициентом."""
        tail, self.tail = self.tail, np.empty(0, np.float32)
        return tail.astype("<f4").tobytes()
