"""Свет умного дома встроенного плагина home-assistant (phrases/light.json +
scenarios/smart_home/light): список ламп — устройства Home Assistant (заглушка
tests/stub_ha.py, названия — площадка или friendly_name), падежные комнаты — общий
словарь room; тексты ядра сохранены («Свет (…) включён на 50%.», «Лампы … нет»,
«Не понял, какую лампу…»). Плагин не настроен или ламп нет — фраза неизвестная;
сбой HA при вызове — понятная фраза. Действие идёт в HA (entity_id), state.light
колонки не трогается."""
import contextlib
import datetime

import pytest  # noqa: F401
from aiohttp import ClientSession  # noqa: F401  (WS-хелперы conftest)

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import _open_session, listen
from tests.stub_ha import start as start_ha

EXT = "builtin.home-assistant"
UNAVAILABLE = ("Home Assistant недоступен — проверьте адрес и токен в настройках "
               "расширения «Home Assistant».")


async def ha_home(tmp_path, lights=("кухня", "спальня"), room=None, configure=True,
                  areas=None, url_override=None):
    """Runtime + локальная заглушка HA: настройки плагина, записи датасета
    (rooms/devices) записаны, справочник фраз пересобран — закрытый список готов."""
    runner, stub, base = await start_ha()
    stub.set_lights(lights)
    stub.areas.update(areas or {})
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({'operation': 'user', 'id': 'owner', 'name': 'O'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-T', 'user_id': 'owner',
                               'device_ids': ['dev-t'], 'profile': {'aapi_model': True}})
    if room:
        device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN-T')
        device['room'] = room
    if configure:
        settings = runtime.settings.setdefault("extensions", {})
        settings.setdefault("settings", {})[EXT] = {"url": url_override or base,
                                                    "token": "test-token"}
        runtime.extensions.load_enabled()
        await refresh_lights(runtime)
    runtime.stub_ha = stub  # доступ тестов к журналу вызовов
    return runtime, stub, runner


async def refresh_lights(runtime, base=None, token="test-token"):
    """Записи датасета ha.lights по настройкам плагина + пересборка справочника."""
    if base is not None:
        runtime.settings["extensions"]["settings"][EXT] = {"url": base, "token": token}
        runtime.extensions.load_enabled()
    for key in ("rooms", "devices"):
        await runtime.scenario_executor.refresh_dataset("ha.lights", key)
    runtime.rebuild_phrase_directory()


def service_calls(stub):
    return [c for c in stub.calls if c["kind"] == "service"]


async def _say(runtime, text):
    replies = []

    async def send(data):
        from server_emulator.schema import load_type
        cls = load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
        message = cls.FromString(data[4:])
        if message.Event.WhichOneof("Event") == "AliceResponse":
            replies.append(message.Event.AliceResponse.Response.Cards[0].Text)

    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": "SN-T", "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn="SN-T"))
    report = await runtime.dialog.handle_text(text, ctx, "dg", send)
    return report, replies[0] if replies else None


@pytest.mark.parametrize("phrase,entity,reply", [
    ("включи свет на кухне", "light.kitchen", "Свет (кухня) включён."),
    ("выключи свет в спальне", "light.bedroom", "Свет (спальня) выключен."),
    ("включи свет на кухне на 50 процентов", "light.kitchen", "Свет (кухня) включён на 50%."),
])
async def test_light_rooms_via_ha(tmp_path, phrase, entity, reply):
    """Комната из закрытого списка устройств (падеж — словарь room), сервис HA по
    entity_id; яркость — data.brightness_pct сервиса."""
    runtime, stub, runner = await ha_home(tmp_path)
    try:
        report, text = await _say(runtime, phrase)
        assert report["scenario"] == "smart_home.light" and text == reply
        assert len(service_calls(stub)) == 1
        call = service_calls(stub)[0]
        assert call["path"] == "/api/services/light/turn_on" or \
            call["path"] == "/api/services/light/turn_off"
        assert call["body"]["entity_id"] == entity
        assert call["auth"] == "Bearer test-token"
        if "50" in phrase:
            assert call["body"].get("brightness_pct") == 50
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_light_default_room_and_single(tmp_path):
    """Без названной комнаты: комната колонки (имена HA регистронезависимы, в ответе
    имя как у устройства); единственная лампа — когда комнаты колонки нет."""
    runtime, stub, runner = await ha_home(tmp_path, lights=("Кухня",), room="кухня")
    try:
        _, text = await _say(runtime, "зажги свет")
        assert text == "Свет (Кухня) включён."
        assert service_calls(stub)[-1]["body"]["entity_id"] == "light.kitchen"
    finally:
        await runner.cleanup()
        await runtime.close()

    runtime, stub, runner = await ha_home(tmp_path, lights=("спальня",))  # комната колонки не задана
    try:
        _, text = await _say(runtime, "включи свет")
        assert text == "Свет (спальня) включён."
        assert service_calls(stub)[-1]["body"]["entity_id"] == "light.bedroom"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_light_ambiguous_and_unknown(tmp_path):
    """Тексты ядра: не понял, какую лампу (без комнаты при двух), лампы «X» нет."""
    runtime, stub, runner = await ha_home(tmp_path)
    try:
        _, text = await _say(runtime, "включи свет")
        assert text == 'Не понял, какую лампу: доступны ["кухня", "спальня"].'
        _, text = await _say(runtime, "включи свет в ванной")
        assert text.startswith('Лампы «ванная» нет. Доступны: ["кухня", "спальня"]')
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_light_ha_unavailable(tmp_path):
    """Список есть (закрыт кешем датасета), а сервис HA недоступен — понятная фраза,
    не ошибка сценария."""
    runtime, stub, runner = await ha_home(tmp_path)
    try:
        runtime.settings["extensions"]["settings"][EXT] = {"url": "http://127.0.0.1:9",
                                                           "token": "test-token"}
        runtime.extensions.load_enabled()
        with contextlib.suppress(Exception):  # обновление списка не удалось — старая запись
            await runtime.scenario_executor.refresh_dataset("ha.lights", "rooms")
        runtime.rebuild_phrase_directory()
        report, text = await _say(runtime, "включи свет на кухне")
        assert report["scenario"] == "smart_home.light" and text == UNAVAILABLE
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_phrase_unknown_when_unconfigured(tmp_path):
    """Нет настроек (нет токена) или в HA нет ламп — файл фраз вне справочника:
    «включи свет» не обрабатывается, как у выключенного плагина."""
    runtime = Runtime(tmp_path)  # плагин включён, но не настроен
    try:
        assert all(ext != EXT for ext, _p, _k, _l in runtime.extensions.phrase_files())
        report, _ = await _say(runtime, "включи свет")
        assert report.get("scenario") != "smart_home.light"
    finally:
        await runtime.close()

    runtime, stub, runner = await ha_home(tmp_path, lights=())  # настроен, ламп нет
    try:
        assert all(ext != EXT for ext, _p, _k, _l in runtime.extensions.phrase_files())
        assert runtime.extensions.watched_phrase_lists() == [("ha.lights", "rooms")]
        report, _ = await _say(runtime, "включи свет")
        assert report.get("scenario") != "smart_home.light"
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_phrase_directory_has_light(tmp_path):
    """Настроенный плагин со списком ламп: запись «builtin.home-assistant.light»
    в справочнике, закрытый список — устройства HA (группа «Прочее» — вид пустой)."""
    runtime, stub, runner = await ha_home(tmp_path)
    try:
        entries = runtime.dialog.phrase_directory().entries
        assert "builtin.home-assistant.light" in entries
        lists = [l for e, _p, k, l in runtime.extensions.phrase_files() if e == EXT]
        assert lists and set(lists[0]["ha_room"]["values"]) == {"кухня", "спальня"}
        assert lists[0]["ha_room"]["via"] == "room"
        assert all(k == "" for e, _p, k, _l in runtime.extensions.phrase_files() if e == EXT)
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_area_name_wins_over_friendly(tmp_path):
    """Название устройства — площадка HA (area_name), нет — friendly_name; закрытый
    список и слоты — по названиям площадок (имя как в HA, падеж — словарь room)."""
    runner, stub, base = await start_ha()
    stub.states = [
        {"entity_id": "light.bedroom", "state": "off", "attributes": {"friendly_name": "Bedroom lamp"}},
        {"entity_id": "light.office", "state": "on", "attributes": {"friendly_name": "Study"}},
    ]
    stub.areas = {"light.bedroom": "Спальня", "light.office": "Гостиная"}
    runtime = Runtime(tmp_path)
    settings = runtime.settings.setdefault("extensions", {})
    settings.setdefault("settings", {})[EXT] = {"url": base, "token": "test-token"}
    runtime.extensions.load_enabled()
    try:
        for key in ("rooms", "devices"):
            await runtime.scenario_executor.refresh_dataset("ha.lights", key)
        runtime.rebuild_phrase_directory()
        _report, text = await _say(runtime, "выключи свет в спальне")
        assert text == "Свет (Спальня) выключен."
        call = service_calls(stub)[-1]
        assert (call["domain"], call["service"], call["body"]["entity_id"]) == (
            "light", "turn_off", "light.bedroom")
        _report, text = await _say(runtime, "включи свет в гостиной на 50 процентов")
        assert text == "Свет (Гостиная) включён на 50%."
        call = service_calls(stub)[-1]
        assert call["body"]["entity_id"] == "light.office" and call["body"].get("brightness_pct") == 50
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_duplicate_names_collapsed(tmp_path):
    """Одинаковые названия сущностей свёрнуты в одно (закрытый список без дублей),
    команда уходит на все лампы с этим названием."""
    runner, stub, base = await start_ha()
    stub.states = [
        {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Кухня"}},
        {"entity_id": "light.kitchen2", "state": "off", "attributes": {"friendly_name": "Кухня"}},
    ]
    runtime = Runtime(tmp_path)
    settings = runtime.settings.setdefault("extensions", {})
    settings.setdefault("settings", {})[EXT] = {"url": base, "token": "test-token"}
    runtime.extensions.load_enabled()
    try:
        for key in ("rooms", "devices"):
            await runtime.scenario_executor.refresh_dataset("ha.lights", key)
        runtime.rebuild_phrase_directory()
        lists = [l for e, _p, k, l in runtime.extensions.phrase_files() if e == EXT]
        assert lists[0]["ha_room"]["values"] == ["Кухня"]
        _report, text = await _say(runtime, "включи свет на кухонь")
        assert text == "Свет (Кухня) включён."
        assert service_calls(stub)[-1]["body"]["entity_id"] == ["light.kitchen", "light.kitchen2"]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_light_over_ws(tmp_path):
    """WS-путь как у прежнего ядра: «включи свет на кухне»/«выключи …» → ответ,
    сервис уходит в HA (заглушка), state.light колонки не меняется."""
    runtime, stub, runner_ = await ha_home(tmp_path, lights=("кухня",))
    runner, url = await listen(create_app(runtime=runtime))
    try:
        first, _, ws = await _open_session(runtime, url, "включи свет на кухне")
        assert first.Event.AliceResponse.Response.Cards[0].Text == "Свет (кухня) включён."
        assert service_calls(stub)[-1]["body"]["entity_id"] == "light.kitchen"
        device = next(d for d in runtime.identities.data['devices'] if d['sn'] == 'SN-T')
        assert not (device['state'].get('light') or {}).get('кухня', {}).get('on')
        second, _, _ = await _open_session(runtime, url, "выключи свет на кухне")
        assert second.Event.AliceResponse.Response.Cards[0].Text == "Свет (кухня) выключен."
        await ws.close()
    finally:
        await runner.cleanup()
        await runtime.close()
