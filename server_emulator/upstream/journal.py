"""Журнал трафика прохода наверх (TASK-043): state/traffic.sqlite3.

Пишет обмены (HTTP и WS-сессии): время, SN/DeviceId устройства, направление, хост,
метод, путь, статус, размеры, длительность и источник ответа (emulator|upstream).
Тела — сжатые zstandard с лимитом на тело; для WS — кадры с лимитом на сессию
(сверх лимита — отметка frames_truncated). Ротация по размеру файла и сроку —
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
  session TEXT, frames_truncated INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS exchanges_ts ON exchanges(ts);
CREATE INDEX IF NOT EXISTS exchanges_host ON exchanges(host);
CREATE INDEX IF NOT EXISTS exchanges_sn ON exchanges(device_sn);
CREATE TABLE IF NOT EXISTS ws_frames(
  exchange_id INTEGER NOT NULL REFERENCES exchanges(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL, ts REAL NOT NULL, direction TEXT NOT NULL,
  binary INTEGER NOT NULL, size INTEGER NOT NULL, data BLOB);
CREATE INDEX IF NOT EXISTS ws_frames_exchange ON ws_frames(exchange_id, sequence);
"""

COLUMNS = ("ts", "kind", "direction", "event", "host", "method", "path", "status", "device_sn",
           "device_id", "request_size", "response_size", "duration_ms", "source", "error",
           "close_code", "request_headers", "response_headers", "request_body", "response_body",
           "session")
LIST_COLUMNS = ("id", "ts", "kind", "direction", "event", "host", "method", "path", "status",
                "device_sn", "device_id", "request_size", "response_size", "duration_ms", "source", "error")
DETAIL_COLUMNS = LIST_COLUMNS + ("request_headers", "response_headers", "request_body", "response_body",
                                 "body_truncated", "session", "frames_truncated", "close_code")
BLOB_COLUMNS = ("request_body", "response_body")

DEFAULTS = {"max_body_kb": 256, "ws_frames": 200, "max_size_mb": 100, "max_age_days": 14}
AGE_ROTATE_EVERY_S = 3600
SECRET_HEADER = re.compile(r"authorization|cookie|token|ticket|secret|password|api[-_]?key|session[-_]?id", re.I)
MASK_VERSION = 1  # PRAGMA user_version: записи старше — с открытыми значениями, вычищаются


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
        self._scrub()
        self._queue = None
        self._worker = None
        self._closed = False
        self._sessions = {}  # session → {"id": exchange_id, "frames": записано, "truncated": bool}
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

    def frame(self, session, direction, binary, raw, sequence):
        """Кадр WS-сессии; привязывается к записи open этой сессии."""
        if self._closed:
            return
        try:
            self._ensure_worker()
        except RuntimeError:
            return
        item = {"sequence": sequence, "ts": time.time(), "direction": direction, "binary": bool(binary),
                "size": len(raw), "data": bytes(raw)}
        self._queue.put_nowait(("frame", session, item))

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
        """Элементы партии обрабатываются в порядке очереди: кадры WS идут за своим open."""
        events = []
        for item in batch:
            if item[0] == "exchange":
                self._insert(item[1])
            elif item[0] == "frame":
                self._insert_frame(item[1], item[2])
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
        columns = ",".join(row)
        cursor = self.db.execute(f"INSERT INTO exchanges({columns}) VALUES({','.join('?' * len(row))})",
                                 tuple(row.values()))
        if row.get("kind") == "ws" and row.get("session"):
            if row.get("event") == "open":
                self._sessions[row["session"]] = {"id": cursor.lastrowid, "frames": 0, "truncated": False}
            elif row.get("event") == "close":
                entry = self._sessions.pop(row["session"], None)
                if entry is not None and entry["truncated"]:
                    self.db.execute("UPDATE exchanges SET frames_truncated=1 WHERE id=?", (entry["id"],))

    def _insert_frame(self, session, item):
        entry = self._sessions.get(session)
        if entry is None or entry["truncated"]:  # лимит кадров сессии исчерпан
            return
        limit = int(self.settings()["max_body_kb"] * 1024)
        self.db.execute("INSERT INTO ws_frames(exchange_id,sequence,ts,direction,binary,size,data) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (entry["id"], item["sequence"], item["ts"], item["direction"],
                         int(item["binary"]), item["size"], zstandard.compress(item["data"][:limit])))
        entry["frames"] += 1
        if entry["frames"] >= int(self.settings()["ws_frames"]):
            entry["truncated"] = True  # отметку поставит запись close

    # --- чтение ---------------------------------------------------------------
    def list(self, device=None, host=None, source=None, limit=100, offset=0):
        """Список обменов (без тел), новые сверху; фильтры: устройство и хост — подстрока,
        источник — точное значение."""
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
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.db.execute("SELECT COUNT(*) FROM exchanges" + where, args).fetchone()[0]
        rows = self.db.execute(f"SELECT {','.join(LIST_COLUMNS)} FROM exchanges{where} "
                               "ORDER BY id DESC LIMIT ? OFFSET ?",
                               [*args, max(1, min(int(limit), 500)), max(0, int(offset))]).fetchall()
        return {"items": [dict(zip(LIST_COLUMNS, row)) for row in rows], "total": total}

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
            else:
                self.db.execute("DELETE FROM exchanges WHERE id=?", (int(exchange_id),))

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
        if max_size <= 0:
            self.db.commit()
            return
        while self._bytes_used() > max_size:
            oldest = [row[0] for row in self.db.execute("SELECT id FROM exchanges ORDER BY id LIMIT 10")]
            if not oldest:
                break
            marks = ",".join("?" * len(oldest))
            self.db.execute(f"DELETE FROM exchanges WHERE id IN ({marks})", oldest)
        self.db.commit()

    def _rotate_maybe(self):
        if time.monotonic() - self._last_age_rotation > AGE_ROTATE_EVERY_S or self._writes >= SIZE_CHECK_EVERY:
            self._writes = 0
            self._last_age_rotation = time.monotonic()
            try:
                self.rotate()
            except sqlite3.Error as exc:
                log.warning("Ротация журнала трафика не удалась: %s", exc)
