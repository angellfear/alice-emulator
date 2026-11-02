"""Речевой сервер пакета: чужой процесс на порту — ошибка, а не молчаливое использование;
остановка на Windows снимает всё дерево (venv\\Scripts\\python.exe — пускатель)."""
import os

import pytest

from server_emulator import voice_stack
from server_emulator.voice_stack import LocalSpeech, load_packages

PACKAGE = next(p for p in load_packages()["tts"] if p["id"] == "omnivoice")


@pytest.fixture
def local(tmp_path, monkeypatch):
    speech = LocalSpeech(tmp_path / "speech", tmp_path / "options")
    speech.folder(PACKAGE).mkdir(parents=True)
    monkeypatch.setattr(speech, "installed", lambda package: True)

    async def fast_sleep(seconds):
        return None
    monkeypatch.setattr(voice_stack.asyncio, "sleep", fast_sleep)
    return speech


def answering(monkeypatch, speech, *results):
    """healthy() по очереди возвращает results (последний — дальше всегда)."""
    queue = list(results)

    async def healthy(package):
        return queue.pop(0) if len(queue) > 1 else queue[0]
    monkeypatch.setattr(speech, "healthy", healthy)


async def test_foreign_server_on_port_is_an_error(local, monkeypatch):
    answering(monkeypatch, local, True)
    with pytest.raises(ValueError, match="занят другим процессом"):
        await local.start(PACKAGE)


async def test_own_server_from_previous_panel_is_reused(local, monkeypatch):
    (local.folder(PACKAGE) / "server.pid").write_text("12345", encoding="ascii")
    answering(monkeypatch, local, True)
    monkeypatch.setattr(voice_stack.subprocess, "Popen", lambda *a, **k: pytest.fail("не должен запускать"))
    assert await local.start(PACKAGE) is None


async def test_port_released_after_stop_starts_new_server(local, monkeypatch):
    answering(monkeypatch, local, True, False)  # только что остановленный сервер ещё отвечал

    class Process:
        pid = 4321

        def poll(self):
            return None
    started = []
    monkeypatch.setattr(voice_stack.subprocess, "Popen", lambda *a, **k: started.append(a) or Process())
    await local.start(PACKAGE)
    assert started and (local.folder(PACKAGE) / "server.pid").read_text(encoding="ascii") == "4321"


@pytest.mark.skipif(os.name != "nt", reason="пускатель venv — особенность Windows")
def test_stop_kills_process_tree_on_windows(local, monkeypatch):
    killed = []
    monkeypatch.setattr(voice_stack, "kill_tree", killed.append)

    class Process:
        pid = 777
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 0
    process = Process()
    local.processes[PACKAGE["id"]] = process
    local.stop(PACKAGE)
    assert killed == [777] and not process.terminated
