"""Резолвер прохода наверх (TASK-043): внешний DNS в обход локального.

Колонки резолвят quasar.yandex.net на сервер эмулятора (hosts-файл или локальный
DNS), поэтому настоящий адрес хоста берётся у внешних DNS-серверов (aiodns/c-ares),
а не у системы. Поверх запроса — закреплённые адреса (settings.upstream.upstream_ip)
и кеш по TTL. Защита от петли: адрес, совпадающий с адресами собственных
интерфейсов сервера, в пересылку не идёт — LoopError с понятным текстом, иначе
запрос вернулся бы в самого себя.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time

from aiohttp.resolver import AbstractResolver, ResolveResult

log = logging.getLogger(__name__)

DEFAULT_SERVERS = ("77.88.8.8", "1.1.1.1")
DNS_TIMEOUT_S = 2.0
MAX_TTL_S = 120
STALE_TTL_S = 30  # протухший кеш на случай полного отказа DNS


class LoopError(OSError):
    """Резолв указывает на сам сервер: пересылка вернулась бы в себя."""


def is_ip(host):
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except OSError:
            continue
    return False


_local_addresses = None


def local_addresses():
    """Адреса собственных интерфейсов сервера (имя хоста + loopback) — защита от петли."""
    global _local_addresses
    if _local_addresses is None:
        found = {"127.0.0.1", "::1"}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM):
                found.add(info[4][0])
        except OSError:
            pass
        _local_addresses = frozenset(found)
    return _local_addresses


class UpstreamResolver(AbstractResolver):
    """aiohttp-резолв для коннектора пересылки: пин → кеш TTL → внешний DNS."""

    def __init__(self, config=None):
        self.config = config  # dict или callable → settings["upstream"]
        self._cache = {}  # host → (monotonic-срок, [ips])
        self._locks = {}

    def _settings(self):
        value = self.config() if callable(self.config) else self.config
        return value if isinstance(value, dict) else {}

    def _pins(self):
        value = self._settings().get("upstream_ip")
        return value if isinstance(value, dict) else {}

    def _servers(self):
        value = self._settings().get("dns_servers")
        if isinstance(value, list) and value:
            return [str(item).strip() for item in value if str(item).strip()]
        return list(DEFAULT_SERVERS)

    def invalidate(self, host=None):
        if host:
            self._cache.pop(str(host).lower().rstrip("."), None)
        else:
            self._cache.clear()

    @staticmethod
    def _result(hostname, ip, port):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return ResolveResult(hostname=hostname, host=ip, port=port, family=family,
                             proto=socket.IPPROTO_TCP, flags=0)

    def _check_loop(self, host, ip):
        if ip in local_addresses():
            raise LoopError(
                f"{host} резолвится в {ip} — это адрес самого сервера: пересылка вернулась бы "
                f"в себя. Проверьте настройку upstream.upstream_ip и DNS-серверы.")

    async def resolve(self, hostname, port=0, family=socket.AF_INET):
        host = (hostname or "").lower().rstrip(".")
        if is_ip(host):
            self._check_loop(host, host)
            return [self._result(host, host, port)]
        pin = str(self._pins().get(host) or "").strip()
        ips = [pin] if pin else await self._resolve_dns(host)
        for ip in ips:
            self._check_loop(host, ip)
        return [self._result(host, ip, port) for ip in ips]

    async def _resolve_dns(self, host):
        expiry, cached = self._cache.get(host), None
        if expiry and expiry[0] > time.monotonic():
            return expiry[1]
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            expiry = self._cache.get(host)
            if expiry and expiry[0] > time.monotonic():
                return expiry[1]
            ips, ttl = [], 0
            try:
                ips, ttl = await self._query_dns(host)
            except Exception as exc:  # noqa: BLE001 — fallback на системный DNS ниже
                log.warning("Внешний DNS (%s) не ответил для %s: %s", " ".join(self._servers()), host, exc)
            if not ips:
                try:
                    loop = asyncio.get_running_loop()
                    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                    ips = [item[4][0] for item in infos if item[0] == socket.AF_INET]
                    ttl = MAX_TTL_S
                except OSError:
                    pass
            if not ips:
                if expiry:  # протухший кеш лучше ошибки
                    self._cache[host] = (time.monotonic() + STALE_TTL_S, expiry[1])
                    return expiry[1]
                raise OSError(f"Нет A-записей для {host}")
            ttl = max(1, min(ttl or 60, MAX_TTL_S))
            self._cache[host] = (time.monotonic() + ttl, ips)
            return ips

    async def _query_dns(self, host):
        import aiodns
        resolver = aiodns.DNSResolver(nameservers=self._servers(), timeout=DNS_TIMEOUT_S)
        try:
            records = await resolver.query(host, "A")
            ips = [record.host for record in records]
            ttl = min(int(record.ttl) for record in records) if records else 0
            return ips, ttl
        finally:
            resolver.cancel()

    async def close(self):
        self._cache.clear()
