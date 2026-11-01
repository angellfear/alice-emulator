"""Qwen3-TTS 1.7B: OpenAI Speech API /v1/audio/speech — запечённый голос по эталону или голос по описанию.

Два режима (выбирает наличие эталона):
* есть QW_REF_AUDIO — модель Base: эталон обрабатывается один раз (create_voice_clone_prompt), результат
  хранится рядом с эталоном в <эталон>.<хеш>.pt; синтез идёт без эталона;
* эталона нет — модель VoiceDesign: голос по текстовому описанию QW_INSTRUCT (лучше на английском).
Формат ответа — tools/speech_common.py. Окружение (пакет voice_stack/packages/qwen-tts.json):
  QW_MODEL_BASE / QW_MODEL_DESIGN  репозитории Hugging Face (скачиваются при первом запуске в HF_HOME пакета)
  QW_DEVICE      cpu | cuda      QW_THREADS  потоков процессора     QW_LANGUAGE  язык текста (Russian)
  QW_REF_AUDIO   эталон WAV      QW_REF_TEXT его текст (пусто — только тембр, качество ниже)
  QW_INSTRUCT    описание голоса (без эталона)
  QW_TEMPERATURE, QW_TOP_K       параметры выборки
  CUDA_VISIBLE_DEVICES           какая видеокарта видна процессу
"""
import os

import speech_common as common

DEFAULT_RATE = 24000


class Engine:
    """model — Qwen3TTSModel; prompt — запечённый голос (список VoiceClonePromptItem) или None (описание)."""

    def __init__(self, model, prompt, mode, device):
        self.model, self.prompt, self.mode, self.device = model, prompt, mode, device
        self.language = common.env("QW_LANGUAGE", "Russian")
        self.instruct = common.env("QW_INSTRUCT", "A calm adult woman with a clear, warm voice")
        self.options = {"temperature": common.env_float("QW_TEMPERATURE", 0.9), "top_k": common.env_int("QW_TOP_K", 50)}
        self.rate = DEFAULT_RATE  # уточняется по ответу модели

    def synth(self, text, voice=None):
        if self.prompt is not None:
            waves, rate = self.model.generate_voice_clone(text=text, language=self.language,
                                                          voice_clone_prompt=self.prompt, **self.options)
        else:
            waves, rate = self.model.generate_voice_design(text=text, language=self.language,
                                                           instruct=self.instruct, **self.options)
        self.rate = int(rate)
        return waves[0]

    def info(self):
        return {"engine": "qwen3-tts", "model": common.env("QW_MODEL", "Qwen3-TTS-12Hz-1.7B"), "device": self.device,
                "voice": self.mode,
                "state": {"model_loaded": True, "voice_mode": "baked" if self.prompt is not None else "design"}}


def save_prompt(items, path):
    import torch

    def cpu(value):
        return value.detach().cpu() if hasattr(value, "detach") else value
    torch.save([{"ref_code": cpu(item.ref_code), "ref_spk_embedding": cpu(item.ref_spk_embedding),
                 "x_vector_only_mode": bool(item.x_vector_only_mode), "icl_mode": bool(item.icl_mode),
                 "ref_text": item.ref_text} for item in items], path)


def load_prompt(path, device):
    import torch
    from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem

    def to(value):
        return value.to(device) if hasattr(value, "to") else value
    return [VoiceClonePromptItem(ref_code=to(item["ref_code"]), ref_spk_embedding=to(item["ref_spk_embedding"]),
                                 x_vector_only_mode=item["x_vector_only_mode"], icl_mode=item["icl_mode"],
                                 ref_text=item["ref_text"])
            for item in torch.load(path, map_location="cpu", weights_only=True)]


def load_engine():
    import soundfile
    import torch
    from qwen_tts import Qwen3TTSModel

    threads = common.env_int("QW_THREADS", 0)
    if threads > 0:
        torch.set_num_threads(threads)
    device = common.env("QW_DEVICE", "cuda")
    if device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA недоступна: нет видеокарты NVIDIA или установлен PyTorch без CUDA")
    if device == "cuda":
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    else:
        dtype = torch.float32
    ref_audio = common.env("QW_REF_AUDIO")
    name = common.env("QW_MODEL_BASE", "Qwen/Qwen3-TTS-12Hz-1.7B-Base") if ref_audio else \
        common.env("QW_MODEL_DESIGN", "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign")
    print("Загрузка модели", name, device, str(dtype).replace("torch.", ""), "(при первом запуске скачивается ~4 ГБ)", flush=True)
    model = Qwen3TTSModel.from_pretrained(name, device_map="cuda:0" if device == "cuda" else "cpu",
                                          dtype=dtype, attn_implementation="sdpa")
    if not ref_audio:
        return Engine(model, None, "по описанию", device)
    ref_text = common.env("QW_REF_TEXT")

    def create(text):
        wave, rate = soundfile.read(ref_audio, dtype="float32", always_2d=True)
        return model.create_voice_clone_prompt(ref_audio=(wave.mean(axis=1), rate), ref_text=text,
                                               x_vector_only_mode=not text)

    prompt, mode = common.baked_voice(ref_audio, ref_text, create, save_prompt, lambda path: load_prompt(path, model.device))
    return Engine(model, prompt, mode, device)


def build_app(engine):
    return common.tts_app(engine.synth, lambda: engine.rate, engine.info)


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    common.run(lambda: build_app(load_engine()), 5021, "Qwen3-TTS")
