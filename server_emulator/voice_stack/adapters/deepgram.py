"""Deepgram: распознавание речи Nova-3 (русский) — потоком по WebSocket и целой записью по REST.

  потоком — wss://api.deepgram.com/v1/listen?model=nova-3&language=ru&encoding=linear16&sample_rate=16000
            &interim_results=true&smart_format=true&endpointing=300; заголовок «Authorization: Token <ключ>»,
            звук — бинарными кадрами PCM 16 бит, конец — {"type":"CloseStream"}; ответы {"type":"Results",
            "channel":{"alternatives":[{"transcript"}]}, "is_final", "speech_final"}:
            https://developers.deepgram.com/docs/live-streaming-audio
            https://developers.deepgram.com/docs/understand-endpointing-interim-results
            https://developers.deepgram.com/docs/close-stream
  целиком — POST https://api.deepgram.com/v1/listen, тело — файл, ответ results.channels[0].alternatives[0].transcript:
            https://developers.deepgram.com/docs/pre-recorded-audio
  языки   — Nova-3 понимает русский (ru): https://developers.deepgram.com/docs/models-languages-overview

Ключ уходит только заголовком Authorization и в журналы и тексты ошибок не попадает. Новым аккаунтам Deepgram
выдаёт стартовый кредит (сумма меняется — см. console.deepgram.com), дальше оплата по минутам: https://deepgram.com/pricing
"""
import asyncio
import json

from aiohttp import ClientError, WSMsgType, WSServerHandshakeError

from . import _vendor as vendor

TITLE = "Deepgram (Online Free)"
DESCRIPTION = "Распознавание речи Deepgram Nova-3 с частичными результатами: стартовый кредит бесплатно, дальше платно."
NAME = "Deepgram"
URL = "https://api.deepgram.com/v1/listen"
YES_NO = [{"value": "yes", "label": "Включено"}, {"value": "no", "label": "Выключено"}]
SERVICES = {
    "asr": {
        "title": "Deepgram — распознавание (Online Free)",
        "description": "Deepgram Nova-3: потоковое распознавание с частичными результатами. Стартовый кредит бесплатно, дальше платно.",
        "fields": [{"key": "api_key", "label": "Ключ API", "type": "password", "required": True,
                    "link": "https://console.deepgram.com/signup",
                    "help": "console.deepgram.com → API Keys → Create a New API Key"},
                   {"key": "model", "label": "Модель", "type": "select", "options": [
                       {"value": "nova-3", "label": "Nova-3 (новая, точная)"},
                       {"value": "nova-2", "label": "Nova-2"}]},
                   {"key": "language", "label": "Язык", "type": "text", "help": "Код языка: ru, en, uk…"},
                   {"key": "smart_format", "label": "Умное форматирование", "type": "select", "group": "advanced",
                    "options": YES_NO, "help": "Заглавные буквы, числа, даты"},
                   {"key": "punctuate", "label": "Пунктуация", "type": "select", "group": "advanced", "options": YES_NO},
                   {"key": "endpointing", "label": "Пауза конца фразы, мс", "type": "number", "step": 50, "group": "advanced",
                    "help": "Через сколько миллисекунд тишины фраза считается законченной"},
                   {"key": "url", "label": "Адрес сервиса", "type": "text", "group": "advanced",
                    "help": "Пусто — https://api.deepgram.com/v1/listen"}],
        "defaults": {"model": "nova-3", "language": "ru", "smart_format": "yes", "punctuate": "yes", "endpointing": 300},
        "examples": {"Deepgram Nova-3 (Online Free)": {"model": "nova-3", "language": "ru"}},
    },
}
MATCH = {"asr": r"api\.deepgram\.com"}


def _address(config):
    return config.get("url") or URL


def _flag(config, key):
    return "false" if config.get(key) == "no" else "true"


def _params(config):
    params = {"model": config.get("model") or "nova-3", "language": config.get("language") or "ru",
              "smart_format": _flag(config, "smart_format"), "punctuate": _flag(config, "punctuate")}
    return params


async def transcribe(runtime, config, audio, filename):
    key = vendor.api_key(config, NAME)
    result = await vendor.call_json(
        runtime, NAME, "POST", _address(config), key, params=_params(config), data=audio,
        headers={"Authorization": "Token " + key, "Content-Type": "audio/ogg" if audio[:4] == b"OggS" else "audio/wav"})
    try:
        return str(result["results"]["channels"][0]["alternatives"][0].get("transcript") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ValueError(f"{NAME}: в ответе нет текста") from None


def _text(message):
    try:
        return str(message["channel"]["alternatives"][0].get("transcript") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


async def recognize_stream(runtime, config, chunks, fmt, on_partial=None):
    """Живой звук устройства → Deepgram по WebSocket; частичные тексты — в on_partial, итог — {"text"}."""
    from ...audio_stream import transcode
    key = vendor.api_key(config, NAME)
    params = {**_params(config), "encoding": "linear16", "sample_rate": "16000", "channels": "1",
              "interim_results": "true"}
    if config.get("endpointing") not in (None, ""):
        params["endpointing"] = str(int(config["endpointing"]))
    session = runtime.lifecycle.client_session()
    finals, last = [], ""
    try:
        async with asyncio.timeout(120):
            async with session.ws_connect(_address(config), params=params, headers={"Authorization": "Token " + key},
                                          max_msg_size=0, heartbeat=20) as ws:
                async def upload():
                    try:
                        async for chunk in transcode(chunks, fmt, "pcm16"):
                            await ws.send_bytes(chunk)
                        await ws.send_str(json.dumps({"type": "CloseStream"}))
                    except BaseException:
                        await ws.close()
                        raise

                sender = asyncio.create_task(upload())
                try:
                    async for packet in ws:
                        if packet.type != WSMsgType.TEXT:
                            continue
                        message = json.loads(packet.data)
                        if message.get("type") == "Error" or message.get("err_code"):
                            raise ValueError(f"{NAME}: {vendor.clean(message.get('description') or message.get('err_msg') or 'ошибка службы', key)}")
                        if message.get("type") != "Results":
                            continue
                        text = _text(message)
                        if message.get("is_final"):
                            if text:
                                finals.append(text)
                            shown = " ".join(finals)
                        else:
                            shown = " ".join(finals + ([text] if text else []))
                        if shown and shown != last and on_partial:
                            await on_partial(shown)
                        last = shown or last
                        if message.get("speech_final") and finals:
                            return {"text": " ".join(finals)}  # пауза — фраза закончена, не дожидаясь закрытия микрофона
                    if sender.done() and not sender.cancelled() and sender.exception():
                        raise sender.exception()  # сбой чтения/кодирования звука, а не закрытие службой
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
    except WSServerHandshakeError as exc:
        raise vendor.failure(NAME, exc.status) from None
    except asyncio.TimeoutError:
        raise ValueError(f"{NAME}: поставщик не ответил вовремя") from None
    except ClientError as exc:
        raise ValueError(f"{NAME}: нет соединения ({type(exc).__name__})") from None
    return {"text": " ".join(finals)}
