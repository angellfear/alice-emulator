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
import logging
import time
from collections import deque

import aiohttp

log = logging.getLogger(__name__)

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
        self.branch_streams = set()  # StreamId, которые ветка упоминала (TTS-потоки) — для релея
        self.branch_messages = set()  # MessageId кадров ветки — для релея Ack колонки
        self.exchange = []  # голосовой ход колонки (098): VoiceInput + аудио + StreamControl
        self.pending_streams = deque(maxlen=4)  # StreamControl перед VoiceInput (Start)
        self.exchange_open = False
        self.exchange_stream_id = None  # StreamId текущего голосового хода
        self.exchange_close_seen = False  # колонка сама закрыла поток (синтез Close не нужен)
        self._client_t = None
        self.link_ok = None  # None — ещё не открывали; True/False — результат связи
        self._ws = None
        self._session = None
        self._reader = None
        self._tap = None
        self._closed = False

    def observe(self, trigger, data):
        """Кадры голосового хода колонки — в копилку релея ветке (098): StreamControl до
        VoiceInput (Start), сам VoiceInput открывает ход, далее аудио и StreamControl
        (Close). Прочее (sync, служебные AAPI) не копится. Кадры как есть — без
        перекодировки."""
        if trigger == 'VoiceInput':
            try:
                self.exchange_stream_id = self._client_type().FromString(data[4:]).Event.Header.StreamId
            except Exception:
                self.exchange_stream_id = None
            self.exchange = list(self.pending_streams) + [data]
            self.pending_streams.clear()
            self.exchange_open = True
            self.exchange_close_seen = False
        elif trigger == 'StreamControl':
            if self.exchange_open:
                self.exchange.append(data)
                if not self.exchange_close_seen:
                    self._note_close(data)
            else:
                self.pending_streams.append(data)
        elif self.exchange_open and not data.startswith(AAPI):
            self.exchange.append(data)  # бинарный кадр аудио
            if len(self.exchange) > 6000:  # ~предел обмена: дальше колонка не говорит
                self.exchange_open = False

    def _note_close(self, data):
        """Колонка закрыла поток голосового хода сама — синтез Close не нужен."""
        try:
            sc = self._client_type().FromString(data[4:]).StreamControl
            if sc.HasField("Close") and sc.StreamId == self.exchange_stream_id:
                self.exchange_close_seen = True
        except Exception:
            pass

    def _client_type(self):
        if self._client_t is None:
            from pathlib import Path
            from ..schema import load_type
            root = Path(__file__).resolve().parent.parent
            self._client_t = load_type(root / "schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage")
        return self._client_t

    # --- журнал -------------------------------------------------------------------
    def _tap_for(self):
        if self._tap is None:
            # sn появляется после идентификации колонки: без него record_mode(None) = «off»
            # и кадры ветки не писались в журнал никогда (даже с включённой «Записью»)
            self._tap = self.runtime.upstream.ws_tap(
                "hybrid", self.host, "/uni.ws", [], handled=lambda trigger: True, sn=self.sn or None)
        return self._tap

    def tap_frame(self, direction, data):
        try:
            self._tap_for().frame(direction, True, data)
        except Exception:  # журнал не должен ломать ветку
            pass

    # --- жизненный цикл -----------------------------------------------------------
    def keep(self, raw_client_frame):
        """Сырой кадр SynchronizeStateRequest колонки — для авторизации ветки от её имени.

        Вызывающий (runtime.websocket) зовёт keep только для кадра с trigger
        'SynchronizeStateRequest' — проверять подстрокой нельзя: в protobuf-проводе нет
        имён полей, только номера тегов (дефект 097: проверка не срабатывала никогда,
        ветка открывалась без sync — анонимной сессией, и Яндекс отвечал пустой
        заглушкой без контента)."""
        if self.sync_frame is None:
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
    def _register(self, message_id):
        self.registered[message_id] = time.time()
        for mid in [m for m, ts in self.registered.items() if time.time() - ts > 120]:
            self.registered.pop(mid, None)

    async def relay_voice(self, voice_mid, stream_id):
        """Фаза 3 (098): переслать ветке накопленный голосовой ход колонки — VoiceInput,
        аудиокадры и StreamControl как есть (StreamId колонки для ветки — первый поток).
        Перед релеем ждём Close колонки (аудио может дозреть; лимит 2 с — при VAD-финале
        колонка шлёт Close позже), затем синтезируем свой Close (reason 2), если колонка
        не прислала. True — обмен ушёл, ответ ждём; хода нет — False (прежний путь)."""
        if self._closed or not self.exchange_open:
            return False
        deadline = time.monotonic() + 2.0
        while self.exchange_open and not self.exchange_close_seen and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        frames = list(self.exchange)
        self.exchange_open = False
        try:
            if self._ws is None or self._ws.closed:
                await self._open()
            if not self.exchange_close_seen:  # колонка шлёт Close после нашего endpoint — в момент релея его нет
                close = self._client_type()()
                close.StreamControl.StreamId = stream_id
                close.StreamControl.Close.Reason = 2  # STOP_RECORDING — конец аудио хода
                frames.append(b"AAPI" + close.SerializeToString())
            self._register(voice_mid)
            for raw in frames:
                await self._ws.send_bytes(raw)
                self.tap_frame("client_to_server", raw)
            self.link_ok = True
            return True
        except Exception:
            self.link_ok = False
            return False

    async def forward(self, raw_client_frame, message_id):
        """Одиночный кадр колонки наверх (колбэки плеера ветки): ленивое открытие, регистрация."""
        if self._closed:
            return False
        try:
            if self._ws is None or self._ws.closed:
                await self._open()
            self._register(message_id)
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
        if message.WhichOneof("Message") == "StreamControl":
            # поток ветки (TTS): запоминаем id — релей закрывающего кадра колонки
            self.branch_streams.add(message.StreamControl.StreamId)
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
            if message.Event.Header.MessageId:
                self.branch_messages.add(message.Event.Header.MessageId)  # Ack колонки уйдёт ветке
                if len(self.branch_messages) > 64:
                    self.branch_messages = set(sorted(self.branch_messages)[-32:])
            # колонке — только цепочка ответа (098): её голосовой ход уже отработал
            # эмулятор, протокольные кадры ветки (InputStartAck, AsrResult,
            # SpotterValidation, StreamControl ветки) продублировали бы чужие StreamId
            if event in ("AliceResponse", "DeferredAliceResponse", "TtsSpeak", "TtsTimings"):
                self.tap_frame("server_to_client", frame)
                await self.send(frame)
                return
            self.tap_frame("server_to_client", frame)
            return
        self.tap_frame("server_to_client", frame)  # служебные кадры чужих веток — только журнал

    def relay(self, message, raw):
        """Протокольные ответы колонки — ветке (в «Прозрачно» их получает сервер напрямую):
        StreamControl по потоку ветки, Ack по кадру ветки. КОПИИ — локальную обработку
        не заменяют. RequestStat НЕ релеится: колонка шлёт его по завершении хода, а в
        «Вместе» он уходил ветке сразу после заглушки — мегамайнда принимала это за конец
        хода и отменяла ещё не доставленный отложенный ответ (дефект 097, сессия 01:52)."""
        if self._closed or self._ws is None or self._ws.closed:
            return
        try:
            which = message.WhichOneof("Message")
            send = False
            if which == "StreamControl":
                send = message.StreamControl.StreamId in self.branch_streams
            elif which == "Event" and message.Event.WhichOneof("Event") == "Ack":
                send = message.Event.Ack.ReceivedHeader.MessageId in self.branch_messages
            if send:
                self.tap_frame("client_to_server", raw)
                task = asyncio.ensure_future(self._ws.send_bytes(raw))
                task.add_done_callback(lambda t: None if t.cancelled() or t.exception() is None
                                       else log.debug("Релей ветки не прошёл: %s", t.exception()))
        except Exception:  # релей не должен ломать локальную обработку
            pass
