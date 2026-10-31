"""TASK-036: реестр пакетов сценариев перенесён в каталог расширений.

Миграция настроек (registry.url → источник, установленные пакеты → data-расширения, откаты,
пользовательские копии) и установка пакетов прежнего реестра через общий менеджер расширений:
sha256, origin, zip-бандл, обновление и откат. Сеть — локальный http-сервер aiohttp.
"""
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from aiohttp import ClientSession, web

from server_emulator.extensions.legacy_registry import extension_id, normalize_version
from server_emulator.runtime import Runtime
from server_emulator.scenario_engine import library as scenario_library
from server_emulator.server import create_app
from tests.conftest import listen

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"X-Emulator": "1"}

HELLO = {
    "format_version": 2, "id": "user.hello", "version": "1.0.0", "category": "custom",
    "triggers": [{"kind": "phrase", "phrases": ["привет реестр"]}],
    "flow": [{"id": "say", "op": "compose", "reply": {"text": "Привет из пакета."}},
             {"id": "done", "op": "finish"}],
}
HELLO_V2 = {**HELLO, "version": "2.0.0",
            "flow": [{"id": "say", "op": "compose", "reply": {"text": "Привет, версия два."}},
                     {"id": "done", "op": "finish"}]}


def package_bytes(*scenarios):
    return json.dumps({"manifest": {"format": "scenario-package", "version": 1},
                       "scenarios": list(scenarios)}, ensure_ascii=False).encode()


def zip_bundle(payload):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("bundle/package.json", payload)
    return buffer.getvalue()


async def serve(routes):
    """Локальный реестр: путь → bytes (отдаётся как есть) или dict (JSON); читается на каждый запрос."""
    app = web.Application()

    async def handler(request):
        item = routes[request.path.lstrip("/")]
        return web.json_response(item) if isinstance(item, dict) else web.Response(body=item)

    for name in routes:
        app.router.add_get("/" + name, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def registry_manifest(base, packages):
    return {"format": "scenario-registry", "version": 1, "packages": [
        {**p, "url": p["url"] if "://" in p["url"] else f"{base}/{p['url']}"} for p in packages]}


async def op(client, url, payload, expect=200):
    async with client.post(url + "/emulator/api/extensions", headers=HEADERS, json=payload) as response:
        body = await response.json()
        assert response.status == expect, body
        return body


def phrases(runtime):
    return {s["id"] for s in scenario_library.extension_scenarios(runtime.extensions)[0].values()}


# --- миграция настроек --------------------------------------------------------------

def old_registry_state(url, history, scenarios):
    return {"registry": {"url": url, "installed": history}, "scenarios": scenarios}


async def seed_old_state(tmp_path, **state):
    """Состояние до TASK-036: ключ registry в настройках и сценарии пакета среди пользовательских."""
    runtime = Runtime(tmp_path)
    runtime.write("settings.json", {**runtime.settings, **state})
    await runtime.close()


async def test_migration_turns_registry_into_source_and_extensions(tmp_path):
    fork = {**HELLO, "id": "user.fork", "triggers": [{"kind": "phrase", "phrases": ["вилка"]}]}
    changed = {**fork, "flow": [{"id": "mine", "op": "finish"}]}  # локальная правка пакета
    history = [
        {"id": "pack.greet", "version": "1.0.0", "installed_ts": 100,
         "scenarios": ["user.hello", "user.fork"],
         "snapshot": [{**HELLO, "_group": "clock/watches"}, {**fork, "_group": "clock/watches"}]},
        {"id": "pack.greet", "version": "2", "installed_ts": 200, "scenarios": ["user.hello", "user.fork"],
         "snapshot": [{**HELLO_V2, "_group": "clock/watches"}, {**fork, "_group": "clock/watches"}]},
    ]
    pristine_v2 = {**HELLO_V2, "_group": "clock/watches"}  # у пользователя — нетронутая копия v2, как после install из реестра
    await seed_old_state(tmp_path, **old_registry_state(
        "https://example.com/manifest.json", history, [pristine_v2, changed]))

    runtime = Runtime(tmp_path)
    try:
        settings = runtime.settings
        assert "registry" not in settings  # второго механизма нет
        assert "https://example.com/manifest.json" in settings["extensions"]["sources"]
        installed = settings["extensions"]["installed"]["pack.greet"]
        assert installed["version"] == "2.0.0" and installed["previous"] == "1.0.0"  # откат сохранён
        assert installed["source"] == "https://example.com/manifest.json"
        assert settings["extensions"]["enabled"]["pack.greet"] is True
        # нетронутая копия ушла в расширение; локально изменённая осталась и перекрывает
        assert [s["id"] for s in settings["scenarios"]] == ["user.fork"]
        assert settings["scenarios"][0]["flow"][0]["id"] == "mine"
        card = next(c for c in runtime.extensions.listing()["extensions"] if c["id"] == "pack.greet")
        assert card["class"] == "data" and card["folders"] == ["scenarios"]
        # сценарии работают: из расширения отвечает hello (версия 2)
        assert phrases(runtime) == {"user.hello", "user.fork"}
        runtime.settings["aapi_model"] = False
        report = await runtime.dialog.handle_text("привет реестр", {"input": {"text": "привет реестр", "sn": "",
                                                                              "message_id": "m", "request_id": "r",
                                                                              "dialog_id": "d"}}, "dry", _sink)
        assert report["handled"] and report["scenario"] == "user.hello"
        # откат на версию 1 работает штатным менеджером
        await runtime.extensions.rollback("pack.greet")
        assert runtime.extensions.current_version("pack.greet") == "1.0.0"
    finally:
        await runtime.close()

    # повторный запуск ничего не меняет (ключа registry нет)
    again = Runtime(tmp_path)
    try:
        assert again.settings["extensions"]["installed"]["pack.greet"]["version"] == "1.0.0"
        assert len(again.settings["extensions"]["sources"]) == 1
    finally:
        await again.close()


async def _sink(data):
    return None


async def test_migration_keeps_disabled_copy_and_skips_bad_source(tmp_path):
    disabled = {**HELLO, "enabled": False}
    history = [{"id": "pack.greet", "version": "1.0.0", "installed_ts": 1, "scenarios": ["user.hello"],
                "snapshot": [HELLO]}]
    await seed_old_state(tmp_path, **old_registry_state("http://example.com/manifest.json", history, [disabled]))
    runtime = Runtime(tmp_path)
    try:
        assert runtime.settings["extensions"]["sources"] == []  # http не-loopback источником быть не может
        assert [s["id"] for s in runtime.settings["scenarios"]] == ["user.hello"]  # выключенная копия остаётся
        assert "pack.greet" in runtime.settings["extensions"]["installed"]
    finally:
        await runtime.close()


async def test_migration_without_registry_does_nothing(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        assert "registry" not in runtime.settings and runtime.settings["extensions"]["sources"] == []
    finally:
        await runtime.close()


def test_version_and_id_normalisation():
    assert normalize_version("1") == "1.0.0" and normalize_version("1.2") == "1.2.0"
    assert normalize_version("1.2.3.4") == "1.2.3" and normalize_version("1.2.3-beta") == "1.2.3-beta"
    assert normalize_version("latest") == "0.0.1" and normalize_version(None) == "0.0.1"
    assert extension_id("Pack_Greet!") == "pack-greet" and extension_id("???") == "scenarios"


# --- установка пакета прежнего реестра через менеджер расширений -------------------------

async def test_registry_source_installs_package_as_extension(tmp_path):
    payload = package_bytes(HELLO)
    routes = {"pack.json": payload, "pack2.json": b"", "manifest.json": {}}
    reg_runner, base = await serve(routes)
    routes["manifest.json"] = registry_manifest(base, [
        {"id": "pack.greet", "version": "1.0.0", "name": "Приветствие", "url": "pack.json",
         "sha256": hashlib.sha256(payload).hexdigest(), "path": "clock/watches"}])
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            source = base + "/manifest.json"
            await op(client, url, {"op": "add_source", "url": source})
            entries = (await op(client, url, {"op": "catalog", "url": source}))["entries"]
            assert [(e["id"], e["version"], e["description"]) for e in entries] == [
                ("pack.greet", "1.0.0", "Приветствие")]
            result = await op(client, url, {"op": "install", "url": source, "id": "pack.greet"})
            assert result["installed"] == "pack.greet" and result["class"] == "data"  # без подтверждения кода
            await op(client, url, {"op": "enable", "id": "pack.greet"})
            assert phrases(runtime) == {"user.hello"}
            listing = await (await client.get(url + "/emulator/api/scenarios")).json()
            assert [s["extension"] for s in listing["extensions"]] == ["pack.greet"]  # метка источника
            # новая версия в реестре → «Обновить», затем «Откатить»
            payload2 = package_bytes(HELLO_V2)
            routes["pack2.json"] = payload2
            routes["manifest.json"] = registry_manifest(base, [
                {"id": "pack.greet", "version": "2.0.0", "name": "Приветствие", "url": "pack2.json",
                 "sha256": hashlib.sha256(payload2).hexdigest()}])
            await op(client, url, {"op": "update", "id": "pack.greet"})
            assert runtime.extensions.current_version("pack.greet") == "2.0.0"
            await op(client, url, {"op": "rollback", "id": "pack.greet"})
            assert runtime.extensions.current_version("pack.greet") == "1.0.0"
    finally:
        await runner.cleanup()
        await runtime.close()
        await reg_runner.cleanup()


async def test_registry_source_rejects_bad_hash_and_foreign_origin(tmp_path):
    payload = package_bytes(HELLO)
    routes = {"pack.json": payload, "manifest.json": {}}
    reg_runner, base = await serve(routes)
    routes["manifest.json"] = registry_manifest(base, [
        {"id": "pack.bad", "version": "1.0.0", "name": "Плохой", "url": "pack.json", "sha256": "0" * 64},
        {"id": "pack.evil", "version": "1.0.0", "name": "Чужой", "url": "http://evil.example/p.json"}])
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            source = base + "/manifest.json"
            body = await op(client, url, {"op": "install", "url": source, "id": "pack.bad"}, expect=400)
            assert "sha256" in body["error"]
            body = await op(client, url, {"op": "install", "url": source, "id": "pack.evil"}, expect=400)
            assert "origin" in body["error"]
        assert runtime.settings["extensions"]["installed"] == {}
    finally:
        await runner.cleanup()
        await runtime.close()
        await reg_runner.cleanup()


async def test_registry_source_installs_zip_bundle(tmp_path):
    bundle = zip_bundle(package_bytes(HELLO))
    routes = {"pack.zip": bundle, "manifest.json": {}}
    reg_runner, base = await serve(routes)
    routes["manifest.json"] = registry_manifest(base, [
        {"id": "pack.zip", "version": "1", "name": "Бандл", "url": "pack.zip",
         "sha256": hashlib.sha256(bundle).hexdigest()}])
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            result = await op(client, url, {"op": "install", "url": base + "/manifest.json", "id": "pack.zip"})
            assert result["version"] == "1.0.0"  # «1» из реестра → semver
            await op(client, url, {"op": "enable", "id": "pack.zip"})
            assert phrases(runtime) == {"user.hello"}
    finally:
        await runner.cleanup()
        await runtime.close()
        await reg_runner.cleanup()


async def test_make_registry_catalog_is_installable_as_extension_source(tmp_path):
    """tools/make_registry.py по-прежнему публикует каталог; ставится как источник расширений."""
    source_runtime = Runtime(tmp_path / "state")
    await source_runtime.save_settings({**source_runtime.settings,
                                        "scenarios": [dict(HELLO, _group="clock/watches")]})
    await source_runtime.close()
    out = tmp_path / "registry"
    for as_zip in (False, True):
        result = subprocess.run([sys.executable, "tools/make_registry.py", str(tmp_path / "state"), str(out),
                                 *(["--zip"] if as_zip else [])], capture_output=True, text=True, cwd=ROOT)
        assert result.returncode == 0, result.stderr
        routes = {p.name: p.read_bytes() for p in out.iterdir() if p.suffix != ".json" or p.name != "manifest.json"}
        routes["manifest.json"] = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        reg_runner, base = await serve(routes)
        target = Runtime(tmp_path / f"target{int(as_zip)}")
        runner, url = await listen(create_app(runtime=target))
        try:
            async with ClientSession() as client:
                result = await op(client, url, {"op": "install", "url": base + "/manifest.json", "id": "user.hello"})
                assert result["installed"] == "user.hello"
                await op(client, url, {"op": "enable", "id": "user.hello"})
            assert phrases(target) == {"user.hello"}
        finally:
            await runner.cleanup()
            await target.close()
            await reg_runner.cleanup()
