"""OpenAI-совместимый HTTP: /v1/chat/completions, /v1/audio/transcriptions, /v1/audio/speech."""
from aiohttp import ClientTimeout, FormData

from ...auth import secret

TITLE = "OpenAI-совместимый"
DESCRIPTION = "Любой сервер с OpenAI API: OpenAI, Groq, DeepSeek, OpenRouter, Gemini, Ollama, vLLM, speaches, свой."
URL = {"key": "url", "label": "Адрес сервиса", "type": "text", "required": True,
       "help": "Полный адрес запроса, например https://api.openai.com/v1/chat/completions"}
KEY = {"key": "api_key", "label": "API-ключ", "type": "password", "link": "https://platform.openai.com/api-keys",
       "help": "Ключ сервиса: OpenAI, Groq (console.groq.com/keys), DeepSeek, OpenRouter, Google AI Studio; у локальных серверов не нужен"}
MODEL = {"key": "model", "label": "Модель", "type": "text", "required": True}
OPENAI_VOICES = "alloy, ash, ballad, cedar, coral, echo, fable, marin, nova, onyx, sage, shimmer, verse"
SERVICES = {
    "llm": {
        "fields": [URL, KEY, MODEL,
                   {"key": "temperature", "label": "Температура", "type": "number", "step": 0.1, "group": "advanced"},
                   {"key": "reasoning_effort", "label": "Глубина размышления", "type": "select", "group": "advanced",
                    "options": [{"value": "", "label": "Не передавать"}, {"value": "none", "label": "Нет (для инструментов в OpenAI)"},
                                {"value": "low", "label": "Малая"}, {"value": "medium", "label": "Средняя"},
                                {"value": "high", "label": "Большая"}],
                    "help": "Только для моделей-«рассуждающих» (OpenAI, Gemini); остальным серверам не передаётся"}],
        "defaults": {"url": "https://api.openai.com/v1/chat/completions", "model": "gpt-4o-mini", "temperature": 0.3,
                     "reasoning_effort": ""},
        "examples": {
            "OpenAI (Online)": {"url": "https://api.openai.com/v1/chat/completions", "model": "gpt-6-luna",
                                "temperature": 0.3, "reasoning_effort": "none"},
            "Groq Llama (Online Free)": {"url": "https://api.groq.com/openai/v1/chat/completions",
                                         "model": "llama-3.3-70b-versatile", "temperature": 0.6, "reasoning_effort": ""},
            "Groq GPT-OSS (Online Free)": {"url": "https://api.groq.com/openai/v1/chat/completions",
                                           "model": "openai/gpt-oss-20b", "temperature": 0.6, "reasoning_effort": ""},
            "DeepSeek (Online)": {"url": "https://api.deepseek.com/chat/completions", "model": "deepseek-v4-flash",
                                  "temperature": 0.5, "reasoning_effort": ""},
            "OpenRouter бесплатные модели (Online Free)": {"url": "https://openrouter.ai/api/v1/chat/completions",
                                                           "model": "google/gemma-4-31b-it:free", "temperature": 0.5,
                                                           "reasoning_effort": ""},
            "OpenRouter (Online)": {"url": "https://openrouter.ai/api/v1/chat/completions",
                                    "model": "google/gemini-3.8-flash", "temperature": 0.5, "reasoning_effort": ""},
            "Gemini, OpenAI-совместимый (Online Free)": {
                "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                "model": "gemini-3.8-flash", "temperature": 0.5, "reasoning_effort": "low"},
            "Ollama": {"url": "http://127.0.0.1:11434/v1/chat/completions", "model": "qwen2.5:7b", "temperature": 0.5,
                       "reasoning_effort": ""},
            "vLLM": {"url": "http://127.0.0.1:8000/v1/chat/completions", "model": "Qwen/Qwen2.5-7B-Instruct",
                     "temperature": 0.6, "reasoning_effort": ""},
        },
    },
    "asr": {
        "fields": [URL, KEY, MODEL,
                   {"key": "language", "label": "Язык", "type": "text", "help": "Код языка: ru, en… Пусто — определить автоматически"}],
        "defaults": {"url": "https://api.openai.com/v1/audio/transcriptions", "model": "whisper-1", "language": "ru"},
        "examples": {
            "OpenAI gpt-transcribe (Online)": {"url": "https://api.openai.com/v1/audio/transcriptions",
                                               "model": "gpt-transcribe", "language": "ru"},
            "OpenAI gpt-4o-transcribe (Online)": {"url": "https://api.openai.com/v1/audio/transcriptions",
                                                  "model": "gpt-4o-transcribe", "language": "ru"},
            "OpenAI gpt-4o-mini-transcribe (Online)": {"url": "https://api.openai.com/v1/audio/transcriptions",
                                                       "model": "gpt-4o-mini-transcribe", "language": "ru"},
            "Groq Whisper large-v3 (Online Free)": {"url": "https://api.groq.com/openai/v1/audio/transcriptions",
                                                    "model": "whisper-large-v3", "language": "ru"},
            "Groq Whisper large-v3-turbo (Online Free)": {"url": "https://api.groq.com/openai/v1/audio/transcriptions",
                                                          "model": "whisper-large-v3-turbo", "language": "ru"},
            "faster-whisper (speaches)": {"url": "http://127.0.0.1:8000/v1/audio/transcriptions",
                                          "model": "Systran/faster-whisper-small", "language": "ru"},
        },
    },
    "tts": {
        "voice_modes": ["preset"],
        "fields": [URL, KEY, MODEL,
                   {"key": "voice", "label": "Голос", "type": "text", "required": True,
                    "help": "Голоса OpenAI: " + OPENAI_VOICES + "; у других серверов — их названия голосов"},
                   {"key": "response_format", "label": "Формат аудио", "type": "select", "group": "advanced", "options": [
                       {"value": "opus", "label": "Ogg Opus (целиком)"},
                       {"value": "pcm_f32le", "label": "PCM float32 (потоком)"},
                       {"value": "wav", "label": "WAV (потоком)"}]}],
        "defaults": {"url": "https://api.openai.com/v1/audio/speech", "model": "tts-1", "voice": "alloy", "response_format": "opus"},
        "examples": {
            "OpenAI gpt-4o-mini-tts (Online)": {"url": "https://api.openai.com/v1/audio/speech", "model": "gpt-4o-mini-tts",
                                                "voice": "coral", "response_format": "opus"},
        },
    },
}
MATCH = {"llm": r"", "asr": r"/audio/transcriptions", "tts": r"/audio/speech"}
LIMIT = 16 * 1024 ** 2


def _headers(config):
    return {"Authorization": "Bearer " + secret(config["api_key"])} if secret(config.get("api_key")) else {}


async def _post(runtime, config, kind, **kwargs):
    if not config.get("url"):
        raise ValueError(f"Настройте endpoint {kind}")
    response = await runtime.lifecycle.client_session().post(
        config["url"], headers=_headers(config), timeout=ClientTimeout(total=90), **kwargs)
    async with response:
        if response.status >= 400:
            raise ValueError(f"{kind}: HTTP {response.status} от провайдера")
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > LIMIT:
                raise ValueError("Ответ провайдера >16 MiB")
        return bytes(body), response.headers.get("Content-Type", "application/octet-stream")


async def complete(runtime, config, payload):
    import json
    if config.get("reasoning_effort") and "reasoning_effort" not in payload:
        payload = {**payload, "reasoning_effort": config["reasoning_effort"]}
    raw, _ = await _post(runtime, config, "model", json=payload)
    return json.loads(raw)


async def transcribe(runtime, config, audio, filename):
    import json
    form = FormData()
    form.add_field("file", audio, filename=filename, content_type="application/octet-stream")
    form.add_field("model", config.get("model", ""))
    if config.get("language"):
        form.add_field("language", config["language"])
    raw, _ = await _post(runtime, config, "stt", data=form)
    return json.loads(raw).get("text", "")


async def speech(runtime, config, payload):
    return await _post(runtime, config, "tts", json=payload)


def streams(config):
    return config.get("response_format") in ("wav", "pcm_f32le")


async def synth_pcm(runtime, config, text):
    """Синтез целиком в float32 PCM (для звукового кеша): → (numpy-массив, частота)."""
    import numpy as np
    raw = bytearray()
    async with runtime.lifecycle.client_session().post(
            config["url"], headers=_headers(config), timeout=ClientTimeout(total=180),
            json={"model": config["model"], "voice": config["voice"],
                  "response_format": config["response_format"], "input": text}) as response:
        if response.status != 200:
            raise ValueError("TTS HTTP " + str(response.status))
        rate = int(response.headers.get("X-Audio-Sample-Rate", "0"))
        if not 8000 <= rate <= 192000 or response.headers.get("X-Audio-Format") != "f32le":
            raise ValueError("TTS must declare float32 PCM sample rate")
        async for chunk in response.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw) > LIMIT:
                raise ValueError("Ответ провайдера >16 MiB")
    return np.frombuffer(bytes(raw[:len(raw) - len(raw) % 4]), dtype=np.float32).copy(), rate


async def speech_stream(runtime, config, text):
    """PCM float32 потоком (сервер сообщает X-Audio-Sample-Rate) → Ogg Opus страницы."""
    from ...audio_stream import ogg_pages, transcode
    async with runtime.lifecycle.client_session().post(
            config["url"], headers=_headers(config), timeout=ClientTimeout(total=90),
            json={"model": config["model"], "voice": config["voice"],
                  "response_format": config["response_format"], "input": text}) as response:
        if response.status != 200:
            raise ValueError("TTS stream HTTP " + str(response.status))
        source = response.content.iter_chunked(8192)
        if config["response_format"] not in ("opus", "ogg"):
            rate = int(response.headers.get("X-Audio-Sample-Rate", "0"))
            if not 8000 <= rate <= 192000 or response.headers.get("X-Audio-Format") != "f32le":
                raise ValueError("TTS must declare float32 PCM sample rate")
            source = transcode(source, f"audio/x-pcm;bit=32;rate={rate};channels=1;float=1", "opus")
        async for page in ogg_pages(source):
            yield page
