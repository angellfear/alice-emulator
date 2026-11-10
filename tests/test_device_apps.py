"""Ручной список установленных приложений колонки: карточка «Устройства →
Приложения» пишет device.state.apps через API device_apps (остальные ключи state
сохраняются); сценарий device.app.open находит приложение по имени, промпт агента и
режима «модель» получают компактные имена (intelligence.apps_context). «Какие
приложения установлены?» — ответ формулировкой записи .85; «открой ютуб» — тихий запуск
webview-картой по образцу записи (прочие приложения — честное «запуск не поддержан»).
Пустой список — прежний честный ответ «не найдено»."""
import json
from pathlib import Path
import json

import pytest

from server_emulator import intelligence
from server_emulator.runtime import Runtime
from tests.conftest import types as message_types


def apps_state(runtime, apps):
    """Список приложений на устройстве конфтест-сессии (SN-T/dev-t)."""
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                               'device_ids': ['dev-t'], 'name': 'Гостиная'})
    device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN-T')
    runtime.identities.mutate({'operation': 'state', 'sn': 'SN-T',
                               'state': dict(device['state'], apps=apps)})


def registry(runtime, sn="SN-A", apps=None):
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': sn, 'user_id': 'owner',
                               'device_ids': ['a1'], 'name': 'Гостиная'})
    state = dict(next(d for d in runtime.identities.data['devices'] if d['sn'] == sn)['state'])
    state['fetch_state_last'] = {'ts': 123.0}  # чужой ключ состояния — должен пережить правку списка
    runtime.identities.mutate({'operation': 'state', 'sn': sn, 'state': state})
    if apps is not None:
        runtime.identities.mutate({'operation': 'state', 'sn': sn, 'state': dict(state, apps=apps)})


async def test_device_apps_api_persists_and_preserves_state(tmp_path):
    from server_emulator.server import create_app
    from aiohttp import ClientSession
    from tests.conftest import _open_session, listen
    runtime = Runtime(tmp_path)
    registry(runtime, apps=["YouTube"])
    runner, url = await listen(create_app(runtime=runtime))
    EMU = {"X-Emulator": "1"}
    try:
        async with ClientSession() as client:
            async with client.put(url + "/emulator/api/device_apps",
                                  json={"sn": "SN-A", "apps": ["YouTube", "Спотифай"]}, headers=EMU) as r:
                assert r.status == 200
                assert (await r.json())["apps"] == ["YouTube", "Спотифай"]
        device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN-A')
        assert device['state']['apps'] == ["YouTube", "Спотифай"]
        assert device['state']['fetch_state_last'] == {'ts': 123.0}  # остальные ключи на месте
        # строки хранятся как есть; неверная форма — 400
        async with ClientSession() as client:
            async with client.put(url + "/emulator/api/device_apps",
                                  json={"sn": "SN-A", "apps": ["", {"name": "x"}]}, headers=EMU) as r:
                assert r.status == 400
            async with client.put(url + "/emulator/api/device_apps",
                                  json={"sn": "SN-НЕТ", "apps": ["x"]}, headers=EMU) as r:
                assert r.status == 400
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_app_open_finds_string_names(tmp_path):
    """Сценарий работает со строками (форма карточки) и {name}-словарями (старый вид)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "app_open", "server_emulator/scenarios/device/app.open.py")
    app_open = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_open)
    from server_emulator.sdk import ScenarioContext
    runtime = Runtime(tmp_path)
    registry(runtime, apps=["Спотифай", {"name": "YouTube", "package": "com.google.android.youtube"}])
    ctx = {"input": {"text": "открой спотифай", "sn": "SN-A", "message_id": "m", "request_id": "r",
                     "dialog_id": "d"},
           "clock": {"now": __import__('datetime').datetime.now()},
           "sequence": 1,
           "slots": {"app": "спотифай"}}
    ctx.update(runtime._scenario_context(sn="SN-A"))
    scenario = ScenarioContext(runtime, ctx)
    spec = await app_open.handle(scenario)
    assert "«Спотифай» найдено" in spec["text"]


async def test_apps_context_names_and_empty():
    runtime = Runtime(__import__('pathlib').Path(__import__('tempfile').mkdtemp()))
    try:
        registry(runtime, apps=["YouTube", "Спотифай"])
        text = intelligence.apps_context(runtime, "SN-A")
        assert text and "YouTube" in text and "Спотифай" in text
        assert intelligence.apps_context(runtime, "SN-НЕТ") is None  # нет устройства
        registry(runtime, apps=[])
        assert intelligence.apps_context(runtime, "SN-A") is None  # пустой список
    finally:
        runtime.store.close()


async def test_agent_prompt_carries_apps(tmp_path):
    runtime = Runtime(tmp_path)
    if runtime.agent is None:
        pytest.skip("LangChain не установлен")
    registry(runtime, apps=["YouTube", "Спотифай"])
    try:
        prompt = runtime.agent.system_prompt(runtime, {"sn": "SN-A"})
        assert "YouTube" in prompt and "Спотифай" in prompt
        assert "scenario_run" in prompt
    finally:
        await runtime.close()


async def test_legacy_model_prompt_carries_apps(tmp_path):
    """Режим «модель»: system-сообщение запроса содержит список приложений."""
    runtime = Runtime(tmp_path)
    registry(runtime, apps=["YouTube"])
    runtime.settings["model"]["stub_text"] = ""
    captured = []

    async def provider(kind, payload=None, **kwargs):
        captured.append(json.loads(json.dumps(payload)))
        return json.dumps({"choices": [{"message": {"content": "ок"}}], "usage": {}}).encode(), "application/json"
    runtime.provider = provider
    try:
        await runtime.provider_manager.chat(runtime, "привет", history=[], sn="SN-A")
        system = captured[0]["messages"][0]["content"]
        assert "YouTube" in system
    finally:
        await runtime.close()


# --- wire по записи .85 2026-11-10: список и запуск ютуба -------------------------------


async def test_apps_list_answer_from_state(tmp_path):
    """«какие приложения установлены?» — формулировка записи (126116-118) из ручного списка."""
    from tests.test_time_scenarios import _ask
    runtime = Runtime(tmp_path)
    apps_state(runtime, ["Ютуб", "Спотифай", "Кинопоиск"])
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "какие приложения установлены?")
        await ws.close()
        text = reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text
        assert text == "На колонке установлены Ютуб, Спотифай, Кинопоиск."
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_apps_list_empty_is_honest(tmp_path):
    """Пустой ручной список — честный ответ о состоянии, промаха нет."""
    from tests.test_time_scenarios import _ask
    runtime = Runtime(tmp_path)
    apps_state(runtime, [])
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "какие приложения установлены?")
        await ws.close()
        text = reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text
        assert text == "Список приложений колонки пока не заполнен."
        assert runtime.phrase_misses.public() == []  # фраза обслужена, не промах
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_app_open_launches_youtube_webview(tmp_path):
    """«открой ютуб» (приложение в списке) — тихий ответ с divui_show_view и webview-картой
    по образцу записи (126042): tts_play_placeholder + карта, речи нет; логId/request_id
    карты — один свежий uuid, константы формы совпадают с образцом."""
    import json as jsonlib
    from server_emulator import app_directives
    from server_emulator.schema import load_type, schemas_dir, station_file
    from tests.test_time_scenarios import _ask
    runtime = Runtime(tmp_path)
    apps_state(runtime, ["Ютуб", "Кинопоиск"])
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "открой ютуб")
        await ws.close()
        directives = reply.Event.AliceResponse.Response.Directives
        assert [d.Name for d in directives] == ["tts_play_placeholder", "divui_show_view"]
        assert reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text == ""  # тихий запуск
        view = load_type(schemas_dir(), station_file(),
                         "NAlice.TLayeredDivUICapability.TDivUIShowViewDirective")
        raw = directives[1].PayloadRaw
        assert raw, "payload_raw обязателен"
        msg = view.FromString(bytes(raw))
        assert msg.Name == "divui_show_view"
        assert msg.InactivityTimeout == 3  # Infinity
        assert msg.Div2Card.CardName == "YouTubeWebviewData"
        assert msg.Div2Card.Id.CardName == "youtube" and msg.Div2Card.Id.CardId == "youtube_key_static_key"
        body = jsonlib.loads(msg.Div2Card.StringBody)
        props = body["card"]["states"][0]["div"]["items"][0]["items"][0]["custom_props"]
        request_id = props["request_id"]
        assert request_id == body["card"]["log_id"] and request_id.count("-") == 4  # свежий uuid, как в записи
        assert props["url"] == "https://m.youtube.com/" and props["content_provider"] == "YOUTUBE"
        # константы формы — байт-в-байт с образцом (кроме подменённого uuid)
        resource = jsonlib.loads((Path(app_directives.CARDS_DIR) / "youtube.json").read_text(encoding="utf-8"))
        sample_body = resource["directive"]["Div2Card"]["StringBody"]  # в ресурсе — уже словарь
        sample_props = sample_body["card"]["states"][0]["div"]["items"][0]["items"][0]["custom_props"]
        sample_props["request_id"] = request_id
        sample_body["card"]["log_id"] = request_id
        assert body == sample_body
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_app_open_other_apps_stay_honest(tmp_path):
    """Приложение без образца запуска — прежняя честная карточка «найдено, запуск не поддержан»."""
    from tests.test_time_scenarios import _ask
    runtime = Runtime(tmp_path)
    apps_state(runtime, ["Кинопоиск"])
    runtime.settings.update(aapi_model=True, builtin_scenarios=True)

    async def chat(*args, **kwargs):
        raise AssertionError("модель не должна вызываться")

    runtime.chat = chat
    from server_emulator.server import create_app
    from tests.conftest import _open_session, listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        reply, _, ws = await _open_session(runtime, url, "открой кинопоиск")
        await ws.close()
        text = reply.Event.AliceResponse.VoiceResponse.OutputSpeech.Text
        assert "«Кинопоиск» найдено" in text and "не поддерживается" in text
    finally:
        await runner.cleanup()
        await runtime.close()
