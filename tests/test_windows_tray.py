"""TASK-045: трей-лаунчер — контроллер с подменёнными процессами и сервером, без pystray и окон."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import windows_tray  # noqa: E402

from server_emulator import server_config  # noqa: E402


def test_server_import_does_not_load_gui():
    code = "import sys, server_emulator.server; print('pystray' in sys.modules, 'PIL.Image' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert out.split() == ["False", "False"]


class FakeChild:
    def __init__(self, code=None):
        self.returncode, self.pid = code, 4242

    def poll(self):
        return self.returncode


class FakeServer:
    def __init__(self, state):
        self.state, self.value, self.stops = state, None, 0

    def status(self):
        return self.value

    def stop(self):
        self.stops += 1

    def url(self):
        return "http://127.0.0.1:80"

    def panel(self):
        return self.url() + "/emulator/"


@pytest.fixture
def controller(tmp_path, monkeypatch):
    state = tmp_path / "state"
    fake = FakeServer(state)
    opened = []
    item = windows_tray.Controller(root=tmp_path, state=state, server=fake, opener=opened.append, open_panel=True)
    launches = []

    def launch():
        launches.append(time.monotonic())
        item.child = FakeChild()

    monkeypatch.setattr(item, "launch", launch)
    monkeypatch.setattr(windows_tray, "process_alive", lambda pid: False)
    item.fake, item.launches, item.opened = fake, launches, opened
    return item


def running(item):
    return {"pid": 4242, "state": str(item.state)}


def test_starts_server_and_opens_panel_when_ready(controller):
    controller.iterate()
    assert len(controller.launches) == 1 and controller.state_text == "Сервер запускается…"
    controller.fake.value = running(controller)
    controller.iterate()
    assert controller.ready and controller.state_text == "Сервер работает"
    assert controller.opened == ["http://127.0.0.1:80/emulator/"]


def test_crashed_server_is_restarted_after_pause(controller):
    controller.iterate()
    controller.child.returncode = 1
    controller.iterate()
    assert "завершился" in controller.state_text and len(controller.launches) == 1
    controller.retry_at = 0
    controller.iterate()
    assert len(controller.launches) == 2


def test_restart_code_relaunches_immediately(controller):
    controller.iterate()
    controller.child.returncode = server_config.RESTART_CODE
    controller.iterate()
    assert len(controller.launches) == 2


def test_update_code_waits_for_plan(controller, tmp_path):
    """TASK-040: код UPDATE_CODE — трей не перезапускает сервер, ждёт план updater'а."""
    controller.iterate()
    controller.child.returncode = server_config.UPDATE_CODE
    controller.iterate()
    assert controller.updating and controller.state_text == "Обновление…"
    controller.iterate()  # плана ещё нет — ждём
    assert len(controller.launches) == 1


def test_update_plan_ok_relaunches_server_and_tray(controller, monkeypatch):
    controller.iterate()
    controller.child.returncode = server_config.UPDATE_CODE
    controller.iterate()
    spawned = []

    def fake_popen(*args, **kwargs):
        spawned.append(args[0])
        return type("P", (), {"pid": 1})()

    monkeypatch.setattr(windows_tray.subprocess, "Popen", fake_popen)
    plan = controller.state / "update" / "plan.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(json.dumps({"status": "ok", "target_tag": "v0.2.0"}), encoding="utf-8")
    exits = []
    controller.on_exit = lambda: exits.append(True)
    controller.iterate()
    assert len(controller.launches) == 2  # сервер запущен updater'ом завершён — трей поднял сервер
    assert spawned and "start_emulator.pyw" in str(spawned[0])  # и перезапустил себя
    assert exits == [True]
    controller.iterate()  # old exiting: no more relaunches
    assert len(controller.launches) == 2


def test_update_plan_failed_also_relaunches(controller, monkeypatch):
    controller.iterate()
    controller.child.returncode = server_config.UPDATE_CODE
    controller.iterate()
    monkeypatch.setattr(windows_tray.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    plan = controller.state / "update" / "plan.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
    controller.iterate()
    assert len(controller.launches) == 2  # и после неудачного обновления сервер возвращается


def test_stop_uses_graceful_api_and_escalates_when_stuck(controller, monkeypatch):
    controller.iterate()
    controller.fake.value = running(controller)
    controller.iterate()
    killed = []

    def kill(pid):
        killed.append(pid)
        controller.fake.value, controller.child.returncode = None, 1

    monkeypatch.setattr(controller, "kill_server", kill)
    controller.request("stop")
    controller.iterate()
    assert controller.fake.stops == 1 and controller.stopping
    controller.stop_deadline = time.monotonic() - 1      # остановка зависла
    controller.iterate()
    assert killed == [4242] and controller.state_text == "Сервер остановлен"
    assert len(controller.launches) == 1                 # остановлен по желанию — не перезапускается


def test_refuses_to_stop_foreign_server(controller):
    controller.fake.value = {"pid": 1, "state": str(controller.state.parent / "other")}
    controller.request("stop")
    controller.iterate()
    assert controller.fake.stops == 0 and controller.state_text == "На порту другой сервер"


def test_log_rotation(tmp_path):
    log = tmp_path / "server.log"
    for round_ in range(4):
        log.write_bytes(b"x" * 20)
        windows_tray.rotate(log, limit=10, keep=3)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["server.log.1", "server.log.2", "server.log.3"]


@pytest.mark.skipif(sys.platform != "win32", reason="mutex — Windows")
def test_second_instance_is_refused(tmp_path):
    import ctypes
    first = windows_tray.single_instance(tmp_path)
    try:
        assert first is not None
        assert windows_tray.single_instance(tmp_path) is None
    finally:
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(first))
