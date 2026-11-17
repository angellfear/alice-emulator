"""Домен voice-stack: пресеты (адаптер + настройки) и пакеты локальной установки ASR/TTS/LLM.

GET    voice-stack            — {asr|tts|llm: {presets, packages, adapters, active}}
POST   voice-stack-preset     — {service, adapter, title, values, id?} создать/обновить пресет
DELETE voice-stack-preset     — ?service=&id=
POST   voice-stack-apply      — {service, ref: "p:<пресет>"|"k:<пакет>", background?} → остановить ненужные серверы,
                                записать settings[stt|tts|model], запустить пакет; background → {job}, ход — GET ?job=
GET    voice-stack-apply      — ?job= журнал шагов применения {state, summary, steps}
POST   voice-stack-apply-plan — {service, ref} → {stop: [...], start, keep} что остановится и запустится
GET    voice-stack-installed  — установленное локально: строки, размеры (фон, кеш), итоги
POST   voice-stack-clean      — {ids: [...]} | {all_unused: true} удалить неиспользуемое и ставшие ненужными зависимости
POST   voice-stack-package    — {id, values} параметры запуска пакета (до установки тоже)
POST   voice-stack-install    — {id} фоновая установка (прогресс в GET)
POST   voice-stack-uninstall  — {id} удалить установку остановленного пакета (параметры остаются)
POST   voice-stack-restart    — {id} перезапуск сервера пакета; дожидается /health или смерти процесса —
                                умерший сразу сервер даёт ошибку с причиной, а не «restarted»
POST   voice-stack-stop       — {id} остановить сервер пакета
POST   voice-stack-file       — ?id=&key= тело — WAV (проверка формата и длительности), один файл на поле
GET    voice-stack-file       — ?id=&key= прослушать; DELETE — удалить
POST   voice-stack-test       — {ref} проба синтеза источника TTS без кеша → WAV
POST   voice-stack-transcribe — ?id=&key= текст загруженного эталона WAV текущим ASR → {text}
POST   voice-stack-voices     — {service, adapter, values, id?, refresh?} список голосов поставщика (options_from: voices) → {options, error?}
POST   voice-stack-bake       — multipart meta{service, adapter, values, id?, text} + audio (WAV): запечь голос поставщика →
                                {voice_id, title}; JSON {package: <id>} — запечь эталон пакета (перезапуск сервера) → {bake}
POST   voice-stack-unbake     — {service, adapter, values, id?} удалить запечённый голос поставщика; {package} — голос пакета
POST   voice-stack-preset-file — ?service=&id=&key= тело — WAV эталона клонирования пресета; GET — прослушать; DELETE — удалить
POST   voice-stack-login       — {service} запустить вход подписки кодом устройства (отдельный процесс);
                                GET — его состояние и вывод {running, done, exit, output, auth_ready, cooldown}; DELETE — отменить
GET    voice-levels            — живые уровни микрофона для метра вкладки (окно ~100 мс + история):
                                {active, gain, in, out, history}; ?sn= — метр колонки,
                                ответ несёт sn и columns (сводка хаба для селектора UI)
POST   voice-autotune          — {sn, seconds?} автонастройка колонки: слушать микрофон, измерить
                                шум и речь, подобрать предусиление и цель, записать профиль
                                stt.profiles[sn] → {pre_gain_db, level_target_dbfs, speech_dbfs,
                                noise_dbfs, note}
"""
import json
import logging

from aiohttp import web

from ...voice_stack import SERVICES
from . import route

log = logging.getLogger(__name__)


async def _body(request):
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("Ожидался JSON-объект")
    return body


async def _package(request, runtime):
    return await _package_by(request, runtime, "id")


async def _package_by(request, runtime, key):
    body = await _body(request)
    return body, runtime.voice_stack.package(str(body.get(key, "")))


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


@route("voice-stack-apply", methods=("GET", "POST"))
async def voice_stack_apply(request, runtime, app):
    lifecycle = runtime.voice_stack.lifecycle
    if request.method == "GET":
        job = lifecycle.jobs.get(request.query.get("job", ""))
        if job is None:
            raise ValueError("Применение не найдено")
        return web.json_response(job.public())
    body = await _body(request)
    service, ref = body.get("service"), str(body.get("ref", ""))
    if body.get("background"):
        if service not in SERVICES:
            raise ValueError("service: asr|tts|llm")
        job = lifecycle.begin(service, ref)
        lifecycle.start_background(job)
        return web.json_response({"job": job.id})
    return web.json_response(await runtime.voice_stack.apply(service, ref))


@route("voice-stack-apply-plan", methods=("POST",))
async def voice_stack_apply_plan(request, runtime, app):
    body = await _body(request)
    if body.get("service") not in SERVICES:
        raise ValueError("service: asr|tts|llm")
    return web.json_response(await runtime.voice_stack.lifecycle.plan(body["service"], str(body.get("ref", ""))))


@route("voice-stack-installed", methods=("GET",))
async def voice_stack_installed(request, runtime, app):
    return web.json_response(await runtime.voice_stack.installed.items())


@route("voice-stack-clean", methods=("POST",))
async def voice_stack_clean(request, runtime, app):
    body = await _body(request)
    ids = body.get("ids")
    if not body.get("all_unused") and not (isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids)):
        raise ValueError("ids: непустой список или all_unused")
    return web.json_response(await runtime.voice_stack.installed.clean(ids, everything=bool(body.get("all_unused"))))


@route("voice-stack-package", methods=("POST",))
async def voice_stack_package(request, runtime, app):
    body, package = await _package(request, runtime)
    stack = runtime.voice_stack
    if not isinstance(body.get("values"), dict):
        raise ValueError("values: объект")
    before = stack.local.options(package)
    options = stack.local.save_options(package, body["values"])
    bake = package.get("bake") or {}
    for field in package.get("fields", []):  # эталон без текста — текст распознаёт текущий ASR
        target = field.get("transcribe_to")
        if bake and (options.get("voice_mode") != bake.get("mode", "baked") or not bake.get("transcribe", True)):
            break  # эталон не используется в этом режиме голоса или модели текст эталона не нужен
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


@route("voice-stack-voices", methods=("POST",))
async def voice_stack_voices(request, runtime, app):
    body = await _body(request)
    if not isinstance(body.get("values", {}), dict):
        raise ValueError("values: объект")
    return web.json_response(await runtime.voice_stack.voices(
        body.get("service"), str(body.get("adapter", "")), body.get("values") or {}, body.get("id") or None,
        refresh=bool(body.get("refresh"))))


@route("voice-stack-bake", methods=("POST",))
async def voice_stack_bake(request, runtime, app):
    stack = runtime.voice_stack
    if request.content_type == "application/json":  # эталон пакета: сервер запекает при запуске
        body, package = await _package_by(request, runtime, "package")
        spec = package.get("bake")
        if not spec:
            raise ValueError("Пакет не запекает голос")
        if not stack.local.installed(package):
            raise ValueError("Сначала установите пакет")
        options = stack.local.options(package)
        if not options.get(spec["audio"]):
            raise ValueError("Сначала загрузите эталон")
        if spec.get("transcribe", True) and not str(options.get(spec["text"]) or "").strip():
            options = stack.local.save_options(package, {spec["text"]: await _transcribe(
                runtime, stack.local.file_path(package, spec["audio"]))})
        stack.local.dirty.discard(package["id"])
        stack.local.stop(package)
        await stack.local.start(package)
        return web.json_response({"started": package["id"]})
    form = await request.post()
    try:
        meta = json.loads(str(form.get("meta") or "{}"))
    except ValueError:
        raise ValueError("meta: JSON")
    audio = form.get("audio")
    if not isinstance(meta, dict) or not hasattr(audio, "file"):
        raise ValueError("Нужны meta и файл audio")
    result = await stack.bake(meta.get("service"), str(meta.get("adapter", "")), meta.get("values") or {},
                              audio.file.read(), meta.get("text"), meta.get("id") or None)
    return web.json_response(result)


@route("voice-stack-unbake", methods=("POST",))
async def voice_stack_unbake(request, runtime, app):
    stack = runtime.voice_stack
    body = await _body(request)
    if body.get("package"):
        package = stack.package(str(body["package"]))
        spec = package.get("bake")
        if not spec:
            raise ValueError("Пакет не запекает голос")
        stack.local.remove_file(package, spec["audio"])
        return web.json_response({"removed": package["id"]})
    await stack.unbake(body.get("service"), str(body.get("adapter", "")), body.get("values") or {}, body.get("id") or None)
    return web.json_response({"removed": True})


@route("voice-stack-preset-file", methods=("GET", "POST", "DELETE"))
async def voice_stack_preset_file(request, runtime, app):
    stack = runtime.voice_stack
    service, identifier, key = request.query.get("service"), request.query.get("id", ""), request.query.get("key", "")
    path, _ = stack.preset_file_path(service, identifier, key)
    if request.method == "GET":
        if not path.is_file():
            raise ValueError("Файл не загружен")
        return web.FileResponse(path, headers={"Content-Type": "audio/wav", "Cache-Control": "no-cache"})
    if request.method == "DELETE":
        stack.remove_preset_file(service, identifier, key)
        info = {"removed": key}
    else:
        info = stack.upload_preset_file(service, identifier, key, await request.read())
    if stack.active(service) == "p:" + identifier:
        await stack.apply(service, "p:" + identifier)  # применённый пресет — сразу с новым эталоном
    return web.json_response(info)


@route("voice-stack-login", methods=("GET", "POST", "DELETE"))
async def voice_stack_login(request, runtime, app):
    runner = runtime.voice_stack.login
    if request.method == "GET":
        return web.json_response(runner.status())
    if request.method == "DELETE":
        return web.json_response({"cancelled": runner.cancel()})
    body = await _body(request)
    return web.json_response(runner.start(str(body.get("service", ""))))


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


RESTART_READY_TIMEOUT = 15.0  # сколько ждать /health после перезапуска: дольше — «грузится модель», карточка показывает состояние


@route("voice-stack-restart", methods=("POST",))
async def voice_stack_restart(request, runtime, app):
    _, package = await _package(request, runtime)
    local = runtime.voice_stack.local
    local.stop(package)
    await local.start(package)
    try:  # процесс, умерший сразу после запуска, не должен выглядеть как успешный перезапуск
        await local.wait_ready(package, RESTART_READY_TIMEOUT)
    except ValueError as exc:
        raise ValueError(f"Не запустился «{package.get('title', package['id'])}»: {exc}") from exc
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

    from ...voice_stack import adapters, call_adapter
    body = await _body(request)
    config, _ = runtime.voice_stack.config("tts", str(body.get("ref", "")))
    adapter = adapters.resolve("tts", config)
    if not hasattr(adapter, "synth_pcm"):
        raise ValueError("Адаптер синтеза не отдаёт PCM — проба недоступна")
    pcm, rate = await call_adapter(adapter.synth_pcm, runtime, config, TEST_PHRASE)
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


@route("voice-levels", methods=("GET",))
async def voice_levels(request, runtime, app):
    """Живые уровни микрофона для метра вкладки «Голосовой стек»: текущее окно
    (~100 мс) и история — «Вход» (микрофон как есть) и «В ASR» (после выравнивания).
    ?sn=<колонка> — метр её микрофона (audio_meter.HUB); без sn — колонка со свежим
    звуком, иначе последняя говорившая, иначе общий. Ответ несёт sn выбранной колонки
    и columns — сводку хаба (селектор и слежение UI). Опрос UI ~10 Гц, пока вкладка
    видима; чтение состояние измерителя не меняет."""
    from ...audio_meter import HUB, LEVELS
    sn = request.query.get("sn") or HUB.active() or ""
    payload = (HUB.meter(sn) if sn else LEVELS).snapshot()
    payload["sn"] = sn
    payload["columns"] = HUB.columns()
    return web.json_response(payload)


AUTOTUNE_SECONDS = 8.0   # сколько слушать микрофон по умолчанию (вилка 2–12)
SPEECH_FLOOR_DB = 10.0   # окна громче шума на столько дБ считаются речью
GAIN_HEADROOM_DB = 5.0   # запас усиления до потолка AutoGain (~+50 дБ)


@route("voice-autotune", methods=("POST",))
async def voice_autotune(request, runtime, app):
    """Автонастройка колонки: слушает её микрофон seconds секунд (2–12), измеряет шум
    и пики речи, подбирает предусиление и цель нормализации и записывает профиль
    колонки (stt.profiles[sn], выравнивание включено). Звук не дошёл до распознавания
    или речь не услышана — ValueError с подсказкой; шумное окружение — предупреждение
    в note, настройка всё равно применяется."""
    import asyncio

    from ...audio_meter import HUB
    body = await _body(request)
    sn = str(body.get("sn") or "").strip()
    if not sn:
        raise ValueError("Выберите колонку для настройки")
    try:
        seconds = float(body.get("seconds") or AUTOTUNE_SECONDS)
    except (TypeError, ValueError):
        seconds = AUTOTUNE_SECONDS
    seconds = min(12.0, max(2.0, seconds))
    meter = HUB.meter(sn)
    meter.seal()  # хвост чужого измерения (метр живёт с первого звука колонки) в запись не попадёт
    meter.start_record()
    done = False
    try:
        await asyncio.sleep(seconds)
        entries = meter.stop_record()
        done = True
        levels = sorted(entry["i"] for entry in entries if entry["i"] is not None)
        if len(levels) < 5:
            raise ValueError("Звук колонки не дошёл до сервера — скажите что-нибудь в колонку во время настройки")
        noise = levels[max(0, int(len(levels) * 0.1) - 1)]  # самые тихие окна — шум микрофона и комнаты
        speech = [entry for entry in entries if entry["i"] is not None and entry["i"] >= noise + SPEECH_FLOOR_DB]
        peaks = [entry["ip"] for entry in speech if entry["ip"] is not None]
        if len(peaks) < 8:
            raise ValueError("Речь не услышана — повторите, говоря в колонку обычным голосом")
        peak = max(peaks)
        typical = sorted(entry["i"] for entry in speech)[int(len(speech) * 0.75)]
        target = -6.0
        # Выравнивание тянет пик речи к цели, но потолок усиления AutoGain около +50 дБ:
        # недостающие до цели−45 дБ добавляет предусиление (до +24); при исключительно
        # тихом микрофоне цель опускается до достижимой.
        needed = target - peak
        pre = min(24.0, max(0.0, needed - (50.0 - GAIN_HEADROOM_DB)))
        if needed > 70.0:
            target = max(-18.0, peak + 70.0)
        stt = {**(runtime.settings.get("stt") or {})}
        profiles = {**(stt.get("profiles") or {})}
        profiles[sn] = {"level": "yes", "pre_gain_db": round(pre, 1),
                        "level_target_dbfs": round(target, 1)}
        stt["profiles"] = profiles
        await runtime.save_settings({**runtime.settings, "stt": stt})
        return web.json_response({
            "sn": sn, "pre_gain_db": profiles[sn]["pre_gain_db"],
            "level_target_dbfs": profiles[sn]["level_target_dbfs"],
            "speech_dbfs": round(typical, 1), "noise_dbfs": round(noise, 1),
            "note": "" if typical - noise >= SPEECH_FLOOR_DB + 12 else
                    "Много шума относительно речи — говорите ближе к колонке"})
    finally:
        if not done:
            meter.stop_record()  # клиент оборвался — запись измерения всё равно сбросить
