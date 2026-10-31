"""TASK-034: режимы «Интеллекта» из расширений (modes/*.py).

Расширение-папка (режим разработки, как в tests/test_extensions_manager.py) с modes/*.py:
MODE + async answer(ctx, text, history) отвечает на фразу без сценария вместо модели.
Выключение/удаление расширения или неизвестный ext:<id> — откат на scenarios с записью
в recent; встроенные уровни не меняются (tests/test_intelligence.py).
"""
import json

from aiohttp import ClientSession

from server_emulator import intelligence
from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.runtime import Runtime
from server_emulator.sdk import SDK_VERSION
from server_emulator.server import create_app
from tests.conftest import listen

ECHO_MODE = '''"""Режим расширения «Эхо» (тест)."""
MODE = {"id": "echo", "title": "Эхо", "needs": ["network"],
        "tabs": [["echo", "Эхо"], {"id": "cfg", "title": "Настройки"}],
        "preset": {"voice": "echo", "model": "stub"}}


async def answer(ctx, text, history):
    return {"text": "Эхо: " + text, "tool_calls": [], "usage": {},
            "ctx_text": ctx.text, "ctx_sn": ctx.sn, "history": len(history)}
'''

SPOOF_MODE = '''"""Попытка выдать себя за встроенный уровень."""
MODE = {"id": "echo", "title": "Эхо"}


async def answer(ctx, text, history):
    return {"text": "x", "source": "scenarios"}
'''

FAILING_MODE = '''"""Режим с падающим answer."""
MODE = {"id": "echo", "title": "Эхо"}


async def answer(ctx, text, history):
    raise RuntimeError("boom")
'''

BROKEN_MODE = "MODE = {это не python\n"


def make_extension(runtime, ext_id="coffee", files=None, enabled=True):
    """Расширение-папка с modes/*.py, подключённое напрямую (режим разработки):
    manifest + запись installed/enabled в settings, load_enabled делает setup()."""
    root = runtime.directory / "ext" / ext_id
    (root / "modes").mkdir(parents=True)
    (root / "extension.json").write_text(json.dumps({
        "id": ext_id, "name": ext_id, "version": "1.0.0", "description": "тест",
        "author": "T", "license": "MIT",
        "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": f">={SDK_VERSION}"},
        "permissions": {}}, ensure_ascii=False), encoding="utf-8")
    for name, content in (files or {"echo.py": ECHO_MODE}).items():
        (root / "modes" / name).write_text(content, encoding="utf-8")
    state = runtime.settings.setdefault("extensions", {})
    state.setdefault("installed", {})[ext_id] = {"version": "1.0.0", "dev": str(root)}
    state.setdefault("enabled", {})[ext_id] = enabled
    return root


def setup(tmp_path, level="ext:coffee.echo", **kwargs):
    runtime = Runtime(tmp_path)
    make_extension(runtime, **kwargs)
    runtime.extensions.load_enabled()
    runtime.settings["intelligence"] = level
    runtime.settings["model"]["stub_text"] = ""
    calls = []

    async def provider(kind, payload):
        calls.append(json.loads(json.dumps(payload)))
        return json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": {}}).encode(), "application/json"
    runtime.provider = provider
    return runtime, calls


async def test_extension_mode_answers_via_sdk_context(tmp_path):
    runtime, calls = setup(tmp_path)
    answer = await runtime.chat("привет", history=[{"role": "user", "content": "раньше"}], sn="SN-1")
    assert calls == []  # модель не вызывается — отвечает режим расширения
    assert answer["text"] == "Эхо: привет" and answer["source"] == "coffee.echo"
    assert answer["ctx_text"] == "привет" and answer["ctx_sn"] == "SN-1"
    assert answer["history"] == 1 and answer["tool_calls"] == []
    await runtime.close()


async def test_mode_source_cannot_spoof_scenarios(tmp_path):
    runtime, _ = setup(tmp_path, files={"echo.py": SPOOF_MODE})
    answer = await runtime.chat("привет")
    assert answer["source"] == "coffee.echo"  # источник — режим; TTS-уровень «scenarios» не подделать
    await runtime.close()


async def test_disabled_extension_falls_back_with_recent(tmp_path):
    runtime, calls = setup(tmp_path, enabled=False)
    answer = await runtime.chat("привет")
    assert calls == [] and answer["source"] == "scenarios" and answer["text"] == "Я пока этого не умею."
    assert any("coffee.echo" in item["error"] for item in runtime.recent if item["kind"] == "intelligence")
    await runtime.close()


async def test_unknown_ext_value_falls_back(tmp_path):
    runtime, calls = setup(tmp_path, level="ext:нет-такого")
    answer = await runtime.chat("привет")
    assert answer["source"] == "scenarios" and calls == []
    assert any("нет-такого" in item["error"] for item in runtime.recent)
    await runtime.close()


async def test_broken_mode_file_is_skipped_others_work(tmp_path):
    runtime, _ = setup(tmp_path, files={"echo.py": ECHO_MODE, "broken.py": BROKEN_MODE})
    answer = await runtime.chat("привет")
    assert answer["source"] == "coffee.echo" and answer["text"] == "Эхо: привет"  # битый файл не мешает
    await runtime.close()


async def test_failing_answer_falls_back_to_not_understood(tmp_path):
    runtime, _ = setup(tmp_path, files={"echo.py": FAILING_MODE})
    answer = await runtime.chat("привет")
    assert answer["source"] == "scenarios" and answer["text"] == "Я пока этого не умею."
    assert any("boom" in item["error"] for item in runtime.recent)
    await runtime.close()


async def test_validation_accepts_ext_format_rejects_garbage(tmp_path):
    runtime = Runtime(tmp_path)
    value = runtime.public()
    value["intelligence"] = "ext:coffee.echo"
    assert runtime.settings_service.validate(value, runtime.settings)["intelligence"] == "ext:coffee.echo"
    for bad in ("genius", "ext:", "ext"):
        attempt = dict(value, intelligence=bad)
        try:
            runtime.settings_service.validate(attempt, runtime.settings)
        except ValueError:
            continue
        await runtime.close()
        raise AssertionError("валидация приняла " + bad)
    await runtime.close()


async def test_ext_setting_survives_restart(tmp_path):
    runtime, _ = setup(tmp_path)
    runtime.settings_service.persist(runtime.settings)  # сохранение с ext-значением
    reopened = Runtime(tmp_path)
    assert reopened.settings["intelligence"] == "ext:coffee.echo"  # миграция не затирает режим расширения
    await reopened.close()
    await runtime.close()


async def test_modes_api_lists_tabs_and_preset(tmp_path):
    runtime, _ = setup(tmp_path, files={"echo.py": ECHO_MODE, "broken.py": BROKEN_MODE})
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            body = await (await client.get(url + "/emulator/api/intelligence")).json()
        assert body["levels"] == ["scenarios", "model", "agent"]
        assert body["modes"] == [{"id": "coffee.echo", "title": "Эхо", "ext": "coffee",
                                  "tabs": [{"id": "echo", "title": "Эхо"}, {"id": "cfg", "title": "Настройки"}],
                                  "preset": {"voice": "echo", "model": "stub"}, "needs": ["network"]}]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_modes_api_hides_disabled_extension(tmp_path):
    runtime, _ = setup(tmp_path, enabled=False)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            body = await (await client.get(url + "/emulator/api/intelligence")).json()
        assert body["modes"] == []  # выключенное расширение — сегментов и подвкладок нет
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_builtin_levels_unchanged(tmp_path):
    runtime = Runtime(tmp_path)
    for value, expected in (("scenarios", "scenarios"), ("model", "model"), ("agent", "agent"), ("ext:x", "agent")):
        runtime.settings["intelligence"] = value
        assert intelligence.level(runtime) == expected
    await runtime.close()
