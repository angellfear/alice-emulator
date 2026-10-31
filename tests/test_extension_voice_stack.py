"""TASK-031: точки подключения голосового стека — пакеты, адаптеры, пресеты расширения.

Расширение-папка (режим разработки, как в tests/test_extensions_manager.py и
tests/test_extension_catalogs.py) даёт слои voice/packages/*.json (пакеты с id
«<ext-id>.<pkg>», origin в API), voice/adapters/*.py (адаптеры с id «<ext-id>.<файл>»,
горячая перезагрузка по mtime — как у своих адаптеров) и voice/presets/*.json
(пресеты «<ext-id>.<id>» — только чтение, «Сделать копию» через существующий
POST voice-stack-preset; один список для «Голосового стека» и «Интеллект → Модель»).
Выключение расширения убирает его пакеты/адаптеры/пресеты из списков; применённый
пресет расширения не роняет карточку — понятная ошибка в GET voice-stack.
"""
import json
import time

import pytest
from aiohttp import ClientSession

from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.runtime import Runtime
from server_emulator.sdk import SDK_VERSION
from server_emulator.server import create_app
from server_emulator.voice_stack import PLATFORM, LocalSpeech
from server_emulator.voice_stack import adapters
from tests.conftest import listen

API_HEADERS = {"X-Emulator": "1"}

ADAPTER_SRC = '''"""Эхо-адаптер расширения: ответ модели и синтез без сети."""

TITLE = "Demo Echo"
DESCRIPTION = "Расширение: фиксированный ответ и синтез-заглушка."
SERVICES = {
    "llm": {"fields": [{"key": "url", "label": "URL", "type": "text"}],
            "defaults": {"url": "http://127.0.0.1:9/v1/chat/completions"}},
    "tts": {"fields": [{"key": "url", "label": "URL", "type": "text"}],
            "defaults": {"url": "http://127.0.0.1:9/v1/audio/speech"}},
}


async def complete(runtime, config, payload):
    return {"choices": [{"message": {"content": "ext-echo"}}], "usage": {}}


async def speech(runtime, config, payload):
    return b"demo-audio", "audio/demo"
'''

PACKAGE = {
    "id": "demo", "service": "tts", "title": "Demo Ext TTS", "description": "Пакет расширения",
    "order": 5, "requirements": {"gpu": False, "vram_gb": 0, "disk_gb": 0.1},
    "adapter": "vsx.echo",
    "config": {"url": "http://127.0.0.1:{port}/v1/audio/speech", "voice": "demo"},
    "fields": [{"key": "port", "label": "Порт", "type": "number", "step": 1}],
    "defaults": {"port": 50777},
    "server": {"script": "tools/none.py", "health": "/health"},
    "platforms": {"windows": {"cpu": [{"pip": ["demo"]}]}, "linux": {"cpu": [{"pip": ["demo"]}]}},
}

PRESET = {"id": "echo", "service": "llm", "title": "Ext Echo", "adapter": "vsx.echo",
          "values": {"url": "http://127.0.0.1:9/v1/chat/completions"}}


def manifest(ext_id="vsx"):
    return {"id": ext_id, "name": "Голосовое расширение", "version": "1.0.0", "description": "тест",
            "author": "T", "license": "MIT",
            "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": f">={SDK_VERSION}"},
            "permissions": {}}


def ext_files(ext_id="vsx"):
    return {
        "voice/adapters/echo.py": ADAPTER_SRC,
        "voice/packages/demo.json": json.dumps(PACKAGE, ensure_ascii=False),
        "voice/presets/echo.json": json.dumps(PRESET, ensure_ascii=False),
    }


def make_extension(runtime, ext_id="vsx", enabled=True):
    """Расширение-папка (dev-режим): manifest + файлы, запись installed/enabled
    в settings; load_enabled() подключает (как в tests/test_extension_catalogs.py)."""
    root = runtime.directory / "ext" / ext_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "extension.json").write_text(json.dumps(manifest(ext_id), ensure_ascii=False),
                                         encoding="utf-8")
    for name, content in ext_files(ext_id).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    state = runtime.settings.setdefault("extensions", {})
    state.setdefault("installed", {})[ext_id] = {"version": "1.0.0", "dev": str(root)}
    state.setdefault("enabled", {})[ext_id] = enabled
    return root


def isolate_speech(runtime, tmp_path):
    """Установка пакетов — не в data/speech репозитория, а во временную папку."""
    runtime.voice_stack.local = LocalSpeech(tmp_path / "speech", runtime.voice_stack.state_dir / "packages")


# --- пакет, адаптер и пресет видны в API ------------------------------------------------

async def test_extension_contributions_visible_in_api(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime)
    runtime.extensions.load_enabled()
    app_runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            data = await (await client.get(url + "/emulator/api/voice-stack",
                                           headers=API_HEADERS)).json()
            package = next(p for p in data["tts"]["packages"] if p["id"] == "vsx.demo")
            assert package["origin"] == "extension:vsx" and package["title"] == "Demo Ext TTS"
            assert package["state"]["status"] == "absent"
            preset = next(p for p in data["llm"]["presets"] if p["id"] == "vsx.echo")
            assert preset["origin"] == "extension:vsx" and preset["readonly"] is True
            assert preset["values"]["url"] == "http://127.0.0.1:9/v1/chat/completions"
            # список llm-пресетов один: его показывают и «Голосовой стек», и «Интеллект → Модель»
            assert any(a["id"] == "vsx.echo" for a in data["llm"]["adapters"])
            assert any(a["id"] == "vsx.echo" for a in data["tts"]["adapters"])
            assert [p["id"] for p in data["llm"]["presets"]] == ["vsx.echo"]  # свои пресеты появятся рядом
    finally:
        await app_runner.cleanup()
        await runtime.close()


# --- адаптер расширения резолвится и работает -------------------------------------------

async def test_extension_adapter_resolves_for_preset_and_package(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime)
    runtime.extensions.load_enabled()
    isolate_speech(runtime, tmp_path)
    try:
        stack = runtime.voice_stack
        module = adapters.get("vsx.echo")
        assert module.TITLE == "Demo Echo" and stack.adapter("llm", "vsx.echo") is module
        # пресет расширения → settings["model"]["adapter"] = "vsx.echo" → конвейер отвечает
        await stack.apply("llm", "p:vsx.echo")
        assert runtime.settings["model"]["adapter"] == "vsx.echo"
        assert stack.active("llm") == "p:vsx.echo"
        raw, content_type = await runtime.provider("model", payload={"messages": [{"role": "user", "content": "привет"}]})
        assert json.loads(raw)["choices"][0]["message"]["content"] == "ext-echo"
        # пакет расширения: конфиг по настройкам пакета резолвится в адаптер расширения
        package = stack.package("vsx.demo")
        stack.local.folder(package).mkdir(parents=True, exist_ok=True)
        (stack.local.folder(package) / "installed.json").write_text("{}", encoding="utf-8")
        config, _ = stack.config("tts", "k:vsx.demo")
        assert config["adapter"] == "vsx.echo"
        assert config["url"] == "http://127.0.0.1:50777/v1/audio/speech"
        resolved = adapters.resolve("tts", config)
        assert resolved is module
        assert await resolved.speech(runtime, config, {}) == (b"demo-audio", "audio/demo")
    finally:
        await runtime.close()
        adapters.configure(tmp_path / "none")


# --- пресет расширения: только чтение, копия через существующий механизм -----------------

async def test_extension_preset_readonly_copy_and_delete(tmp_path):
    runtime = Runtime(tmp_path)
    root = make_extension(runtime)
    runtime.extensions.load_enabled()
    try:
        stack = runtime.voice_stack
        with pytest.raises(ValueError, match="копию"):
            stack.delete_preset("llm", "vsx.echo")
        app_runner, url = await listen(create_app(runtime=runtime))
        try:
            async with ClientSession() as client:
                # «Сделать копию»: POST voice-stack-preset с id пресета расширения
                body = await (await client.post(url + "/emulator/api/voice-stack-preset", headers=API_HEADERS,
                                                json={"service": "llm", "id": "vsx.echo", "adapter": "vsx.echo",
                                                      "title": "Копия эха",
                                                      "values": {"url": "http://127.0.0.1:8/v1/chat/completions"}})).json()
                assert body == {"id": "vsx.echo"}
                data = await (await client.get(url + "/emulator/api/voice-stack", headers=API_HEADERS)).json()
                copy = next(p for p in data["llm"]["presets"] if p["id"] == "vsx.echo")
                assert "origin" not in copy and not copy.get("readonly")  # пользовательская копия перекрыла
                assert copy["values"]["url"] == "http://127.0.0.1:8/v1/chat/completions"
        finally:
            await app_runner.cleanup()
        assert (runtime.directory / "voice_stack" / "presets" / "llm" / "vsx.echo.json").is_file()
        # файл расширения не тронут — правка ушла в state-копию
        assert json.loads((root / "voice/presets/echo.json").read_text(encoding="utf-8"))["title"] == "Ext Echo"
        stack.delete_preset("llm", "vsx.echo")  # копию удалить можно — слой расширения вернулся
        assert stack.preset("llm", "vsx.echo")["readonly"] is True
    finally:
        await runtime.close()
        adapters.configure(tmp_path / "none")


# --- выключение расширения убирает вклад и честно репортит применённый пресет ------------

async def test_disable_removes_contributions_and_reports_active_preset(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime)
    runtime.extensions.load_enabled()
    isolate_speech(runtime, tmp_path)
    try:
        stack = runtime.voice_stack
        await stack.apply("llm", "p:vsx.echo")  # применённый пресет расширения переживёт выключение
        await runtime.extensions.disable("vsx")
        data = await stack.public()  # не падает
        assert not any(p["id"] == "vsx.demo" for p in data["tts"]["packages"])
        assert not any(a["id"] == "vsx.echo" for a in data["llm"]["adapters"])
        assert not any(p["id"] == "vsx.echo" for p in data["llm"]["presets"])
        assert data["llm"]["active"] == ""
        assert "vsx.echo" in data["llm"]["warning"] and "недоступен" in data["llm"]["warning"]
        assert data["asr"].get("warning") is None  # сервисы без применённого источника молчат
        # включение возвращает слои: применённая ссылка оживает без перенастройки
        await runtime.extensions.enable("vsx")
        data = await stack.public()
        assert data["llm"]["active"] == "p:vsx.echo" and "warning" not in data["llm"]
        assert stack.preset("llm", "vsx.echo")["title"] == "Ext Echo"
    finally:
        await runtime.close()
        adapters.configure(tmp_path / "none")


# --- горячая перезагрузка адаптера расширения по mtime ------------------------------------

async def test_extension_adapter_hot_reload(tmp_path):
    runtime = Runtime(tmp_path)
    root = make_extension(runtime)
    runtime.extensions.load_enabled()
    try:
        assert adapters.get("vsx.echo").TITLE == "Demo Echo"
        time.sleep(0.01)  # mtime-разрешение файловой системы
        (root / "voice/adapters/echo.py").write_text(ADAPTER_SRC.replace("Demo Echo", "Demo Echo v2"),
                                                     encoding="utf-8")
        assert adapters.get("vsx.echo").TITLE == "Demo Echo v2"  # без рестарта, как у своих адаптеров
    finally:
        await runtime.close()
        adapters.configure(tmp_path / "none")
