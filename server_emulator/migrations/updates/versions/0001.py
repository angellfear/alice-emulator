"""Базовая ревизия базы обновлений: текущая схема (updates.py; колонка ip — на месте
ALTER'а старых баз, в конце списка колонок attempts)."""
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    op.execute("""CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, ts REAL, endpoint TEXT,
        device_sn TEXT, user_id TEXT, device_id TEXT, policy TEXT, status INTEGER, error TEXT,
        ip TEXT)""")
    op.execute("CREATE INDEX IF NOT EXISTS attempts_ts ON attempts(ts)")
    op.execute("CREATE TABLE IF NOT EXISTS urls (url TEXT PRIMARY KEY, first_seen REAL, "
               "last_seen REAL, hits INTEGER, metadata TEXT)")
    op.execute("""CREATE TABLE IF NOT EXISTS sightings (attempt_id TEXT, url TEXT,
        PRIMARY KEY(attempt_id,url))""")
    op.execute("""CREATE TABLE IF NOT EXISTS last_requests (endpoint TEXT, device_sn TEXT,
        method TEXT, target TEXT, headers TEXT, body BLOB, ts REAL,
        PRIMARY KEY(endpoint, device_sn))""")


def downgrade():
    for table in ("last_requests", "sightings", "urls", "attempts", "settings"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
