"""Смена источника голосового стека: план, остановка ненужных серверов, проверка и запуск нового.

`Lifecycle.plan` — что будет остановлено и запущено; `Lifecycle.run` выполняет план шагами
(остановка → порт → видеопамять → запись настроек → запуск → готовность) и ведёт журнал шагов `Job`,
который панель отдаёт через `GET /api/voice-stack-apply?job=`. Остановка нужного новой конфигурации
сервера не происходит; старый движок после смены не остаётся: ищутся и осиротевшие процессы прошлых
запусков (см. processes.py). `shutdown` завершает все серверы при остановке панели.
"""
import asyncio
import copy
import logging
import uuid

import psutil

from . import SERVICES, processes

log = logging.getLogger(__name__)

READY_TIMEOUT = 90.0  # сколько ждать ответа /health нового сервера (загрузка модели)
KEEP_JOBS = 8


class Job:
    """Журнал одного применения: шаги {n, kind, title, status: run|ok|warn|fail, detail, pid?}."""

    def __init__(self, service, ref, title):
        self.id = uuid.uuid4().hex[:8]
        self.service, self.ref, self.title = service, ref, title
        self.steps, self.state, self.summary = [], "running", ""

    def step(self, kind, title, **extra):
        item = {"n": len(self.steps) + 1, "kind": kind, "title": title, "status": "run", "detail": "", **extra}
        self.steps.append(item)
        return item

    def finish(self, state, summary):
        self.state, self.summary = state, summary

    def public(self):
        return {"id": self.id, "service": self.service, "ref": self.ref, "title": self.title,
                "state": self.state, "summary": self.summary, "steps": copy.deepcopy(self.steps)}


class Lifecycle:
    def __init__(self, stack):
        self.stack = stack
        self.jobs = {}
        self.lock = asyncio.Lock()
        self.ready_timeout = READY_TIMEOUT
        self._tasks = set()

    # --- что где запущено --------------------------------------------------------
    def _all_packages(self):
        return [p for items in self.stack.packages().values() for p in items]

    def discover(self, snapshot=None):
        """Работающие серверы всех пакетов: [{package, roots, pids, port}]. Пакеты с идущей установкой
        пропускаются (их pip — не сервер)."""
        local = self.stack.local
        shot = snapshot if snapshot is not None else processes.snapshot()
        result = []
        for package in self._all_packages():
            job = local.jobs.get(package["id"])
            folder = local.folder(package)
            if (job and job["stage"] not in ("done", "error")) or not folder.is_dir():
                continue
            registered = local.processes.get(package["id"])
            roots = processes.find(
                folder, local.script(package),
                registered.pid if registered is not None and registered.poll() is None else None,
                folder / "server.pid", shot)
            if roots:
                everyone = processes.tree(roots)
                result.append({"package": package, "roots": roots, "procs": everyone,
                               "pids": [p.pid for p in everyone], "port": int(local.options(package).get("port") or 0)})
        return result

    @staticmethod
    def _public(engine):
        package = engine["package"]
        return {"id": package["id"], "title": package["title"], "service": package["service"],
                "pid": engine["roots"][0].pid, "pids": engine["pids"], "port": engine["port"]}

    def wanted(self, service, ref):
        """id пакетов, нужных конфигурации после смены service → ref. Озвучка выключена — сервер TTS
        не нужен (как в autostart), если только его не применяют прямо сейчас."""
        from ..spoken import voice_output
        speak = voice_output(self.stack.runtime)[0]
        result = set()
        for name in SERVICES:
            source = ref if name == service else self.stack.active(name)
            kind, _, identifier = source.partition(":")
            if kind == "k" and (name != "tts" or speak or name == service):
                result.add(identifier)
        return result

    async def engines(self):
        return [self._public(e) for e in await asyncio.to_thread(self.discover)]

    async def plan(self, service, ref):
        """Что произойдёт при применении: {"stop": [...], "start": {...}|None, "keep": [...]}.
        Ошибка конфигурации (пакет не установлен и т.п.) — ValueError, как у apply."""
        _, package = self.stack.config(service, ref)
        wanted = self.wanted(service, ref)
        found = await asyncio.to_thread(self.discover)
        stop = [self._public(e) for e in found if e["package"]["id"] not in wanted]
        keep = [self._public(e) for e in found if e["package"]["id"] in wanted]
        start = None
        if package and not (package["id"] in {e["id"] for e in keep} and await self.stack.local.healthy(package)):
            start = {"id": package["id"], "title": package["title"], "port": self.stack.local.options(package).get("port")}
        return {"stop": stop, "start": start, "keep": keep}

    # --- применение --------------------------------------------------------------
    def begin(self, service, ref):
        """Журнал нового применения (для фонового запуска: id нужен до начала работы)."""
        _, package = self.stack.config(service, ref)  # ошибка конфигурации — сразу, до окна
        title = package["title"] if package else next(
            (p["title"] for p in self.stack.presets(service) if "p:" + p["id"] == ref), ref)
        job = Job(service, ref, title)
        self.jobs[job.id] = job
        for old in list(self.jobs)[:-KEEP_JOBS]:
            self.jobs.pop(old, None)
        return job

    def start_background(self, job):
        if self.lock.locked():
            self.jobs.pop(job.id, None)
            raise ValueError("Применение уже идёт — дождитесь окончания")
        task = asyncio.get_running_loop().create_task(self._guarded(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, job):
        try:
            await self.run(job.service, job.ref, job)
        except Exception as exc:  # noqa: BLE001 — причина уже в журнале; здесь только страховка
            log.debug("Применение %s: %s", job.ref, exc)
            if job.state == "running":
                job.finish("error", str(exc))

    async def run(self, service, ref, job=None):
        """Применить источник. Возвращает {"service", "active"}; сбой — ValueError с причиной
        (журнал шагов остаётся в job)."""
        job = job or self.begin(service, ref)
        async with self.lock:
            try:
                return await self._run(service, ref, job)
            except Exception as exc:
                if job.state == "running":
                    job.finish("error", str(exc))
                raise

    async def _run(self, service, ref, job):
        stack = self.stack
        config, package = stack.config(service, ref)
        wanted = self.wanted(service, ref)
        found = await asyncio.to_thread(self.discover)
        stale = [e for e in found if e["package"]["id"] not in wanted]
        failed = []
        gpu_before = await asyncio.to_thread(processes.gpu_state) if stale else None
        for engine in stale:
            if not await self._stop(job, engine, gpu_before):
                failed.append(engine["package"]["title"])
        if failed:
            raise ValueError("Не удалось остановить: " + ", ".join(failed) + " — источник не изменён")
        await stack.save_source(service, ref, config)
        if package is not None:
            await self._start(job, package)
        if not job.steps:
            job.step("info", "Остановить и запускать нечего — источник применён").update(status="ok")
        warn = any(s["status"] == "warn" for s in job.steps)
        job.finish("warn" if warn else "ok", "Применено с замечанием" if warn else "Применено")
        return {"service": service, "active": ref}

    async def _stop(self, job, engine, gpu_before):
        package, local = engine["package"], self.stack.local
        step = job.step("stop", f"Останавливается: {package['title']}", pid=engine["roots"][0].pid, pids=engine["pids"])
        alive = await asyncio.to_thread(processes.terminate, engine["procs"])
        popen = local.processes.pop(package["id"], None)
        if popen is not None:
            await asyncio.to_thread(_reap, popen)
        if alive:
            step.update(status="fail", detail="процесс не завершился даже после kill: PID " + ", ".join(str(p.pid) for p in alive))
            return False
        (local.folder(package) / "server.pid").unlink(missing_ok=True)
        step.update(status="ok", detail=f"остановлено процессов: {len(engine['pids'])}")
        ok = True
        if engine["port"]:
            port = job.step("port", f"Порт {engine['port']} освобождается")
            busy = await asyncio.to_thread(processes.wait_port_free, engine["port"], set(engine["pids"]))
            if busy:
                names = ", ".join(f"{processes.process_name(p) if p else 'неизвестный процесс'}" + (f" (PID {p})" if p else "") for p in busy)
                port.update(status="fail", detail=f"порт занят: {names} — чужой процесс не трогаю")
                ok = False
            else:
                port.update(status="ok", detail="порт свободен")
        used_gpu = bool(gpu_before) and (set(engine["pids"]) & set(gpu_before["pids"])
                                         or local.options(package).get("device") == "cuda")
        if used_gpu:
            gpu = job.step("gpu", "Видеопамять освобождается")
            result = await asyncio.to_thread(processes.wait_gpu_released, engine["pids"])
            if result is None:
                gpu.update(status="warn", detail="состояние видеокарты не удалось прочитать")
            elif result[0]:
                gpu.update(status="fail", detail="процессы всё ещё держат видеопамять: PID " + ", ".join(map(str, result[0])))
                ok = False
            else:
                freed = sum(gpu_before["used"].values()) - sum(result[1]["used"].values())
                gpu.update(status="ok", detail=f"освобождено около {freed} МБ" if freed > 0 else "видеопамять свободна")
        return ok

    async def _start(self, job, package):
        local = self.stack.local
        step = job.step("start", f"Запускается: {package['title']}")
        try:
            await local.start(package)
            outcome = await local.wait_ready(package, self.ready_timeout)
        except (OSError, ValueError) as exc:
            step.update(status="fail", detail=str(exc))
            raise ValueError(f"Не запустился {package['title']}: {exc}") from exc
        if outcome == "healthy":
            step.update(status="ok", detail=f"отвечает на порту {local.options(package).get('port')}")
        elif outcome == "already":
            step.update(status="ok", detail="уже запущен")
        else:
            step.update(status="warn", detail="процесс работает, но ответа ещё нет (грузится модель) — состояние в карточке пакета")

    # --- остановка панели --------------------------------------------------------
    def stop_all(self, timeout=3.0):
        """Завершить все серверы пакетов, включая осиротевшие; возвращает остановленные [{id, pids}]."""
        local = self.stack.local
        found = self.discover()
        procs = [p for e in found for p in e["procs"]]
        for popen in list(local.processes.values()):
            try:
                procs.append(psutil.Process(popen.pid))
            except psutil.Error:
                pass
        alive = processes.terminate(list({p.pid: p for p in procs}.values()), timeout)
        for e in found:
            (local.folder(e["package"]) / "server.pid").unlink(missing_ok=True)
        for popen in local.processes.values():
            _reap(popen)
        local.processes.clear()
        if alive:
            log.warning("Не завершились процессы речевых серверов: %s", [p.pid for p in alive])
        return [{"id": e["package"]["id"], "pids": e["pids"]} for e in found]

    async def shutdown(self):
        await asyncio.to_thread(self.stop_all)


def _reap(popen):
    try:
        popen.wait(2)
    except Exception:  # noqa: BLE001 — процесс уже убран или недоступен
        pass
