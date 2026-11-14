"""Домен providers («Поставщики данных»): виды поставщиков и выбор активного.

Реестр видов — server_emulator/providers.py; на свежем Runtime музыка поставляется
встроенными плагинами (builtin.spotify/builtin.yandex-music — включены по умолчанию),
внешние расширения добавляются рядом (origin extension). Выбор — точечное сохранение
settings["providers"][<вид>] (пусто — авто), неизвестный вид или выключенный
поставщик — 400. Статус Яндекс.Музыки — захват токена аккаунта (без вывода токена).
"""
import json

from aiohttp import ClientSession

from server_emulator import music, providers
from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen
from tests.test_music_provider import MUSIC_PHRASES, _provider_module, make_music_extension


async def api_get(url, client):
    async with client.get(url + "/emulator/api/providers") as r:
        assert r.status == 200
        return await r.json()


async def api_put(url, client, body, headers=None):
    async with client.put(url + "/emulator/api/providers", json=body,
                          headers={"X-Emulator": "1", **(headers or {})}) as r:
        return r, await r.json()


async def test_call_invoker_of_consumer(tmp_path):
    """Вызов функции поставщика: потребитель вида объявил invoker
    (providers.call_invoker) — invoke зовёт им; без объявления — ExtensionContext
    плагина первым аргументом, как раньше."""
    runtime = Runtime(tmp_path)

    class _Ctx:
        def _services_runtime(self):
            return runtime

    async def invoker(ctx, provider, function, *args):
        return ("by-invoker", provider["kind"], args)

    providers.call_invoker("fake-kind", invoker)
    try:
        async def function(context, *args):
            return ("by-context", context, args)

        record = {"kind": "fake-kind", "ext_id": "x"}
        assert await providers.invoke(_Ctx(), record, function, 1) == ("by-invoker", "fake-kind", (1,))
        base = await providers.invoke(_Ctx(), {"kind": "other-kind", "ext_id": "x"}, function, 1)
        assert base[0] == "by-context" and base[2] == (1,)  # без объявления — прежний путь
        assert base[1].get("yandex_oauth") in (None, "")  # системных значений в нём нет
    finally:
        providers.CALL_INVOKERS.pop("fake-kind", None)  # объявление тестового вида — снять


def _disable_builtins(runtime):
    state = runtime.settings.setdefault("extensions", {})
    for ext_id in list(state["installed"]):
        if ext_id.startswith("builtin."):
            state["enabled"][ext_id] = False
    runtime.extensions.load_enabled()


async def test_builtin_providers_listed_on_fresh_runtime(tmp_path):
    """Без внешних расширений вкладка «Музыка» показывает встроенных: origin builtin,
    версия эмулятора в карточках «Расширений», авто — первый по алфавиту."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            data = await api_get(url, client)
            music_kind = data["kinds"]["music"]
            assert music_kind["title"] == "Музыка"
            assert [p["id"] for p in music_kind["providers"]] == ["builtin.spotify", "builtin.yandex-music"]
            assert all(p["origin"] == "builtin" and p["enabled"] for p in music_kind["providers"])
            assert music_kind["selected"] == ""  # авто
            # запись реестра держит модуль поставщика (_module) — наружу идут только
            # объявленные поля, поэтому payload сериализуется как есть
            assert all(set(p) == {"id", "title", "extension", "origin", "enabled",
                                  "login", "status"}
                       for p in music_kind["providers"])
            yandex = music_kind["providers"][1]
            assert yandex["status"]["oauth_captured"] is False
            assert "y0_" not in json.dumps(data)  # токен наружу не идёт
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_selection_persists_in_providers_settings(tmp_path):
    """Выбор активного сохраняется точечно (settings["providers"]["music"]) и
    возвращается GET; авто («») возвращает реестр к первому по алфавиту."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            r, _body = await api_put(url, client, {"kind": "music", "id": "builtin.yandex-music"})
            assert r.status == 200
            assert runtime.settings["providers"]["music"] == "builtin.yandex-music"
            assert music.selected(runtime.settings)["id"] == "builtin.yandex-music"
            data = await api_get(url, client)
            assert data["kinds"]["music"]["selected"] == "builtin.yandex-music"
            r, _body = await api_put(url, client, {"kind": "music", "id": ""})
            assert r.status == 200
            assert runtime.settings["providers"]["music"] == ""
            assert music.selected(runtime.settings)["id"] == "builtin.spotify"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_external_provider_lives_next_to_builtin(tmp_path):
    """Внешнее расширение с поставщиком — отдельная запись реестра (origin extension),
    выбор сохраняется; PUT сохраняет настройки расширения через общий менеджер."""
    runtime = Runtime(tmp_path)
    make_music_extension(runtime, ext_id="fake-ext",
                         files={"music/fake.py": _provider_module(tmp_path / "m.json")},
                         phrases=MUSIC_PHRASES)
    runtime.extensions.load_enabled()
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            data = await api_get(url, client)
            ids = [p["id"] for p in data["kinds"]["music"]["providers"]]
            assert "fake" in ids and "builtin.spotify" in ids  # рядом с встроенным
            fake = next(p for p in data["kinds"]["music"]["providers"] if p["id"] == "fake")
            assert fake["extension"] == "fake-ext" and fake["origin"] == "extension"
            r, _body = await api_put(url, client, {"kind": "music", "id": "fake"})
            assert r.status == 200
            assert runtime.settings["providers"]["music"] == "fake"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_unknown_provider_and_kind_rejected(tmp_path):
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            r, body = await api_put(url, client, {"kind": "music", "id": "no-such"})
            assert r.status == 400 and "Неизвестный" in body["error"]
            r, body = await api_put(url, client, {"kind": "weather", "id": "builtin.open-meteo"})
            assert r.status == 200  # вид weather — настоящий поставщик (встроенный Open-Meteo)
            assert runtime.settings["providers"]["weather"] == "builtin.open-meteo"
            r, body = await api_put(url, client, {"kind": "tv", "id": ""})
            assert r.status == 400  # вида tv в реестре видов нет
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_no_providers_no_kinds(tmp_path):
    """Нет включённых плагинов с поставщиками — видов нет (группы вкладок не будет)."""
    runtime = Runtime(tmp_path)
    _disable_builtins(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            data = await api_get(url, client)
            assert data["kinds"] == {}
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_yandex_builtin_status_shows_token_capture(tmp_path):
    """Статус Яндекс Музыки — захват токена аккаунта (без вывода самого токена в API)."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            assert music.note_token(runtime, "y0_" + "e" * 30)
            data = await api_get(url, client)
            provider = next(p for p in data["kinds"]["music"]["providers"]
                            if p["id"] == "builtin.yandex-music")
            assert provider["status"]["oauth_captured"] is True
            assert "y0_" not in json.dumps(data)  # сам токен наружу не идёт
            spotify = next(p for p in data["kinds"]["music"]["providers"]
                           if p["id"] == "builtin.spotify")
            assert spotify["status"] == {"login_authorized": False}  # статус входа Spotify
    finally:
        await runner.cleanup()
        await runtime.close()
