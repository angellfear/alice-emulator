"""TASK-051: картинки устройств с веб-версии умного дома — на синтетике.

Локальный http-сервер отдаёт поддельные HTML, CSS и webp; базовые URL и список разрешённых хостов
подменяются. К Яндексу тесты не обращаются.
"""
import collections

import pytest
from aiohttp import ClientSession, web

from server_emulator import device_images as di
from server_emulator.device_images import DEVICE_IMAGES, DeviceImages, ImageFetchError
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

WEBP = b"RIFF\x08\x00\x00\x00WEBPVP8 "
HOSTS = ("127.0.0.1",)


def image_set(url2, url3=None):
    sets = f"url({url2})2x" + (f",url({url3})3x" if url3 else "")
    return f"background-image:-webkit-image-set({sets});background-image:image-set({sets})"


def rule(selectors, body):
    return ",".join(selectors) + "{" + body + "}"


T = "." + di._TYPE_PREFIX
C = "." + di._COLOR_PREFIX
ON = ".device-image__icon_on"


def css_for(base):
    return "\n".join([
        ".other{color:red}",
        rule([T + "station-chiron", T + "station-chiron" + C + "black"], image_set(base + "/i/chiron-2.webp", base + "/i/chiron-3.webp")),
        rule([T + "station-chiron" + ON], image_set(base + "/i/chiron-on.webp", base + "/i/chiron-on3.webp")),
        rule([T + "station-chiron" + C + "black.device-image__icon_mod-ready-for-pairing"], image_set(base + "/i/mod.webp", base + "/i/mod3.webp")),
        rule([T + "station-chiron.device-image__icon_file-resolution_large"], image_set(base + "/i/large.webp", base + "/i/large3.webp")),
        rule([T + "station-chiron" + C + "beige"], image_set(base + "/i/beige-2.webp")),   # только 2x
        "@media (min-width:1px){" + rule([T + "fiero-blackthorn"], image_set(base + "/i/fiero-2.webp", base + "/i/fiero-3.webp")) + "}",
        rule([T + "station-plum" + C + "black"], image_set("https://evil.example/x.webp", "https://evil.example/y.webp")),
        rule([".wrap " + T + "station-orion"], image_set(base + "/i/descendant.webp", base + "/i/descendant3.webp")),
        rule([T + "station-mango" + C + "green"], "color:red"),                              # без картинки
    ])


async def serve(handlers):
    app = web.Application()
    for path, handler in handlers.items():
        app.router.add_get(path, handler)
    return await listen(app)


def make_site(css=css_for, images=None, hits=None):
    """Маршруты поддельного сайта: /iot/ → HTML, /s3/main@iot-app.css, /i/*.webp."""
    hits = collections.Counter() if hits is None else hits

    async def page(request):
        hits["page"] += 1
        return web.Response(text=f'<html><link rel="stylesheet" href="http://{request.host}/s3/v1/main@iot-app.css"></html>')

    async def style(request):
        hits["css"] += 1
        return web.Response(text=css("http://" + request.host), content_type="text/css")

    async def image(request):
        name = request.match_info["name"]
        hits["image:" + name] += 1
        if images is not None and name in images:
            return images[name](request)
        return web.Response(body=WEBP + name.encode())

    return {"/iot/": page, "/s3/v1/main@iot-app.css": style, "/i/{name}": image}, hits


def fetcher(url, tmp_path, **kwargs):
    return DeviceImages(tmp_path / "devices", base_url=url + "/iot/", allowed_hosts=HOSTS, insecure=True, **kwargs)


# ── Разбор CSS ───────────────────────────────────────────────────────────────

def test_parse_css_takes_plain_rules_only():
    base = "http://127.0.0.1:1"
    table = di.parse_css(css_for(base), HOSTS, insecure=True)
    assert table["station-chiron"] == {"-": base + "/i/chiron-3.webp", "black": base + "/i/chiron-3.webp",
                                       "beige": base + "/i/beige-2.webp"}   # 3x, иначе 2x; `_on`, `_mod-…`, размеры — мимо
    assert table["fiero-blackthorn"] == {"-": base + "/i/fiero-3.webp"}      # правило внутри @media
    assert "station-plum" not in table                                       # чужой хост
    assert "station-orion" not in table                                      # селектор с предком
    assert "station-mango" not in table                                      # нет image-set


def test_parse_css_requires_https_and_known_hosts_by_default():
    css = rule([T + "station-chiron"], image_set("http://pudya.yastatic.net/a.webp", "https://pudya.yastatic.net/a3.webp"))
    assert di.parse_css(css)["station-chiron"]["-"] == "https://pudya.yastatic.net/a3.webp"
    http_only = rule([T + "station-chiron"], image_set("http://pudya.yastatic.net/a.webp"))
    assert di.parse_css(http_only) == {}
    other_host = rule([T + "station-chiron"], image_set("https://pudya.yastatic.net.evil.example/a.webp"))
    assert di.parse_css(other_host) == {}
    assert di.parse_css(rule([T + "station-chiron"], image_set("https://pudya.yastatic.net:8443/a.webp"))) == {}


def test_resolve_falls_back_to_plain_rule_and_skips_unknown():
    table = {"station-chiron": {"-": "u-plain", "black": "u-black"}, "fiero-blackthorn": {"-": "u-fiero"}}
    found = di.resolve(table)
    assert found == {"iot_station_chiron_black": "u-black", "iot_station_chiron_biege": "u-plain",
                     "iot_station_chiron_green": "u-plain", "iot_station_chiron_red": "u-plain",
                     "iot_station_fiero": "u-fiero"}


def test_css_links_from_html():
    html = ('<link href="//pudya.yastatic.net/s3/pudya/app/v1.2.3-ab/main@iot-app.css">'
            '<script>{"x":"\\u002F\\u002Fpudya.yastatic.net\\u002Fs3\\u002Fpudya\\u002Fapp\\u002Fv1.2.3-ab\\u002Fchunk-extended@iot-app.css"}</script>'
            '<link href="https://pudya.yastatic.net/s3/pudya/app/v1.2.3-ab/main@iot-app.css">')
    links = di.css_links(html, "https://iot.quasar.yandex.ru/iot/")
    assert links == ["https://pudya.yastatic.net/s3/pudya/app/v1.2.3-ab/main@iot-app.css",
                     "https://pudya.yastatic.net/s3/pudya/app/v1.2.3-ab/chunk-extended@iot-app.css"]
    assert di.css_links("<html>без стилей</html>", "https://iot.quasar.yandex.ru/iot/") == []


def test_device_table_is_consistent():
    assert len(DEVICE_IMAGES) == 43 == len({name for name in DEVICE_IMAGES})
    assert all(name.startswith("iot_") for name in DEVICE_IMAGES)


# ── Загрузка ─────────────────────────────────────────────────────────────────

def full_css(base):
    return "\n".join(rule([T + kind + (C + color if color else "")], image_set(f"{base}/i/{name}-2.webp", f"{base}/i/{name}.webp"))
                     for name, (kind, color) in DEVICE_IMAGES.items())


async def test_refresh_downloads_images_and_skips_known_ones(tmp_path):
    handlers, hits = make_site(css=full_css)
    runner, url = await serve(handlers)
    try:
        images = fetcher(url, tmp_path)
        assert images.count() == 0
        status = await images.refresh()
        assert status["images"] == status["images_total"] == len(DEVICE_IMAGES) and not status["images_error"]
        assert (tmp_path / "devices" / "iot_station_fiero.webp").read_bytes() == WEBP + b"iot_station_fiero.webp"   # 3x
        assert not [path for path in (tmp_path / "devices").iterdir() if path.suffix == ".tmp"]
        first = sum(count for key, count in hits.items() if key.startswith("image:"))
        assert first == len(DEVICE_IMAGES)
        await images.refresh()                                   # ничего не изменилось: webp не перекачиваются
        assert sum(count for key, count in hits.items() if key.startswith("image:")) == first
        assert hits["page"] == hits["css"] == 2
        (tmp_path / "devices" / "iot_station_fiero.webp").unlink()
        await images.refresh()                                   # пропавший файл докачивается
        assert (tmp_path / "devices" / "iot_station_fiero.webp").is_file()
        assert sum(count for key, count in hits.items() if key.startswith("image:")) == first + 1
    finally:
        await runner.cleanup()


async def test_refresh_partial_coverage_and_changed_url(tmp_path):
    version = {"n": 1}

    def css(base):
        return rule([T + "station-chiron" + C + "black"], image_set(f"{base}/i/v{version['n']}.webp", f"{base}/i/v{version['n']}-3.webp"))

    handlers, hits = make_site(css=css)
    runner, url = await serve(handlers)
    try:
        images = fetcher(url, tmp_path)
        status = await images.refresh()
        assert status["images"] == 1                              # остальных правил на сайте нет — силуэт
        assert (tmp_path / "devices" / "iot_station_chiron_black.webp").read_bytes().endswith(b"v1-3.webp")
        version["n"] = 2                                          # сайт обновил картинку (новый url)
        await images.refresh()
        assert (tmp_path / "devices" / "iot_station_chiron_black.webp").read_bytes().endswith(b"v2-3.webp")
    finally:
        await runner.cleanup()


async def test_refresh_rejects_bad_responses(tmp_path):
    def broken(kind):
        def handler(request):
            if kind == "text":
                return web.Response(body=b"<html>not an image</html>")
            if kind == "huge":
                return web.Response(body=WEBP + b"x" * (di.MAX_IMAGE + 10))
            if kind == "status":
                return web.Response(status=500)
            raise AssertionError(kind)
        return handler

    for kind in ("text", "huge", "status"):
        handlers, _ = make_site(css=full_css, images={name + ".webp": broken(kind) for name in DEVICE_IMAGES})
        runner, url = await serve(handlers)
        try:
            images = fetcher(url, tmp_path / kind)
            with pytest.raises(ImageFetchError):
                await images.refresh()
            assert images.count() == 0 and images.error
        finally:
            await runner.cleanup()


async def test_refresh_errors_in_russian_when_site_unavailable_or_changed(tmp_path):
    images = fetcher("http://127.0.0.1:9", tmp_path)               # порт закрыт: нет сети
    with pytest.raises(ImageFetchError, match="Нет связи"):
        await images.refresh()
    assert images.status()["images_error"].startswith("Нет связи") and not images.busy

    async def no_css(request):
        return web.Response(text="<html>без ссылки на стили</html>")
    runner, url = await serve({"/iot/": no_css})
    try:
        with pytest.raises(ImageFetchError, match="main@iot-app.css"):
            await fetcher(url, tmp_path / "b").refresh()
    finally:
        await runner.cleanup()


async def test_redirect_to_foreign_host_is_refused(tmp_path):
    def redirect(request):
        raise web.HTTPFound("http://example.org/x.webp")

    handlers, _ = make_site(css=full_css, images={"iot_station_fiero.webp": redirect})
    runner, url = await serve(handlers)
    try:
        images = fetcher(url, tmp_path)
        await images.refresh()                                    # остальные скачались, редирект наружу отклонён
        assert not (tmp_path / "devices" / "iot_station_fiero.webp").exists()
        assert images.count() == len(DEVICE_IMAGES) - 1
    finally:
        await runner.cleanup()


async def test_production_settings_refuse_local_http_server(tmp_path):
    handlers, hits = make_site(css=full_css)
    runner, url = await serve(handlers)
    try:
        images = DeviceImages(tmp_path / "devices", base_url=url + "/iot/", allowed_hosts=HOSTS)   # insecure=False
        with pytest.raises(ImageFetchError, match="разрешённых"):
            await images.refresh()
        assert not hits
    finally:
        await runner.cleanup()


def test_cli_requires_accept(capsys):
    from server_emulator import device_images
    assert device_images.main([]) == 2                         # без --accept — только условие, без сети
    assert "ООО «Яндекс»" in capsys.readouterr().err


# ── API и UI-раздача ─────────────────────────────────────────────────────────

async def test_api_refreshes_images_and_serves_them(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICA_IMAGES_DIR", str(tmp_path / "devices"))
    monkeypatch.setenv("ALICA_SCHEMAS_DIR", str(tmp_path / "schemas"))
    handlers, hits = make_site(css=full_css)
    site, site_url = await serve(handlers)
    runtime = Runtime(tmp_path / "state")
    runtime.device_images = fetcher(site_url, tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/setup") as response:
                state = await response.json()
            assert state["images"] == 0 and state["images_total"] == len(DEVICE_IMAGES)
            async with client.get(url + "/emulator/img/devices/iot_station_fiero.webp") as response:
                assert response.status == 404                       # нет картинки — UI покажет силуэт
            assert not hits                                         # запрос картинки ничего не скачивает сам
            async with client.post(url + "/emulator/api/setup/images", json={"accept": True}) as response:
                assert response.status == 403                       # admin-guard: нужен X-Emulator
            async with client.post(url + "/emulator/api/setup/images", headers={"X-Emulator": "1"}) as response:
                assert response.status == 400                       # без согласия не качаем
            async with client.post(url + "/emulator/api/setup/images", headers={"X-Emulator": "1"},
                                   json={"accept": False}) as response:
                assert response.status == 400
            assert not hits
            async with client.post(url + "/emulator/api/setup/images", headers={"X-Emulator": "1"},
                                   json={"accept": True}) as response:
                assert response.status == 200
                body = await response.json()
            assert body["images"] == body["images_total"] == len(DEVICE_IMAGES)
            async with client.get(url + "/emulator/img/devices/iot_station_fiero.webp") as response:
                assert response.status == 200
                assert response.headers["Content-Type"] == "image/webp"
    finally:
        await runner.cleanup()
        await site.cleanup()


async def test_api_reports_missing_network_without_breaking_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICA_IMAGES_DIR", str(tmp_path / "devices"))
    monkeypatch.setenv("ALICA_SCHEMAS_DIR", str(tmp_path / "schemas"))
    runtime = Runtime(tmp_path / "state")
    runtime.device_images = fetcher("http://127.0.0.1:9", tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/emulator/api/setup/images", headers={"X-Emulator": "1"},
                                   json={"accept": True}) as response:
                assert response.status == 502
                body = await response.json()
            assert "Нет связи" in body["error"] and body["images"] == 0
            async with client.get(url + "/emulator/api/setup") as response:
                assert (await response.json())["images_error"].startswith("Нет связи")
            async with client.get(url + "/emulator/") as response:
                assert response.status == 200
            async with client.get(url + "/emulator/img/generic/speaker.svg") as response:
                assert response.status == 200
    finally:
        await runner.cleanup()
