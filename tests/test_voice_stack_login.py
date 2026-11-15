"""Кнопка входа подписки: LoginRunner запускает login отдельным процессом и отдаёт его вывод."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from server_emulator.voice_stack import login as login_module
from server_emulator.voice_stack.login import LoginRunner
from server_emulator.voice_stack.adapters import chatgpt


def _wait_done(runner, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runner.status()["done"]:
            return
        time.sleep(0.02)
    pytest.fail("вход не завершился")


def test_start_runs_login_process(tmp_path, monkeypatch):
    seen, release = {}, threading.Event()

    class _FakeProc:
        def __init__(self, cmd, kwargs):
            self.cmd, self.kwargs = cmd, kwargs
            self.returncode = None

        @property
        def stdout(self):
            yield "Sign in with ChatGPT using device code:"
            yield "1) Visit https://auth.example/device"
            yield "2) Enter code: ABC-123"
            release.wait(3)  # «процесс висит»: running стабилен до освобождения

        def poll(self):
            return self.returncode

        def wait(self):
            release.wait(3)
            self.returncode = 0
            return self.returncode

    def fake_popen(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return _FakeProc(cmd, kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    runner = LoginRunner(tmp_path)
    status = runner.start("chatgpt")
    assert status["running"] and not status["done"]
    assert seen["cmd"][1:4] == ["-m", "server_emulator.voice_stack.login", "chatgpt"]
    assert seen["cmd"][4:6] == ["--state", str(tmp_path)]
    assert seen["kwargs"]["stdout"] is subprocess.PIPE  # вывод процесса — журнал для UI
    release.set()
    _wait_done(runner)
    final = runner.status()
    assert final["exit"] == 0 and final["running"] is False
    assert "2) Enter code: ABC-123" in "\n".join(final["output"])
    assert final["auth_ready"] is False


def test_start_unknown_service(tmp_path):
    with pytest.raises(ValueError, match="service:"):
        LoginRunner(tmp_path).start("nope")


def test_start_while_running_returns_same_process(tmp_path, monkeypatch):
    calls, release = [], threading.Event()

    class Live:
        returncode = None
        stdout = iter([])

        def poll(self):
            return None

        def wait(self):
            release.wait(2)

        def terminate(self):
            pass

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return Live()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    runner = LoginRunner(tmp_path)
    first = runner.start("chatgpt")
    second = runner.start("chatgpt")
    assert first["running"] and second["running"]
    assert len(calls) == 1  # второй запуск не плодит процессы — вход уже идёт
    assert runner.cancel() is True
    release.set()


def test_status_auth_ready(tmp_path):
    runner = LoginRunner(tmp_path)
    assert runner.status()["service"] is None
    path = chatgpt.auth_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    runner.service = "chatgpt"
    assert runner.status()["auth_ready"] is True


def test_status_cooldown(tmp_path):
    """Запись недавнего запроса кода → время до перевыпуска; устаревшая → 0."""
    runner = LoginRunner(tmp_path)
    runner.service = "chatgpt"
    assert runner.status()["cooldown"] == 0
    path = chatgpt.auth_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"device_code_requested_at": time.time()}), encoding="utf-8")
    cooldown = runner.status()["cooldown"]
    assert 0 < cooldown <= 300
    path.write_text(json.dumps({"device_code_requested_at": time.time() - 600}), encoding="utf-8")
    assert runner.status()["cooldown"] == 0


def test_module_run_has_no_import_warning():
    """python -m ...login не должен импортировать себя через пакет __init__:
    runpy тогда ругается на модуль, уже найденный в sys.modules."""
    proc = subprocess.run(
        [sys.executable, "-m", "server_emulator.voice_stack.login", "--help"],
        capture_output=True, text=True, timeout=60,
        cwd=Path(login_module.__file__).resolve().parents[2])
    assert "RuntimeWarning" not in proc.stderr, proc.stderr
