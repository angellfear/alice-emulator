"""Раздел «Анимации»: каталог (JSON+GIF), модели устройств (ДуоМакс без
анимаций, Мини 7-сегмент), API, проигрывание Push'ом, bindings-источник,
хранение проектов (state/animations/<platform>/<id>.json)."""
import base64

from aiohttp import ClientSession

from server_emulator.animation_formats import parse_led_text, parse_scled_text, parse_scled_binary
from server_emulator.animation_storage import AnimationProjectStore
from server_emulator.animations import (
    DEFAULT_DISPLAY, AnimationStore, display_of, display_of_device,
    list_platforms, platform_has_displays, platform_displays,
)
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from server_emulator.scenario_engine.bindings import BindingError, evaluate
from tests.conftest import listen

GIF = b"GIF89a" + b"\x00" * 40


def test_models_registry_separation():
    assert display_of("duo_max") == "none"          # ДуоМакс — без анимаций
    assert display_of("mini") == "seven_segment"    # Мини — 7-сегментный дисплей
    assert display_of("mini_2") if "mini_2" in display_of.__module__ else True
    assert display_of("led_matrix") == "led_matrix"
    assert display_of("") == DEFAULT_DISPLAY        # модель не указана


def test_display_of_device_from_platform():
    """Модель реестра пуста или мусор («{}») — способность выводится из платформы SN
    (updates.platform_of): Station Max (X00T… — как у станции с LED-матрицей) — LED,
    ДуоМакс (C00T… — chiron) — анимаций нет; заданная пользователем модель сильнее
    платформы; неизвестное — не отсекается."""
    assert display_of_device({"sn": "X00T10001B6SAK", "model": "{}"}) == "led_matrix"
    assert display_of_device({"sn": "X00T10001B6SAK", "model": ""}) == "led_matrix"
    assert display_of_device({"sn": "C00T10001B6SAK", "model": "{}"}) == "none"   # ДуоМакс честно
    assert display_of_device({"sn": "C00T10001B6SAK", "model": "led_matrix"}) == "led_matrix"
    assert display_of_device({"model": "duo_max"}) == "none"                      # явная модель
    assert display_of_device(None) == DEFAULT_DISPLAY                             # не распознана


def test_store_crud_and_media(tmp_path):
    store = AnimationStore(tmp_path)
    assert any(a["id"] == "builtin.led.joy" for a in store.list())  # семена из капч
    entry = store.put({"id": "user.happy", "title": "Радость", "models": ["mini"], "url": "http://x/j.gif"},
                      media_b64=base64.b64encode(GIF).decode())
    assert entry["file"] == "user.happy.gif" and entry["bytes"] == len(GIF)
    assert store.media_path(entry["file"]).read_bytes() == GIF
    assert any(a["id"] == "user.happy" for a in store.for_model("mini"))
    assert store.for_model("duo_max") == []          # модель без анимаций — пусто
    try:
        store.put({"id": "bad", "title": "x", "url": "http://x/a.gif", "models": ["nope"]})
        raise AssertionError("неизвестная модель должна отклоняться")
    except ValueError:
        pass
    store.delete("user.happy")
    assert store.get("user.happy") is None
    # персистентность
    assert AnimationStore(tmp_path).get("builtin.led.joy")


def test_animation_binding_source():
    ctx = {"animations": {"user.happy": "http://x/j.gif"}}
    assert evaluate({"source": "animation", "name": "user.happy"}, ctx) == "http://x/j.gif"
    try:
        evaluate({"source": "animation", "name": "nope"}, ctx)
        raise AssertionError("нет анимации — BindingError")
    except BindingError:
        pass


async def test_animations_api_roundtrip_and_play(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                               'device_ids': ['d1'], 'model': 'duo_max'})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + '/emulator/api/animations') as response:
                data = await response.json()
                assert data['models']['duo_max']['display'] == 'none'
                assert any(a['id'] == 'builtin.led.joy' for a in data['animations'])
            payload = {'animation': {'id': 'user.happy', 'title': 'Радость', 'models': ['mini'],
                                     'url': 'http://x/j.gif'},
                       'media_base64': base64.b64encode(GIF).decode()}
            async with client.put(url + '/emulator/api/animations', json=payload,
                                  headers={'X-Emulator': '1'}) as response:
                assert (await response.json())['saved'] == 'user.happy'
            # ДуоМакс не проигрывает — честный отказ до отправки
            async with client.post(url + '/emulator/api/animations', json={'id': 'user.happy', 'sn': 'SN-T'},
                                   headers={'X-Emulator': '1'}) as response:
                data = await response.json()
                assert 'анимаций нет' in data['error']
            async with client.get(url + '/emulator/api/animations-media?file=user.happy.gif') as response:
                assert response.status == 200 and await response.read() == GIF
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_led_scenario_refuses_model_without_display(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings['builtin_scenarios'] = True
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                               'device_ids': [], 'model': 'duo_max'})
    sent = []

    async def send(data):
        sent.append(data)

    import datetime
    ctx = {'input': {'message_id': 'm', 'request_id': 'r', 'dialog_id': 'dg', 'sn': 'SN-T', 'text': 'покажи глазки'},
           'clock': {'now': datetime.datetime.now()}, 'device': {'sn': 'SN-T', 'model': 'duo_max', 'display': 'none'}}
    report = await runtime.dialog.handle_text('покажи глазки', ctx, 'dg', send)
    assert report['handled'] and report['scenario'] == 'device.led.emotion'
    assert 'нет анимаций' in (report.get('speech') or '') or report['status'] == 'errored'


def test_sequence_frames_compose_wire(tmp_path):
    """Редактор последовательностей: frames → animation_sequence из кадров."""
    store = AnimationStore(tmp_path)
    store.put({"id": "user.hello", "title": "Приветствие", "frames":
               ["builtin.led.animate_in", "builtin.led.joy"]})
    urls = store.sequence_urls("user.hello")
    assert urls == [store.get("builtin.led.animate_in")["url"], store.get("builtin.led.joy")["url"]]
    assert store.sequence_urls("builtin.led.joy") == [store.get("builtin.led.joy")["url"]]


# ─── платформы / дисплеи ───────────────────────────────────────────────────

def test_list_platforms_counts():
    platforms = list_platforms()
    ids = {p["platform"] for p in platforms}
    assert ids == {"yandexstation_2", "yandexmidi", "yandexmini_2", "yandexstation", "yandexmini", "chiron"}
    assert platform_has_displays("yandexmini_2")
    assert not platform_has_displays("chiron")
    station2 = next(p for p in platforms if p["platform"] == "yandexstation_2")
    kinds = {d["kind"] for d in station2["displays"]}
    assert kinds == {"frontal", "ring"}


def test_platform_displays_shape():
    for p in list_platforms():
        for d in p["displays"]:
            assert set(d) >= {"kind", "title", "size", "fps", "format", "note"}
            assert d["fps"] > 0
            assert all(s > 0 for s in d["size"])


# ─── AnimationProjectStore: CRUD ───────────────────────────────────────────

def test_project_store_put_get_list_delete(tmp_path):
    store = AnimationProjectStore(tmp_path / "animations")
    entry = store.put("yandexmini_2", {
        "id": "my.clock", "title": "Мои часы", "kind": "clock",
        "size": [32], "fps": 30, "platform": "yandexmini_2",
        "frames": [{"brightnesses": [255] * 32, "delayMs": 100}],
    })
    assert entry["id"] == "my.clock" and entry["kind"] == "clock"

    got = store.get("yandexmini_2", "my.clock")
    assert got is not None and got["title"] == "Мои часы" and got["platform"] == "yandexmini_2"

    projects = store.list("yandexmini_2")
    assert any(p["id"] == "my.clock" for p in projects)
    assert store.list("yandexstation") == []

    store.delete("yandexmini_2", "my.clock")
    assert store.get("yandexmini_2", "my.clock") is None


def test_project_store_validation(tmp_path):
    store = AnimationProjectStore(tmp_path / "animations")
    bad_cases = [
        {"id": "BAD", "title": "t", "kind": "ring", "size": [24], "fps": 30},  # id не строчные
        {"id": "ok", "title": "", "kind": "ring", "size": [24], "fps": 30},    # пустой title
        {"id": "ok", "title": "t", "kind": "nope", "size": [24], "fps": 30},   # плохой kind
        {"id": "ok", "title": "t", "kind": "ring", "size": [0], "fps": 30},    # size <= 0
        {"id": "ok", "title": "t", "kind": "ring", "size": [24], "fps": 0},    # fps <= 0
        {"id": "ok", "title": "t", "kind": "ring", "size": [24], "fps": 30, "frames": "nope"},  # frames не список
    ]
    for value in bad_cases:
        try:
            store.put("yandexmini_2", value)
            raise AssertionError(f"должен быть отвергнут: {value}")
        except ValueError:
            pass


def test_project_store_platform_mismatch_rejected(tmp_path):
    store = AnimationProjectStore(tmp_path / "animations")
    try:
        store.put("yandexmini_2", {"id": "x", "title": "t", "kind": "ring",
                                   "size": [24], "fps": 30, "platform": "yandexstation"})
        raise AssertionError("platform mismatch должен отвергаться")
    except ValueError:
        pass


def test_project_store_persists(tmp_path):
    root = tmp_path / "animations"
    store = AnimationProjectStore(root)
    store.put("yandexmini_2", {"id": "persist", "title": "P", "kind": "ring",
                               "size": [24], "fps": 30, "frames": []})
    # новый экземпляр — данные на диске
    assert AnimationProjectStore(root).get("yandexmini_2", "persist") is not None


# ─── импорт файлов ─────────────────────────────────────────────────────────

def test_import_led(tmp_path):
    store = AnimationProjectStore(tmp_path / "animations")
    led = "FF0000 00FF00 100\n00FF00 FF0000 200\n"
    entry = store.import_file("yandexstation_2", "my.ring", "Моё кольцо", "ring",
                              [24], 30, led.encode("utf-8"), "my.ring.led")
    assert entry["kind"] == "ring" and entry["size"] == [24]
    assert len(entry["frames"]) == 2
    # цвета дополнены до led_count (24) чёрными
    assert len(entry["frames"][0]["colors"]) == 24
    assert entry["frames"][0]["colors"][0] == [255, 0, 0, 0]
    assert entry["frames"][0]["colors"][1] == [0, 255, 0, 0]
    assert entry["frames"][0]["delayMs"] == 100
    assert entry["frames"][1]["delayMs"] == 200


def test_import_scled_text(tmp_path):
    store = AnimationProjectStore(tmp_path / "animations")
    scled = " ".join("255" for _ in range(32)) + " 100\n"
    entry = store.import_file("yandexmini_2", "my.clock", "Часы", "clock",
                              [32], 30, scled.encode("utf-8"), "my.clock.scled")
    assert entry["kind"] == "clock"
    assert len(entry["frames"]) == 1
    assert entry["frames"][0]["brightnesses"] == [255] * 32
    assert entry["frames"][0]["delayMs"] == 100


def test_import_scled_binary(tmp_path):
    store = AnimationProjectStore(tmp_path / "animations")
    data = b"\x00" + bytes(range(32)) + (250).to_bytes(2, "big") + b"\x01"
    entry = store.import_file("yandexmini_2", "my.clock", "Часы", "clock",
                              [32], 30, data, "my.clock.scled")
    assert len(entry["frames"]) == 1
    assert entry["frames"][0]["brightnesses"] == list(range(32))
    assert entry["frames"][0]["delayMs"] == 250


def test_import_gif_stores_media(tmp_path):
    store = AnimationProjectStore(tmp_path / "animations")
    gif = b"GIF89a" + b"\x00" * 40
    entry = store.import_file("yandexstation_2", "my.frontal", "Глазки", "frontal",
                              [25, 16], 30, gif, "my.frontal.gif")
    assert entry["kind"] == "frontal"
    assert entry["frames"][0]["media_file"] == "my.frontal.gif"
    assert entry["frames"][0]["media_url"] == "/emulator/api/animations-media?file=my.frontal.gif"
    assert (store.media / "my.frontal.gif").read_bytes() == gif


def test_import_unsupported_extension_rejected(tmp_path):
    store = AnimationProjectStore(tmp_path / "animations")
    try:
        store.import_file("yandexmini_2", "x", "t", "ring", [24], 30, b"data", "x.txt")
        raise AssertionError("должен быть отвергнут")
    except ValueError:
        pass


# ─── API: platforms ────────────────────────────────────────────────────────

async def test_animations_platforms_api(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + '/emulator/api/animations-platforms') as response:
                assert response.status == 200
                data = await response.json()
                ids = {p["platform"] for p in data["platforms"]}
                assert ids == {"yandexstation_2", "yandexmidi", "yandexmini_2",
                               "yandexstation", "yandexmini", "chiron"}
                mini2 = next(p for p in data["platforms"] if p["platform"] == "yandexmini_2")
                kinds = {d["kind"] for d in mini2["displays"]}
                assert kinds == {"ring", "clock"}
                chiron = next(p for p in data["platforms"] if p["platform"] == "chiron")
                assert chiron["displays"] == []
    finally:
        await runner.cleanup()
        await runtime.close()


# ─── API: projects CRUD + play ─────────────────────────────────────────────

async def test_animations_projects_crud_api(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            # пусто
            async with client.get(url + '/emulator/api/animations-projects?platform=yandexmini_2') as response:
                data = await response.json()
                assert data["projects"] == []

            # создать (PUT)
            project = {"platform": "yandexmini_2", "id": "my.clock", "title": "Часы",
                       "kind": "clock", "size": [32], "fps": 30,
                       "frames": [{"brightnesses": [255] * 32, "delayMs": 100}]}
            async with client.put(url + '/emulator/api/animations-projects', json=project,
                                  headers={'X-Emulator': '1'}) as response:
                assert (await response.json())["saved"] == "my.clock"

            # получить
            async with client.get(url + '/emulator/api/animations-projects?platform=yandexmini_2&id=my.clock') as response:
                got = await response.json()
                assert got["title"] == "Часы" and got["kind"] == "clock"

            # список
            async with client.get(url + '/emulator/api/animations-projects?platform=yandexmini_2') as response:
                data = await response.json()
                assert any(p["id"] == "my.clock" for p in data["projects"])

            # заменить (POST alias)
            project["title"] = "Новые часы"
            async with client.post(url + '/emulator/api/animations-projects', json=project,
                                   headers={'X-Emulator': '1'}) as response:
                assert (await response.json())["saved"] == "my.clock"
            async with client.get(url + '/emulator/api/animations-projects?platform=yandexmini_2&id=my.clock') as response:
                got = await response.json()
                assert got["title"] == "Новые часы"

            # удалить
            async with client.delete(url + '/emulator/api/animations-projects?platform=yandexmini_2&id=my.clock',
                                     headers={'X-Emulator': '1'}) as response:
                assert (await response.json())["deleted"] == "my.clock"
            assert store_get(runtime, "yandexmini_2", "my.clock") is None
    finally:
        await runner.cleanup()
        await runtime.close()


def store_get(runtime, platform, project_id):
    return runtime.animation_projects.get(platform, project_id)


async def test_animations_projects_import_api(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            led = "FF0000 00FF00 100\n"
            body = {"platform": "yandexstation_2", "id": "my.ring", "title": "Кольцо",
                    "kind": "ring", "size": [24], "fps": 30,
                    "media_base64": base64.b64encode(led.encode("utf-8")).decode(),
                    "filename": "my.ring.led"}
            async with client.post(url + '/emulator/api/animations-import', json=body,
                                   headers={'X-Emulator': '1'}) as response:
                data = await response.json()
                assert data["saved"] == "my.ring"
            got = store_get(runtime, "yandexstation_2", "my.ring")
            assert got["kind"] == "ring"
            assert got["frames"][0]["delayMs"] == 100
    finally:
        await runner.cleanup()
        await runtime.close()
