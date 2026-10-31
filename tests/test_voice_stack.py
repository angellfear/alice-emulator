import asyncio
import copy
import json

import pytest

from server_emulator import voice_stack
from server_emulator.voice_stack import DEFAULT_KEYS, KEEP, PLATFORM, VoiceStack, load_packages
from server_emulator.voice_stack import adapters
from server_emulator.voice_stack.adapters import anthropic


class FakeRuntime:
    def __init__(self, directory):
        self.directory = directory
        self.settings = copy.deepcopy(DEFAULT_KEYS)
        self.saved = []

    async def save_settings(self, value, preserve_mode=False):
        self.saved.append(preserve_mode)
        self.settings = value


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


PACKAGE = {"id": "loc", "service": "tts", "title": "Loc", "adapter": "openai",
           "config": {"url": "http://127.0.0.1:{port}/v1/audio/speech", "voice": "{voice}", "response_format": "pcm_f32le"},
           "fields": [{"key": "voice", "label": "V", "type": "select", "options": ["a", "b"]},
                      {"key": "device", "label": "D", "type": "select", "options": ["cpu", "cuda"]},
                      {"key": "port", "label": "P", "type": "number", "step": 1}],
           "defaults": {"voice": "a", "device": "cpu", "port": 59999},
           "server": {"script": "tools/x.py"}, "platforms": {PLATFORM: {"cpu": [{"pip": ["x"]}]}},
           "reinstall_on": ["voice"]}


@pytest.fixture
def stack(tmp_path):
    write(tmp_path / "pk" / "loc.json", PACKAGE)
    write(tmp_path / "pk" / "bad.json", "{")
    write(tmp_path / "pk" / "other-os.json", {**PACKAGE, "id": "other", "platforms": {"plan9": {"cpu": []}}})
    yield VoiceStack(FakeRuntime(tmp_path / "state"), tmp_path / "pk", speech_dir=tmp_path / "speech")
    adapters.configure(tmp_path / "none")


def test_shipped_packages_and_adapters_are_valid():
    packages = load_packages()
    assert {p["id"] for p in packages["asr"]} >= {"sherpa-t-one"}
    assert {p["id"] for p in packages["tts"]} >= {"piper", "f5-tts"}
    for items in packages.values():
        for package in items:
            assert {"windows", "linux"} <= set(package["platforms"])
            assert package["service"] in adapters.get(package["adapter"]).SERVICES
    assert {"openai", "anthropic", "stream", "stub"} <= set(adapters.available())


def test_resolve_defaults():
    assert adapters.resolve("stt", {"url": "http://h/asr/stream"}).__name__.endswith(".stream")
    assert adapters.resolve("stt", {"url": "http://h/v1/audio/transcriptions"}).__name__.endswith(".openai")
    assert adapters.resolve("model", {"adapter": "anthropic"}).__name__.endswith(".anthropic")
    with pytest.raises(ValueError):
        adapters.resolve("tts", {"adapter": "anthropic"})  # anthropic не умеет tts


def test_user_adapter_from_state(stack):
    write(stack.state_dir / "adapters" / "mine.py",
          'TITLE = "Мой"\nSERVICES = {"llm": {"fields": [], "defaults": {}}}\n'
          'async def complete(runtime, config, payload):\n    return {"choices": [{"message": {"content": "ok"}}]}\n')
    module = adapters.get("mine")
    assert asyncio.run(module.complete(None, {}, {}))["choices"][0]["message"]["content"] == "ok"


def test_packages_skip_broken_and_mark_unsupported(stack):
    assert [p["id"] for p in stack.packages()["tts"]] == ["loc", "other"]
    result = asyncio.run(stack.public())
    states = {p["id"]: p["state"]["status"] for p in result["tts"]["packages"]}
    assert states == {"loc": "absent", "other": "unsupported"}
    device = next(f for p in result["tts"]["packages"] if p["id"] == "loc" for f in p["fields"] if f["key"] == "device")
    assert device["options"] == ["cpu"]  # cuda нет в platforms для этой ОС


def test_migrate_current_settings_to_preset(stack):
    stack.runtime.settings["model"].update(url="http://h/v1/chat/completions", model="cur", api_key="secret")
    result = asyncio.run(stack.public())
    preset = result["llm"]["presets"][0]
    assert result["llm"]["active"] == "p:" + preset["id"] and preset["adapter"] == "openai"
    assert preset["values"]["api_key"] == KEEP and stack.preset("llm", preset["id"])["values"]["api_key"] == "secret"
    assert stack.runtime.settings["model"]["adapter"] == "openai"
    assert result["asr"]["active"] == ""  # пустой URL — пресет не создаётся


def test_preset_apply_writes_adapter_and_keeps_key(stack):
    item = stack.save_preset("llm", "anthropic", "Claude", {"api_key": "k1", "model": "claude-haiku-5-5", "max_tokens": "300", "evil": 1})
    asyncio.run(stack.apply("llm", "p:" + item["id"]))
    model = stack.runtime.settings["model"]
    assert model["adapter"] == "anthropic" and model["api_key"] == "k1" and model["max_tokens"] == 300
    assert "evil" not in model and model["stub_text"] == "" and stack.runtime.saved[-1] is True
    stack.save_preset("llm", "anthropic", "Claude", {"api_key": KEEP, "model": "claude-opus-5-5"}, item["id"])
    assert stack.preset("llm", item["id"])["values"]["api_key"] == "k1"
    with pytest.raises(ValueError):
        stack.delete_preset("llm", item["id"])  # применённый
    with pytest.raises(ValueError):
        stack.save_preset("tts", "anthropic", "x", {})  # адаптер не умеет tts
    with pytest.raises(ValueError):
        stack.save_preset("llm", "openai", " ", {})


def test_package_options_before_install_and_after_uninstall(stack):
    package = stack.package("loc")
    stack.local.save_options(package, {"voice": "b", "port": "60001"})
    assert stack.local.options(package) == {"voice": "b", "device": "cpu", "port": 60001}
    with pytest.raises(ValueError):
        stack.local.save_options(package, {"device": "cuda"})  # нет в platforms
    with pytest.raises(ValueError):
        asyncio.run(stack.apply("tts", "k:loc"))  # не установлен
    write(stack.local.folder(package) / "installed.json", {"voice": "a"})
    assert stack.local.outdated(package) == ["voice"]
    config, _ = stack.config("tts", "k:loc")
    assert config["adapter"] == "openai" and config["voice"] == "b"
    assert config["url"] == "http://127.0.0.1:60001/v1/audio/speech" and config["api_key"] == ""
    stack.local.uninstall(package)
    assert not stack.local.folder(package).exists() and stack.local.options(package)["port"] == 60001


def test_active_ignores_dangling_refs(stack):
    stack.runtime.settings["voice_stack"] = {"llm": "p:gone", "tts": "k:nope", "asr": "x"}
    assert [stack.active(s) for s in ("llm", "tts", "asr")] == ["", "", ""]


def test_anthropic_conversion_roundtrip():
    payload = {"model": "m", "temperature": 0.2, "messages": [
        {"role": "system", "content": "sys"}, {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function",
                                                             "function": {"name": "light", "arguments": '{"on": true}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"}, {"role": "user", "content": "ещё"}],
        "tools": [{"type": "function", "function": {"name": "light", "description": "d", "parameters": {"type": "object"}}}]}
    body = anthropic.to_anthropic(payload, {"model": "claude-haiku-5-5", "max_tokens": 100})
    assert body["system"] == "sys" and body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 100
    assert body["messages"][1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "light", "input": {"on": True}}
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    reply = anthropic.from_anthropic({"content": [{"type": "text", "text": "Готово"},
                                                  {"type": "tool_use", "id": "t2", "name": "light", "input": {"on": False}}],
                                      "usage": {"input_tokens": 5, "output_tokens": 3}})
    message = reply["choices"][0]["message"]
    assert message["content"] == "Готово" and json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"on": False}
    assert reply["usage"] == {"prompt_tokens": 5, "completion_tokens": 3}


def test_runtime_chat_goes_through_adapter(tmp_path):
    from server_emulator.runtime import Runtime
    runtime = Runtime(tmp_path)
    calls = []

    async def complete(rt, config, payload):
        calls.append((config["adapter"], payload["messages"][-1]["content"]))
        return {"choices": [{"message": {"content": "ответ"}}], "usage": {}}

    original = anthropic.complete
    anthropic.complete = complete
    try:
        runtime.settings["model"].update(adapter="anthropic", api_key="k")
        result = asyncio.run(runtime.chat("вопрос"))
    finally:
        anthropic.complete = original
    assert result["text"] == "ответ" and calls == [("anthropic", "вопрос")]
    assert voice_stack.SETTINGS_KEY["llm"] == "model"


def _wav(seconds=3.0, rate=16000, tag=1):
    import struct
    frames = int(seconds * rate)
    data = b"\0\0" * frames
    fmt = struct.pack("<HHIIHH", tag, 1, rate, rate * 2, 2, 16)
    return b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + \
        b"data" + struct.pack("<I", len(data)) + data


def test_wav_validation():
    from server_emulator.voice_stack import wav_info
    assert wav_info(_wav(3.0)) == {"seconds": 3.0, "rate": 16000, "channels": 1, "bits": 16}
    for bad in (b"ID3" + b"\0" * 60, _wav(1.0), _wav(40.0), _wav(3.0, tag=85)):
        with pytest.raises(ValueError):
            wav_info(bad)


def test_reference_upload_one_file_per_field(stack):
    package = {**PACKAGE, "fields": PACKAGE["fields"] + [{"key": "ref", "label": "R", "type": "file"}]}
    write(stack.packages_dir / "loc.json", package)
    package = stack.package("loc")
    assert stack.local.upload(package, "ref", _wav(3.0))["seconds"] == 3.0
    stack.local.upload(package, "ref", _wav(5.0))  # замена, а не второй файл
    assert stack.local.files(package)["ref"]["seconds"] == 5.0
    assert len(list((stack.local.options_dir / "loc").glob("*.wav"))) == 1
    with pytest.raises(ValueError):
        stack.local.upload(package, "voice", _wav(3.0))  # не поле-файл
    stack.local.save_options(package, {"ref": ""})  # форма со старым пустым значением путь не затирает
    assert stack.local.files(package)["ref"]["seconds"] == 5.0 and "loc" in stack.local.dirty
    (stack.local.options_dir / "loc" / "ref.0123abcd.pt").write_bytes(b"baked")
    stack.local.remove_file(package, "ref")
    assert stack.local.files(package) == {} and stack.local.options(package)["ref"] == ""
    assert not list((stack.local.options_dir / "loc").glob("*.pt"))


def test_sound_cache_index_and_regenerate(tmp_path):
    import numpy as np
    from server_emulator.tts_cache import TtsCache, engine_identity

    class Engine:
        def __init__(self, voice):
            self.voice, self.calls = voice, []

        async def synth_pcm(self, text):
            self.calls.append(text)
            if text == "сбой":
                raise RuntimeError("boom")
            return np.full(1600, 0.1, dtype=np.float32), 16000

    cache, first = TtsCache(tmp_path), Engine("a")
    asyncio.run(cache.synth(first, ["Включаю."]))
    assert cache.texts() == ["Включаю."] and TtsCache(tmp_path).texts() == ["Включаю."]  # индекс на диске
    second = Engine("b")
    assert not cache.cached(engine_identity(second), "Включаю.")

    async def run():
        cache.regenerate(second, ["Включаю.", "Готово.", "сбой", "Готово."])
        await cache._task
    asyncio.run(run())
    assert second.calls == ["Включаю.", "Готово.", "сбой"]
    assert cache.job["state"] == "done" and cache.job["done"] == 3 and cache.job["errors"] == 1
    assert cache.job["items"] == {"Включаю.": "done", "Готово.": "done", "сбой": "error"}
    assert cache.cached(engine_identity(second), "Готово.")


def test_scenario_phrases_skip_variables_and_tags():
    from server_emulator.ui.api.sound_cache import scenario_phrases
    scenarios = [{"id": "s", "title": {"ru": "Погода"}, "flow": [
        {"reply": {"text": "Сейчас {{t}}", "speech_prefix": "<speaker x> ", "speech_parts": ["Сейчас", "{{t}}", "<b>градусов</b>"]}},
        {"reply": {"text": "Включаю."}},
        {"reply": {"text": "Свет", "speech": "Свет  включён."}},
        {"reply": {"text": "Привет, {{user.name}}"}},
        {"question": "На какое время?", "message": "Не понял {{x}}"},
        {"when": {"then": [{"reply": {"text": "Вложенный ответ."}}]}}]}]
    # speech_parts с переменной — фраза целиком мимо кеша; без переменных — одна фраза
    assert scenario_phrases(scenarios) == [(t, "Погода", "s") for t in (
        "Включаю.", "Свет включён.", "На какое время?", "Вложенный ответ.")]
    assert scenario_phrases([{"id": "x", "title": "X", "flow": [{"reply": {"speech_parts": ["Готово.", "<b>Жду</b>"]}}]}])         == [("Готово. Жду", "X", "x")]


def test_library_scenarios_give_phrases():
    from server_emulator.scenario_engine.library import SCENARIOS
    from server_emulator.ui.api.sound_cache import scenario_phrases
    texts = [t for t, _, _ in scenario_phrases(SCENARIOS.values())]
    assert "Выключаю." in texts and not any("{{" in t for t in texts)


def test_cache_cleanup_removes_gone_static_keeps_dynamic(tmp_path):
    import numpy as np
    from server_emulator.tts_cache import TtsCache, engine_identity

    class Engine:
        voice = "a"

        async def synth_pcm(self, text):
            return np.full(160, 0.1, dtype=np.float32), 16000

    cache, engine, static = TtsCache(tmp_path), Engine(), {"Включаю.", "Старая фраза."}
    cache.is_static = lambda text: text in static
    asyncio.run(cache.synth(engine, ["Включаю.", "Старая фраза.", "+12"]))
    assert cache.texts() == ["Включаю.", "Старая фраза."]  # +12 — значение переменной, в список не идёт
    assert cache.cleanup({"Включаю."}) == ["Старая фраза."]
    identity = engine_identity(engine)
    assert not cache.cached(identity, "Старая фраза.") and cache.cached(identity, "Включаю.")
    assert cache.cached(identity, "+12")  # динамическое не удаляется как «лишнее»
    assert TtsCache(tmp_path).texts() == ["Включаю."]


def test_cache_drop_dynamic_keeps_static(tmp_path):
    import numpy as np
    from server_emulator.tts_cache import TtsCache, engine_identity

    class Engine:
        voice = "a"

        async def synth_pcm(self, text):
            return np.full(160, 0.1, dtype=np.float32), 16000

    cache, engine = TtsCache(tmp_path), Engine()
    cache.is_static = lambda text: text == "Включаю."
    asyncio.run(cache.synth(engine, ["Включаю.", "+12"]))
    identity = engine_identity(engine)
    assert cache.drop_dynamic() == 1
    assert cache.cached(identity, "Включаю.") and not cache.cached(identity, "+12")
    assert all(not item.get("dynamic") for item in TtsCache(tmp_path).index.values())
