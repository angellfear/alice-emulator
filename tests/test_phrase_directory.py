"""Каркас «от плагинов»: объявления contributes.providers в реестре поставщиков,
справочник фраз (записи с handler core:<сценарий>), ранжирование кандидатов по
точности (закрытый список → литеральные куски текста) и отчёт кандидатов в dry-run.

Встроенные шаблонные сценарии участвуют в том же ранжировании: «поставь будильник
на 7» (закрытые списки времени) обязано выигрывать у записи-поиска «(поставь|включи)
{query}» — свободный хвост ниже закрытых списков и литеральных слов.
"""
import json

from aiohttp import ClientSession

from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.extensions.manager import validate_contributes
from server_emulator.runtime import Runtime
from server_emulator.sdk import SDK_VERSION
from server_emulator.server import create_app
from tests.conftest import listen
from tests.test_music_provider import make_music_extension

DECL = {"kind": "music", "title": "Музыка", "module": "music/fake.py",
        "capabilities": ["podcasts"]}

PROVIDER_MODULE = (
    "PROVIDER = {'id': 'fake-declared', 'title': 'Объявленный'}\n"
    "\n\n"
    "async def search(ctx, query, kind):\n    return {'tracks': []}\n"
    "\n\n"
    "async def neighbor(ctx, track_id, context, step):\n    return {'tracks': []}\n"
)


def _declaration_extension(runtime, ext_id="fake-declared", provider=PROVIDER_MODULE,
                           manifest_extra=None, phrases=None):
    """Расширение с объявлением contributes.providers (модуль называет точку подключения
    сам) и, опционально, файлом справочника фраз."""
    files = {"music/fake.py": provider}
    manifest = {"id": ext_id, "name": ext_id, "version": "1.0.0", "description": "тест",
                "author": "T", "license": "MIT",
                "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": f">={SDK_VERSION}"},
                "permissions": {},
                "contributes": {"providers": [dict(DECL, **(manifest_extra or {}))]}}
    if phrases:
        (manifest["contributes"]["providers"][0])["phrases"] = "phrases/music.json"
        files["phrases/music.json"] = json.dumps(phrases, ensure_ascii=False)
    return make_music_extension(runtime, ext_id=ext_id,
                                files={**files,
                                       **{"extension.json": json.dumps(manifest, ensure_ascii=False)}})


def test_declaration_validation():
    """Манифест: объявление — {kind, title, module, capabilities?, phrases?}; фразы —
    путь или список путей."""
    validate_contributes({"providers": [dict(DECL)]})
    validate_contributes({"providers": [dict(DECL, phrases="phrases/music.json")]})
    validate_contributes({"providers": [dict(DECL, phrases=["a.json", "b.json"])]})
    for bad in ({"providers": []},
                {"providers": [{"kind": "music", "module": "m.py"}]},  # нет title
                {"providers": [dict(DECL, capabilities="podcasts")]},
                {"providers": [dict(DECL, phrases=42)]}):
        try:
            validate_contributes(bad)
        except ValueError:
            continue
        raise AssertionError(f"валидация пропустила: {bad}")


async def test_declaration_registry_next_to_folders(tmp_path):
    """Объявление собирает поставщика в реестр (origin extension); плагин без
    объявления собирается по папке вида как раньше; встроенные — по своим объявлениям."""
    runtime = Runtime(tmp_path)
    _declaration_extension(runtime, ext_id="fake-declared")
    make_music_extension(runtime, ext_id="fake-folder",
                         files={"music/fake.py": PROVIDER_MODULE.replace("'fake-declared'", "'fake-folder'")})
    runtime.extensions.load_enabled()
    from server_emulator import providers
    try:
        music = providers.PROVIDERS["music"]
        assert "fake-declared" in music and "fake-folder" in music
        assert music["fake-declared"]["origin"] == "extension"
        assert music["fake-declared"]["capabilities"] == ["podcasts"]
        assert "builtin.spotify" in music and "builtin.yandex-music" in music
        assert music["builtin.spotify"]["origin"] == "builtin"  # реестр из объявлений встроенных
        assert any(path.name == "music.json" for _e, path, _k in runtime.extensions.phrase_files())
    finally:
        await runtime.close()


async def test_new_kind_declared_by_plugin(tmp_path):
    """Вид, объявленный плагином впервые: реестр и вкладка строятся без правки ядра,
    название вида — из объявления; обязательные функции проверит его потребитель."""
    runtime = Runtime(tmp_path)
    _declaration_extension(runtime, ext_id="fake-tv",
                           manifest_extra={"kind": "tv", "title": "ТВ"},
                           provider="PROVIDER = {'id': 'fake-tv', 'title': 'ТВ-плагин'}\n")
    runtime.extensions.load_enabled()
    runner, url = await listen(create_app(runtime=runtime))
    try:
        from server_emulator import providers
        assert providers.PROVIDERS["tv"]["fake-tv"]["title"] == "ТВ-плагин"
        assert providers.kind_title("tv") == "ТВ"
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/providers") as r:
                assert r.status == 200
                data = await r.json()
        assert data["kinds"]["tv"]["title"] == "ТВ"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_phrase_directory_entry_routes_to_core_handler(tmp_path):
    """Запись справочника (handler core:media.music) — кандидат в сценарий-обработчик:
    реплика, у которой нет своего шаблона, уходит в music с слотами записи; в dry-run
    видны кандидаты с причиной dir:<запись>."""
    runtime = Runtime(tmp_path)
    _declaration_extension(runtime, ext_id="fake-declared", phrases={
        "entries": [{"id": "radio", "sentences": ["включи радиостанцию {query}"],
                     "handler": "core:media.music"}]})
    runtime.extensions.load_enabled()
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/emulator/api/scenarios", headers={"X-Emulator": "1"},
                                   json={"text": "включи радиостанцию джаз"}) as r:
                assert r.status == 200
                report = await r.json()
        assert report["handled"] and report["scenario"] == "media.music", report
        assert report["slots"].get("query") == "джаз"
        reasons = [reason for sid, reason in report["candidates"] if sid == "media.music"]
        assert reasons and reasons[0].startswith("dir:fake-declared.radio"), report["candidates"]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_builtin_phrases_rank_above_search_entries(tmp_path):
    """Встроенные фразы в том же ранжировании: «поставь будильник на 7» (закрытые
    списки времени, больше литеральных слов) сильнее записи-поиска «(поставь|включи)
    {query}» — поиск не забирает фразу будильника; «включи музыку» — запись с закрытым
    списком {media_object} сильнее хвоста."""
    runtime = Runtime(tmp_path)
    _declaration_extension(runtime, ext_id="fake-declared", phrases={
        "entries": [{"id": "search", "sentences": ["(поставь|включи) {query}"],
                     "handler": "core:media.music", "search": True}]})
    runtime.extensions.load_enabled()
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            for text, winner in (("поставь будильник на 7", "timers.alarm.set"),
                                 ("включи музыку", "media.music")):
                async with client.post(url + "/emulator/api/scenarios",
                                       headers={"X-Emulator": "1"},
                                       json={"text": text}) as r:
                    assert r.status == 200
                    report = await r.json()
                assert report["handled"] and report["scenario"] == winner, (text, report)
                ids = [sid for sid, _reason in report["candidates"]]
                assert ids[0] == winner, (text, report["candidates"])
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_broken_phrase_file_warns_without_dropping_others(tmp_path):
    """Битый файл справочника — warning, остальные файлы работают; дубль записи —
    пропущен."""
    runtime = Runtime(tmp_path)
    manifest = {"id": "fake-declared", "name": "fake-declared", "version": "1.0.0",
                "description": "тест", "author": "T", "license": "MIT",
                "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": f">={SDK_VERSION}"},
                "permissions": {},
                "contributes": {"providers": [dict(DECL, phrases=["phrases/a.json", "phrases/b.json"])]}}
    make_music_extension(runtime, ext_id="fake-declared", files={
        "extension.json": json.dumps(manifest, ensure_ascii=False),
        "music/fake.py": PROVIDER_MODULE,
        "phrases/a.json": "{битый json",
        "phrases/b.json": json.dumps({"entries": [
            {"id": "one", "sentences": ["включи аудиокнигу {query}"], "handler": "core:media.music"}]},
            ensure_ascii=False)})
    runtime.extensions.load_enabled()
    try:
        from server_emulator import providers
        assert "fake-declared" in providers.PROVIDERS["music"]  # модуль собран
        directory = runtime.dialog.phrase_directory()
        assert directory.warnings and "a.json" in directory.warnings[0]
        assert "fake-declared.one" in directory.entries  # файл-дубль и битый — мимо, свои на месте
    finally:
        await runtime.close()
