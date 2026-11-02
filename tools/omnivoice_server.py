"""OmniVoice (k2-fsa/OmniVoice): OpenAI Speech API (/v1/audio/speech) — клонирование голоса по эталону
или голос по описанию (instruct). Ответ — float32 PCM (X-Audio-Format: f32le, X-Audio-Sample-Rate),
как у builder_tts.py / f5_tts_server.py. Окружение (пакет voice_stack/packages/omnivoice.json):
  OV_MODEL    k2-fsa/OmniVoice        OV_DEVICE  cuda|cpu      OV_THREADS  потоков процессора
  OV_REF_AUDIO эталон wav — запекается один раз в <эталон>.<хеш>.pt рядом с ним
  OV_REF_TEXT его текст (пусто — распознает модель)             OV_LANGUAGE язык (ru)
  OV_INSTRUCT описание голоса («female, low pitch»; пустые пункты отбрасываются) — только без эталона
  OV_SPEED, OV_STEPS                    скорость речи и шаги генерации (качество/время)
  CUDA_VISIBLE_DEVICES                 какая видеокарта видна процессу (её выбирает пакет)
"""
import asyncio
import hashlib
import inspect
import os

import numpy as np
from aiohttp import web


def load_voice(model, VoiceClonePrompt):
    """Голос для клонирования: эталон WAV, запечённый один раз (create_voice_clone_prompt →
    <эталон>.<хеш>.pt рядом с ним; новый эталон или текст — новое запекание). (prompt, режим)."""
    ref_audio = os.environ.get("OV_REF_AUDIO")
    if not ref_audio:
        return None, "по описанию"
    ref_text = " ".join((os.environ.get("OV_REF_TEXT") or "").split()) or None
    with open(ref_audio, "rb") as handle:
        digest = hashlib.sha1(handle.read() + (ref_text or "").encode("utf-8")).hexdigest()[:12]
    path = os.path.splitext(ref_audio)[0] + "." + digest + ".pt"
    if os.path.exists(path):
        return VoiceClonePrompt.load(path), "клон по эталону (" + os.path.basename(path) + ")"
    print("Запекание голоса из", os.path.basename(ref_audio), "— текст:", ref_text or "распознаётся моделью", flush=True)
    prompt = model.create_voice_clone_prompt(ref_audio=ref_audio, ref_text=ref_text)
    for old in os.listdir(os.path.dirname(path) or "."):  # прежние запекания этого эталона
        if old.startswith(os.path.basename(os.path.splitext(ref_audio)[0]) + ".") and old.endswith(".pt"):
            os.remove(os.path.join(os.path.dirname(path), old))
    prompt.save(path)
    return prompt, "клон по эталону (" + os.path.basename(path) + ", запечён сейчас)"


async def main():
    import soundfile
    import torch
    import torchaudio
    from omnivoice import OmniVoice, VoiceClonePrompt

    def load_audio(path, *args, **kwargs):
        """torchaudio ≥2.9 читает аудио через torchcodec (нужны DLL FFmpeg) — эталон читаем soundfile."""
        data, rate = soundfile.read(path, dtype="float32", always_2d=True)
        return torch.from_numpy(data.T.copy()), rate

    torchaudio.load = load_audio

    threads = int(os.environ.get("OV_THREADS") or 0)
    if threads > 0:
        torch.set_num_threads(threads)
    device = os.environ.get("OV_DEVICE", "cuda")
    if device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA недоступна: нет видеокарты NVIDIA или установлен PyTorch без CUDA")
    gpu = torch.cuda.get_device_name(0) if device == "cuda" else ""
    name = os.environ.get("OV_MODEL", "k2-fsa/OmniVoice")
    print("Загрузка модели", name, device, flush=True)
    model = await asyncio.to_thread(OmniVoice.from_pretrained, name,
                                    device_map="cuda:0" if device == "cuda" else "cpu",
                                    dtype=torch.float16 if device == "cuda" else torch.float32)
    accepted = set(inspect.signature(model.generate).parameters)
    rate = int(getattr(model, "sampling_rate", 0) or getattr(model, "sample_rate", 0) or 24000)
    voice, mode = await asyncio.to_thread(load_voice, model, VoiceClonePrompt)
    # описание из списков пакета «female, , low pitch, , » — пустые пункты убираются
    instruct = ", ".join(item.strip() for item in (os.environ.get("OV_INSTRUCT") or "").split(",") if item.strip())
    options = {"voice_clone_prompt": voice, "instruct": None if voice else instruct or None,
               "language": os.environ.get("OV_LANGUAGE") or None,
               "speed": float(os.environ.get("OV_SPEED") or 1.0), "num_step": int(os.environ.get("OV_STEPS") or 32)}
    # num_step и прочие поля OmniVoiceGenerationConfig идут через **kwargs generate
    options = {k: v for k, v in options.items() if v is not None and (k in accepted or "kwargs" in accepted)}
    print("Голос:", mode, "· аргументы generate:", sorted(options), flush=True)
    lock = asyncio.Lock()  # одна видеокарта — один синтез за раз
    completed = 0

    async def health(request):
        return web.json_response({"status": "ok", "engine": "omnivoice", "model": name, "device": device, "gpu": gpu,
                                  "threads": torch.get_num_threads(), "format": "f32le", "sample_rate": rate,
                                  "voice": mode,
                                  "busy": lock.locked(), "completed": completed})

    async def speech(request):
        nonlocal completed
        data = await request.json()
        text = data.get("input")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise web.HTTPBadRequest(text="input: 1..2000 characters")
        async with lock:
            try:
                audio = await asyncio.to_thread(model.generate, text=text.strip(), **options)
            except ValueError as exc:  # неподдерживаемое описание голоса и т. п. — причина клиенту, не 500
                raise web.HTTPBadRequest(text=str(exc).splitlines()[0][:300])
        completed += 1
        wave = audio[0] if isinstance(audio, (list, tuple)) else audio
        if hasattr(wave, "detach"):
            wave = wave.detach().float().cpu().numpy()
        pcm = np.asarray(wave, dtype=np.float32).reshape(-1).tobytes()
        response = web.StreamResponse(headers={"Content-Type": "application/octet-stream", "X-Audio-Format": "f32le",
                                               "X-Audio-Sample-Rate": str(rate)})
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
    await web.TCPSite(runner, os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8778"))).start()
    print("OmniVoice готов на порту", os.environ.get("PORT", "8778"), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
