"""Миграции схем SQLite: хранилище доводит свою базу до head ревизией, копия перед
изменяющим обновлением, сбой миграции (копия восстановлена, причина в журнале),
база новее кода, миграции при старте runtime."""
import sqlite3

import pytest

from server_emulator.migrations import MigrationFailed, upgrade_all, upgrade_database

pytestmark = pytest.mark.no_state_template  # базы здесь собирает сам тест — ревизией


def schema_of(path):
    """Форма схемы: нормализованные строки sqlite_master (таблицы и индексы),
    отсортированные; alembic_version — метаданные миграций, вне сравнения."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
        return sorted(" ".join(row[1].split()) for row in rows if row[0] != "alembic_version")
    finally:
        db.close()


def at_base(path):
    """База «на базе»: alembic_version есть, ревизии в ней нет — файл требует обновления."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL)")
    db.commit()
    db.close()
    return path


def revision_count(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0]
    finally:
        db.close()


def tables_of(path):
    db = sqlite3.connect(path)
    try:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


def test_upgrade_all_creates_all_databases(tmp_path):
    """Чистый каталог state: upgrade_all создаёт все базы с alembic_version."""
    upgrade_all(tmp_path)
    for name in ("server_log.sqlite3", "updates.sqlite3", "traffic.sqlite3",
                 "agent_ops.sqlite3", "routines.sqlite3", "emulator.sqlite3"):
        assert (tmp_path / name).exists(), name
        assert revision_count(tmp_path / name) == 1, name


def test_storages_self_migrate(tmp_path):
    """Открытие хранилища без runtime: своё соединение с полной схемой ревизии."""
    from server_emulator.agent.ops import Ops
    from server_emulator.agent.routines import Routines
    from server_emulator.server_log import ServerLog
    from server_emulator.state_store import StateStore
    from server_emulator.updates import Updates
    from server_emulator.upstream.journal import TrafficJournal
    Updates(tmp_path)
    Ops(tmp_path)
    Routines(None, tmp_path / "routines.sqlite3")
    journal = TrafficJournal(tmp_path / "traffic.sqlite3")
    journal.db.close()
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.close()
    ServerLog(tmp_path / "server_log.sqlite3").db.close()
    for name, required in (
            ("updates.sqlite3", {"settings", "attempts", "urls", "sightings", "last_requests"}),
            ("emulator.sqlite3", {"meta", "settings", "users", "devices", "aliases", "unassigned",
                                  "executions", "param_defs", "param_values", "scenario_data"}),
            ("traffic.sqlite3", {"exchanges", "ws_frames", "journal_groups"}),
            ("server_log.sqlite3", {"entries"}),
            ("agent_ops.sqlite3", {"runs"}),
            ("routines.sqlite3", {"routines", "routine_runs", "inbox"})):
        assert required <= tables_of(tmp_path / name), name
        assert revision_count(tmp_path / name) == 1, name
    db = sqlite3.connect(tmp_path / "updates.sqlite3")
    assert "ip" in {row[1] for row in db.execute("PRAGMA table_info(attempts)")}  # колонка из ревизии
    db.close()
    db = sqlite3.connect(tmp_path / "emulator.sqlite3")
    assert {"room", "model", "fields", "route_mode", "record"} <= \
        {row[1] for row in db.execute("PRAGMA table_info(devices)")}
    db.close()
    db = sqlite3.connect(tmp_path / "traffic.sqlite3")
    assert "exchanges_group" in {row[1] for row in db.execute("PRAGMA index_list(exchanges)")}
    db.close()


def test_storage_schema_is_revision_schema(tmp_path):
    """Схема одна: база, открытая хранилищем, равна базе, собранной ревизией ветки."""
    from server_emulator.updates import Updates
    Updates(tmp_path)
    built = tmp_path / "built.sqlite3"
    upgrade_database(built, "updates")
    assert schema_of(tmp_path / "updates.sqlite3") == schema_of(built)


def test_backup_created_before_upgrade(tmp_path):
    """Перед изменяющим обновлением — копия базы рядом, с датой в имени."""
    old = at_base(tmp_path / "updates.sqlite3")
    upgrade_database(old, "updates")
    backups = list(tmp_path.glob("updates.sqlite3.backup-*"))
    assert backups and schema_of(backups[0]) == []  # база «на базе» была без таблиц
    assert schema_of(old)


def test_no_change_at_head_makes_no_backup(tmp_path):
    """База на head — быстрый путь без прогонов миграций и без копии."""
    path = tmp_path / "updates.sqlite3"
    upgrade_database(path, "updates")
    upgrade_database(path, "updates")
    assert not list(tmp_path.glob("updates.sqlite3.backup-*"))


def test_database_newer_than_code_fails(tmp_path):
    """База с ревизией, которой нет в коде: обновление не молчит — MigrationFailed."""
    path = tmp_path / "updates.sqlite3"
    upgrade_database(path, "updates")
    db = sqlite3.connect(path)
    db.execute("UPDATE alembic_version SET version_num='9999'")
    db.commit()
    db.close()
    with pytest.raises(MigrationFailed):
        upgrade_database(path, "updates")


def test_migration_failure_restores_and_logs(tmp_path, monkeypatch):
    """Сбой миграции: файл восстановлен из копии (как до обновления), причина — в
    журнале сервера, наверх уходит MigrationFailed — сервер не стартует."""
    old = at_base(tmp_path / "updates.sqlite3")
    db = sqlite3.connect(old)
    db.executescript("CREATE TABLE attempts (id TEXT PRIMARY KEY, ts REAL);"
                     "INSERT INTO attempts VALUES ('a1', 1.0);")
    db.commit()
    db.close()
    entries = []
    from alembic import command as alembic_command
    real_upgrade = alembic_command.upgrade

    def boom(config, revision, **kwargs):  # падает только ветка updates — как реальный сбой ревизии
        if "updates.sqlite3" in config.get_main_option("sqlalchemy.url"):
            raise RuntimeError("взорвалось на upgrade")
        return real_upgrade(config, revision, **kwargs)

    monkeypatch.setattr("alembic.command.upgrade", boom)
    with pytest.raises(MigrationFailed, match="взорвалось"):
        upgrade_database(old, "updates", log=lambda level, source, message: entries.append(message))
    assert entries == ["Сбой миграции updates.sqlite3: взорвалось на upgrade"]
    with pytest.raises(MigrationFailed):  # при старте runtime сервер не стартует,
        from server_emulator.runtime import Runtime  # причина — в журнале сервера
        Runtime(directory=tmp_path)
    log_db = sqlite3.connect(tmp_path / "server_log.sqlite3")
    try:
        rows = log_db.execute("SELECT message FROM entries WHERE level='error'").fetchall()
        assert any("updates.sqlite3" in message and "взорвалось" in message
                   for (message,) in rows)
    finally:
        log_db.close()
    db = sqlite3.connect(old)
    try:
        assert db.execute("SELECT id FROM attempts").fetchall() == [("a1",)]  # данные на месте
        assert db.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0] == 0  # и без ревизии
    finally:
        db.close()


@pytest.mark.asyncio
async def test_runtime_runs_migrations_on_start(tmp_path):
    """Миграции при старте runtime: до открытия соединений все шесть баз на head."""
    from server_emulator.runtime import Runtime
    runtime = Runtime(directory=tmp_path)
    try:
        for name in ("updates.sqlite3", "emulator.sqlite3", "server_log.sqlite3",
                     "traffic.sqlite3", "agent_ops.sqlite3", "routines.sqlite3"):
            assert (tmp_path / name).exists(), name
            assert revision_count(tmp_path / name) == 1, name
    finally:
        await runtime.close()
