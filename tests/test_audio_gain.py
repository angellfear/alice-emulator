"""Нормализация громкости звука колонки перед распознаванием."""
import numpy as np

from server_emulator.audio_gain import AutoGain, FRAME


def tone(amplitude, seconds):
    t = np.arange(int(8000 * seconds)) / 8000
    return (np.sin(2 * np.pi * 300 * t) * amplitude).astype("<f4")


def run(gain, samples):
    out = gain.process(samples.tobytes()) + gain.flush()
    return np.frombuffer(out, dtype="<f4")


def test_quiet_speech_is_raised_to_target_level():
    out = run(AutoGain(), tone(0.002, 2))
    assert len(out) == 16000
    assert 0.4 < np.abs(out[-1600:]).max() < 0.55


def test_silence_is_not_inflated():
    out = run(AutoGain(), np.full(8000, 1e-5, dtype="<f4"))
    assert np.abs(out).max() < 0.01


def test_loud_audio_is_limited_and_state_survives_chunks():
    gain = AutoGain()
    samples = tone(0.9, 1)
    parts = [gain.process(samples[i:i + 123].tobytes()) for i in range(0, len(samples), 123)]
    out = np.frombuffer(b"".join(parts) + gain.flush(), dtype="<f4")
    assert len(out) == len(samples)
    assert np.abs(out).max() <= 0.62
    assert FRAME == 80
