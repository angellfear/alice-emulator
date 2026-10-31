"""Проход наверх к настоящим серверам Яндекса и журнал трафика (TASK-043).

До этой задачи пересылку к настоящим серверам делал внешний прокси (модуль proxy.py
исходного проекта), отдельный эмулятор умел только эмулировать. Здесь механизм:
резолвер внешнего DNS (в обход локального, с защитой от петли), пересылка HTTP,
мост WS и журнал traffic.sqlite3. Какие запросы пересылать — решают режимы
маршрута устройств (TASK-044): dispatcher.route_mode_for + upstream.target_for.
"""
from __future__ import annotations

from .forward import Forwarder, UpstreamError, forward_headers  # noqa: F401 (re-export)
from .journal import TrafficJournal
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

    async def serve(self, request, target, host=None, path=None, journal=True,
                    device_sn=None, device_id=None):
        """Переслать запрос по решению маршрута; журнал — для hybrid/record."""
        if not device_id:
            device_id = (request.headers.get("x-uprx-device-id") or request.headers.get("X-YaQuasarDeviceId")
                         or request.query.get("device_id") or "") or None
        if device_sn is None and device_id:
            device = self.runtime.identities.resolve(device_id)
            device_sn = device.get("sn") if device else None
        context = {"host": host or request.host.split(":", 1)[0],
                   "path": path or str(request.rel_url),
                   "device_id": device_id, "device_sn": device_sn}
        return await self.forwarder.serve(request, target, journal=journal, **context)

    async def close(self):
        await self.journal.close()
        await self.forwarder.close()
