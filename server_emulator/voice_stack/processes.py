"""Жизненный цикл локальных речевых серверов: поиск, остановка деревом, проверка порта и видеопамяти.

Процессы пакета ищутся тремя путями (всё через psutil, чужие процессы не трогаются):

* реестр панели — `LocalSpeech.processes` (Popen, запущенный этим экземпляром);
* `<папка пакета>/server.pid` — процесс прошлого экземпляра панели (PID проверяется по командной
  строке: переиспользованный системой PID чужого процесса не считается нашим);
* сканирование всех процессов: интерпретатор из `<папка пакета>/venv` (кроме `-m pip|venv`) либо
  скрипт сервера пакета с рабочим каталогом в папке пакета — так находятся осиротевшие серверы
  и их рабочие процессы после падения панели.

Остановка — всегда деревом: на Windows `venv\\Scripts\\python.exe` — пускатель, настоящий сервер
его дочерний процесс, а рабочие процессы (multiprocessing) — внуки.
"""
import os
import shutil
import subprocess
import time

import psutil

TERMINATE_TIMEOUT = 5.0   # мягкое завершение, затем kill
PORT_TIMEOUT = 5.0        # сколько ждать освобождения порта
GPU_TIMEOUT = 5.0         # сколько ждать освобождения видеопамяти
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _norm(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _inside(path, folder):
    path = _norm(path)
    return path == folder or path.startswith(folder + os.sep)


def _info(proc):
    """(cmdline, exe) процесса; недоступное — пусто."""
    info = getattr(proc, "info", None) or {}
    try:
        cmd = info["cmdline"] if info.get("cmdline") is not None else proc.cmdline()
        exe = info["exe"] if info.get("exe") is not None else proc.exe()
    except (psutil.Error, KeyError, OSError):
        return [], ""
    return list(cmd or []), exe or ""


def belongs(proc, folder, script):
    """Процесс — сервер пакета из этой папки (а не установка pip и не чужой процесс)."""
    folder = _norm(folder)
    cmd, exe = _info(proc)
    if not cmd:
        return False
    if any(_inside(item, folder) for item in (cmd[0], exe) if item):
        return not (len(cmd) > 2 and cmd[1] == "-m" and cmd[2] in ("pip", "venv", "ensurepip"))
    if script and any(_norm(arg) == _norm(script) for arg in cmd[1:] if arg and not arg.startswith("-")):
        try:
            return _norm(proc.cwd()) == folder
        except (psutil.Error, OSError):
            return False
    return False


def snapshot():
    return list(psutil.process_iter(["pid", "ppid", "exe", "cmdline"]))


def find(folder, script, registered_pid=None, pid_file=None, processes=None):
    """Корневые процессы сервера пакета. registered_pid — PID из реестра панели (доверяем: запустили мы),
    pid_file — server.pid (проверяется по командной строке; устаревший файл удаляется)."""
    found = {}
    if registered_pid:
        try:
            found[registered_pid] = psutil.Process(registered_pid)
        except psutil.Error:
            pass
    if pid_file is not None:
        try:
            pid = int(pid_file.read_text(encoding="ascii"))
        except (OSError, ValueError):
            pid = None
        if pid is not None:
            try:
                candidate = psutil.Process(pid)
                if belongs(candidate, folder, script):
                    found[pid] = candidate
                else:
                    pid_file.unlink(missing_ok=True)  # PID чужой или процесса нет: файл устарел
            except psutil.Error:
                pid_file.unlink(missing_ok=True)
    for proc in processes if processes is not None else snapshot():
        if proc.pid not in found and proc.pid != os.getpid() and belongs(proc, folder, script):
            found[proc.pid] = proc
    members = set(found)
    return [p for p in found.values() if _parent(p) not in members]


def _parent(proc):
    try:
        return proc.ppid()
    except psutil.Error:
        return None


def tree(roots):
    """Корни и все их потомки (без повторов)."""
    seen = {}
    for root in roots:
        try:
            for proc in [root, *root.children(recursive=True)]:
                seen.setdefault(proc.pid, proc)
        except psutil.Error:
            continue
    return list(seen.values())


def terminate(procs, timeout=TERMINATE_TIMEOUT):
    """Мягко завершить процессы, по таймауту kill; вернуть оставшиеся живыми."""
    for proc in procs:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(alive, timeout=timeout)
    return alive


def kill_tree(pid, timeout=TERMINATE_TIMEOUT):
    """Остановить процесс вместе с дочерними; вернуть PID оставшихся живыми."""
    try:
        procs = tree([psutil.Process(pid)])
    except psutil.Error:
        return []
    return [p.pid for p in terminate(procs, timeout)]


def listeners(port):
    """PID процессов, слушающих TCP-порт на локальном адресе ({None} — владелец неизвестен)."""
    result = set()
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                result.add(conn.pid)
    except (psutil.Error, OSError):
        pass
    return result


def wait_port_free(port, own, timeout=PORT_TIMEOUT):
    """Дождаться, пока порт никто не слушает. Возвращает PID оставшихся слушателей: чужих — их не трогаем."""
    deadline = time.monotonic() + timeout
    while True:
        busy = listeners(port)
        if not busy or time.monotonic() >= deadline:
            return sorted(p for p in busy if p is not None and p not in own) + ([0] if None in busy else [])
        time.sleep(0.2)


def process_name(pid):
    try:
        return psutil.Process(pid).name()
    except psutil.Error:
        return "процесс"


def gpu_state():
    """{"used": {индекс: МБ}, "pids": {PID: МБ|None}} по nvidia-smi или None (нет видеокарты/утилиты)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None

    def query(*args):
        done = subprocess.run([exe, *args, "--format=csv,noheader,nounits"], capture_output=True, text=True,
                              timeout=8, creationflags=NO_WINDOW)
        return [[part.strip() for part in line.split(",")] for line in done.stdout.splitlines() if line.strip()]

    def number(text):
        return int(text) if text.isdigit() else None
    try:
        used = {row[0]: number(row[1]) or 0 for row in query("--query-gpu=index,memory.used")}
        apps = {number(row[0]): number(row[1]) for row in query("--query-compute-apps=pid,used_memory")
                if number(row[0]) is not None}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    return {"used": used, "pids": apps}


def wait_gpu_released(pids, timeout=GPU_TIMEOUT):
    """Дождаться, пока процессы пропадут из списка вычислительных приложений видеокарты.
    Возвращает (оставшиеся PID, состояние) либо None, если видеокарту опросить нельзя."""
    deadline = time.monotonic() + timeout
    while True:
        state = gpu_state()
        if state is None:
            return None
        held = sorted(set(pids) & set(state["pids"]))
        if not held or time.monotonic() >= deadline:
            return held, state
        time.sleep(0.3)
