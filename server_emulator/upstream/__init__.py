"""Проход наверх к настоящим серверам Яндекса и журнал трафика (TASK-043).

До этой задачи пересылку к настоящим серверам делал внешний прокси (модуль proxy.py
исходного проекта), отдельный эмулятор умел только эмулировать. Здесь механизм:
резолвер внешнего DNS (в обход локального, с защитой от петли), пересылка HTTP,
мост WS и журнал traffic.sqlite3. Какие запросы пересылать — решают режимы
маршрута устройств (TASK-044): dispatcher.route_mode_for + upstream.target_for.
Что писать в журнал — опция устройства «Запись» (TASK-054): record_mode / recording / WsTap.
"""
from __future__ import annotations

import time

from .forward import Forwarder, UpstreamError, forward_headers  # noqa: F401 (re-export)
from .journal import TrafficJournal
from .record import WsTap  # noqa: F401 (re-export)
from .resolver import LoopError, UpstreamResolver, local_addresses  # noqa: F401 (re-export)


class Upstream:
    """Механизм прохода наверх: резолвер + пересылка + журнал."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.journal = TrafficJournal(runtime.directory / "traffic.sqlite3",
                                      config=lambda: self.settings().get("journal"))
        self.resolver = UpstreamResolver(config=self.settings)
        self.forwarder = Forwarder(self.resolver, self.journal)

    def settings(self):
        value = self.runtime.settings.get("upstream")
        return value if isinstance(value, dict) else {}

    def target_for(self, request, host=None, path=None, websocket=False):
        """URL настоящего сервера для запроса колонки (TASK-044) — см. target_for_host."""
        host = (host or request.host or "").split(":", 1)[0].lower()
        return self.target_for_host(host, path if path is not None else request.raw_path,
                                    websocket=websocket)

    def target_for_host(self, host, path, query="", websocket=False):
        """URL настоящего сервера по хосту и пути (без запроса: TASK-052 action: fetch).

        Канонический хост берётся из алиасов эмулятора (uniproxy.local →
        uniproxy.alice.yandex.net); значение алиаса может быть полным upstream
        со схемой и портом (http://127.0.0.1:9000 — тесты и кастомные upstream),
        иначе — https (wss для WS) к настоящему Яндексу.
        """
        host = (host or "").split(":", 1)[0].lower()
        mapped = str((self.runtime.settings.get("aliases") or {}).get(host) or "").strip()
        if mapped.lower().startswith(("http://", "https://")):
            base = mapped.rstrip("/")
        else:
            base = ("wss://" if websocket else "https://") + (mapped or host)
        return base + path + (("?" + query) if query else "")

    # --- опция «Запись» устройства (TASK-054) -------------------------------------
    def record_mode(self, sn):
        """off | on | misses по SN устройства; нет SN — off (неопознанное не пишется)."""
        return self.runtime.identities.record_mode(sn)

    def recording(self, sn, handled):
        """Писать ли HTTP-обмен устройства: on — всегда, misses — только без локального обработчика."""
        mode = self.record_mode(sn)
        return mode == "on" or (mode == "misses" and not handled)

    def aapi_handled(self, sn, trigger):
        """Есть ли у эмулятора обработчик AAPI-события колонки (по настройкам устройства)."""
        from .. import voice  # noqa: F401 — обработчики AAPI регистрируются при импорте
        from ..aapi_handlers import has_handler
        identities = self.runtime.identities
        device = next((d for d in identities.data["devices"] if d["sn"] == sn), None)
        return has_handler(trigger, identities.settings_for(self.runtime.settings, device))

    def ws_tap(self, source, host, path, headers=(), sn=None, device_id=None, mode=None, handled=None):
        """Запись WS-соединения: mode/handled по умолчанию читают опцию устройства и его настройки."""
        tap = WsTap(self.journal, source, mode=mode or (lambda: self.record_mode(tap.sn)),
                    handled=handled or (lambda trigger: self.aapi_handled(tap.sn, trigger)),
                    host=host, path=path, headers=headers, sn=sn, device_id=device_id)
        return tap

    def record_emulated(self, sn, device_id, handled, host, method, path, status, request_headers,
                        response_headers, request_body, response_body, started):
        """Обмен, на который ответил сам эмулятор (source=emulator): по опции устройства."""
        if not self.recording(sn, handled):
            return
        self.journal.write(kind="http", direction="exchange", source="emulator", host=host, method=method,
                           path=path, status=status, device_sn=sn, device_id=device_id,
                           request_size=len(request_body or b""), response_size=len(response_body or b""),
                           duration_ms=(time.monotonic() - started) * 1000, unhandled=not handled,
                           request_headers=request_headers, response_headers=response_headers,
                           request_body=request_body, response_body=response_body)

    async def serve(self, request, target, host=None, path=None, journal=True,
                    device_sn=None, device_id=None, unhandled=False):
        """Переслать запрос по решению маршрута. journal: HTTP — писать ли обмен (решает вызывающий
        по опции устройства), WS — bool или callable → off | on | misses (читается на каждом кадре);
        unhandled — у эмулятора нет обработчика (пометка записи и фильтр журнала)."""
        if not device_id:
            device_id = (request.headers.get("x-uprx-device-id") or request.headers.get("X-YaQuasarDeviceId")
                         or request.query.get("device_id") or "") or None
        if device_sn is None and device_id:
            device = self.runtime.identities.resolve(device_id)
            device_sn = device.get("sn") if device else None
        context = {"host": host or request.host.split(":", 1)[0],
                   "path": path or str(request.rel_url),
                   "device_id": device_id, "device_sn": device_sn}
        if request.headers.get("Upgrade", "").lower() == "websocket":
            mode = journal if callable(journal) else (lambda: "on" if journal else "off")
            tap = self.ws_tap("upstream", context["host"], context["path"], list(request.headers.items()),
                              sn=device_sn, device_id=device_id, mode=mode)
            return await self.forwarder.bridge_ws(request, target, tap=tap, **context)
        return await self.forwarder.serve(request, target, journal=bool(journal), unhandled=unhandled, **context)

    async def close(self):
        await self.journal.close()
        await self.forwarder.close()
