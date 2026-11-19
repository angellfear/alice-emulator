"""Трей-лаунчер — контроллер с подменёнными процессами и сервером, без pystray и окон."""
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
        self.update_value, self.update_calls = None, 0

    def status(self):
        return self.value

    def stop(self):
        self.stops += 1

    def url(self):
        return "http://127.0.0.1:80"

    def panel(self):
        return self.url() + "/emulator/"

    def update_status(self):
        self.update_calls += 1
        if isinstance(self.update_value, type) and issubclass(self.update_value, Exception):
            raise self.update_value("offline")
        return self.update_value


@pytest.fixture
def controller(tmp_path, monkeypatch):
    state = tmp_path / "state"
    fake = FakeServer(state)
    opened, notices = [], []
    item = windows_tray.Controller(root=tmp_path, state=state, server=fake, opener=opened.append,
                                   open_panel=True, on_update=notices.append)
    launches = []

    def launch():
        launches.append(time.monotonic())
        item.launched_at = launches[-1]
        item.child = FakeChild()

    monkeypatch.setattr(item, "launch", launch)
    monkeypatch.setattr(windows_tray, "process_alive", lambda pid: False)
    item.fake, item.launches, item.opened, item.notices = fake, launches, opened, notices
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


def test_busy_ports_backs_off_and_tells(controller):
    """Мгновенная смерть при занятых портах — пауза в минуту и честный статус
    вместо петли запусков каждые 10 секунд (инцидент 2026-11-20)."""
    controller.iterate()
    controller.child.returncode = 1
    (controller.logs / "server.log").write_text("Ни один порт не открыт: порт 443: …\n", encoding="utf-8")
    controller.iterate()
    assert controller.state_text == "Порты заняты другим сервером"
    assert len(controller.launches) == 1
    controller.iterate()  # retry_at ещё далеко — не долбим запусками
    assert len(controller.launches) == 1
    controller.retry_at = 0
    controller.iterate()
    assert len(controller.launches) == 2


def test_slow_death_keeps_short_retry(controller):
    """Падение в работе (прожил дольше FAST_EXIT) — прежняя пауза 10 с, не «порты заняты»."""
    controller.iterate()
    controller.child.returncode = 1
    controller.launched_at -= windows_tray.RETRY * 10  # прожил «долго»
    (controller.logs / "server.log").write_text("Ни один порт не открыт: порт 443: …\n", encoding="utf-8")
    controller.iterate()
    assert "завершился" in controller.state_text and len(controller.launches) == 1


def test_running_server_of_same_state_is_own(controller):
    """Здоровый сервер своего набора данных (осиротевший, не ребёнок трея) — свой:
    трей показывает «Сервер работает» и не запускает дубликат поверх него."""
    controller.fake.value = {"pid": 9999, "state": str(controller.state)}
    controller.iterate()
    assert controller.ready and controller.pid == 9999 and controller.state_text == "Сервер работает"
    assert len(controller.launches) == 0


def test_restart_code_relaunches_immediately(controller):
    controller.iterate()
    controller.child.returncode = server_config.RESTART_CODE
    controller.iterate()
    assert len(controller.launches) == 2


def test_update_code_waits_for_plan(controller, tmp_path):
    """Код UPDATE_CODE — трей не перезапускает сервер, ждёт план updater'а."""
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


def test_update_available_notifies_once_per_version(controller):
    """Новая версия — уведомление один раз на версию; смена версии уведомляет снова."""
    controller.iterate()
    assert controller.notices == [] and controller.fake.update_calls == 0  # сервер ещё не готов
    controller.fake.value = running(controller)
    controller.fake.update_value = {"available": True, "latest": {"version": "1.2.0"}}
    controller.iterate()
    assert controller.notices == ["1.2.0"]
    controller.iterate()  # тот же ответ: троттлинг минуты и «один раз на версию»
    assert controller.notices == ["1.2.0"]
    controller.fake.update_value = {"available": True, "latest": {"version": "1.3.0"}}
    controller.update_notice_at = 0
    controller.iterate()
    assert controller.notices == ["1.2.0", "1.3.0"]


def test_no_notice_when_update_not_available(controller):
    """«Позже» и «Пропустить версию» гасят available в API — трей молчит вместе с панелью."""
    controller.fake.value = running(controller)
    controller.fake.update_value = {"available": False, "latest": {"version": "1.2.0"}}
    controller.iterate()
    assert controller.notices == []


def test_update_status_failure_is_silent(controller):
    """Сервер не ответил на запрос статуса — итерация трея не падает, поворот по расписанию."""
    controller.fake.value = running(controller)
    controller.fake.update_value = RuntimeError
    controller.iterate()
    assert controller.notices == [] and controller.state_text == "Сервер работает"


@pytest.mark.no_state_template
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
