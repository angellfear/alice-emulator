"""Миграции схем SQLite: stamp старых баз, схема новой установки, резервная копия,
сбой миграции (копия восстановлена, причина в журнале), запуск при старте runtime."""
import json
import sqlite3
import time

import pytest

from server_emulator.migrations import MigrationFailed, upgrade_all, upgrade_database

OLD_UPDATES = """
CREATE TABLE settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE attempts (id TEXT PRIMARY KEY, ts REAL, endpoint TEXT, device_sn TEXT,
    user_id TEXT, device_id TEXT, policy TEXT, status INTEGER, error TEXT);
CREATE INDEX attempts_ts ON attempts(ts);
CREATE TABLE urls (url TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, hits INTEGER, metadata TEXT);
CREATE TABLE sightings (attempt_id TEXT, url TEXT, PRIMARY KEY(attempt_id,url));
CREATE TABLE last_requests (endpoint TEXT, device_sn TEXT, method TEXT, target TEXT,
    headers TEXT, body BLOB, ts REAL, PRIMARY KEY(endpoint, device_sn));
"""


def old_updates_fixture(path, attempt_id="a1"):
    """База updates прежнего кода: без ip и без alembic_version, с данными."""
    db = sqlite3.connect(path)
    db.executescript(OLD_UPDATES)
    db.execute("INSERT INTO attempts (id, ts, error) VALUES (?, 1.0, NULL)", (attempt_id,))
    db.commit()
    db.close()
    return path


def schema_of(path):
    """Форма схемы: нормализованные строки sqlite_master (таблицы и индексы),
    отсортированные; alembic_version — метаданные миграций, вне сравнения."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
        return sorted(" ".join(row[1].split()) for row in rows if row[0] != "alembic_version")
    finally:
        db.close()


def test_old_fixture_stamped_and_data_intact(tmp_path):
    """Старая база (без alembic_version) получает stamp: данные на месте, после
    страховочных ALTER кода схема как у базы, собранной миграцией с нуля."""
    old = old_updates_fixture(tmp_path / "updates.sqlite3")
    upgrade_database(old, "updates")
    fresh = tmp_path / "fresh.sqlite3"
    upgrade_database(fresh, "updates")
    from server_emulator.updates import Updates
    Updates(tmp_path)  # страховка кода (ALTER ip) на старой базе
    assert schema_of(old) == schema_of(fresh)
    db = sqlite3.connect(old)
    try:
        assert db.execute("SELECT id FROM attempts").fetchall() == [("a1",)]
        assert db.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0] == 1
    finally:
        db.close()


def test_fresh_install_schema_matches_code(tmp_path):
    """Новая установка: миграция создаёт ту же схему, что CREATE IF NOT EXISTS кода."""
    migrated = tmp_path / "migrated.sqlite3"
    upgrade_database(migrated, "updates")
    from server_emulator.updates import Updates
    code = tmp_path / "code"
    code.mkdir()
    Updates(code)
    assert schema_of(migrated) == schema_of(code / "updates.sqlite3")


def test_backup_created_before_upgrade(tmp_path):
    """Перед изменяющим обновлением — копия базы рядом, с датой в имени."""
    old = old_updates_fixture(tmp_path / "updates.sqlite3")
    upgrade_database(old, "updates")
    backups = list(tmp_path.glob("updates.sqlite3.backup-*"))
    assert backups and schema_of(backups[0]) == schema_of(old_updates_fixture(tmp_path / "ref.sqlite3"))


def test_migration_failure_restores_and_logs(tmp_path, monkeypatch):
    """Сбой миграции: файл восстановлен из копии (как до обновления), причина — в
    журнале сервера, наверх уходит MigrationFailed — сервер не стартует."""
    old = old_updates_fixture(tmp_path / "updates.sqlite3")
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
    assert sqlite3.connect(old).execute("SELECT id FROM attempts").fetchall() == [("a1",)]
    db = sqlite3.connect(old)
    try:
        assert db.execute("SELECT id FROM attempts").fetchall() == [("a1",)]
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='alembic_version'").fetchone()[0] == 0
    finally:
        db.close()


def test_state_old_fixture_keeps_users_and_devices(tmp_path):
    """Старое хранилище: stamp на head, пользователи и устройства на месте, после
    страховки кода (ALTER/DROP) схема равна базе, собранной с нуля."""
    old = tmp_path / "emulator.sqlite3"
    db = sqlite3.connect(old)
    db.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE settings(revision INTEGER PRIMARY KEY AUTOINCREMENT,
                              payload TEXT NOT NULL, ts REAL NOT NULL);
        CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT NOT NULL, profile TEXT NOT NULL);
        CREATE TABLE devices(sn TEXT PRIMARY KEY,
                             user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                             name TEXT NOT NULL, profile TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE handlers(id TEXT PRIMARY KEY);
        INSERT INTO users VALUES ('u1', 'U', '{}');
        INSERT INTO devices VALUES ('SN-1', 'u1', 'N', '{}', '{}');
        INSERT INTO handlers VALUES ('h1');
    """)
    db.commit()
    db.close()
    upgrade_all(tmp_path)
    from server_emulator.state_store import StateStore
    StateStore(old)  # страховка кода: ALTER колонок, DROP убранных разделов
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    StateStore(fresh_dir / "emulator.sqlite3")
    assert schema_of(old) == schema_of(fresh_dir / "emulator.sqlite3")
    db = sqlite3.connect(old)
    try:
        assert db.execute("SELECT sn, user_id FROM devices").fetchall() == [("SN-1", "u1")]
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='handlers'").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_runtime_runs_migrations_on_start(tmp_path):
    """Миграции при старте runtime: старая база получает stamp и резервную копию
    до открытия соединений, все шесть баз — с alembic_version."""
    old_updates_fixture(tmp_path / "updates.sqlite3")
    from server_emulator.runtime import Runtime
    runtime = Runtime(directory=tmp_path)
    try:
        for name in ("updates.sqlite3", "emulator.sqlite3", "server_log.sqlite3",
                     "traffic.sqlite3", "agent_ops.sqlite3", "routines.sqlite3"):
            assert (tmp_path / name).exists(), name
            db = sqlite3.connect(tmp_path / name)
            try:
                assert db.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0] == 1
            finally:
                db.close()
        assert list(tmp_path.glob("updates.sqlite3.backup-*"))
        data = sqlite3.connect(tmp_path / "updates.sqlite3")
        try:
            assert data.execute("SELECT id FROM attempts").fetchall() == [("a1",)]  # данные на месте
        finally:
            data.close()
    finally:
        await runtime.close()


def test_upgrade_all_creates_all_databases(tmp_path):
    """Чистый каталог state: upgrade_all создаёт все базы с alembic_version."""
    upgrade_all(tmp_path)
    for name in ("server_log.sqlite3", "updates.sqlite3", "traffic.sqlite3",
                 "agent_ops.sqlite3", "routines.sqlite3", "emulator.sqlite3"):
        db = sqlite3.connect(tmp_path / name)
        try:
            assert db.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0] == 1, name
        finally:
            db.close()


def test_old_fixtures_of_remaining_branches(tmp_path):
    """Журнал трафика (старый exchanges без новых колонок), журнал сервера и прогоны
    агента: stamp, страховка кода при открытии — данные на месте, схема как у новой."""
    old = tmp_path / "traffic.sqlite3"
    db = sqlite3.connect(old)
    db.executescript("""
        CREATE TABLE exchanges(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
          direction TEXT, event TEXT, host TEXT, method TEXT, path TEXT, status INTEGER,
          device_sn TEXT, device_id TEXT, request_size INTEGER, response_size INTEGER,
          duration_ms REAL, source TEXT, error TEXT, close_code INTEGER,
          request_headers TEXT, response_headers TEXT,
          request_body BLOB, response_body BLOB, body_truncated INTEGER NOT NULL DEFAULT 0,
          session TEXT, frames_truncated INTEGER NOT NULL DEFAULT 0);
        INSERT INTO exchanges (ts, kind, path) VALUES (1.0, 'http', '/x');
    """)
    db.commit()
    db.close()
    log_db = tmp_path / "server_log.sqlite3"
    db = sqlite3.connect(log_db)
    db.executescript("""CREATE TABLE entries(
        id INTEGER PRIMARY KEY AUTOINCREMENT, dedup TEXT NOT NULL,
        ts REAL NOT NULL, last_ts REAL NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL,
        message TEXT NOT NULL, device_sn TEXT, detail TEXT, count INTEGER NOT NULL DEFAULT 1);
        INSERT INTO entries (dedup, ts, last_ts, level, source, message)
        VALUES ('k', 1.0, 1.0, 'error', 'server', 'сбой');""")
    db.commit()
    db.close()
    upgrade_all(tmp_path)
    from server_emulator.upstream.journal import TrafficJournal
    journal = TrafficJournal(old)
    journal.db.close()
    from server_emulator.server_log import ServerLog
    ServerLog(log_db).db.close()
    fresh_traffic = tmp_path / "fresh-traffic.sqlite3"
    upgrade_database(fresh_traffic, "traffic")
    fresh_journal = TrafficJournal(fresh_traffic)  # та же страховка кода, что на старой базе
    fresh_journal.db.close()
    assert schema_of(old) == schema_of(fresh_traffic)
    fresh_log = tmp_path / "fresh-log.sqlite3"
    upgrade_database(fresh_log, "server_log")
    ServerLog(fresh_log).db.close()  # та же страховка кода, что на старой базе
    assert schema_of(log_db) == schema_of(fresh_log)
    db = sqlite3.connect(old)
    try:
        assert db.execute("SELECT path FROM exchanges").fetchall() == [("/x",)]  # данные на месте
    finally:
        db.close()
