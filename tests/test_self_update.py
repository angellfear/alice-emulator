"""TASK-040: самообновление — проверка Releases API, возможность обновления, stash, updater.

Без сети: Releases API — локальный aiohttp-сервер; origin — локальный bare-репозиторий.
Реальный перезапуск сервера и pip в тестах updater'а подменяются.
"""
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from aiohttp import web

from server_emulator.self_update import Service
from server_emulator.self_update import check, repo, updater
from server_emulator.settings_service import DEFAULT


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def make_origin(tmp, tags=("0.1.0", "0.2.0"), with_pyproject=True):
    """Bare-репозиторий origin с тегами v* и рабочая копия на первом теге; возвращает (origin, work).
    Путь origin содержит owner/name — проверка origin в can_update проходит как для GitHub."""
    origin, work = tmp / "angellfear" / "alice-emulator.git", tmp / "work"
    origin.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    uri = origin.as_uri()  # file:// — иначе dulwich на Windows трактует «C:» как хост
    subprocess.run(["git", "clone", "-q", uri, str(work)], check=True)
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "Test", cwd=work)
    for number in tags:
        (work / "a.txt").write_text("release " + number, encoding="utf-8")
        if with_pyproject:
            (work / "pyproject.toml").write_text("[project]\nname = 't'\n", encoding="utf-8")
        git("add", ".", cwd=work)
        git("commit", "-q", "-m", "release " + number, cwd=work)
        git("tag", "-a", "v" + number, "-m", number, cwd=work)
    git("push", "-q", "origin", "--tags", "main", cwd=work)
    git("remote", "set-url", "origin", uri, cwd=work)  # в конфиге клона — тот же file:// URI
    git("reset", "-q", "--hard", "v" + tags[0], cwd=work)  # копия отстаёт от origin
    return origin, work


class FakeRuntime:
    def __init__(self, settings, directory):
        self.settings = settings
        self.directory = Path(directory)


@pytest.fixture
def service(tmp_path):
    """Service на отстающей рабочей копии; settings — дефолт self_update."""
    _, work = make_origin(tmp_path)
    settings = {"self_update": json.loads(json.dumps(DEFAULT["self_update"]))}
    return Service(FakeRuntime(settings, tmp_path / "state"), root=work), work


def releases_app(releases, etag="W/test"):
    """Локальный аналог Releases API: /releases/latest и /releases, 304 по If-None-Match."""
    async def latest(request):
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304)
        stable = next((r for r in releases if not r["prerelease"]), None)
        return web.json_response(_raw(stable), headers={"ETag": etag}) if stable else web.HTTPNotFound()

    async def all_releases(request):
        return web.json_response([_raw(r) for r in releases if not r.get("draft")])

    def _raw(r):
        return {"tag_name": "v" + r["version"], "body": r.get("notes", ""), "prerelease": r["prerelease"],
                "published_at": r.get("published_at", ""), "html_url": "http://example/" + r["version"]}

    app = web.Application()
    app.router.add_get("/repos/angellfear/alice-emulator/releases/latest", latest)
    app.router.add_get("/repos/angellfear/alice-emulator/releases", all_releases)
    return app


async def test_latest_stable_ignores_prerelease_and_beta_takes_it(monkeypatch, service):
    _, work = service
    # /releases отдаёт релизы от нового к старому; beta берёт первый (пререлиз), stable — /releases/latest
    releases = [{"version": "0.3.0-rc1", "prerelease": True}, {"version": "0.2.0", "prerelease": False}]
    from tests.conftest import listen
    runner, url = await listen(releases_app(releases))
    try:
        monkeypatch.setattr(check, "API", url)
        stable = await check.latest("angellfear/alice-emulator")
        assert stable["release"]["version"] == "0.2.0" and stable["release"]["prerelease"] is False
        beta = await check.latest("angellfear/alice-emulator", channel="beta")
        assert beta["release"]["version"] == "0.3.0-rc1" and beta["release"]["prerelease"] is True
    finally:
        await runner.cleanup()


async def test_check_new_version_and_304(monkeypatch, tmp_path, service):
    svc, work = service
    from tests.conftest import listen
    runner, url = await listen(releases_app([{"version": "0.2.0", "prerelease": False}]))
    try:
        monkeypatch.setattr(check, "API", url)
        first = await svc.check()
        assert first["release"]["version"] == "0.2.0"
        status = svc.status()
        assert status["available"] is True and status["latest"]["version"] == "0.2.0"
        assert status["can_update"] is True and status["installed_kind"] == "git"
        second = await svc.check()  # If-None-Match → 304: релиз сохраняется, а не стирается
        assert second["release"]["version"] == "0.2.0"
    finally:
        await runner.cleanup()


async def test_check_private_repo_is_notice(monkeypatch, tmp_path, service):
    """Приватный репозиторий без токена: 404 — статус «недоступен», не исключение сети."""
    svc, _ = service
    from tests.conftest import listen
    runner, url = await listen(web.Application())  # без маршрутов — всегда 404
    try:
        monkeypatch.setattr(check, "API", url)
        result = await svc.check()
        assert "недоступен" in result["error"]
        assert svc.status()["available"] is False
    finally:
        await runner.cleanup()


async def test_skip_hides_until_next_version(monkeypatch, tmp_path, service):
    svc, _ = service
    from tests.conftest import listen
    runner, url = await listen(releases_app([{"version": "0.2.0", "prerelease": False}]))
    try:
        monkeypatch.setattr(check, "API", url)
        await svc.check()
        svc.runtime.settings["self_update"]["skipped"] = "0.2.0"
        assert svc.status()["available"] is False and svc.status()["skipped"] is True
    finally:
        await runner.cleanup()


def test_can_update_blocks_modified_files(service):
    svc, work = service
    (work / "a.txt").write_text("локальная правка", encoding="utf-8")
    ok, reasons, files = repo.can_update(work, "angellfear/alice-emulator")
    assert ok is False and files == ["a.txt"] and any("правки" in r for r in reasons)
    sha = repo.stash(repo.discover(work))  # stash по клику — правки отложены, копия чистая
    assert sha and repo.can_update(work, "angellfear/alice-emulator")[0] is True
    assert (work / "a.txt").read_text(encoding="utf-8") == "release 0.1.0"


def test_zip_install_is_notice_only(tmp_path):
    plain = tmp_path / "zip-install"
    plain.mkdir()
    (plain / "server_emulator").mkdir()
    ok, reasons, files = repo.can_update(plain, "angellfear/alice-emulator")
    assert ok is False and any("zip" in r for r in reasons) and files == []


def test_stash_keeps_changes_recoverable(service):
    svc, work = service
    (work / "a.txt").write_text("правка", encoding="utf-8")
    sha = repo.stash(repo.discover(work))
    stored = repo.discover(work)[sha.encode()]
    assert stored.message.decode() == repo.STASH_MESSAGE


def test_updater_switches_tag_and_writes_ok(monkeypatch, tmp_path):
    _, work = make_origin(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    plan = {"action": "update", "target_tag": "v0.2.0", "target_version": "0.2.0",
            "previous_tag": "v0.1.0", "current_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(work), check=True, capture_output=True, text=True).stdout.strip(),
            "pid": None, "python": sys.executable, "args": [], "root": str(work),
            "state": str(state), "launch": "manager", "status": "running", "started_at": 0}
    (state / "update").mkdir()
    (state / "update" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (state / "emulator.sqlite3").write_bytes(b"db")
    (state / "server.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(updater, "start_server", lambda plan, log: None)
    monkeypatch.setattr(updater, "wait_healthy", lambda *a, **k: None)
    result = updater.run(state)
    assert result["status"] == "ok"
    peeled = subprocess.run(["git", "rev-parse", "v0.2.0^{}"], cwd=str(work), capture_output=True, text=True).stdout.strip()
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(work), capture_output=True,
                          text=True).stdout.strip() == peeled
    assert (work / "a.txt").read_text(encoding="utf-8") == "release 0.2.0"
    backups = list((state / "backups").glob("update-*.zip"))
    assert len(backups) == 1
    with zipfile.ZipFile(backups[0]) as archive:
        assert "emulator.sqlite3" in archive.namelist() and "server.json" in archive.namelist()
    assert json.loads((state / "update" / "plan.json").read_text(encoding="utf-8"))["log"]


def test_updater_failure_rolls_back(monkeypatch, tmp_path):
    _, work = make_origin(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    old_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(work), check=True,
                                capture_output=True, text=True).stdout.strip()
    plan = {"action": "update", "target_tag": "v0.2.0", "target_version": "0.2.0",
            "previous_tag": "v0.1.0", "current_commit": old_commit, "pid": None,
            "python": sys.executable, "args": [], "root": str(work), "state": str(state),
            "launch": "self", "status": "running", "started_at": 0}
    (state / "update").mkdir()
    (state / "update" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(updater, "start_server", lambda plan, log: None)  # реальный перезапуск подменён
    monkeypatch.setattr(updater, "wait_healthy", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("не поднялся")))
    result = updater.run(state)
    assert result["status"] == "failed" and "не поднялся" in result["log"]
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(work), capture_output=True,
                          text=True).stdout.strip() == old_commit
    assert (work / "a.txt").read_text(encoding="utf-8") == "release 0.1.0"


def test_backup_rotation(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    for index in range(6):
        (state / "backups").mkdir(exist_ok=True)
        (state / "backups" / f"update-2026010{index}-000000-v0.1.0.zip").write_bytes(b"zip")
    monkeypatch.setattr(updater.time, "strftime", lambda fmt: "20260107-000000")
    updater.backup(state, "v0.2.0")
    names = sorted(p.name for p in (state / "backups").glob("update-*.zip"))
    assert len(names) == updater.BACKUPS_KEEP and "update-20260107-000000-v0.2.0.zip" in names


def test_service_plan_and_launch_mode(service, monkeypatch):
    svc, work = service
    monkeypatch.delenv("ALICA_LAUNCHER", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert svc.launch_mode() == "self"
    monkeypatch.setenv("ALICA_LAUNCHER", "1")
    assert svc.launch_mode() == "tray"
    monkeypatch.delenv("ALICA_LAUNCHER", raising=False)
    svc.runtime.settings["self_update"]["restart"] = "manager"
    assert svc.launch_mode() == "manager"
    plan = svc.plan("0.2.0", "update")
    assert plan["target_tag"] == "v0.2.0" and plan["launch"] == "manager" and plan["pid"]
    assert plan["root"] == str(work) and plan["args"] == list(sys.orig_argv[1:])


async def test_manual_check_and_button_do_not_need_enabled(monkeypatch, service):
    """Галочка автообновления отключает только фоновую проверку: кнопка-индикатор и
    ручная проверка работают и при enabled=False (обновление — всегда ручной режим)."""
    svc, _ = service
    from tests.conftest import listen
    runner, url = await listen(releases_app([{"version": "0.2.0", "prerelease": False}]))
    try:
        monkeypatch.setattr(check, "API", url)
        svc.runtime.settings["self_update"]["enabled"] = False
        assert (await svc.check())["release"] is None  # фоновая проверка молчит
        manual = await svc.check(manual=True)  # «Проверить сейчас» работает
        assert manual["release"]["version"] == "0.2.0"
        assert svc.status()["available"] is True  # кнопка от галочки не зависит
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("stored, expected", [("angellfear/alica_emule", "angellfear/alice-emulator"),
                                              ("someone/fork", "someone/fork")])
async def test_renamed_repo_migrates_in_settings(tmp_path, stored, expected):
    """Репозиторий переименован: прежний адрес самообновления в сохранённых настройках переводится
    на новый, свой адрес (форк) остаётся."""
    from server_emulator.runtime import Runtime
    from server_emulator.state_store import StateStore
    runtime = Runtime(tmp_path)
    await runtime.close()
    store = StateStore(tmp_path / "emulator.sqlite3")
    settings = dict(store.load_settings())
    settings["self_update"] = {**settings["self_update"], "repo": stored}
    store.save_settings(settings)
    store.close()
    migrated = Runtime(tmp_path)
    try:
        assert migrated.settings["self_update"]["repo"] == expected
    finally:
        await migrated.close()
    assert StateStore(tmp_path / "emulator.sqlite3").load_settings()["self_update"]["repo"] == expected
