"""Coqui XTTS-v2: OpenAI Speech API /v1/audio/speech — клонирование голоса с запечёнными латентами.

Латенты говорящего (gpt_cond_latent + speaker_embedding) считаются по эталону один раз и хранятся рядом с
ним в <эталон>.<хеш>.pt; синтез идёт без эталона. Ответ — float32 PCM потоком (24 кГц), формат — tools/speech_common.py.
Окружение (пакет voice_stack/packages/xtts.json):
  XTTS_MODEL_DIR  папка модели (config.json, model.pth, vocab.json, dvae.pth, mel_stats.pth, speakers_xtts.pth)
  XTTS_DEVICE     cpu | cuda     XTTS_THREADS  потоков процессора
  XTTS_REF_AUDIO  эталон WAV (6–30 с)     XTTS_REF_TEXT  не используется моделью
  XTTS_LANGUAGE   язык текста (ru)        XTTS_TEMPERATURE, XTTS_SPEED   выборка и скорость речи
  XTTS_STREAM     1 — отдавать звук по мере синтеза
  CUDA_VISIBLE_DEVICES                    какая видеокарта видна процессу
Лицензия модели CPML — только некоммерческое использование.
"""
import os

import numpy as np

import speech_common as common

RATE = 24000


class Engine:
    """model — TTS.tts.models.xtts.Xtts; latents — (gpt_cond_latent, speaker_embedding)."""

    def __init__(self, model, latents, mode, device):
        self.model, self.latents, self.mode, self.device = model, latents, mode, device
        self.language = common.env("XTTS_LANGUAGE", "ru")
        self.options = {"temperature": common.env_float("XTTS_TEMPERATURE", 0.75),
                        "speed": common.env_float("XTTS_SPEED", 1.0), "enable_text_splitting": True}

    def synth(self, text, voice=None):
        output = self.model.inference(text, self.language, *self.latents, **self.options)
        return np.asarray(output["wav"], dtype=np.float32).reshape(-1)

    def stream(self, text, voice=None):
        for chunk in self.model.inference_stream(text, self.language, *self.latents, **self.options):
            yield chunk.detach().float().cpu().numpy().reshape(-1) if hasattr(chunk, "detach") else chunk

    def info(self):
        return {"engine": "xtts-v2", "model": "XTTS-v2", "device": self.device, "voice": self.mode,
                "state": {"model_loaded": True, "voice_mode": "baked", "streaming": common.env_flag("XTTS_STREAM", True)}}


def save_latents(latents, path):
    import torch
    torch.save({"gpt_cond_latent": latents[0].detach().cpu(), "speaker_embedding": latents[1].detach().cpu()}, path)


def load_latents(path, device):
    import torch
    data = torch.load(path, map_location="cpu", weights_only=True)
    return data["gpt_cond_latent"].to(device), data["speaker_embedding"].to(device)


def load_engine():
    import soundfile
    import torch
    import torchaudio

    def load_audio(path, *args, **kwargs):
        """torchaudio ≥2.9 читает аудио через torchcodec (нужны DLL FFmpeg) — эталон читаем soundfile."""
        data, rate = soundfile.read(path, dtype="float32", always_2d=True)
        return torch.from_numpy(data.T.copy()), rate

    torchaudio.load = load_audio
    ref_audio = common.env("XTTS_REF_AUDIO")
    if not ref_audio or not os.path.exists(ref_audio):
        raise SystemExit("Загрузите эталонную запись голоса (WAV, 6–30 с): XTTS клонирует голос по эталону")
    threads = common.env_int("XTTS_THREADS", 0)
    if threads > 0:
        torch.set_num_threads(threads)
    device = common.env("XTTS_DEVICE", "cuda")
    if device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA недоступна: нет видеокарты NVIDIA или установлен PyTorch без CUDA")
    folder = common.env("XTTS_MODEL_DIR")
    if not os.path.exists(os.path.join(folder, "model.pth")):
        raise SystemExit("Модель не найдена в " + folder + ": переустановите пакет")
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    print("Загрузка модели XTTS-v2", device, flush=True)
    config = XttsConfig()
    config.load_json(os.path.join(folder, "config.json"))
    model = Xtts(config)
    model.load_checkpoint(config, checkpoint_dir=folder, use_deepspeed=False)
    model.to(torch.device("cuda" if device == "cuda" else "cpu"))
    latents, mode = common.baked_voice(
        ref_audio, common.env("XTTS_REF_TEXT"),
        lambda text: model.get_conditioning_latents(audio_path=[ref_audio]),
        save_latents, lambda path: load_latents(path, model.device))
    return Engine(model, latents, mode, device)


def build_app(engine):
    return common.tts_app(engine.synth, RATE, engine.info,
                          stream=engine.stream if common.env_flag("XTTS_STREAM", True) else None)


if __name__ == "__main__":
    common.run(lambda: build_app(load_engine()), 5022, "XTTS")
