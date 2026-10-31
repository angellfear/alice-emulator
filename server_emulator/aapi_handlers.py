"""Реестр обработчиков AAPI-событий колонки (WS).

Обработчик объявляется рядом со своим кодом декоратором @aapi_handler(trigger, …, enabled=…).
Conversation.receive перебирает обработчики события в порядке объявления, пока один не вернёт
True. has_handler отвечает, есть ли у события обработчик при данных настройках, ничего не
выполняя: опция «Запись» → «Нет обработчиков» (TASK-054). Новый обработчик виден сразу,
отдельного списка событий нет.

enabled(settings) — условие по настройкам устройства (флаги профиля, стандартные реплики).
То, что зависит от состояния соединения (открыт ли канал и т. п.), обработчик решает сам,
вернув False; для has_handler такое событие считается обработанным.
"""
from __future__ import annotations

_HANDLERS: dict[str, list] = {}  # trigger → [(handler, enabled)] в порядке объявления


def aapi_handler(*triggers, enabled=None):
    """Зарегистрировать async handler(conversation, incoming, trigger) -> bool для событий triggers."""
    def register(handler):
        for trigger in triggers:
            _HANDLERS.setdefault(trigger, []).append((handler, enabled))
        return handler
    return register


def handlers(trigger, settings):
    """Обработчики события, включённые при этих настройках, в порядке объявления."""
    return [handler for handler, enabled in _HANDLERS.get(trigger, ())
            if enabled is None or enabled(settings)]


def has_handler(trigger, settings):
    """Есть ли у события обработчик при этих настройках (без выполнения)."""
    return bool(handlers(trigger, settings))
