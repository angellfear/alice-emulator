"""Эмуляция потокового распознавания (batch-STT + Silero VAD): сегменты между паузами,
частичные результаты по ходу речи, финал по концу фразы; слово активации отбрасывается
(BOS нового логического потока), включение галочкой stt.pseudo_stream с честным откатом
на batch, если эмуляция невыполнима. Модель VAD в тестах — подмена (настоящий silero
v5 живёт в установленном голосовом пакете, в репозитории его нет)."""
import pathlib
import sys
import types

import numpy as np
import pytest

from server_emulator import asr_pseudo_stream, provider_manager
from server_emulator.audio_stream import transcode
from server_emulator.voice_stack import VoiceStack

PCM48 = "audio/x-pcm;bit=16;rate=48000;channels=1"
_REAL_VAD = pathlib.Path("data/speech/gigaam/silero-vad/silero_vad.onnx").is_file()


class _Runtime:
    def __init__(self, settings):
        self.settings = settings


def _tone(seconds, amp=0.3):
    t = np.arange(int(seconds * 48000), dtype=np.float32) / 48000
    return (amp * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2").tobytes()


def _silence(seconds):
    return np.zeros(int(seconds * 48000), dtype="<i2").tobytes()


async def _chunks(data, size=1920):  # 20 мс на кадр 48 кГц — как wire колонки
    for pos in range(0, len(data), size):
        yield data[pos:pos + size]


async def _pages(opus):
    """Wire-кадры колонки: по одной Ogg-странице (pump проверяет BOS по началу кадра)."""
    buf = bytearray(opus)
    while len(buf) >= 27:
        n = buf[26]
        size = 27 + n + sum(buf[27:27 + n])
        yield bytes(buf[:size])
        del buf[:size]


class _EnergyVad:
    """Вероятность речи по громкости кадра — pipeline без настоящего onnxruntime."""

    def __init__(self, path):
        assert path is not None

    def probabilities(self, frames, state):
        rms = np.sqrt(np.mean(np.asarray(frames) * np.asarray(frames), axis=1))
        return [0.9 if r > 0.02 else 0.0 for r in rms], state


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """Подмена VAD и модели: recognize_stream/go с энергетическим детектором и
    транскрибером-счётчиком (каждый сегмент — «сегмент N»)."""
    (tmp_path / "silero_vad.onnx").write_bytes(b"stub")
    monkeypatch.setattr(asr_pseudo_stream, "_Vad", _EnergyVad)
    monkeypatch.setattr(asr_pseudo_stream, "vad_model", lambda runtime: tmp_path / "silero_vad.onnx")
    calls = []

    async def transcribe(runtime, audio, filename):
        calls.append(len(audio))
        return f"сегмент {len(calls)}"

    return transcribe, calls


async def test_pause_ends_phrase_and_sends_partials(pipeline):
    """Речь → пауза: частичный текст после 0.4 с тишины, финал после 1.8 с —
    тот же текст; колонка закрывает микрофон по финалу, а не по тайм-ауту."""
    transcribe, calls = pipeline
    data = _tone(0.8) + _silence(2.2)
    partials = []

    async def on_partial(text):
        partials.append(text)

    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _chunks(data), PCM48, on_partial)
    assert partials == ["сегмент 1"], partials
    assert result == {"text": "сегмент 1"}
    assert len(calls) == 1


async def test_pause_ends_phrase_while_mic_stays_open(pipeline):
    """Колонка держит микрофон открытым и шлёт тишину: конец фразы решает пауза
    END_PAUSE_S, отсчитанная от конца речи, хотя частичное распознавание уже было —
    финал приходит до закрытия микрофона колонкой."""
    transcribe, calls = pipeline
    sent = {"seconds": 0.0}

    async def endless():
        async for chunk in _chunks(_tone(0.8)):
            yield chunk
        while sent["seconds"] < 10:  # колонка закрыла бы микрофон своим тайм-аутом
            sent["seconds"] += 0.02
            yield _silence(0.02)

    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, endless(), PCM48, None)
    assert result == {"text": "сегмент 1"} and len(calls) == 1
    assert sent["seconds"] < asr_pseudo_stream.END_PAUSE_S + 0.3, sent


async def test_internal_pause_splits_into_progressive_text(pipeline):
    """Пауза 0.5 с внутри фразы — частичное распознавание первой части, затем обеих;
    финальный текст — обе части по порядку."""
    transcribe, calls = pipeline
    data = _tone(0.8) + _silence(0.5) + _tone(0.8) + _silence(2.2)
    partials = []

    async def on_partial(text):
        partials.append(text)

    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _chunks(data), PCM48, on_partial)
    assert partials == ["сегмент 1", "сегмент 1 сегмент 2"], partials
    assert result == {"text": "сегмент 1 сегмент 2"}
    assert len(calls) == 2


async def test_silence_only_recognizes_nothing(pipeline):
    """Тишина без речи: сегментов нет, финальный текст пуст (колонка закроет
    микрофон своим тайм-аутом, как раньше)."""
    transcribe, calls = pipeline
    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _chunks(_silence(2.0)), PCM48, None)
    assert result == {"text": ""} and calls == []


async def test_mic_closed_mid_phrase_flushes_tail(pipeline):
    """Колонка закрыла микрофон до паузы END (свой тайм-аут): накопленный хвост
    распознаётся и становится финальным текстом."""
    transcribe, calls = pipeline
    data = _tone(0.8) + _silence(0.2)  # конец без паузы 1.8 с
    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _chunks(data), PCM48, None)
    assert result == {"text": "сегмент 1"} and len(calls) == 1


async def test_bos_stream_drops_wake_word(pipeline):
    """Opus: слово активации и реплика — разные логические потоки Ogg; распознаётся
    последний (BOS сбрасывает накопление), как у stream-адаптера."""
    transcribe, calls = pipeline

    async def opus(pcm):
        buf = b""
        async for page in transcode(_chunks(pcm), PCM48, "opus"):
            buf += page
        return buf

    wake = await opus(_tone(0.6))
    utterance = await opus(_tone(0.8) + _silence(2.2))
    partials = []

    async def on_partial(text):
        partials.append(text)

    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _pages(wake + utterance), "audio/opus", on_partial)
    assert result == {"text": "сегмент 1"}, result
    assert partials == ["сегмент 1"], partials


async def test_long_speech_segment_is_cut(pipeline):
    """Речь без пауз дольше предельного сегмента: режется на части (лимит моделей
    ~20-30 с), тексты складываются, частичные результаты продолжаются."""
    transcribe, calls = pipeline
    data = _tone(asr_pseudo_stream.MAX_SEGMENT_S + 1.0) + _silence(2.2)
    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _chunks(data), PCM48, None)
    assert len(calls) == 2  # сегмент разрезан
    assert result == {"text": "сегмент 1 сегмент 2"}


def test_enabled_requires_flag_and_feasibility(monkeypatch, tmp_path):
    """Галочка выключена — False; включена, но onnxruntime недоступен — False с
    журналом; доступный onnxruntime + модель — True."""
    runtime = _Runtime({"stt": {"pseudo_stream": "yes"}})
    assert provider_manager.ProviderManager(runtime).asr_streaming(runtime) is False

    stub = types.ModuleType("onnxruntime")  # драйвера нет: честный откат на batch
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # import onnxruntime падает
    assert asr_pseudo_stream.enabled(runtime) is False

    monkeypatch.setitem(sys.modules, "onnxruntime", stub)
    monkeypatch.setattr(asr_pseudo_stream, "vad_model", lambda r: tmp_path / "silero_vad.onnx")
    (tmp_path / "silero_vad.onnx").write_bytes(b"stub")
    assert asr_pseudo_stream.enabled(runtime) is True

    runtime.settings["stt"]["pseudo_stream"] = "no"
    assert asr_pseudo_stream.enabled(runtime) is False


async def test_save_source_keeps_pseudo_stream():
    """Применение источника перезаписывает settings["stt"] целиком — галочка
    «Эмуляция потокового режима» переносится (живёт поверх источника)."""
    saved = {}

    class _Runtime:
        settings = {"stt": {"adapter": "openai", "pseudo_stream": "yes"}}

        async def save_settings(self, value):
            saved.update(value)

    class _Stack:
        runtime = _Runtime()

    await VoiceStack.save_source(_Stack(), "asr", "k:gigaam",
                                 {"adapter": "openai", "url": "http://127.0.0.1:5015/v1/audio/transcriptions"})
    assert saved["stt"]["pseudo_stream"] == "yes" and saved["stt"]["adapter"] == "openai"
    assert saved["voice_stack"]["asr"] == "k:gigaam"


def test_vad_model_prefers_active_package(tmp_path):
    """Модель VAD — из папки активного пакета ASR (k:<id>); нет активного — первый
    найденный; пакетов нет — ValueError с причиной."""
    for package in ("other", "gigaam"):
        (tmp_path / package / "silero-vad").mkdir(parents=True)
        (tmp_path / package / "silero-vad" / "silero_vad.onnx").write_bytes(b"m")

    class _Local:
        directory = tmp_path

    class _Stack:
        local = _Local()

    runtime = _Runtime({"voice_stack": {"asr": "k:gigaam"}})
    runtime.voice_stack = _Stack()
    assert asr_pseudo_stream.vad_model(runtime) == tmp_path / "gigaam" / "silero-vad" / "silero_vad.onnx"
    runtime.settings["voice_stack"]["asr"] = "p:preset"
    assert asr_pseudo_stream.vad_model(runtime) == tmp_path / "gigaam" / "silero-vad" / "silero_vad.onnx"
    empty = _Runtime({"voice_stack": {}})
    empty.voice_stack = type("S", (), {"local": type("L", (), {"directory": tmp_path / "empty"})()})()
    try:
        asr_pseudo_stream.vad_model(empty)
        raise AssertionError("нет модели — ожидался ValueError")
    except ValueError as exc:
        assert "Silero VAD" in str(exc)


@pytest.mark.skipif(not _REAL_VAD,
                    reason="Silero VAD живёт в установленном голосовом пакете (data/, в репозитории нет)")
async def test_real_silero_vad_integration():
    """Настоящий silero_vad.onnx (v5, сборка onnx_asr): окно 576 сэмплов (64 контекст
    + 512) за вызов, состояние переносится; тишина — вероятность около нуля,
    голосоподобный сигнал детектируется (512 без контекста даёт ~0 на любой речи —
    колонка получала ноль частичных результатов)."""
    vad = asr_pseudo_stream._Vad("data/speech/gigaam/silero-vad/silero_vad.onnx")
    window = asr_pseudo_stream.FRAME + asr_pseudo_stream.CONTEXT
    state = np.zeros((2, 1, 128), dtype=np.float32)
    probs, state = vad.probabilities(np.zeros((8, window), dtype=np.float32), state)
    assert len(probs) == 8 and all(0.0 <= p <= 1.0 for p in probs)
    assert max(probs) < 0.5  # тишина не считается речью
    assert state.shape == (2, 1, 128) and np.isfinite(state).all()
    # голосоподобный сигнал: гармонический ряд 120 Гц со слоговой огибающей —
    # модель детектирует в окнах с контекстом (шум она молча игнорирует)
    t = np.arange(24000, dtype=np.float32) / 16000
    voiced = sum(a * np.sin(2 * np.pi * 120 * h * t)
                 for h, a in ((1, 1.0), (2, 0.6), (3, 0.4), (4, 0.25), (5, 0.15)))
    syllable = 0.5 * (1 + np.sign(np.sin(2 * np.pi * 2.5 * t)))
    voiced = (voiced * syllable * 0.35).clip(-1, 1)
    padded = np.concatenate([np.zeros(asr_pseudo_stream.CONTEXT, dtype=np.float32), voiced])
    count = len(voiced) // asr_pseudo_stream.FRAME
    windows = np.stack([padded[i * asr_pseudo_stream.FRAME:
                               i * asr_pseudo_stream.FRAME + window] for i in range(count)])
    probs, _ = vad.probabilities(windows, state)
    assert max(probs) > 0.5  # речь детектируется в окнах с контекстом


async def test_wire_fragments_still_recognize(pipeline):
    """Настоящий wire колонки: ws-сообщения ~60 байт режут Ogg-страницы в произвольных
    местах — конвейер распознаёт и на таких чанках (BOS на границе кадра не нужен)."""
    transcribe, calls = pipeline

    async def opus(pcm):
        buf = b""
        async for page in transcode(_chunks(pcm), PCM48, "opus"):
            buf += page
        return buf

    data = await opus(_tone(0.8) + _silence(2.2))
    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _chunks(data, 61), "audio/opus", None)
    assert result == {"text": "сегмент 1"} and len(calls) == 1


async def test_trace_events_on_opus_stream(pipeline):
    """Синтетический opus-поток: события pseudo_* в трассе речи (SpeechTrace) с числами —
    по ним на живом звуке ищут причину пустого распознавания (VAD или PCM не тот)."""
    from server_emulator.speech_trace import SpeechTrace

    transcribe, calls = pipeline
    records = []
    trace = SpeechTrace(records.append, {"sn": "C00T1000TESTK"})

    async def opus(pcm):
        buf = b""
        async for page in transcode(_chunks(pcm), PCM48, "opus"):
            buf += page
        return buf

    wake = await opus(_tone(0.6))
    utterance = await opus(_tone(0.8) + _silence(2.2))
    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _pages(wake + utterance), "audio/opus", None, trace=trace)
    assert result == {"text": "сегмент 1"}

    starts = [r for r in records if r["name"] == "pseudo_stream_start"]
    assert len(starts) == 2  # слово активации и реплика — два логических потока
    assert [r.get("generation") for r in starts] == [1, 2]
    vad = [r for r in records if r["name"] == "pseudo_vad"]
    assert len(vad) >= 2  # раз в ~0.5 с звука
    assert all({"samples_total", "seconds", "rms", "p_max"} <= set(r) for r in vad)
    assert any(r["rms"] > 0.01 and r["p_max"] > 0.5 for r in vad)  # речь детектируется
    assert all(r["sn"] == "C00T1000TESTK" for r in records)  # контекст трассы едет с событием
    segments = [r for r in records if r["name"] == "pseudo_segment"]
    # пауза после речи: сначала частичное распознавание накопленного, затем по той же
    # паузе — конец фразы (до закрытия микрофона колонкой)
    assert [r["action"] for r in segments] == ["partial", "end"]
    assert segments[0]["speech_frames"] > 0 and segments[0]["seconds"] > 0
    flushes = [r for r in records if r["name"] == "pseudo_flush"]
    assert any(r["had_speech"] is True and r["text_len"] == len("сегмент 1") for r in flushes)
    done = next(r for r in records if r["name"] == "pseudo_done")
    assert done["streams"] == 2 and done["frames"] > 0 and done["samples_total"] > 20000
