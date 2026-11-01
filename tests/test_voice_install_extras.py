"""Установщик пакетов: zip-модели распаковываются; запекание без текста эталона (bake.transcribe: false)."""
import json
import zipfile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server_emulator.voice_stack import PLATFORM, LocalSpeech, load_packages


def test_zip_model_is_downloaded_and_unpacked(tmp_path, monkeypatch):
    archive = tmp_path / "vosk-model-test.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("vosk-model-test/am/final.mdl", b"model")
        out.writestr("../escape.txt", b"no")  # zipfile отбрасывает «..» — файл окажется внутри папки установки
    local = LocalSpeech(tmp_path / "speech", tmp_path / "options")
    package = {"id": "zipped", "platforms": {PLATFORM: {"cpu": []}}, "defaults": {},
               "models": [{"url": archive.as_uri()}]}
    monkeypatch.setattr(LocalSpeech, "_run", lambda self, command, job, stage: None)  # без venv и pip
    job = {}
    local._install_sync(package, job)
    folder = local.folder(package)
    assert (folder / "vosk-model-test" / "am" / "final.mdl").read_bytes() == b"model"
    assert not (folder / "vosk-model-test.zip").exists() and not (tmp_path / "speech" / "escape.txt").exists()
    local._install_sync(package, job)  # повторная установка: папка модели есть — не качает заново
    assert (folder / "installed.json").exists()


def test_vosk_uses_zip_models_without_run_step():
    vosk = next(p for p in load_packages()["asr"] if p["id"] == "vosk")
    assert vosk["models"][0]["url"].endswith(".zip")
    assert not any("run" in step for plat in vosk["platforms"].values() for steps in plat.values() for step in steps)


async def test_bake_without_transcription_when_package_does_not_need_text(tmp_path, monkeypatch):
    from server_emulator.runtime import Runtime
    from server_emulator.ui import api as ui_api
    from server_emulator.ui.api import voice_stack as api_voice

    runtime = Runtime(tmp_path)
    xtts = next(p for p in load_packages()["tts"] if p["id"] == "xtts")
    assert xtts["bake"]["transcribe"] is False
    local = runtime.voice_stack.local
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF")
    options = {"ref_audio": str(ref), "ref_text": "", "voice_mode": "baked"}  # поле-файл задаётся только загрузкой
    monkeypatch.setattr(local, "options", lambda package: dict(options))
    started = []

    async def no_asr(runtime, path):
        raise AssertionError("ASR не должен вызываться: XTTS текст эталона не нужен")

    async def start(package):
        started.append(package["id"])
    monkeypatch.setattr(api_voice, "_transcribe", no_asr)
    monkeypatch.setattr(local, "installed", lambda package: True)
    monkeypatch.setattr(local, "stop", lambda package: None)
    monkeypatch.setattr(local, "start", start)
    app = web.Application()

    async def handler(request):
        return await ui_api.dispatch(request, runtime, app)
    app.router.add_route("*", "/api/{action}", handler)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/voice-stack-bake", json={"package": "xtts"}, headers={"X-Emulator": "1"})
        assert response.status == 200, await response.text()
        assert json.loads(await response.text()) == {"started": "xtts"}
    assert started == ["xtts"] and local.options(xtts)["ref_text"] == ""
    await runtime.close()
