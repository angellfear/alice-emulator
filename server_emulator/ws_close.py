"""Закрытие WS-соединений колонок в фоне (TASK-057).

aiohttp `ws.close()` ждёт ответный close клиента (до 10 с) — если обработчик соединения
занят или колонка не отвечает, вызов блокируется. Смена маршрута/привязки не должна ждать
колонку: ответ API уходит сразу, закрытие идёт отдельной задачей с коротким таймаутом, по его
истечении закрывается транспорт сокета (публичный `request.transport`) — колонка
переподключится уже по новому маршруту.
"""
from __future__ import annotations

import asyncio
import contextlib

CLOSE_TIMEOUT_S = 2.0


async def close_socket(ws, transport=None, code=1012, message=b"", timeout=None):
    """Закрыть WS: close-кадр с ожиданием ответа не дольше timeout, затем — транспорт."""
    timeout = CLOSE_TIMEOUT_S if timeout is None else timeout
    try:
        await asyncio.wait_for(ws.close(code=code, message=message), timeout)
    except Exception:  # noqa: BLE001 — таймаут, обрыв, повторное закрытие: соединение надо закрыть любой ценой
        pass
    if transport is not None and not transport.is_closing():
        with contextlib.suppress(Exception):
            transport.close()


class SocketCloser:
    """Фоновые задачи закрытия: ссылки хранятся, пока задача не завершилась."""

    def __init__(self):
        self.tasks = set()

    def close(self, entries, code, message, timeout=None):
        """entries — [(ws, transport)]; возвращает сразу, число запланированных закрытий."""
        entries = list(entries)
        for ws, transport in entries:
            task = asyncio.ensure_future(close_socket(ws, transport, code, message, timeout))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return len(entries)

    async def drain(self):
        """Дождаться закрытий (остановка сервера); каждое ограничено таймаутом."""
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
