"""Базовая ревизия журнала прогонов агента: текущая схема (agent/ops.py, SCHEMA)."""
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, thread TEXT, kind TEXT,
        user_id TEXT, sn TEXT, text TEXT, answer TEXT, tools TEXT, model_calls INTEGER,
        tokens INTEGER, ms INTEGER, status TEXT)""")
    op.execute("CREATE INDEX IF NOT EXISTS runs_day ON runs (day, user_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS runs")
