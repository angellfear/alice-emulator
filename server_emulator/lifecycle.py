"""B01: общий жизненный цикл ресурсов эмулятора и неизменяемый контекст запроса.

Lifecycle владеет разделяемым HTTP-клиентом и фоновыми задачами; close()
идемпотентен и не оставляет открытых клиентских сессий и незавершённых задач.
RequestContext — единственный неизменяемый носитель идентичности запроса;
recorder передаётся извне (встраивающий прокси — захват, standalone — None);
внешний код из ядра эмулятора не импортируется.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from aiohttp import ClientSession, ClientTimeout


@dataclass(frozen=True)
class RequestContext:
    """Неизменяемый контекст одного запроса/соединения."""

    device_id: Optional[str] = None
    device_sn: Optional[str] = None
    user_id: Optional[str] = None
    connection_id: Optional[str] = None
    recorder: object = None  # callable-интерфейс захвата прокси; None в standalone

    def record(self, payload, **kwargs):
        """payload — тела обмена; kwargs (kind/direction/event/…) — атрибуты записи захвата."""
        if self.recorder is None:
            return
        self.recorder(payload, **kwargs)


class Lifecycle:
    """Разделяемые ресурсы Runtime: HTTP-клиент и фоновые задачи."""

    def __init__(self):
        self.client: Optional[ClientSession] = None
        self.tasks: set = set()
        self.closed = False

    def client_session(self) -> ClientSession:
        if self.closed:
            raise RuntimeError("Эмулятор остановлен: клиент недоступен")
        if self.client is None or self.client.closed:
            self.client = ClientSession(timeout=ClientTimeout(total=90))
        return self.client

    def spawn(self, coroutine) -> asyncio.Task:
        if self.closed:
            raise RuntimeError("Эмулятор остановлен: новые задачи не создаются")
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def close(self):
        """Идемпотентное освобождение: задачи отменяются, клиент закрывается."""
        if self.closed:
            return
        self.closed = True
        if self.tasks:
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()
        if self.client is not None and not self.client.closed:
            await self.client.close()
        self.client = None
