"""Базовая ревизия журнала сервера: текущая схема (server_log.py, SCHEMA)."""
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS entries(
      id INTEGER PRIMARY KEY AUTOINCREMENT, dedup TEXT NOT NULL,
      ts REAL NOT NULL, last_ts REAL NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL,
      message TEXT NOT NULL, device_sn TEXT, detail TEXT, count INTEGER NOT NULL DEFAULT 1)""")
    op.execute("CREATE INDEX IF NOT EXISTS entries_last_ts ON entries(last_ts)")
    op.execute("CREATE INDEX IF NOT EXISTS entries_dedup ON entries(dedup, last_ts)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS entries")
