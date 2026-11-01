"""TASK-074: каталог приложений — миграция режима, три режима ответа, разделы,
версия в поле 8 (дефект «Обновить»), перехват get_app_catalog при любом маршруте."""
import json

from aiohttp import ClientSession

from server_emulator.catalog import Catalog, catalog_view, textfield, vint
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen


def app_record(identifier, name, package, version_code, apk="https://apk.test/x.apk", version="1.0"):
    item = b"".join(textfield(n, value) for n, value in
                    [(1, identifier), (2, name), (3, ""), (4, ""), (5, package), (7, version)])
    item += textfield(6, apk)
    return item + vint(8 * 8 + 0) + vint(version_code)  # поле 8, varint — versionCode


def group(title, records, category=None):
    body = b""
    if category is not None:
        body += vint(1 * 8 + 0) + vint(category)
    body += b"".join(textfield(2, record) for record in records)
    body += textfield(3, title)
    return textfield(1, body)


def base_catalog():
    """Обезличенная база каталога Яндекса: группа с enum и «Другое» (категория 0)."""
    return group("Игры", [app_record("y1", "Игра Яндекс", "com.yandex.game", 5)], category=7) \
        + group("Другое", [app_record("y2", "Своё Яндекс", "com.yandex.own", 3)])


def write_catalog(tmp_path, value):
    (tmp_path / "data" / "catalog").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "catalog" / "catalog.json").write_text(json.dumps(value), encoding="utf-8")


def test_migration_enabled_to_mode(tmp_path):
    write_catalog(tmp_path, {"enabled": True, "public_base_url": "http://rpc.alice.yandex.net", "apps": []})
    catalog = Catalog(tmp_path)
    assert catalog.value["mode"] == "merged" and "enabled" not in catalog.value
    write_catalog(tmp_path, {"enabled": False, "apps": []})
    catalog = Catalog(tmp_path)
    assert catalog.value["mode"] == "direct"
    assert catalog.value.get("sections") == []  # разделы присутствуют всегда


def test_own_record_version_in_field_eight(tmp_path):
    """Дефект «Обновить»: version_code уходил в поле 64, колонка читает поле 8 —
    после исправления запись читается собственной картой catalog_view."""
    write_catalog(tmp_path, {"mode": "own", "apps": [{"id": "a1", "name": "Палитра", "package": "com.test.palette",
                                                      "version": "1.0", "version_code": 5, "sha256": "abc"}]})
    catalog = Catalog(tmp_path)
    output = catalog.build_own("http://rpc.alice.yandex.net")
    apps = [app for group in catalog_view(output) for app in group["apps"]]
    mine = next(app for app in apps if app["name"] == "Палитра")
    assert mine["version_code"] == 5  # колонка видит versionCode в поле 8
    assert mine["apk_url"] == "http://rpc.alice.yandex.net/__catalog/apk/abc"
    assert mine["package"] == "com.test.palette"


def test_modes_direct_merged_own(tmp_path):
    write_catalog(tmp_path, {"mode": "direct",
                             "apps": [{"id": "a1", "name": "Палитра", "package": "com.test.palette",
                                       "version": "1.0", "version_code": 5, "sha256": "abc",
                                       "section": "Игры"}]})
    catalog = Catalog(tmp_path)
    base = base_catalog()
    assert catalog.patch(base) == base  # напрямую — база как есть
    catalog.value["mode"] = "merged"
    merged = catalog_view(catalog.patch(base))
    games = next(group for group in merged if group["title"] == "Игры")
    assert [app["name"] for app in games["apps"]] == ["Игра Яндекс", "Палитра"]  # в раздел Яндекса
    own_group = next(group for group in merged if group["title"] == "Другое")
    assert not any(app["name"] == "Палитра" for app in own_group["apps"])
    catalog.value["mode"] = "own"
    own = catalog_view(catalog.build_own("http://rpc.alice.yandex.net"))
    assert [group["title"] for group in own] == ["Другое"]  # групп Яндекса нет
    assert [app["name"] for app in own[0]["apps"]] == ["Палитра"]


def test_sections_order_and_unknown_goes_to_other(tmp_path):
    write_catalog(tmp_path, {"mode": "merged",
                             "sections": [{"id": "s1", "title": "Мои сказки", "order": 0},
                                          {"id": "s2", "title": "Мои игры", "order": 1}],
                             "apps": [{"id": "a1", "name": "Палитра", "package": "com.test.p", "version": "1",
                                       "version_code": 5, "sha256": "abc", "section": "s1"},
                                      {"id": "a2", "name": "Инструмент", "package": "com.test.i", "version": "1",
                                       "version_code": 2, "sha256": "def", "section": "s2"},
                                      {"id": "a3", "name": "Без раздела", "package": "com.test.n", "version": "1",
                                       "version_code": 1, "sha256": "000"},
                                      {"id": "a4", "name": "Мимо", "package": "com.test.m", "version": "1",
                                       "version_code": 1, "sha256": "111", "section": "несуществующий"}]})
    catalog = Catalog(tmp_path)
    merged = catalog_view(catalog.patch(base_catalog()))
    titles = [group["title"] for group in merged]
    assert titles[:2] == ["Игры", "Другое"]  # группы Яндекса раньше
    assert titles[2:] == ["Мои сказки", "Мои игры"]  # свои — по order
    tales = next(group for group in merged if group["title"] == "Мои сказки")
    assert [app["name"] for app in tales["apps"]] == ["Палитра"]
    other = next(group for group in merged if group["title"] == "Другое")
    # свои приложения без/с неизвестным разделом добавляются к группе Яндекса
    assert sorted(app["name"] for app in other["apps"]) == ["Без раздела", "Мимо", "Своё Яндекс"]
    # удаление раздела переносит приложения в «Другое»
    catalog.value["sections"] = [s for s in catalog.value["sections"] if s["id"] != "s1"]
    merged = catalog_view(catalog.patch(base_catalog()))
    other = next(group for group in merged if group["title"] == "Другое")
    assert "Палитра" in [app["name"] for app in other["apps"]]
    assert not any(group["title"] == "Мои сказки" for group in merged)


async def test_catalog_intercepted_on_proxy_route_and_fallbacks(tmp_path):
    """Каталог перехватывается и в «Прозрачно»: own — свои без Яндекса; без облака и кеша
    merged отдаёт свои, direct — 502."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-C", "user_id": "u",
                               "device_ids": ["c-id"]})
    runtime.identities.mutate({"operation": "route_mode", "sn": "SN-C", "mode": "proxy"})
    runtime.catalog.value["mode"] = "own"
    runtime.catalog.value["apps"] = [{"id": "a1", "name": "Палитра", "package": "com.test.p",
                                      "version": "1.0", "version_code": 5, "sha256": "abc",
                                      "apk_url": "https://apk.test/p.apk"}]
    called = []

    from server_emulator.upstream.forward import UpstreamError

    async def dead_upstream(*args, **kwargs):
        called.append(args)
        raise UpstreamError("сети нет")  # контракт forwarder: наверх всегда UpstreamError

    runtime.upstream.forwarder.http = dead_upstream
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/gproxy/get_app_catalog?device_id=c-id",
                                   headers={"Host": "rpc.alice.yandex.net"}) as response:
                assert response.status == 200
                groups = catalog_view(await response.read())
                assert [app["name"] for group in groups for app in group["apps"]] == ["Палитра"]
            assert called == []  # режим own: Яндекс не вызывался
            # merged без облака и кеша — свои приложения
            runtime.catalog.value["mode"] = "merged"
            async with client.post(url + "/gproxy/get_app_catalog?device_id=c-id",
                                   headers={"Host": "rpc.alice.yandex.net"}) as response:
                assert response.status == 200
                groups = catalog_view(await response.read())
                assert [app["name"] for group in groups for app in group["apps"]] == ["Палитра"]
            # direct без облака и кеша — честный 502
            runtime.catalog.value["mode"] = "direct"
            async with client.post(url + "/gproxy/get_app_catalog?device_id=c-id",
                                   headers={"Host": "rpc.alice.yandex.net"}) as response:
                assert response.status == 502
            # кеш есть — direct отдаёт его как есть
            runtime.catalog.store_upstream(base_catalog())
            async with client.post(url + "/gproxy/get_app_catalog?device_id=c-id",
                                   headers={"Host": "rpc.alice.yandex.net"}) as response:
                assert response.status == 200
                assert await response.read() == base_catalog()
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_catalog_api_mode_and_sections(tmp_path):
    """API: PUT режима и разделов; удаление раздела чистит section приложений."""
    runtime = Runtime(tmp_path)
    runtime.catalog.value["apps"] = [{"id": "a1", "name": "Палитра", "package": "com.test.p",
                                      "version": "1", "version_code": 5, "sha256": "abc", "section": "s1"}]
    runner, url = await listen(create_app(runtime=runtime))
    headers = {"X-Emulator": "1", "Content-Type": "application/json"}
    try:
        async with ClientSession() as client:
            async with client.put(url + "/emulator/api/catalog", headers=headers,
                                  json={"mode": "merged"}) as response:
                assert (await response.json())["mode"] == "merged"
            async with client.put(url + "/emulator/api/catalog", headers=headers,
                                  json={"sections": [{"id": "s1", "title": "Мои сказки", "order": 0}]}) as response:
                assert (await response.json())["sections"][0]["title"] == "Мои сказки"
            async with client.put(url + "/emulator/api/catalog/apps/a1", headers=headers,
                                  json={"section": "s1"}) as response:
                assert (await response.json())["section"] == "s1"
            async with client.put(url + "/emulator/api/catalog", headers=headers,
                                  json={"sections": []}) as response:
                assert (await response.json())["sections"] == []
            assert runtime.catalog.value["apps"][0]["section"] == ""  # в «Другое»
            async with client.get(url + "/emulator/api/catalog", headers=headers) as response:
                body = await response.json()
                assert body["mode"] == "merged" and body["yandex"]["cached"] is False
    finally:
        await runner.cleanup()
        await runtime.close()
