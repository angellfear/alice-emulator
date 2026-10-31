"""Опция «Запись» устройства (TASK-054): что из трафика попадает в журнал.

Опция устройства record = off | on | misses читается на каждом обмене и кадре — смена
действует сразу, соединение не закрывается. HTTP решает диспетчер (по результату
Dispatcher.local_handler), WS — WsTap этого модуля: он общий для эмулируемой сессии
(runtime.websocket, источник emulator) и моста наверх (Forwarder.bridge_ws, источник
upstream). Звук (бинарные кадры без префикса AAPI) не пишется — только счётчики кадров и
размера в записи закрытия сессии; AAPI-кадр — отдельная запись с именем сообщения,
MessageId/RefMessageId и телом; прочие кадры — запись «неизвестный» как есть.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import deque

from ..schema import ALICE_API, DEFAULT_SCHEMAS, load_type, schemas_ready

log = logging.getLogger(__name__)

AAPI_PREFIX = b"AAPI"
CLIENT_TYPE = "NAlice.NAliceApi.TClientMessage"
SERVER_TYPE = "NAlice.NAliceApi.TServerMessage"
C2S, S2C = "client_to_server", "server_to_client"
ARROW = {C2S: "→", S2C: "←"}
MISSED_LIMIT = 256  # MessageId событий без обработчика, ответы на которые ещё ждём
PENDING_LIMIT = 16  # кадры до опознания устройства (sync-only идентификация)


def parse_aapi(raw, direction):
    """AAPI-кадр → {name, trigger, message_id, ref_message_id}; None — не разобрать
    (нет схем, не protobuf, пустое сообщение) — такой кадр пишется как неизвестный."""
    if not schemas_ready():
        return None
    try:
        message = load_type(DEFAULT_SCHEMAS, ALICE_API, CLIENT_TYPE if direction == C2S else SERVER_TYPE) \
            .FromString(bytes(raw[len(AAPI_PREFIX):]))
        kind = message.WhichOneof("Message")
        if kind == "StreamControl":
            return {"name": "StreamControl", "trigger": "StreamControl", "message_id": None, "ref_message_id": None}
        if kind != "Event":
            return None
        trigger = message.Event.WhichOneof("Event")
        if not trigger:
            return None
        header = message.Event.Header
        return {"name": "Event." + trigger, "trigger": trigger,
                "message_id": header.MessageId or None, "ref_message_id": header.RefMessageId or None}
    except Exception:  # noqa: BLE001 — битый protobuf: кадр неизвестный
        return None


class WsTap:
    """Запись одного WS-соединения по опции устройства.

    mode() → off | on | misses (читается на каждом кадре), handled(trigger) → есть ли у
    эмулятора обработчик AAPI-события. sn/device_id уточняются по ходу соединения
    (identify): кадры до опознания держатся в небольшом буфере и пишутся, как только
    устройство известно. Неопознанное устройство (нет SN) не пишется.
    """

    def __init__(self, journal, source, mode, handled, host, path, headers=(), sn=None, device_id=None):
        self.journal, self.source = journal, source
        self.mode, self.handled = mode, handled
        self.host, self.path, self.headers = host, path, list(headers)
        self.sn, self.device_id = sn, device_id
        self.session = uuid.uuid4().hex
        self.opened = False
        self.missed = deque(maxlen=MISSED_LIMIT)
        self.audio = {C2S: [0, 0], S2C: [0, 0]}  # направление → [кадров, байт]
        self.pending = deque(maxlen=PENDING_LIMIT)

    def _write(self, **fields):
        self.journal.write(kind="ws", source=self.source, host=self.host, path=self.path, session=self.session,
                           device_sn=self.sn, device_id=self.device_id, **fields)

    def start(self):
        self._open_if_needed()

    def identify(self, sn, device_id):
        """Идентичность соединения уточнилась: накопленные кадры пишутся."""
        self.sn, self.device_id = sn or self.sn, device_id or self.device_id
        self._open_if_needed()
        pending, self.pending = list(self.pending), deque(maxlen=PENDING_LIMIT)
        for ts, direction, binary, raw in pending:
            self._record(direction, binary, raw, ts)

    def _open_if_needed(self):
        if self.opened or not self.sn or self.mode() != "on":
            return
        self.opened = True
        self._write(direction="lifecycle", event="open", status=101, key="ws open", request_headers=self.headers)

    def frame(self, direction, binary, raw):
        """Кадр соединения. Звук — только счётчики; остальное — по опции устройства."""
        raw = bytes(raw)
        if binary and not raw.startswith(AAPI_PREFIX):
            counter = self.audio[direction]
            counter[0] += 1
            counter[1] += len(raw)
            return
        if not self.sn:
            self.pending.append((time.time(), direction, binary, raw))
            return
        self._record(direction, binary, raw, None)

    def _record(self, direction, binary, raw, ts):
        try:
            mode = self.mode()
            if mode == "off":
                return
            info = parse_aapi(raw, direction) if binary else None
            extra = {"ts": ts} if ts else {}
            body = {"request_body": raw, "request_size": len(raw)} if direction == C2S \
                else {"response_body": raw, "response_size": len(raw)}
            if info is None:  # неизвестный кадр — как есть, только при полной записи
                if mode == "on":
                    self._open_if_needed()
                    self._write(direction=direction, event="unknown", key=f"ws unknown {ARROW[direction]}",
                                **body, **extra)
                return
            if direction == C2S:
                unhandled = not self.handled(info["trigger"])
                if unhandled and info["message_id"]:
                    self.missed.append(info["message_id"])
            else:
                unhandled = bool(info["ref_message_id"]) and info["ref_message_id"] in self.missed
            if mode == "misses" and not unhandled:
                return
            if mode == "on":
                self._open_if_needed()
            self._write(direction=direction, event="aapi", key=f"ws {ARROW[direction]} {info['name']}",
                        message_id=info["message_id"], ref_message_id=info["ref_message_id"],
                        unhandled=unhandled, **body, **extra)
        except Exception as exc:  # noqa: BLE001 — журнал не должен ронять соединение
            log.warning("Журнал трафика: кадр WS не записан: %s", exc)

    def close(self, close_code=None):
        """Закрытие сессии — запись lifecycle (только при полной записи) со счётчиками звука."""
        if not self.sn or self.mode() != "on":
            return
        self._open_if_needed()
        self._write(direction="lifecycle", event="close", status=0, close_code=close_code, key="ws close",
                    audio_in_frames=self.audio[C2S][0], audio_in_bytes=self.audio[C2S][1],
                    audio_out_frames=self.audio[S2C][0], audio_out_bytes=self.audio[S2C][1])
