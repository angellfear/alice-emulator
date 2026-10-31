"""Домен voice-stack: пресеты (адаптер + настройки) и пакеты локальной установки ASR/TTS/LLM.

GET    voice-stack            — {asr|tts|llm: {presets, packages, adapters, active}}
POST   voice-stack-preset     — {service, adapter, title, values, id?} создать/обновить пресет
DELETE voice-stack-preset     — ?service=&id=
POST   voice-stack-apply      — {service, ref: "p:<пресет>"|"k:<пакет>"} → settings[stt|tts|model]
POST   voice-stack-package    — {id, values} параметры запуска пакета (до установки тоже)
POST   voice-stack-install    — {id} фоновая установка (прогресс в GET)
POST   voice-stack-uninstall  — {id} удалить установку остановленного пакета (параметры остаются)
POST   voice-stack-restart    — {id} перезапуск сервера пакета
POST   voice-stack-stop       — {id} остановить сервер пакета
POST   voice-stack-file       — ?id=&key= тело — WAV (проверка формата и длительности), один файл на поле
GET    voice-stack-file       — ?id=&key= прослушать; DELETE — удалить
POST   voice-stack-test       — {ref} проба синтеза источника TTS без кеша → WAV
POST   voice-stack-transcribe — ?id=&key= текст загруженного эталона WAV текущим ASR → {text}
"""
import logging

from aiohttp import web

from . import route

log = logging.getLogger(__name__)


async def _body(request):
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("Ожидался JSON-объект")
    return body


async def _package(request, runtime):
    body = await _body(request)
    return body, runtime.voice_stack.package(str(body.get("id", "")))


@route("voice-stack", methods=("GET",))
async def voice_stack(request, runtime, app):
    return web.json_response(await runtime.voice_stack.public())


@route("voice-stack-preset", methods=("POST", "DELETE"))
async def voice_stack_preset(request, runtime, app):
    stack = runtime.voice_stack
    if request.method == "DELETE":
        stack.delete_preset(request.query.get("service"), request.query.get("id", ""))
        return web.json_response({"removed": request.query.get("id")})
    body = await _body(request)
    service, identifier = body.get("service"), body.get("id") or None
    if not isinstance(body.get("values", {}), dict):
        raise ValueError("values: объект")
    item = stack.save_preset(service, str(body.get("adapter", "")), body.get("title"), body.get("values") or {}, identifier)
    if identifier and stack.active(service) == "p:" + identifier:
        await stack.apply(service, "p:" + identifier)  # правка применённого пресета сразу в работе
    return web.json_response({"id": item["id"]})


@route("voice-stack-apply", methods=("POST",))
async def voice_stack_apply(request, runtime, app):
    body = await _body(request)
    return web.json_response(await runtime.voice_stack.apply(body.get("service"), str(body.get("ref", ""))))


@route("voice-stack-package", methods=("POST",))
async def voice_stack_package(request, runtime, app):
    body, package = await _package(request, runtime)
    stack = runtime.voice_stack
    if not isinstance(body.get("values"), dict):
        raise ValueError("values: объект")
    before = stack.local.options(package)
    options = stack.local.save_options(package, body["values"])
    for field in package.get("fields", []):  # эталон без текста — текст распознаёт текущий ASR
        target = field.get("transcribe_to")
        if target and options.get(field["key"]) and not str(options.get(target) or "").strip():
            try:
                text = await _transcribe(runtime, stack.local.file_path(package, field["key"]))
            except Exception as exc:  # noqa: BLE001 — ASR недоступен: текст распознает сам сервер пакета
                log.warning("Текст эталона %s не распознан: %s", package["id"], exc)
            else:
                options = stack.local.save_options(package, {target: text})
    ref = "k:" + package["id"]
    changed = options != before or package["id"] in stack.local.dirty
    stack.local.dirty.discard(package["id"])
    if changed and not stack.local.outdated(package):
        if stack.active(package["service"]) == ref:
            stack.local.stop(package)  # применённый сервер: перезапуск с новыми параметрами
            await stack.apply(package["service"], ref)
        elif await stack.local.healthy(package):
            stack.local.stop(package)  # запущенный вручную — тоже с новыми параметрами (эталон запечётся)
            await stack.local.start(package)
    return web.json_response({"values": options})


@route("voice-stack-install", methods=("POST",))
async def voice_stack_install(request, runtime, app):
    _, package = await _package(request, runtime)
    runtime.voice_stack.local.install(package)
    return web.json_response({"started": package["id"]})


@route("voice-stack-uninstall", methods=("POST",))
async def voice_stack_uninstall(request, runtime, app):
    _, package = await _package(request, runtime)
    local = runtime.voice_stack.local
    process = local.processes.get(package["id"])
    if await local.healthy(package) or (process is not None and process.poll() is None):
        raise ValueError("Сервер пакета запущен — сначала остановите его")
    local.uninstall(package)
    return web.json_response({"removed": package["id"]})


@route("voice-stack-stop", methods=("POST",))
async def voice_stack_stop(request, runtime, app):
    _, package = await _package(request, runtime)
    runtime.voice_stack.local.stop(package)
    return web.json_response({"stopped": package["id"]})


@route("voice-stack-restart", methods=("POST",))
async def voice_stack_restart(request, runtime, app):
    _, package = await _package(request, runtime)
    runtime.voice_stack.local.stop(package)
    await runtime.voice_stack.local.start(package)
    return web.json_response({"restarted": package["id"]})


@route("voice-stack-file", methods=("GET", "POST", "DELETE"))
async def voice_stack_file(request, runtime, app):
    stack = runtime.voice_stack
    package, key = stack.package(request.query.get("id", "")), request.query.get("key", "")
    if request.method == "GET":
        path = stack.local.file_path(package, key)
        if not path.is_file():
            raise ValueError("Файл не загружен")
        return web.FileResponse(path, headers={"Content-Type": "audio/wav", "Cache-Control": "no-cache"})
    if request.method == "DELETE":
        stack.local.remove_file(package, key)
        return web.json_response({"removed": key})
    # сервер подхватит новый эталон после «Сохранить настройки» (там же запекание голоса)
    return web.json_response(stack.local.upload(package, key, await request.read()))


TEST_PHRASE = "Привет, это проверка голоса"


@route("voice-stack-test", methods=("POST",))
async def voice_stack_test(request, runtime, app):
    import io
    import wave

    import numpy as np

    from ...voice_stack import adapters
    body = await _body(request)
    config, _ = runtime.voice_stack.config("tts", str(body.get("ref", "")))
    adapter = adapters.resolve("tts", config)
    if not hasattr(adapter, "synth_pcm"):
        raise ValueError("Адаптер синтеза не отдаёт PCM — проба недоступна")
    pcm, rate = await adapter.synth_pcm(runtime, config, TEST_PHRASE)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    return web.Response(body=buffer.getvalue(), headers={"Content-Type": "audio/wav", "Cache-Control": "no-cache"})


@route("voice-stack-transcribe", methods=("POST",))
async def voice_stack_transcribe(request, runtime, app):
    """Текст эталона для клонирования голоса — распознаётся текущим источником ASR."""
    stack = runtime.voice_stack
    path = stack.local.file_path(stack.package(request.query.get("id", "")), request.query.get("key", ""))
    return web.json_response({"text": await _transcribe(runtime, path)})


async def _transcribe(runtime, path):
    import json

    if not path.is_file():
        raise ValueError("Сначала загрузите WAV")
    data = path.read_bytes()
    if runtime.asr_streaming():
        async def chunks():
            yield data
        result = await runtime.recognize_stream(chunks(), "audio/wav")
    else:
        raw, _ = await runtime.provider("stt", audio=data, filename=path.name)
        result = json.loads(raw)
    text = " ".join(str(result.get("text") or "").split())
    if not text:
        raise ValueError("Распознавание вернуло пустой текст")
    return text
