"""SQLite-хранилище состояния эмулятора (identities/settings).

- чтение и первая запись — в памяти (sqlite3 `:memory:`, копия файла на момент открытия): sync,
  без дискового ввода-вывода, цикл событий не блокируется; запись в файл — зеркало: транзакции
  памяти ставятся в очередь и применяются воркером на aiosqlite (WAL, foreign_keys=ON, один writer);
  вне цикла событий (старт процесса, инструменты, sync-тесты) транзакция пишется в файл сразу
  на sync-соединении; flush() дожидается очереди, close() её дорабатывает;
- settings — версионированные JSON-_payload'ы (история ревизий);
- однократная миграция legacy JSON: backup ДО миграции, импорт в одной
  транзакции, при конфликте полный откат с отчётом, после успешного коммита
  оригиналы удаляются (копии остаются в backup);
- двойной записи в JSON и SQLite нет: после миграции источник один — БД;
- close идемпотентен; операции после close дают явный RuntimeError.

Writers таблиц (инвариант: каждую таблицу пишет только её владелец,
блокировка в store не нужна — сериализация на asyncio-цикле, мутации настроек
под runtime.lock у админ-API):

- settings                          → Runtime.write (сохранение настроек);
                                      migrate_legacy (сырой текст legacy);
- users/devices/aliases/unassigned  → IdentityRegistry.commit → save_identity_data;
                                      migrate_legacy;
- executions                        → Executor._persist/_forget (scenario_engine);
- param_defs / param_values         → ParamStore (params.py);
- meta                              → set_meta (группы и overrides Quasar из identity,
                                      перехват OAuth из music).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import sqlite3
import time
from pathlib import Path

import aiosqlite

from .migrations import upgrade_database

log = logging.getLogger(__name__)


class MigrationConflict(ValueError):
    """Конфликт миграции/записи: транзакция откатана целиком, отчёт — в args[0]."""


_WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


class _MemoryDb:
    """Соединение памяти с интерфейсом sqlite3.Connection, который используют вызывающие
    (execute, with, in_transaction): записи транзакции копятся и по commit уходят в зеркало."""

    def __init__(self, memory, mirror):
        self._memory, self._mirror, self._ops = memory, mirror, []

    @property
    def in_transaction(self):
        return self._memory.in_transaction

    def execute(self, sql, params=()):
        cursor = self._memory.execute(sql, params)
        if sql.lstrip()[:7].upper().startswith(_WRITES):
            self._ops.append((sql, tuple(params)))
        return cursor

    def commit(self):
        self._memory.commit()
        ops, self._ops = self._ops, []
        if ops:
            self._mirror(ops)

    def rollback(self):
        self._memory.rollback()
        self._ops = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def close(self):
        self._memory.close()


class StateStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self.on_scenario_data = None  # fn(scenario_id, key) после записи scenario_data (runtime)
        upgrade_database(self.path, "state")  # схема — базовая ревизия ветки state
        self._pending = collections.deque()  # транзакции, ещё не записанные в файл
        self._worker = None
        self._wake = None
        self._idle = None
        self._adb = None  # aiosqlite.Connection — зеркало файла в цикле событий
        self._file = None  # sync-соединение с файлом: загрузка при открытии и запись вне цикла
        memory = sqlite3.connect(":memory:", check_same_thread=False)
        self._file_connection().backup(memory)  # единственное чтение файла: снимок при открытии
        memory.row_factory = sqlite3.Row
        memory.execute("PRAGMA foreign_keys=ON")
        self.db = _MemoryDb(memory, self._mirror)

    def _file_connection(self):
        if self._file is None:
            self._file = sqlite3.connect(self.path, timeout=10)
            self._file.execute("PRAGMA journal_mode=WAL")  # режим хранится в файле базы
            self._file.execute("PRAGMA foreign_keys=ON")
        return self._file

    def _live(self):
        if self._closed:
            raise RuntimeError("StateStore закрыт")
        return self.db

    # --- зеркало в файл ----------------------------------------------------------
    def _mirror(self, ops):
        if self._closed:
            raise RuntimeError("StateStore закрыт")
        self._pending.append(ops)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # нет цикла (старт, инструменты, sync-тесты): запись в файл сразу
            self._drain_direct()
            return
        if self._worker is None or self._worker.done():
            self._wake, self._idle = asyncio.Event(), asyncio.Event()
            self._worker = loop.create_task(self._run())
        self._idle.clear()
        self._wake.set()

    def _drain_direct(self):
        file = self._file_connection()
        while self._pending:
            ops = self._pending.popleft()
            try:
                with file:
                    for sql, params in ops:
                        file.execute(sql, params)
            except sqlite3.Error as exc:
                log.warning("Хранилище состояния: запись в файл не удалась: %s", exc)

    async def _run(self):
        self._adb = await aiosqlite.connect(self.path, timeout=10)
        await self._adb.execute("PRAGMA foreign_keys=ON")
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._pending:
                ops = self._pending.popleft()
                try:
                    for sql, params in ops:
                        await self._adb.execute(sql, params)
                    await self._adb.commit()
                except Exception as exc:  # noqa: BLE001 — зеркало не должно ронять цикл
                    log.warning("Хранилище состояния: запись в файл не удалась: %s", exc)
                    await self._adb.rollback()
            self._idle.set()

    async def flush(self):
        """Дождаться записи в файл всего поставленного (остановка, тесты)."""
        if self._worker is not None and not self._worker.done():
            await self._idle.wait()
        elif self._pending:
            self._drain_direct()

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

    # --- executions: ожидающие продолжения диалоги  ------------------------
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
        callback = self.on_scenario_data
        if callback is not None:  # подписчик (runtime: справочник фраз) не должен ронять запись
            try:
                callback(scenario_id, key)
            except Exception:  # noqa: BLE001
                log.warning("Извещение о записи scenario_data %s не обработано", scenario_id, exc_info=True)

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

    async def close(self):
        """Идемпотентное закрытие: доработать очередь записи, закрыть соединения."""
        if self._closed:
            return
        await self.flush()
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self._adb is not None:
            await self._adb.close()
            self._adb = None
        if self._file is not None:
            self._file.close()
            self._file = None
        self.db.close()


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
