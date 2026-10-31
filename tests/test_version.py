"""TASK-039: версия из git-описания рабочей копии и manifest release-please.

Тесты используют локальные git-репозитории (git CLI есть на машинах разработки;
сам version.py работает без CLI — через dulwich). Битый/отсутствующий git —
fallback на manifest.
"""
import json
import subprocess
import sys
from pathlib import Path

from server_emulator.version import current


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(tmp, commits_after=0, tag="0.2.0"):
    git("init", "-q", cwd=tmp)
    git("config", "user.email", "test@example.com", cwd=tmp)
    git("config", "user.name", "Test", cwd=tmp)
    (tmp / "a.txt").write_text("one", encoding="utf-8")
    git("add", "a.txt", cwd=tmp)
    git("commit", "-q", "-m", "one", cwd=tmp)
    if tag:
        git("tag", "-a", f"v{tag}", "-m", tag, cwd=tmp)
    for i in range(commits_after):
        (tmp / "a.txt").write_text(f"change {i}", encoding="utf-8")
        git("commit", "-q", "-am", f"change {i}", cwd=tmp)
    return subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], cwd=tmp,
                          check=True, capture_output=True, text=True).stdout.strip()


def test_tag_version_and_clean(tmp_path):
    sha = make_repo(tmp_path)
    result = current(tmp_path)
    assert result["version"] == "0.2.0"
    assert result["commit"] == sha and result["dirty"] is False


def test_commits_after_tag_use_describe_scheme(tmp_path):
    sha = make_repo(tmp_path, commits_after=2)
    result = current(tmp_path)
    assert result["version"] == f"0.2.0+2.g{sha}"
    assert result["dirty"] is False


def test_dirty_tracked_file(tmp_path):
    make_repo(tmp_path)
    (tmp_path / "a.txt").write_text("правка", encoding="utf-8")
    result = current(tmp_path)
    assert result["dirty"] is True and result["version"] == "0.2.0"


def test_ignored_files_are_not_dirty(tmp_path):
    """Данные сервера (state/) игнорируются — работа эмулятора не делает копию dirty."""
    make_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("server_emulator/state*/\n*.sqlite3\n", encoding="utf-8")
    (tmp_path / "server_emulator").mkdir()
    git("add", ".gitignore", cwd=tmp_path)
    git("commit", "-q", "-m", "ignore", cwd=tmp_path)
    (tmp_path / "server_emulator" / "state").mkdir()
    (tmp_path / "server_emulator" / "state" / "store.db").write_bytes("данные".encode("utf-8"))
    assert current(tmp_path)["dirty"] is False


def test_without_git_falls_back_to_manifest(monkeypatch):
    """Установка из zip без .git: версия из .release-please-manifest.json (TASK-039).
    Каталог заведомо вне git-репозитория — Repo.discover не найдёт ни одного родителя."""
    import tempfile
    manifest = Path(__file__).resolve().parent.parent / ".release-please-manifest.json"
    monkeypatch.setattr("server_emulator.version.MANIFEST", manifest)
    empty = Path(tempfile.mkdtemp(prefix="zip-install-"))
    try:
        (empty / ".release-please-manifest.json").write_text(
            json.dumps({".": json.loads(manifest.read_text(encoding="utf-8"))["."]}), encoding="utf-8")
        result = current(empty)
        assert result["version"] == "0.1.0" and result["commit"] == "" and result["dirty"] is False
    finally:
        import shutil
        shutil.rmtree(empty, ignore_errors=True)


def test_server_run_leaves_working_copy_clean(tmp_path):
    """После работы сервера git status --porcelain пуст: данные — в игнорируемых путях."""
    repo = tmp_path / "clone"
    repo.mkdir()
    make_repo(repo)
    ignore = Path(__file__).resolve().parent.parent / ".gitignore"
    (repo / ".gitignore").write_text(ignore.read_text(encoding="utf-8"), encoding="utf-8")
    git("add", ".gitignore", cwd=repo)
    git("commit", "-q", "-m", "gitignore", cwd=repo)

    from server_emulator.runtime import Runtime
    from server_emulator.server import create_app
    import asyncio
    from aiohttp import ClientSession
    from tests.conftest import listen

    async def run():
        runtime = Runtime(repo / "server_emulator" / "state")  # данные — в клона state (в .gitignore)
        runner, url = await listen(create_app(runtime=runtime))
        try:
            async with ClientSession() as client:
                await client.get(url + "/emulator/")
        finally:
            await runner.cleanup()
            await runtime.close()
    asyncio.run(run())
    state = repo / "server_emulator" / "state"
    assert state.exists()  # сервер реально писал данные
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()
    assert status == ""
