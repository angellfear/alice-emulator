"""Метр микрофона перед ASR: audio_meter (уровни dBFS, окна, история), колёса
выравнивания audio_gain (предусиление, цель нормализации) и их чтение из
настроек stt (asr_pseudo_stream.gain_config), запись уровней из transcode и
домен api/voice-levels. Звук — синтетический тон, микрофон не нужен."""
import asyncio
import json
import math

import numpy as np
import pytest
from aiohttp.test_utils import make_mocked_request

from server_emulator import asr_pseudo_stream, audio_meter
from server_emulator.audio_gain import AutoGain
from server_emulator.audio_meter import LevelMeter, to_db
from server_emulator.audio_stream import transcode


class _Runtime:
    def __init__(self, settings):
        self.settings = settings


def _tone(seconds, rate=16000, amp=0.5):
    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    return amp * np.sin(2 * np.pi * 440 * t)


def _pcm16(samples):
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


async def _chunks(data, size=3840):  # 40 мс на кусок 48 кГц (2 байта на сэмпл)
    for pos in range(0, len(data), size):
        yield data[pos:pos + size]


def test_to_db():
    """Линейный уровень → dBFS: полный масштаб 0, тон 0.5 ≈ −6, тишина и уровень
    тише SILENCE_DB — SILENCE_DB (в JSON −∞ не бывает)."""
    assert to_db(1.0) == 0.0
    assert to_db(0.5) == pytest.approx(-6.0, abs=0.1)
    assert to_db(0.0) == audio_meter.SILENCE_DB
    assert to_db(1e-12) == audio_meter.SILENCE_DB


def test_meter_measures_tone_levels():
    """Тон 0.5 в окно: пик −6 dBFS, RMS −9 dBFS (0.5/√2); вход и выход совпадают,
    пока усиление выключено (gain — None), метр «живой»."""
    meter = LevelMeter()
    tone = _tone(0.05)
    for _ in range(4):
        meter.observe_in(tone)
        meter.observe_out(tone)
    snap = meter.snapshot()
    assert snap["in"]["peak"] == pytest.approx(-6.0, abs=0.2)
    assert snap["in"]["rms"] == pytest.approx(-9.0, abs=0.3)
    assert snap["out"]["peak"] == pytest.approx(-6.0, abs=0.2)
    assert snap["gain"] is None and snap["active"] is True


def test_meter_counts_clip():
    """Сэмплы |x| ≥ CLIP считаются ограничением сигнала: вход и выход по отдельности."""
    meter = LevelMeter()
    meter.observe_in(np.full(100, 1.0, dtype=np.float32))
    meter.observe_out(np.full(1600, 0.991, dtype=np.float32), 0.0)
    snap = meter.snapshot()
    assert snap["in"]["clip"] == 100 and snap["out"]["clip"] == 1600
    assert snap["out"]["peak"] == pytest.approx(-0.1, abs=0.05)  # 0.991 → −0.08 dBFS


async def test_meter_history_and_silence_gap():
    """Заполненные окна уходят в историю (запись по 100 мс реального времени);
    звук старше active_s — метр не «живой»."""
    meter = LevelMeter()
    for _ in range(3):
        meter.observe_in(_tone(0.02))
        await asyncio.sleep(0.12)
    snap = meter.snapshot()
    assert len(snap["history"]) >= 2
    assert snap["history"][0]["i"] is not None  # в первом окне был тон
    await asyncio.sleep(meter.active_s + 0.05)
    assert meter.snapshot()["active"] is False


def test_autogain_reaches_target_with_preamp():
    """Тихий тон 0.01 с предусилением +6 дБ: огибающая держит пик около цели 0.5
    (tanh смягчает верх), gain_db — итоговое усиление (предусиление + огибающая)."""
    gain = AutoGain(16000, pre_gain_db=6)
    out = np.frombuffer(gain.process(_tone(1.0, amp=0.01).astype("<f4").tobytes()), dtype="<f4")
    assert float(np.abs(out[-8000:]).max()) == pytest.approx(0.5, abs=0.06)
    # итоговое усиление — target/amp (34 дБ): предусиление входит в огибающую,
    # так что AGC-часть уменьшается, сумма — та же
    assert gain.gain_db == pytest.approx(20 * math.log10(0.5 / 0.01), abs=2.0)


def test_autogain_silence_keeps_preamplifier():
    """Тишина: огибающую тишина не поднимает (порог NOISE_PEAK), выход усилён только
    предусилением — gain_db равен ему."""
    gain = AutoGain(16000, pre_gain_db=12)
    gain.process(np.zeros(16000, dtype="<f4").tobytes())
    assert gain.gain_db == pytest.approx(12.0)


def test_gain_config_reads_and_clamps_stt_settings():
    """Колёса из stt: числа в диапазоне — берутся (цель dBFS → линейный пик); ключа
    нет, не число или вне диапазона — умолчания 0 дБ и −6 dBFS, без ошибки."""
    default = {"pre_gain_db": 0.0, "target": pytest.approx(10 ** (-6 / 20), abs=1e-4)}
    assert asr_pseudo_stream.gain_config(_Runtime({})) == default
    cfg = asr_pseudo_stream.gain_config(
        _Runtime({"stt": {"pre_gain_db": 12, "level_target_dbfs": -12}}))
    assert cfg["pre_gain_db"] == 12.0
    assert cfg["target"] == pytest.approx(10 ** (-12 / 20), abs=1e-4)
    for stt in ({"pre_gain_db": "abc", "level_target_dbfs": 99},
                {"pre_gain_db": 30, "level_target_dbfs": -30}):
        assert asr_pseudo_stream.gain_config(_Runtime({"stt": stt})) == default


async def test_transcode_feeds_meter(monkeypatch):
    """transcode пишет уровни до и после усиления: тихий тон усиливается («В ASR» громче
    входа, усиление видно); без выравнивания вход пишется как есть, gain — None."""
    own = LevelMeter()
    monkeypatch.setattr(audio_meter, "LEVELS", own)
    async for _ in transcode(_chunks(_pcm16(_tone(0.4, rate=48000, amp=0.01))),
                             "audio/x-pcm;bit=16;rate=48000;channels=1", "pcm16",
                             level=True, level_gain={"pre_gain_db": 0, "target": 0.5}):
        pass
    snap = own.snapshot()
    assert snap["out"]["peak"] > snap["in"]["peak"] > audio_meter.SILENCE_DB + 20  # тон усилен
    assert snap["gain"] is not None and snap["gain"] > 20
    own = LevelMeter()
    monkeypatch.setattr(audio_meter, "LEVELS", own)
    async for _ in transcode(_chunks(_pcm16(_tone(0.4, rate=48000, amp=0.3))),
                             "audio/x-pcm;bit=16;rate=48000;channels=1", "pcm16", level=False):
        pass
    snap = own.snapshot()
    assert snap["in"]["peak"] == pytest.approx(-10.5, abs=0.5)  # 0.3 → −10.5 dBFS
    assert snap["out"]["peak"] == snap["in"]["peak"] and snap["gain"] is None


async def test_voice_levels_endpoint(tmp_path):
    """GET api/voice-levels — снапшот метра: активность, усиление, окна и история."""
    from server_emulator.runtime import Runtime
    from server_emulator.ui.api.voice_stack import voice_levels
    runtime = Runtime(tmp_path)
    try:
        audio_meter.LEVELS.observe_in(_tone(0.05))
        response = await voice_levels(make_mocked_request("GET", "/emulator/api/voice-levels"),
                                      runtime, None)
        assert response.status == 200
        payload = json.loads(response.body)
        assert payload["active"] is True
        assert payload["in"]["rms"] is not None
        assert {"t", "i", "ip", "ic", "o", "op", "oc", "g"} <= set(payload["history"][-1])
    finally:
        await runtime.close()
