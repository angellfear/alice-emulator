"""Эмуляция потокового распознавания для batch-STT: детектор пауз Silero VAD по ходу
приёма звука, каждая пауза — распознавание накопленного сегмента тем же batch-STT
(WAV) и частичный AsrResult колонке, пауза дольше порога — финальный текст и конец
фразы (колонка закрывает микрофон сама, без тайм-аута NO_SPEECH). Ветвление —
provider_manager: адаптер с recognize_stream (T-one, deepgram) — настоящий поток;
batch-адаптер с включённой настройкой stt.pseudo_stream («Эмуляция потокового режима»
в карточке ASR) — этот модуль. Готового потокового режима у локальных batch-движков
(onnx-asr/GigaAM) нет. Модель VAD — silero-vad/silero_vad.onnx установленного
голосового пакета (gigaam и sherpa-t-one ставят её вместе с движком), драйвер —
onnxruntime (диапазон в pyproject.toml)."""
from __future__ import annotations

import asyncio
import collections
import logging

import numpy as np

from .audio_buffer import input_audio
from .audio_stream import transcode

log = logging.getLogger(__name__)

RATE = 16000  # вход Silero VAD: PCM 16 кГц mono (требование модели)
FRAME = 512  # шаг VAD — 32 мс новых сэмплов на кадр
CONTEXT = 64  # контекст кадра: модель ждёт 576 сэмплов = 64 прошлых + 512 новых
# (эталон вызова — onnx_asr.models.silero._encode этой сборки silero_vad.onnx:
# без контекста вероятности ~0.003 на любой речи — детектор молчит)
SPEECH_THRESHOLD = 0.5  # вероятность речи: кадр считается речью (рекомендация silero-vad)
MIN_SPEECH_S = 0.25  # сегмент короче — шум (щелчок, вздох), не распознаётся
PARTIAL_PAUSE_S = 0.4  # пауза внутри фразы → частичное распознавание накопленного
END_PAUSE_S = 1.8  # пауза дольше — конец фразы (как trailing_silence потокового движка)
MAX_SEGMENT_S = 20.0  # предельный сегмент batch-STT (модели принимают ~20-30 с)


def _frames(seconds):
    return max(1, round(seconds * RATE / FRAME))


MIN_SPEECH_FRAMES = _frames(MIN_SPEECH_S)
PREROLL_FRAMES = _frames(0.24)  # preroll до начала речи попадает в сегмент (атака не режется)
PARTIAL_FRAMES = _frames(PARTIAL_PAUSE_S)
END_FRAMES = _frames(END_PAUSE_S)
MAX_SEGMENT_FRAMES = _frames(MAX_SEGMENT_S)

_unavailable = ""  # причина последнего отката на batch (в журнал один раз)


def enabled(runtime):
    """«Эмуляция потокового режима» (settings["stt"].pseudo_stream = "yes") включена
    и выполнима: onnxruntime установлен, Silero VAD есть в установленном голосовом
    пакете. Не выполнима — False: обычное распознавание целиком, причина в журнале."""
    global _unavailable
    if str((runtime.settings.get("stt") or {}).get("pseudo_stream") or "").strip().lower() != "yes":
        return False
    try:
        import onnxruntime  # noqa: F401 — доступность драйвера в основном окружении
        vad_model(runtime)
    except Exception as exc:  # noqa: BLE001 — любая причина: честный откат на batch
        if _unavailable != str(exc):
            _unavailable = str(exc)
            log.warning("Эмуляция потокового режима недоступна (%s) — распознавание целиком", exc)
        return False
    return True


def vad_model(runtime):
    """Путь silero_vad.onnx установленного голосового пакета: папка активного пакета
    ASR (k:<id>), иначе первый найденный в папке пакетов. Нет — ValueError с причиной."""
    stack = getattr(runtime, "voice_stack", None)
    root = getattr(getattr(stack, "local", None), "directory", None)
    if root is None:
        raise ValueError("Локальные голосовые пакеты не настроены")
    ref = str((runtime.settings.get("voice_stack") or {}).get("asr") or "")
    if ref.startswith("k:"):
        candidate = root / ref[2:] / "silero-vad" / "silero_vad.onnx"
        if candidate.is_file():
            return candidate
    for candidate in sorted(root.glob("*/silero-vad/silero_vad.onnx")):
        return candidate
    raise ValueError("Silero VAD не найден: установите голосовой пакет, который его ставит (GigaAM)")


class _Vad:
    """Silero VAD v5 (onnxruntime, CPU): вероятности речи по кадрам 512 сэмплов
    16 кГц. Чистый вызов: состояние возвращает накопителю — поколения логических
    потоков не мешают друг другу; session.run потокобезопасен."""

    def __init__(self, path):
        import onnxruntime  # тяжёлый драйвер — только для включённой эмуляции
        self.session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])

    def probabilities(self, frames, state):
        """(вероятности по кадрам, новое состояние): кадры по FRAME+CONTEXT сэмплов —
        модель ждёт 576 сэмплов на вызов (input [1, 576], state [2, 1, 128], sr),
        состояние переносится от кадра к кадру."""
        out = []
        for frame in np.asarray(frames, dtype=np.float32):
            probability, state = self.session.run(None, {
                "input": frame.reshape(1, len(frame)),
                "state": state,
                "sr": np.array(RATE, dtype=np.int64),
            })
            out.append(float(probability.reshape(-1)[0]))
        return out, np.asarray(state, dtype=np.float32)


class _Accumulator:
    """Речь между паузами: кадры VAD, preroll до начала речи (атака слова не режется),
    счётчик тишины, готовые тексты сегментов. Состояние VAD живёт здесь."""

    def __init__(self, vad, trace=None):
        self.vad = vad
        self.trace = trace
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.pending = np.empty(0, dtype=np.float32)  # хвост меньше кадра
        self.context = np.zeros(CONTEXT, dtype=np.float32)  # прошлые 64 сэмпла (вход модели)
        self.buffer = []  # кадры сегмента: речь + короткий хвост тишины до решения
        self.pre = collections.deque(maxlen=PREROLL_FRAMES)
        self.in_speech = False
        self.silence = 0  # кадров тишины подряд
        self.speech = 0  # речевых кадров в сегменте
        self.texts = []  # распознанные сегменты по порядку
        self.samples_total = 0  # сэмплов звука принято всего (трасса pseudo_vad)
        self._vad_mark = 0  # сэмплов с последнего события pseudo_vad

    def joined(self):
        return " ".join(self.texts).strip()

    async def feed(self, samples):
        """PCM 16 кГц этого кадра колонки → вероятности речи → 'partial' | 'max' | 'end'
        (накопленное готово к распознаванию) или None. Кадр модели — 576 сэмплов
        (64 прошлых + 512 новых), шаг 512: контекст — последние 64 сэмпла кадра."""
        self.pending = np.concatenate([self.pending, samples])
        count = len(self.pending) // FRAME
        if not count:
            return None
        self.samples_total += len(samples)
        frames = np.asarray(self.pending[:count * FRAME], dtype=np.float32).reshape(count, FRAME)
        self.pending = self.pending[count * FRAME:]
        windows = np.empty((count, CONTEXT + FRAME), dtype=np.float32)
        windows[0, :CONTEXT] = self.context
        windows[0, CONTEXT:] = frames[0]
        if count > 1:
            windows[1:, :CONTEXT] = frames[:-1, -CONTEXT:]
            windows[1:, CONTEXT:] = frames[1:]
        self.context = frames[-1, -CONTEXT:].copy()
        # onnxruntime в потоке: цикл событий не ждёт несколько миллисекунд на кадр
        probs, self.state = await asyncio.to_thread(self.vad.probabilities, windows, self.state)
        self._vad_mark += count * FRAME
        if self.trace is not None and self._vad_mark >= RATE // 2:  # раз в ~0.5 с звука
            self._vad_mark = 0
            self.trace.event("pseudo_vad", "server", samples_total=self.samples_total,
                             seconds=round(self.samples_total / RATE, 2),
                             rms=round(float(np.sqrt(np.mean(np.square(frames)))), 5),
                             p_max=round(float(max(probs)), 4))
        for frame, probability in zip(frames, probs):
            action = self._step(frame, probability)
            if action:
                return action
        return None

    def _step(self, frame, probability):
        if probability >= SPEECH_THRESHOLD:
            if not self.in_speech:
                self.in_speech = True
                self.buffer.extend(self.pre)
                self.pre.clear()
            self.silence = 0
            self.speech += 1
            self.buffer.append(frame)
            return "max" if len(self.buffer) >= MAX_SEGMENT_FRAMES else None
        if not self.in_speech:
            self.pre.append(frame)
            if self.texts:  # после частичного распознавания пауза та же: её длина решает конец фразы
                self.silence += 1
                if self.silence >= END_FRAMES:
                    return "end"
            return None
        self.silence += 1
        self.buffer.append(frame)
        if self.silence >= END_FRAMES:
            return "end"
        if self.silence >= PARTIAL_FRAMES:
            return "partial"
        return None

    async def flush(self, runtime, transcribe):
        """Накопленный сегмент → WAV (input_audio) → тем же batch-STT → текст сегмента.
        Сегмент короче MIN_SPEECH_S — шум: без распознавания."""
        if not self.buffer:
            return ""
        samples = np.concatenate(self.buffer)
        had_speech = self.speech >= MIN_SPEECH_FRAMES
        self.buffer = []
        self.pre.clear()
        self.in_speech = False
        self.speech = 0  # счётчик тишины не сбрасывается: пауза продолжается и после сегмента
        if not had_speech:
            if self.trace is not None:
                self.trace.event("pseudo_flush", "server", had_speech=False, text_len=0)
            return ""
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        audio, filename = await input_audio(pcm, f"audio/x-pcm;bit=16;rate={RATE};channels=1")
        text = str(await transcribe(runtime, audio, filename) or "").strip()
        if text:
            self.texts.append(text)
        if self.trace is not None:
            self.trace.event("pseudo_flush", "server", had_speech=True, text_len=len(text))
        return text


async def recognize_stream(runtime, transcribe, chunks, fmt, on_partial=None, trace=None):
    """Как recognize_stream адаптера: consume wire-кадров, частичные тексты через
    on_partial, финальный {'text': ...}. Слово активации и реплика приходят разными
    логическими потоками Ogg — распознаётся последний (BOS сбрасывает накопление),
    как у stream-адаптера; PCM и прочие форматы — один поток. trace — трасса речи
    (SpeechTrace из voice.py): события pseudo_* показывают ход детектора пауз."""
    vad = _Vad(vad_model(runtime))
    totals = {"samples": 0}  # сэмплов звука за все логические потоки (pseudo_done)
    if fmt not in ("audio/opus", "audio/ogg"):
        if trace is not None:
            trace.event("pseudo_stream_start", "server", fmt=fmt)
        return await _run_one(runtime, transcribe, chunks, fmt, on_partial, vad,
                              trace=trace, totals=totals)
    result = asyncio.get_running_loop().create_future()
    current = {"generation": 0, "queue": None, "task": None, "frames": 0}

    async def run(generation, queue):
        async def frames():
            while (item := await queue.get()) is not None:
                yield item

        async def partial(text):
            if generation == current["generation"] and on_partial:
                await on_partial(text)

        try:
            value = await _run_one(runtime, transcribe, frames(), fmt, partial, vad,
                                   trace=trace if generation == current["generation"] else None,
                                   totals=totals)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — ошибку отдаёт только актуальный поток
            if generation == current["generation"] and not result.done():
                result.set_exception(exc)
            return
        if generation == current["generation"] and not result.done():
            result.set_result(value)

    def start():
        if current["task"]:
            current["task"].cancel()
        current["generation"] += 1
        current["queue"] = asyncio.Queue()
        current["frames"] = 0
        if trace is not None:
            trace.event("pseudo_stream_start", "server", generation=current["generation"])
        current["task"] = asyncio.create_task(run(current["generation"], current["queue"]))

    async def pump():
        start()
        async for frame in chunks:
            if _is_stream_start(frame) and current["frames"]:
                start()  # новое логическое поток: слово активации отбрасывается
            current["frames"] += 1
            current["queue"].put_nowait(frame)
        current["queue"].put_nowait(None)

    feeder = asyncio.create_task(pump())
    try:
        return await result
    finally:
        if trace is not None:
            trace.event("pseudo_done", "server", streams=current["generation"],
                        frames=current["frames"], samples_total=totals["samples"])
        feeder.cancel()
        if current["task"]:
            current["task"].cancel()
        await asyncio.gather(feeder, current["task"], return_exceptions=True)


def _is_stream_start(frame):
    """Кадр канала начинается со страницы BOS Ogg (начало логического потока)."""
    return frame[:4] == b"OggS" and len(frame) > 5 and bool(frame[5] & 2)


async def _run_one(runtime, transcribe, chunks, fmt, on_partial, vad, trace=None, totals=None):
    """Один логический поток wire-кадров: PCM 16 кГц → Silero VAD → сегменты между
    паузами; пауза PARTIAL_PAUSE_S — batch-STT сегмента и частичный текст колонке,
    пауза END_PAUSE_S — финальный {'text': ...} (колонка закроет микрофон сама)."""
    acc = _Accumulator(vad, trace=trace)
    last = ""
    try:
        async with asyncio.timeout(120):
            # микрофон колонки тихий (пик около −55 dBFS): Silero VAD на таком уровне речи не
            # видит — уровень выравнивается при декодировании, сегмент распознаётся выровненным
            async for chunk in transcode(chunks, fmt, "pcm16", level=True):
                samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
                if totals is not None:
                    totals["samples"] += len(samples)
                action = await acc.feed(samples)
                if action is None:
                    continue
                if trace is not None:  # сегмент готов к распознаванию: что накопилось
                    trace.event("pseudo_segment", "server", action=action,
                                speech_frames=acc.speech,
                                seconds=round(len(acc.buffer) * FRAME / RATE, 2))
                text = await acc.flush(runtime, transcribe)
                if action == "end":
                    return {"text": acc.joined()}
                joined = acc.joined()
                if text and on_partial and joined != last:
                    last = joined
                    await on_partial(joined)
    except asyncio.TimeoutError as exc:
        raise ValueError("Эмуляция потока: распознавание не завершилось за 120 с") from exc
    # микрофон закрылся раньше паузы END (колонка сама): хвост — в финальный текст
    await acc.flush(runtime, transcribe)
    return {"text": acc.joined()}
