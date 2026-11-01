"""Запуск под systemd: супервизор переживает самообновление (server_emulator/service.py).

Проверяем то, что легко проверить без systemd и без настоящего updater'а: разбор `--state`,
ожидание итога в плане обновления, цикл «сервер → обновление → сервер» на заглушке процесса
и возврат кода для менеджера служб.
"""
import json
import signal
import threading
import time
from pathlib import Path

from server_emulator import service


def write_plan(state, pid, status):
    directory = Path(state) / "update"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plan.json").write_text(json.dumps({"pid": pid, "status": status}), encoding="utf-8")


class Child:
    """Заглушка subprocess.Popen: помнит сигналы, отдаёт заданный код."""

    def __init__(self, pid, code, on_wait=None):
        self.pid = pid
        self.code = code
        self.on_wait = on_wait
        self.signals = []

    def wait(self):
        if self.on_wait:
            self.on_wait()
        return self.code

    def send_signal(self, signum):
        self.signals.append(signum)


def test_state_from_args():
    assert service.state_from_args(["--state", "/data/state", "--port", "8443"]) == Path("/data/state")
    assert service.state_from_args(["--state=/data/state"]) == Path("/data/state")
    assert service.state_from_args(["--port", "8443"]).name == "state"   # умолчание сервера


def test_wait_update_ignores_foreign_plan(tmp_path):
    write_plan(tmp_path, 999, "ok")   # план прошлого обновления: чужой pid
    assert service.wait_update(tmp_path, 111, timeout=0.05, poll=0.01) is None


def test_wait_update_reads_status(tmp_path):
    write_plan(tmp_path, 111, "ok")
    assert service.wait_update(tmp_path, 111, timeout=0.05, poll=0.01) == "ok"


def test_wait_update_waits_for_finish(tmp_path):
    write_plan(tmp_path, 111, "running")
    threading.Timer(0.05, write_plan, (tmp_path, 111, "failed")).start()
    assert service.wait_update(tmp_path, 111, timeout=2, poll=0.01) == "failed"


def test_wait_update_without_plan(tmp_path):
    assert service.wait_update(tmp_path, 111, timeout=0.05, poll=0.01) is None


def test_wait_update_stops_early_without_own_plan(tmp_path):
    """Своего плана нет — не ждём полный таймаут (сервер мог выйти с кодом 76 случайно)."""
    write_plan(tmp_path, 999, "running")
    began = time.monotonic()
    assert service.wait_update(tmp_path, 111, timeout=30, poll=0.01, grace=0.05) is None
    assert time.monotonic() - began < 5


def test_wait_update_takes_plan_that_appears_late(tmp_path):
    threading.Timer(0.05, write_plan, (tmp_path, 111, "ok")).start()
    assert service.wait_update(tmp_path, 111, timeout=2, poll=0.01, grace=1) == "ok"


def test_supervise_restarts_after_update(tmp_path):
    write_plan(tmp_path, 111, "ok")   # сервер с pid 111 вышел на обновление, updater уже закончил
    codes = [service.UPDATE_CODE, 0]
    spawned = []

    def spawn():
        child = Child(111 if not spawned else 222, codes[len(spawned)])
        spawned.append(child)
        return child

    assert service.supervise(tmp_path, spawn, update_timeout=1) == 0
    assert [child.pid for child in spawned] == [111, 222]


def test_supervise_restarts_when_update_has_no_result(tmp_path):
    write_plan(tmp_path, 999, "running")   # updater завис/умер: план не про этот сервер
    codes = [service.UPDATE_CODE, 0]
    spawned = []

    def spawn():
        child = Child(111, codes[len(spawned)])
        spawned.append(child)
        return child

    assert service.supervise(tmp_path, spawn, update_timeout=0.05) == 0
    assert len(spawned) == 2


def test_supervise_returns_plain_exit_code(tmp_path):
    spawned = []

    def spawn():
        child = Child(111, 1)
        spawned.append(child)
        return child

    assert service.supervise(tmp_path, spawn, update_timeout=0.05) == 1
    assert len(spawned) == 1   # падение перезапускает systemd (Restart=on-failure), не супервизор


def test_supervise_stop_returns_zero(tmp_path):
    stopped = threading.Event()
    child = Child(111, 0, on_wait=stopped.set)   # пришёл SIGTERM, сервер завершился

    assert service.supervise(tmp_path, lambda: child, stopped=stopped) == 0
    assert len(child.signals) == 0   # сигнал уже обработан, перезапуска нет


def test_forward_ignores_dead_child():
    child = Child(111, 0)
    service.forward(child, signal.SIGTERM)
    assert child.signals == [signal.SIGTERM]

    class Dead(Child):
        def send_signal(self, signum):
            raise ProcessLookupError

    service.forward(Dead(112, 0), signal.SIGTERM)   # не падает


def test_run_returns_child_code_and_keeps_handlers(tmp_path):
    before = signal.getsignal(signal.SIGTERM)
    assert service.run(["--state", str(tmp_path)], spawn=lambda: Child(1, 0)) == 0
    assert signal.getsignal(signal.SIGTERM) == before
