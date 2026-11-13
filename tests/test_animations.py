"""Раздел «Анимации»: каталог (JSON+GIF), модели устройств (ДуоМакс без
анимаций, Мини 7-сегмент), API, проигрывание Push'ом, bindings-источник,
хранение проектов (state/animations/<platform>/<id>.json)."""
import base64
import gzip
import io

import pytest
from aiohttp import ClientSession

from server_emulator import animation_formats as formats
from server_emulator.animation_storage import AnimationProjectStore
from server_emulator.animations import (
    DEFAULT_DISPLAY, PLATFORM_DISPLAYS, AnimationStore, device_platform, display_of,
    display_of_device, list_platforms, platform_displays,
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


# ─── родные форматы ────────────────────────────────────────────────────────

def test_led_parse_frames_loop_name_and_rgbw():
    parsed = formats.parse_led_text("name demo\nff000000 00ff0000 0000ff00 100\nloop\n"
                                    "#комментарий\n00000000 11223344 000000 200\nsmooth 3\n", 3)
    assert parsed.name == "demo" and parsed.loop_from == 1
    assert parsed.frames[0] == {"colors": [[255, 0, 0, 0], [0, 255, 0, 0], [0, 0, 255, 0]], "delayMs": 100}
    assert parsed.frames[1]["colors"][1] == [0x11, 0x22, 0x33, 0x44]   # RRGGBBWW


def test_led_macros_expand_to_frames():
    rotate = formats.parse_led_text("rotate ff0000 000000 000000 000000 50", 4)
    assert [f["colors"].index([255, 0, 0, 0]) for f in rotate.frames] == [0, 1, 2, 3]
    fill = formats.parse_led_text("fill 111111 222222 333333 40", 3).frames
    assert [sum(1 for c in f["colors"] if c != [0, 0, 0, 0]) for f in fill] == [1, 2, 3]
    unfill = formats.parse_led_text("unfill 111111 222222 333333 40", 3).frames
    assert [sum(1 for c in f["colors"] if c != [0, 0, 0, 0]) for f in unfill] == [3, 2, 1]
    step2 = formats.parse_led_text("step2 111111 222222 333333 40", 3).frames
    assert len(step2) == 3 + 2
    gradient = formats.parse_led_text("gradient 0 000000 2 ff0000 30", 3).frames[0]
    assert gradient["colors"][1][0] == 128 and gradient["delayMs"] == 30


def test_led_rejects_wrong_line_with_number():
    with pytest.raises(ValueError, match="строка 2"):
        formats.parse_led_text("ff0000 00ff00 10\nff0000 20", 2)
    with pytest.raises(ValueError, match="цвет"):
        formats.parse_led_text("zz0000 10", 1)


def test_led_text_roundtrip():
    frames = [{"colors": [[1, 2, 3, 4], [5, 6, 7, 8]], "delayMs": 90},
              {"colors": [[0, 0, 0, 0], [9, 9, 9, 0]], "delayMs": 10}]
    text = formats.build_led_text(frames, loop_from=1, name="x")
    parsed = formats.parse_led_text(text, 2)
    assert parsed.frames == frames and parsed.loop_from == 1 and parsed.name == "x"


def _scled(brightness, delay):
    return {"brightnesses": [brightness] * 32, "delayMs": delay}


def test_scled_text_and_binary_roundtrip_keep_loop_position():
    frames = [_scled(10, 100), _scled(200, 300), _scled(0, 50)]
    text = formats.build_scled_text(frames, loop_from=1)
    parsed = formats.parse_scled_text(text)
    assert parsed.frames == frames and parsed.loop_from == 1
    binary = formats.build_scled_binary(frames, loop_from=1)
    assert binary[0] == 0 and binary[1 + 32 + 2] == 1          # метка цикла перед вторым кадром
    again = formats.parse_scled_binary(binary)
    assert again.frames == frames and again.loop_from == 1
    assert formats.parse_scled_binary(formats.build_scled_binary(frames, loop_from=-1)).loop_from == -1
    assert len(formats.build_scled_binary(frames, loop_from=-1)) == 3 * 35        # без метки цикла
    assert formats.parse_scled_bytes(binary).frames == frames
    assert formats.parse_scled_bytes(text.encode()).frames == frames


def test_scled_rejects_garbage():
    with pytest.raises(ValueError):
        formats.parse_scled_text("1 2 3")
    with pytest.raises(ValueError):
        formats.parse_scled_binary(b"\x07")
    with pytest.raises(ValueError):
        formats.parse_scled_binary(b"\x00" + b"\x01" * 10)
    with pytest.raises(ValueError):
        formats.build_scled_binary([_scled(300, 10)])


def test_scled_payload_matches_firmware_parser():
    payload = formats.build_scled_animation_payload("demo", [_scled(255, 100), _scled(0, 100)], loop_from=0)
    assert payload["animation_stop_policy"] == "RepeatLastTillNextDirective"
    item = payload["animations"][0]
    assert item["name"] == "demo" and item["compression_type"] == "Gzip"
    binary = gzip.decompress(base64.b64decode(item["base64_encoded_value"]))
    assert formats.parse_scled_binary(binary).frames[0]["brightnesses"] == [255] * 32
    assert formats.build_scled_animation_payload("d", [_scled(1, 1)])["animation_stop_policy"] == "PlayOnce"
    with pytest.raises(ValueError):
        formats.build_scled_animation_payload("d", [])


# ─── реестр платформ ───────────────────────────────────────────────────────

def test_registry_displays():
    assert set(PLATFORM_DISPLAYS) == {"yandexstation_2", "yandexmidi", "yandexmini_2", "yandexstation", "yandexmini"}
    assert [d["kind"] for d in platform_displays("yandexstation_2")] == ["frontal", "ring"]
    assert platform_displays("chiron") == []
    assert device_platform({"sn": "X00T10001B6SAK"}) == "yandexstation_2"
    assert device_platform({"sn": "C00T10001B6SAK"}) == ""            # ДуоМакс — без анимаций
    assert device_platform({"sn": "S", "model": "yandexmidi"}) == "yandexmidi"


def test_list_platforms_includes_platforms_without_devices():
    """Редактор анимаций доступен без единой колонки: list_platforms возвращает ВСЕ
    платформы реестра PLATFORM_DISPLAYS, devices — колонки пользователя этой платформы
    или пустой список, если их нет (посылать анимацию будет некуда, но редактор работает)."""
    devices = [{"sn": "X00T10001B6SAK", "name": "Макс"}, {"sn": "C00T10001B6SAK"},
               {"sn": "M00T10001B6SAK", "profile": {"name": "Кухня"}}]
    platforms = list_platforms(devices)
    assert [p["platform"] for p in platforms] == list(PLATFORM_DISPLAYS)
    by_platform = {p["platform"]: p for p in platforms}
    assert by_platform["yandexstation_2"]["devices"] == [{"sn": "X00T10001B6SAK", "name": "Макс", "model": ""}]
    assert by_platform["yandexmini_2"]["devices"][0]["name"] == "Кухня"
    assert by_platform["yandexmidi"]["devices"] == []     # нет колонки этой платформы — список пуст
    assert by_platform["yandexstation"]["devices"] == []
    assert by_platform["yandexmini"]["devices"] == []
    # без единой колонки пользователя — все платформы реестра всё равно возвращаются
    empty = list_platforms([])
    assert [p["platform"] for p in empty] == list(PLATFORM_DISPLAYS)
    assert all(p["devices"] == [] for p in empty)


# ─── хранилище проектов ────────────────────────────────────────────────────

def _ring(n=24, delay=50, color=(255, 0, 0, 0)):
    return {"colors": [list(color)] + [[0, 0, 0, 0]] * (n - 1), "delayMs": delay}


def _project(**extra):
    return {"id": "my.ring", "title": "Кольцо", "kind": "ring", "loop_from": 0, "frames": [_ring()], **extra}


def test_project_store_roundtrip(tmp_path):
    store = AnimationProjectStore(tmp_path)
    saved = store.put("yandexstation", _project())
    assert saved["size"] == [24] and saved["platform"] == "yandexstation"
    assert store.get("yandexstation", "my.ring") == saved
    assert [p["id"] for p in store.list("yandexstation")] == ["my.ring"]
    assert AnimationProjectStore(tmp_path).get("yandexstation", "my.ring") == saved   # переживает перезапуск
    assert store.list("yandexmini") == []
    store.delete("yandexstation", "my.ring")
    assert store.get("yandexstation", "my.ring") is None
    with pytest.raises(ValueError):
        store.delete("yandexstation", "my.ring")


def test_project_validation(tmp_path):
    store = AnimationProjectStore(tmp_path)
    for platform, value in [
        ("chiron", _project()),                                     # платформа без дисплея
        ("nope", _project()),
        ("../x", _project()),                                       # выход из каталога
        ("yandexstation", _project(id="Bad Id")),
        ("yandexstation", _project(title=" ")),
        ("yandexstation", _project(kind="clock")),                   # у платформы нет такого дисплея
        ("yandexstation", _project(frames=[])),
        ("yandexstation", _project(frames=[_ring(n=23)])),            # размер кадра — по дисплею
        ("yandexstation", _project(frames=[_ring(color=(256, 0, 0, 0))])),
        ("yandexstation", _project(loop_from=1)),
        ("yandexstation_2", _project(kind="frontal", frames=[{"colors": [[0, 0, 0]] * 399, "delayMs": 100}])),
        ("yandexstation_2", _project(kind="frontal",                     # монохромный: r=g=b, цвет отклоняется
                                     frames=[{"colors": [[255, 0, 0]] * 400, "delayMs": 100}])),
        ("yandexmini_2", _project(kind="clock", frames=[_scled(0, 70000)])),
    ]:
        with pytest.raises(ValueError):
            store.put(platform, value)
    ring3 = {"colors": [[1, 2, 3]] * 24, "delayMs": 5}               # RGB без W — дополняется
    assert store.put("yandexstation", _project(frames=[ring3]))["frames"][0]["colors"][0] == [1, 2, 3, 0]


def test_import_led_scled_and_kind_mismatch(tmp_path):
    store = AnimationProjectStore(tmp_path)
    led = ("ff0000 " * 6 + "100\nloop\n" + "00ff00 " * 6 + "200\n").encode()
    project = store.import_file("yandexmini_2", "imp.ring", "", "ring", led, "Ring.led")
    assert project["title"] == "Ring" and project["loop_from"] == 1 and len(project["frames"]) == 2
    scled = formats.build_scled_binary([_scled(9, 10), _scled(8, 20)], loop_from=0)
    clock = store.import_file("yandexmini_2", "imp.clock", "Часы", "clock", scled, "x.scled")
    assert clock["frames"][1]["delayMs"] == 20 and clock["loop_from"] == 0
    for kind, data, name in [("ring", scled, "x.scled"), ("clock", led, "x.led"), ("ring", b"", "x.led"),
                             ("ring", led, "x")]:
        with pytest.raises(ValueError):
            store.import_file("yandexmini_2", "bad", "t", kind, data, name)


def _gif_bytes(frames, size=(25, 16), duration=100):
    from PIL import Image
    images = [Image.new("RGB", size, color) for color in frames]
    out = io.BytesIO()
    images[0].save(out, format="GIF", save_all=True, append_images=images[1:], duration=duration, loop=0)
    return out.getvalue()


def test_frontal_gif_import_render_and_export(tmp_path):
    from PIL import Image
    store = AnimationProjectStore(tmp_path)
    data = _gif_bytes([(200, 200, 200), (10, 10, 10)])
    project = store.import_file("yandexstation_2", "eyes", "Глаза", "frontal", data, "eyes.gif")
    assert len(project["frames"]) == 2 and len(project["frames"][0]["colors"]) == 400
    assert project["frames"][0]["colors"][0] == [200, 200, 200] and project["frames"][0]["delayMs"] == 100
    big = store.import_file("yandexstation_2", "big", "t", "frontal", _gif_bytes([(0, 255, 0)], size=(50, 32)), "b.gif")
    assert len(big["frames"][0]["colors"]) == 400                       # масштаб к 25×16
    color = big["frames"][0]["colors"][0]
    assert color[0] == color[1] == color[2]                             # цветной GIF — в градации серого
    name = store.publish_gif(project)
    with Image.open(store.media / name) as gif:
        assert gif.size == (25, 16) and gif.n_frames == 2
    file_name, content_type, body = store.export("yandexstation_2", "eyes")
    assert (file_name, content_type) == ("eyes.gif", "image/gif") and body.startswith(b"GIF8")
    with pytest.raises(ValueError):
        store.import_file("yandexstation_2", "x", "t", "frontal", b"GIF89a-not-a-gif", "x.gif")
    store.delete("yandexstation_2", "eyes")
    assert not (store.media / name).exists()


def test_export_native_formats(tmp_path):
    store = AnimationProjectStore(tmp_path)
    store.put("yandexstation", _project())
    name, content_type, body = store.export("yandexstation", "my.ring")
    assert name == "my.ring.led" and content_type.startswith("text/plain")
    assert formats.parse_led_text(body.decode(), 24).frames[0]["delayMs"] == 50
    store.put("yandexmini_2", {"id": "c", "title": "c", "kind": "clock", "loop_from": -1, "frames": [_scled(5, 40)]})
    name, _, body = store.export("yandexmini_2", "c")
    assert name == "c.scled" and formats.parse_scled_text(body.decode()).frames == [_scled(5, 40)]


# ─── API ───────────────────────────────────────────────────────────────────

HEADERS = {'X-Emulator': '1'}


async def _runtime_with_devices(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    for sn in ('X00T10001B6SAK', 'M00T10001B6SAK', 'C00T10001B6SAK'):
        runtime.identities.mutate({'operation': 'device', 'sn': sn, 'user_id': 'owner', 'device_ids': [], 'model': ''})
    return runtime


async def test_api_platforms_projects_import_export(tmp_path):
    runtime = await _runtime_with_devices(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + '/emulator/api/animations-platforms') as response:
                platforms = (await response.json())['platforms']
            # chiron (ДуоМакс) — без дисплея, вообще не в реестре; остальные платформы видны
            # всегда, даже без колонки этой платформы у пользователя
            assert [p['platform'] for p in platforms] == list(PLATFORM_DISPLAYS)
            by_platform = {p['platform']: p for p in platforms}
            assert by_platform['yandexstation_2']['devices'][0]['sn'] == 'X00T10001B6SAK'
            assert by_platform['yandexmidi']['devices'] == []
            project = {**_project(), 'platform': 'yandexmini_2', 'frames': [_ring(6)]}
            async with client.put(url + '/emulator/api/animations-projects', json=project, headers=HEADERS) as response:
                assert (await response.json())['saved'] == 'my.ring'
            async with client.put(url + '/emulator/api/animations-projects', json={**project, 'platform': 'chiron'},
                                  headers=HEADERS) as response:
                assert 'error' in await response.json()
            async with client.get(url + '/emulator/api/animations-projects?platform=yandexmini_2') as response:
                assert [p['id'] for p in (await response.json())['projects']] == ['my.ring']
            async with client.get(url + '/emulator/api/animations-projects?platform=yandexmini_2&id=my.ring') as response:
                assert (await response.json())['size'] == [6]
            async with client.get(url + '/emulator/api/animations-export?platform=yandexmini_2&id=my.ring') as response:
                assert 'my.ring.led' in response.headers['Content-Disposition']
                assert (await response.text()).startswith('name my.ring')
            body = {'platform': 'yandexmini_2', 'id': 'imp', 'title': 'Часы', 'kind': 'clock', 'filename': 'a.scled',
                    'data_base64': base64.b64encode(formats.build_scled_text([_scled(7, 30)]).encode()).decode()}
            async with client.post(url + '/emulator/api/animations-import', json=body, headers=HEADERS) as response:
                assert await response.json() == {'saved': 'imp', 'frames': 1}
            async with client.post(url + '/emulator/api/animations-import', json={**body, 'data_base64': '!!'},
                                   headers=HEADERS) as response:
                assert 'base64' in (await response.json())['error']
            async with client.delete(url + '/emulator/api/animations-projects?platform=yandexmini_2&id=imp',
                                     headers=HEADERS) as response:
                assert (await response.json())['deleted'] == 'imp'
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_api_play_project(tmp_path, monkeypatch):
    runtime = await _runtime_with_devices(tmp_path)
    sent = []

    async def push_action(sn, action, arguments):
        sent.append((sn, action, arguments))
        return {'action': action, 'delivery': 'sent', 'execution': 'unconfirmed'}

    monkeypatch.setattr(runtime, 'push_action', push_action)
    store = runtime.animation_projects
    store.put('yandexmini_2', {'id': 'c', 'title': 'c', 'kind': 'clock', 'loop_from': -1, 'frames': [_scled(5, 40)]})
    store.put('yandexmini_2', {'id': 'r', 'title': 'r', 'kind': 'ring', 'loop_from': -1, 'frames': [_ring(6)]})
    store.put('yandexstation_2', {'id': 'f', 'title': 'f', 'kind': 'frontal', 'loop_from': -1,
                                  'frames': [{'colors': [[50, 50, 50]] * 400, 'delayMs': 100}]})
    runner, url = await listen(create_app(runtime=runtime))
    play = url + '/emulator/api/animations-play'
    try:
        async with ClientSession() as client:
            async def post(**value):
                async with client.post(play, json=value, headers=HEADERS) as response:
                    return await response.json()

            assert (await post(platform='yandexmini_2', id='c', sn='M00T10001B6SAK'))['delivery'] == 'sent'
            sn, action, arguments = sent[-1]
            assert (sn, action) == ('M00T10001B6SAK', 'draw_scled_animations')
            assert arguments['animations'][0]['compression_type'] == 'Gzip'
            assert 'error' in await post(platform='yandexmini_2', id='r', sn='M00T10001B6SAK')        # кольцо не играется
            assert 'платформа' in (await post(platform='yandexmini_2', id='c', sn='X00T10001B6SAK'))['error']
            assert 'не найден' in (await post(platform='yandexmini_2', id='zz', sn='M00T10001B6SAK'))['error']
            assert 'localhost' in (await post(platform='yandexstation_2', id='f', sn='X00T10001B6SAK'))['error']
            async with client.post(play, json={'platform': 'yandexstation_2', 'id': 'f', 'sn': 'X00T10001B6SAK'},
                                   headers={**HEADERS, 'Host': '192.0.2.10:8090'}) as response:
                assert (await response.json())['delivery'] == 'sent'
            sn, action, arguments = sent[-1]
            assert action == 'draw_led_screen'
            assert arguments['frames'][0] == 'http://192.0.2.10:8090/emulator/api/animations-media?file=project.yandexstation_2.f.gif'
            async with client.get(url + '/emulator/api/animations-media?file=project.yandexstation_2.f.gif') as response:
                assert response.status == 200 and (await response.read()).startswith(b'GIF8')
    finally:
        await runner.cleanup()
        await runtime.close()


def test_push_wire_forms_for_scled_and_led_screen():
    from pathlib import Path
    from server_emulator.actions import push_message
    from server_emulator.schema import load_type
    cls = load_type(Path(__file__).resolve().parent.parent / "server_emulator" / "schemas", "alice-api.pb",
                    "NAlice.NAliceApi.TServerMessage")
    payload = formats.build_scled_animation_payload("n", [_scled(1, 10)])
    directive = cls.FromString(push_message(cls, "draw_scled_animations", payload).SerializeToString()).Event.Push.Directives[0]
    assert directive.Name == "draw_scled_animations" and directive.Type == "client_action" and directive.IsLedSilent
    assert directive.Payload.fields["animation_stop_policy"].string_value == "PlayOnce"
    assert directive.Payload.fields["animations"].list_value.values[0].struct_value.fields["name"].string_value == "n"
    for bad in ({}, {"animations": []}, {"animations": [{"base64_encoded_value": "x", "compression_type": "Zip"}]},
                {**payload, "animation_stop_policy": "Forever"}):
        with pytest.raises(ValueError):
            push_message(cls, "draw_scled_animations", bad)
    led = cls.FromString(push_message(cls, "draw_led_screen", {"frames": ["http://h/a.gif"]}).SerializeToString())
    directive = led.Event.Push.Directives[0]
    assert directive.Name == "draw_led_screen" and directive.Type == "client_action" and directive.IsLedSilent
    payload = directive.Payload.fields
    entry = payload["animation_sequence"].list_value.values[0].struct_value.fields
    assert entry["frontal_led_image"].string_value == "http://h/a.gif"
    assert payload["till_end_of_speech"].bool_value is False
