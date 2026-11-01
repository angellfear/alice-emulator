"""Общие помощники облачных речевых адаптеров (не адаптер: без SERVICES, в списках не показывается)."""
import json
import struct

from aiohttp import ClientConnectionError, ClientTimeout

LIMIT = 32 * 1024 ** 2

STATUS_TEXT = {
    400: "Некорректный запрос — проверьте параметры",
    401: "Неверный ключ или токен",
    402: "Требуется оплата — баланс исчерпан",
    403: "Нет доступа к сервису — проверьте права ключа и каталог",
    404: "Сервис или модель не найдены — проверьте адрес и название модели",
    413: "Слишком большой запрос",
    422: "Параметры запроса не приняты",
    429: "Превышен лимит запросов (возможно, исчерпан бесплатный тариф)",
}


def failure(vendor, status, body=b""):
    """Понятная ошибка (ValueError) по статусу поставщика; ключи и токены в текст не попадают."""
    text = STATUS_TEXT.get(status) or ("Сервис временно недоступен" if status >= 500 else "Отказ сервиса")
    detail = ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            raw = data.get("error_message") or data.get("message") or data.get("error") or ""
            detail = raw.get("message", "") if isinstance(raw, dict) else str(raw)
    except (ValueError, TypeError):
        pass
    detail = " ".join(detail.split())[:160]
    return ValueError(f"{vendor}: {text} (HTTP {status})" + (f": {detail}" if detail else ""))


async def call(runtime, vendor, method, url, *, timeout=90, **kwargs):
    """Запрос → (статус, тело, заголовки); сетевые сбои — ValueError без URL и токенов."""
    try:
        async with runtime.lifecycle.client_session().request(
                method, url, timeout=ClientTimeout(total=timeout), **kwargs) as response:
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > LIMIT:
                    raise ValueError(f"{vendor}: ответ больше 32 МБ")
            return response.status, bytes(body), response.headers
    except ValueError:
        raise
    except TimeoutError:
        raise ValueError(f"{vendor}: нет ответа от сервиса — проверьте сеть") from None
    except ClientConnectionError as exc:
        raise ValueError(f"{vendor}: нет соединения ({type(exc).__name__}) — проверьте сеть и адрес") from None


async def stream_call(runtime, vendor, method, url, *, timeout=90, **kwargs):
    """Как call, но тело — асинхронный генератор чанков (статус проверяется до первого чанка)."""
    try:
        response = await runtime.lifecycle.client_session().request(
            method, url, timeout=ClientTimeout(total=timeout), **kwargs)
    except TimeoutError:
        raise ValueError(f"{vendor}: нет ответа от сервиса — проверьте сеть") from None
    except ClientConnectionError as exc:
        raise ValueError(f"{vendor}: нет соединения ({type(exc).__name__}) — проверьте сеть и адрес") from None
    if response.status != 200:
        try:
            body = await response.read()
        finally:
            response.release()
        raise failure(vendor, response.status, body)
    return response


def pcm_source(response, size=8192):
    return response.content.iter_chunked(size)


async def pcm_pages(chunks, rate):
    """Сырой PCM s16le моно → страницы Ogg Opus (кодек конвейера)."""
    from ...audio_stream import ogg_pages, transcode
    async for page in ogg_pages(transcode(chunks, f"audio/x-pcm;bit=16;rate={rate};channels=1", "opus")):
        yield page


async def pcm_float(chunks):
    """Сырой PCM s16le → float32-массив (синтез целиком для звукового кеша)."""
    import numpy as np
    raw = bytearray()
    async for chunk in chunks:
        raw.extend(chunk)
        if len(raw) > LIMIT:
            raise ValueError("Ответ больше 32 МБ")
    return np.frombuffer(bytes(raw[:len(raw) - len(raw) % 2]), dtype="<i2").astype(np.float32) / 32768.0


async def bytes_iter(data):
    yield data


def strip_wav(data):
    """Сырой PCM из ответа: WAV-заголовок (если есть) отбрасывается."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    position = 12
    while position + 8 <= len(data):
        chunk, length = data[position:position + 4], struct.unpack("<I", data[position + 4:position + 8])[0]
        if chunk == b"data":
            return data[position + 8:position + 8 + length]
        position += 8 + length + (length & 1)
    raise ValueError("WAV без данных")


async def pcm16_from(audio, filename):
    """Звук устройства (WAV PCM16 любой частоты или Ogg Opus) → сырой PCM s16le, 16 кГц, моно."""
    from ...audio_stream import transcode
    source = "audio/opus" if audio[:4] == b"OggS" or str(filename).lower().endswith((".ogg", ".opus")) else "audio/wav"
    out = bytearray()
    async for chunk in transcode(bytes_iter(audio), source, "pcm16"):
        out.extend(chunk)
    if not out:
        raise ValueError("Пустая запись")
    return bytes(out)
