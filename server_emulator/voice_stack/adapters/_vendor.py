"""Общие помощники облачных адаптеров: запросы с понятными русскими ошибками, кеш списков
голосов и преобразование звука поставщиков в Ogg Opus / float32 PCM конвейера.

Не адаптер (в SERVICES не участвует). Ключи и токены нигде не попадают в тексты ошибок.
"""
import asyncio
import hashlib
import io
import json
import time
import wave

from aiohttp import ClientError, ClientTimeout

from ...auth import secret

LIMIT = 32 * 1024 ** 2  # ответ поставщика не больше 32 MiB
PCM_FORMAT = "audio/x-pcm;bit=16;rate={rate};channels=1"
REASONS = {
    400: "Некорректный запрос",
    401: "Неверный ключ",
    402: "Недостаточно средств или кредитов",
    403: "Нет доступа: проверьте ключ, права и тариф",
    404: "Не найдено: проверьте модель, голос или адрес",
    408: "Поставщик не дождался запроса",
    413: "Слишком большой запрос",
    415: "Формат не поддерживается",
    422: "Поставщик отклонил параметры запроса",
    429: "Превышен лимит бесплатного тарифа или частоты запросов",
}


def api_key(config, vendor, field="api_key"):
    """Ключ из настроек (env:VAR раскрывается); пустой — понятная ошибка."""
    value = secret((config or {}).get(field)).strip()
    if not value:
        raise ValueError(f"{vendor}: введите ключ")
    return value


def clean(text, *secrets):
    """Короткий безопасный фрагмент ответа поставщика для текста ошибки (без ключей)."""
    text = " ".join(str(text or "").split())
    for item in secrets:
        if item:
            text = text.replace(item, "***")
    return text[:200]


def detail_of(body):
    """Сообщение об ошибке из JSON-ответа поставщика (разные API — разные ключи)."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return ""
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in ("message", "msg", "error_description", "err_msg", "detail"):
                if isinstance(item.get(key), str):
                    return item[key]
            stack.extend(v for k, v in item.items() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            stack.extend(item)
    return ""


def failure(vendor, status, body=b"", *secrets):
    """ValueError с русским пояснением по HTTP-статусу."""
    reason = REASONS.get(status) or ("Сервис временно недоступен" if status >= 500 else "Ошибка запроса")
    detail = clean(detail_of(body), *secrets)
    return ValueError(f"{vendor}: {reason} (HTTP {status})" + (f": {detail}" if detail else ""))


async def read_body(response, limit=LIMIT):
    body = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > limit:
            raise ValueError("Ответ поставщика слишком большой")
    return bytes(body)


async def call(runtime, vendor, method, url, *secrets, timeout=90, **kwargs):
    """Запрос → (тело, заголовки). Статус ≥400 и сетевые сбои — ValueError без секретов."""
    try:
        async with runtime.lifecycle.client_session().request(
                method, url, timeout=ClientTimeout(total=timeout), **kwargs) as response:
            body = await read_body(response)
            if response.status >= 400:
                raise failure(vendor, response.status, body, *secrets)
            return body, response.headers
    except asyncio.TimeoutError:
        raise ValueError(f"{vendor}: поставщик не ответил вовремя") from None
    except ClientError as exc:
        raise ValueError(f"{vendor}: нет соединения ({type(exc).__name__})") from None


async def call_json(runtime, vendor, method, url, *secrets, **kwargs):
    body, _ = await call(runtime, vendor, method, url, *secrets, **kwargs)
    try:
        return json.loads(body)
    except ValueError:
        raise ValueError(f"{vendor}: ответ не в формате JSON") from None


async def stream_chunks(runtime, vendor, method, url, *secrets, timeout=90, size=8192, **kwargs):
    """Тело ответа потоком; статус ≥400 — ValueError (до первого чанка)."""
    try:
        async with runtime.lifecycle.client_session().request(
                method, url, timeout=ClientTimeout(total=timeout), **kwargs) as response:
            if response.status >= 400:
                raise failure(vendor, response.status, await read_body(response, 65536), *secrets)
            total = 0
            async for chunk in response.content.iter_chunked(size):
                total += len(chunk)
                if total > LIMIT:
                    raise ValueError("Ответ поставщика слишком большой")
                yield chunk
    except asyncio.TimeoutError:
        raise ValueError(f"{vendor}: поставщик не ответил вовремя") from None
    except ClientError as exc:
        raise ValueError(f"{vendor}: нет соединения ({type(exc).__name__})") from None


# --- кеш списков голосов (на время работы эмулятора) -----------------------------------

_voices = {}
VOICES_TTL = 600  # секунд: «Обновить список» (config["refresh"]) сбрасывает кеш сразу


def _voices_key(vendor, token):
    return vendor, hashlib.sha256(token.encode()).hexdigest()


def voices_cached(vendor, token, refresh=False):
    """Список из кеша или None. token — всё, от чего зависит список (ключ, регион, движок)."""
    found = None if refresh else _voices.get(_voices_key(vendor, token))
    return found[1] if found and time.monotonic() - found[0] < VOICES_TTL else None


def voices_store(vendor, token, items):
    _voices[_voices_key(vendor, token)] = (time.monotonic(), items)
    return items


def option(value, label, lang="", gender=""):
    return {"value": value, "label": label, "lang": lang, "gender": gender}


# --- звук ---------------------------------------------------------------------------------


async def bytes_chunks(data, size=65536):
    for pos in range(0, len(data), size):
        yield data[pos:pos + size]


def pcm_pages(source, rate):
    """Поток PCM16 mono (async-итератор bytes) → Ogg Opus страницы."""
    from ...audio_stream import ogg_pages, transcode
    return ogg_pages(transcode(source, PCM_FORMAT.format(rate=rate), "opus"))


async def pcm_to_ogg(pcm, rate):
    """PCM16 mono целиком → байты Ogg Opus (для speech)."""
    if not pcm:
        raise ValueError("Поставщик вернул пустой звук")
    return b"".join([page async for page in pcm_pages(bytes_chunks(pcm), rate)])


def pcm_float(pcm, rate):
    """PCM16 mono → (float32-массив, частота) для звукового кеша."""
    import numpy as np
    if not pcm:
        raise ValueError("Поставщик вернул пустой звук")
    usable = len(pcm) - len(pcm) % 2
    return np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32) / 32768.0, rate


async def collect(source):
    out = bytearray()
    async for chunk in source:
        out.extend(chunk)
    return bytes(out)


async def ogg_normalized(data):
    """Ogg Opus поставщика → Ogg Opus с заголовком и EOS-страницей; иначе перекодируется."""
    from ...audio_buffer import ogg_pages
    from ...audio_stream import transcode
    try:
        ogg_pages(data)
        return data
    except ValueError:
        return b"".join([page async for page in transcode(bytes_chunks(data), "audio/ogg", "opus")])


def wav_pcm(data):
    """WAV (PCM16) → (PCM16 mono, частота): стерео сводится в моно."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            width, channels, rate = wav.getsampwidth(), wav.getnchannels(), wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError):
        raise ValueError("Поставщик вернул неверный WAV") from None
    if width != 2 or not 1 <= channels <= 2:
        raise ValueError("Поддержан только WAV PCM16")
    if channels == 2:
        import numpy as np
        pair = np.frombuffer(frames[:len(frames) - len(frames) % 4], dtype="<i2").reshape(-1, 2)
        frames = pair.mean(axis=1).astype("<i2").tobytes()
    return frames, rate


async def input_pcm16(audio, filename=""):
    """Вход ASR конвейера (WAV или Ogg Opus) → PCM16 mono 16 кГц."""
    from ...audio_stream import transcode
    if audio[:4] == b"RIFF":
        source = "audio/wav"
    elif audio[:4] == b"OggS":
        source = "audio/ogg"
    else:
        raise ValueError("Поддержаны WAV и Ogg Opus")
    return b"".join([chunk async for chunk in transcode(bytes_chunks(audio), source, "pcm16")])


def pcm_wav(pcm, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()
