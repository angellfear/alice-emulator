"""Маршрут «Вместе», фаза 2 (091): промах эмулятора отвечает настоящий Яндекс.

HybridSession — одна на WS колонки с маршрутом `hybrid`. Открывается лениво при первой
пересылке: первое сообщение — сохранённый сырой кадр `SynchronizeStateRequest` колонки
(в нём её OAuthToken — авторизация от её имени). Ответы Яндекса с `RefMessageId` из
зарегистрированных веткой сообщений пересылаются колонке через `send` сессии эмулятора;
бинарные кадры звука — колонке, пока играет ветка Яндекса; `Push` и прочее не пересылается
(следующий шаг — см. «Итог» 091). Кадры ветки пишутся WsTap с источником «hybrid» по опции
«Запись» устройства. Разрыв наверх — пометка «нет связи», переоткрытие при следующей пересылке.
"""
import asyncio
import time

import aiohttp

AAPI = b"AAPI"


class HybridSession:
    def __init__(self, runtime, request, send, sn=None):
        self.runtime = runtime
        from .forward import forward_headers
        self.headers = forward_headers(request.headers, websocket=True)
        self.target = runtime.upstream.target_for(request, websocket=True)
        self.host = request.host.split(":", 1)[0].lower()
        self.send = send  # отправка в WS колонки (сессия эмулятора)
        self.sn = sn
        self.sync_frame = None  # сырой кадр SynchronizeStateRequest колонки
        self.registered = {}  # message_id → ts отправки (ветка Яндекса)
        self._answered = {}  # message_id → asyncio.Event: ответ Яндекса на это сообщение
        self.yandex_player = False  # играет контент Яндекса: колбэки плеера — наверх
        self.stream_ids = set()  # потоки, открытые веткой Яндекса
        self.link_ok = None  # None — ещё не открывали; True/False — результат связи
        self._ws = None
        self._session = None
        self._reader = None
        self._tap = None
        self._closed = False

    # --- журнал -------------------------------------------------------------------
    def _tap_for(self):
        if self._tap is None:
            self._tap = self.runtime.upstream.ws_tap(
                "hybrid", self.host, "/uni.ws", [], handled=lambda trigger: True)
        return self._tap

    def tap_frame(self, direction, data):
        try:
            self._tap_for().frame(direction, True, data)
        except Exception:  # журнал не должен ломать ветку
            pass

    # --- жизненный цикл -----------------------------------------------------------
    def keep(self, raw_client_frame):
        """Сырой кадр колонки — запоминаем SynchronizeStateRequest для авторизации ветки."""
        if self.sync_frame is None and b"SynchronizeStateRequest" in raw_client_frame:
            self.sync_frame = raw_client_frame

    def link(self):
        return self.link_ok is not False

    async def close(self):
        self._closed = True
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self.link_ok = False if self.link_ok is True else self.link_ok

    # --- пересылка ----------------------------------------------------------------
    async def forward(self, raw_client_frame, message_id):
        """Кадр колонки наверх: ленивое открытие ветки, регистрация message_id."""
        if self._closed:
            return False
        self.registered[message_id] = time.time()
        for mid in [m for m, ts in self.registered.items() if time.time() - ts > 120]:
            self.registered.pop(mid, None)
        try:
            if self._ws is None or self._ws.closed:
                await self._open()
            await self._ws.send_bytes(raw_client_frame)
            self.link_ok = True
            self.tap_frame("client_to_server", raw_client_frame)
            return True
        except Exception:
            self.link_ok = False
            return False

    async def wait_response(self, message_id, timeout=10):
        """Ждать финальный AliceResponse ветки (звук колонка проигрывает из потока сама).
        False — ответа не было (отказ наверху: прежний путь)."""
        done = asyncio.Event()
        self._answered[message_id] = done
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
            await asyncio.sleep(1.0)  # хвост звука ветки уходит колонке в цикле приёма
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._answered.pop(message_id, None)

    async def _open(self):
        if self._ws is not None and not self._ws.closed:
            return
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        self._ws = await self._session.ws_connect(self.target, headers=self.headers)
        if self.sync_frame is not None:
            await self._ws.send_bytes(self.sync_frame)
            self.tap_frame("client_to_server", self.sync_frame)
        self._reader = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self):
        try:
            while not self._closed and self._ws is not None and not self._ws.closed:
                msg = await self._ws.receive()
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await self._on_frame(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # ветка не должна ронять диалог: пометка «нет связи» и переоткрытие
            import traceback
            traceback.print_exc()
        self._ws = None
        self.link_ok = False

    async def _on_frame(self, frame):
        """Кадр Яндекса: AAPI — по RefMessageId ветки (звук — пока флаг/поток ветки),
        остальное (Push, чужие ветки) — только в журнал, колонке не пересылается."""
        from pathlib import Path
        from ..schema import load_type
        root = Path(__file__).resolve().parent.parent
        server_type = load_type(root / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
        if frame[:4] != AAPI:
            await self.send(frame)  # бинарный кадр звука ветки — колонке
            return
        try:
            message = server_type.FromString(frame[4:])
        except Exception:
            return  # не разобранный AAPI-кадр — не пересылать
        event = message.Event.WhichOneof("Event")
        ref = getattr(message.Event.Header, "RefMessageId", "") or ""
        if event == "Push":
            self.tap_frame("server_to_client", frame)
            return  # Push Яндекса — следующий шаг 091 (см. «Итог»)
        if event == "AliceResponse":
            directives = [d.Name for d in message.Event.AliceResponse.Response.Directives]
            if "audio_play" in directives or "apply_player_playback_queue_directive" in directives:
                self.yandex_player = True  # в колонке играет контент Яндекса
        if ref in self.registered:
            if event == "AliceResponse":
                done = self._answered.get(ref)
                if done is not None:
                    done.set()  # финальный ответ ветки получен
            self.tap_frame("server_to_client", frame)
            await self.send(frame)
            return
        self.tap_frame("server_to_client", frame)  # служебные кадры чужих веток — только журнал
