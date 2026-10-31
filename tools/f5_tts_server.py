"""F5-TTS: OpenAI Speech API (/v1/audio/speech) поверх f5_tts.api.F5TTS — клонирование голоса по эталону.

Ответ — float32 PCM (X-Audio-Format: f32le, X-Audio-Sample-Rate), как у builder_tts.py: конвейер
эмулятора перекодирует его в Opus потоком. Окружение (пакет voice_stack/packages/f5-tts.json):
  F5_DEVICE   cuda|cpu        F5_MODEL  F5TTS_v1_Base     F5_CKPT/F5_VOCAB  путь или hf://repo/file
  F5_REF_AUDIO эталон wav     F5_REF_TEXT его текст (пусто — распознать автоматически)
  F5_SPEED, F5_NFE            скорость речи и число шагов (качество/время)
  F5_THREADS                  потоков процессора (ограничение нагрузки на CPU)
  CUDA_VISIBLE_DEVICES        какая видеокарта видна процессу (её выбирает пакет)
"""
import asyncio
import os

import numpy as np
from aiohttp import web


def resolve(value):
    """hf://user/repo/path → локальный файл из кеша Hugging Face (скачивается при первом запуске)."""
    if value.startswith("hf://"):
        from cached_path import cached_path
        return str(cached_path(value))
    return value


def example_reference():
    from importlib.resources import files
    return (str(files("f5_tts").joinpath("infer/examples/basic/basic_ref_en.wav")),
            "Some call me nature, others call me mother nature.")


async def main():
    import soundfile
    import torch
    import torchaudio
    from f5_tts.api import F5TTS

    def load_audio(path, *args, **kwargs):
        """torchaudio ≥2.9 читает аудио через torchcodec (нужны DLL FFmpeg) — эталон читаем soundfile."""
        data, rate = soundfile.read(path, dtype="float32", always_2d=True)
        return torch.from_numpy(data.T.copy()), rate

    torchaudio.load = load_audio  # f5_tts.infer.utils_infer вызывает torchaudio.load(ref_audio)

    threads = int(os.environ.get("F5_THREADS") or 0)
    if threads > 0:
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(max(1, min(threads, 4)))
    device = os.environ.get("F5_DEVICE", "cuda")
    if device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA недоступна: нет видеокарты NVIDIA или установлен PyTorch без CUDA")
    gpu = torch.cuda.get_device_name(0) if device == "cuda" else ""
    kwargs = {"model": os.environ.get("F5_MODEL", "F5TTS_v1_Base"), "device": device}
    if os.environ.get("F5_CKPT"):
        kwargs["ckpt_file"] = await asyncio.to_thread(resolve, os.environ["F5_CKPT"])
    if os.environ.get("F5_VOCAB"):
        kwargs["vocab_file"] = await asyncio.to_thread(resolve, os.environ["F5_VOCAB"])
    print("Загрузка модели", kwargs, flush=True)
    tts = await asyncio.to_thread(F5TTS, **kwargs)
    reference, reference_text = os.environ.get("F5_REF_AUDIO", ""), os.environ.get("F5_REF_TEXT", "")
    if not reference:
        reference, reference_text = example_reference()
    speed, steps = float(os.environ.get("F5_SPEED", "1.0")), int(os.environ.get("F5_NFE", "32"))
    lock = asyncio.Lock()  # одна видеокарта — один синтез за раз
    completed = 0

    async def health(request):
        return web.json_response({"status": "ok", "engine": "f5-tts", "model": kwargs["model"], "device": device, "gpu": gpu,
                                  "threads": torch.get_num_threads(), "format": "f32le", "reference": os.path.basename(reference),
                                  "busy": lock.locked(), "completed": completed})

    async def speech(request):
        nonlocal completed
        data = await request.json()
        text = data.get("input")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise web.HTTPBadRequest(text="input: 1..2000 characters")
        async with lock:
            wave, rate, _ = await asyncio.to_thread(tts.infer, reference, reference_text, text.strip(),
                                                    speed=speed, nfe_step=steps, show_info=lambda *a: None)
        completed += 1
        pcm = np.asarray(wave, dtype=np.float32).reshape(-1).tobytes()
        response = web.StreamResponse(headers={"Content-Type": "application/octet-stream", "X-Audio-Format": "f32le",
                                               "X-Audio-Sample-Rate": str(int(rate))})
        await response.prepare(request)
        for position in range(0, len(pcm), 32768):
            await response.write(pcm[position:position + 32768])
        await response.write_eof()
        return response

    app = web.Application(client_max_size=32768)
    app.router.add_get("/health", health)
    app.router.add_post("/v1/audio/speech", speech)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8777"))).start()
    print("F5-TTS готов на порту", os.environ.get("PORT", "8777"), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
