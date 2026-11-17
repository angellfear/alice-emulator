"""Метр микрофона перед ASR: audio_meter (уровни dBFS, окна, история, хаб метров
по колонкам, запись измерения), колёса выравнивания audio_gain (предусиление,
цель нормализации) и их чтение из настроек stt (профиль колонки stt.profiles[sn]
сильнее общих), запись уровней из transcode и input_audio, домен api/voice-levels
(?sn= — метр колонки) и автонастройка api/voice-autotune. Звук — синтетический
тон, микрофон не нужен."""
import asyncio
import json
import math
import time

import numpy as np
import pytest
from aiohttp.test_utils import make_mocked_request

from server_emulator import asr_pseudo_stream, audio_meter
from server_emulator.audio_gain import AutoGain
from server_emulator.audio_meter import LevelMeter, MeterHub, to_db
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


def test_autogain_gate_ignores_preamplified_noise():
    """Гейт тишины — в сырых сэмплах: предусиление поднимает шум над порогом,
    но огибающую он не открывает (иначе тишина усиливается до цели)."""
    gain = AutoGain(16000, pre_gain_db=12)
    noise = np.full(16000, 0.0003, dtype=np.float32)  # пик ниже NOISE_PEAK в сыром сигнале
    out = np.frombuffer(gain.process(noise.astype("<f4").tobytes()), dtype="<f4")
    assert float(np.abs(out).max()) == pytest.approx(0.0003 * 10 ** (12 / 20), rel=0.02)
    assert gain.gain_db == pytest.approx(12.0)  # только предусиление, огибающая спит


def test_gain_config_profile_overrides_global():
    """Профиль колонки stt.profiles[sn] сильнее общих колёс; у колонки с пустым
    профилем или вовсе без профиля — общие; level() читает профиль так же."""
    runtime = _Runtime({"stt": {"pre_gain_db": 3, "level_target_dbfs": -9,
                                "profiles": {"C00T1000TESTK": {"pre_gain_db": 7, "level_target_dbfs": -12,
                                                               "level": "no"},
                                             "C00T2000TESTK": {}}}})
    cfg = asr_pseudo_stream.gain_config(runtime, "C00T1000TESTK")
    assert cfg["pre_gain_db"] == 7.0
    assert cfg["target"] == pytest.approx(10 ** (-12 / 20), abs=1e-4)
    assert asr_pseudo_stream.gain_config(runtime, "C00T2000TESTK")["pre_gain_db"] == 3.0
    assert asr_pseudo_stream.gain_config(runtime, "C00T3000TESTK")["pre_gain_db"] == 3.0
    assert asr_pseudo_stream.gain_config(runtime) == asr_pseudo_stream.gain_config(runtime, None)
    assert asr_pseudo_stream.level(runtime, "C00T1000TESTK") is False
    assert asr_pseudo_stream.level(runtime, "C00T3000TESTK") is True


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


def test_meter_hub_keeps_columns_apart():
    """Хаб метров: у каждой колонки свой метр (одни и те же sn — один и тот же метр),
    без sn — общий LEVELS; columns()/active() — сводка для селектора и слежения UI."""
    hub = MeterHub()
    first, second = hub.meter("C00T1000TESTK"), hub.meter("C00T2000TESTK")
    assert first is not second and hub.meter("C00T1000TESTK") is first
    assert hub.meter("") is audio_meter.LEVELS and hub.meter(None) is audio_meter.LEVELS
    assert hub.active() is None  # все молчат
    first.observe_in(_tone(0.05, amp=0.3))
    time.sleep(0.03)
    second.observe_in(_tone(0.05, amp=0.01))
    columns = {c["sn"]: c for c in hub.columns()}
    assert set(columns) == {"C00T1000TESTK", "C00T2000TESTK"}
    assert all(c["active"] for c in columns.values())
    assert hub.active() == "C00T2000TESTK"  # самая свежая говорившая


async def test_meter_record_captures_windows():
    """Запись измерения (автонастройка): окна истории дублируются в запись плюс хвост
    текущего окна; повторный stop — пусто, записи не мешают истории."""
    meter = LevelMeter()
    meter.start_record()
    for _ in range(3):
        meter.observe_in(_tone(0.05, amp=0.3))
        await asyncio.sleep(0.12)
    record = meter.stop_record()
    assert len(record) >= 2 and all(entry["i"] is not None for entry in record)
    assert meter.stop_record() == []
    meter.seal()  # хвост прошлой фазы закрывается досрочно — во вторую запись не попадёт
    meter.start_record()
    meter.observe_in(_tone(0.05))
    assert len(meter.stop_record()) == 1
    assert len(meter.history) >= 3  # запись не мешает истории: запечатанные окна там же


async def test_input_audio_levels_and_gain():
    """batch-путь: input_audio с gain и meter — WAV выровнен до цели, уровни пошли
    в метр колонки окнами ~100 мс; WAV по-прежнему валиден и полной длины."""
    import io
    import wave

    from server_emulator.audio_buffer import input_audio
    own = LevelMeter()
    data, filename = await input_audio(_pcm16(_tone(0.5, rate=48000, amp=0.01)),
                                       "audio/x-pcm;bit=16;rate=48000;channels=1",
                                       level=True, gain={"pre_gain_db": 0, "target": 0.5},
                                       meter=own)
    assert filename == "input.wav"
    entries = [e for e in own.snapshot()["history"] if e["i"] is not None]
    assert len(entries) >= 4
    assert entries[0]["o"] > entries[0]["i"] + 20  # выравнивание усилило тихий тон
    with wave.open(io.BytesIO(data)) as wav:
        assert wav.getnframes() == 24000


async def test_voice_levels_sn_and_columns(tmp_path, monkeypatch):
    """?sn= — метр конкретной колонки (sn в ответе, columns — сводка хаба); без sn —
    колонка со свежим звуком, иначе последняя говорившая."""
    from server_emulator.runtime import Runtime
    from server_emulator.ui.api.voice_stack import voice_levels
    runtime = Runtime(tmp_path)
    try:
        hub = MeterHub()
        monkeypatch.setattr(audio_meter, "HUB", hub)
        hub.meter("C00T1000TESTK").observe_in(_tone(0.05, amp=0.3))
        hub.meter("C00T2000TESTK").observe_in(_tone(0.05, amp=0.2))
        await asyncio.sleep(0.03)
        hub.meter("C00T1000TESTK").observe_in(_tone(0.05, amp=0.3))
        response = await voice_levels(
            make_mocked_request("GET", "/emulator/api/voice-levels?sn=C00T2000TESTK"), runtime, None)
        payload = json.loads(response.body)
        assert payload["sn"] == "C00T2000TESTK"
        assert {c["sn"] for c in payload["columns"]} == {"C00T1000TESTK", "C00T2000TESTK"}
        response = await voice_levels(
            make_mocked_request("GET", "/emulator/api/voice-levels"), runtime, None)
        payload = json.loads(response.body)
        assert payload["sn"] == "C00T1000TESTK"  # самая свежая говорившая
        assert {"sn", "active", "gain", "in", "out", "history", "columns"} <= set(payload)
    finally:
        await runtime.close()


class _JsonRequest:
    """Достаточно для обработчиков API: тело JSON (запросы панели)."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def _feed(meter, seconds, speech, silence=0.00005):
    """Синтетический микрофон колонки: bursts речи по 1.0 с и тишина по 0.4 с, пока
    длится настройка (чистые окна — автотюн мерит шум и пики речи)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for _ in range(50):
            if time.monotonic() >= end:
                break
            meter.observe_in(_tone(0.02, amp=speech))
            await asyncio.sleep(0.02)
        for _ in range(20):
            if time.monotonic() >= end:
                break
            meter.observe_in(_tone(0.02, amp=silence))
            await asyncio.sleep(0.02)


async def test_voice_autotune_writes_column_profile(tmp_path, monkeypatch):
    """Автонастройка: слушает колонку, мерит шум и пики речи, пишет профиль
    stt.profiles[sn] (выравнивание включено); громкий микрофон — без предусиления,
    исключительно тихий — недостающее до цели тянет предусиление."""
    from server_emulator.runtime import Runtime
    from server_emulator.ui.api.voice_stack import voice_autotune
    runtime = Runtime(tmp_path)
    try:
        hub = MeterHub()
        monkeypatch.setattr(audio_meter, "HUB", hub)
        feeder = asyncio.create_task(_feed(hub.meter("C00T1000TESTK"), 2.3, speech=0.05))  # пик −26 dBFS
        response = await voice_autotune(_JsonRequest({"sn": "C00T1000TESTK", "seconds": 2.0}),
                                        runtime, None)
        await feeder
        payload = json.loads(response.body)
        assert payload["sn"] == "C00T1000TESTK"
        assert payload["pre_gain_db"] == 0.0 and payload["level_target_dbfs"] == -6.0
        assert payload["speech_dbfs"] == pytest.approx(-29, abs=5)
        profile = runtime.settings["stt"]["profiles"]["C00T1000TESTK"]
        assert profile["level"] == "yes" and profile["pre_gain_db"] == 0.0
        feeder = asyncio.create_task(_feed(hub.meter("C00T2000TESTK"), 2.3, speech=0.002))  # пик −54 dBFS
        payload = json.loads((await voice_autotune(
            _JsonRequest({"sn": "C00T2000TESTK", "seconds": 2.0}), runtime, None)).body)
        await feeder
        assert payload["pre_gain_db"] == 3.0  # недостающие 48 дБ − (50−5) запаса
        assert runtime.settings["stt"]["profiles"]["C00T2000TESTK"]["pre_gain_db"] == 3.0
    finally:
        await runtime.close()


async def test_voice_autotune_no_speech_fails_honest(tmp_path, monkeypatch):
    """Тишина во время настройки: честный отказ «Речь не услышана», профиль не пишется,
    запись измерения сброшена; колонка без звука вовсе — отказ «не дошёл»."""
    from server_emulator.runtime import Runtime
    from server_emulator.ui.api.voice_stack import voice_autotune
    runtime = Runtime(tmp_path)
    try:
        hub = MeterHub()
        monkeypatch.setattr(audio_meter, "HUB", hub)
        meter = hub.meter("C00T1000TESTK")
        feeder = asyncio.create_task(_feed(meter, 1.0, speech=0.0))  # только тишина
        with pytest.raises(ValueError, match="Речь не услышана"):
            await voice_autotune(_JsonRequest({"sn": "C00T1000TESTK", "seconds": 0.6}), runtime, None)
        await feeder
        assert meter.stop_record() == []
        assert "profiles" not in runtime.settings["stt"]
        with pytest.raises(ValueError, match="не дошёл"):
            await voice_autotune(_JsonRequest({"sn": "C00T3000TESTK", "seconds": 0.5}), runtime, None)
    finally:
        await runtime.close()
