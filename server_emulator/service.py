"""Запуск эмулятора под менеджером служб (systemd): супервизор, переживающий самообновление.

При обновлении сервер пишет план, запускает отделённый updater и завершается кодом
UPDATE_CODE (76): updater ждёт выхода сервера, бэкапит `state/`, переключает рабочую копию
на новый тег и — если его об этом не попросили иначе — поднимает сервер сам. Под systemd
сервер видит `INVOCATION_ID` и ставит в план `launch: manager` (self_update.Service.launch_mode),
то есть поднимать его должен менеджер служб. Простой `Restart=always` для этого не годится:
systemd поднимет старую версию сразу после выхода, пока updater ещё переключает файлы.

Этот модуль и есть «менеджер»: сервер живёт дочерним процессом, а после кода UPDATE_CODE
супервизор ждёт итог обновления в `<state>/update/plan.json` и запускает сервер снова — уже
с новым кодом. Штатный выход (0) и падение с другим кодом дальше не перезапускаются:
перезапуск при падении — дело юнита (`Restart=on-failure`).

Запуск (ExecStart юнита, см. deploy/systemd/alice-emulator.service и docs/deploy.md):

    python -m server_emulator.service --state /var/lib/alice-emulator

Остальные аргументы (--host, --port, --http-port, --cert, --key ...) передаются серверу как есть.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .server_config import RESTART_CODE, UPDATE_CODE

UPDATE_DIR = "update"   # как self_update.UPDATE_DIR: план и лог обновления в папке данных
PLAN_FILE = "plan.json"
UPDATE_TIMEOUT = 900    # сколько ждём updater: бэкап state/, fetch тегов, переключение, pip install
PLAN_GRACE = 60         # план пишется до выхода сервера: нет своего плана за это время — обновления нет
POLL_S = 1.0
STATUS_LABEL = {"ok": "готово — запускаю новую версию",
                "failed": "не удалось — запускаю прежнюю версию"}


def say(message):
    """Строка в stdout — journald покажет её как есть."""
    print("service: " + message, flush=True)


def state_from_args(argv):
    """Папка данных из `--state` (как у сервера); по умолчанию — `server_emulator/state`."""
    for index, item in enumerate(argv):
        if item == "--state" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if item.startswith("--state="):
            return Path(item.split("=", 1)[1])
    return Path(__file__).with_name("state")


def read_plan(state):
    """План обновления или None, если файла ещё нет/он битый."""
    try:
        plan = json.loads((Path(state) / UPDATE_DIR / PLAN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return plan if isinstance(plan, dict) else None


def wait_update(state, pid, timeout=UPDATE_TIMEOUT, poll=POLL_S, grace=PLAN_GRACE):
    """Итог обновления этого сервера: `ok`, `failed` или None (плана нет / updater не уложился).

    Сверяем pid из плана: план пишет сам сервер перед выходом, поэтому чужой (прошлый) план
    обновлением этого процесса не считается, а если своего плана нет за `grace` — обновление
    не начиналось и ждать 15 минут нечего.
    """
    started = time.monotonic()
    seen = False
    while True:
        plan = read_plan(state)
        if plan and plan.get("pid") == pid:
            seen = True
            if plan.get("status") in STATUS_LABEL:
                return plan["status"]
        spent = time.monotonic() - started
        if spent >= timeout or (not seen and spent >= grace):
            return None
        time.sleep(poll)


def forward(child, signum):
    """Сигнал остановки — серверу: он завершится сам, супервизор дождётся и выйдет."""
    try:
        child.send_signal(signum)
    except (ProcessLookupError, OSError):   # уже вышел — ждать нечего
        pass


def supervise(state, spawn, update_timeout=UPDATE_TIMEOUT, stopped=None):
    """Цикл «сервер → обновление → сервер». Возвращает код выхода для systemd.

    `spawn` запускает сервер и возвращает объект с `pid`, `wait()` и `send_signal()`
    (в тестах — заглушка, в бою — subprocess.Popen); `stopped` — событие «просят
    остановиться» (его ставит обработчик SIGTERM/SIGINT).
    """
    stopped = stopped or threading.Event()
    while True:
        child = spawn()
        say("сервер запущен (pid %d)" % child.pid)
        if stopped.is_set():      # сигнал пришёл раньше, чем сервер успел запуститься
            forward(child, signal.SIGTERM)
        code = child.wait()
        if stopped.is_set():
            say("остановка: сервер завершился с кодом %d" % code)
            return 0
        if code == RESTART_CODE:      # перезапуск настроек: сервер обычно перезапускает себя сам
            say("перезапуск по настройкам")
            time.sleep(1)             # пауза: не крутимся вхолостую, если порты так и не открылись
            continue
        if code != UPDATE_CODE:
            say("сервер завершился с кодом %d" % code)
            return code
        say("идёт обновление — жду updater")
        status = wait_update(state, child.pid, update_timeout)
        say("обновление: " + STATUS_LABEL.get(status, "без итога — запускаю как есть"))


def run(argv, spawn=None, update_timeout=UPDATE_TIMEOUT):
    """Супервизор с обработчиками сигналов. Возвращает код выхода процесса."""
    state = state_from_args(argv)
    if spawn is None:
        def start():
            return subprocess.Popen([sys.executable, "-m", "server_emulator.server", *argv],
                                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
        spawn = start
    holder = {}
    stopped = threading.Event()

    def spawn_child():
        child = spawn()
        holder["child"] = child
        return child

    def on_stop(signum, _frame):
        # systemd шлёт SIGTERM супервизору (KillMode=mixed) — серверу передаём его же
        stopped.set()
        child = holder.get("child")
        if child is not None:
            forward(child, signum)

    previous = {number: signal.signal(number, on_stop) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        return supervise(state, spawn_child, update_timeout, stopped)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv=None):
    sys.exit(run(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
