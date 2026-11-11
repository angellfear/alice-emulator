"""Базовая ревизия журнала трафика: текущая схема (upstream/journal.py, SCHEMA);
PRAGMA user_version ветки не задаётся — маскирование и группы живут в коде журнала."""
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS exchanges(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
      direction TEXT, event TEXT, host TEXT, method TEXT, path TEXT, status INTEGER,
      device_sn TEXT, device_id TEXT, request_size INTEGER, response_size INTEGER,
      duration_ms REAL, source TEXT, error TEXT, close_code INTEGER,
      request_headers TEXT, response_headers TEXT,
      request_body BLOB, response_body BLOB, body_truncated INTEGER NOT NULL DEFAULT 0,
      session TEXT, frames_truncated INTEGER NOT NULL DEFAULT 0,
      key TEXT, unhandled INTEGER NOT NULL DEFAULT 0, message_id TEXT, ref_message_id TEXT,
      audio_in_frames INTEGER NOT NULL DEFAULT 0, audio_in_bytes INTEGER NOT NULL DEFAULT 0,
      audio_out_frames INTEGER NOT NULL DEFAULT 0, audio_out_bytes INTEGER NOT NULL DEFAULT 0)""")
    op.execute("CREATE INDEX IF NOT EXISTS exchanges_ts ON exchanges(ts)")
    op.execute("CREATE INDEX IF NOT EXISTS exchanges_host ON exchanges(host)")
    op.execute("CREATE INDEX IF NOT EXISTS exchanges_sn ON exchanges(device_sn)")
    op.execute("""CREATE TABLE IF NOT EXISTS ws_frames(
      exchange_id INTEGER NOT NULL REFERENCES exchanges(id) ON DELETE CASCADE,
      sequence INTEGER NOT NULL, ts REAL NOT NULL, direction TEXT NOT NULL,
      binary INTEGER NOT NULL, size INTEGER NOT NULL, data BLOB)""")
    op.execute("CREATE INDEX IF NOT EXISTS ws_frames_exchange ON ws_frames(exchange_id, sequence)")
    op.execute("""CREATE TABLE IF NOT EXISTS journal_groups(
      device_sn TEXT NOT NULL, key TEXT NOT NULL, count INTEGER NOT NULL,
      first_ts REAL NOT NULL, last_ts REAL NOT NULL, PRIMARY KEY(device_sn, key))""")


def downgrade():
    for table in ("journal_groups", "ws_frames", "exchanges"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
