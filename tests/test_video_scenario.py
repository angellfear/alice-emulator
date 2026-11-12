"""Вид video: встроенный каталог ссылок и сценарий media.video (выдача + выбор по номеру).

Контракт вида: catalog(ctx) → {"items": [запись, ...], "note"?}, ctx — ExtensionContext
плагина. Сценарий media.video собирает open_search по образцу журнала (карточки
SearchVideoItem с сущностями selected_video_title + tts_play_placeholder) и запускает
запись по номеру (play_web_video_directive + «Запускаю.») из памяти последней выдачи.
"""
import datetime
import importlib.util
import json
from pathlib import Path

import pytest

import server_emulator
from server_emulator import providers
from server_emulator.runtime import Runtime

MODULE = Path(server_emulator.__file__).parent / "plugins" / "video-links" / "video" / "video_links.py"

# каталог «владельца»: три комедии (выдача по запросу «комедий») и один мультфильм
CATALOG = [
    {"title": "Комедия про агентов", "embed": "https://example.com/embed/one",
     "url": "https://example.com/watch/one", "genre": "комедия", "duration": 600},
    {"title": "Комедия про путешествия", "embed": "https://example.com/embed/two",
     "genre": "комедия", "duration": 900},
    {"title": "Комедия про космос", "embed": "https://example.com/embed/three",
     "genre": "комедия"},
    {"title": "Мультик про кота", "embed": "https://example.com/embed/four",
     "genre": "мультфильм", "year": 2020},
]

SEARCH_PROTO = "NAlice.TSearchCapability.TOpenSearchDirective"
PLAY_PROTO = "NAlice.TWebPlayerCapability.TPlayWebVideoDirective"


def load_provider():
    """Модуль встроенного плагина как отдельный (без импорта остального кода ядра)."""
    spec = importlib.util.spec_from_file_location("builtin_video_links", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExtCtx:
    """Минимальный ExtensionContext-двойник."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key, default=None):
        return self._values.get(key, default)


def make_runtime(tmp_path, catalog=CATALOG):
    """Runtime с владельцем и колонкой; музыкальные поставщики и встроенный
    video-links выключены, каталог — golden-плагин, читающий свой JSON-файл."""
    runtime = Runtime(tmp_path)
    state = runtime.settings.setdefault("extensions", {})
    for ext_id in ("builtin.spotify", "builtin.yandex-music", "builtin.video-links"):
        state["enabled"][ext_id] = False
    runtime.extensions.load_enabled()
    runtime.identities.mutate({"operation": "user", "id": "owner", "name": "O"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-N", "user_id": "owner",
                               "name": "Станция"})
    if catalog is not None:
        ext = runtime.directory / "ext" / "golden-video"
        (ext / "video").mkdir(parents=True, exist_ok=True)
        catalog_file = ext / "catalog.json"
        catalog_file.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
        (ext / "extension.json").write_text(json.dumps({
            "id": "golden-video", "name": "golden-video", "version": "1.0.0", "description": "d",
            "author": "A", "license": "MIT",
            "requires": {"emulator": ">=0.1.0", "sdk": ">=2.7.0"}, "permissions": {},
            "contributes": {"providers": [{"kind": "video", "title": "Видео",
                                           "module": "video/golden.py",
                                           "phrases": "phrases/video.json"}]}}), encoding="utf-8")
        golden = chr(10).join([
            "import json",
            "from pathlib import Path",
            "FILE = Path(r'" + str(catalog_file).replace("\\", "/") + "')",
            'PROVIDER = {"id": "golden-video", "title": "Видео (каталог)"}',
            "async def catalog(ctx):",
            "    parsed = json.loads(FILE.read_text(encoding='utf-8'))",
            "    return parsed if isinstance(parsed, dict) else {'items': parsed}"])
        (ext / "video" / "golden.py").write_text(golden, encoding="utf-8")
        phrases_dir = ext / "phrases"
        phrases_dir.mkdir(exist_ok=True)
        (phrases_dir / "video.json").write_text(json.dumps({"entries": [
            {"id": "search", "sentences": ["открой список {query}", "список {query}"],
             "handler": "core:media.video"},
            {"id": "select", "sentences": ["[номер ]{num}"],
             "lists": {"num": {"values": [
                 {"in": "один", "out": 1}, {"in": "два", "out": 2}, {"in": "три", "out": 3},
                 {"in": "двадцать", "out": 20}]}},
             "handler": "core:media.video"}]}, ensure_ascii=False), encoding="utf-8")
        state["installed"]["golden-video"] = {"version": "1.0.0", "dev": str(ext)}
        state["enabled"]["golden-video"] = True
        runtime.extensions.load_enabled()
    return runtime


async def _say(runtime, text, route="emulate", sn="SN-N"):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": sn,
                     "text": text},
           "clock": {"now": datetime.datetime(2026, 10, 27, 21, 7)}, "sequence": 1,
           "route_mode": route, "player_owner": None,
           **runtime._scenario_context(sn=sn)}
    return await runtime.dialog.handle_text(text, ctx, "dg", None, deliver=False)


def _directives(report):
    """Все директивы ответа в порядке доставки."""
    out = []
    for message in report.get("messages") or []:
        if message.Event.WhichOneof("Event") != "AliceResponse":
            continue
        out.extend(message.Event.AliceResponse.Response.Directives)
    return out


def _decode(directive, proto):
    """PayloadRaw по схеме станции; нет схем — тест пропускается вызывающим."""
    from server_emulator.schema import load_type, schemas_dir, station_file
    cls = load_type(Path(server_emulator.__file__).parent / "schemas",
                    station_file(schemas_dir()), proto)
    message = cls()
    message.ParseFromString(directive.PayloadRaw)
    return message


def _named(report, name):
    """Директивы с именем name в порядке доставки."""
    return [d for d in _directives(report) if d.Name == name]


async def test_video_links_provider_normalizes_catalog():
    """Каталог из настройки: явные host/player — как есть, год — датой, битые записи пропущены."""
    module = load_provider()
    result = await module.catalog(ExtCtx({"videos": json.dumps([
        {"title": "Одно", "embed": "https://vkvideo.ru/video_ext.php?x=1", "duration": "90"},
        {"title": "Два", "embed": "https://example.com/e", "player": "vk", "host": "vk.com",
         "year": 2020},
        {"embed": "https://example.com/no-title"},
        {"title": "Без плеера"},
        "строка вместо записи",
    ], ensure_ascii=False)}))
    items = result["items"]
    assert len(items) == 2
    assert "host" not in items[0] and "player" not in items[0]
    assert items[0]["duration"] == 90
    assert items[1]["player"] == "vk" and items[1]["host"] == "vk.com"
    assert 1890 <= int(items[1]["released"]) // 31536000 + 1970 <= 2999


async def test_video_links_provider_broken_settings_is_note():
    """Битый JSON настройки — note с честным отказом, сценарий его произносит."""
    module = load_provider()
    result = await module.catalog(ExtCtx({"videos": "{битый json"}))
    assert result["items"] == [] and "JSON" in result["note"]
    result = await module.catalog(ExtCtx({}))
    assert result["items"] == [] and result["note"]


async def test_builtin_video_links_in_registry(tmp_path):
    """Свежий Runtime: builtin.video-links в реестре вида video; выбран по умолчанию."""
    runtime = Runtime(tmp_path)
    try:
        records = providers.PROVIDERS["video"]
        assert "builtin.video-links" in records
        assert records["builtin.video-links"]["origin"] == "builtin"
        assert callable(records["builtin.video-links"]["catalog"])
        assert providers.selected("video", runtime.settings)["id"] == "builtin.video-links"
    finally:
        await runtime.close()


async def test_search_shows_open_search_cards(tmp_path):
    """«Открой список комедий»: open_search с тремя карточками + tts_play_placeholder, без речи."""
    runtime = make_runtime(tmp_path)
    try:
        report = await _say(runtime, "открой список комедий")
        assert report["handled"], report
        assert report["scenario"] == "media.video"
        assert report["speech"] == ""
        directives = _directives(report)
        assert [d.Name for d in directives] == ["open_search", "tts_play_placeholder"]
        assert directives[0].IsLedSilent and not directives[0].AnalyticsType
        if not (Path(server_emulator.__file__).parent / "schemas" / "alice-api.pb").is_file():
            pytest.skip("нет схем протокола")
        search = _decode(directives[0], SEARCH_PROTO)
        gallery = search.SearchResultData.Galleries[0].BasicCarousel
        assert search.SearchQuery == "комедий" and search.SearchResultData.SearchText == "комедий"
        assert gallery.Title == "Результаты поиска по видео"
        assert [item.Number for item in gallery.Items] == [1, 2, 3]
        first = gallery.Items[0]
        video = first.SearchVideoItem
        assert video.ContentType == "video" and video.Title == "Комедия про агентов"
        assert video.EmbedUri == "https://example.com/embed/one"
        assert video.Id == "https://example.com/watch/one" and int(video.Duration) == 600
        assert video.Hosting == "example.com" and video.PlayerId == "example"
        assert len(first.VoiceControls) == 2
        for control in first.VoiceControls:
            entity = control.ExternalEntitiesDescriptions[0]
            assert control.ActivationConditions[0].VisibleCondition.VisibleShare == 0.01
            assert entity.Name == "selected_video_title"
            phrases = [p.Phrase for p in entity.Items[0].Phrases]
            assert phrases == ["Комедия про агентов", "1", "номер 1"]
            meta = entity.Items[0].VideoMeta
            assert meta.Type == "video" and meta.Position == 1 and meta.Duration == 600
    finally:
        await runtime.close()


async def test_search_filter_umbrella_and_refusals(tmp_path):
    """Общее слово — весь каталог; жанр — фильтр; совпадений нет или меньше трёх — отказ."""
    runtime = make_runtime(tmp_path)
    try:
        report = await _say(runtime, "список фильмов")  # общее слово — весь каталог
        assert report["scenario"] == "media.video"
        assert len(_directives(report)) == 2
        report = await _say(runtime, "список ужастики")  # в каталоге нет
        assert "ничего нет" in report["speech"] and not _named(report, "open_search")
    finally:
        await runtime.close()


async def test_search_fewer_than_three_is_refusal(tmp_path):
    """Жанр отфильтровал меньше трёх записей — выдача не собирается, честный отказ."""
    runtime = make_runtime(tmp_path, catalog=[CATALOG[0], CATALOG[1], CATALOG[3]])
    try:
        report = await _say(runtime, "список мультиков")  # один мультфильм
        assert "Нашлось всего 1" in report["speech"]
        report = await _say(runtime, "открой список комедий")  # две комедии
        assert "Нашлось всего 2" in report["speech"] and not _named(report, "open_search")
    finally:
        await runtime.close()


async def test_select_number_plays_video(tmp_path):
    """«Один»/«номер два» после выдачи — play_web_video_directive + «Запускаю.»."""
    runtime = make_runtime(tmp_path)
    try:
        report = await _say(runtime, "открой список комедий")
        assert len(_directives(report)) == 2
        report = await _say(runtime, "один")
        assert report["handled"] and report["scenario"] == "media.video"
        assert report["speech"] == "Запускаю."
        directives = _directives(report)
        assert [d.Name for d in directives] == ["play_web_video_directive", "tts_play_placeholder"]
        if not (Path(server_emulator.__file__).parent / "schemas" / "alice-api.pb").is_file():
            pytest.skip("нет схем протокола")
        play = _decode(directives[0], PLAY_PROTO)
        assert directives[0].AnalyticsType == "Комедия про агентов"
        assert play.EmbedContent.EmbedUri == "https://example.com/embed/one"
        assert play.Title == play.Name == "Комедия про агентов"
        assert play.PlaybackTiming.Duration == 600
        assert play.SearchUrl == "https://example.com/watch/one"
        report = await _say(runtime, "номер два")
        play = _decode(_directives(report)[0], PLAY_PROTO)
        assert play.EmbedContent.EmbedUri == "https://example.com/embed/two"
        report = await _say(runtime, "двадцать")  # за пределами выдачи
        assert report["speech"] == "В списке всего 3."
    finally:
        await runtime.close()


async def test_select_without_memory_declines(tmp_path):
    """«Один» без выдачи — сценарий отказывается, фразу забирает следующий кандидат."""
    runtime = make_runtime(tmp_path)
    try:
        report = await _say(runtime, "один")
        assert report.get("scenario") != "media.video", report
        await _say(runtime, "открой список комедий")
        report = await _say(runtime, "один", sn="SN-DUO")  # выдача другой колонки — не своя
        assert report.get("scenario") != "media.video", report
    finally:
        await runtime.close()


async def test_hybrid_route_declines(tmp_path):
    """«Вместе» — сценарий отказывается: Яндекс отвечает сам."""
    runtime = make_runtime(tmp_path)
    try:
        report = await _say(runtime, "открой список комедий", route="hybrid")
        assert report.get("scenario") != "media.video", report
    finally:
        await runtime.close()


async def test_plugin_off_no_phrases(tmp_path):
    """Плагина видео нет — сценарий недостижим ни одной фразой."""
    runtime = make_runtime(tmp_path, catalog=None)
    try:
        for phrase in ("открой список комедий", "список фильмов", "один"):
            report = await _say(runtime, phrase)
            assert report.get("scenario") != "media.video", (phrase, report)
    finally:
        await runtime.close()


async def test_provider_failure_and_note_are_honest(tmp_path):
    """Сбой поставщика и note пустого каталога — честные реплики. Разговор до подмены
    обязателен: сценарий при загрузке вызывает providers.require и возвращает записям
    функции из их модулей — подмена до первого диалога была бы затёрта."""
    runtime = make_runtime(tmp_path)
    try:
        await _say(runtime, "список фильмов")  # прогрев: сценарий загружен, require отработал
        records = providers.PROVIDERS["video"]

        async def broken(ctx):
            raise ConnectionError("каталог недоступен")

        async def empty(ctx):
            return {"items": [], "note": "Каталог ещё не заполнен."}

        records["golden-video"]["catalog"] = broken
        report = await _say(runtime, "открой список комедий")
        assert report["handled"] and report["speech"] == "Каталог видео недоступен."
        records["golden-video"]["catalog"] = empty
        report = await _say(runtime, "открой список комедий")
        assert report["speech"] == "Каталог ещё не заполнен."
    finally:
        await runtime.close()
