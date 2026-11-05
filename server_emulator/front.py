"""Фронт TLS-порта (TASK-067): SNI решает — эмулятор или слепой туннель.

Слушатель публичного TLS-порта — сырой TCP. На каждое соединение читается
ClientHello (до 32 КиБ, без расшифровки), из него — SNI:

- имени нет, оно покрыто сертификатом сервера (DNS-SAN листового PEM, wildcard
  ровно на один уровень — как проверяет сам TLS) или это литеральный IP/.local
  (публично не резолвится) — соединение целиком передаётся внутреннему
  aiohttp-слушателю с тем же ssl_context (сплайс, накопленные байты идут
  первыми, ничего не теряется);
- иначе (домен не покрыт — например strm-хосты музыки) — по настроенному
  front_uncovered (TASK-093; режим и лист читаются живьём из server.json на каждое
  соединение, применяются без перезапуска): "tunnel" (по умолчанию) — слепой TCP-туннель
  к настоящему адресу хоста: A-записи у внешних DNS (настройка server.json
  front_dns, aiodns), порт тот же; настоящий TLS с настоящим сертификатом,
  содержимое фронту не видно. Адрес самого сервера в адреса туннеля не
  попадает (защита от петли — upstream.resolver.local_addresses).
  "allowlist" — туннель только доменам из front_allow (wildcard на один уровень —
  cert_covers), прочие отклоняются; "block" — все непокрытые отклоняются:
  соединение закрывается сразу после чтения SNI, TLS не состоялся.
- каждое непокрытое имя фиксируется в Front.seen (имя → счётчик, в памяти с запуска
  фронта) — наружу через runtime.front_observed (api/server-front, «Дополнительно → Сервер»).

Реальный адрес колонки сохраняется таблицей peers (исходящий сокет сплайса →
оригинальный пир); dispatcher.client_ip получает его через runtime.client_peer.
События — в журнал сервера (TASK-068): предупреждение о туннеле по домену
(повторы склеивает журнал, адрес — в detail), предупреждение об отклонении
непокрытого домена (режим в detail), ошибка открытия туннеля, старт
фронта. Перенесено из реализаций фронтов прежних установок (dns_names_in_cert,
cert_covers, разбор ClientHello, сплайс и помпа) и переписано под запуск
эмулятора: одна конфигурация из server.json, журнал, защиты.
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import logging

from . import server_config
from .server_log import log_event
from .upstream.resolver import local_addresses

log = logging.getLogger(__name__)

MAX_PEEK = 32768
PUMP_CHUNK = 65536
CONNECT_TIMEOUT_S = 10
IDLE_TIMEOUT_S = 300  # простой туннеля/сплайса дольше пяти минут — соединение закрыто
DEFAULT_DNS = ("8.8.8.8", "1.1.1.1")
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".localdomain")


def dns_names_in_cert(cert_path) -> tuple:
    """DNS-SAN первого (листового) PEM-сертификата файла; не читается — пусто (всё сплайсом)."""
    try:
        from cryptography import x509
        raw = open(cert_path, "rb").read()
        start = raw.find(b"-----BEGIN CERTIFICATE-----")
        end = raw.find(b"-----END CERTIFICATE-----")
        cert = x509.load_pem_x509_certificate(raw[start:end + 25])
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return tuple(v.lower() for v in sans.get_values_for_type(x509.DNSName))
    except Exception:
        log.warning("Не удалось прочитать SAN сертификата %s", cert_path, exc_info=True)
        return ()


def cert_covers(host: str, names: tuple) -> bool:
    """Покрытие имени SAN-списком (RFC 6125: wildcard ровно на один уровень)."""
    host = host.lower().strip(".")
    for name in names:
        name = name.strip(".")
        if name == host:
            return True
        if name.startswith("*.") and host.endswith(name[1:]) and "." not in host[: -len(name) + 1]:
            return True
    return False


async def peek_tls_sni(reader):
    """Буферизовать ClientHello; → (sni|None, накопленные байты)."""
    data = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            return None, data
        data += chunk
        if len(data) >= 5 and (data[0] != 0x16 or int.from_bytes(data[3:5], "big") + 5 <= len(data)):
            break
        if len(data) > MAX_PEEK:
            break
    if len(data) < 5 or data[0] != 0x16:
        return None, data
    hello = data[5:5 + int.from_bytes(data[3:5], "big")]
    try:
        if not hello or hello[0] != 0x01:
            return None, data
        p = 4 + 2 + 32  # заголовок handshake (1+3) + legacy_version + random
        p += 1 + hello[p]  # session id
        p += 2 + int.from_bytes(hello[p:p + 2], "big")  # cipher suites
        p += 1 + hello[p]  # compression methods
        p += 2  # длина списка расширений
        while p + 4 <= len(hello):
            etype = int.from_bytes(hello[p:p + 2], "big")
            length = int.from_bytes(hello[p + 2:p + 4], "big")
            body = hello[p + 4:p + 4 + length]
            p += 4 + length
            if etype != 0x0000:  # server_name
                continue
            end = 2 + int.from_bytes(body[0:2], "big") if len(body) >= 2 else 0
            q = 2
            while q + 3 <= end:
                ntype, nlen = body[q], int.from_bytes(body[q + 1:q + 3], "big")
                if ntype == 0:
                    return body[q + 3:q + 3 + nlen].decode("idna").lower(), data
                q += 3 + nlen
    except IndexError:
        pass
    return None, data


class Front:
    """Публичный TLS-порт: SNI → сплайс в aiohttp (TLS терминирует он) или туннель."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.peers = {}  # (исходящий ip, исходящий порт, внутренний порт) → оригинальный пир
        self.connections = set()
        self.tunnels = {}  # host → число туннелей (для статуса/журнала)
        self.seen = {}  # домены вне сертификата с запуска фронта: host → {"count", "first"} (TASK-093)
        self._server = None

    def client_peer(self, request):
        """Оригинальный адрес колонки по таблице (dispatcher.client_ip через runtime.client_peer)."""
        transport = request.transport
        if transport is None:
            return None
        peer = transport.get_extra_info("peername")
        local = transport.get_extra_info("sockname")
        if not peer or not local:
            return None
        return self.peers.get((peer[0], peer[1], local[1]))

    async def start(self, host, port, internal_port, cert_path=None, dns_servers=None):
        """Слушать публичный порт; sans — из листового сертификата, dns — для туннелей.
        Туннель идёт на тот же порт публичного слушателя (443 в бою)."""
        self.sans = dns_names_in_cert(cert_path) if cert_path else ()
        self.internal_port = internal_port
        self.public_port = port
        self.dns_servers = [str(item).strip() for item in (dns_servers or DEFAULT_DNS) if str(item).strip()]
        self._server = await asyncio.start_server(self._connection, host, port)
        self.runtime.front_observed = self.seen  # «Дополнительно → Сервер»: список с кнопкой «Добавить»
        self.runtime.front_sans = self.sans  # имена сертификата, которые видит фронт (для того же API)
        log_event("info", "tls", f"Фронт TLS запущен на порту {port}: имён в сертификате {len(self.sans)}",
                  sans=", ".join(self.sans[:8]))
        return self._server

    async def stop(self):
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001 — остановка не должна падать
                pass
        for writer in list(self.connections):
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        self.connections.clear()
        self.peers.clear()

    async def _connection(self, reader, writer):
        self.connections.add(writer)
        peer_key = None
        upstream_reader = upstream_writer = None
        try:
            name, buffered = await peek_tls_sni(reader)
            if name is None and not buffered.startswith(b"\x16"):
                return  # не TLS (не handshake-запись) — тихо закрыть: handshake колонки не состоялся
            # TLS без SNI — тоже эмулятор: сертификат по умолчанию (TASK-067)
            mode, allow = self._policy()
            decision = self._decision(name, mode, allow)
            if decision == "block":
                self._note_seen(name)
                log_event("warning", "tls", f"Домен {name} не в сертификате — соединение отклонено",
                          host=name, mode=mode)
                return  # TLS не состоялся; поток закроет finally
            if decision == "splice":
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    "127.0.0.1", self.internal_port, limit=PUMP_CHUNK * 4)
                outgoing = upstream_writer.get_extra_info("sockname")
                original = writer.get_extra_info("peername")
                if outgoing and original:
                    peer_key = (outgoing[0], outgoing[1], self.internal_port)
                    self.peers[peer_key] = original  # реальный адрес колонки для dispatcher.client_ip
            else:
                self._note_seen(name)
                upstream_reader, upstream_writer = await self._tunnel(name)
                if upstream_writer is None:
                    return  # причина уже в журнале
            upstream_writer.write(buffered)
            await upstream_writer.drain()
            await asyncio.gather(self._pump(reader, upstream_writer), self._pump(upstream_reader, writer))
        except (ConnectionError, asyncio.TimeoutError, OSError):
            log.debug("Фронт: соединение оборвалось", exc_info=True)
        finally:
            if peer_key is not None:
                self.peers.pop(peer_key, None)
            for stream in (writer, upstream_writer):
                if stream is None:
                    continue
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            self.connections.discard(writer)

    def _policy(self):
        """Режим и лист — живьём из server.json: применяются без перезапуска (TASK-093)."""
        config = server_config.load(self.runtime.directory)
        return (config.get("front_uncovered") or "tunnel",
                tuple(config.get("front_allow") or ()))

    def _decision(self, name, mode, allow):
        """Покрытое имя, отсутствие SNI, IP и `.local` — "splice" (как в TASK-067); прочее — по
        front_uncovered: "tunnel" | "block" ("allowlist" туннелирует только лист)."""
        if not name:
            return "splice"  # SNI нет — решает aiohttp (сертификат по умолчанию)
        try:
            ipaddress.ip_address(name)
            return "splice"  # литеральный IP публично не резолвится
        except ValueError:
            pass
        if name.endswith(LOCAL_SUFFIXES):
            return "splice"
        if not self.sans or cert_covers(name, self.sans):
            return "splice"  # сертификат не читается (всё сплайсом) или имя покрыто
        if mode == "allowlist":
            return "tunnel" if cert_covers(name, allow) else "block"
        return "block" if mode == "block" else "tunnel"

    def _note_seen(self, name):
        """Зафиксировать непокрытый домен: api/server-front покажет его с кнопкой «Добавить»."""
        entry = self.seen.setdefault(name, {"count": 0, "first": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds")})
        entry["count"] += 1

    async def _tunnel(self, name):
        """Слепой туннель к настоящему адресу: внешний DNS, защита от петли, таймауты."""
        try:
            answers = await self._resolve(name)
        except Exception as exc:  # noqa: BLE001 — DNS не ответил
            log_event("error", "tls", f"Туннель к {name} не открылся: DNS не ответил",
                      host=name, error=str(exc)[:200])
            return None, None
        ips = [ip for ip in answers if ip not in local_addresses()]  # петля: свой адрес не используется
        if not ips:
            log_event("error", "tls", f"Туннель к {name} не открылся: адресов нет (защита от петли)", host=name)
            return None, None
        for ip in ips:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, self.public_port, limit=PUMP_CHUNK * 4), CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError) as exc:
                log_event("error", "tls", f"Туннель к {name} не открылся: соединение не прошло",
                          host=name, ip=ip, error=str(exc)[:200])
                continue
            self.tunnels[name] = self.tunnels.get(name, 0) + 1
            log_event("warning", "tls", f"Домен {name} не найден в сертификате — соединение идёт туннелем",
                      host=name, ip=ip)
            return reader, writer
        return None, None

    async def _resolve(self, name):
        """A-записи у внешних DNS (aiodns); фильтр петли — в _tunnel."""
        import aiodns
        resolver = aiodns.DNSResolver(nameservers=self.dns_servers or list(DEFAULT_DNS), timeout=2)
        try:
            records = await resolver.query(name, "A")
        finally:
            resolver.cancel()
        return [record.host for record in records]

    @staticmethod
    async def _pump(reader, writer):
        """Байт-в-байт в обе стороны; простой дольше IDLE_TIMEOUT_S закрывает пару."""
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(PUMP_CHUNK), IDLE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                if writer.can_write_eof():
                    writer.write_eof()
            except Exception:  # noqa: BLE001
                pass
