"""Каркас «от плагинов»: объявления contributes.providers в реестре поставщиков,
справочник фраз (записи с handler core:<сценарий>), ранжирование кандидатов по
точности (закрытый список → литеральные куски текста), отчёт кандидатов в dry-run
и ступень свободных хвостов (записи search:true — опрос каталогов плагинов).

Встроенные шаблонные сценарии участвуют в том же ранжировании: «поставь будильник
на 7» (закрытые списки времени) обязано выигрывать у записи-поиска «(поставь|включи)
{query}» — свободный хвост ниже закрытых списков и литеральных слов.
"""
import datetime
import json
import logging
import re

from aiohttp import ClientSession

from server_emulator import music
from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.extensions.manager import validate_contributes
from server_emulator.runtime import Runtime
from server_emulator.sdk import SDK_VERSION
from server_emulator.server import create_app
from tests.conftest import listen
from tests.test_music_provider import (_disable_builtin_providers, _provider_module,
                                       _yandex_token_provider_module, MUSIC_PHRASES,
                                       TRACKS, make_music_extension)

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
        assert "fake-declared" in music  # объявление собирает поставщика
        assert music["fake-declared"]["origin"] == "extension"
        assert music["fake-declared"]["capabilities"] == ["podcasts"]
        # папка вида без объявления больше не собирается — карточке добавлена подсказка
        assert "fake-folder" not in music
        card = next(c for c in runtime.extensions.listing()["extensions"] if c["id"] == "fake-folder")
        assert "contributes.providers" in (card.get("provider_hint") or "")
        assert "builtin.spotify" in music and "builtin.yandex-music" in music
        assert music["builtin.spotify"]["origin"] == "builtin"  # реестр из объявлений встроенных
        assert any(path.name == "music.json" for _e, path, _k, _l in runtime.extensions.phrase_files())
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


# --- ступень свободных хвостов: опрос каталогов плагинов ------------------------

def _probe_ctx(runtime, text):
    """Контекст реплики стенда «Проверка»: dry-run без устройства, время фиксировано."""
    ctx = {"input": {"message_id": "probe", "request_id": "probe", "dialog_id": "probe",
                     "sn": "", "text": text},
           "clock": {"now": datetime.datetime(2026, 11, 8, 12, 0)}, "dry_run": True}
    ctx.update(runtime._scenario_context(sn=""))
    return ctx


async def _probe(runtime, text):
    return await runtime.dialog.handle_text(text, _probe_ctx(runtime, text), "probe",
                                            None, deliver=False)


async def test_bare_phrase_asks_catalogs_and_winner_answers(tmp_path):
    """Голое «включи {X}» без закрытых списков: записи справочника search:true —
    ступень опроса каталогов; нашедший поставщик отдаёт треки, отвечает сценарий-
    исполнитель его handler'а (handler core:media.music), запрос — хвост реплики."""
    runtime = Runtime(tmp_path)
    _disable_builtin_providers(runtime)
    marker = tmp_path / "calls.json"
    make_music_extension(runtime, ext_id="fake-music", provider_id="fake",
                         files={"music/fake.py": _provider_module(marker)},
                         phrases=MUSIC_PHRASES)
    runtime.extensions.load_enabled()
    try:
        report = await _probe(runtime, "включи эминем")
        assert report["handled"] and report["scenario"] == "media.music", report
        assert str(report["reason"]).startswith("search:fake-music."), report["reason"]
        assert report["speech"] == f"Включаю {TRACKS[0]['title']}.", report["speech"]
        assert json.loads(marker.read_text(encoding="utf-8")) == [["search", "эминем", "music"]]
    finally:
        await runtime.close()


async def test_bare_phrase_without_catalog_hits_reports_candidates(tmp_path):
    """Каталоги опрошены, но никто не нашёл: честный no_match с запросом (режим
    «сценарии» отвечает «Не нашла «X».») и кандидаты dry-run — чьи записи спрашивали,
    чтобы промах был виден в «Без ответа»."""
    runtime = Runtime(tmp_path)
    _disable_builtin_providers(runtime)
    marker = tmp_path / "calls.json"
    silent = _provider_module(marker).replace('{"tracks": TRACKS', '{"tracks": []')
    make_music_extension(runtime, ext_id="fake-music", provider_id="fake",
                         files={"music/fake.py": silent}, phrases=MUSIC_PHRASES)
    runtime.extensions.load_enabled()
    try:
        report = await _probe(runtime, "включи эминем")
        assert not report["handled"] and report["reason"] == "no_match", report
        assert report["not_found_query"] == "эминем", report
        assert report["candidates"] == [("fake-music.search", "search")], report["candidates"]
    finally:
        await runtime.close()


def _token_probe_provider_module(marker):
    """Поставщик музыки без признака сервиса: в маркер — токен вызова (yandex_oauth),
    треков нет — победить он не может."""
    path = str(marker).replace("\\", "/")
    return f'''import json
from pathlib import Path
MARKER = Path("{path}")

def _note(event):
    calls = json.loads(MARKER.read_text(encoding="utf-8")) if MARKER.exists() else []
    calls.append(event)
    MARKER.write_text(json.dumps(calls, ensure_ascii=False), encoding="utf-8")

async def search(ctx, query, kind):
    ext = getattr(ctx, "extension", None)
    _note(["search", str(ext.get("yandex_oauth") or "") if ext is not None else ""])
    return {{"tracks": []}}

async def neighbor(ctx, track_id, context, step):
    return None

PROVIDER = {{"id": "probe", "title": "Проба токена"}}
'''


async def test_search_stage_calls_provider_with_music_call_context(tmp_path):
    """Токен аккаунта доходит до поиска ступени свободных хвостов («включи {X}» без
    слова «музыку»): поставщик с service=yandex-music получает yandex_oauth в
    ExtensionContext вызова (music.call_context), поставщик без признака сервиса —
    обычный контекст без токена."""
    runtime = Runtime(tmp_path)
    _disable_builtin_providers(runtime)
    ya_marker = tmp_path / "ya.json"
    other_marker = tmp_path / "other.json"
    make_music_extension(runtime, ext_id="yandex-ext",
                         files={"music/yandex.py": _yandex_token_provider_module(ya_marker)},
                         phrases=MUSIC_PHRASES, module="music/yandex.py")
    make_music_extension(runtime, ext_id="other-ext",
                         files={"music/other.py": _token_probe_provider_module(other_marker)},
                         phrases=MUSIC_PHRASES, module="music/other.py")
    runtime.extensions.load_enabled()
    try:
        assert music.note_token(runtime, "y0_" + "c" * 30)
        report = await _probe(runtime, "включи эминем")
        assert report["handled"] and report["scenario"] == "media.music", report
        assert report["speech"] == f"Включаю {TRACKS[0]['title']}.", report["speech"]
        ya_calls = json.loads(ya_marker.read_text(encoding="utf-8"))
        assert [c for c in ya_calls if c[0] == "search"] == [[
            "search", music.captured_oauth()]], ya_calls
        other_calls = json.loads(other_marker.read_text(encoding="utf-8"))
        assert [c for c in other_calls if c[0] == "search"] == [["search", ""]], other_calls
    finally:
        await runtime.close()


async def test_search_stage_logs_success_with_elapsed_and_best(tmp_path, caplog):
    """Успешный поиск ступени виден в журнале: info-запись с запросом, поставщиком,
    временем, числом треков и типом best из контекста поиска."""
    runtime = Runtime(tmp_path)
    _disable_builtin_providers(runtime)
    marker = tmp_path / "calls.json"
    provider = _provider_module(marker).replace(
        '"context": {"type": "search", "id": "q"}',
        '"context": {"type": "search", "best": "artist"}')
    make_music_extension(runtime, ext_id="fake-music", provider_id="fake",
                         files={"music/fake.py": provider}, phrases=MUSIC_PHRASES)
    runtime.extensions.load_enabled()
    try:
        with caplog.at_level(logging.INFO, logger="server_emulator.scenario_engine.dialog"):
            report = await _probe(runtime, "включи эминем")
        assert report["handled"], report
        records = [record for record in caplog.records if "нашёл за" in record.getMessage()]
        assert len(records) == 1, records
        message = records[0].getMessage()
        assert "«эминем»" in message and "fake-music" in message, message
        assert "7 треков" in message and "best artist" in message, message
        assert re.search(r"за \d+\.\d+ с", message), message
    finally:
        await runtime.close()


def _parallel_probe_module(marker, role):
    """Поставщик для проверки параллельных опросов ступени (service=yandex-music —
    ядро кладёт в ctx.extension свой ExtensionContext и восстанавливает по окончании).
    «fast» уступает управление циклу и отвечает пусто; «slow» читает токен до и после
    уступки: пока slow висит, fast успевает завершиться (очередь цикла: fast стоит
    раньше и завершается за одну уступку slow — без ожидания по времени), и slow
    находит треки только если токен остался его собственным."""
    path = str(marker).replace("\\", "/")
    track = json.dumps(TRACKS[0], ensure_ascii=False)
    return f'''import asyncio
import json
from pathlib import Path
MARKER = Path("{path}")
ROLE = "{role}"
TRACK = {track}

def _note(event):
    calls = json.loads(MARKER.read_text(encoding="utf-8")) if MARKER.exists() else []
    calls.append(event)
    MARKER.write_text(json.dumps(calls, ensure_ascii=False), encoding="utf-8")

def _token(ctx):
    ext = getattr(ctx, "extension", None)
    return str(ext.get("yandex_oauth") or "") if ext is not None else ""

async def search(ctx, query, kind):
    if ROLE == "fast":
        await asyncio.sleep(0)  # уступка управления: сосед успевает поставить свой контекст
        return {{"tracks": []}}  # здесь fast завершается: ядро восстанавливает ctx.extension
    _note(["search", "first", _token(ctx)])
    await asyncio.sleep(0)  # сосед завершается, пока slow висит; затем второй читок
    token = _token(ctx)
    _note(["search", "second", token])
    return {{"tracks": [TRACK], "start": 0, "context": {{}}}} if token else {{"tracks": []}}

async def neighbor(ctx, track_id, context, step):
    return None

PROVIDER = {{"id": "probe-" + ROLE, "title": "Проба параллельности", "service": "yandex-music"}}
'''


async def test_parallel_search_providers_keep_own_extension_context(tmp_path):
    """Параллельные опросы ступени не делят ExtensionContext: быстрый поставщик
    завершается и восстанавливает ctx.extension, пока медленный висит; у медленного
    контекст свой — токен не пропадает, треки находятся. На общем контексте
    восстановление соседа затирает токен медленного — он отвечает пусто, ступень
    не находит ничего («Я пока этого не умею» вместо музыки)."""
    runtime = Runtime(tmp_path)
    _disable_builtin_providers(runtime)
    fast_marker = tmp_path / "fast.json"
    slow_marker = tmp_path / "slow.json"
    make_music_extension(runtime, ext_id="fast-music", provider_id="fast",
                         files={"music/fast.py": _parallel_probe_module(fast_marker, "fast")},
                         phrases=MUSIC_PHRASES, module="music/fast.py")
    make_music_extension(runtime, ext_id="slow-music", provider_id="slow",
                         files={"music/slow.py": _parallel_probe_module(slow_marker, "slow")},
                         phrases=MUSIC_PHRASES, module="music/slow.py")
    runtime.extensions.load_enabled()
    try:
        music.note_token(runtime, "y0_" + "c" * 30)
        report = await _probe(runtime, "включи эминем")
        assert report["handled"] and report["scenario"] == "media.music", report
        assert report["speech"] == f"Включаю {TRACKS[0]['title']}.", report
        events = json.loads(slow_marker.read_text(encoding="utf-8"))
        assert events == [["search", "first", music.captured_oauth()],
                          ["search", "second", music.captured_oauth()]], events
    finally:
        await runtime.close()
