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
    """Речь → пауза: частичный текст после 0.4 с тишины — вся накопленная фраза,
    финал после 1.8 с — вся фраза распознаётся ещё раз; колонка закрывает микрофон
    по финалу, а не по тайм-ауту."""
    transcribe, calls = pipeline
    data = _tone(0.8) + _silence(2.2)
    partials = []

    async def on_partial(text):
        partials.append(text)

    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _chunks(data), PCM48, on_partial)
    assert partials == ["сегмент 1"], partials
    assert result == {"text": "сегмент 2"}, result  # финал — повторное распознавание всей фразы
    assert len(calls) == 2


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
    assert result == {"text": "сегмент 2"} and len(calls) == 2  # частичный и финал — вся фраза ещё раз
    assert sent["seconds"] < asr_pseudo_stream.END_PAUSE_S + 0.3, sent


async def test_end_pause_setting_ends_phrase_earlier(pipeline):
    """stt.pseudo_end_pause=0.6: тишина 1.0 с закрывает фразу по паузе — финал есть
    (при умолчании 1.8 конца по паузе не было бы, только закрытие микрофона)."""
    from server_emulator.speech_trace import SpeechTrace

    transcribe, calls = pipeline
    records = []
    trace = SpeechTrace(records.append, {"sn": "C00T1000TESTK"})
    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({"stt": {"pseudo_end_pause": 0.6}}), transcribe,
        _chunks(_tone(0.8) + _silence(1.0)), PCM48, None, trace=trace)
    assert result == {"text": "сегмент 2"} and len(calls) == 2  # частичный и финал
    actions = [r["action"] for r in records if r["name"] == "pseudo_segment"]
    assert actions == ["partial", "end"]  # конец по паузе 0.6 с, до закрытия микрофона


async def test_level_no_disables_gain(pipeline, monkeypatch):
    """stt.level="no": детектор пауз идёт без выравнивания уровня — transcode
    вызывается с level=False (batch-путь voice.py берёт ту же настройку)."""
    transcribe, calls = pipeline
    levels = []
    real_transcode = asr_pseudo_stream.transcode

    async def spy(chunks, source, target, **kwargs):
        levels.append(kwargs.get("level"))
        async for chunk in real_transcode(chunks, source, target, **kwargs):
            yield chunk

    monkeypatch.setattr(asr_pseudo_stream, "transcode", spy)
    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({"stt": {"level": "no"}}), transcribe,
        _chunks(_tone(0.8) + _silence(2.2)), PCM48, None)
    assert result == {"text": "сегмент 2"}  # частичный и финал — вся фраза ещё раз
    assert levels == [False]


async def test_internal_pause_splits_into_progressive_text(pipeline):
    """Пауза 0.5 с внутри фразы: каждый частичный текст — вся накопленная фраза
    на момент вызова (заменяет прежний); финал — вся фраза распознаётся ещё раз."""
    transcribe, calls = pipeline
    data = _tone(0.8) + _silence(0.5) + _tone(0.8) + _silence(2.2)
    partials = []

    async def on_partial(text):
        partials.append(text)

    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _chunks(data), PCM48, on_partial)
    assert partials == ["сегмент 1", "сегмент 2"], partials
    assert result == {"text": "сегмент 3"}, result
    assert len(calls) == 3


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
    # слово активации отбрасывается отменой задачи (BOS), распознаётся только реплика:
    # частичный — вся реплика, финал — вся реплика ещё раз
    assert result == {"text": "сегмент 2"}, result
    assert partials == ["сегмент 1"], partials


async def test_long_speech_segment_is_cut(pipeline):
    """Речь без пауз дольше предельного сегмента (лимит моделей ~20-30 с): текст окна
    20 с фиксируется, буфер начинается заново; финал — фикс + текст последнего окна
    (вся оставшаяся фраза распознаётся ещё раз), частичные результаты продолжаются."""
    transcribe, calls = pipeline
    data = _tone(asr_pseudo_stream.MAX_SEGMENT_S + 1.0) + _silence(2.2)
    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _chunks(data), PCM48, None)
    assert len(calls) == 3  # окно 20 с, частичное по последнему окну, финал
    assert result == {"text": "сегмент 1 сегмент 3"}, result


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


def test_pauses_from_settings_fall_back_to_defaults():
    """Ключи pseudo_partial_pause/pseudo_end_pause: число в диапазоне — берётся;
    ключа нет, не число или вне диапазона — умолчания 0.4/1.8 (без ошибки);
    конец фразы должен быть длиннее паузы между словами."""
    frames = asr_pseudo_stream._frames
    defaults = (frames(asr_pseudo_stream.PARTIAL_PAUSE_S), frames(asr_pseudo_stream.END_PAUSE_S))
    for stt in ({},  # ключей нет
                {"pseudo_partial_pause": "abc", "pseudo_end_pause": 99},  # мусор, вне диапазона
                {"pseudo_partial_pause": 0.1, "pseudo_end_pause": 6.0}):  # оба вне диапазона
        assert asr_pseudo_stream.pauses(_Runtime({"stt": stt})) == defaults, stt
    # конец не длиннее паузы между словами — откатывается только конец, пауза между словами в диапазоне
    assert asr_pseudo_stream.pauses(
        _Runtime({"stt": {"pseudo_partial_pause": 1.5, "pseudo_end_pause": 1.5}})) == (
        frames(1.5), frames(asr_pseudo_stream.END_PAUSE_S))
    assert asr_pseudo_stream.pauses(
        _Runtime({"stt": {"pseudo_partial_pause": 0.6, "pseudo_end_pause": 2.5}})) == (frames(0.6), frames(2.5))


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
    assert result == {"text": "сегмент 2"} and len(calls) == 2  # частичный и финал — вся фраза ещё раз


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
    assert result == {"text": "сегмент 2"}  # wake-поток отменён (BOS), реплика: частичный и финал

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


async def _recognize_by_length(runtime, audio, filename, texts):
    """Транскрибёр, текст которого зависит от длины полученного аудио: короткий вызов
    и вызов с полной фразой возвращают разный текст."""
    seconds = round(len(audio) / 2 / asr_pseudo_stream.RATE, 1)  # PCM16 mono: 2 байта на сэмпл
    texts.append(seconds)
    return f"фраза {seconds} с"


async def test_final_recognizes_whole_phrase(pipeline):
    """Финал — распознавание всей накопленной фразы целиком: transcribe получает
    весь буфер фразы (аудио длиннее любого из частичных), финальный текст — от этого
    вызова, а не склейка частичных текстов."""
    transcribe, calls = pipeline
    texts = []

    async def by_length(runtime, audio, filename):
        return await _recognize_by_length(runtime, audio, filename, texts)

    data = _tone(0.3) + _silence(0.5) + _tone(0.8) + _silence(2.2)
    partials = []

    async def on_partial(text):
        partials.append(text)

    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), by_length, _chunks(data), PCM48, on_partial)
    assert len(texts) == 3  # частичный, частичный, финал
    assert texts[-1] >= 1.6  # финалу досталась вся фраза (0.3+0.5+0.8+1.8 = 3.4 с)
    assert result == {"text": f"фраза {texts[-1]} с"}, result  # финальный текст — с полной фразой
    assert partials == [f"фраза {texts[0]} с", f"фраза {texts[1]} с"], partials


async def test_partial_replaces_previous_text(pipeline):
    """Частичный текст — вся фраза на момент вызова: следующий частичный заменяет
    прежний, а не дописывается к нему."""
    transcribe, calls = pipeline
    texts = []

    async def by_length(runtime, audio, filename):
        return await _recognize_by_length(runtime, audio, filename, texts)

    data = _tone(0.8) + _silence(0.5) + _tone(0.8) + _silence(2.2)
    partials = []

    async def on_partial(text):
        partials.append(text)

    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), by_length, _chunks(data), PCM48, on_partial)
    assert texts[1] > texts[0]  # второй частичный получил всю накопленную фразу
    assert partials == [f"фраза {texts[0]} с", f"фраза {texts[1]} с"], partials  # замена, не склейка


async def test_end_fires_after_empty_partial(pipeline):
    """Первое частичное распознавание вернуло пусто — конец фразы по паузе всё равно
    наступает: счёт тишины до конца фразы не зависит от результатов распознавания."""
    transcribe, calls = pipeline
    attempts = []

    async def empty_first(runtime, audio, filename):
        attempts.append(len(attempts))
        return "" if len(attempts) == 1 else "текст"

    from server_emulator.speech_trace import SpeechTrace
    records = []
    trace = SpeechTrace(records.append, {"sn": "C00T1000TESTK"})
    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), empty_first, _chunks(_tone(0.5) + _silence(0.6) + _tone(0.6) + _silence(2.2)),
        PCM48, None, trace=trace)
    actions = [r["action"] for r in records if r["name"] == "pseudo_segment"]
    assert actions == ["partial", "partial", "end"], actions  # конец — по паузе, не по закрытию микрофона
    assert result == {"text": "текст"} and len(attempts) == 3


async def test_noise_burst_stays_in_phrase_buffer(pipeline):
    """Шумовой всплеск короче MIN_SPEECH_S не вызывает распознавания сам по себе,
    но его кадры остаются в буфере фразы: следующая речь распознаётся вместе с ним."""
    transcribe, calls = pipeline
    data = _tone(0.15) + _silence(0.6) + _tone(0.8) + _silence(2.2)
    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), transcribe, _chunks(data), PCM48, None)
    assert len(calls) == 2  # частичный по всей фразе (шум + пауза + речь) и финал
    assert result == {"text": "сегмент 2"}, result


async def test_long_phrase_fixes_window_and_adds_last(pipeline):
    """Фраза 25 с без пауз: текст первых 20 с фиксируется, буфер начинается заново;
    финал — зафиксированный текст + текст последнего окна (вся оставшаяся фраза)."""
    transcribe, calls = pipeline
    texts = []

    async def by_length(runtime, audio, filename):
        return await _recognize_by_length(runtime, audio, filename, texts)

    data = _tone(asr_pseudo_stream.MAX_SEGMENT_S + 5.0) + _silence(2.2)
    partials = []

    async def on_partial(text):
        partials.append(text)

    result = await asr_pseudo_stream.recognize_stream(
        _Runtime({}), by_length, _chunks(data), PCM48, on_partial)
    assert texts[0] == 20.0  # первое окно — ровно MAX_SEGMENT_S
    assert texts[1] >= 5.0  # частичный последнего окна — весь остаток фразы
    assert texts[2] > texts[1]  # финал распознал больше аудио (тишина до конца фразы)
    # частичный на границе окна 20 с (текст окна) и частичный всего накопленного (фикс + остаток)
    assert partials == ["фраза 20.0 с", f"фраза 20.0 с фраза {texts[1]} с"], partials
    assert result == {"text": f"фраза 20.0 с фраза {texts[2]} с"}, result
