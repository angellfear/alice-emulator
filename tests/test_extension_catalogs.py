"""TASK-030: точки подключения каталогов — сценарии, эндпоинты, Quasar, списки hassil.

Расширение-папка (режим разработки, как в tests/test_extensions_manager.py и
tests/test_extension_actions.py) даёт слои: scenarios/ (JSON и .py — script_path
от каталога расширения), endpoints/ (+ .py обработчики), quasar/ (common и
платформы-патчи), lists/ (словари template-триггеров). Приоритет слоёв —
пользовательское > расширение > встроенное: пользовательская запись с тем же id
перекрывает, правка записи расширения создаёт пользовательскую копию, выключение
расширения убирает его вклад. API списков сценариев и эндпоинтов отдаёт origin.
"""
import base64
import datetime
import json

from aiohttp import ClientSession

from server_emulator.endpoint_scripts import ScriptRequest
from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.quasar_config import compose
from server_emulator.runtime import Runtime
from server_emulator.scenario_engine import library as scenario_library
from server_emulator.scenario_packages.validate import validate_scenario
from server_emulator.sdk import SDK_VERSION
from server_emulator.server import create_app
from tests.conftest import listen

API_HEADERS = {"X-Emulator": "1"}

BREW_PY = ("from server_emulator.sdk import say\n\n\n"
           "async def handle(ctx):\n    return say('Эспрессо готов')\n")

MOTD_PY = ("from server_emulator.endpoint_scripts import reply\n\n\n"
           "async def handle(request, emulator):\n    return reply({'motd': 'Кофе готов'})\n")


def manifest(ext_id="coffee"):
    return {"id": ext_id, "name": ext_id, "version": "1.0.0", "description": "тест",
            "author": "T", "license": "MIT",
            "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": f">={SDK_VERSION}"},
            "permissions": {}}


def coffee_files():
    """Содержимое расширения: сценарии (JSON, template, py), эндпоинты (+py),
    списки hassil и слои Quasar."""
    return {
        "scenarios/custom/coffee.make.json": json.dumps({
            "format_version": 2, "id": "coffee.make", "version": "1.0.0", "category": "custom",
            "triggers": [{"kind": "phrase", "phrases": ["варь кофе"]}],
            "flow": [{"id": "say", "op": "compose", "reply": {"text": "Варю кофе."}},
                     {"id": "done", "op": "finish"}],
        }, ensure_ascii=False),
        "scenarios/custom/coffee.drink.json": json.dumps({
            "format_version": 2, "id": "coffee.drink", "version": "1.0.0", "category": "custom",
            "triggers": [{"kind": "template", "sentences": ["сделай {drink}"]}],
            "flow": [{"id": "say", "op": "compose", "reply": {"text": "Готовлю: {{slots.drink}}"}},
                     {"id": "done", "op": "finish"}],
        }, ensure_ascii=False),
        "scenarios/custom/coffee.brew.json": json.dumps({
            "format_version": 2, "id": "coffee.brew", "version": "1.0.0", "category": "custom",
            "triggers": [{"kind": "phrase", "phrases": ["свари эспрессо"]}],
            "executor": {"kind": "script"},
        }, ensure_ascii=False),
        "scenarios/custom/coffee.brew.py": BREW_PY,
        "lists/drinks.json": json.dumps({"lists": {"drink": {"values": [
            {"in": "кофе", "out": "coffee"}, {"in": "чай", "out": "tea"}]}}}, ensure_ascii=False),
        "endpoints/coffee.status.json": json.dumps({
            "id": "coffee.status", "host": "coffee.test", "method": "GET", "path": "/status",
            "status": 200, "content_type": "application/json",
            "body_base64": base64.b64encode(json.dumps({"status": "ok"}).encode()).decode(),
        }, ensure_ascii=False),
        "endpoints/coffee.motd.json": json.dumps({
            "id": "coffee.motd", "host": "coffee.test", "method": "GET", "path": "/motd",
            "status": 200, "mode": "script", "content_type": "application/json", "body_base64": "",
        }, ensure_ascii=False),
        "endpoints/coffee.motd.py": MOTD_PY,
        "quasar/common.json": json.dumps({"config": {"extension_key": "coffee"}}, ensure_ascii=False),
        "quasar/platforms/coffee.json": json.dumps(
            {"config": {"device_config": {"drink": "latte"}}}, ensure_ascii=False),
    }


def make_extension(runtime, ext_id="coffee", files=None, enabled=True):
    """Расширение-папка (dev-режим): manifest + файлы, запись installed/enabled
    в settings; load_enabled() подключает (как в tests/test_extension_actions.py)."""
    root = runtime.directory / "ext" / ext_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "extension.json").write_text(json.dumps(manifest(ext_id), ensure_ascii=False),
                                         encoding="utf-8")
    for name, content in (files or coffee_files()).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    state = runtime.settings.setdefault("extensions", {})
    state.setdefault("installed", {})[ext_id] = {"version": "1.0.0", "dev": str(root)}
    state.setdefault("enabled", {})[ext_id] = enabled
    return root


async def say_to(runtime, text, sn="SN-S"):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dlg", "sn": sn, "text": text},
           "clock": {"now": datetime.datetime.now()}}
    ctx.update(runtime._scenario_context(sn=sn))
    return await runtime.dialog.handle_text(text, ctx, "dlg", None, deliver=False)


def user_scenario_copy(text="Кофе из копии"):
    return {
        "format_version": 2, "id": "coffee.make", "version": "1.0.0", "category": "custom",
        "triggers": [{"kind": "phrase", "phrases": ["варь кофе"]}],
        "flow": [{"id": "say", "op": "compose", "reply": {"text": text}},
                 {"id": "done", "op": "finish"}],
    }


# --- сценарии и эндпоинты расширения работают ---------------------------------------

async def test_extension_scenario_and_endpoints_work(tmp_path):
    runtime = Runtime(tmp_path)
    root = make_extension(runtime)
    runtime.extensions.load_enabled()
    try:
        report = await say_to(runtime, "варь кофе")
        assert report["handled"] and report["speech"] == "Варю кофе."
        in_catalog = next(s for s in runtime._scenarios() if s["id"] == "coffee.make")
        assert in_catalog["_origin"] == "extension:coffee"
        # py-сценарий: script_path — .py рядом с JSON в каталоге расширения, и он исполняется
        brew = next(s for s in runtime._scenarios() if s["id"] == "coffee.brew")
        assert runtime.scenario_executor.script_path(brew) == root / "scenarios" / "custom" / "coffee.brew.py"
        report = await say_to(runtime, "свари эспрессо")
        assert report["handled"] and report["speech"] == "Эспрессо готов"
        # эндпоинт расширения: find/serve; script-режим — .py от каталога расширения
        entry = runtime.endpoints.find("coffee.test", "GET", "/status")
        assert entry["id"] == "coffee.status" and entry["_origin"] == "extension:coffee"
        status, body, content_type = runtime.endpoints.serve(entry, {})
        assert (status, content_type) == (200, "application/json")
        assert json.loads(body) == {"status": "ok"}
        motd = runtime.endpoints.find("coffee.test", "GET", "/motd")
        assert runtime.endpoints.script_path(motd) == root / "endpoints" / "coffee.motd.py"
        assert runtime.endpoints.read_script("coffee.motd") == MOTD_PY
        served = await runtime.endpoint_scripts.run(
            runtime.endpoints.script_path(motd),
            ScriptRequest("GET", "coffee.test", "/motd", {}, {}, b""), runtime)
        assert served.body == {"motd": "Кофе готов"}
    finally:
        await runtime.close()


# --- приоритет пользовательского, копия при правке ------------------------------------

async def test_user_copy_overrides_extension_and_edit_creates_copy(tmp_path):
    runtime = Runtime(tmp_path)
    root = make_extension(runtime)
    runtime.extensions.load_enabled()
    try:
        # пользовательский сценарий с тем же id перекрывает сценарий расширения
        runtime.settings["scenarios"] = [user_scenario_copy()]
        report = await say_to(runtime, "варь кофе")
        assert report["speech"] == "Кофе из копии"
        # правка сценария расширения через API: копия в settings, файл расширения не тронут
        app_runner, url = await listen(create_app(runtime=runtime))
        try:
            async with ClientSession() as client:
                body = await (await client.put(url + "/emulator/api/scenarios", headers=API_HEADERS,
                                               json={"scenario": user_scenario_copy("Кофе по API")})).json()
                assert body == {"saved": "coffee.make"}
        finally:
            await app_runner.cleanup()
        assert json.loads((root / "scenarios/custom/coffee.make.json").read_text(encoding="utf-8")
                          )["flow"][0]["reply"]["text"] == "Варю кофе."
        # правка эндпоинта расширения: переключатель создаёт пользовательскую копию
        extension_file = root / "endpoints" / "coffee.status.json"
        original = extension_file.read_text(encoding="utf-8")
        item = runtime.endpoints.toggle("coffee.status", False)
        assert item["enabled"] is False
        user_file = runtime.directory / "endpoints" / "other" / "coffee.status.json"
        assert user_file.is_file()
        assert extension_file.read_text(encoding="utf-8") == original
        assert runtime.endpoints.find("coffee.test", "GET", "/status")["enabled"] is False
        runtime.endpoints.remove("coffee.status")  # копия удалена — слой расширения вернулся
        runtime.endpoints.reload()
        assert runtime.endpoints.find("coffee.test", "GET", "/status")["enabled"] is True
        # upsert пользовательской копии с тем же id: заменяет запись расширения в каталоге
        runtime.endpoints.upsert({"id": "coffee.status", "host": "coffee.test", "method": "GET",
                                  "path": "/status", "status": 200, "content_type": "application/json",
                                  "body_base64": base64.b64encode(b'{"status": "user"}').decode()})
        entry = runtime.endpoints.find("coffee.test", "GET", "/status")
        assert "_origin" not in entry  # пользовательская копия без метки расширения
        _status, body, _type = runtime.endpoints.serve(entry, {})
        assert json.loads(body) == {"status": "user"}
        assert extension_file.read_text(encoding="utf-8") == original
    finally:
        await runtime.close()


async def test_endpoint_script_edit_copies_handler_from_extension(tmp_path):
    runtime = Runtime(tmp_path)
    root = make_extension(runtime)
    runtime.extensions.load_enabled()
    try:
        runtime.endpoints.upsert({"id": "coffee.motd", "host": "coffee.test", "method": "GET",
                                  "path": "/motd", "status": 200, "mode": "script",
                                  "content_type": "application/json", "body_base64": ""},
                                 script=None)
        user_script = runtime.endpoints.script_path(
            runtime.endpoints.find("coffee.test", "GET", "/motd"))
        assert user_script == runtime.directory / "endpoints" / "other" / "coffee.motd.py"
        assert user_script.read_text(encoding="utf-8") == MOTD_PY  # код переехал в копию
        assert (root / "endpoints" / "coffee.motd.py").read_text(encoding="utf-8") == MOTD_PY
        served = await runtime.endpoint_scripts.run(user_script,
                                                    ScriptRequest("GET", "coffee.test", "/motd", {}, {}, b""),
                                                    runtime)
        assert served.body == {"motd": "Кофе готов"}
    finally:
        await runtime.close()


# --- выключение расширения убирает его вклад ------------------------------------------

async def test_disable_extension_removes_scenarios_endpoints_and_phrases(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime)
    runtime.extensions.load_enabled()
    try:
        assert any(s["id"] == "coffee.make" for s in runtime._scenarios())
        assert "Варю кофе." in runtime._static_phrases()  # звуковой кеш видит фразы расширения
        await runtime.extensions.disable("coffee")
        assert not any(s["id"] == "coffee.make" for s in runtime._scenarios())
        assert not any(s["id"] == "coffee.make" for s in runtime._scenarios())
        assert runtime.endpoints.find("coffee.test", "GET", "/status") is None
        assert "Варю кофе." not in runtime._static_phrases()
    finally:
        await runtime.close()


# --- списки hassil из расширения -------------------------------------------------------

async def test_extension_list_used_by_template_trigger(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime)
    runtime.extensions.load_enabled()
    try:
        report = await say_to(runtime, "сделай чай")
        assert report["handled"] and report["scenario"] == "coffee.drink"
        assert report["speech"] == "Готовлю: tea"  # out-значение списка расширения
        report = await say_to(runtime, "сделай кофе")
        assert report["handled"] and report["speech"] == "Готовлю: coffee"
        # сценарий расширения валидируется со списком расширения (слои в check_template)
        extension, errors = scenario_library.extension_scenarios(runtime.extensions)
        assert not errors and validate_scenario(extension["coffee.drink"])["id"] == "coffee.drink"
    finally:
        await runtime.close()


# --- Quasar: common и платформы расширения ---------------------------------------------

async def test_extension_quasar_layers(tmp_path):
    runtime = Runtime(tmp_path)
    root = make_extension(runtime)
    runtime.extensions.load_enabled()
    try:
        store = runtime.quasar_platforms
        assert store.load_common()["config"]["extension_key"] == "coffee"
        assert store.load("coffee")["config"]["device_config"] == {"drink": "latte"}
        device = {"sn": "SN-Q", "device_ids": ["dev-q"], "profile": {"platform": "coffee"}}
        assert store.platform_for(device) == "coffee"
        common, patch = store.resolve_files(device)
        body = compose(common, patch, [], runtime, device)
        assert body["config"] == {"extension_key": "coffee", "device_config": {"drink": "latte"}}
        # пользовательский common поверх слоя расширения; правка платформы — копия
        store.save_common({"config": {"user_key": 1}})
        assert store.load_common()["config"] == {"extension_key": "coffee", "user_key": 1}
        store.save("coffee", {"config": {"device_config": {"drink": "ristretto"}}})
        assert store.load("coffee")["config"]["device_config"] == {"drink": "ristretto"}
        assert json.loads((root / "quasar/platforms/coffee.json").read_text(encoding="utf-8")
                          )["config"]["device_config"] == {"drink": "latte"}
        (runtime.directory / "quasar" / "platforms" / "coffee.json").unlink()
        assert store.load("coffee")["config"]["device_config"] == {"drink": "latte"}
    finally:
        await runtime.close()


# --- API списков отдаёт origin ----------------------------------------------------------

async def test_api_lists_report_origin(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime)
    runtime.extensions.load_enabled()
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            data = await (await client.get(url + "/emulator/api/scenarios",
                                           headers=API_HEADERS)).json()
            assert all(item["origin"] == "builtin" for item in data["builtin"])
            entry = next(item for item in data["extensions"] if item["id"] == "coffee.make")
            assert entry["origin"] == "extension:coffee" and entry["extension"] == "coffee"
            assert all(item.get("origin") == "user" for item in data["scenarios"])
            data = await (await client.get(url + "/emulator/api/endpoints",
                                           headers=API_HEADERS)).json()
            by_id = {item["id"]: item for item in data["endpoints"]}
            assert by_id["coffee.status"]["origin"] == "extension:coffee"
            assert by_id["coffee.motd"]["origin"] == "extension:coffee"
            assert by_id["coffee.motd"]["has_script"] is True
            # пользовательская копия получает origin user
            runtime.endpoints.toggle("coffee.status", False)
            data = await (await client.get(url + "/emulator/api/endpoints",
                                           headers=API_HEADERS)).json()
            by_id = {item["id"]: item for item in data["endpoints"]}
            assert by_id["coffee.status"]["origin"] == "user"
    finally:
        await app_runner.cleanup()
        await runtime.close()
