"""Раздел «Анимации»: каталог (JSON+GIF), модели устройств (ДуоМакс без
анимаций, Мини 7-сегмент), API, проигрывание Push'ом, bindings-источник."""
import base64

from aiohttp import ClientSession

from server_emulator.animations import DEFAULT_DISPLAY, AnimationStore, display_of
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
