"""Журнал трафика устройств (TASK-043, TASK-054): state/traffic.sqlite3.

Пишет обмены: время, SN/DeviceId устройства, направление, хост, метод, путь, статус,
размеры, длительность и источник ответа (emulator|upstream). Что писать, решает опция
«Запись» устройства (upstream/record.py), журнал только хранит. Тела — сжатые zstandard
с лимитом на тело. WS: открытие и закрытие сессии — записи lifecycle (в закрытии —
счётчики звуковых кадров без данных), AAPI-кадр и неизвестный кадр — отдельные записи.
Записи группируются по устройству и ключу события (HTTP — «METHOD host path» без query,
WS — направление и имя сообщения): на пару хранятся последние GROUP_LIMIT записей, счётчик
всех случаев — в таблице journal_groups. Ротация по размеру файла и сроку —
настройки upstream.journal. Значения секретных заголовков (Authorization, Cookie, токены,
тикеты) маскируются до записи — журнал и экспорт не содержат учётных данных колонки; записи,
сделанные до маскирования, вычищаются при открытии. Запись асинхронная, через очередь: обмен кладёт запись
и не ждёт диска, воркер батчит и пишет в SQLite; flush() дожидается записи
(остановка, тесты).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from pathlib import Path

import zstandard

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
  direction TEXT, event TEXT, host TEXT, method TEXT, path TEXT, status INTEGER,
  device_sn TEXT, device_id TEXT, request_size INTEGER, response_size INTEGER,
  duration_ms REAL, source TEXT, error TEXT, close_code INTEGER,
  request_headers TEXT, response_headers TEXT,
  request_body BLOB, response_body BLOB, body_truncated INTEGER NOT NULL DEFAULT 0,
  session TEXT, frames_truncated INTEGER NOT NULL DEFAULT 0,
  key TEXT, unhandled INTEGER NOT NULL DEFAULT 0, message_id TEXT, ref_message_id TEXT,
  audio_in_frames INTEGER NOT NULL DEFAULT 0, audio_in_bytes INTEGER NOT NULL DEFAULT 0,
  audio_out_frames INTEGER NOT NULL DEFAULT 0, audio_out_bytes INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS exchanges_ts ON exchanges(ts);
CREATE INDEX IF NOT EXISTS exchanges_host ON exchanges(host);
CREATE INDEX IF NOT EXISTS exchanges_sn ON exchanges(device_sn);
CREATE TABLE IF NOT EXISTS ws_frames(
  exchange_id INTEGER NOT NULL REFERENCES exchanges(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL, ts REAL NOT NULL, direction TEXT NOT NULL,
  binary INTEGER NOT NULL, size INTEGER NOT NULL, data BLOB);
CREATE INDEX IF NOT EXISTS ws_frames_exchange ON ws_frames(exchange_id, sequence);
CREATE TABLE IF NOT EXISTS journal_groups(
  device_sn TEXT NOT NULL, key TEXT NOT NULL, count INTEGER NOT NULL,
  first_ts REAL NOT NULL, last_ts REAL NOT NULL, PRIMARY KEY(device_sn, key));
"""
# колонки TASK-054: старые журналы получают их ALTER'ом (до создания индекса по ним)
GROUP_COLUMNS = (("key", "TEXT"), ("unhandled", "INTEGER NOT NULL DEFAULT 0"), ("message_id", "TEXT"),
                 ("ref_message_id", "TEXT"), ("audio_in_frames", "INTEGER NOT NULL DEFAULT 0"),
                 ("audio_in_bytes", "INTEGER NOT NULL DEFAULT 0"), ("audio_out_frames", "INTEGER NOT NULL DEFAULT 0"),
                 ("audio_out_bytes", "INTEGER NOT NULL DEFAULT 0"))
GROUP_INDEX = "CREATE INDEX IF NOT EXISTS exchanges_group ON exchanges(device_sn, key, id)"
GROUP_LIMIT = 10  # записей на пару (устройство, ключ события)

COLUMNS = ("ts", "kind", "direction", "event", "host", "method", "path", "status", "device_sn",
           "device_id", "request_size", "response_size", "duration_ms", "source", "error",
           "close_code", "request_headers", "response_headers", "request_body", "response_body",
           "session", "key", "unhandled", "message_id", "ref_message_id", "audio_in_frames",
           "audio_in_bytes", "audio_out_frames", "audio_out_bytes")
LIST_COLUMNS = ("id", "ts", "kind", "direction", "event", "host", "method", "path", "status",
                "device_sn", "device_id", "request_size", "response_size", "duration_ms", "source", "error",
                "key", "unhandled", "message_id", "ref_message_id")
DETAIL_COLUMNS = LIST_COLUMNS + ("request_headers", "response_headers", "request_body", "response_body",
                                 "body_truncated", "session", "frames_truncated", "close_code",
                                 "audio_in_frames", "audio_in_bytes", "audio_out_frames", "audio_out_bytes")
BLOB_COLUMNS = ("request_body", "response_body")

DEFAULTS = {"max_body_kb": 256, "max_size_mb": 100, "max_age_days": 14}
AGE_ROTATE_EVERY_S = 3600
SECRET_HEADER = re.compile(r"authorization|cookie|token|ticket|secret|password|api[-_]?key|session[-_]?id", re.I)
MASK_VERSION = 1  # PRAGMA user_version: записи старше — с открытыми значениями, вычищаются
GROUP_VERSION = 2  # PRAGMA user_version: записи старше — без ключа события, группы строятся при открытии
AAPI_PREFIX = b"AAPI"


def event_key(method, host, path):
    """Ключ события HTTP: «METHOD host path» без query (группировка журнала)."""
    return f"{(method or '').upper()} {(host or '').lower()} {str(path or '').split('?', 1)[0]}".strip()


def mask_value(name, value):
    """Значение секретного заголовка → «***» (схема Authorization сохраняется: «OAuth ***»)."""
    if not SECRET_HEADER.search(str(name)):
        return value
    scheme, _sep, rest = str(value).partition(" ")
    return f"{scheme} ***" if rest and scheme.isalpha() and len(scheme) <= 16 else "***"


def mask_headers(headers):
    """Список пар или словарь заголовков → список пар с замаскированными секретами."""
    pairs = headers.items() if hasattr(headers, "items") else headers
    return [(name, mask_value(name, value)) for name, value in pairs]
SIZE_CHECK_EVERY = 200


class TrafficJournal:
    """SQLite-журнал трафика с асинхронной записью через очередь."""

    def __init__(self, path, config=None):
        self.path = Path(path)
        self.config = config or {}  # dict или callable → settings["upstream"]["journal"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.db:
            self.db.executescript(SCHEMA)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(exchanges)")}
            for name, definition in GROUP_COLUMNS:
                if name not in columns:
                    self.db.execute(f"ALTER TABLE exchanges ADD COLUMN {name} {definition}")
            self.db.execute(GROUP_INDEX)
        self._scrub()
        self._regroup()
        self._queue = None
        self._worker = None
        self._closed = False
        self._writes = 0
        self._last_age_rotation = time.monotonic()

    def _scrub(self):
        """Однократно маскирует заголовки записей, сделанных до маскирования."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= MASK_VERSION:
            return
        with self.db:
            for row_id, *values in self.db.execute(
                    "SELECT id, request_headers, response_headers FROM exchanges").fetchall():
                masked = []
                for value in values:
                    try:
                        pairs = json.loads(value) if value else None
                    except ValueError:
                        pairs = None
                    masked.append(json.dumps(mask_headers(pairs), ensure_ascii=False)
                                  if isinstance(pairs, (list, dict)) else value)
                self.db.execute("UPDATE exchanges SET request_headers=?, response_headers=? WHERE id=?",
                                (*masked, row_id))
            self.db.execute(f"PRAGMA user_version={MASK_VERSION}")

    def _regroup(self):
        """Однократно (TASK-054): старым записям — ключ события, из них — таблица групп с
        прежним числом случаев, на пару остаются последние GROUP_LIMIT. Звуковые кадры старых
        WS-сессий (бинарные без AAPI) удаляются, их число и размер — в счётчиках записи open."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= GROUP_VERSION:
            return
        with self.db:
            for row_id, kind, event, host, method, path in self.db.execute(
                    "SELECT id, kind, event, host, method, path FROM exchanges WHERE key IS NULL").fetchall():
                key = f"ws {event or 'unknown'}" if kind == "ws" else event_key(method, host, path)
                self.db.execute("UPDATE exchanges SET key=? WHERE id=?", (key, row_id))
            self.db.execute("DELETE FROM journal_groups")
            self.db.execute(
                "INSERT INTO journal_groups(device_sn, key, count, first_ts, last_ts) "
                "SELECT COALESCE(device_sn, ''), key, COUNT(*), MIN(ts), MAX(ts) FROM exchanges "
                "WHERE key IS NOT NULL GROUP BY COALESCE(device_sn, ''), key")
            for exchange_id, sequence, direction, size, data in self.db.execute(
                    "SELECT exchange_id, sequence, direction, size, data FROM ws_frames WHERE binary=1").fetchall():
                if (self._decompress(data) or b"")[:4] == AAPI_PREFIX:
                    continue
                column = "audio_in" if direction == "client_to_server" else "audio_out"
                self.db.execute(f"UPDATE exchanges SET {column}_frames={column}_frames+1, "
                                f"{column}_bytes={column}_bytes+? WHERE id=?", (size, exchange_id))
                self.db.execute("DELETE FROM ws_frames WHERE exchange_id=? AND sequence=?",
                                (exchange_id, sequence))
            for device_sn, key in self.db.execute(
                    "SELECT device_sn, key FROM journal_groups").fetchall():
                self._trim(device_sn or None, key)
            self.db.execute(f"PRAGMA user_version={GROUP_VERSION}")

    # --- запись ---------------------------------------------------------------
    def settings(self):
        value = self.config() if callable(self.config) else self.config
        result = dict(DEFAULTS)
        if isinstance(value, dict):
            for key in result:
                number = value.get(key)
                if isinstance(number, (int, float)) and not isinstance(number, bool) and number >= 0:
                    result[key] = number
        return result

    def _ensure_worker(self):
        if self._worker is None and not self._closed:
            self._queue = asyncio.Queue()
            self._worker = asyncio.create_task(self._run())

    def write(self, **fields):
        """Поставить обмен в очередь (не ждёт диска); тела — bytes, сжимает воркер."""
        if self._closed:
            return
        try:
            self._ensure_worker()
        except RuntimeError:  # нет цикла событий — журналить некому
            return
        fields.setdefault("ts", time.time())
        self._queue.put_nowait(("exchange", fields))

    async def flush(self):
        """Дождаться записи всего поставленного (остановка, тесты)."""
        if self._worker is None:
            return
        done = asyncio.Event()
        self._queue.put_nowait(("flush", done))
        await done.wait()

    async def close(self):
        """Идемпотентная остановка: доработать очередь, закрыть БД."""
        if self._closed:
            return
        self._closed = True
        if self._worker is not None:
            self._queue.put_nowait(("stop",))
            try:
                await asyncio.wait_for(asyncio.shield(self._worker), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        self.db.close()

    async def _run(self):
        while True:
            batch = [await self._queue.get()]
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            stop = any(item[0] == "stop" for item in batch)
            try:
                self._apply(batch)
            except Exception as exc:  # noqa: BLE001 — журнал не должен ронять цикл
                log.warning("Журнал трафика: запись не удалась: %s", exc)
            if stop:
                break

    def _apply(self, batch):
        """Элементы партии обрабатываются в порядке очереди."""
        events = []
        for item in batch:
            if item[0] == "exchange":
                self._insert(item[1])
            elif item[0] == "flush":
                events.append(item[1])
        self.db.commit()
        for event in events:
            event.set()
        self._writes += 1
        self._rotate_maybe()

    def _insert(self, fields):
        limit = int(self.settings()["max_body_kb"] * 1024)
        row = {column: fields.get(column) for column in COLUMNS}
        for name in ("request_headers", "response_headers"):
            if isinstance(row.get(name), (list, tuple, dict)) or hasattr(row.get(name), "items"):
                row[name] = json.dumps(mask_headers(row[name]), ensure_ascii=False)
        truncated = 0
        for name in BLOB_COLUMNS:
            blob = row.get(name)
            if not blob:
                row[name] = None
                continue
            data = bytes(blob)
            if len(data) > limit:
                data, truncated = data[:limit], 1
            row[name] = zstandard.compress(data)
        row["body_truncated"] = truncated
        row["kind"] = row.get("kind") or "http"
        row["device_sn"] = row.get("device_sn") or None
        if not row.get("key") and row["kind"] == "http" and (row.get("host") or row.get("path")):
            row["key"] = event_key(row.get("method"), row.get("host"), row.get("path"))
        row["unhandled"] = 1 if row.get("unhandled") else 0
        for name in ("audio_in_frames", "audio_in_bytes", "audio_out_frames", "audio_out_bytes"):
            row[name] = int(row.get(name) or 0)
        columns = ",".join(row)
        self.db.execute(f"INSERT INTO exchanges({columns}) VALUES({','.join('?' * len(row))})",
                        tuple(row.values()))
        if row.get("key"):
            sn = row.get("device_sn") or ""
            self.db.execute(
                "INSERT INTO journal_groups(device_sn, key, count, first_ts, last_ts) VALUES(?,?,1,?,?) "
                "ON CONFLICT(device_sn, key) DO UPDATE SET count=count+1, last_ts=excluded.last_ts",
                (sn, row["key"], row["ts"], row["ts"]))
            self._trim(row.get("device_sn"), row["key"])

    def _trim(self, device_sn, key):
        """На пару (устройство, ключ) остаются последние GROUP_LIMIT записей."""
        self.db.execute("DELETE FROM exchanges WHERE id IN (SELECT id FROM exchanges WHERE device_sn IS ? AND key=? "
                        "ORDER BY id DESC LIMIT -1 OFFSET ?)", (device_sn, key, GROUP_LIMIT))

    # --- чтение ---------------------------------------------------------------
    def list(self, device=None, host=None, source=None, unhandled=False, key=None, limit=100, offset=0):
        """Список обменов (без тел), новые сверху; фильтры: устройство и хост — подстрока,
        источник и ключ события — точное значение, unhandled — только запросы без обработчика."""
        clauses, args = [], []
        if device:
            clauses.append("device_sn LIKE ?")
            args.append(f"%{device}%")
        if host:
            clauses.append("host LIKE ?")
            args.append(f"%{host}%")
        if source:
            clauses.append("source=?")
            args.append(source)
        if key:
            clauses.append("key=?")
            args.append(key)
        if unhandled:
            clauses.append("unhandled=1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.db.execute("SELECT COUNT(*) FROM exchanges" + where, args).fetchone()[0]
        rows = self.db.execute(f"SELECT {','.join(LIST_COLUMNS)} FROM exchanges{where} "
                               "ORDER BY id DESC LIMIT ? OFFSET ?",
                               [*args, max(1, min(int(limit), 500)), max(0, int(offset))]).fetchall()
        return {"items": [dict(zip(LIST_COLUMNS, row)) for row in rows], "total": total}

    def groups(self, device_sn=None, source=None, unhandled=False):
        """Группы событий по устройствам: [{device_sn, groups: [{key, count, first_ts, last_ts}]}].
        Счётчик count — все случаи (не теряется при вытеснении записей); с фильтрами источника
        и «без обработчика» остаются группы, где есть хотя бы одна подходящая запись."""
        clauses, args = [], []
        if device_sn is not None:
            clauses.append("g.device_sn=?")
            args.append(device_sn)
        inner = []
        if source:
            inner.append("e.source=?")
            args.append(source)
        if unhandled:
            inner.append("e.unhandled=1")
        if inner:
            clauses.append("EXISTS (SELECT 1 FROM exchanges e WHERE COALESCE(e.device_sn,'')=g.device_sn "
                           "AND e.key=g.key AND " + " AND ".join(inner) + ")")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        result = {}
        for sn, key, count, first_ts, last_ts in self.db.execute(
                "SELECT g.device_sn, g.key, g.count, g.first_ts, g.last_ts FROM journal_groups g"
                f"{where} ORDER BY g.last_ts DESC", args):
            result.setdefault(sn, []).append({"key": key, "count": count, "first_ts": first_ts, "last_ts": last_ts})
        return [{"device_sn": sn or None, "groups": groups} for sn, groups in result.items()]

    def group_items(self, device_sn, key, source=None, unhandled=False):
        """Записи группы (до GROUP_LIMIT), новые сверху, без тел."""
        clauses, args = ["device_sn IS ?", "key=?"], [device_sn or None, key]
        if source:
            clauses.append("source=?")
            args.append(source)
        if unhandled:
            clauses.append("unhandled=1")
        rows = self.db.execute(f"SELECT {','.join(LIST_COLUMNS)} FROM exchanges WHERE {' AND '.join(clauses)} "
                               "ORDER BY id DESC LIMIT ?", [*args, GROUP_LIMIT]).fetchall()
        return [dict(zip(LIST_COLUMNS, row)) for row in rows]

    def item(self, exchange_id):
        """Карточка обмена: заголовки, распакованные тела и кадры WS; None — нет записи."""
        row = self.db.execute(f"SELECT {','.join(DETAIL_COLUMNS)} FROM exchanges WHERE id=?",
                              (int(exchange_id),)).fetchone()
        if row is None:
            return None
        record = dict(zip(DETAIL_COLUMNS, row))
        for name in ("request_headers", "response_headers"):
            try:
                record[name] = json.loads(record.get(name) or "[]")
            except ValueError:
                record[name] = []
        for name in BLOB_COLUMNS:
            record[name] = self._decompress(record.get(name))
        record["frames"] = []
        if record.get("session"):
            for frame in self.db.execute("SELECT sequence,ts,direction,binary,size,data FROM ws_frames "
                                         "WHERE exchange_id=? ORDER BY sequence", (record["id"],)):
                record["frames"].append({"sequence": frame[0], "ts": frame[1], "direction": frame[2],
                                         "binary": bool(frame[3]), "size": frame[4],
                                         "data": self._decompress(frame[5])})
        return record

    @staticmethod
    def _decompress(blob):
        if not blob:
            return None
        try:
            return zstandard.decompress(blob)
        except zstandard.ZstdError:
            return None

    def forget(self, exchange_id=None):
        """Удалить запись (None — очистить журнал)."""
        with self.db:
            if exchange_id is None:
                self.db.execute("DELETE FROM ws_frames")
                self.db.execute("DELETE FROM exchanges")
                self.db.execute("DELETE FROM journal_groups")
            else:
                self.db.execute("DELETE FROM exchanges WHERE id=?", (int(exchange_id),))
                self._drop_empty_groups()

    def _drop_empty_groups(self):
        """Группа без записей (удалена вручную или ротацией) из списка уходит."""
        self.db.execute("DELETE FROM journal_groups WHERE NOT EXISTS (SELECT 1 FROM exchanges e "
                        "WHERE COALESCE(e.device_sn, '')=journal_groups.device_sn AND e.key=journal_groups.key)")

    # --- ротация ---------------------------------------------------------------
    def _bytes_used(self):
        """Оценка занятого места: тела и кадры + постоянная часть строки."""
        blobs = self.db.execute("SELECT COALESCE(SUM(LENGTH(request_body)+LENGTH(response_body)),0) "
                                "FROM exchanges").fetchone()[0]
        frames = self.db.execute("SELECT COALESCE(SUM(LENGTH(data)),0) FROM ws_frames").fetchone()[0]
        count = self.db.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
        return int(blobs) + int(frames) + count * 512

    def rotate(self, now=None):
        """Ротация по сроку (max_age_days) и объёму данных (max_size_mb)."""
        settings = self.settings()
        now = time.time() if now is None else now
        max_age = float(settings["max_age_days"]) * 86400
        if max_age > 0:
            self.db.execute("DELETE FROM exchanges WHERE ts < ?", (now - max_age,))
        max_size = int(float(settings["max_size_mb"]) * 1024 ** 2)
        if max_size > 0:
            while self._bytes_used() > max_size:
                oldest = [row[0] for row in self.db.execute("SELECT id FROM exchanges ORDER BY id LIMIT 10")]
                if not oldest:
                    break
                marks = ",".join("?" * len(oldest))
                self.db.execute(f"DELETE FROM exchanges WHERE id IN ({marks})", oldest)
        self._drop_empty_groups()
        self.db.commit()

    def _rotate_maybe(self):
        if time.monotonic() - self._last_age_rotation > AGE_ROTATE_EVERY_S or self._writes >= SIZE_CHECK_EVERY:
            self._writes = 0
            self._last_age_rotation = time.monotonic()
            try:
                self.rotate()
            except sqlite3.Error as exc:
                log.warning("Ротация журнала трафика не удалась: %s", exc)
