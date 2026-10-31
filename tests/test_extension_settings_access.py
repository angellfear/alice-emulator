"""TASK-053: настройки расширения в его коде (ExtensionContext).

Режим (ctx.extension), действие (execution.extension) и адаптер голосового стека
(аргумент extension, если функция его принимает — проверка сигнатуры) читают
значения настроек своего расширения: defaults манифеста + сохранённые, строки
env:ИМЯ раскрыты через auth.secret, как секреты провайдеров. Контекст создаётся
заново на каждый вызов — правка настроек видна без рестарта; второе расширение
чужие настройки не видит; встроенное действие получает execution.extension is
None; прежний адаптер без параметра extension вызывается как раньше; repr/str
маскируют строковые значения.
"""
import inspect
import json

import pytest

from server_emulator import sdk as sdk_module
from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.extensions.context import ExtensionContext
from server_emulator.runtime import Runtime
from server_emulator.sdk import SDK_VERSION, ScenarioContext
from server_emulator.voice_stack import adapters, pcm_tts
from tests.conftest import CTX

TOKEN_ENV = "TEST_EXTENSION_SETTINGS_TOKEN"
SECRET = "секрет-из-окружения"

MODE_SRC = '''"""Режим расширения: отвечает значением настройки (TASK-053)."""
MODE = {"id": "echo", "title": "Эхо"}


async def answer(ctx, text, history):
    ext = ctx.extension
    return {"text": f"{ext.get('greeting')}: {text}", "tool_calls": [], "usage": {},
            "ext_id": ext.id, "token": ext.get("token"),
            "missing": ext.get("нет-поля", "нет"), "kind": type(ext.settings).__name__}
'''

ADAPTER_EXT_SRC = '''"""TTS-адаптер расширения: возвращает прочитанную настройку (TASK-053)."""
SERVICES = {
    "tts": {"fields": [{"key": "url", "label": "URL", "type": "text"}],
            "defaults": {"url": "http://127.0.0.1:9/v1/audio/speech"}},
}


def streams(config):
    return True


async def synth_pcm(runtime, config, text, extension=None):
    return (extension.get("greeting") if extension else None,
            extension.get("token", "нет") if extension else None)
'''

ADAPTER_LEGACY_SRC = '''"""Прежний адаптер расширения: без параметра extension."""
SERVICES = {
    "tts": {"fields": [{"key": "url", "label": "URL", "type": "text"}],
            "defaults": {"url": "http://127.0.0.1:9/v1/audio/speech"}},
}


def streams(config):
    return True


async def synth_pcm(runtime, config, text):
    return ("прежний-адаптер", None)
'''

DEFAULT_FIELDS = [{"key": "greeting", "label": "Приветствие", "type": "text"},
                  {"key": "token", "label": "Токен", "type": "password"}]
DEFAULT_VALUES = {"greeting": "Привет", "token": f"env:{TOKEN_ENV}"}


def action_src(marker, ref, name="brew"):
    """Модуль actions/*.py: пишет в marker, что действие видел в execution.extension."""
    path = str(marker).replace("\\", "/")
    return (
        "import json\n"
        "from pathlib import Path\n\n"
        f'MARKER = Path("{path}")\n\n'
        f"async def {name}(arguments, execution):\n"
        "    ext = execution.extension\n"
        "    seen = {\n"
        '        "id": ext.id if ext else None,\n'
        '        "greeting": ext.get("greeting") if ext else None,\n'
        '        "token": ext.get("token", "нет") if ext else None,\n'
        '        "kind": type(ext.settings).__name__ if ext else None,\n'
        "    }\n"
        '    calls = json.loads(MARKER.read_text(encoding="utf-8")) if MARKER.exists() else []\n'
        "    calls.append(seen)\n"
        '    MARKER.write_text(json.dumps(calls, ensure_ascii=False), encoding="utf-8")\n'
        "    return seen\n\n\n"
        f'ACTIONS = {{"{ref}": {name}}}\n'
    )


def make_extension(runtime, ext_id="coffee", marker=None, extra=None, permissions=None,
                   fields=None, defaults=None):
    """Расширение-папка (режим разработки, как в tests/test_extension_actions.py):
    режим, действие и адаптер, каждый возвращает прочитанную настройку."""
    marker = marker or runtime.directory / f"{ext_id}-actions.json"
    root = runtime.directory / "ext" / ext_id
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "modes/echo.py": MODE_SRC,
        "actions/brew.py": action_src(marker, f"{ext_id}.brew"),
        "voice/adapters/speak.py": ADAPTER_EXT_SRC,
    }
    files.update(extra or {})
    manifest = {
        "id": ext_id, "name": ext_id, "version": "1.0.0", "description": "тест",
        "author": "T", "license": "MIT",
        "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": f">={SDK_VERSION}"},
        "permissions": permissions or {},
        "settings": {"fields": DEFAULT_FIELDS if fields is None else fields,
                     "defaults": DEFAULT_VALUES if defaults is None else defaults},
    }
    (root / "extension.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    state = runtime.settings.setdefault("extensions", {})
    state.setdefault("installed", {})[ext_id] = {"version": "1.0.0", "dev": str(root)}
    state.setdefault("enabled", {})[ext_id] = True
    return root


# --- режим: ctx.extension ------------------------------------------------------------


async def test_mode_reads_own_settings_and_env_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    runtime = Runtime(tmp_path)
    make_extension(runtime)
    runtime.extensions.load_enabled()
    runtime.settings["intelligence"] = "ext:coffee.echo"
    runtime.settings["model"]["stub_text"] = ""
    try:
        answer = await runtime.chat("привет")
        assert answer["source"] == "coffee.echo"
        assert answer["text"] == "Привет: привет"  # значение настройки из defaults
        assert answer["ext_id"] == "coffee"
        assert answer["token"] == SECRET  # env:ИМЯ раскрыт, как у секретов провайдеров
        assert answer["missing"] == "нет"  # get(key, default)
        assert answer["kind"] == "mappingproxy"  # settings — только чтение
    finally:
        await runtime.close()


# --- действие: execution.extension ----------------------------------------------------


async def test_action_reads_own_settings_and_env_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    runtime = Runtime(tmp_path)
    marker = runtime.directory / "brew.json"
    make_extension(runtime, marker=marker)
    runtime.extensions.load_enabled()
    try:
        ctx = ScenarioContext(runtime, dict(CTX))
        seen = await ctx.action("coffee.brew", size=2)
        assert seen == {"id": "coffee", "greeting": "Привет", "token": SECRET,
                        "kind": "mappingproxy"}
        assert json.loads(marker.read_text(encoding="utf-8")) == [seen]
    finally:
        await runtime.close()


async def test_edited_settings_visible_on_next_call(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    runtime = Runtime(tmp_path)
    marker = runtime.directory / "brew.json"
    make_extension(runtime, marker=marker)
    runtime.extensions.load_enabled()
    try:
        ctx = ScenarioContext(runtime, dict(CTX))
        await ctx.action("coffee.brew")
        await runtime.extensions.set_values("coffee", {"greeting": "Свежее приветствие"})
        seen = await ctx.action("coffee.brew")
        assert seen["greeting"] == "Свежее приветствие"  # новый контекст на каждый вызов
        assert seen["token"] == SECRET  # несохранённое поле — из defaults манифеста
        calls = json.loads(marker.read_text(encoding="utf-8"))
        assert [call["greeting"] for call in calls] == ["Привет", "Свежее приветствие"]
    finally:
        await runtime.close()


async def test_second_extension_sees_only_own_settings(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    runtime = Runtime(tmp_path)
    coffee_marker = runtime.directory / "coffee-actions.json"
    tea_marker = runtime.directory / "tea-actions.json"
    make_extension(runtime, ext_id="coffee", marker=coffee_marker)
    make_extension(runtime, ext_id="tea", marker=tea_marker,
                   fields=[{"key": "greeting", "label": "Приветствие", "type": "text"}],
                   defaults={"greeting": "Чай"})
    runtime.extensions.load_enabled()
    try:
        ctx = ScenarioContext(runtime, dict(CTX))
        tea = await ctx.action("tea.brew")
        coffee = await ctx.action("coffee.brew")
        assert tea == {"id": "tea", "greeting": "Чай", "token": "нет",
                       "kind": "mappingproxy"}  # поля coffee у tea нет
        assert coffee == {"id": "coffee", "greeting": "Привет", "token": SECRET,
                          "kind": "mappingproxy"}
    finally:
        await runtime.close()


async def test_action_outside_prefix_owned_via_declaration(tmp_path, monkeypatch):
    """Имя вне префикса, объявленное в permissions.actions (TASK-033), — контекст
    расширения-владельца всё равно передаётся."""
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    runtime = Runtime(tmp_path)
    marker = runtime.directory / "extra.json"
    make_extension(runtime, marker=marker,
                   extra={"actions/extra.py": action_src(marker, "extra.call", name="call")},
                   permissions={"actions": ["extra.call"]})
    runtime.extensions.load_enabled()
    try:
        seen = await ScenarioContext(runtime, dict(CTX)).action("extra.call")
        assert seen["id"] == "coffee" and seen["token"] == SECRET
    finally:
        await runtime.close()


async def test_builtin_action_receives_none_extension(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    runtime = Runtime(tmp_path)
    make_extension(runtime)
    runtime.extensions.load_enabled()
    try:
        real = runtime.scenario_executor.actions["timer.cancel"]
        seen = []

        def spy(arguments, execution):  # заглушка поверх встроенной, как в tts_cache.is_static
            seen.append(execution.extension)
            return real(arguments, execution)

        runtime.scenario_executor.actions["timer.cancel"] = spy
        ctx = ScenarioContext(runtime, dict(CTX))
        await ctx.action("timer.cancel", kind="timer")
        assert seen == [None]  # встроенному действию контекста расширения нет
        assert (await ctx.action("coffee.brew"))["id"] == "coffee"  # соседнее — своё
    finally:
        await runtime.close()


# --- адаптер голосового стека: аргумент extension -------------------------------------


async def test_adapter_receives_extension_and_env_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    runtime = Runtime(tmp_path)
    make_extension(runtime)
    runtime.extensions.load_enabled()
    runtime.settings["tts"] = {"adapter": "coffee.speak", "url": "http://127.0.0.1:9/v1/audio/speech"}
    try:
        engine = pcm_tts(runtime)  # PCM-движок звукового кеша вызывает адаптер через call_adapter
        assert engine is not None and engine.adapter == "coffee.speak"
        greeting, token = await engine.synth_pcm("текст")
        assert greeting == "Привет" and token == SECRET
    finally:
        await runtime.close()
        adapters.configure(tmp_path / "none")  # слои расширения не утекают в другие тесты


async def test_legacy_adapter_without_extension_param_works(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime, extra={"voice/adapters/legacy.py": ADAPTER_LEGACY_SRC})
    runtime.extensions.load_enabled()
    runtime.settings["tts"] = {"adapter": "coffee.legacy", "url": "http://127.0.0.1:9/v1/audio/speech"}
    try:
        engine = pcm_tts(runtime)
        assert engine is not None
        assert await engine.synth_pcm("текст") == ("прежний-адаптер", None)  # как раньше, без kwarg
        assert "extension" not in inspect.signature(  # встроенные тоже принимают только прежние аргументы
            adapters.get("openai").synth_pcm).parameters
    finally:
        await runtime.close()
        adapters.configure(tmp_path / "none")


ADAPTER_PIPELINE_SRC = '''"""Адаптер расширения для всех трёх сервисов: отвечает настройкой расширения."""
SERVICES = {
    "asr": {"fields": [], "defaults": {}},
    "tts": {"fields": [], "defaults": {}},
    "llm": {"fields": [], "defaults": {}},
}


def mark(extension):
    return (extension.get("greeting") if extension else "без-контекста") + "/" + (
        extension.get("token", "нет") if extension else "нет")


def streams(config):
    return True


async def transcribe(runtime, config, audio, filename, extension=None):
    return mark(extension)


async def speech(runtime, config, payload, extension=None):
    return mark(extension).encode(), "text/plain"


async def complete(runtime, config, payload, extension=None):
    return {"choices": [{"message": {"content": mark(extension)}}]}


async def recognize_stream(runtime, config, chunks, fmt, on_partial=None, extension=None):
    return mark(extension)


async def speech_stream(runtime, config, text, extension=None):
    yield mark(extension).encode()
'''


async def test_pipeline_calls_pass_extension(tmp_path, monkeypatch):
    """Конвейер (ProviderManager): распознавание, синтез, модель и потоковые режимы —
    адаптер расширения получает свой ExtensionContext (TASK-053, все пути вызова)."""
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    runtime = Runtime(tmp_path)
    make_extension(runtime, extra={"voice/adapters/pipeline.py": ADAPTER_PIPELINE_SRC})
    runtime.extensions.load_enabled()
    for kind in ("stt", "tts", "model"):
        runtime.settings[kind] = {"adapter": "coffee.pipeline"}
    expected = "Привет/" + SECRET
    manager = runtime.provider_manager
    try:
        body, _ = await manager.call(runtime, "stt", audio=b"RIFF")
        assert json.loads(body)["text"] == expected
        assert await manager.call(runtime, "tts", payload={"input": "текст"}) == (expected.encode(), "text/plain")
        body, _ = await manager.call(runtime, "model", payload={"messages": []})
        assert json.loads(body)["choices"][0]["message"]["content"] == expected
        assert await manager.recognize_stream(runtime, [], "pcm") == expected
        assert [chunk async for chunk in manager.speech_stream(runtime, "текст")] == [expected.encode()]
    finally:
        await runtime.close()
        adapters.configure(tmp_path / "none")


# --- ExtensionContext: только чтение, маскирование, границы ---------------------------


async def test_context_readonly_and_repr_masks_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    runtime = Runtime(tmp_path)
    make_extension(runtime)
    runtime.extensions.load_enabled()
    try:
        ctx = runtime.extensions.context("coffee")
        assert ctx.id == "coffee"
        assert ctx.get("token") == SECRET and ctx.get("greeting") == "Привет"
        assert repr(ctx) == str(ctx)
        assert SECRET not in repr(ctx) and "Привет" not in repr(ctx)  # секреты не утекают
        assert "***" in repr(ctx)
        with pytest.raises(TypeError):
            ctx.settings["greeting"] = "правка"
        with pytest.raises(AttributeError):
            ctx.id = "другое"
        # тип экспортируется SDK для аннотаций; версия поднята минорно (1.1.0 → 1.2.0)
        assert sdk_module.ExtensionContext is ExtensionContext and SDK_VERSION == "1.2.0"
        # владелец действия: встроенное и чужое — None
        assert runtime.extensions.action_context("timer.start") is None
        assert runtime.extensions.action_context("tea.brew") is None
        assert runtime.extensions.action_context("coffee.brew") is not None
    finally:
        await runtime.close()


def test_sdk_secret_expands_env_reference(monkeypatch):
    """sdk.secret — публичное раскрытие env:ИМЯ для api_key пресета адаптера расширения."""
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    assert sdk_module.secret(f"env:{TOKEN_ENV}") == SECRET
    assert sdk_module.secret("env:TEST_EXTENSION_SETTINGS_NO_SUCH_VAR") == ""
    assert sdk_module.secret("plain-token") == "plain-token"
    assert sdk_module.secret(None) == ""
