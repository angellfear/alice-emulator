"""TASK-033: действия и датасеты расширений (actions/*.py, datasets/*.py).

Расширение-папка (режим разработки, как в tests/test_extensions_manager.py и
tests/test_extension_modes.py) объявляет ACTIONS = {"имя": fn} и DATASETS =
{"имя": async (key, args) -> value}; после включения они живут в реестрах
Executor (runtime.scenario_executor.actions/plugins) рядом со встроенными
(timer.start…, weather) и доступны JSON-шагу action/load/refresh и SDK
ctx.action/ctx.dataset (тот же реестр — полный e2e py-сценария остаётся
заделом TASK-019, пока py-сценарии не подключены).

Правило сверки разрешения (TASK-033): имя действия обязано начинаться с
префикса расширения (contributes.actions.prefix или id) ИЛИ быть объявленным
в permissions.actions; имя вне префикса без объявления — противоречие. Имя
датасета — всегда с префикса. Конфликт со встроенным или другим расширением →
расширение не включается: уходит из extensions.loaded с ошибкой в warnings,
встроенные реестры и остальные расширения не затронуты. dry_run (SDK) действие
не вызывает — пишет effect.
"""
import json

from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.runtime import Runtime
from server_emulator.scenario_packages.validate import validate_scenario
from server_emulator.sdk import SDK_VERSION, SDKError, ScenarioContext
from tests.conftest import CTX


def action_module(marker, refs=("coffee.brew",), name="brew",
                  result='{"brewed": arguments.get("size")}'):
    """Модуль actions/*.py: пишет каждый вызов в marker (JSON-список аргументов)."""
    path = str(marker).replace("\\", "/")
    table = ", ".join(f'"{ref}": {name}' for ref in refs)
    return (
        f'import json\n'
        f'from pathlib import Path\n'
        f'MARKER = Path("{path}")\n\n'
        f'async def note(arguments):\n'
        f'    calls = json.loads(MARKER.read_text(encoding="utf-8")) if MARKER.exists() else []\n'
        f'    calls.append(dict(arguments))\n'
        f'    MARKER.write_text(json.dumps(calls), encoding="utf-8")\n\n'
        f'async def {name}(arguments, execution):\n'
        f'    await note(arguments)\n'
        f'    return {result}\n\n'
        f'ACTIONS = {{{table}}}\n'
    )


def dataset_module(marker, ref="coffee.machines", name="machines"):
    """Модуль datasets/*.py: пишет каждый вызов в marker (JSON-список [key, args])."""
    path = str(marker).replace("\\", "/")
    return (
        f'import json\n'
        f'from pathlib import Path\n'
        f'MARKER = Path("{path}")\n\n'
        f'async def {name}(key, args):\n'
        f'    calls = json.loads(MARKER.read_text(encoding="utf-8")) if MARKER.exists() else []\n'
        f'    calls.append([key, dict(args or {{}})])\n'
        f'    MARKER.write_text(json.dumps(calls), encoding="utf-8")\n'
        f'    return {{"key": key, "kind": (args or {{}}).get("kind", ""), "fresh": True}}\n\n'
        f'DATASETS = {{"{ref}": {name}}}\n'
    )


def make_extension(runtime, ext_id="coffee", actions=None, datasets=None,
                   permissions=None, prefix=None, enabled=True):
    """Расширение-папка с actions/ и datasets/, подключённое напрямую (dev-режим):
    manifest + записи installed/enabled в settings; load_enabled() подключает."""
    root = runtime.directory / "ext" / ext_id
    (root / "actions").mkdir(parents=True, exist_ok=True)
    (root / "datasets").mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": ext_id, "name": ext_id, "version": "1.0.0", "description": "тест",
        "author": "T", "license": "MIT",
        "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": f">={SDK_VERSION}"},
        "permissions": permissions or {},
    }
    if prefix:
        manifest["contributes"] = {"actions": {"prefix": prefix}}
    (root / "extension.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for name, source in (actions or {}).items():
        (root / "actions" / name).write_text(source, encoding="utf-8")
    for name, source in (datasets or {}).items():
        (root / "datasets" / name).write_text(source, encoding="utf-8")
    state = runtime.settings.setdefault("extensions", {})
    state.setdefault("installed", {})[ext_id] = {"version": "1.0.0", "dev": str(root)}
    state.setdefault("enabled", {})[ext_id] = enabled
    return root


def action_scenario():
    return validate_scenario({
        "format_version": 2, "id": "coffee.make", "version": "1.0.0", "category": "custom",
        "triggers": [{"kind": "phrase", "phrases": ["сделай кофе"], "match": "contains"}],
        "flow": [
            {"id": "brew", "op": "action", "ref": "coffee.brew", "arguments": {"size": 2},
             "slot": "result"},
            {"id": "say", "op": "compose", "reply": {"text": "Кофе: {{slots.result.brewed}}"}},
            {"id": "done", "op": "finish"},
        ]})


async def loaded_runtime(tmp_path, **kwargs):
    runtime = Runtime(tmp_path)
    make_extension(runtime, **kwargs)
    runtime.extensions.load_enabled()  # хук пересборки: реестры уже с расширением
    return runtime


# --- действие расширения: JSON-шаг action и SDK ctx.action --------------------------


async def test_extension_action_runs_via_json_step(tmp_path):
    marker = tmp_path / "calls.json"
    runtime = await loaded_runtime(tmp_path, actions={"brew.py": action_module(marker)})
    try:
        assert "coffee.brew" in runtime.scenario_executor.actions  # зарегистрировано рядом…
        assert "timer.start" in runtime.scenario_executor.actions  # …со встроенными
        outcome = await runtime.scenario_executor.execute(action_scenario(), dict(CTX), "dlg-json")
        assert outcome.status == "finished"
        assert outcome.slots["result"] == {"brewed": 2}  # результат эффекта — данные JSON
        assert json.loads(marker.read_text(encoding="utf-8")) == [{"size": 2}]
        ref, message = outcome.composed[0]
        assert ref == "reply"
        assert message.Event.AliceResponse.Response.Cards[0].Text == "Кофе: 2"
    finally:
        await runtime.close()


async def test_extension_action_via_sdk_context(tmp_path):
    """Py-путь — через SDK ctx.action (тот же реестр Executor). Полный e2e
    py-сценария (handle(ctx) рядом с JSON) — задел TASK-019: py-сценарии
    пока не подключены."""
    marker = tmp_path / "calls.json"
    runtime = await loaded_runtime(tmp_path, actions={"brew.py": action_module(marker)})
    try:
        ctx = ScenarioContext(runtime, dict(CTX))
        assert await ctx.action("coffee.brew", size=3) == {"brewed": 3}
        assert json.loads(marker.read_text(encoding="utf-8")) == [{"size": 3}]
        try:
            await ctx.action("tea.brew")
        except SDKError:
            pass
        else:
            raise AssertionError("чужое действие не должно быть доступно")
    finally:
        await runtime.close()


async def test_dry_run_records_effect_without_calling(tmp_path):
    marker = tmp_path / "calls.json"
    runtime = await loaded_runtime(tmp_path, actions={"brew.py": action_module(marker)})
    try:
        ctx = ScenarioContext(runtime, dict(CTX), dry_run=True)
        assert await ctx.action("coffee.brew", size=5) is None
        assert ctx.effects == [{"kind": "action", "name": "coffee.brew", "arguments": {"size": 5}}]
        assert not marker.exists()  # реестр не тронут — действие не вызывалось
    finally:
        await runtime.close()


# --- датасеты: Executor.refresh_dataset и SDK ctx.dataset -----------------------------------


async def test_extension_dataset_via_refresh_and_ctx(tmp_path):
    marker = tmp_path / "ds.json"
    runtime = await loaded_runtime(tmp_path, datasets={"machines.py": dataset_module(marker)})
    try:
        assert "coffee.machines" in runtime.scenario_executor.plugins
        assert "weather" in runtime.scenario_executor.plugins  # встроенный на месте
        value = await runtime.scenario_executor.refresh_dataset("coffee.machines", "k1", {"kind": "espresso"})
        assert value == {"key": "k1", "kind": "espresso", "fresh": True}
        assert runtime.store.scenario_data_record("dataset:coffee.machines", "system", "", "k1")[0] == value
        assert json.loads(marker.read_text(encoding="utf-8")) == [["k1", {"kind": "espresso"}]]
        # запись в БД сделала сторона executor: ctx.dataset читает из БД, плагин спит
        ctx = ScenarioContext(runtime, dict(CTX))
        assert await ctx.dataset("coffee.machines", "k1", max_age=600) == \
            {"key": "k1", "kind": "espresso", "fresh": True}
        assert json.loads(marker.read_text(encoding="utf-8")) == [["k1", {"kind": "espresso"}]]
        # другой ключ — честный вызов плагина с args
        assert await ctx.dataset("coffee.machines", "k2", args={"kind": "latte"}) == \
            {"key": "k2", "kind": "latte", "fresh": True}
        assert json.loads(marker.read_text(encoding="utf-8")) == \
            [["k1", {"kind": "espresso"}], ["k2", {"kind": "latte"}]]
    finally:
        await runtime.close()


# --- пересборка реестров при включении/выключении -------------------------------------


async def test_enable_disable_rebuilds_registries(tmp_path):
    marker, ds_marker = tmp_path / "calls.json", tmp_path / "ds.json"
    runtime = Runtime(tmp_path)
    make_extension(runtime, actions={"brew.py": action_module(marker)},
                   datasets={"machines.py": dataset_module(ds_marker)}, enabled=False)
    runtime.extensions.load_enabled()
    try:
        assert "coffee.brew" not in runtime.scenario_executor.actions  # выключено — реестр чист
        await runtime.extensions.enable("coffee")
        assert "coffee.brew" in runtime.scenario_executor.actions
        assert "coffee.machines" in runtime.scenario_executor.plugins
        # объекты реестров не подменяются: алиас scenario_plugins жив (заглушки tools/тестов)
        assert runtime.scenario_plugins is runtime.scenario_executor.plugins
        assert "weather" in runtime.scenario_plugins
        await runtime.extensions.disable("coffee")
        assert "coffee.brew" not in runtime.scenario_executor.actions
        assert "coffee.machines" not in runtime.scenario_executor.plugins
        assert "timer.start" in runtime.scenario_executor.actions
        assert "weather" in runtime.scenario_executor.plugins
    finally:
        await runtime.close()


# --- имена: префикс, permissions.actions, конфликты ------------------------------------


async def test_declared_prefix_namespaces_actions(tmp_path):
    marker = tmp_path / "calls.json"
    runtime = await loaded_runtime(tmp_path, actions={"cook.py": action_module(marker,
                                                                                refs=("kitchen.cook",),
                                                                                name="cook")},
                                   prefix="kitchen")
    try:
        assert "kitchen.cook" in runtime.scenario_executor.actions  # префикс ≠ id — легально
    finally:
        await runtime.close()


async def test_action_declared_in_permissions_outside_prefix_allowed(tmp_path):
    """Сверка разрешения: имя в permissions.actions ИЛИ под префиксом — явное
    объявление легализует имя вне пространства расширения."""
    marker = tmp_path / "calls.json"
    runtime = await loaded_runtime(tmp_path, actions={"call.py": action_module(marker,
                                                                               refs=("extra.call",),
                                                                               name="call",
                                                                               result='{"ok": True}')},
                                   permissions={"actions": ["extra.call"]})
    try:
        assert "extra.call" in runtime.scenario_executor.actions
        assert runtime.extensions.warnings == []
    finally:
        await runtime.close()


async def test_action_outside_namespace_rejected(tmp_path):
    """Имя без префикса и без объявления в permissions.actions — противоречие:
    расширение не включается, ни одно его действие не регистрируется."""
    marker = tmp_path / "calls.json"
    runtime = Runtime(tmp_path)
    make_extension(runtime, actions={
        "brew.py": action_module(marker),  # coffee.brew — сам по себе легален
        "call.py": action_module(marker, refs=("evil.do",), name="do", result='{"ok": True}')})
    runtime.extensions.load_enabled()
    try:
        assert runtime.extensions.loaded == {}  # «не включается»
        warning = runtime.extensions.warnings[0]
        assert warning["id"] == "coffee"
        assert "evil.do" in warning["error"] and "permissions.actions" in warning["error"]
        assert "coffee.brew" not in runtime.scenario_executor.actions
    finally:
        await runtime.close()


async def test_conflict_with_builtin_disables_extension(tmp_path):
    marker = tmp_path / "calls.json"
    runtime = Runtime(tmp_path)
    make_extension(runtime, actions={
        "brew.py": action_module(marker, refs=("coffee.brew", "timer.start"), result='{"ok": True}')})
    runtime.extensions.load_enabled()
    try:
        assert runtime.extensions.loaded == {}
        assert "timer.start" in runtime.extensions.warnings[0]["error"]  # понятная ошибка
        # встроенное действие не подменено, действие расширения не зарегистрировано
        assert runtime.scenario_executor.actions["timer.start"].__module__ == "server_emulator.runtime"
        assert "coffee.brew" not in runtime.scenario_executor.actions
    finally:
        await runtime.close()


async def test_name_conflict_between_extensions_disables_the_later_one(tmp_path):
    first, second = tmp_path / "c.json", tmp_path / "t.json"
    runtime = Runtime(tmp_path)
    make_extension(runtime, ext_id="coffee",
                   actions={"brew.py": action_module(first, refs=("coffee.brew", "tea.pot"),
                                                     result='{"ok": True}')},
                   permissions={"actions": ["tea.pot"]})  # имя вне префикса — с объявлением
    make_extension(runtime, ext_id="tea", actions={
        "pour.py": action_module(second, refs=("tea.pot",), name="pour", result='{"ok": True}')})
    runtime.extensions.load_enabled()
    try:
        assert [w["id"] for w in runtime.extensions.warnings] == ["tea"]  # первый по id владеет
        assert "coffee" in runtime.extensions.loaded
        assert "расширением coffee" in runtime.extensions.warnings[0]["error"]
        assert "coffee.brew" in runtime.scenario_executor.actions  # coffee работает
        assert runtime.scenario_executor.actions["tea.pot"].__module__ == "ext_actions_brew"
        assert "pour" not in [fn.__name__ for fn in runtime.scenario_executor.actions.values()]
    finally:
        await runtime.close()


async def test_dataset_outside_prefix_rejected(tmp_path):
    marker = tmp_path / "ds.json"
    runtime = await loaded_runtime(tmp_path, datasets={
        "m.py": dataset_module(marker, ref="machines", name="machines")})
    try:
        assert runtime.extensions.loaded == {}
        assert "machines" in runtime.extensions.warnings[0]["error"]  # «Имя датасета …»
        assert "machines" not in runtime.scenario_executor.plugins
        assert "weather" in runtime.scenario_executor.plugins
    finally:
        await runtime.close()


async def test_broken_actions_module_disables_extension(tmp_path):
    marker = tmp_path / "calls.json"
    runtime = await loaded_runtime(tmp_path, actions={
        "brew.py": action_module(marker),
        "broken.py": "ACTIONS = {это не python\n"})
    try:
        assert runtime.extensions.loaded == {}
        assert "broken.py" in runtime.extensions.warnings[0]["error"]
        assert "coffee.brew" not in runtime.scenario_executor.actions
    finally:
        await runtime.close()
