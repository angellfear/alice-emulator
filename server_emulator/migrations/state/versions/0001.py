"""Базовая ревизия хранилища состояния: текущая схема (state_store.py, SCHEMA +
SCHEMA_VERSION в meta пишет код). Удалённые разделы (handlers/ignored/timers) —
DROP для баз, доживших с ними."""
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.execute("DROP TABLE IF EXISTS handlers")
    op.execute("DROP TABLE IF EXISTS ignored")
    op.execute("DROP TABLE IF EXISTS timers")
    op.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    op.execute("""CREATE TABLE IF NOT EXISTS settings(revision INTEGER PRIMARY KEY AUTOINCREMENT,
                                    payload TEXT NOT NULL, ts REAL NOT NULL)""")
    op.execute("""CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                                 profile TEXT NOT NULL,
                                 location TEXT NOT NULL DEFAULT '{}',
                                 fields TEXT NOT NULL DEFAULT '{}')""")
    op.execute("""CREATE TABLE IF NOT EXISTS devices(sn TEXT PRIMARY KEY,
                                   user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                                   name TEXT NOT NULL, profile TEXT NOT NULL, state TEXT NOT NULL,
                                   room TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                                   fields TEXT NOT NULL DEFAULT '{}', route_mode TEXT, record TEXT)""")
    op.execute("""CREATE TABLE IF NOT EXISTS aliases(device_id TEXT PRIMARY KEY,
                                   sn TEXT NOT NULL REFERENCES devices(sn) ON DELETE CASCADE)""")
    op.execute("""CREATE TABLE IF NOT EXISTS unassigned(device_id TEXT PRIMARY KEY,
                                      first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                                      source TEXT, ip TEXT)""")
    op.execute("""CREATE TABLE IF NOT EXISTS executions(dialog_id TEXT PRIMARY KEY,
                                    payload TEXT NOT NULL, ts REAL NOT NULL)""")
    op.execute("""CREATE TABLE IF NOT EXISTS param_defs(key TEXT PRIMARY KEY, scope TEXT NOT NULL,
                                    category TEXT NOT NULL DEFAULT 'user',
                                    user_editable INTEGER NOT NULL DEFAULT 0,
                                    type TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                                    hint TEXT NOT NULL DEFAULT '', options TEXT,
                                    default_value TEXT)""")
    op.execute("CREATE INDEX IF NOT EXISTS param_defs_scope ON param_defs(scope, category)")
    op.execute("""CREATE TABLE IF NOT EXISTS param_values(owner TEXT NOT NULL, key TEXT NOT NULL,
                                     value TEXT NOT NULL, updated_ts REAL NOT NULL,
                                     PRIMARY KEY(owner, key))""")
    op.execute("""CREATE TABLE IF NOT EXISTS scenario_data(scenario_id TEXT NOT NULL,
                                         scope TEXT NOT NULL, owner TEXT NOT NULL,
                                         key TEXT NOT NULL, value TEXT NOT NULL,
                                         updated_ts REAL NOT NULL,
                                         PRIMARY KEY(scenario_id, scope, owner, key))""")
    op.execute("CREATE INDEX IF NOT EXISTS devices_owner ON devices(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS settings_ts ON settings(ts)")


def downgrade():
    for table in ("scenario_data", "param_values", "param_defs", "executions", "unassigned",
                  "aliases", "devices", "users", "settings", "meta"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
