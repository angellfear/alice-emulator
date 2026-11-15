"""Каждый файл данных пакета в git попадает в сборку (package-data в pyproject.toml),
рабочее состояние server_emulator/state — нет."""
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

from setuptools import find_namespace_packages

ROOT = Path(__file__).resolve().parents[1]


def _config():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]


def test_tracked_data_files_are_packaged():
    patterns = _config()["package-data"]["server_emulator"]
    tracked = subprocess.run(["git", "ls-files", "server_emulator"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
    missing = [name for name in tracked if not name.endswith(".py")
               and not any(PurePosixPath(name).relative_to("server_emulator").full_match(p)
                           for p in patterns)]
    assert missing == []


def test_state_is_not_a_package():
    find = _config()["packages"]["find"]
    packages = find_namespace_packages(str(ROOT), include=find["include"], exclude=find["exclude"])
    assert not [name for name in packages if name.startswith("server_emulator.state")]
