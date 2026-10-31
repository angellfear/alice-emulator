"""Updater самообновления (TASK-040): отдельный процесс, схема Sonarr/Radarr.

Сервер пишет план в state/update/plan.json, копирует этот файл в state/update/ и запускает
отделённым процессом тем же интерпретатором, затем штатно завершается (код UPDATE_CODE).
Updater ждёт выхода сервера, бэкапит данные state/ в zip с ротацией, fetch'ит теги origin и
переключается на целевой тег (ветка — fast-forward, иначе checkout), при изменении pyproject
между релизами — pip install -e ".[emulator]", запускает сервер с прежними аргументами и ждёт
ответа /__control/status с новой версией. Сбой на любом шаге — возврат на прежний коммит,
повтор шага зависимостей, запуск; в план пишется failed с логом. Под треем и менеджером служб
(launch: tray|manager) сервер updater'ом не запускается: трей ждёт план после UPDATE_CODE,
менеджер перезапускает службу сам.

Файл копируется в state/update/ (вне отслеживаемого дерева): замена файлов рабочей копии не
должна задевать исполняемый код updater'а. Импорт self_update/repo — из рабочей копии по
plan["root"]; сам модуль к переключению не относится. Запуск: python state/update/updater.py <state>.
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

PLAN_FILE = "plan.json"
LOG_LIMIT = 200 * 1024
BACKUPS_KEEP = 5
START_TIMEOUT = 240  # сервер с LangChain поднимается не мгновенно


def note(state, text):
    """Лог обновления — state/update/update.log (хвост читает UI)."""
    try:
        path = Path(state) / "update" / "update.log"
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def read_plan(state):
    return json.loads((Path(state) / "update" / PLAN_FILE).read_text(encoding="utf-8"))


def write_plan(state, plan):
    """Атомарная запись плана: итог ok/failed ждёт трей (UPDATE_CODE), статус читает UI."""
    path = Path(state) / "update" / PLAN_FILE
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def wait_pid(pid, timeout=600):
    """Выход старого сервера: файлы процесса (загруженные .pyd Windows) заблокированы до выхода."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return
        time.sleep(0.5)
    raise RuntimeError("сервер не завершился за 10 минут")


def alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def backup(state, target_tag):
    """Данные пользователя (БД, настройки, сценарии, расширения) → zip в state/backups/, ротация до 5."""
    state = Path(state)
    backups = state / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    path = backups / ("update-" + time.strftime("%Y%m%d-%H%M%S") + "-" + target_tag + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(state.glob("*.json")):
            archive.write(item, item.name)
        for db in ("emulator.sqlite3", "emulator.sqlite3-wal", "emulator.sqlite3-shm"):
            if (state / db).exists():
                archive.write(state / db, db)
        for folder in ("scenarios", "extensions", "animations", "endpoints"):
            base = state / folder
            if not base.exists():
                continue
            for file in base.rglob("*"):
                if file.is_file():
                    archive.write(file, str(file.relative_to(state)))
    for extra in sorted(backups.glob("update-*.zip"))[:-BACKUPS_KEEP]:
        try:
            extra.unlink()
        except OSError:
            pass
    return str(path)


def deps_changed(repo, old_commit, new_commit):
    """Сравнение pyproject.toml между релизами по деревьям коммитов, без git CLI."""
    from server_emulator.self_update import repo as git_repo
    return git_repo.blob_at(repo, old_commit, "pyproject.toml") != git_repo.blob_at(repo, new_commit, "pyproject.toml")


def pip_install(state, root, python, log):
    """Зависимости из pyproject: editable — код нового тега уже в рабочей копии."""
    command = [str(python), "-m", "pip", "install", "-q", "-e", ".[emulator]"]
    note(state, "pip: " + " ".join(command))
    result = subprocess.run(command, cwd=str(root), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError("pip install завершился с ошибкой: " + (result.stderr or result.stdout)[-400:])
    log.append("зависимости обновлены")


def start_server(plan, log):
    """Запуск сервера с прежними аргументами — только когда никто другой этого не делает."""
    if plan["launch"] != "self":
        log.append("сервер запускает " + plan["launch"])
        return None
    process = subprocess.Popen([str(plan["python"]), *plan["args"]], cwd=str(plan["root"]),
                               stdin=subprocess.DEVNULL)
    log.append("сервер запущен (pid %d)" % process.pid)
    return process


def wait_healthy(state, old_pid, target_version, timeout=START_TIMEOUT):
    """Ответ /__control/status с новой версией: ключ управления новый сервер пишет сам при старте."""
    sys.path.insert(0, str(Path(state).resolve().parents[1]))
    from server_emulator import control, server_config
    url = server_config.local_url(server_config.load(state))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            request = urllib.request.Request(url + "/__control/status",
                                             headers={control.HEADER: control.read_token(state)})
            with urllib.request.urlopen(request, timeout=3, context=ssl._create_unverified_context()) as response:
                value = json.load(response)
            if value.get("pid") not in (None, old_pid) and value.get("version") == target_version:
                return
        except Exception:
            continue  # ещё не поднялся / ещё старый ключ
    raise RuntimeError("сервер не ответил с версией " + target_version + " за " + str(timeout) + " с")


def run(state):
    """Полный проход обновления по плану. Возвращает план с итогом ok/failed."""
    plan = read_plan(state)
    root = Path(plan["root"])
    sys.path.insert(0, str(root))
    from server_emulator.self_update import repo as git_repo
    from dulwich import porcelain as dul_porcelain
    log, deps_ran = [], False

    def finish(status):
        plan.update(status=status, finished_at=time.time(), log="; ".join(log))
        write_plan(state, plan)
        note(state, "обновление на " + plan["target_tag"] + ": " + status)

    try:
        wait_pid(plan["pid"])
        plan["backup"] = backup(state, plan["target_tag"])
        log.append("бэкап: " + plan["backup"])
        repo = git_repo.discover(root)
        version_tags = git_repo.fetch_tags(repo, git_repo.origin_url(repo))
        tag = plan["target_tag"]
        if tag.lstrip("v") not in version_tags:
            raise RuntimeError("тег " + tag + " не найден в origin")
        tag_sha = version_tags[tag.lstrip("v")]
        mode = git_repo.switch(repo, tag, tag_sha)
        log.append("переключение: " + tag + " (" + mode + ")")
        if deps_changed(repo, plan["current_commit"], tag_sha):
            pip_install(state, root, plan["python"], log)
            deps_ran = True
        if plan["launch"] == "self":
            start_server(plan, log)
            wait_healthy(state, plan["pid"], plan["target_version"])
        finish("ok")
    except Exception as exc:  # noqa: BLE001 — сбой любого шага: откат и отчёт в плане
        log.append("ОШИБКА: " + type(exc).__name__ + ": " + str(exc)[:200])
        try:
            repo = git_repo.discover(root)
            dul_porcelain.reset(repo, "hard", plan["current_commit"])
            log.append("возвращён прежний коммит " + plan["current_commit"][:7])
            if deps_ran:  # повтор шага зависимостей: откат вернул прежний pyproject
                pip_install(state, root, plan["python"], log)
            if plan["launch"] == "self":
                start_server(plan, log)
            finish("failed")
        except Exception as rollback_exc:  # noqa: BLE001
            log.append("откат не удался: " + type(rollback_exc).__name__ + ": " + str(rollback_exc)[:150])
            finish("failed")
    return plan


def main():
    state = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    run(state)
    sys.exit(0)


if __name__ == "__main__":
    main()
