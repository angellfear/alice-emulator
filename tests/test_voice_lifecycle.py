"""Жизненный цикл локальных речевых серверов: смена источника останавливает старый движок со всем
деревом и осиротевшими процессами, порт освобождается, чужое не трогается; очистка установленного."""
import asyncio
import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import venv

import psutil
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server_emulator import voice_stack
from server_emulator.voice_stack import PLATFORM, VoiceStack, processes

SERVER = '''
import os, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])  # рабочий процесс-внук
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="module")
def venv_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("venv-template") / "venv"
    venv.create(path, with_pip=False)
    return path


class FakeRuntime:
    def __init__(self, directory):
        self.directory = directory
        self.settings = copy.deepcopy(voice_stack.DEFAULT_KEYS)
        self.saved = 0

    async def save_settings(self, value):
        self.settings = value
        self.saved += 1


def package_json(identifier, service, port, script, **extra):
    return {"id": identifier, "title": "Движок " + identifier, "service": service, "adapter": "openai",
            "config": {"url": "http://127.0.0.1:{port}/v1/x"}, "defaults": {"port": port},
            "server": {"script": str(script), "health": "/health"},
            "platforms": {PLATFORM: {"cpu": []}}, **extra}


@pytest.fixture
def world(tmp_path, venv_template):
    script = tmp_path / "fake_server.py"
    script.write_text(SERVER, encoding="utf-8")
    packages = tmp_path / "packages"
    packages.mkdir()
    ports = {"a": free_port(), "b": free_port()}
    specs = {"a": ("asr", {}), "b": ("asr", {})}
    for identifier, (service, extra) in specs.items():
        (packages / (identifier + ".json")).write_text(
            json.dumps(package_json(identifier, service, ports[identifier], script, **extra)), encoding="utf-8")
    runtime = FakeRuntime(tmp_path / "state")
    stack = VoiceStack(runtime, packages_dir=packages, speech_dir=tmp_path / "speech")
    for identifier in specs:
        folder = stack.local.folder(stack.package(identifier))
        folder.mkdir(parents=True)
        shutil.copytree(venv_template, folder / "venv")
        (folder / "installed.json").write_text("{}", encoding="utf-8")
    stack.lifecycle.ready_timeout = 30
    yield stack, ports, script
    stack.lifecycle.stop_all(timeout=2)


def tree_of(pid):
    """Процесс и потомки как объекты psutil: is_running() сверяет время создания — переиспользованный PID не обманет."""
    root = psutil.Process(pid)
    return [root, *root.children(recursive=True)]


async def wait_listening(port, seconds=20):
    for _ in range(int(seconds / 0.2)):
        if listening(port):
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"порт {port} не открылся")


def all_gone(procs):
    return not [p for p in procs if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]


async def test_switch_stops_old_engine_tree_and_orphans(world):
    stack, ports, script = world
    local = stack.local
    a, b = stack.package("a"), stack.package("b")
    await stack.apply("asr", "k:a")
    await wait_listening(ports["a"])
    own = tree_of(local.processes["a"].pid)
    assert len(own) >= 2  # пускатель venv, сервер, рабочий процесс — дерево, а не один процесс
    # сирота прошлого запуска: нет ни в реестре, ни в server.pid; свой порт
    folder = local.folder(a)
    orphan = subprocess.Popen([str(local.python(a)), str(script)], cwd=str(folder),
                              env=dict(os.environ, PORT=str(free_port())))
    await asyncio.sleep(1.5)
    orphan_tree = tree_of(orphan.pid)
    # посторонний процесс не должен пострадать
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        plan = await stack.lifecycle.plan("asr", "k:b")
        assert [e["id"] for e in plan["stop"]] == ["a"] and plan["start"]["id"] == "b"
        assert {p.pid for p in own + orphan_tree} <= {p for e in plan["stop"] for p in e["pids"]}
        result = await stack.apply("asr", "k:b")
        assert result == {"service": "asr", "active": "k:b"}
        assert all_gone(own + orphan_tree)
        assert not listening(ports["a"])
        await wait_listening(ports["b"])
        assert bystander.poll() is None
        assert stack.runtime.settings["voice_stack"]["asr"] == "k:b"
        job = next(reversed(stack.lifecycle.jobs.values()))
        kinds = [(s["kind"], s["status"]) for s in job.steps]
        assert kinds[0] == ("stop", "ok") and ("port", "ok") in kinds and kinds[-1] == ("start", "ok")
        assert job.service == "asr"
    finally:
        bystander.kill()
        bystander.wait(5)
        orphan.kill()
        orphan.wait(5)


async def test_apply_cloud_preset_stops_package_engine(world):
    stack, ports, _ = world
    await stack.apply("asr", "k:a")
    await wait_listening(ports["a"])
    pids = tree_of(stack.local.processes["a"].pid)
    preset = stack.save_preset("asr", "openai", "Облако", {"url": "http://127.0.0.1:9/v1/audio/transcriptions", "model": "m"})
    job = stack.lifecycle.begin("asr", "p:" + preset["id"])
    await stack.lifecycle.run("asr", "p:" + preset["id"], job)
    assert all_gone(pids) and not listening(ports["a"])
    assert job.state == "ok" and job.summary == "Применено"


async def test_foreign_listener_keeps_port_and_blocks_switch(world, monkeypatch):
    stack, ports, _ = world
    await stack.apply("asr", "k:a")
    await wait_listening(ports["a"])
    pids = tree_of(stack.local.processes["a"].pid)
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    real = processes.wait_port_free
    monkeypatch.setattr(processes, "wait_port_free", lambda port, own, timeout=0: [foreign.pid])
    try:
        before = stack.runtime.saved
        with pytest.raises(ValueError, match="источник не изменён"):
            await stack.apply("asr", "k:b")
        job = next(reversed(stack.lifecycle.jobs.values()))
        port_step = next(s for s in job.steps if s["kind"] == "port")
        assert port_step["status"] == "fail" and str(foreign.pid) in port_step["detail"] and job.state == "error"
        assert all_gone(pids) and foreign.poll() is None  # старый остановлен, чужой жив
        assert stack.runtime.saved == before
    finally:
        monkeypatch.setattr(processes, "wait_port_free", real)
        foreign.kill()
        foreign.wait(5)


async def test_failed_start_is_reported_with_log(world):
    stack, ports, script = world
    broken = stack.package("b")
    folder = stack.local.folder(broken)
    # сервер падает сразу: порт занят нашим же слушателем
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", ports["b"]))
    blocker.listen(1)
    try:
        job = stack.lifecycle.begin("asr", "k:b")
        with pytest.raises(ValueError, match="Не запустился"):
            await stack.lifecycle.run("asr", "k:b", job)
        step = job.steps[-1]
        assert step["kind"] == "start" and step["status"] == "fail" and job.state == "error"
        assert (folder / "server.log").exists()
    finally:
        blocker.close()


async def test_pid_file_with_reused_pid_is_not_killed(world):
    stack, _, _ = world
    local, a = stack.local, stack.package("a")
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        (local.folder(a) / "server.pid").write_text(str(bystander.pid), encoding="ascii")
        assert await stack.lifecycle.engines() == []  # чужой PID — не наш движок
        assert not (local.folder(a) / "server.pid").exists()
        local.stop(a)
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(5)


async def test_shutdown_kills_everything_including_orphans(world):
    stack, ports, script = world
    local = stack.local
    await stack.apply("asr", "k:a")
    await wait_listening(ports["a"])
    own = tree_of(local.processes["a"].pid)
    b = stack.package("b")
    orphan = subprocess.Popen([str(local.python(b)), str(script)], cwd=str(local.folder(b)),
                              env=dict(os.environ, PORT=str(ports["b"])))
    await wait_listening(ports["b"])
    orphan_tree = tree_of(orphan.pid)
    await stack.shutdown()
    assert all_gone(own + orphan_tree)
    assert not listening(ports["a"]) and not listening(ports["b"])
    assert local.processes == {}


async def test_install_pip_is_not_an_engine(world):
    stack, _, _ = world
    a = stack.package("a")
    folder = stack.local.folder(a)
    pip = subprocess.Popen([str(stack.local.python(a)), "-c", "import time; time.sleep(300)", "-m", "pip"], cwd=str(folder))
    try:
        # командная строка «python … -m pip» из venv — установка, а не сервер
        pip2 = subprocess.Popen([str(stack.local.python(a)), "-m", "pip", "--version"], cwd=str(folder),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pip2.wait(30)
        shot = [p for p in processes.snapshot() if p.pid == pip2.pid]
        assert not shot or not processes.belongs(shot[0], folder, stack.local.script(a))
    finally:
        pip.kill()
        pip.wait(5)


# --- API ------------------------------------------------------------------------------------
async def test_api_plan_apply_job(world):
    from server_emulator.ui import api as ui_api
    stack, ports, _ = world
    stack.runtime.voice_stack = stack
    app = web.Application()

    async def handler(request):
        return await ui_api.dispatch(request, stack.runtime, app)
    app.router.add_route("*", "/api/{action}", handler)
    headers = {"X-Emulator": "1"}
    async with TestClient(TestServer(app)) as client:
        plan = await (await client.post("/api/voice-stack-apply-plan", json={"service": "asr", "ref": "k:a"}, headers=headers)).json()
        assert plan["stop"] == [] and plan["start"]["id"] == "a"
        started = await (await client.post("/api/voice-stack-apply", json={"service": "asr", "ref": "k:a", "background": True}, headers=headers)).json()
        for _ in range(200):
            job = await (await client.get("/api/voice-stack-apply?job=" + started["job"])).json()
            if job["state"] != "running":
                break
            await asyncio.sleep(0.2)
        assert job["state"] == "ok" and job["steps"][-1]["kind"] == "start" and job["steps"][-1]["status"] == "ok"
        again = await (await client.post("/api/voice-stack-apply-plan", json={"service": "asr", "ref": "k:b"}, headers=headers)).json()
        assert [e["id"] for e in again["stop"]] == ["a"] and again["stop"][0]["pid"]
        missing = await client.get("/api/voice-stack-apply?job=nope")
        assert missing.status == 400
        bad = await client.post("/api/voice-stack-apply-plan", json={"service": "x", "ref": "k:a"}, headers=headers)
        assert bad.status == 400
    await stack.shutdown()


async def test_api_restart_restarts_running_package(world):
    from server_emulator.ui import api as ui_api
    stack, ports, _ = world
    stack.runtime.voice_stack = stack
    app = web.Application()

    async def handler(request):
        return await ui_api.dispatch(request, stack.runtime, app)
    app.router.add_route("*", "/api/{action}", handler)
    headers = {"X-Emulator": "1"}
    async with TestClient(TestServer(app)) as client:
        await stack.apply("asr", "k:a")
        await wait_listening(ports["a"])
        old_pid = stack.local.processes["a"].pid
        restarted = await (await client.post("/api/voice-stack-restart", json={"id": "a"}, headers=headers)).json()
        assert restarted == {"restarted": "a"}
        assert stack.local.processes["a"].pid != old_pid  # запущен новый процесс, а не переиспользован старый
        await wait_listening(ports["a"])
    await stack.shutdown()


async def test_api_restart_reports_dead_process(world, tmp_path, venv_template):
    """Перезапуск пакета, чей сервер умирает сразу: ошибка с причиной, а не молчаливый «restarted»."""
    from server_emulator.ui import api as ui_api
    stack, _, _ = world
    dying = tmp_path / "dying_server.py"
    dying.write_text("", encoding="utf-8")  # пустой скрипт: процесс завершается, ничего не печатая
    (tmp_path / "packages" / "c.json").write_text(json.dumps(package_json("c", "asr", free_port(), dying)), encoding="utf-8")
    folder = stack.local.folder(stack.package("c"))
    folder.mkdir(parents=True)
    shutil.copytree(venv_template, folder / "venv")
    (folder / "installed.json").write_text("{}", encoding="utf-8")
    stack.runtime.voice_stack = stack
    app = web.Application()

    async def handler(request):
        return await ui_api.dispatch(request, stack.runtime, app)
    app.router.add_route("*", "/api/{action}", handler)
    headers = {"X-Emulator": "1"}
    async with TestClient(TestServer(app)) as client:
        failed = await client.post("/api/voice-stack-restart", json={"id": "c"}, headers=headers)
        assert failed.status == 400
        error = (await failed.json())["error"]
        assert "Не запустился" in error and "завершился" in error
    await stack.shutdown()


# --- установленное локально ----------------------------------------------------------------
@pytest.fixture
def installed(tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    script = tmp_path / "s.py"
    script.write_text("", encoding="utf-8")
    shared_pkgs = {"one": ["lib"], "two": ["lib", "extra"], "three": []}
    for index, (identifier, depends) in enumerate(shared_pkgs.items()):
        (packages / (identifier + ".json")).write_text(json.dumps(
            package_json(identifier, "asr" if identifier != "three" else "tts", 5000 + index, script, depends=depends)), encoding="utf-8")
    runtime = FakeRuntime(tmp_path / "state")
    stack = VoiceStack(runtime, packages_dir=packages, speech_dir=tmp_path / "speech")
    root = stack.local.directory
    for name, size in {"one": 1000, "two": 2000, "three": 500, "ghost": 300, "_shared/lib": 700, "_shared/extra": 100,
                       "_shared/lonely": 50}.items():
        (root / name).mkdir(parents=True)
        (root / name / "blob.bin").write_bytes(b"x" * size)
        if "/" not in name and name != "ghost":
            (root / name / "installed.json").write_text("{}", encoding="utf-8")
    return stack


async def settled(stack):
    data = await stack.installed.items()
    for _ in range(50):
        if not data["computing"]:
            return data
        await asyncio.sleep(0.1)
        data = await stack.installed.items()
    raise AssertionError("размеры не посчитаны")


async def test_installed_lists_sizes_status_and_shared(installed):
    stack = installed
    stack.runtime.settings["voice_stack"] = {"asr": "k:one"}
    data = await settled(stack)
    rows = {r["id"]: r for r in data["items"]}
    assert rows["one"]["status"] == "active" and not rows["one"]["removable"]
    assert rows["two"]["status"] == "unused" and rows["two"]["size"] == 2002  # blob + installed.json
    assert rows["ghost"]["kind"] == "orphan" and rows["ghost"]["removable"]
    assert rows["_shared/lib"]["status"] == "needed" and not rows["_shared/lib"]["removable"]
    assert rows["_shared/lonely"]["removable"]
    assert rows["two"]["frees"] == ["extra"]  # lib нужна применённому «one», extra — только «two»
    assert data["total"] == sum(r["size"] for r in data["items"])
    reclaim = set(data["reclaim_ids"])
    assert reclaim == {"two", "three", "ghost", "_shared/extra", "_shared/lonely"}
    assert data["reclaimable"] == sum(rows[i]["size"] for i in reclaim)


async def test_clean_removes_unused_with_orphan_dependencies_only(installed):
    stack = installed
    stack.runtime.settings["voice_stack"] = {"asr": "k:one"}
    await settled(stack)
    root = stack.local.directory
    result = await stack.installed.clean(["two"])
    assert sorted(result["removed"]) == ["_shared/extra", "two"] and not result["errors"]
    assert not (root / "two").exists() and not (root / "_shared" / "extra").exists()
    assert (root / "_shared" / "lib").exists() and (root / "one").exists()  # общая зависимость нужного пакета цела
    with pytest.raises(ValueError, match="используется"):
        await stack.installed.clean(["one"])
    with pytest.raises(ValueError, match="используется"):
        await stack.installed.clean(["_shared/lib"])
    assert (root / "one").exists()


async def test_clean_all_unused_and_shared_goes_when_last_user_goes(installed):
    stack = installed
    await settled(stack)
    root = stack.local.directory
    stack.runtime.settings["voice_stack"] = {}
    data = await stack.installed.items()
    assert "_shared/lib" in data["reclaim_ids"]  # применённых нет: уходят оба пользователя lib и сама lib
    result = await stack.installed.clean(everything=True)
    assert result["freed"] > 0 and not result["errors"]
    assert [p.name for p in root.iterdir()] == ["_shared"] and list((root / "_shared").iterdir()) == []


async def test_clean_via_api_and_active_is_refused(installed):
    from server_emulator.ui import api as ui_api
    stack = installed
    stack.runtime.voice_stack = stack
    stack.runtime.settings["voice_stack"] = {"asr": "k:one"}
    app = web.Application()

    async def handler(request):
        return await ui_api.dispatch(request, stack.runtime, app)
    app.router.add_route("*", "/api/{action}", handler)
    headers = {"X-Emulator": "1"}
    async with TestClient(TestServer(app)) as client:
        for _ in range(50):
            data = await (await client.get("/api/voice-stack-installed")).json()
            if not data["computing"]:
                break
            await asyncio.sleep(0.1)
        assert data["total"] and {r["id"] for r in data["items"]} >= {"one", "ghost", "_shared/lib"}
        refused = await client.post("/api/voice-stack-clean", json={"ids": ["one"]}, headers=headers)
        assert refused.status == 400 and "используется" in (await refused.json())["error"]
        empty = await client.post("/api/voice-stack-clean", json={}, headers=headers)
        assert empty.status == 400
        done = await (await client.post("/api/voice-stack-clean", json={"all_unused": True}, headers=headers)).json()
        assert "ghost" in done["removed"] and "one" not in done["removed"]
        assert (stack.local.directory / "one").exists()
