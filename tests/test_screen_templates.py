"""Реестр шаблонов экранных директив (screen_templates) и домен «Шаблоны»:
встроенные из app_cards, переопределения в state, подстановки {{имя}}/<uuid>,
валидация, сборка директив сценариями, API (список/чтение/сохранение/сброс/отправка).
Контракт — docs/contracts/templates.md."""
import hashlib
import json
import random
from pathlib import Path

import pytest

from aiohttp import ClientSession

from server_emulator import screen_templates
from server_emulator.runtime import Runtime
from server_emulator.screen_templates import Registry, TemplateError
from server_emulator.server import create_app
from tests.conftest import listen

SHOW_VIEW_CARD = {"Id": {"CardName": "youtube", "CardId": "youtube_key_static_key"},
                  "StringBody": {"templates": {},
                                 "card": {"log_id": "p", "states": [{"state_id": 0}]}}}


@pytest.fixture
def registry(tmp_path):
    return Registry(state_dir=tmp_path / "screen_templates")


def test_builtin_templates_from_app_cards(registry):
    """Встроенные шаблоны — образцы настоящего сервера; источник builtin."""
    entries = {t["id"]: t for t in registry.list()}
    assert {"youtube", "clock_refusal", "player_pause_animation"} <= set(entries)
    assert all(entries[i]["source"] == "builtin" for i in entries)
    assert entries["youtube"]["name"] == "divui_show_view"
    assert entries["clock_refusal"]["analytics_type"] == "default_div_text_card"
    assert entries["player_pause_animation"]["name"] == "draw_animation_directive"


def test_override_stronger_than_builtin_and_reset(registry):
    body = {"Name": "divui_show_view", "Div2Card": dict(SHOW_VIEW_CARD)}
    assert registry.save("youtube", body)["source"] == "override"
    assert registry.read("youtube")["body"] == body
    assert registry.read("youtube")["source"] == "override"
    registry.reset("youtube")
    assert registry.read("youtube")["source"] == "builtin"


def test_custom_template_and_reset_deletes_it(registry):
    body = {"Name": "draw_animation_directive",
            "Animations": [{"S3Directory": {"Bucket": "https://quasar.s3.yandex.net",
                                            "Path": "animations/player_like"}}]}
    assert registry.save("player_like", body, title="Своё")["source"] == "custom"
    assert registry.read("player_like")["source"] == "custom"
    registry.reset("player_like")
    assert registry.read("player_like") is None


def test_validation_errors_carry_json_path(registry):
    with pytest.raises(TemplateError, match="Name"):
        registry.validate({})
    with pytest.raises(TemplateError, match="Div2Card"):
        registry.validate({"Name": "divui_show_view"})
    with pytest.raises(TemplateError, match="Div2Card.Id"):
        registry.validate({"Name": "divui_show_view", "Div2Card": {"StringBody": SHOW_VIEW_CARD["StringBody"]}})
    with pytest.raises(TemplateError, match="StringBody.card.log_id"):
        registry.validate({"Name": "divui_show_view",
                           "Div2Card": {"Id": SHOW_VIEW_CARD["Id"],
                                        "StringBody": {"templates": {}, "card": {"states": [1]}}}})
    with pytest.raises(TemplateError, match="Animations"):
        registry.validate({"Name": "draw_animation_directive"})
    with pytest.raises(TemplateError, match="S3Directory.Bucket"):
        registry.validate({"Name": "draw_animation_directive",
                           "Animations": [{"S3Directory": {"Bucket": "no-scheme", "Path": "x"}}]})


def test_build_substitutions_uuid_and_compact_string_body(registry):
    random.seed(7)
    first = registry.build("youtube")
    second = registry.build("youtube")
    fields = first["fields"]
    assert first["name"] == "divui_show_view"
    assert first["proto"] == "NAlice.TLayeredDivUICapability.TDivUIShowViewDirective"
    assert first["analytics_type"] == "divui_show_view"
    string_body = fields["Div2Card"]["StringBody"]
    assert isinstance(string_body, str) and '"log_id":"' in string_body  # компактный провод
    assert "<uuid>" not in string_body
    assert first["fields"] != second["fields"]  # <uuid> — свежий на каждую сборку


def test_build_requires_known_variables(registry):
    body = {"Name": "draw_animation_directive",
            "Animations": [{"S3Directory": {"Bucket": "{{bucket}}", "Path": "{{path}}"}}]}
    registry.save("templated", body)
    assert registry.read("templated")["variables"] == ["bucket", "path"]
    built = registry.build("templated", {"bucket": "https://quasar.s3.yandex.net",
                                         "path": "animations/player_pause"})
    directory = built["fields"]["Animations"][0]["S3Directory"]
    assert directory == {"Bucket": "https://quasar.s3.yandex.net", "Path": "animations/player_pause"}
    with pytest.raises(TemplateError, match="bucket"):
        registry.build("templated", {"path": "x"})


def test_scenario_directives_come_from_registry(tmp_path):
    """Сценарные сборщики берут шаблоны из реестра: правка встроенного меняет ответ."""
    from server_emulator import app_directives, media_directives, screen_templates
    state = tmp_path / "st"
    screen_templates.configure(state)
    try:
        registry = screen_templates.registry()
        body = {"Name": "divui_show_view", "Div2Card": dict(SHOW_VIEW_CARD)}
        registry.save("youtube", body)
        built = app_directives.youtube_launch_directive()
        assert built["fields"]["Div2Card"]["StringBody"] == \
            json.dumps(body["Div2Card"]["StringBody"], ensure_ascii=False, separators=(",", ":"))  # компактный провод
        assert built["name"] == "divui_show_view"
        registry.reset("youtube")
        registry.save("clock_refusal", {"Name": "default_div_text_card", "Div2Card": dict(SHOW_VIEW_CARD)})
        assert app_directives.not_here_directive()["analytics_type"] == "default_div_text_card"
        registry.reset("clock_refusal")
        pause = media_directives.draw_player_pause()
        assert pause["fields"]["Animations"][0]["S3Directory"] == \
            {"Bucket": "https://quasar.s3.yandex.net", "Path": "animations/player_pause"}
        assert pause["analytics_type"] == "draw_animation_directive"
    finally:
        screen_templates.configure(None)  # singleton общий — тесты не должны наследовать state


def test_vendored_divkit_matches_contract():
    """Vendor-бандл DivKit: версия и sha256 зафиксированы в контракте."""
    path = Path(__file__).resolve().parent.parent / "server_emulator" / "ui" / "vendor" / "divkit.min.js"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == "98c444b1ba62c48082246ca9b31d0c94a5bbf74052fcc78e7531d7a8f3370fd0"
    contract = (Path(__file__).resolve().parent.parent / "docs" / "contracts" / "templates.md").read_text(encoding="utf-8")
    assert "33.11.0" in contract and digest in contract and "Apache-2.0" in contract


def test_templates_tab_wired():
    """Вкладка «Шаблоны»: секция и скрипт в index.html, файл в ASSETS, вкладка в GROUPS."""
    root = Path(__file__).resolve().parent.parent
    index = (root / "server_emulator" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'id="templates"' in index and 'templates.js' in index
    runtime_src = (root / "server_emulator" / "runtime.py").read_text(encoding="utf-8")
    assert '"templates.js"' in runtime_src  # ASSETS
    app_js = (root / "server_emulator" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "['templates','Шаблоны']" in app_js


async def test_templates_api(tmp_path):
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/templates") as response:
                assert response.status == 200
                entries = {t["id"]: t for t in (await response.json())["templates"]}
                assert entries["youtube"]["source"] == "builtin"
            async with client.get(url + "/emulator/api/templates?id=missing") as response:
                assert response.status == 400
            body = {"Name": "divui_show_view", "Div2Card": dict(SHOW_VIEW_CARD)}
            async with client.put(url + "/emulator/api/templates", json={"id": "youtube", "body": body},
                                  headers={"X-Emulator": "1"}) as response:
                assert response.status == 200
                assert (await response.json())["source"] == "override"
            state = tmp_path / "screen_templates" / "youtube.json"
            assert json.loads(state.read_text(encoding="utf-8"))["body"] == body
            async with client.put(url + "/emulator/api/templates", json={"id": "youtube", "body": {"Name": "divui_show_view"}},
                                  headers={"X-Emulator": "1"}) as response:
                assert response.status == 400
                assert "Div2Card" in (await response.json())["error"]
            async with client.get(url + "/emulator/api/templates?id=youtube") as response:
                assert (await response.json())["source"] == "override"
            async with client.post(url + "/emulator/api/templates", json={"op": "reset", "id": "youtube"},
                                   headers={"X-Emulator": "1"}) as response:
                assert response.status == 200
            async with client.get(url + "/emulator/api/templates?id=youtube") as response:
                assert (await response.json())["source"] == "builtin"
            async with client.post(url + "/emulator/api/templates", json={"op": "send", "id": "youtube", "sn": "SN-X"},
                                   headers={"X-Emulator": "1"}) as response:
                assert response.status == 200
                assert (await response.json())["delivery"] == "no_session"  # живой сессии нет
            async with client.post(url + "/emulator/api/templates", json={"op": "send", "id": "youtube"},
                                   headers={"X-Emulator": "1"}) as response:
                assert response.status == 400
    finally:
        await runner.cleanup()


def test_runtime_configures_registry(tmp_path):
    """Каталог пользовательских шаблонов — screen_templates внутри каталога state."""
    runtime = Runtime(tmp_path)
    assert screen_templates.registry().state_dir == tmp_path / "screen_templates"
    assert screen_templates.registry().cards_dir == screen_templates.CARDS_DIR
