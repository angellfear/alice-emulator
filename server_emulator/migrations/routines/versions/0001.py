"""Базовая ревизия рутин агента: текущая схема (agent/routines.py, SCHEMA)."""
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS routines (id TEXT PRIMARY KEY, owner TEXT NOT NULL,
        data TEXT NOT NULL, created REAL NOT NULL, last_run REAL)""")
    op.execute("""CREATE TABLE IF NOT EXISTS routine_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
        routine_id TEXT NOT NULL, ts REAL NOT NULL, status TEXT NOT NULL, text TEXT, error TEXT,
        delivered TEXT)""")
    op.execute("CREATE INDEX IF NOT EXISTS routine_runs_by_routine ON routine_runs (routine_id, ts)")
    op.execute("""CREATE TABLE IF NOT EXISTS inbox (id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL, sn TEXT, routine_id TEXT, title TEXT, text TEXT NOT NULL,
        ts REAL NOT NULL, delivered REAL)""")


def downgrade():
    for table in ("inbox", "routine_runs", "routines"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
