"""Поля поставщиков голосового стека: метаданные для UI, секреты, список голосов, режимы голоса и запекание."""
import asyncio
import copy
import hashlib
import json

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from server_emulator import voice_stack
from server_emulator.voice_stack import KEEP, PLATFORM, VoiceStack, adapters, validate_fields, validate_package
from tests.ru_stubs import wav

FAKE = '''
TITLE = "Фиктивный (Online)"
DESCRIPTION = "Тест"
SERVICES = {"tts": {
    "title": "Фиктивный синтез (Online)", "voice_modes": ["preset", "baked", "clone"], "bake": {"seconds": [1, 30]},
    "fields": [{"key": "api_key", "label": "Ключ", "type": "password", "required": True, "link": "https://example.test/keys"},
               {"key": "secret_key", "label": "Секрет", "type": "password"},
               {"key": "voice", "label": "Голос", "type": "select", "options_from": "voices",
                "options": [{"value": "default", "label": "По умолчанию"}], "when": {"voice_mode": "preset"}},
               {"key": "ref_audio", "label": "Эталон", "type": "file", "when": {"voice_mode": "clone"}},
               {"key": "speed", "label": "Скорость", "type": "number", "step": 0.1, "group": "advanced"}],
    "defaults": {"voice": "default", "voice_mode": "preset"}}}
CALLS = []
REFRESH = []

async def voices(runtime, config, refresh=False):
    CALLS.append(config["api_key"])
    REFRESH.append(bool(config.get("refresh")))
    if config["api_key"] == "boom":
        raise RuntimeError("http://secret-host/?key=boom")
    if config["api_key"] == "bad":
        raise ValueError("Неверный ключ")
    return [{"value": "v1", "label": "Голос 1", "lang": "ru", "gender": "f", "extra": 1}, {"label": "без value"}]

async def bake_voice(runtime, config, audio, text):
    CALLS.append(("bake", len(audio), text, config["api_key"]))
    return {"voice_id": "vid-1", "title": "Мой голос"}

async def unbake_voice(runtime, config, voice_id):
    CALLS.append(("unbake", voice_id))
'''


class FakeRuntime:
    def __init__(self, directory):
        self.directory = directory
        self.settings = copy.deepcopy(voice_stack.DEFAULT_KEYS)
        self.lifecycle = None

    async def save_settings(self, value):
        self.settings = value


@pytest.fixture
def stack(tmp_path):
    (tmp_path / "state" / "voice_stack" / "adapters").mkdir(parents=True)
    (tmp_path / "state" / "voice_stack" / "adapters" / "fakeco.py").write_text(FAKE, encoding="utf-8")
    yield VoiceStack(FakeRuntime(tmp_path / "state"), speech_dir=tmp_path / "speech")
    adapters.configure(tmp_path / "none")


def fake_calls(stack):
    return adapters.get("fakeco").CALLS


# --- метаданные в API ------------------------------------------------------------------------
def test_api_gives_field_metadata_and_modes(stack):
    result = asyncio.run(stack.public())
    kinds = {k["id"]: k for k in result["tts"]["adapters"]}
    fake, yandex = kinds["fakeco"], kinds["yandex"]
    assert fake["title"] == "Фиктивный синтез (Online)" and fake["voice_modes"] == ["preset", "baked", "clone"]
    assert fake["bake"] == {"seconds": [1, 30], "available": True} and fake["voices"] is True
    key = next(f for f in fake["fields"] if f["key"] == "api_key")
    assert key["type"] == "password" and key["required"] and key["link"].startswith("https://")
    assert any(f.get("group") == "advanced" for f in fake["fields"])
    assert any(f.get("help") and f["key"] == "api_key" for f in yandex["fields"]) and yandex["voice_modes"] == ["preset"]
    assert "bake" not in yandex
    packages = {p["id"]: p for p in result["tts"]["packages"]}
    assert packages["omnivoice"]["voice_modes"] == ["preset", "baked"] and packages["f5-tts"]["voice_modes"] == ["clone"]
    assert packages["omnivoice"]["bake"]["baked"] is False
    piper = {f["key"]: f for f in packages["piper"]["fields"]}
    assert piper["port"]["group"] == "advanced" and "group" not in piper["voice"]
    asr = {p["id"]: p for p in result["asr"]["packages"]}["sherpa-t-one"]
    assert {f["key"] for f in asr["fields"] if f.get("group") == "advanced"} >= {"port", "threads", "workers", "priority"}


def test_every_adapter_secret_field_is_password_with_link():
    for identifier in ("yandex", "sber", "tbank"):
        module = adapters.get(identifier)
        for service, spec in module.SERVICES.items():
            validate_fields(spec["fields"])
            secrets = [f for f in spec["fields"] if f["key"] in ("api_key", "secret_key")]
            assert secrets and all(f["type"] == "password" and f.get("link") and f.get("required") for f in secrets)


def test_validate_field_metadata():
    validate_fields([{"key": "a", "type": "text", "group": "advanced", "options_from": "voices"}])
    with pytest.raises(ValueError):
        validate_fields([{"key": "a", "type": "text", "group": "hidden"}])
    with pytest.raises(ValueError):
        validate_fields([{"key": "a", "type": "text", "options_from": "disk"}])


# --- секреты ---------------------------------------------------------------------------------
def test_secret_fields_are_never_returned_open(stack):
    item = stack.save_preset("tts", "tbank", "T", {"api_key": "test-token", "secret_key": "test-secret", "voice": "alyona"})
    shown = next(p for p in asyncio.run(stack.public())["tts"]["presets"] if p["id"] == item["id"])
    assert shown["values"]["api_key"] == KEEP and shown["values"]["secret_key"] == KEEP and shown["values"]["voice"] == "alyona"
    assert "test-token" not in json.dumps(asyncio.run(stack.public())) and "test-secret" not in json.dumps(asyncio.run(stack.public()))
    stack.save_preset("tts", "tbank", "T", {"api_key": KEEP, "secret_key": "new-secret", "voice": "maxim"}, item["id"])
    values = stack.preset("tts", item["id"])["values"]
    assert values["api_key"] == "test-token" and values["secret_key"] == "new-secret" and values["voice"] == "maxim"


def test_voice_mode_must_be_declared(stack):
    with pytest.raises(ValueError, match="не поддерживается"):
        stack.save_preset("tts", "yandex", "Y", {"voice_mode": "clone"})
    item = stack.save_preset("tts", "fakeco", "F", {"api_key": "k", "voice_mode": "baked", "baked_voice_id": "v", "baked_voice_title": "T"})
    values = stack.preset("tts", item["id"])["values"]
    assert values["voice_mode"] == "baked" and values["baked_voice_id"] == "v"
    stack.save_preset("tts", "fakeco", "F", {"api_key": KEEP}, item["id"])  # форма без служебных полей их не стирает
    assert stack.preset("tts", item["id"])["values"]["baked_voice_id"] == "v"


# --- голоса ----------------------------------------------------------------------------------
def test_voices_success_and_fallback_to_default_options(stack):
    result = asyncio.run(stack.voices("tts", "fakeco", {"api_key": "ok"}))
    assert result == {"options": [{"value": "v1", "label": "Голос 1", "lang": "ru", "gender": "f"}]}
    bad = asyncio.run(stack.voices("tts", "fakeco", {"api_key": "bad"}))
    assert bad == {"options": [{"value": "default", "label": "По умолчанию"}], "error": "Неверный ключ"}
    boom = asyncio.run(stack.voices("tts", "fakeco", {"api_key": "boom"}))
    assert boom["options"] == [{"value": "default", "label": "По умолчанию"}]
    assert "secret-host" not in boom["error"] and "boom" not in boom["error"]  # текст сетевой ошибки не отдаётся
    nothing = asyncio.run(stack.voices("tts", "yandex", {"api_key": "ok"}))  # адаптер без voices()
    assert nothing["options"] == [] and "не умеет" in nothing["error"]


def test_voices_refresh_flag_goes_into_config(stack):
    asyncio.run(stack.voices("tts", "fakeco", {"api_key": "ok"}))
    asyncio.run(stack.voices("tts", "fakeco", {"api_key": "ok"}, refresh=True))
    assert adapters.get("fakeco").REFRESH[-2:] == [False, True]


def test_voices_use_saved_secret_for_keep(stack):
    item = stack.save_preset("tts", "fakeco", "F", {"api_key": "saved-key"})
    asyncio.run(stack.voices("tts", "fakeco", {"api_key": KEEP}, item["id"]))
    assert fake_calls(stack)[-1] == "saved-key"


# --- запекание голоса поставщика -------------------------------------------------------------
def test_bake_and_unbake(stack):
    result = asyncio.run(stack.bake("tts", "fakeco", {"api_key": "k1"}, wav(3.0), "  Привет   мир "))
    assert result == {"voice_id": "vid-1", "title": "Мой голос"}
    assert fake_calls(stack)[-1] == ("bake", len(wav(3.0)), "Привет мир", "k1")
    asyncio.run(stack.unbake("tts", "fakeco", {"api_key": "k1", "baked_voice_id": "vid-1"}))
    assert fake_calls(stack)[-1] == ("unbake", "vid-1")
    with pytest.raises(ValueError, match="текст"):
        asyncio.run(stack.bake("tts", "fakeco", {"api_key": "k1"}, wav(3.0), ""))
    with pytest.raises(ValueError, match="Длительность"):
        asyncio.run(stack.bake("tts", "fakeco", {"api_key": "k1"}, wav(0.5), "т"))
    with pytest.raises(ValueError, match="не умеет запекать"):
        asyncio.run(stack.bake("tts", "yandex", {"api_key": "k1"}, wav(3.0), "т"))


# --- эталон клонирования у пресета -----------------------------------------------------------
def test_preset_reference_file_roundtrip(stack):
    item = stack.save_preset("tts", "fakeco", "F", {"api_key": "k", "voice_mode": "clone"})
    assert stack.upload_preset_file("tts", item["id"], "ref_audio", wav(3.0))["seconds"] == 3.0
    shown = next(p for p in asyncio.run(stack.public())["tts"]["presets"] if p["id"] == item["id"])
    assert shown["files"]["ref_audio"]["seconds"] == 3.0 and shown["values"]["ref_audio"] == ""  # путь наружу не уходит
    stack.save_preset("tts", "fakeco", "F2", {"api_key": KEEP, "ref_audio": "/etc/passwd"}, item["id"])
    path = stack.preset("tts", item["id"])["values"]["ref_audio"]
    assert path.endswith("ref_audio.wav") and "passwd" not in path  # форма путь не меняет
    config, _ = stack.config("tts", "p:" + item["id"])
    assert config["ref_audio"] == path and config["adapter"] == "fakeco"
    with pytest.raises(ValueError, match="нет поля-файла"):
        stack.upload_preset_file("tts", item["id"], "voice", wav(3.0))
    stack.remove_preset_file("tts", item["id"], "ref_audio")
    assert stack.preset("tts", item["id"])["values"]["ref_audio"] == ""
    stack.upload_preset_file("tts", item["id"], "ref_audio", wav(3.0))
    folder = stack.state_dir / "presets" / "tts" / item["id"]
    assert folder.is_dir()
    stack.delete_preset("tts", item["id"])
    assert not folder.exists()


# --- пакеты: режим голоса и запечённый эмбеддинг ---------------------------------------------
def test_package_voice_mode_and_bake_status(stack):
    package = stack.package("omnivoice")
    assert stack.local.options(package)["voice_mode"] == "preset" and stack.local.baked(package) == {"baked": False, "name": ""}
    with pytest.raises(ValueError, match="не поддерживается"):
        stack.local.save_options(package, {"voice_mode": "clone"})
    stack.local.upload(package, "ref_audio", wav(4.0))
    assert stack.local.options(package)["voice_mode"] == "baked"  # загруженный эталон — режим «запечённый»
    stack.local.save_options(package, {"ref_text": "Привет мир"})
    path = stack.local.file_path(package, "ref_audio")
    digest = hashlib.sha1(path.read_bytes() + "Привет мир".encode()).hexdigest()[:12]
    assert stack.local.baked(package) == {"baked": False, "name": ""}
    path.with_name(f"ref_audio.{digest}.pt").write_bytes(b"baked")  # так сервер пакета хранит эмбеддинг
    assert stack.local.baked(package) == {"baked": True, "name": f"ref_audio.{digest}.pt"}
    stack.local.save_options(package, {"ref_text": "Другой текст"})
    assert stack.local.baked(package)["baked"] is False  # новый текст — нужно запечь заново
    stack.local.save_options(package, {"voice_mode": "preset"})
    assert stack.local.options(package)["voice_mode"] == "preset"


def test_package_server_gets_reference_only_in_baked_mode(stack, monkeypatch):
    package = stack.package("omnivoice")
    folder = stack.local.folder(package)
    folder.mkdir(parents=True)
    (folder / "installed.json").write_text("{}", encoding="utf-8")
    (folder / "venv").mkdir()
    stack.local.upload(package, "ref_audio", wav(4.0))
    stack.local.save_options(package, {"ref_text": "Привет", "port": 59981})
    seen = []

    class Process:
        pid = 1

        def poll(self):
            return None

    monkeypatch.setattr(voice_stack.subprocess, "Popen", lambda *a, **k: seen.append(k["env"]) or Process())
    asyncio.run(stack.local.start(package))
    assert seen[-1]["OV_REF_AUDIO"].endswith("ref_audio.wav") and seen[-1]["OV_REF_TEXT"] == "Привет"
    stack.local.processes.clear()
    stack.local.save_options(package, {"voice_mode": "preset"})
    asyncio.run(stack.local.start(package))
    assert seen[-1]["OV_REF_AUDIO"] == "" and seen[-1]["OV_REF_TEXT"] == ""


def test_package_validation_of_modes_and_bake():
    base = {"id": "p", "service": "tts", "adapter": "openai", "config": {}, "defaults": {"port": 5000},
            "server": {"script": "x.py"}, "platforms": {PLATFORM: {"cpu": []}}, "fields": [{"key": "ref", "type": "file"}]}
    validate_package({**base, "voice_modes": ["preset", "clone"]})
    with pytest.raises(ValueError):
        validate_package({**base, "voice_modes": ["magic"]})
    with pytest.raises(ValueError):
        validate_package({**base, "bake": {"audio": "ref", "text": "nope"}})


# --- API -------------------------------------------------------------------------------------
def fake_module(runtime):
    return adapters.get("fakeco")


async def test_api_endpoints(tmp_path):
    from server_emulator.runtime import Runtime
    from server_emulator.ui import api as ui_api
    runtime = Runtime(tmp_path)
    folder = runtime.voice_stack.state_dir / "adapters"
    folder.mkdir(parents=True)
    (folder / "fakeco.py").write_text(FAKE, encoding="utf-8")
    app = web.Application(client_max_size=16 * 1024 ** 2)

    async def handler(request):
        return await ui_api.dispatch(request, runtime, app)
    app.router.add_route("*", "/api/{action}", handler)
    headers = {"X-Emulator": "1"}
    async with TestClient(TestServer(app)) as client:
        voices = await (await client.post("/api/voice-stack-voices", json={"service": "tts", "adapter": "fakeco", "values": {"api_key": "ok"}}, headers=headers)).json()
        assert voices["options"][0]["value"] == "v1"
        await client.post("/api/voice-stack-voices", json={"service": "tts", "adapter": "fakeco", "values": {"api_key": "ok"}, "refresh": True}, headers=headers)
        assert fake_module(runtime).REFRESH[-1] is True
        failed = await (await client.post("/api/voice-stack-voices", json={"service": "tts", "adapter": "fakeco", "values": {"api_key": "bad"}}, headers=headers)).json()
        assert failed["error"] == "Неверный ключ" and failed["options"][0]["value"] == "default"
        form = FormData()
        form.add_field("meta", json.dumps({"service": "tts", "adapter": "fakeco", "values": {"api_key": "k"}, "text": "Привет"}))
        form.add_field("audio", wav(3.0), filename="ref.wav", content_type="audio/wav")
        baked = await client.post("/api/voice-stack-bake", data=form, headers=headers)
        assert baked.status == 200 and (await baked.json()) == {"voice_id": "vid-1", "title": "Мой голос"}
        bad = FormData()
        bad.add_field("meta", json.dumps({"service": "tts", "adapter": "fakeco", "values": {"api_key": "k"}, "text": "т"}))
        bad.add_field("audio", b"not wav", filename="ref.wav")
        rejected = await client.post("/api/voice-stack-bake", data=bad, headers=headers)
        assert rejected.status == 400 and "WAV" in (await rejected.json())["error"]
        unbaked = await client.post("/api/voice-stack-unbake", json={"service": "tts", "adapter": "fakeco", "values": {"api_key": "k", "baked_voice_id": "vid-1"}}, headers=headers)
        assert unbaked.status == 200
        created = await (await client.post("/api/voice-stack-preset", json={"service": "tts", "adapter": "fakeco", "title": "F", "values": {"api_key": "k", "voice_mode": "clone"}}, headers=headers)).json()
        url = f"/api/voice-stack-preset-file?service=tts&id={created['id']}&key=ref_audio"
        up = await client.post(url, data=wav(3.0), headers=headers)
        assert up.status == 200 and (await up.json())["seconds"] == 3.0
        assert (await client.get(url)).status == 200
        assert (await client.delete(url, headers=headers)).status == 200
        assert (await client.get(url)).status == 400
        state = await (await client.get("/api/voice-stack")).json()
        assert state["tts"]["presets"][0]["values"]["api_key"] == KEEP
        package = await client.post("/api/voice-stack-bake", json={"package": "omnivoice"}, headers=headers)
        assert package.status == 400 and "установите" in (await package.json())["error"]
    await runtime.close()
