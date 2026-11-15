import asyncio
import time
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

    async def save_settings(self, value):
        self.saved.append(True)
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


def test_speech_dir_from_config_priority(tmp_path, monkeypatch):
    monkeypatch.delenv("ALICA_SPEECH_DIR", raising=False)
    assert voice_stack.speech_dir_from_config({}) == voice_stack.SPEECH_DIR
    assert voice_stack.speech_dir_from_config({"speech_dir": "custom/speech"}) == voice_stack.PROJECT / "custom" / "speech"
    absolute = tmp_path / "abs-speech"
    assert voice_stack.speech_dir_from_config({"speech_dir": str(absolute)}) == absolute
    monkeypatch.setenv("ALICA_SPEECH_DIR", str(tmp_path / "env-speech"))  # приоритет выше настройки
    assert voice_stack.speech_dir_from_config({"speech_dir": str(absolute)}) == tmp_path / "env-speech"


def test_resolve_speech_dir_reads_server_json(tmp_path, monkeypatch):
    from server_emulator import server_config
    monkeypatch.delenv("ALICA_SPEECH_DIR", raising=False)
    state = tmp_path / "state"
    assert voice_stack.resolve_speech_dir(state) == voice_stack.SPEECH_DIR  # нет server.json — значение по умолчанию
    server_config.save(state, {**server_config.DEFAULTS, "speech_dir": str(tmp_path / "custom")})
    assert voice_stack.resolve_speech_dir(state) == tmp_path / "custom"


def test_voice_stack_uses_configured_speech_dir_without_explicit_arg(tmp_path, monkeypatch):
    from server_emulator import server_config
    monkeypatch.delenv("ALICA_SPEECH_DIR", raising=False)
    state = tmp_path / "state"
    configured = tmp_path / "configured-speech"
    server_config.save(state, {**server_config.DEFAULTS, "speech_dir": str(configured)})
    stack = VoiceStack(FakeRuntime(state), tmp_path / "pk")
    assert stack.local.directory == configured


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


def test_save_source_keeps_stream_settings(stack):
    """Смена источника перезаписывает settings["stt"] целиком — настройки «Эмуляции
    потокового режима» (галочка и обе паузы) и выравнивание уровня переносятся."""
    url = "http://127.0.0.1:5015/v1/audio/transcriptions"
    stack.runtime.settings["stt"] = {
        "adapter": "openai", "url": url,
        "pseudo_stream": "yes", "pseudo_partial_pause": 0.6, "pseudo_end_pause": 2.5, "level": "no"}
    asyncio.run(stack.save_source("asr", "k:gigaam", {"adapter": "openai", "url": url}))
    stt = stack.runtime.settings["stt"]
    assert stt["pseudo_stream"] == "yes" and stt["pseudo_partial_pause"] == 0.6
    assert stt["pseudo_end_pause"] == 2.5 and stt["level"] == "no"
    assert stack.runtime.settings["voice_stack"]["asr"] == "k:gigaam"


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


def test_state_of_busy_server_uses_short_probe(stack, monkeypatch):
    """Сервер занят (грузит модель, /health не отвечает) — статус берёт короткую пробу,
    процесс жив: карточка показывает starting, не дожидаясь длинного тайм-аута."""
    package = stack.package("loc")
    stack.local.installed = lambda pkg: True

    class Live:
        def poll(self):
            return None  # процесс жив

    stack.local.processes["loc"] = Live()
    seen = []

    async def probe(pkg, timeout=1.5):
        seen.append(timeout)
        return None  # /health не отвечает

    monkeypatch.setattr(stack.local, "health", probe)
    assert asyncio.run(stack.local.state(package))["status"] == "starting"
    assert seen == [voice_stack.STATE_HEALTH_TIMEOUT] and seen[0] < 1.0


def test_health_short_timeout_of_silent_server(stack):
    """health() с коротким тайм-аутом против сервера, принявшего соединение и молчащего,
    возвращается сам — готовность после запуска ждёт дольше, статус карточки нет."""
    import socket

    with socket.socket() as reserved:  # свободный порт, чтобы не столкнуться с чужим сервером
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    package = stack.package("loc")
    package["defaults"] = {**package["defaults"], "port": port}

    async def scenario():
        server = await asyncio.start_server(lambda reader, writer: asyncio.sleep(10),
                                            "127.0.0.1", port)
        try:
            started = time.perf_counter()
            assert await stack.local.health(package, voice_stack.STATE_HEALTH_TIMEOUT) is None
            assert time.perf_counter() - started < 1.0
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_public_probes_states_in_parallel(stack, monkeypatch):
    """Состояния пакетов в public() собираются параллельно: медленная проба каждого
    пакета не складывается в секунды ожидания вкладки."""
    async def slow(pkg):
        await asyncio.sleep(0.4)
        return {"status": "absent"}

    monkeypatch.setattr(stack.local, "state", slow)
    started = time.perf_counter()
    result = asyncio.run(stack.public())
    assert time.perf_counter() - started < 0.8  # подряд (0.4 × 2 пакета) дало бы ≥0.8 с
    assert {p["id"] for p in result["tts"]["packages"]} == {"loc", "other"}
    assert all(p["state"]["status"] == "absent" for p in result["tts"]["packages"])


# --- статус занятого сервера: порт принимает, /health за короткую пробу не успел -------


async def test_state_busy_server_is_running(tmp_path, stack):
    """/health не ответил за короткую пробу, но порт принимает соединение — сервер
    занят (распознаёт/синтезирует/греет модель): состояние running, а не starting/stopped,
    и ответ не позже ~0.6 с (проба 0.4 с + проверка порта)."""
    from aiohttp import web

    async def health(request):
        await asyncio.sleep(2)
        return web.json_response({"model": "busy"})

    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 59999).start()
    try:
        package = stack.package("loc")
        folder = stack.local.folder(package)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "installed.json").write_text('{"device": "cpu"}', encoding="utf-8")
        started = time.monotonic()
        state = await stack.local.state(package)
        assert time.monotonic() - started <= 0.6
        assert state["status"] == "running"
    finally:
        await runner.cleanup()


@pytest.fixture
def closed_port(stack, monkeypatch):
    """Порт пакета не принимает соединений. Настоящий порт не пробуется: при параллельном
    прогоне его может занять сервер другого теста."""
    async def no_health(*_args):
        return None

    async def refused(*_args):
        return False

    monkeypatch.setattr(stack.local, "health", no_health)
    monkeypatch.setattr(stack.local, "_accepts", refused)


async def test_state_closed_port_is_stopped(tmp_path, stack, closed_port):
    """Порт не принимает соединение — пакет не запущен: stopped, штатно (без журнала)."""
    package = stack.package("loc")
    folder = stack.local.folder(package)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "installed.json").write_text('{"device": "cpu"}', encoding="utf-8")
    state = await stack.local.state(package)
    assert state["status"] == "stopped" and state["crashed"] is False and "log" not in state


# --- журнал только при сбое: признак crashed в состоянии карточки ----------------------


async def test_state_crash_flag(tmp_path, stack, closed_port):
    """Штатная остановка — без журнала (crashed: false); упавший процесс — журнал и
    crashed: true; запускающийся — журнал есть, crashed: false; кода нет, журнал
    кончается трассировкой Python — сбой."""
    from types import SimpleNamespace

    package = stack.package("loc")
    folder = stack.local.folder(package)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "installed.json").write_text('{"device": "cpu"}', encoding="utf-8")

    stack.local.processes["loc"] = SimpleNamespace(poll=lambda: 0)
    state = await stack.local.state(package)
    assert state["status"] == "stopped" and state["crashed"] is False and "log" not in state

    stack.local.processes["loc"] = SimpleNamespace(poll=lambda: 1)
    state = await stack.local.state(package)
    assert state["status"] == "stopped" and state["crashed"] is True and "log" in state

    stack.local.processes["loc"] = SimpleNamespace(poll=lambda: None)
    state = await stack.local.state(package)
    assert state["status"] == "starting" and state["crashed"] is False and "log" in state

    del stack.local.processes["loc"]
    (folder / "server.log").write_text(
        "предупреждение\nTraceback (most recent call last):\n  File \"x\", line 1\nError: бум\n",
        encoding="utf-8")
    state = await stack.local.state(package)
    assert state["status"] == "stopped" and state["crashed"] is True and "log" in state

    (folder / "server.log").write_text("работал и остановлен\n", encoding="utf-8")
    state = await stack.local.state(package)
    assert state["status"] == "stopped" and state["crashed"] is False and "log" not in state


def test_tail_strips_nul_and_ansi(tmp_path, stack):
    """Журнал с NUL-байтами (onnxruntime на Windows пишет предупреждения в stderr
    в UTF-16) и ANSI-цветом — читаемый текст без NUL и ESC-последовательностей."""
    package = stack.package("loc")
    folder = stack.local.folder(package)
    folder.mkdir(parents=True, exist_ok=True)
    raw = (b"\x1b\x00[\x009\x000\x00m\x00[\x00W\x00:\x00o\x00n\x00n\x00x\x00r\x00u\x00n\x00t\x00i\x00m\x00e\x00:"
           b"\x00 \x00M\x00e\x00m\x00c\x00p\x00y\x00T\x00r\x00a\x00n\x00s\x00f\x00o\x00r\x00m\x00e\x00r\x00]\x00"
           b"\x1b\x00[\x00m\x00 \x00o\x00k\x00\n"
           b"\x1b[0;32m" + "готов".encode() + b"\x1b[0m\n")
    (folder / "server.log").write_bytes(raw)
    text = stack.local._tail(package)
    assert "\x00" not in text and "\x1b" not in text
    assert "onnxruntime" in text and "готов" in text
