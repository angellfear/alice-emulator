"""Домен sound-cache: звуковой кеш реплик (tts_cache) — список фраз, статусы, перегенерация.

GET  sound-cache             — {engine, entries: [{text, source: reply|scenario|agent|cache, name?, status}], job}
     status: cached (есть файл текущего движка) | missing | pending | running | error
POST sound-cache-regenerate  — пересинтез текущей моделью в фоне (прогресс — job): все фразы
     или {texts: [...]} — только указанные (одна запись)
GET  sound-cache-audio       — ?text= WAV фразы из кеша текущей модели (прослушать)
"""
from aiohttp import web

from . import route


from ...tts_cache import _spoken, scenario_phrases, static_phrases  # noqa: F401 (реэкспорт для тестов)


def _phrases(runtime):
    """Список раздела: статические фразы + уже озвученные фразы, которых нет среди них (не переменные)."""
    result = static_phrases(runtime)
    for text in runtime.tts_cache.texts():
        result.setdefault(text, {"text": text, "source": "cache"})
    return list(result.values())


@route("sound-cache", methods=("GET",))
async def sound_cache(request, runtime, app):
    from ...tts_cache import engine_identity
    runtime.prune_sound_cache()  # фразы, ушедшие из сценариев/ответов, — из кеша вон
    cache, tts = runtime.tts_cache, runtime.get_tts()
    identity = engine_identity(tts) if tts else None
    job = cache.job or {}
    entries = []
    for entry in _phrases(runtime):
        state = job.get("items", {}).get(entry["text"]) if job.get("state") == "running" or \
            job.get("items", {}).get(entry["text"]) == "error" else None
        # без движка «в кеше» — фраза из пакета озвучки (файл любого движка, tts_cache.pack_path)
        covered = cache.cached(identity, entry["text"]) if identity else cache.pack_path(entry["text"]) is not None
        entry["status"] = state or ("cached" if covered else "missing")
        entries.append(entry)
    public_job = {k: v for k, v in job.items() if k != "items"} or None
    return web.json_response({"engine": tts.label() if tts else None, "entries": entries, "job": public_job,
                              "stats": cache.stats()})


@route("sound-cache-regenerate", methods=("POST",))
async def sound_cache_regenerate(request, runtime, app):
    tts = runtime.get_tts()
    if tts is None:
        raise ValueError("Текущий синтез не отдаёт PCM (нужен формат pcm_f32le) — кеш недоступен")
    body = await request.json() if request.can_read_body else {}
    texts = [e["text"] for e in _phrases(runtime)]
    if isinstance(body, dict) and body.get("texts"):
        wanted = {" ".join(str(t).split()) for t in body["texts"]}
        texts = [t for t in texts if t in wanted]
        if not texts:
            raise ValueError("Фраза не найдена в кеше")
    job = runtime.tts_cache.regenerate(tts, texts)
    return web.json_response({"total": job["total"]})


@route("sound-cache-audio", methods=("GET",))
async def sound_cache_audio(request, runtime, app):
    from ...tts_cache import engine_identity
    tts = runtime.get_tts()
    text = request.query.get("text", "")
    path = runtime.tts_cache.path_for(engine_identity(tts), text) if tts else runtime.tts_cache.pack_path(text)
    if path is None or not path.is_file():
        raise ValueError("Фразы нет в кеше текущей модели")
    return web.FileResponse(path, headers={"Content-Type": "audio/wav", "Cache-Control": "no-cache"})
