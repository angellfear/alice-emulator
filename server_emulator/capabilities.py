"""Возможности колонки: сбор из её объявления, правило записи, чтение.

Колонка сама объявляет возможности в каждом запросе — Request.EnvironmentStateRaw →
TEnvironmentState.Endpoints (AnyCapabilities и Capabilities). Разбор — station_state.capabilities;
наблюдатель AAPI-событий (TextInput/VoiceInput) записывает набор в
device.state.capabilities = {ts, items: {имя: параметры}} реестра, только когда состав
изменился. Чтение — has/params/names/announced_at; своих таблиц нет: нет объявления —
возможность «не заявлена», догадок по платформе нет.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time

from . import station_state

log = logging.getLogger(__name__)

HANDLER_NAME = "station_capabilities"


def merge(stored, fresh):
    """Правило записи из журнала (объявления станции за 2026-11-11…15): колонка шлёт и
    сокращённые наборы — объявление-подмножество (33 и 67 имени при полном 68) не затирает
    хранимый полный набор; изменился состав или хранилище пусто — заменяется целиком.
    Состав сравнивается по именам: в телах возможностей болтливые State (яркость, плеер) —
    параметры переписываются только вместе с составом. Возвращает (объявление, изменилось ли)."""
    items = (stored or {}).get("items") or {}
    if set(fresh) <= set(items):
        return stored, False
    return {"ts": int(time.time()), "items": dict(fresh)}, True


def changed(stored, fresh_names):
    """Изменился ли состав набора против хранимого (быстрая проверка по именам, без
    разбора тел возможностей — наблюдатель вызывается на каждый запрос колонки)."""
    return not (set(fresh_names) <= set(((stored or {}).get("items") or {})))


def names(device):
    """Объявленные возможности устройства: отсортированный список коротких имён."""
    return sorted((((device or {}).get("state") or {}).get("capabilities") or {}).get("items") or {})


def has(device, name):
    """Объявлена ли возможность."""
    return name in names(device)


def params(device, name):
    """Параметры возможности (Meta/Parameters/State как dict); нет — {}."""
    items = (((device or {}).get("state") or {}).get("capabilities") or {}).get("items") or {}
    return dict(items.get(name) or {})


def announced_at(device):
    """Момент записи объявления (unix ts); объявления нет — 0."""
    return (((device or {}).get("state") or {}).get("capabilities") or {}).get("ts") or 0


async def observe(conversation, incoming, trigger):
    """Наблюдатель объявления: состав набора из запроса колонки изменился — записал в реестр.
    В выборе обработчика не участвует (всегда False) и колонке не отвечает. Сбой записи —
    warning, протокол не ломаем."""
    try:
        runtime = getattr(conversation, "runtime", None)
        identities = getattr(runtime, "identities", None)
        if runtime is None or identities is None:
            return False
        event = getattr(incoming.Event, str(trigger), None)
        raw = bytes(getattr(getattr(event, "Request", None), "EnvironmentStateRaw", b"") or b"")
        if not raw:
            return False
        fresh_names = station_state.capability_names(raw)
        if not fresh_names:
            return False
        sn = station_state.endpoint_sn(raw) or (getattr(conversation, "trace_context", None) or {}).get("sn") or ""
        device = next((d for d in identities.data["devices"] if d["sn"] == sn), None)
        if device is None or not changed((device.get("state") or {}).get("capabilities"), fresh_names):
            return False
        appeared = "LocalIoT" not in (((device.get("state") or {}).get("capabilities") or {}).get("items") or {})
        lock = getattr(runtime, "lock", None)
        if isinstance(lock, asyncio.Lock):
            async with lock:  # чтение-запись состояния устройства — под общим замком (как fetch_state)
                written = _write(identities, sn, raw)
        else:
            written = _write(identities, sn, raw)
        # колонка объявила LocalIoT — set_iot_user_info ей сразу, не ждать изменения
        # реестра умного дома (после перезагрузки колонки шторка не пустует). runtime
        # приходит с conversation — новой глобальной зависимости не появляется
        push = getattr(runtime, "_smart_home_push", None)
        if written and appeared and "LocalIoT" in fresh_names and push is not None:
            push(sn)
    except Exception:  # noqa: BLE001 — наблюдатель не ломает обработку запроса
        log.warning("Объявление возможностей: запись не удалась", exc_info=True)
    return False


def _write(identities, sn, raw):
    """Записать объявление в state устройства, если состав изменился (реестр перечитывается
    под замком вызывающего: снимок до ожидания замка устарает). Записано ли — True/False."""
    device = next((d for d in identities.data["devices"] if d["sn"] == sn), None)
    if device is None:
        return False
    state = copy.deepcopy(device.get("state") or {})
    fresh = station_state.capabilities(raw)
    announcement, changed = merge(state.get("capabilities"), fresh)
    if not changed:
        return False
    state["capabilities"] = announcement
    identities.mutate({"operation": "state", "sn": sn, "state": state})
    return True


def activate():
    """Зарегистрировать наблюдателя объявления (идемпотентно по имени). Наблюдатель —
    вне выбора обработчика: has_handler и счётчики событий остаются прежними."""
    from . import aapi_handlers
    for trigger in ("TextInput", "VoiceInput"):
        aapi_handlers.ensure_observer(trigger, observe, name=HANDLER_NAME,
                                      description="Объявление возможностей колонки → device.state.capabilities")
