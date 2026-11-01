"""Whisper на faster-whisper (CTranslate2): OpenAI Audio API /v1/audio/transcriptions.

Формат запросов и ответов — tools/speech_common.py. Окружение (пакет voice_stack/packages/faster-whisper.json):
  WHISPER_MODEL_DIR  папка модели (скачивается при установке пакета)   WHISPER_MODEL  её имя — для /health
  WHISPER_DEVICE     cpu | cuda    WHISPER_COMPUTE  auto | float16 | int8_float16 | int8 | float32
                     (auto: float16 на видеокарте, int8 на процессоре)
  WHISPER_BEAM       ширина луча (1 — быстрее, 5 — точнее)    WHISPER_VAD  1 — вырезать паузы Silero VAD (меньше «галлюцинаций»)
  WHISPER_THREADS    потоков процессора    WHISPER_LANGUAGE  язык, если запрос его не указал (пусто — определяет сама)
  WHISPER_WORKERS    параллельных запросов
  CUDA_VISIBLE_DEVICES                     какая видеокарта видна процессу
"""
import os

import speech_common as common


def resolve_compute(value, device):
    value = (value or "auto").lower()
    if value == "auto":
        return "float16" if device == "cuda" else "int8"
    return value


class Engine:
    def __init__(self, model):
        self.model = model
        self.name = common.env("WHISPER_MODEL", "whisper")
        self.device = common.env("WHISPER_DEVICE", "cpu")
        self.compute = resolve_compute(common.env("WHISPER_COMPUTE", "auto"), self.device)
        self.beam = max(1, common.env_int("WHISPER_BEAM", 5))
        self.vad = common.env_flag("WHISPER_VAD", True)
        self.language = os.environ.get("WHISPER_LANGUAGE", "ru").strip()  # пусто — язык определяет модель

    def transcribe(self, samples, language=None):
        segments, _ = self.model.transcribe(samples, language=language or self.language or None, beam_size=self.beam,
                                            vad_filter=self.vad, condition_on_previous_text=False)
        return " ".join(part.text.strip() for part in segments if part.text.strip())

    def info(self):
        return {"engine": "faster-whisper", "model": self.name, "device": self.device, "compute_type": self.compute,
                "voice": "", "state": {"model_loaded": True, "vad": self.vad, "beam_size": self.beam}}


def load_model():
    from faster_whisper import WhisperModel
    device = common.env("WHISPER_DEVICE", "cpu")
    if device == "cuda":
        common.preload_cuda_libs()
    compute = resolve_compute(common.env("WHISPER_COMPUTE", "auto"), device)
    print("Загрузка модели", common.env("WHISPER_MODEL"), device, compute, flush=True)
    try:
        return WhisperModel(common.env("WHISPER_MODEL_DIR"), device=device, compute_type=compute,
                            cpu_threads=common.env_int("WHISPER_THREADS", 0), local_files_only=True)
    except ValueError as exc:
        if device == "cuda":
            raise SystemExit("CUDA недоступна или не хватает библиотек cuBLAS/cuDNN: " + str(exc)) from exc
        raise


def build_app(engine):
    return common.asr_app(engine.transcribe, engine.info, capacity=common.env_int("WHISPER_WORKERS", 1))


if __name__ == "__main__":
    common.run(lambda: build_app(Engine(load_model())), 5017, "Whisper")
