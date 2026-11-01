"""Журнал событий сервера «Дополнительно → Лог» (TASK-068): state/server_log.sqlite3.

Сюда попадает то, что администратору должен знать о работе сервера: ошибки HTTP и
TLS-фронта, сбои сценариев и обработчиков эндпоинтов, пересылка наверх, самообновление,
неудачный вход в панель. Хранение — 3 дня (RETENTION_DAYS) и не больше MAX_ROWS строк,
лишнее удаляется с самых старых (чистка при старте и раз в час).

Запись: log(level, source, message, device_sn=..., **detail) — в очередь, воркер пишет
в SQLite (WAL, короткие транзакции); вызов не ждёт диска. Повторы в пределах часа
склеиваются: тот же (level, source, message, device_sn) увеличивает count и last_ts,
а не создаёт строку — иначе ошибки на каждом соединении забьют журнал. Секретов нет:
detail принимает только отдельные поля (хост, путь, код, причина), заголовки и тела
сюда не передаются; logging-обработчик кладёт в detail только имя логгера.

Уровни info/warning/error; source — короткий код из SOURCES. Модульные функции —
для кода без доступа к runtime: log_event() пишет в журнал активного экземпляра
(его ставит install_logging при создании Runtime), flush() дожидается записи.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
import traceback
from pathlib import Path

log = logging.getLogger(__name__)

LEVELS = ("info", "warning", "error")
SOURCES = ("server", "tls", "http", "ws", "upstream", "quasar", "extensions", "scripts",
           "scenarios", "updates", "self_update", "auth")
RETENTION_DAYS = 3
MAX_ROWS = 100_000
GLUE_WINDOW = 3600  # секунды: повторы с тем же ключом склеиваются
MESSAGE_LIMIT = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, dedup TEXT NOT NULL,
  ts REAL NOT NULL, last_ts REAL NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL,
  message TEXT NOT NULL, device_sn TEXT, detail TEXT, count INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS entries_last_ts ON entries(last_ts);
CREATE INDEX IF NOT EXISTS entries_dedup ON entries(dedup, last_ts);
"""
LIST_COLUMNS = ("id", "ts", "last_ts", "level", "source", "message", "device_sn", "detail", "count")

# Журнал не должен расти сам: logging-обработчик ловит только WARNING+ (устанавливает
# install_logging); сервер_log пишет о себе через logging — имя логгера отфильтровано.
TARGET_LOGGERS = ("aiohttp.server", "aiohttp.access", "asyncio", "server_emulator")

_ACTIVE = None  # журнал текущего Runtime (install_logging)
_HANDLER = None  # logging-обработчик ставится один раз на процесс


def _dedup_key(level, source, device_sn, message):
    raw = "\x1f".join((level, source, device_sn or "", message))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def _clean_detail(detail):
    """**detail → JSON-словарь или None: только сериализуемые значения без пустых."""
    clean = {}
    for key, value in detail.items():
        if value is None or value == "":
            continue
        try:
            json.dumps(value)
            clean[str(key)[:60]] = value
        except (TypeError, ValueError):
            clean[str(key)[:60]] = str(value)[:300]
    return clean or None


class ServerLog:
    """SQLite-журнал сервера с асинхронной записью через очередь (схема журнала трафика)."""

    def __init__(self, path, max_rows=None):
        self.path = Path(path)
        self.max_rows = int(max_rows or MAX_ROWS)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.executescript(SCHEMA)
        self._queue = None
        self._worker = None
        self._loop = None
        self._worker_thread = None
        self._closed = False
        self._direct_lock = threading.Lock()
        self._last_rotation = time.monotonic()
        self.rotate()

    # --- запись ---------------------------------------------------------------
    def log(self, level, source, message, *, device_sn=None, ts=None, **detail):
        """Событие в журнал (не ждёт диска). ts — подмена времени (тесты, перенос старого)."""
        if self._closed:
            return
        level = level if level in LEVELS else "info"
        message = str(message or "").strip().splitlines()[0][:MESSAGE_LIMIT] if str(message or "").strip() else "(без сообщения)"
        item = {"level": level, "source": source if source in SOURCES else "server",
                "message": message, "device_sn": (str(device_sn)[:64] if device_sn else None),
                "detail": _clean_detail(detail), "ts": float(ts) if ts else time.time()}
        if self._worker is None:
            try:
                self._ensure_worker()
            except RuntimeError:  # нет цикла событий (синхронный контекст) — короткая прямая запись
                self._write_direct(item)
                return
        if threading.get_ident() == self._worker_thread:
            self._queue.put_nowait(("entry", item))
        else:
            try:  # logging-обработчик может звать из любого потока
                self._loop.call_soon_threadsafe(self._queue.put_nowait, ("entry", item))
            except RuntimeError:  # цикл уже закрыт — запись теряется (как у журнала трафика)
                pass

    def _ensure_worker(self):
        if self._worker is None and not self._closed:
            self._loop = asyncio.get_running_loop()
            self._worker_thread = threading.get_ident()
            self._queue = asyncio.Queue()
            self._worker = self._loop.create_task(self._run())

    def _write_direct(self, item):
        with self._direct_lock:
            try:
                self._insert(item)
                self.db.commit()
            except sqlite3.Error as exc:
                log.warning("Журнал сервера: запись не удалась: %s", exc)

    async def flush(self):
        """Дождаться записи всего поставленного (остановка сервера, тесты)."""
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
                log.warning("Журнал сервера: запись не удалась: %s", exc)
            if stop:
                break

    def _apply(self, batch):
        events = []
        for item in batch:
            if item[0] == "flush":
                events.append(item[1])
            elif item[0] == "entry":
                self._insert(item[1])
        self.db.commit()
        for event in events:
            event.set()
        if time.monotonic() - self._last_rotation > 3600:  # чистка старше 3 суток — раз в час
            self._last_rotation = time.monotonic()
            self.rotate()

    def _insert(self, item):
        """Вставка или склейка: тот же ключ в пределах часа — счётчик и время последнего."""
        key = _dedup_key(item["level"], item["source"], item["device_sn"], item["message"])
        glued = self.db.execute(
            "SELECT id, count FROM entries WHERE dedup=? AND last_ts >= ? ORDER BY last_ts DESC LIMIT 1",
            (key, item["ts"] - GLUE_WINDOW)).fetchone()
        if glued is not None:
            self.db.execute("UPDATE entries SET count=?, last_ts=?, detail=? WHERE id=?",
                            (glued[1] + 1, item["ts"], json.dumps(item["detail"], ensure_ascii=False)
                             if item["detail"] else None, glued[0]))
            return
        self.db.execute(
            "INSERT INTO entries(dedup, ts, last_ts, level, source, message, device_sn, detail, count) "
            "VALUES(?,?,?,?,?,?,?,?,1)",
            (key, item["ts"], item["ts"], item["level"], item["source"], item["message"],
             item["device_sn"], json.dumps(item["detail"], ensure_ascii=False) if item["detail"] else None))

    # --- чтение ---------------------------------------------------------------
    def query(self, level=None, source=None, q=None, before=None, limit=200):
        """Список записей, новые сверху (по времени последнего случая — склеенная строка поднимается).
        level — имя или имена через запятую; q — подстрока сообщения или устройства;
        before — только события с last_ts меньше метки (пагинация «Показать ещё»).
        sources — коды источников, встречающиеся в журнале (вне фильтров)."""
        clauses, args = [], []
        names = [x for x in str(level or "").split(",") if x in LEVELS]
        if names:
            clauses.append(f"level IN ({','.join('?' * len(names))})")
            args.extend(names)
        if source:
            clauses.append("source=?")
            args.append(source)
        if q:
            clauses.append("(message LIKE ? OR device_sn LIKE ?)")
            args.extend((f"%{q}%", f"%{q}%"))
        if before:
            clauses.append("last_ts < ?")
            args.append(float(before))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        size = max(1, min(int(limit), 500))
        rows = self.db.execute(f"SELECT {','.join(LIST_COLUMNS)} FROM entries{where} "
                               "ORDER BY last_ts DESC, id DESC LIMIT ?",
                               [*args, size + 1]).fetchall()
        items = []
        for row in rows[:size]:
            item = dict(zip(LIST_COLUMNS, row))
            try:
                item["detail"] = json.loads(item.get("detail") or "null")
            except ValueError:
                item["detail"] = None
            items.append(item)
        sources = [row[0] for row in self.db.execute("SELECT DISTINCT source FROM entries ORDER BY source")]
        return {"items": items, "sources": sources, "more": len(rows) > size}

    def clear(self):
        """Очистить журнал целиком (кнопка в UI)."""
        with self.db:
            self.db.execute("DELETE FROM entries")

    # --- хранение -------------------------------------------------------------
    def rotate(self, now=None):
        """Удалить записи старше 3 суток (по времени последнего случая) и лишние строки."""
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("DELETE FROM entries WHERE last_ts < ?", (now - RETENTION_DAYS * 86400,))
            extra = self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] - self.max_rows
            if extra > 0:
                self.db.execute("DELETE FROM entries WHERE id IN "
                                "(SELECT id FROM entries ORDER BY id LIMIT ?)", (extra,))
            self.db.commit()


def where(exception, limit=6):
    """Место сбоя: последние кадры трейсбека в коде эмулятора («dispatcher.py:475 serve») — без значений
    переменных, заголовков и тел; кадров эмулятора нет — последние кадры вообще (aiohttp и т. п.)."""
    if exception is None or exception.__traceback__ is None:
        return None
    frames = traceback.extract_tb(exception.__traceback__)
    own = [f for f in frames if "server_emulator" in Path(f.filename).parts] or frames
    return [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in own[-limit:]]


class JournalHandler(logging.Handler):
    """WARNING+ из aiohttp/asyncio/эмулятора → журнал (source по имени логгера).
    Сообщение — первая строка исключения; в detail — текст записи логгера (context) и место сбоя (where:
    файл:строка функция, без значений); заголовки и тела не попадают."""

    def emit(self, record):
        store = _ACTIVE
        if store is None or record.name == __name__:
            return  # свои сбои журнала — не в журнал (иначе рекурсия)
        try:
            level = "error" if record.levelno >= logging.ERROR else "warning"
            exception = record.exc_info[1] if record.exc_info else None
            if exception is not None:
                text = str(exception).strip().splitlines()[0] if str(exception).strip() else ""
                message = f"{type(exception).__name__}: {text}" if text else type(exception).__name__
            else:
                text = str(record.getMessage()).strip()
                message = text.splitlines()[0] if text else "(без сообщения)"
            context = str(record.getMessage()).strip().splitlines()[0][:200] if exception is not None and str(record.getMessage()).strip() else None
            store.log(level, logger_source(record.name), message[:MESSAGE_LIMIT], logger=record.name,
                      context=context, where=where(exception))
        except Exception:  # noqa: BLE001 — сбой логирования не должен ронять поток
            self.handleError(record)


# Имя логгера → код источника журнала.
_PREFIX_SOURCES = (("server_emulator.upstream", "upstream"), ("server_emulator.quasar", "quasar"),
                   ("server_emulator.extensions", "extensions"), ("server_emulator.endpoint_scripts", "scripts"),
                   ("server_emulator.scripting", "scripts"), ("server_emulator.scenario", "scenarios"),
                   ("server_emulator.self_update", "self_update"), ("server_emulator.updates", "updates"),
                   ("server_emulator.auth", "auth"), ("server_emulator.ws", "ws"),
                   ("server_emulator.voice", "ws"))


def logger_source(name):
    for prefix, source in _PREFIX_SOURCES:
        if name.startswith(prefix):
            return source
    return "http" if name.startswith("aiohttp") else "server"


def install_logging(store):
    """Журнал экземпляра Runtime становится активным; logging-обработчик — один на процесс."""
    global _ACTIVE, _HANDLER
    _ACTIVE = store
    if _HANDLER is None:
        _HANDLER = JournalHandler(level=logging.WARNING)
        for name in TARGET_LOGGERS:
            logging.getLogger(name).addHandler(_HANDLER)
    return store


def log_event(level, source, message, **detail):
    """Точка для кода без runtime: вход в панель, слушатели serve(). Нет журнала — мимо."""
    if _ACTIVE is not None:
        _ACTIVE.log(level, source, message, **detail)


async def flush():
    """Дождаться записи очереди активного журнала (выход сервера: записи о старте/остановке)."""
    if _ACTIVE is not None:
        await _ACTIVE.flush()
