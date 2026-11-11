"""Миграции схем SQLite-баз на Alembic: по ветке на файл базы.

Каждая база — свой каталог ревизий `migrations/<ветка>/versions/` (метка ветки — имя
базы) и свой Alembic Config с sqlalchemy.url на этот файл. Схема одна — в базовой
ревизии: хранилище вызывает upgrade_database своей ветки при открытии файла, поэтому
CREATE в модулях не нужен и модульный тест открывает хранилище без runtime.
Перед изменяющим обновлением — резервная копия файла рядом (sqlite backup API, дата
в имени); сбой миграции: копия восстанавливается, причина — в журнале сервера, и
исключение уходит наверх — сервер не стартует. render_as_batch=True — будущие
ALTER на SQLite идут batch-миграциями.

Порядок при старте (runtime): ветка server_log мигрирует до открытия её соединения
(причина сбоя миграций должна попадать в журнал), затем журнал сервера открывается,
затем upgrade_all — остальные базы до открытия их соединений.
"""
import datetime
import os
import sqlite3
from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

PACKAGE_DIR = Path(__file__).parent
# ветка → файл базы в state/; server_log первым: журнал сервера нужен для записи
# причины сбоя миграций, его соединение открывается сразу после его ветки
BRANCHES = ("server_log", "updates", "traffic", "agent_ops", "routines", "state")
FILES = {"server_log": "server_log.sqlite3", "updates": "updates.sqlite3",
         "traffic": "traffic.sqlite3", "agent_ops": "agent_ops.sqlite3",
         "routines": "routines.sqlite3", "state": "emulator.sqlite3"}


class MigrationFailed(ValueError):
    """Сбой миграции: копия базы восстановлена, причина — в журнале сервера."""


def _config(db_path, branch):
    cfg = Config()
    cfg.set_main_option("script_location", str(PACKAGE_DIR))
    cfg.set_main_option("version_locations", str(PACKAGE_DIR / branch / "versions"))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + str(db_path).replace("\\", "/"))
    cfg.set_main_option("path_separator", "os")
    cfg.attributes["render_as_batch"] = True
    return cfg


@lru_cache(maxsize=None)
def _head(branch):
    """Head ветки: каталог ревизий читается один раз на процесс."""
    cfg = Config()
    cfg.set_main_option("script_location", str(PACKAGE_DIR))
    cfg.set_main_option("version_locations", str(PACKAGE_DIR / branch / "versions"))
    cfg.set_main_option("path_separator", "os")
    return ScriptDirectory.from_config(cfg).get_current_head()


def _revision(path):
    """Текущая ревизия базы по alembic_version (mode=ro — файл не создаётся и не
    меняется); таблицы версий или пустой/битый файл — None."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


def _backup(path):
    """Копия базы рядом: <имя>.backup-<дата> (sqlite backup API — согласованно с WAL)."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(path.name + f".backup-{stamp}")
    source = sqlite3.connect(path)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    return target


def upgrade_database(path, branch, log=None):
    """Миграции одной базы до head её ветки.

    path — файл базы в state/ (нет файла — создаётся ревизией). База на head — быстрая
    проверка alembic_version без прогона миграций. Если файл изменится — сначала
    резервная копия. Сбой: копия восстановлена, причина — в журнал (если передан) и в
    MigrationFailed — сервер не стартует.
    """
    path = Path(path)
    backup = None
    try:
        if not (path.exists() and _revision(path) == _head(branch)):
            if path.exists():
                backup = _backup(path)
            command.upgrade(_config(path, branch), "head")
    except Exception as exc:
        if backup is not None and backup.exists():
            os.replace(backup, path)  # файл из копии — база как до обновления
            for suffix in ("-wal", "-shm"):  # журнал сбойной миграции не применяется к копии
                path.with_name(path.name + suffix).unlink(missing_ok=True)
        if log is not None:
            log("error", "server", f"Сбой миграции {path.name}: {exc}")
        raise MigrationFailed(f"{path.name}: {exc}") from exc


def upgrade_all(directory, skip=None, log=None):
    """Миграции всех баз веток BRANCHES в каталоге state/ (кроме skip) — до открытия
    соединений хранилищ. log — callable(level, source, message) журнала сервера."""
    directory = Path(directory)
    for branch in BRANCHES:
        if branch == skip:
            continue
        upgrade_database(directory / FILES[branch], branch, log=log)
