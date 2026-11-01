"""Самообновление эмулятора (TASK-040): проверка релизов GitHub и обновление по кнопке.

Service живёт на Runtime (runtime.self_update): фоновая проверка Releases API с задержкой
и затем раз в interval_hours, статус для API/UI и подготовка плана обновления. Само обновление
делает отдельный процесс — self_update/updater.py (схема Sonarr/Radarr); сервер только пишет
план, копирует updater в state/update/ и завершается кодом UPDATE_CODE. Автоматически ничего
не ставится: обновление и откат — только по клику в окне.
"""
import asyncio
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import ClientError

from .. import auth, version
from ..server_log import log_event  # TASK-068: события самообновления — в журнал сервера
from . import check, repo

UPDATE_DIR = "update"
DEFAULT_REPO = "angellfear/alice-emulator"  # константа публичного репозитория — дефолт настройки


class Service:
    def __init__(self, runtime, root=None):
        self.runtime = runtime
        self.root = Path(root) if root else version.ROOT
        self.last = {}  # последний ответ Releases API: release, etag, checked_at, error
        self.snoozed = False  # «Позже» — до следующей плановой проверки
        self.manual = asyncio.Event()  # «Проверить сейчас» не ждёт интервала
        self.task = None
        self._reported_release = None  # версию о находке пишет один раз (TASK-068)

    # --- фоновая проверка -------------------------------------------------------------

    def start(self):
        self.task = asyncio.get_running_loop().create_task(self._loop())

    async def close(self):
        task = getattr(self, "task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        """Проверка при старте (с задержкой — серверу надо подняться) и затем раз в interval_hours."""
        await asyncio.sleep(45)
        self._report_last_update()  # TASK-068: итог обновления/отката от updater (отдельный процесс)
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001 — ошибки сети уже в статусе, цикл не должен умирать
                pass
            hours = (self.settings() or {}).get("interval_hours") or 12
            try:
                await asyncio.wait_for(self.manual.wait(), timeout=hours * 3600)
            except asyncio.TimeoutError:
                pass
            self.manual.clear()

    def settings(self):
        value = self.runtime.settings.get("self_update")
        return value if isinstance(value, dict) else None

    # --- проверка ---------------------------------------------------------------------

    async def check(self, manual=False):
        """Один проход проверки: результат и ошибки сети — в self.last, без исключений.
        manual — «Проверить сейчас» из окна: работает и при выключенном автообновлении
        (галочка отключает только фоновую проверку, кнопка-индикатор от неё не зависит)."""
        self.snoozed = False  # «Позже» действует до следующей проверки
        settings = self.settings() or {}
        result = {"checked_at": time.time(), "release": None, "etag": None, "error": None}
        if not settings.get("enabled", True) and not manual:
            self.last = result
            return result
        try:
            answer = await check.latest(settings.get("repo") or DEFAULT_REPO,
                                        token=auth.secret(settings.get("token", "")),
                                        channel=settings.get("channel", "stable"),
                                        etag=self.last.get("etag"))
            if answer["release"] is not None:
                result["release"] = answer["release"]
            elif self.last.get("release"):
                result["release"] = self.last["release"]  # 304 Not Modified: релиз не менялся
            result["etag"] = answer["etag"]
        except (LookupError, ClientError, OSError, asyncio.TimeoutError) as exc:
            result["error"] = str(exc)
        self._log_check(result)  # TASK-068: находка версии и сбой проверки — в журнал сервера
        self.last = result
        return result

    def _log_check(self, result):
        """События проверки в журнал: новая доступная версия — один раз на версию,
        сбой проверки — при переходе в состояние ошибки (повторы склеивает журнал)."""
        release = self._available(version.current(self.root))
        if release and self._reported_release != release["version"]:
            self._reported_release = release["version"]
            log_event("info", "self_update",
                      f"Доступна новая версия {release['version']} (сейчас {version.current(self.root)['version']})")
        error = result.get("error")
        if error and not self.last.get("error"):
            log_event("warning", "self_update", f"Проверка обновлений не удалась: {error}")

    def _report_last_update(self):
        """Итог последнего обновления/отката (plan.json писал updater отдельным процессом) —
        в журнал один раз при старте, если updater закончил недавно."""
        try:
            plan = json.loads((self.runtime.directory / UPDATE_DIR / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        status, finished = plan.get("status"), plan.get("finished_at")
        if status not in ("ok", "failed") or not isinstance(finished, (int, float)) \
                or finished < time.time() - 600:
            return
        verb = "Откат" if plan.get("action") == "rollback" else "Обновление"
        if status == "ok":
            log_event("info", "self_update", f"{verb} до {plan.get('target_version')} завершено успешно")
        else:
            log_event("error", "self_update",
                      f"{verb} до {plan.get('target_version')} не удалось (вернулась версия "
                      f"{plan.get('previous_tag')}); лог: {UPDATE_DIR}/update.log")

    def _available(self, current):
        """Новый релиз есть и он старше текущей версии (packaging, пререлиз — только канал beta)."""
        release = self.last.get("release")
        if not release or not release.get("version"):
            return None
        try:
            from packaging.version import Version
            if Version(release["version"]) <= Version(current["version"] or "0"):
                return None
        except ValueError:  # не semver в теге — не предлагаем
            return None
        return release

    # --- статус для API/UI ------------------------------------------------------------

    def status(self, app=None):
        """Статус для GET api/self_update. can_update — про рабочую копию; обновление ещё требует
        штатного выхода процесса (EXIT_KEY от serve) — из встроенного прокси обновляться нельзя."""
        from ..control import EXIT_KEY
        current = version.current(self.root)
        settings = self.settings() or {}
        release = self._available(current)
        can, reasons, files = repo.can_update(self.root, settings.get("repo") or DEFAULT_REPO)
        if app is not None and app.get(EXIT_KEY) is None:
            can, reasons = False, reasons + ["сервер запущен не самостоятельно (встроенный режим)"]
        skipped = bool(release) and release["version"] == settings.get("skipped")
        return {
            "current": current,
            "latest": release,  # None — новой версии нет; в skipped-случае релиз всё равно виден
            # Кнопка-индикатор — ручной режим: от галочки автообновления не зависит
            "available": bool(release) and not skipped and not self.snoozed,
            "skipped": bool(release) and skipped,
            "can_update": can,
            "reasons": reasons,
            "blocked_files": files,
            "last_check": self.last.get("checked_at"),
            "error": self.last.get("error"),
            "last_update": self._last_update(),
            "previous": self._previous(current["version"]),
            "installed_kind": "git" if repo.is_clone(self.root) else "zip",
        }

    def _previous(self, current_version):
        """Предыдущий тег для отката: наибольший semver-тег ниже текущей версии."""
        try:
            from packaging.version import Version
            lower = [(Version(number), number) for number in repo.tags(repo.discover(self.root))
                     if Version(number) < Version(current_version or "0")]
            return max(lower)[1] if lower else None
        except Exception:  # noqa: BLE001 — нет git или тегов: отката нет
            return None

    def _last_update(self):
        """Итог последнего обновления из плана: status, лог, откуда и куда."""
        try:
            plan = json.loads((self.runtime.directory / UPDATE_DIR / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {"status": plan.get("status"), "log": plan.get("log") or "",
                "from": plan.get("previous_tag"), "to": plan.get("target_tag"),
                "finished_at": plan.get("finished_at")}

    # --- подготовка обновления ----------------------------------------------------------

    def launch_mode(self):
        """Кто поднимает сервер после обновления: трей (UPDATE_CODE), менеджер служб или updater."""
        if os.environ.get("ALICA_LAUNCHER"):
            return "tray"
        settings = self.settings() or {}
        if settings.get("restart") == "manager" or os.environ.get("INVOCATION_ID"):
            return "manager"
        return "self"

    def plan(self, target_version, action):
        """План для updater: целевой тег, прежний тег, коммит, pid, аргументы запуска, интерпретатор.
        rollback переключает на предыдущий тег тем же путём (previous_tag — текущий)."""
        current = version.current(self.root)
        if action == "rollback":
            previous, target = current["version"], target_version
        else:
            previous, target = self._previous(current["version"]), target_version
        return {
            "action": action,
            "target_tag": "v" + target,
            "target_version": target,
            "previous_tag": ("v" + previous) if previous else None,
            "current_commit": self._full_commit(),
            "pid": os.getpid(),
            "python": sys.executable,
            "args": list(sys.orig_argv[1:]),
            "root": str(self.root),
            "state": str(self.runtime.directory),
            "launch": self.launch_mode(),
            "status": "running",
            "started_at": time.time(),
        }

    def _full_commit(self):
        from dulwich.repo import Repo
        try:
            return Repo(str(self.root)).head().decode()
        except Exception:  # noqa: BLE001 — без git обновление и так отказано
            return ""

    def spawn_updater(self, plan):
        """plan.json + копия updater.py в state/update/, отделённый процесс тем же интерпретатором."""
        directory = self.runtime.directory / UPDATE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copyfile(Path(__file__).with_name("updater.py"), directory / "updater.py")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, str(directory / "updater.py"), str(self.runtime.directory)],
                         cwd=str(self.root), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env={**os.environ, "PYTHONUNBUFFERED": "1"}, **kwargs)
