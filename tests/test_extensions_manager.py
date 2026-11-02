"""TASK-029: менеджер расширений — источники, установка, состояние, обновление,
откат, хуки, самопроверка. Вся сеть — локальный http-сервер aiohttp (как в
тестах реестра сценариев); обращений в интернет нет.
"""
import hashlib
import io
import json
import zipfile

import pytest
from aiohttp import ClientSession, web

from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.extensions.manager import satisfies, validate_manifest
from server_emulator.extensions.sources import (CatalogSource, GithubSource, LocalSource,
                                                ZipSource, parse_source)
from server_emulator.runtime import Runtime
from server_emulator.sdk import SDK_VERSION
from server_emulator.server import create_app
from tests.conftest import listen


def api_headers():
    return {"X-Emulator": "1"}


def manifest(ext_id="coffee", version="1.0.0", **overrides):
    data = {
        "id": ext_id, "name": "Кофемашина", "version": version,
        "description": "Сценарий «сделай кофе».",
        "author": "Ivan <ivan@example.com>", "license": "MIT",
        "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": f">={SDK_VERSION} <3.0.0"},
        "permissions": {},
    }
    data.update(overrides)
    return data


SCENARIO = {
    "format_version": 2, "id": "coffee.make", "version": "1.0.0", "category": "custom",
    "triggers": [{"kind": "phrase", "phrases": ["сделай кофе"], "match": "contains"}],
    "flow": [{"id": "say", "op": "compose", "reply": {"text": "Варю кофе."}},
             {"id": "done", "op": "finish"}],
}


def ext_files(ext_id="coffee", version="1.0.0", manifest_data=None, hooks=None, extra=None):
    files = {
        "extension.json": json.dumps(manifest_data or manifest(ext_id, version), ensure_ascii=False),
        f"scenarios/custom/{ext_id}.make.json": json.dumps(
            {**SCENARIO, "id": f"{ext_id}.make"}, ensure_ascii=False),
    }
    if hooks is not None:
        files["hooks.py"] = hooks
    for name, content in (extra or {}).items():
        files[name] = content
    return files


def zip_bytes(files, extra_entries=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
        for name, content in (extra_entries or {}).items():
            bundle.writestr(name, content)
    return buffer.getvalue()


async def serve(pages):
    """Локальный источник: /имя — zip (bytes) или JSON-каталог (dict); страницы
    читаются на каждый запрос — тест правит их между операциями."""
    app = web.Application()

    async def handler(request):
        item = pages[request.path.lstrip("/")]
        return web.json_response(item) if isinstance(item, dict) else web.Response(body=item)

    for name in pages:
        app.router.add_get("/" + name, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def op(client, url, payload, expect=200):
    async with client.post(url + "/emulator/api/extensions", headers=api_headers(),
                           json=payload) as response:
        body = await response.json()
        assert response.status == expect, body
        return body


# --- unit: разбор источников, диапазоны, валидация манифеста ----------------------

def test_parse_source_kinds(tmp_path):
    assert parse_source("https://github.com/o/r") == GithubSource("https://github.com/o/r", None)
    assert parse_source("https://github.com/o/r#v1.2.0").ref == "v1.2.0"  # закреплённый тег
    assert isinstance(parse_source("https://example.com/x.zip"), ZipSource)
    assert isinstance(parse_source("https://example.com/catalog.json"), CatalogSource)
    assert isinstance(parse_source(str(tmp_path)), LocalSource)  # локальная папка — без сети
    with pytest.raises(ValueError):  # http — только loopback
        parse_source("http://example.com/x.zip")
    with pytest.raises(ValueError):
        parse_source("https://github.com/o")  # нет <repo>
    with pytest.raises(ValueError):
        parse_source(str(tmp_path / "missing"))  # несуществующая локальная папка
    with pytest.raises(ValueError):
        parse_source("")


def test_satisfies_ranges():
    assert satisfies("1.2.3", ">=1.0.0 <2.0.0")
    assert satisfies("1.2.3", "^1.2.0") and not satisfies("2.0.0", "^1.2.0")
    assert satisfies("0.1.0", ">=0.1.0") and not satisfies("0.0.9", ">=0.1.0")
    assert satisfies("0.1.5", "^0.1.0") and not satisfies("0.2.0", "^0.1.0")
    assert satisfies("1.2.9", "1.2") and not satisfies("1.3.0", "1.2")
    assert satisfies("1.2.3-beta", "^1.2.0")
    assert satisfies("1.2.3", "")  # пустой диапазон — любая версия
    assert satisfies("1.2.3", ">=1.0,<2") and not satisfies("2.0.0", ">=1.0,<2")


def test_manifest_validation():
    validate_manifest(manifest())
    typo = manifest()
    typo["permisions"] = {}  # опечатка не должна превратить расширение в «без разрешений»
    with pytest.raises(ValueError):
        validate_manifest(typo)
    bad_network = manifest()
    bad_network["permissions"] = {"network": "example.com"}  # нужен список
    with pytest.raises(ValueError):
        validate_manifest(bad_network)
    bad_version = manifest(version="1.0")
    with pytest.raises(ValueError):
        validate_manifest(bad_version)


# --- установка, состояние, включение ------------------------------------------------

async def test_install_zip_enable_disable_and_restart(tmp_path):
    runner, base = await serve({"coffee.zip": zip_bytes(ext_files())})
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            body = await op(client, url, {"op": "install", "url": f"{base}/coffee.zip"})
            assert body["installed"] == "coffee" and body["version"] == "1.0.0"
            assert body["class"] == "data" and body["folders"] == ["scenarios"]
            version_dir = tmp_path / "extensions" / "coffee" / "1.0.0"
            assert (version_dir / "extension.json").is_file()  # распаковано в <id>/<version>/
            current = tmp_path / "extensions" / "coffee" / "current"
            assert current.read_text(encoding="utf-8") == "1.0.0"  # атомарное переключение
            assert runtime.settings["extensions"]["installed"]["coffee"]["version"] == "1.0.0"
            listing = await (await client.get(url + "/emulator/api/extensions")).json()
            card = listing["extensions"][0]
            assert card["enabled"] is False and card["class"] == "data"
            assert listing["sdk"] == SDK_VERSION and listing["emulator"] == EMULATOR_VERSION
            await op(client, url, {"op": "enable", "id": "coffee"})
            assert runtime.extensions.contribution_dirs("scenarios")[0][0] == "coffee"
            await op(client, url, {"op": "disable", "id": "coffee"})
            assert runtime.extensions.contribution_dirs("scenarios") == []
            assert (version_dir / "extension.json").is_file()  # выключение не удаляет файлы
            assert runtime.settings["extensions"]["enabled"]["coffee"] is False
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()
    runtime2 = Runtime(tmp_path)  # перезапуск: включённое расширение снова загружено
    try:
        runtime2.extensions.load_enabled()
        assert runtime2.extensions.loaded == {}  # выключено — каталогов нет
    finally:
        await runtime2.close()


async def test_enable_persists_and_loads_on_restart(tmp_path):
    runner, base = await serve({"coffee.zip": zip_bytes(ext_files())})
    runtime = Runtime(tmp_path)
    try:
        async with ClientSession() as client:
            app_runner, url = await listen(create_app(runtime=runtime))
            try:
                await op(client, url, {"op": "install", "url": f"{base}/coffee.zip"})
                await op(client, url, {"op": "enable", "id": "coffee"})
            finally:
                await app_runner.cleanup()
            await runtime.close()
        reopened = Runtime(tmp_path)
        try:
            assert list(reopened.extensions.loaded) == ["coffee"]
            assert reopened.extensions.action_prefix("coffee") == "coffee"
        finally:
            await reopened.close()
    finally:
        await runner.cleanup()


async def test_install_from_catalog(tmp_path):
    payload = zip_bytes(ext_files())
    catalog = {"extensions": [{"id": "coffee", "url": "coffee.zip", "version": "1.0.0",
                               "sha256": hashlib.sha256(payload).hexdigest(),
                               "description": "Кофе"}]}
    runner, base = await serve({"coffee.zip": payload, "catalog.json": catalog})
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            await op(client, url, {"op": "add_source", "url": f"{base}/catalog.json"})
            entries = await op(client, url, {"op": "catalog", "url": f"{base}/catalog.json"})
            assert entries["entries"][0]["id"] == "coffee"
            body = await op(client, url, {"op": "install", "url": f"{base}/catalog.json",
                                          "id": "coffee"})
            assert body["installed"] == "coffee"
            assert runtime.settings["extensions"]["installed"]["coffee"]["source"] == f"{base}/catalog.json"
            assert runtime.settings["extensions"]["installed"]["coffee"]["source_id"] == "coffee"
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()


async def test_install_wrong_sha256_rejected(tmp_path):
    payload = zip_bytes(ext_files())
    catalog = {"extensions": [{"id": "coffee", "url": "coffee.zip", "version": "1.0.0",
                               "sha256": "0" * 64}]}
    runner, base = await serve({"coffee.zip": payload, "catalog.json": catalog})
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            body = await op(client, url, {"op": "install", "url": f"{base}/catalog.json",
                                          "id": "coffee"}, expect=400)
            assert "sha256" in body["error"]
            assert not (tmp_path / "extensions" / "coffee").exists()  # установка не началась
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()


async def test_zip_traversal_rejected(tmp_path):
    payload = zip_bytes(ext_files(), extra_entries={"../evil.txt": "nope", "a/../../escape.txt": "no"})
    runner, base = await serve({"coffee.zip": payload})
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            body = await op(client, url, {"op": "install", "url": f"{base}/coffee.zip"}, expect=400)
            assert "Небезопасный путь" in body["error"]
            assert not (tmp_path / "evil.txt").exists()  # ничего не вышло за каталог
            assert not (tmp_path / "extensions" / "coffee").exists()
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()


@pytest.mark.parametrize("field,value", [("emulator", ">=9.0.0"), ("sdk", ">=9.0.0")])
async def test_incompatible_requires_rejected(tmp_path, field, value):
    data = manifest()
    data["requires"] = {**data["requires"], field: value}
    runner, base = await serve({"coffee.zip": zip_bytes(ext_files(manifest_data=data))})
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            body = await op(client, url, {"op": "install", "url": f"{base}/coffee.zip"}, expect=400)
            assert f"requires.{field}" in body["error"]
            assert not (tmp_path / "extensions" / "coffee").exists()
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()


# --- класс code: подтверждение и хуки -------------------------------------------------

def hooks_code(marker, body):
    path = str(marker).replace("\\", "/")
    return (f'from pathlib import Path\n\nMARKER = Path("{path}")\n\n'
            f"def {body.split(' ', 1)[0]}(settings):\n"
            f"    MARKER.parent.mkdir(parents=True, exist_ok=True)\n"
            f"    {body.split(' ', 1)[1]}\n")


async def test_code_class_requires_confirmation_and_runs_hook(tmp_path):
    marker = tmp_path / "markers" / "install.txt"
    hooks = hooks_code(marker, "on_install MARKER.write_text('ok', encoding='utf-8')")
    runner, base = await serve({"ha.zip": zip_bytes(ext_files(ext_id="ha", hooks=hooks))})
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            card = await op(client, url, {"op": "install", "url": f"{base}/ha.zip"})
            assert card["requires_confirmation"] is True  # класс code — без подтверждения отказ
            assert card["class"] == "code" and card["py_files"] == ["hooks.py"]
            assert card["id"] == "ha" and card["permissions"] == {}
            assert not (tmp_path / "extensions" / "ha").exists()
            assert not marker.exists()
            body = await op(client, url, {"op": "install", "url": f"{base}/ha.zip",
                                          "confirm": True})
            assert body["installed"] == "ha"
            assert marker.read_text(encoding="utf-8") == "ok"  # хук on_install выполнен
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()


async def test_hook_failure_rolls_back_install(tmp_path):
    hooks = "def on_install(settings):\n    raise RuntimeError('boom')\n"
    runner, base = await serve({"ha.zip": zip_bytes(ext_files(ext_id="ha", hooks=hooks))})
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            body = await op(client, url, {"op": "install", "url": f"{base}/ha.zip",
                                          "confirm": True}, expect=400)
            assert "on_install" in body["error"] and "boom" in body["error"]
            assert not (tmp_path / "extensions" / "ha" / "1.0.0").exists()  # файлы откачены
            assert not (tmp_path / "extensions" / "ha" / "current").exists()
            assert runtime.settings["extensions"]["installed"] == {}
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()


# --- обновление, откат, удаление -------------------------------------------------------

async def test_update_and_rollback(tmp_path):
    pages = {"coffee.zip": zip_bytes(ext_files(version="1.0.0")),
             "catalog.json": {"extensions": [{"id": "coffee", "url": "coffee.zip",
                                              "version": "1.0.0"}]}}
    runner, base = await serve(pages)
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            await op(client, url, {"op": "install", "url": f"{base}/catalog.json", "id": "coffee"})
            body = await op(client, url, {"op": "update", "id": "coffee"}, expect=400)
            assert "уже установленная" in body["error"]  # версия источника не изменилась
            pages["coffee.zip"] = zip_bytes(ext_files(version="1.1.0"))
            pages["catalog.json"]["extensions"][0]["version"] = "1.1.0"
            body = await op(client, url, {"op": "update", "id": "coffee"})
            assert body["version"] == "1.1.0"
            entry = runtime.settings["extensions"]["installed"]["coffee"]
            assert entry["version"] == "1.1.0" and entry["previous"] == "1.0.0"
            assert (tmp_path / "extensions" / "coffee" / "1.0.0").is_dir()  # предыдущая хранится
            body = await op(client, url, {"op": "rollback", "id": "coffee"})
            assert body["version"] == "1.0.0"
            assert (tmp_path / "extensions" / "coffee" / "current").read_text(encoding="utf-8") == "1.0.0"
            body = await op(client, url, {"op": "rollback", "id": "coffee"})  # откат туда-обратно
            assert body["version"] == "1.1.0"
            await op(client, url, {"op": "uninstall", "id": "coffee"})
            assert not (tmp_path / "extensions" / "coffee").exists()  # удаление — с файлами
            assert runtime.settings["extensions"]["installed"] == {}
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()


async def test_local_folder_source_no_copy(tmp_path):
    dev = tmp_path / "dev" / "coffee"
    dev.mkdir(parents=True)
    for name, content in ext_files().items():
        path = dev / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            body = await op(client, url, {"op": "install", "url": str(dev)})
            assert body["installed"] == "coffee"
            entry = runtime.settings["extensions"]["installed"]["coffee"]
            assert entry["dev"] == str(dev.resolve())
            assert not (tmp_path / "extensions" / "coffee" / "1.0.0").exists()  # без копирования
            assert runtime.extensions.loaded == {}
            await op(client, url, {"op": "enable", "id": "coffee"})
            assert [p for _, p in runtime.extensions.contribution_dirs("scenarios")] == [dev / "scenarios"]
            (dev / "extension.json").write_text(
                json.dumps(manifest(version="1.2.0"), ensure_ascii=False), encoding="utf-8")
            body = await op(client, url, {"op": "update", "id": "coffee"})
            assert body == {"updated": "coffee", "version": "1.2.0", "dev": True}
            assert (dev / "scenarios" / "custom" / "coffee.make.json").is_file()  # правки живут
    finally:
        await app_runner.cleanup()
        await runtime.close()


async def test_sources_add_remove(tmp_path):
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            await op(client, url, {"op": "add_source", "url": "https://example.com/catalog.json"})
            body = await op(client, url, {"op": "add_source", "url": "https://example.com/catalog.json"})
            assert body["sources"] == ["https://example.com/catalog.json"]  # без дублей
            await op(client, url, {"op": "remove_source", "url": "https://example.com/catalog.json"})
            body = await op(client, url, {"op": "remove_source", "url": "https://example.com/catalog.json"},
                            expect=400)
            assert "нет в списке" in body["error"]
            body = await op(client, url, {"op": "add_source", "url": "http://example.com/catalog.json"},
                            expect=400)
            assert "https" in body["error"]
    finally:
        await app_runner.cleanup()
        await runtime.close()


# --- самопроверка tests/ (TASK-023) ------------------------------------------------------

async def test_selfcheck_json_ok_and_py_degraded(tmp_path):
    files = ext_files()
    files["tests/cases.json"] = json.dumps({"coffee.make": ["сделай кофе"]}, ensure_ascii=False)
    py_files = ext_files(ext_id="ha",
                         extra={"scenarios/custom/ha.light.py": "async def handle(ctx):\n    return None\n"})
    runner, base = await serve({"coffee.zip": zip_bytes(files), "ha.zip": zip_bytes(py_files)})
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            body = await op(client, url, {"op": "install", "url": f"{base}/coffee.zip"})
            assert body["check"]["status"] == "ok"  # JSON-сценарии: dry-run прогон пройден
            body = await op(client, url, {"op": "install", "url": f"{base}/ha.zip",
                                          "confirm": True})
            assert body["check"]["status"] == "warning"  # py-сценарии — до TASK-019
            assert "TASK-019" in body["check"]["detail"]
            assert runtime.settings["extensions"]["installed"]["ha"]["check"]["status"] == "warning"
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()


async def test_selfcheck_failure_is_warning_not_blocker(tmp_path):
    files = ext_files()
    files["tests/cases.json"] = json.dumps({"coffee.make": ["неизвестная расширению фраза"]},
                                           ensure_ascii=False)
    runner, base = await serve({"coffee.zip": zip_bytes(files)})
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            body = await op(client, url, {"op": "install", "url": f"{base}/coffee.zip"})
            assert body["installed"] == "coffee"  # установка не блокируется
            assert body["check"]["status"] == "warning" and "не обработано" in body["check"]["detail"]
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()


# --- python-зависимости и настройки ------------------------------------------------------

async def test_python_packages_and_settings(tmp_path):
    data = manifest()
    data["requires"]["python_packages"] = ["some-package>=1,<2"]
    data["settings"] = {"fields": [{"key": "token", "label": "Токен", "type": "password"}],
                        "defaults": {"token": ""}}
    runner, base = await serve({"coffee.zip": zip_bytes(ext_files(manifest_data=data))})
    runtime = Runtime(tmp_path)
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            await op(client, url, {"op": "install", "url": f"{base}/coffee.zip"})
            body = await op(client, url, {"op": "install_packages", "id": "coffee"})
            assert body == {"requires_confirmation": True,
                            "packages": ["some-package>=1,<2"]}  # молча пакеты не ставятся
            body = await op(client, url, {"op": "set", "id": "coffee", "values": {"token": "abc"}})
            assert body["values"] == {"token": "abc"}
            body = await op(client, url, {"op": "set", "id": "coffee", "values": {"unknown": 1}},
                            expect=400)
            assert "Неизвестные поля" in body["error"]
            listing = await (await client.get(url + "/emulator/api/extensions")).json()
            card = listing["extensions"][0]
            assert card["python_packages"] == ["some-package>=1,<2"]
            assert card["settings"]["values"] == {"token": "abc"}
    finally:
        await app_runner.cleanup()
        await runtime.close()
        await runner.cleanup()


async def test_extensions_state_survives_settings_save_from_old_ui(tmp_path):
    runner, base = await serve({"coffee.zip": zip_bytes(ext_files())})
    runtime = Runtime(tmp_path)
    try:
        app_runner, url = await listen(create_app(runtime=runtime))
        try:
            async with ClientSession() as client:
                await op(client, url, {"op": "install", "url": f"{base}/coffee.zip"})
                await op(client, url, {"op": "enable", "id": "coffee"})
            value = runtime.public()
            del value["extensions"]  # форма UI, открытая до появления ключа
            await runtime.save_settings(value)
            assert runtime.settings["extensions"]["installed"]["coffee"]["version"] == "1.0.0"
            assert runtime.settings["extensions"]["enabled"]["coffee"] is True
        finally:
            await app_runner.cleanup()
    finally:
        await runner.cleanup()
