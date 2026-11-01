"""Локальные пакеты ASR/TTS голосового стека (gigaam, faster-whisper, parakeet, vosk, espeech, silero,
qwen-tts, xtts): схема JSON и серверы tools/*_server.py на подменённом движке. Модели не скачиваются,
тяжёлые библиотеки (torch, onnx-asr, vosk…) не нужны: движок — заглушка с тем же интерфейсом."""
import asyncio
import hashlib
import json
import re
import struct
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from server_emulator.voice_stack import LocalSpeech, adapters, fill, load_packages, voice_modes

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import speech_common  # noqa: E402
import espeech_server  # noqa: E402
import gigaam_server  # noqa: E402,F401
import onnx_asr_server  # noqa: E402
import parakeet_server  # noqa: E402,F401
import qwen_tts_server  # noqa: E402
import silero_server  # noqa: E402
import vosk_server  # noqa: E402
import whisper_server  # noqa: E402
import xtts_server  # noqa: E402

IDS = {"gigaam": "asr", "faster-whisper": "asr", "parakeet": "asr", "vosk": "asr",
       "espeech": "tts", "silero": "tts", "qwen-tts": "tts", "xtts": "tts"}
LABELS = {"gigaam": "(CPU | GPU)", "faster-whisper": "(GPU)", "parakeet": "(GPU)", "vosk": "(CPU)",
          "espeech": "(CPU | GPU)", "silero": "(CPU | GPU)", "qwen-tts": "(GPU)", "xtts": "(GPU)"}
SOURCES = {"gigaam": ["gigaam_server", "onnx_asr_server"], "parakeet": ["parakeet_server", "onnx_asr_server"],
           "faster-whisper": ["whisper_server"], "vosk": ["vosk_server"], "espeech": ["espeech_server"],
           "silero": ["silero_server"], "qwen-tts": ["qwen_tts_server"], "xtts": ["xtts_server"]}
SERVICE_PATH = {"asr": "/v1/audio/transcriptions", "tts": "/v1/audio/speech"}


def shipped():
    return {p["id"]: p for items in load_packages().values() for p in items}


@pytest.fixture(scope="module")
def packages():
    found = shipped()
    assert set(IDS) <= set(found), "пакет не загрузился: " + ", ".join(sorted(set(IDS) - set(found)))
    return found


def placeholders(value):
    if isinstance(value, str):
        return set(re.findall(r"\{(\w+)\}", value))
    if isinstance(value, dict):
        return set().union(*(placeholders(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(placeholders(v) for v in value)) if value else set()
    return set()


# --- схема пакетов -------------------------------------------------------------------------
@pytest.mark.parametrize("ident", sorted(IDS))
def test_package_schema(packages, ident):
    package = packages[ident]
    assert package["service"] == IDS[ident]
    assert package["title"].endswith(LABELS[ident])
    assert package["description"].strip() and "\n" not in package["description"]
    need = package["requirements"]
    assert isinstance(need["gpu"], bool) and need["vram_gb"] >= 0 and need["disk_gb"] > 0
    assert package["adapter"] == "openai"
    assert package["service"] in adapters.get("openai").SERVICES
    config = package["config"]
    assert config["url"] == "http://127.0.0.1:{port}" + SERVICE_PATH[IDS[ident]]
    assert (TOOLS / Path(package["server"]["script"]).name).is_file()
    assert package["server"]["health"] == "/health"
    assert 1024 <= package["defaults"]["port"] <= 65535
    assert {"windows", "linux"} <= set(package["platforms"])


def test_ports_are_unique_across_packages(packages):
    ports = [p["defaults"]["port"] for p in packages.values()]
    assert len(ports) == len(set(ports))


@pytest.mark.parametrize("ident", sorted(IDS))
def test_placeholders_resolve_to_options(packages, ident):
    package = packages[ident]
    keys = {f["key"] for f in package["fields"]}
    known = set(package["defaults"]) | {"dir", "python"}
    assert keys <= set(package["defaults"]), "поле без значения по умолчанию"
    used = placeholders([package["config"], package["server"].get("env", {}), package["models"], package["platforms"]])
    assert used <= known, used - known
    assert set(package["reinstall_on"]) <= keys
    for field in package["fields"]:
        assert placeholders(field.get("when", {})) == set()
        assert set(field.get("when", {})) <= {"device", "voice_mode"}
        if field["type"] == "select" and "options_from" not in field:
            assert all({"value", "label"} <= set(o) for o in field["options"])
            assert str(package["defaults"][field["key"]]) in [str(o["value"]) for o in field["options"]]


@pytest.mark.parametrize("ident", sorted(IDS))
def test_pip_versions_are_pinned_and_platforms_complete(packages, ident):
    package = packages[ident]
    for platform in package["platforms"].values():
        assert "cpu" in platform
        for device, steps in platform.items():
            assert device in ("cpu", "cuda") and steps
            for step in steps:
                for spec in step.get("pip", []):
                    assert "==" in spec, f"{ident}: версия {spec} не закреплена"
                assert step.get("pip") or step.get("run")
    gpu_only = LABELS[ident] == "(CPU)"
    assert (set(package["platforms"]["windows"]) == {"cpu"}) == gpu_only


@pytest.mark.parametrize("ident", sorted(IDS))
def test_run_steps_are_valid_python(packages, ident):
    package = packages[ident]
    options = {**package["defaults"], "dir": "C:/data/" + ident, "python": "python"}
    for platform in package["platforms"].values():
        for steps in platform.values():
            for step in steps:
                if "run" not in step:
                    continue
                command = [fill(part, options) for part in step["run"]]
                assert command[:2] == ["python", "-c"] and step.get("title")
                compile(command[2], "<run>", "exec")
                assert "{" not in "".join(command[3:]), "неподставленный параметр: " + str(command[3:])


@pytest.mark.parametrize("ident", sorted(IDS))
def test_server_env_is_read_by_script(packages, ident):
    text = "".join((TOOLS / (name + ".py")).read_text(encoding="utf-8") for name in SOURCES[ident] + ["speech_common"])
    for name in packages[ident]["server"]["env"]:
        if name not in ("CUDA_VISIBLE_DEVICES", "HF_HOME", "HF_HUB_OFFLINE"):
            assert name in text, f"{ident}: сервер не читает {name}"


@pytest.mark.parametrize("ident", sorted(IDS))
def test_rare_fields_are_advanced_and_gpu_field_follows_device(packages, ident):
    fields = {f["key"]: f for f in packages[ident]["fields"]}
    for key in ("port", "threads", "workers", "priority"):
        if key in fields:
            assert fields[key].get("group") == "advanced", key
    if "gpu" in fields:
        assert fields["gpu"]["options_from"] == "gpus" and fields["gpu"]["when"] == {"device": "cuda"}
    for field in fields.values():
        assert field.get("group") in (None, "advanced")


def test_licenses_and_limits_are_in_descriptions(packages):
    assert "CC BY-NC" in packages["silero"]["description"] and "некоммерческ" in packages["silero"]["description"]
    assert "CPML" in packages["xtts"]["description"] and "некоммерческ" in packages["xtts"]["description"]
    assert "CC-BY-4.0" in packages["parakeet"]["description"]
    assert "MIT" in packages["gigaam"]["description"] and "Apache-2.0" in packages["vosk"]["description"]
    assert "Apache-2.0" in packages["espeech"]["description"] and "Apache-2.0" in packages["qwen-tts"]["description"]
    assert "MIT" in packages["faster-whisper"]["description"]
    assert "int8" in packages["faster-whisper"]["description"]


def test_no_internal_markers_in_packages_and_servers(packages):
    texts = [json.dumps(p, ensure_ascii=False) for p in packages.values()]
    texts += [path.read_text(encoding="utf-8") for path in TOOLS.glob("*_server.py")] + [(TOOLS / "speech_common.py").read_text(encoding="utf-8")]
    for text in texts:
        assert not re.search(r"TASK-\d|\.local\b|Claude|Anthropic|Co-Authored", text)


# --- режимы голоса -------------------------------------------------------------------------
def test_voice_modes_are_honest(packages):
    for ident in ("gigaam", "faster-whisper", "parakeet", "vosk"):
        assert "voice_modes" not in packages[ident]
    assert voice_modes(packages["silero"]) == ["preset"]
    assert voice_modes(packages["espeech"]) == ["clone"]
    assert voice_modes(packages["xtts"]) == ["baked"]
    assert voice_modes(packages["qwen-tts"]) == ["preset", "baked"]
    for ident in ("espeech", "silero"):
        assert "bake" not in packages[ident]  # эталон подставляется в каждый запрос / эталона нет
    for ident in ("xtts", "qwen-tts"):
        package = packages[ident]
        bake = package["bake"]
        fields = {f["key"]: f for f in package["fields"]}
        assert bake["mode"] == "baked" and fields[bake["audio"]]["type"] == "file"
        assert fields[bake["text"]]["type"] == "textarea"
        assert fields[bake["audio"]]["transcribe_to"] == bake["text"] and fields[bake["audio"]]["required"] is True


def test_qwen_fields_follow_voice_mode(packages):
    fields = {f["key"]: f for f in packages["qwen-tts"]["fields"]}
    assert fields["ref_audio"]["when"] == {"voice_mode": "baked"} and fields["ref_text"]["when"] == {"voice_mode": "baked"}
    assert fields["instruct"]["when"] == {"voice_mode": "preset"}


def test_silero_voices_match_server(packages):
    voices = [o["value"] for o in next(f for f in packages["silero"]["fields"] if f["key"] == "voice")["options"]]
    assert voices == list(silero_server.SPEAKERS) or sorted(voices) == sorted(silero_server.SPEAKERS)


def test_baked_file_name_matches_installer(tmp_path, packages):
    """Файл запечённого голоса, который пишет сервер, — тот же, что ищет LocalSpeech.baked()."""
    ref = tmp_path / "ref_audio.wav"
    ref.write_bytes(b"RIFF....WAVEdata" + bytes(range(64)))
    text = "  привет   мир "
    name, normalized = speech_common.bake_path(str(ref), text)
    assert normalized == "привет мир"
    package = {**packages["xtts"], "id": "xtts"}
    local = LocalSpeech(tmp_path / "speech", tmp_path / "opts")
    local.options_dir.mkdir(parents=True)
    (local.options_dir / "xtts.json").write_text(json.dumps({"ref_audio": str(ref), "ref_text": text}), encoding="utf-8")
    assert local.baked(package) == {"baked": False, "name": ""}
    Path(name).write_bytes(b"x")
    assert local.baked(package) == {"baked": True, "name": Path(name).name}
    assert Path(name).name == "ref_audio." + hashlib.sha1(ref.read_bytes() + b"\xd0\xbf").hexdigest()[:0] + Path(name).name.split(".")[1] + ".pt"


def test_baked_voice_is_created_once_and_old_files_removed(tmp_path):
    ref = tmp_path / "ref_audio.wav"
    ref.write_bytes(b"first")
    calls, saved = [], {}

    def create(text):
        calls.append(text)
        return {"voice": len(calls)}

    def save(voice, path):
        saved[path] = voice
        Path(path).write_text(json.dumps(voice), encoding="utf-8")

    def load(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    voice, mode = speech_common.baked_voice(str(ref), "текст", create, save, load)
    assert voice == {"voice": 1} and "запечён сейчас" in mode and calls == ["текст"]
    voice, mode = speech_common.baked_voice(str(ref), "текст", create, save, load)
    assert voice == {"voice": 1} and "запечён сейчас" not in mode and len(calls) == 1
    ref.write_bytes(b"second")  # новый эталон — новое запекание, старый .pt удалён
    voice, _ = speech_common.baked_voice(str(ref), "текст", create, save, load)
    assert voice == {"voice": 2} and len(list(tmp_path.glob("ref_audio.*.pt"))) == 1
    speech_common.baked_voice(str(ref), "", create, save, load)  # без текста — create(None)
    assert calls[-1] is None


# --- HTTP-часть серверов -------------------------------------------------------------------
def run_app(app, scenario):
    async def go():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await scenario(client)
        finally:
            await client.close()
    return asyncio.run(go())


def upload(content=b"audio-bytes", **fields):
    form = FormData()
    form.add_field("file", content, filename="input.wav", content_type="audio/wav")
    for key, value in fields.items():
        form.add_field(key, value)
    return form


@pytest.fixture
def fake_decode(monkeypatch):
    monkeypatch.setattr(speech_common, "decode_audio", lambda data, rate=16000: np.zeros(16000, dtype=np.float32) if data != b"bad" else (_ for _ in ()).throw(speech_common.UserError("Не удалось прочитать аудио")))


def test_asr_app_transcribes_in_openai_format(fake_decode):
    seen = []

    def transcribe(samples, language):
        seen.append((len(samples), language))
        return "  привет мир "

    app = speech_common.asr_app(transcribe, {"engine": "fake", "model": "m", "voice": "", "state": {"model_loaded": True}})

    async def scenario(client):
        response = await client.post("/v1/audio/transcriptions", data=upload(language="ru", model="x"))
        assert response.status == 200 and await response.json() == {"text": "привет мир"}
        text = await client.post("/v1/audio/transcriptions", data=upload(response_format="text"))
        assert await text.text() == "привет мир"
        verbose = await (await client.post("/v1/audio/transcriptions", data=upload(response_format="verbose_json"))).json()
        assert verbose["text"] == "привет мир" and verbose["duration"] == 1.0
        health = await (await client.get("/health")).json()
        assert health["status"] == "ok" and health["state"]["model_loaded"] is True
        assert health["completed"] == 3 and health["busy"] is False and health["capacity"] == 1
    run_app(app, scenario)
    assert seen[0] == (16000, "ru") and seen[1][1] is None


def test_asr_app_rejects_bad_requests_without_traceback(fake_decode):
    def boom(samples, language):
        raise RuntimeError("модель упала")

    app = speech_common.asr_app(boom, {"engine": "fake"})

    async def scenario(client):
        for response, text in (
                (await client.post("/v1/audio/transcriptions", data={"x": "1"}), "multipart"),
                (await client.post("/v1/audio/transcriptions", data=FormData(default_to_multipart=True)), "file"),
                (await client.post("/v1/audio/transcriptions", data=upload(b"")), "Пустой"),
                (await client.post("/v1/audio/transcriptions", data=upload(b"bad")), "прочитать"),
                (await client.post("/v1/audio/transcriptions", data=upload(response_format="srt")), "response_format")):
            assert response.status == 400
            assert text in (await response.json())["error"]["message"]
        failed = await client.post("/v1/audio/transcriptions", data=upload())
        body = await failed.text()
        assert failed.status == 500 and "модель упала" in body and "Traceback" not in body and "File \"" not in body
    run_app(app, scenario)


def test_asr_queue_limit_answers_429(fake_decode):
    release = threading.Event()
    app = speech_common.asr_app(lambda samples, language: release.wait(10) and "ok", {"engine": "fake"}, capacity=1)

    async def scenario(client):
        tasks = [asyncio.create_task(client.post("/v1/audio/transcriptions", data=upload())) for _ in range(speech_common.QUEUE_LIMIT + 3)]
        await asyncio.sleep(1.0)
        release.set()
        statuses = sorted(r.status for r in await asyncio.gather(*tasks))
        assert statuses.count(429) == 2 and statuses.count(200) == speech_common.QUEUE_LIMIT + 1
    run_app(app, scenario)


class FakeVoice:
    rate = 24000

    def __init__(self):
        self.calls = []

    def synth(self, text, voice=None):
        self.calls.append((text, voice))
        if text == "сбой":
            raise RuntimeError("видеопамять закончилась")
        return np.linspace(-0.5, 0.5, 2400, dtype=np.float32)

    def stream(self, text, voice=None):
        self.calls.append((text, voice))
        if text == "сбой":
            raise RuntimeError("видеопамять закончилась")
        yield np.full(100, 0.25, dtype=np.float32)
        yield np.full(50, -0.25, dtype=np.float32)


def test_tts_app_returns_float32_pcm_with_headers():
    fake = FakeVoice()
    app = speech_common.tts_app(fake.synth, 24000, {"engine": "fake", "voice": "v", "state": {"model_loaded": True}})

    async def scenario(client):
        response = await client.post("/v1/audio/speech", json={"input": " Привет ", "voice": "a", "model": "x"})
        assert response.status == 200
        assert response.headers["X-Audio-Format"] == "f32le" and response.headers["X-Audio-Sample-Rate"] == "24000"
        assert response.headers["X-Audio-Channels"] == "1"
        pcm = np.frombuffer(await response.read(), dtype="<f4")
        assert len(pcm) == 2400 and pcm[0] == pytest.approx(-0.5)
        wav = await client.post("/v1/audio/speech", json={"input": "ещё", "response_format": "wav"})
        with wave.open(__import__("io").BytesIO(await wav.read())) as source:
            assert source.getframerate() == 24000 and source.getnframes() == 2400 and source.getsampwidth() == 2
        health = await (await client.get("/health")).json()
        assert health["sample_rate"] == 24000 and health["format"] == "f32le" and health["state"]["model_loaded"]
    run_app(app, scenario)
    assert fake.calls[0] == ("Привет", "a") and fake.calls[1] == ("ещё", None)


def test_tts_app_streams_chunks_and_rate_may_be_a_function():
    fake = FakeVoice()
    rate = {"value": 24000}
    app = speech_common.tts_app(fake.synth, lambda: rate["value"], {"engine": "fake"}, stream=fake.stream)

    async def scenario(client):
        rate["value"] = 22050
        response = await client.post("/v1/audio/speech", json={"input": "поток"})
        assert response.headers["X-Audio-Sample-Rate"] == "22050"
        pcm = np.frombuffer(await response.read(), dtype="<f4")
        assert len(pcm) == 150 and pcm[0] == 0.25 and pcm[-1] == -0.25
        wav = await client.post("/v1/audio/speech", json={"input": "целиком", "response_format": "wav"})
        assert wav.status == 200 and wav.content_type == "audio/wav"  # WAV идёт целиком через synth
    run_app(app, scenario)


def test_tts_app_validates_input_and_hides_engine_traceback():
    fake = FakeVoice()
    for stream in (None, fake.stream):
        app = speech_common.tts_app(fake.synth, 24000, {"engine": "fake"}, stream=stream)

        async def scenario(client):
            for body, text in (({"input": ""}, "input"), ({"input": "x" * 2001}, "input"), ({"input": 5}, "input"),
                               ({"input": "a", "response_format": "mp3"}, "response_format"), ([1], "JSON")):
                response = await client.post("/v1/audio/speech", json=body)
                assert response.status == 400 and text in (await response.json())["error"]["message"]
            assert (await client.post("/v1/audio/speech", data=b"not json")).status == 400
            failed = await client.post("/v1/audio/speech", json={"input": "сбой"})
            body = await failed.text()
            assert failed.status == 500 and "видеопамять" in body and "Traceback" not in body
        run_app(app, scenario)


# --- движки на заглушках --------------------------------------------------------------------
def test_silero_engine_voices_sentences_and_options(monkeypatch):
    class Model:
        speakers = ["aidar", "baya"]

        def __init__(self):
            self.calls = []

        def apply_tts(self, **kwargs):
            self.calls.append(kwargs)
            return np.ones(10, dtype=np.float32)

    monkeypatch.setenv("SILERO_VOICE", "baya")
    monkeypatch.setenv("SILERO_RATE", "48000")
    monkeypatch.setenv("SILERO_ACCENT", "0")
    model = Model()
    engine = silero_server.Engine(model)
    assert engine.voice == "baya" and engine.rate == 48000
    pcm = engine.synth("Привет! Как дела? Хорошо.", None)
    assert len(pcm) == 30 and [c["text"] for c in model.calls] == ["Привет!", "Как дела?", "Хорошо."]
    assert all(c["speaker"] == "baya" and c["sample_rate"] == 48000 and c["put_accent"] is False for c in model.calls)
    engine.synth("один", "aidar")
    assert model.calls[-1]["speaker"] == "aidar"
    with pytest.raises(speech_common.UserError, match="Неизвестный голос"):
        engine.synth("ещё", "nobody")
    assert engine.info()["voices"] == ["aidar", "baya"] and engine.info()["state"]["voice_mode"] == "preset"
    monkeypatch.setenv("SILERO_VOICE", "eugene")
    with pytest.raises(speech_common.UserError):
        silero_server.Engine(model)


def test_silero_splits_long_text_below_model_limit():
    text = "слово " * 300
    parts = silero_server.split_text(text)
    assert len(parts) > 3 and all(len(p) <= silero_server.SENTENCE_LIMIT for p in parts)
    assert " ".join(parts).split() == text.split()
    assert silero_server.split_text("  ") == [] and silero_server.split_text("Раз.\nДва") == ["Раз.", "Два"]


def test_silero_server_over_http(monkeypatch):
    class Model:
        speakers = list(silero_server.SPEAKERS)

        def apply_tts(self, **kwargs):
            return np.ones(240, dtype=np.float32)

    monkeypatch.setenv("SILERO_VOICE", "xenia")
    engine = silero_server.Engine(Model())
    app = silero_server.build_app(engine)

    async def scenario(client):
        response = await client.post("/v1/audio/speech", json={"input": "Раз. Два.", "voice": "aidar"})
        assert response.status == 200 and len(await response.read()) == 2 * 240 * 4
        bad = await client.post("/v1/audio/speech", json={"input": "Раз.", "voice": "zzz"})
        assert bad.status == 400
        health = await (await client.get("/health")).json()
        assert health["voice"] == "xenia" and health["sample_rate"] == 24000
    run_app(app, scenario)


def test_onnx_asr_engine_short_and_long_audio(monkeypatch):
    class Adapter:
        def __init__(self, label):
            self.label = label

        def recognize(self, samples, sample_rate):
            if self.label == "long":
                return iter([SimpleNamespace(text=" первая "), SimpleNamespace(text=""), SimpleNamespace(text="вторая")])
            return self.label

        def with_vad(self, vad, **options):
            assert vad == "vad:" + "C:/vad" and options["max_speech_duration_s"] == 20.0
            return Adapter("long")

    loaded = []
    monkeypatch.setenv("ASR_MODEL", "gigaam-v3-e2e-rnnt")
    monkeypatch.setenv("ASR_DEVICE", "cuda")
    monkeypatch.setenv("ASR_QUANT", "auto")
    monkeypatch.setenv("ASR_VAD_DIR", "C:/vad")
    engine = onnx_asr_server.Engine(lambda *args: loaded.append(args) or Adapter("short"), lambda path: "vad:" + path)
    assert loaded == [("gigaam-v3-e2e-rnnt", "cuda", None)] and engine.quantization is None
    assert engine.transcribe(np.zeros(16000 * 10, dtype=np.float32)) == "short"
    assert engine.transcribe(np.zeros(16000 * 40, dtype=np.float32)) == "первая вторая"
    assert engine.info()["quantization"] == "fp32" and engine.info()["state"]["long_audio"] is True
    monkeypatch.delenv("ASR_VAD_DIR")
    engine = onnx_asr_server.Engine(lambda *args: Adapter("short"), lambda path: None)
    assert engine.transcribe(np.zeros(16000 * 40, dtype=np.float32)) == "short"  # без VAD — целиком


def test_onnx_quantization_resolution():
    resolve = onnx_asr_server.resolve_quantization
    assert resolve("auto", "cpu") == "int8" and resolve("auto", "cuda") is None
    assert resolve("fp32", "cpu") is None and resolve("int8", "cuda") == "int8" and resolve("", "cpu") == "int8"


def test_gigaam_and_parakeet_http_with_stub_engine(monkeypatch, fake_decode):
    monkeypatch.setenv("ASR_MODEL", "gigaam-v3-ctc")
    monkeypatch.delenv("ASR_VAD_DIR", raising=False)
    engine = onnx_asr_server.Engine(lambda *args: SimpleNamespace(recognize=lambda samples, sample_rate: "включи свет"),
                                    lambda path: None)

    async def scenario(client):
        data = await (await client.post("/v1/audio/transcriptions", data=upload(language="ru"))).json()
        assert data == {"text": "включи свет"}
        health = await (await client.get("/health")).json()
        assert health["model"] == "gigaam-v3-ctc" and health["engine"] == "onnx-asr"
    run_app(onnx_asr_server.build_app(engine), scenario)


def test_whisper_engine_passes_language_beam_and_vad(monkeypatch):
    calls = []

    class Model:
        def transcribe(self, samples, **kwargs):
            calls.append(kwargs)
            return iter([SimpleNamespace(text=" раз "), SimpleNamespace(text="два")]), None

    monkeypatch.setenv("WHISPER_MODEL", "large-v3-turbo")
    monkeypatch.setenv("WHISPER_DEVICE", "cuda")
    monkeypatch.setenv("WHISPER_BEAM", "2")
    monkeypatch.setenv("WHISPER_VAD", "0")
    monkeypatch.setenv("WHISPER_LANGUAGE", "ru")
    engine = whisper_server.Engine(Model())
    assert engine.transcribe(np.zeros(10, dtype=np.float32)) == "раз два"
    assert engine.transcribe(np.zeros(10, dtype=np.float32), "en") == "раз два"
    assert calls[0] == {"language": "ru", "beam_size": 2, "vad_filter": False, "condition_on_previous_text": False}
    assert calls[1]["language"] == "en"
    assert engine.compute == "float16" and engine.info()["compute_type"] == "float16"
    assert whisper_server.resolve_compute("auto", "cpu") == "int8" and whisper_server.resolve_compute("int8_float16", "cuda") == "int8_float16"
    monkeypatch.setenv("WHISPER_LANGUAGE", "")  # пусто — язык определяет модель
    auto = whisper_server.Engine(Model())
    auto.transcribe(np.zeros(10, dtype=np.float32))
    assert calls[-1]["language"] is None


def test_vosk_engine_joins_results_of_long_audio(monkeypatch):
    class Recognizer:
        def __init__(self, model, rate):
            assert rate == 16000
            self.calls = 0

        def AcceptWaveform(self, data):
            self.calls += 1
            assert len(data) <= vosk_server.CHUNK
            return self.calls == 2

        def Result(self):
            return json.dumps({"text": "первая часть"})

        def FinalResult(self):
            return json.dumps({"text": "конец"})

    monkeypatch.setenv("VOSK_MODEL", "vosk-model-small-ru-0.22")
    engine = vosk_server.Engine(object(), Recognizer)
    text = engine.transcribe(np.zeros(16000 * 10, dtype=np.float32))
    assert text == "первая часть конец"
    assert engine.info()["device"] == "cpu" and engine.info()["model"] == "vosk-model-small-ru-0.22"


def test_espeech_engine_stress_and_clone_arguments(monkeypatch):
    calls = []

    class Tts:
        target_sample_rate = 24000

        def infer(self, ref_audio, ref_text, text, **kwargs):
            calls.append((ref_audio, ref_text, text, kwargs))
            return np.zeros(100, dtype=np.float32), 24000, None

    class Accent:
        def process_all(self, text):
            return text.upper()

    monkeypatch.setenv("ES_SPEED", "1.2")
    monkeypatch.setenv("ES_STEPS", "48")
    monkeypatch.setenv("ES_SEED", "0")
    engine = espeech_server.Engine(Tts(), Accent(), "C:/ref.wav", "привет мир")
    assert engine.ref_text_stressed == "ПРИВЕТ МИР" and engine.rate == 24000
    engine.synth("как дела")
    engine.synth("пр+ивет")  # готовая разметка не трогается
    assert calls[0][:3] == ("C:/ref.wav", "ПРИВЕТ МИР", "КАК ДЕЛА")
    assert calls[0][3]["speed"] == 1.2 and calls[0][3]["nfe_step"] == 48 and calls[0][3]["seed"] is None
    assert calls[1][2] == "пр+ивет"
    plain = espeech_server.Engine(Tts(), None, "C:/ref.wav", "привет")
    assert plain.ref_text_stressed == "привет" and plain.info()["state"] == {"model_loaded": True, "voice_mode": "clone", "accent": False}
    assert "ref.wav" in plain.info()["voice"]
    monkeypatch.setenv("ES_SEED", "7")
    assert espeech_server.Engine(Tts(), None, "r.wav", "т").seed == 7


def test_qwen_engine_design_and_baked_modes_report_real_rate(monkeypatch):
    class Model:
        device = "cpu"

        def __init__(self):
            self.calls = []

        def generate_voice_clone(self, **kwargs):
            self.calls.append(("clone", kwargs))
            return [np.zeros(10, dtype=np.float32)], 24000

        def generate_voice_design(self, **kwargs):
            self.calls.append(("design", kwargs))
            return [np.ones(10, dtype=np.float32)], 22050

    monkeypatch.setenv("QW_INSTRUCT", "young woman, calm")
    monkeypatch.setenv("QW_TEMPERATURE", "0.7")
    model = Model()
    design = qwen_tts_server.Engine(model, None, "по описанию", "cuda")
    assert design.rate == 24000
    design.synth("привет")
    assert design.rate == 22050 and design.info()["state"]["voice_mode"] == "design"
    kind, kwargs = model.calls[-1]
    assert kind == "design" and kwargs["instruct"] == "young woman, calm" and kwargs["language"] == "Russian"
    assert kwargs["temperature"] == 0.7 and kwargs["top_k"] == 50
    baked = qwen_tts_server.Engine(model, ["prompt"], "запечённый голос (a.pt)", "cuda")
    baked.synth("привет")
    kind, kwargs = model.calls[-1]
    assert kind == "clone" and kwargs["voice_clone_prompt"] == ["prompt"] and "ref_audio" not in kwargs
    assert baked.info()["state"]["voice_mode"] == "baked" and baked.info()["voice"].startswith("запечённый")


def test_xtts_engine_uses_baked_latents_and_streams(monkeypatch):
    class Model:
        def __init__(self):
            self.calls = []

        def inference(self, text, language, *latents, **options):
            self.calls.append(("full", text, language, latents, options))
            return {"wav": np.ones(20, dtype=np.float32)}

        def inference_stream(self, text, language, *latents, **options):
            self.calls.append(("stream", text, language, latents, options))
            yield np.ones(8, dtype=np.float32)
            yield np.zeros(4, dtype=np.float32)

    monkeypatch.setenv("XTTS_TEMPERATURE", "0.6")
    monkeypatch.setenv("XTTS_SPEED", "1.1")
    model = Model()
    engine = xtts_server.Engine(model, ("gpt", "spk"), "запечённый голос (a.pt)", "cuda")
    assert len(engine.synth("привет")) == 20
    assert [len(c) for c in engine.stream("привет")] == [8, 4]
    assert model.calls[0][2:4] == ("ru", ("gpt", "spk"))
    options = model.calls[0][4]
    assert options["temperature"] == 0.6 and options["speed"] == 1.1 and options["enable_text_splitting"] is True
    assert engine.info()["state"]["voice_mode"] == "baked" and engine.info()["model"] == "XTTS-v2"

    async def scenario(client):
        response = await client.post("/v1/audio/speech", json={"input": "привет"})
        assert response.headers["X-Audio-Sample-Rate"] == "24000"
        assert len(np.frombuffer(await response.read(), dtype="<f4")) == 12
    run_app(xtts_server.build_app(engine), scenario)
    monkeypatch.setenv("XTTS_STREAM", "0")

    async def whole(client):
        assert len(np.frombuffer(await (await client.post("/v1/audio/speech", json={"input": "привет"})).read(), dtype="<f4")) == 20
    run_app(xtts_server.build_app(engine), whole)


def test_speech_common_env_helpers(monkeypatch):
    monkeypatch.setenv("T_INT", " 3.0 ")
    monkeypatch.setenv("T_EMPTY", "  ")
    monkeypatch.setenv("T_FLAG", "ON")
    assert speech_common.env_int("T_INT") == 3 and speech_common.env_int("T_EMPTY", 7) == 7
    assert speech_common.env("T_MISSING", "x") == "x" and speech_common.env_float("T_INT") == 3.0
    assert speech_common.env_flag("T_FLAG") is True and speech_common.env_flag("T_EMPTY", True) is True


def test_decode_audio_reads_wav_and_resamples(tmp_path):
    pytest.importorskip("soundfile")
    pytest.importorskip("soxr")
    import io
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(8000)
        target.writeframes(struct.pack("<hh", 1000, 3000) * 8000)
    samples = speech_common.decode_audio(buffer.getvalue())
    assert samples.dtype == np.float32 and samples.ndim == 1 and abs(len(samples) - 16000) < 40
    assert samples[8000] == pytest.approx((1000 + 3000) / 2 / 32768, abs=0.01)
    with pytest.raises(speech_common.UserError, match="Не удалось прочитать"):
        speech_common.decode_audio("это не аудио".encode("utf-8"))
