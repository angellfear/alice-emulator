"""Общие части речевых серверов пакетов голосового стека (voice_stack/packages/*.json).

Серверы запускаются установщиком пакета как `python tools/<имя>_server.py` в окружении пакета,
поэтому этот модуль импортируется по соседству. Здесь только лёгкие зависимости (aiohttp, numpy;
soundfile и soxr — лениво): движок (torch, onnx-asr, vosk…) подключает сам сервер.

ASR  POST /v1/audio/transcriptions   multipart: file, model, language, response_format
     → {"text": "..."}  (OpenAI Audio API; text | json | verbose_json)
TTS  POST /v1/audio/speech            JSON: input, voice, response_format (pcm_f32le | wav)
     → float32 PCM потоком (X-Audio-Format: f32le, X-Audio-Sample-Rate), как у builder_tts.py
Оба: GET /health → {"status": "ok", "engine", "model", "voice", "state": {"model_loaded": …}, …};
порт открывается только после загрузки модели, поэтому живой /health = «запущен».

Ошибки — JSON {"error": {"message", "type"}} с понятным русским текстом, без трейсбеков.
"""
import asyncio
import concurrent.futures
import hashlib
import io
import json
import logging
import os
import wave

import numpy as np
from aiohttp import web

log = logging.getLogger("speech")

ASR_RATE = 16000
AUDIO_LIMIT = 64 * 1024 ** 2   # байт на один запрос распознавания
TEXT_LIMIT = 2000              # символов на один запрос синтеза
QUEUE_LIMIT = 8                # ожидающих запросов, дальше — 429
SPEECH_FORMATS = ("pcm_f32le", "wav")


class UserError(Exception):
    """Ошибка запроса или настройки: текст показывается клиенту как есть (HTTP 400)."""


# --- окружение -------------------------------------------------------------------------
def env(name, default=""):
    value = os.environ.get(name)
    return default if value is None or not value.strip() else value.strip()


def env_int(name, default=0):
    return int(float(env(name, str(default))))


def env_float(name, default=0.0):
    return float(env(name, str(default)))


def env_flag(name, default=False):
    return env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def preload_cuda_libs():
    """CUDA/cuDNN из pip-пакетов nvidia-*: Windows — каталоги bin в путь поиска DLL, Linux — загрузка
    библиотек по абсолютному пути (LD_LIBRARY_PATH уже не успеть задать). Сбой не фатален:
    библиотеки могут быть установлены в системе."""
    import glob
    import site
    roots = [os.path.join(path, "nvidia") for path in site.getsitepackages()]
    for root in roots:
        if os.name == "nt":
            for folder in glob.glob(os.path.join(root, "*", "bin")):
                os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
                try:
                    os.add_dll_directory(folder)
                except OSError:
                    pass
        else:
            import ctypes
            for pattern in ("libcudart*", "libnvrtc.so*", "libcublasLt*", "libcublas.so*", "libcufft*", "libcurand*", "libcudnn.so*"):
                for library in sorted(glob.glob(os.path.join(root, "*", "lib", pattern))):
                    try:
                        ctypes.CDLL(library, mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass


# --- звук ------------------------------------------------------------------------------
def decode_audio(data, rate=ASR_RATE):
    """Байты аудиофайла (WAV, FLAC, Ogg/Opus…) → float32 моно в частоте rate."""
    import soundfile
    try:
        samples, source = soundfile.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception as exc:  # noqa: BLE001 — libsndfile не знает формат: пробуем PyAV (есть у faster-whisper)
        try:
            from faster_whisper import decode_audio as av_decode
            return np.ascontiguousarray(av_decode(io.BytesIO(data), sampling_rate=rate), dtype=np.float32)
        except Exception:  # noqa: BLE001
            raise UserError("Не удалось прочитать аудио: поддерживаются WAV, FLAC и Ogg/Opus") from exc
    mono = samples.mean(axis=1) if samples.shape[1] > 1 else samples[:, 0]
    if source != rate:
        import soxr
        mono = soxr.resample(mono, source, rate)
    return np.ascontiguousarray(mono, dtype=np.float32)



def wav_bytes(samples, rate):
    """float32 → WAV 16 бит."""
    pcm = (np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 1.0) * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(int(rate))
        target.writeframes(pcm.tobytes())
    return buffer.getvalue()


# --- запечённый голос ------------------------------------------------------------------
def bake_path(ref_audio, ref_text):
    """(путь файла запечённого голоса, нормализованный текст эталона). Имя — как у других пакетов:
    <эталон>.<sha1(WAV + текст)[:12]>.pt рядом с эталоном; новый эталон или текст — новое запекание."""
    text = " ".join((ref_text or "").split())
    with open(ref_audio, "rb") as handle:
        digest = hashlib.sha1(handle.read() + text.encode("utf-8")).hexdigest()[:12]
    return os.path.splitext(ref_audio)[0] + "." + digest + ".pt", text


def baked_voice(ref_audio, ref_text, create, save, load):
    """Голос по эталону, обработанный один раз: create(text) → голос, save(голос, путь), load(путь).
    Возвращает (голос, описание режима). Прежние запекания этого эталона удаляются."""
    path, text = bake_path(ref_audio, ref_text)
    name = os.path.basename(path)
    if os.path.exists(path):
        return load(path), "запечённый голос (" + name + ")"
    print("Запекание голоса из", os.path.basename(ref_audio), flush=True)
    voice = create(text or None)
    stem = os.path.basename(os.path.splitext(ref_audio)[0]) + "."
    folder = os.path.dirname(path) or "."
    for old in os.listdir(folder):
        if old.startswith(stem) and old.endswith(".pt"):
            os.remove(os.path.join(folder, old))
    save(voice, path)
    return voice, "запечённый голос (" + name + ", запечён сейчас)"


# --- HTTP ------------------------------------------------------------------------------
def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


def failure(status, message, kind="invalid_request_error"):
    return web.json_response({"error": {"message": message, "type": kind}}, status=status, dumps=_dumps)


def _info(info):
    return info() if callable(info) else dict(info)


class Gate:
    """Очередь запросов: не больше capacity одновременно, не больше QUEUE_LIMIT ждущих."""

    def __init__(self, capacity):
        self.capacity = max(1, capacity)
        self.pool = concurrent.futures.ThreadPoolExecutor(self.capacity)
        self.slots = asyncio.Semaphore(self.capacity)
        self.waiting = 0
        self.active = 0
        self.completed = 0

    async def enter(self):
        if self.waiting >= QUEUE_LIMIT:
            raise web.HTTPTooManyRequests(text="Очередь запросов переполнена", headers={"Retry-After": "1"})
        self.waiting += 1
        try:
            await self.slots.acquire()
        finally:
            self.waiting -= 1
        self.active += 1

    def leave(self, done=True):
        self.active -= 1
        self.completed += done
        self.slots.release()

    def run(self, function, *args):
        return asyncio.get_running_loop().run_in_executor(self.pool, function, *args)

    def stats(self):
        return {"capacity": self.capacity, "busy": self.active > 0, "active": self.active,
                "waiting": self.waiting, "completed": self.completed}


def _guarded(handler):
    """Ошибки обработчика → JSON с коротким русским текстом (подробности — в журнал сервера)."""
    async def wrapper(request):
        try:
            return await handler(request)
        except UserError as exc:
            return failure(400, str(exc))
        except web.HTTPException as exc:
            return failure(exc.status, exc.text or exc.reason)
        except Exception as exc:  # noqa: BLE001
            log.exception("Сбой запроса %s", request.path)
            return failure(500, "Сбой движка: " + (str(exc) or type(exc).__name__)[:300], "server_error")
    return wrapper


def _routes(app, health):
    async def handle_health(request):
        return web.json_response(health(), dumps=_dumps)
    app.router.add_get("/health", handle_health)


def asr_app(transcribe, info, capacity=1):
    """transcribe(samples_16k_float32, language | None) → текст (блокирующая, вызывается в потоке)."""
    gate = Gate(capacity)

    async def transcriptions(request):
        if not (request.content_type or "").startswith("multipart/"):
            raise UserError("Нужен multipart/form-data с полем file")
        form = await request.post()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "file"):
            raise UserError("Поле file: нужен аудиофайл")
        data = upload.file.read()
        if not data:
            raise UserError("Пустой аудиофайл")
        fmt = str(form.get("response_format") or "json")
        if fmt not in ("json", "text", "verbose_json"):
            raise UserError("response_format: json, text или verbose_json")
        language = str(form.get("language") or "").strip() or None
        samples = await asyncio.to_thread(decode_audio, data)
        await gate.enter()
        done = False
        try:
            text = (await gate.run(transcribe, samples, language) or "").strip()
            done = True
        finally:
            gate.leave(done)
        if fmt == "text":
            return web.Response(text=text, content_type="text/plain")
        body = {"text": text}
        if fmt == "verbose_json":
            body.update(language=language or "", duration=round(len(samples) / ASR_RATE, 3))
        return web.json_response(body, dumps=_dumps)

    def health():
        return {"status": "ok", **_info(info), **gate.stats()}

    app = web.Application(client_max_size=AUDIO_LIMIT)
    _routes(app, health)
    app.router.add_post("/v1/audio/transcriptions", _guarded(transcriptions))
    return app


def tts_app(synth, rate, info, stream=None, capacity=1):
    """synth(text, voice | None) → float32 моно целиком; stream(text, voice | None) → итератор кусков float32
    (необязательно; потоком идёт pcm_f32le). rate — частота дискретизации (число или функция: некоторые модели
    узнают её только после первого синтеза). Блокирующие функции вызываются в потоке."""
    gate = Gate(capacity)

    def current_rate():
        return int(rate() if callable(rate) else rate)

    def headers():
        return {"Content-Type": "application/octet-stream", "X-Audio-Format": "f32le",
                "X-Audio-Sample-Rate": str(current_rate()), "X-Audio-Channels": "1"}

    async def speech(request):
        try:
            data = await request.json()
        except ValueError:
            raise UserError("Тело запроса: JSON с полем input")
        if not isinstance(data, dict):
            raise UserError("Тело запроса: JSON с полем input")
        text = data.get("input")
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_LIMIT:
            raise UserError(f"input: от 1 до {TEXT_LIMIT} символов")
        fmt = str(data.get("response_format") or "pcm_f32le")
        if fmt not in SPEECH_FORMATS:
            raise UserError("response_format: pcm_f32le или wav")
        voice = str(data.get("voice") or "").strip() or None
        text = text.strip()
        await gate.enter()
        done = False
        try:
            if stream is not None and fmt == "pcm_f32le":
                iterator = iter(await gate.run(stream, text, voice))
                first = await gate.run(next, iterator, None)
                response = web.StreamResponse(headers=headers())
                await response.prepare(request)
                chunk = first
                try:
                    while chunk is not None:
                        await response.write(np.asarray(chunk, dtype="<f4").reshape(-1).tobytes())
                        chunk = await gate.run(next, iterator, None)
                    await response.write_eof()
                    done = True
                except Exception:  # noqa: BLE001 — статус уже отправлен: оборвать соединение, причина — в журнале
                    log.exception("Сбой потокового синтеза")
                    if request.transport is not None:
                        request.transport.close()
                return response
            samples = np.asarray(await gate.run(synth, text, voice), dtype=np.float32).reshape(-1)
            done = True
            if fmt == "wav":
                return web.Response(body=wav_bytes(samples, current_rate()), content_type="audio/wav")
            response = web.StreamResponse(headers=headers())
            await response.prepare(request)
            pcm = samples.astype("<f4").tobytes()
            for position in range(0, len(pcm), 32768):
                await response.write(pcm[position:position + 32768])
            await response.write_eof()
            return response
        finally:
            gate.leave(done)

    def health():
        return {"status": "ok", "format": "f32le", "sample_rate": current_rate(), **_info(info), **gate.stats()}

    app = web.Application(client_max_size=65536)
    _routes(app, health)
    app.router.add_post("/v1/audio/speech", _guarded(speech))
    return app


def run(build_app, default_port, title):
    """Загрузить движок (build_app() → web.Application, блокирующе), затем открыть порт и ждать."""
    async def main():
        app = await asyncio.to_thread(build_app)
        runner = web.AppRunner(app)
        await runner.setup()
        port = env_int("PORT", default_port)
        await web.TCPSite(runner, env("HOST", "127.0.0.1"), port).start()
        print(title, "готов на порту", port, flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
    asyncio.run(main())
