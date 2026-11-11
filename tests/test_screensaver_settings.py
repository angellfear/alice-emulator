"""Заставки главного экрана (gproxy): выбор коллекций хранится в состоянии устройства
и зеркалится в GET — как у настоящего сервера (наблюдение по .85: маркеры field 2
ответа совпали с флагами field 3 её save)."""
import json

from aiohttp import ClientSession

from server_emulator import screensaver_settings
from server_emulator.protowire import decode
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen


def teaser_save(*keys):
    """save_teaser_settings: обёртки с (3, 1) у включённых (наблюдённая форма)."""
    items = []
    for key in keys:
        pairs = [(1, key[0])] + ([(2, key[1])] if len(key) > 1 else [])
        items.append((1, [(1, pairs), (3, 1)]))
    from server_emulator.protowire import encode
    return encode([(1, items)])


def afisha_save(*names):
    from server_emulator.protowire import encode
    items = [(1, [(1, [(1, name)]), (2, 1)]) for name in names]
    return encode([(1, items)])


def item_marks(payload):
    """(ключ коллекции, есть ли маркер field 2) по элементам ответа GET."""
    out = []
    for n, v in decode(payload):
        if n != 1:
            continue
        for wrapper in v:
            key, marked = None, False
            for cn, cv in wrapper[1]:
                if cn == 1:
                    key = screensaver_settings._key((1, [(cn, cv)]))
                elif cn == 2 and cv == 1:
                    marked = True
            out.append((key, marked))
    return out


def register(runtime, sn="SN-SS", device_id="ss-id"):
    runtime.identities.mutate({"operation": "user", "id": "u", "name": "U"})
    runtime.identities.mutate({"operation": "device", "sn": sn, "user_id": "u",
                               "device_ids": [device_id]})


async def test_save_then_get_mirrors_selection(tmp_path):
    """Save сохраняет включённые коллекции, GET зеркалит их маркерами field 2;
    чужой ключ (внутри идентификатора) в выбор не попадает."""
    runtime = Runtime(tmp_path)
    register(runtime)
    try:
        body = teaser_save(("YaBroBackgrounds", "centaur_choice"), ("Afisha",))
        status, output, headers = screensaver_settings.answer(
            runtime, "SN-SS", "POST", "/gproxy/save_teaser_settings", body)
        assert status == 200 and output == b""
        assert headers["Content-Type"] == "application/x-protobuf"

        status, payload, _ = screensaver_settings.answer(
            runtime, "SN-SS", "POST", "/gproxy/get_teaser_settings", b"")
        marks = item_marks(payload)
        assert marks[0] == (("YaBroBackgrounds", "centaur_choice"), True)
        assert marks[2] == (("Afisha",), True)
        selected = [key for key, marked in marks if marked]
        assert len(selected) == 2
        # каталог без маркеров не менялся: остальных элементов 25
        assert len(marks) == 27
    finally:
        await runtime.close()


async def test_get_without_save_has_no_marks(tmp_path):
    """Без сохранённого выбора GET отдаёт каталог без маркеров (у статического
    ответа маркеры были чужим захватом .85)."""
    runtime = Runtime(tmp_path)
    register(runtime)
    try:
        _status, payload, _ = screensaver_settings.answer(
            runtime, "SN-SS", "POST", "/gproxy/get_teaser_settings", b"")
        assert all(not marked for _key, marked in item_marks(payload))
    finally:
        await runtime.close()


async def test_afisha_save_accepted_and_mirrored(tmp_path):
    """save_afisha_settings принимается (была 404), GET зеркалит включённые
    подкатегории; get_ya_disk_settings — пустое тело, как у настоящего сервера."""
    runtime = Runtime(tmp_path)
    register(runtime)
    try:
        status, output, _ = screensaver_settings.answer(
            runtime, "SN-SS", "POST", "/gproxy/save_afisha_settings",
            afisha_save("personal-top", "personal-concert-top-main"))
        assert status == 200 and output == b""
        _status, payload, _ = screensaver_settings.answer(
            runtime, "SN-SS", "POST", "/gproxy/get_afisha_settings", b"")
        assert item_marks(payload) == [(("personal-top",), True),
                                       (("personal-concert-top-main",), True)]
        status, output, _ = screensaver_settings.answer(
            runtime, "SN-SS", "POST", "/gproxy/get_ya_disk_settings", b"")
        assert status == 200 and output == b""
    finally:
        await runtime.close()


async def test_save_replaces_selection(tmp_path):
    """Повторный save замещает набор (станция шлёт полный список включённых)."""
    runtime = Runtime(tmp_path)
    register(runtime)
    try:
        screensaver_settings.answer(runtime, "SN-SS", "POST", "/gproxy/save_teaser_settings",
                                    teaser_save(("Afisha",), ("Tretyakovka",)))
        screensaver_settings.answer(runtime, "SN-SS", "POST", "/gproxy/save_teaser_settings",
                                    teaser_save(("YaDiskPhoto",)))
        _status, payload, _ = screensaver_settings.answer(
            runtime, "SN-SS", "POST", "/gproxy/get_teaser_settings", b"")
        selected = [key for key, marked in item_marks(payload) if marked]
        assert selected == [("YaDiskPhoto",)]
    finally:
        await runtime.close()


async def test_broken_save_keeps_state(tmp_path):
    """Битое тело save не затирает сохранённый выбор; ответ — как у настоящего сервера."""
    runtime = Runtime(tmp_path)
    register(runtime)
    try:
        screensaver_settings.answer(runtime, "SN-SS", "POST", "/gproxy/save_teaser_settings",
                                    teaser_save(("Afisha",)))
        status, output, _ = screensaver_settings.answer(
            runtime, "SN-SS", "POST", "/gproxy/save_teaser_settings", b"\xff\xff\xff")
        assert status == 200 and output == b""
        _status, payload, _ = screensaver_settings.answer(
            runtime, "SN-SS", "POST", "/gproxy/get_teaser_settings", b"")
        selected = [key for key, marked in item_marks(payload) if marked]
        assert selected == [("Afisha",)]
    finally:
        await runtime.close()


async def test_unknown_device_answers_without_state(tmp_path):
    """Неопознанное устройство: save отвечает 200 (некому хранить), GET — без маркеров."""
    runtime = Runtime(tmp_path)
    try:
        status, output, _ = screensaver_settings.answer(
            runtime, None, "POST", "/gproxy/save_teaser_settings",
            teaser_save(("Afisha",)))
        assert status == 200 and output == b""
        _status, payload, _ = screensaver_settings.answer(
            runtime, None, "POST", "/gproxy/get_teaser_settings", b"")
        assert all(not marked for _key, marked in item_marks(payload))
    finally:
        await runtime.close()


async def test_paths_served_before_endpoint_catalog(tmp_path):
    """Через HTTP: ветка работает до каталога эндпоинтов (stateful-маршрут), выбор
    одного устройства не виден другому, запись в журнал «Запись» ведётся как
    обработчик (не промах)."""
    runtime = Runtime(tmp_path)
    register(runtime, "SN-A", "a-id")
    runtime.identities.mutate({"operation": "device", "sn": "SN-B", "user_id": "u",
                               "device_ids": ["b-id"]})
    runtime.identities.mutate({"operation": "record", "sn": "SN-A", "mode": "on"})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/gproxy/save_teaser_settings?device_id=a-id",
                                   headers={"Host": "alica.lan"},
                                   data=teaser_save(("Afisha",))) as response:
                assert response.status == 200 and (await response.read()) == b""
            async with client.post(url + "/gproxy/get_teaser_settings?device_id=a-id",
                                   headers={"Host": "alica.lan"}, data=b"") as response:
                payload = await response.read()
                assert any(key == ("Afisha",) and marked for key, marked in item_marks(payload))
            async with client.post(url + "/gproxy/get_teaser_settings?device_id=b-id",
                                   headers={"Host": "alica.lan"}, data=b"") as response:
                assert all(not m for _k, m in item_marks(await response.read()))
        assert runtime.endpoints.misses() == {}
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_local_handler_counts_paths_as_handled(tmp_path):
    """«Запись»: пути заставок — есть обработчик (не «Без ответа»)."""
    runtime = Runtime(tmp_path)
    try:
        from server_emulator.dispatcher import Dispatcher
        dispatcher = Dispatcher(runtime)
        for path in ("/gproxy/get_teaser_settings", "/gproxy/save_teaser_settings",
                     "/gproxy/get_afisha_settings", "/gproxy/save_afisha_settings",
                     "/gproxy/get_ya_disk_settings"):
            assert dispatcher.local_handler("rpc.alice.yandex.net", "POST", path), path
    finally:
        await runtime.close()
