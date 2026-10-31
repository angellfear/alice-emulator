"""B02: SQLite-хранилище состояния эмулятора (identities/settings).

- WAL, foreign_keys=ON, короткие транзакции;
- settings — версионированные JSON-_payload'ы (история ревизий);
- однократная миграция legacy JSON: backup ДО миграции, импорт в одной
  транзакции, при конфликте полный откат с отчётом, после успешного коммита
  оригиналы удаляются (копии остаются в backup);
- двойной записи в JSON и SQLite нет: после миграции источник один — БД;
- close идемпотентен; операции после close дают явный RuntimeError.

Writers таблиц (RF-08 — инвариант: каждую таблицу пишет только её владелец,
блокировка в store не нужна — сериализация на asyncio-цикле, мутации настроек
под runtime.lock у админ-API):

- settings                          → Runtime.write (сохранение настроек);
                                      migrate_legacy (сырой текст legacy);
- users/devices/aliases/unassigned  → IdentityRegistry.commit → save_identity_data;
                                      migrate_legacy;
- executions                        → Executor._persist/_forget (scenario_engine);
- param_defs / param_values         → ParamStore (params.py);
- timers                            → TimerScheduler._save (scenario_engine/scheduler.py);
- meta                              → set_meta (schema_version, legacy_migrated).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1
LEGACY_FILES = ("identities.json", "settings.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(revision INTEGER PRIMARY KEY AUTOINCREMENT,
                                    payload TEXT NOT NULL, ts REAL NOT NULL);
DROP TABLE IF EXISTS handlers; -- раздел «Обработчики» удалён: хранилище обработчиков больше не нужно
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL, profile TEXT NOT NULL,
                                 location TEXT NOT NULL DEFAULT '{}', fields TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS devices(sn TEXT PRIMARY KEY,
                                   user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                                   name TEXT NOT NULL, profile TEXT NOT NULL, state TEXT NOT NULL,
                                   room TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                                   fields TEXT NOT NULL DEFAULT '{}', route_mode TEXT, record TEXT);
CREATE TABLE IF NOT EXISTS aliases(device_id TEXT PRIMARY KEY,
                                   sn TEXT NOT NULL REFERENCES devices(sn) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS unassigned(device_id TEXT PRIMARY KEY,
                                      first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                                      source TEXT, ip TEXT);
DROP TABLE IF EXISTS ignored; -- шаг назад: удаление устройства больше не игнорирует его get_sync
CREATE TABLE IF NOT EXISTS executions(dialog_id TEXT PRIMARY KEY, payload TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS param_defs(key TEXT PRIMARY KEY, scope TEXT NOT NULL,
                                    category TEXT NOT NULL DEFAULT 'user',
                                    user_editable INTEGER NOT NULL DEFAULT 0,
                                    type TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                                    hint TEXT NOT NULL DEFAULT '', options TEXT,
                                    default_value TEXT);
CREATE INDEX IF NOT EXISTS param_defs_scope ON param_defs(scope, category);
CREATE TABLE IF NOT EXISTS param_values(owner TEXT NOT NULL, key TEXT NOT NULL,
                                     value TEXT NOT NULL, updated_ts REAL NOT NULL,
                                     PRIMARY KEY(owner, key));
CREATE TABLE IF NOT EXISTS timers(id TEXT PRIMARY KEY, kind TEXT, sn TEXT, user_id TEXT,
                                  dialog_id TEXT, label TEXT, duration_s INTEGER,
                                  created_ts REAL, due_ts REAL, status TEXT, fired_ts REAL);
CREATE INDEX IF NOT EXISTS timers_status ON timers(status);
CREATE TABLE IF NOT EXISTS scenario_data(scenario_id TEXT NOT NULL, scope TEXT NOT NULL,
                                         owner TEXT NOT NULL, key TEXT NOT NULL,
                                         value TEXT NOT NULL, updated_ts REAL NOT NULL,
                                         PRIMARY KEY(scenario_id, scope, owner, key));
CREATE INDEX IF NOT EXISTS devices_owner ON devices(user_id);
CREATE INDEX IF NOT EXISTS settings_ts ON settings(ts);
"""


class MigrationConflict(ValueError):
    """Конфликт миграции/записи: транзакция откатана целиком, отчёт — в args[0]."""


class StateStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.db:
            self.db.executescript(SCHEMA)
            # B03: аддитивные колонки unassigned (старые БД получают ALTER'ом)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(unassigned)")}
            for column in ("source", "ip"):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE unassigned ADD COLUMN {column} TEXT")
            # Доп. поля пользователя/устройства для сценариев (гео, комната, произвольные)
            user_columns = {row[1] for row in self.db.execute("PRAGMA table_info(users)")}
            for column in ("location", "fields"):
                if column not in user_columns:
                    self.db.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")
            device_columns = {row[1] for row in self.db.execute("PRAGMA table_info(devices)")}
            for column, default in (("room", "''"), ("model", "''"), ("fields", "'{}'")):
                if column not in device_columns:
                    self.db.execute(f"ALTER TABLE devices ADD COLUMN {column} TEXT NOT NULL DEFAULT {default}")
            if "route_mode" not in device_columns:  # TASK-044: режим маршрута устройства
                self.db.execute("ALTER TABLE devices ADD COLUMN route_mode TEXT")
            if "record" not in device_columns:  # TASK-054: опция записи устройства (off | on | misses)
                self.db.execute("ALTER TABLE devices ADD COLUMN record TEXT")
            # M08-будильники: абсолютное время/дни недели/таймзона/следующее срабатывание
            timer_columns = {row[1] for row in self.db.execute("PRAGMA table_info(timers)")}
            for column in ("time_hhmm", "weekdays", "tz", "next_ts"):
                if column not in timer_columns:
                    self.db.execute(f"ALTER TABLE timers ADD COLUMN {column}")
            row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self.db.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            elif int(row[0]) > SCHEMA_VERSION:
                raise ValueError("БД состояния создана более новой версией кода")

    def _live(self):
        if self._closed:
            raise RuntimeError("StateStore закрыт")
        return self.db

    def meta(self, key):
        row = self._live().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        db = self._live()
        with db:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # --- settings: версионированные payload'ы ---------------------------------
    def load_settings(self):
        row = self._live().execute("SELECT payload FROM settings ORDER BY revision DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def save_settings(self, payload):
        db = self._live()
        with db:
            db.execute("INSERT INTO settings(payload,ts) VALUES(?,?)",
                       (json.dumps(payload, ensure_ascii=False, indent=2), time.time()))

    def settings_history(self):
        return [json.loads(r[0]) for r in self._live().execute(
            "SELECT payload FROM settings ORDER BY revision DESC LIMIT 50")]

    # --- executions: ожидающие продолжения диалоги (R06) ------------------------
    def load_execution(self, dialog_id):
        row = self._live().execute("SELECT payload FROM executions WHERE dialog_id=?", (dialog_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_execution(self, dialog_id, payload):
        db = self._live()
        with db:
            db.execute("INSERT INTO executions(dialog_id,payload,ts) VALUES(?,?,?) "
                       "ON CONFLICT(dialog_id) DO UPDATE SET payload=excluded.payload, ts=excluded.ts",
                       (dialog_id, json.dumps(payload, ensure_ascii=False), time.time()))

    def delete_execution(self, dialog_id):
        db = self._live()
        with db:
            db.execute("DELETE FROM executions WHERE dialog_id=?", (dialog_id,))

    def list_executions(self):
        return [json.loads(r[0]) for r in self._live().execute(
            "SELECT payload FROM executions ORDER BY ts")]

    # --- scenario_data: данные сценариев (user — владелец колонки, device — SN,
    # system — общие; owner пустой) -------------------------------------------
    def scenario_data_get(self, scenario_id, scope, owner):
        return {key: json.loads(value) for key, value in self._live().execute(
            "SELECT key, value FROM scenario_data WHERE scenario_id=? AND scope=? AND owner=?",
            (scenario_id, scope, owner or ""))}

    def scenario_data_record(self, scenario_id, scope, owner, key):
        """Одна запись с временем обновления: (value, updated_ts) | None."""
        row = self._live().execute(
            "SELECT value, updated_ts FROM scenario_data WHERE scenario_id=? AND scope=? AND owner=? AND key=?",
            (scenario_id, scope, owner or "", key)).fetchone()
        return None if row is None else (json.loads(row[0]), row[1])

    def scenario_data_set(self, scenario_id, scope, owner, key, value):
        db = self._live()
        with db:
            db.execute("INSERT INTO scenario_data(scenario_id,scope,owner,key,value,updated_ts) "
                       "VALUES(?,?,?,?,?,?) ON CONFLICT(scenario_id,scope,owner,key) "
                       "DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                       (scenario_id, scope, owner or "", key,
                        json.dumps(value, ensure_ascii=False), time.time()))

    def scenario_data_delete(self, scenario_id, scope=None, owner=None, key=None):
        clauses, args = ["scenario_id=?"], [scenario_id]
        for column, value in (("scope", scope), ("owner", owner), ("key", key)):
            if value is not None:
                clauses.append(column + "=?")
                args.append(value)
        db = self._live()
        with db:
            return db.execute("DELETE FROM scenario_data WHERE " + " AND ".join(clauses), args).rowcount

    def scenario_data_list(self, scenario_id=None):
        query = "SELECT scenario_id, scope, owner, key, value, updated_ts FROM scenario_data"
        rows = self._live().execute(query + (" WHERE scenario_id=?" if scenario_id else "") +
                                    " ORDER BY scenario_id, scope, owner, key",
                                    (scenario_id,) if scenario_id else ())
        return [{"scenario_id": r[0], "scope": r[1], "owner": r[2], "key": r[3],
                 "value": json.loads(r[4]), "updated": r[5]} for r in rows]

    # --- identities ------------------------------------------------------------
    def load_identity_data(self):
        db = self._live()
        users = [{"id": r["id"], "name": r["name"], "profile": json.loads(r["profile"]),
                  "location": json.loads(r["location"] or "{}"),
                  "fields": json.loads(r["fields"] or "{}")}
                 for r in db.execute("SELECT * FROM users ORDER BY id")]
        devices = []
        for r in db.execute("SELECT * FROM devices ORDER BY sn"):
            aliases = [a[0] for a in db.execute(
                "SELECT device_id FROM aliases WHERE sn=? ORDER BY device_id", (r["sn"],))]
            devices.append({"sn": r["sn"], "user_id": r["user_id"], "name": r["name"],
                            "device_ids": aliases, "profile": json.loads(r["profile"]),
                            "state": json.loads(r["state"]), "room": r["room"] or "", "model": r["model"] or "",
                            "fields": json.loads(r["fields"] or "{}"),
                            "route_mode": r["route_mode"] or None, "record": r["record"] or "off"})
        unassigned = [{"device_id": r["device_id"], "first_seen": r["first_seen"],
                       "last_seen": r["last_seen"], "source": r["source"], "ip": r["ip"]}
                      for r in db.execute("SELECT * FROM unassigned ORDER BY device_id")]
        return {"users": users, "devices": devices, "unassigned": unassigned}

    def save_identity_data(self, value, manage_transaction=True):
        """Полная замена набора identities с валидацией; конфликт → откат целиком."""
        db = self._live()
        users, devices, unassigned = value.get("users", []), value.get("devices", []), value.get("unassigned", [])
        user_ids = {u["id"] for u in users}
        seen_aliases = {}
        for device in devices:
            for alias in device.get("device_ids", []):
                if alias in seen_aliases and seen_aliases[alias] != device["sn"]:
                    raise MigrationConflict(f"DeviceId {alias} привязан к нескольким SN "
                                            f"({seen_aliases[alias]}, {device['sn']})")
                seen_aliases[alias] = device["sn"]
            if device.get("user_id") and device["user_id"] not in user_ids:
                raise MigrationConflict(f"Устройство {device['sn']} ссылается на неизвестного пользователя {device['user_id']}")
        if db.in_transaction and manage_transaction:
            raise RuntimeError("вложенная транзакция не поддерживается")
        context = db if manage_transaction else _nullcontext()
        with context:
            db.execute("DELETE FROM aliases")
            db.execute("DELETE FROM devices")
            db.execute("DELETE FROM users")
            db.execute("DELETE FROM unassigned")
            for user in users:
                db.execute("INSERT INTO users VALUES(?,?,?,?,?)",
                           (user["id"], user["name"], json.dumps(user.get("profile", {}), ensure_ascii=False),
                            json.dumps(user.get("location", {}), ensure_ascii=False),
                            json.dumps(user.get("fields", {}), ensure_ascii=False)))
            for device in devices:
                db.execute("INSERT INTO devices(sn,user_id,name,profile,state,room,model,fields,route_mode,record) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (device["sn"], device.get("user_id"), device.get("name", device["sn"]),
                            json.dumps(device.get("profile", {}), ensure_ascii=False),
                            json.dumps(device.get("state", {}), ensure_ascii=False),
                            device.get("room", ""), device.get("model", ""),
                            json.dumps(device.get("fields", {}), ensure_ascii=False),
                            device.get("route_mode"), device.get("record") or "off"))
                for alias in device.get("device_ids", []):
                    db.execute("INSERT INTO aliases VALUES(?,?)", (alias, device["sn"]))
            for item in unassigned:
                db.execute("INSERT OR REPLACE INTO unassigned VALUES(?,?,?,?,?)",
                           (item["device_id"], item.get("first_seen", time.time()), time.time(),
                            item.get("source"), item.get("ip")))

    def close(self):
        """Идемпотентное закрытие; повторный вызов — no-op."""
        if self._closed:
            return
        self._closed = True
        self.db.close()


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def migrate_legacy(directory, store):
    """Однократный импорт legacy JSON в StateStore. Возвращает отчёт или None."""
    directory = Path(directory)
    db = store._live()
    migrated = store.meta("legacy_migrated") == "1"
    present = {name: directory / name for name in LEGACY_FILES}
    present = {name: path for name, path in present.items() if path.exists()}
    if migrated:
        if not present:
            return None
        # После миграции единственный источник — БД. Прочие stray-файлы не
        # импортируем и не удаляем — только отчёт.
        return {"backup": None, "imported": [], "conflict": None,
                "skipped_stray": sorted(present)}
    report = {"backup": None, "imported": sorted(present), "conflict": None}
    if present:
        backup = directory / ("legacy-backup-" + time.strftime("%Y%m%d-%H%M%S"))
        backup.mkdir(parents=True, exist_ok=True)  # повтор в ту же секунду — до-копируем
        for path in present.values():
            shutil.copy2(path, backup / path.name)  # backup ДО миграции
        report["backup"] = backup.name
    try:
        with db:
            if "settings.json" in present and store.load_settings() is None:
                db.execute("INSERT INTO settings(payload,ts) VALUES(?,?)",
                           (present["settings.json"].read_text(encoding="utf-8"), time.time()))
            if "identities.json" in present:
                store.save_identity_data(json.loads(present["identities.json"].read_text(encoding="utf-8")),
                                         manage_transaction=False)
            db.execute("INSERT INTO meta(key,value) VALUES('legacy_migrated','1') "
                       "ON CONFLICT(key) DO UPDATE SET value='1'")
    except (ValueError, sqlite3.IntegrityError, KeyError, json.JSONDecodeError) as exc:
        # Откат произошёл (транзакция целиком); файлы не тронуты, backup сохранён.
        report["imported"] = []
        report["conflict"] = f"{type(exc).__name__}: {exc}"
        raise MigrationConflict(json.dumps(report, ensure_ascii=False)) from exc
    for path in present.values():
        path.unlink()  # атомарное переключение: после успешного коммита
    return report
