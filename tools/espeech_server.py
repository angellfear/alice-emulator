"""ESpeech TTS-1 (русский F5-TTS): OpenAI Speech API /v1/audio/speech, клонирование голоса по эталону.

Модель — дообучение F5TTS_v1_Base на русской речи: на каждый запрос подставляется эталон (WAV до 12 с)
и его текст. Ударения ставит RUAccent (знак «+» перед ударной гласной в тексте отключает расстановку).
Формат ответа — tools/speech_common.py. Окружение (пакет voice_stack/packages/espeech.json):
  ES_MODEL_DIR   папка с espeech_*.pt и vocab.txt            ES_VOCODER_DIR  папка вокодера Vocos
  ES_RUACCENT_DIR  папка моделей RUAccent                    ES_ACCENT  1 — ставить ударения автоматически
  ES_DEVICE      cpu | cuda     ES_THREADS  потоков процессора
  ES_REF_AUDIO   эталон WAV     ES_REF_TEXT его текст (обязателен)
  ES_SPEED       скорость речи  ES_STEPS    шагов синтеза (качество/время)   ES_SEED  0 — случайный
  CUDA_VISIBLE_DEVICES          какая видеокарта видна процессу
"""
import glob
import os

import speech_common as common


class Engine:
    """tts — f5_tts.api.F5TTS; accent — RUAccent или None."""

    def __init__(self, tts, accent, ref_audio, ref_text):
        self.tts, self.accent = tts, accent
        self.ref_audio, self.ref_text = ref_audio, ref_text
        self.speed = common.env_float("ES_SPEED", 1.0)
        self.steps = common.env_int("ES_STEPS", 32)
        self.seed = common.env_int("ES_SEED", 0) or None
        self.device = common.env("ES_DEVICE", "cuda")
        self.rate = int(getattr(tts, "target_sample_rate", 24000))
        self.ref_text_stressed = self.stress(ref_text)

    def stress(self, text):
        """Ударения RUAccent; готовая разметка «+» остаётся как есть."""
        if self.accent is None or "+" in text:
            return text
        return self.accent.process_all(text)

    def synth(self, text, voice=None):
        wave, _, _ = self.tts.infer(self.ref_audio, self.ref_text_stressed, self.stress(text), speed=self.speed,
                                    nfe_step=self.steps, seed=self.seed, show_info=lambda *args: None)
        return wave

    def info(self):
        return {"engine": "espeech", "model": common.env("ES_MODEL", "ESpeech-TTS-1"), "device": self.device,
                "voice": "клонирование по эталону (" + os.path.basename(self.ref_audio) + ")",
                "state": {"model_loaded": True, "voice_mode": "clone", "accent": self.accent is not None}}


def load_engine():
    import soundfile
    import torch
    import torchaudio

    def load_audio(path, *args, **kwargs):
        """torchaudio ≥2.9 читает аудио через torchcodec (нужны DLL FFmpeg) — эталон читаем soundfile."""
        data, rate = soundfile.read(path, dtype="float32", always_2d=True)
        return torch.from_numpy(data.T.copy()), rate

    torchaudio.load = load_audio
    ref_audio, ref_text = common.env("ES_REF_AUDIO"), " ".join(common.env("ES_REF_TEXT").split())
    if not ref_audio or not os.path.exists(ref_audio):
        raise SystemExit("Загрузите эталонную запись голоса: ESpeech клонирует голос по эталону (WAV до 12 с)")
    if not ref_text:
        raise SystemExit("Укажите текст эталонной записи (с ударениями «+» — точнее)")
    folder = common.env("ES_MODEL_DIR")
    checkpoints = sorted(glob.glob(os.path.join(folder, "*.pt")))
    if not checkpoints:
        raise SystemExit("Модель не найдена в " + folder + ": переустановите пакет")
    threads = common.env_int("ES_THREADS", 0)
    if threads > 0:
        torch.set_num_threads(threads)
    device = common.env("ES_DEVICE", "cuda")
    if device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA недоступна: нет видеокарты NVIDIA или установлен PyTorch без CUDA")
    from f5_tts.api import F5TTS
    print("Загрузка модели", os.path.basename(checkpoints[0]), device, flush=True)
    tts = F5TTS(model="F5TTS_v1_Base", ckpt_file=checkpoints[0], vocab_file=os.path.join(folder, "vocab.txt"),
                vocoder_local_path=common.env("ES_VOCODER_DIR") or None, device=device)
    accent = None
    if common.env_flag("ES_ACCENT", True):
        from ruaccent import RUAccent
        accent = RUAccent()
        accent.load(omograph_model_size="turbo3.1", use_dictionary=True, tiny_mode=False,
                    workdir=common.env("ES_RUACCENT_DIR") or None)
    return Engine(tts, accent, ref_audio, ref_text)


def build_app(engine):
    return common.tts_app(engine.synth, engine.rate, engine.info)


if __name__ == "__main__":
    common.run(lambda: build_app(load_engine()), 5020, "ESpeech")
