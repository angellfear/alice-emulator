"""Per-connection AAPI conversation and bounded buffered voice pipeline."""

import asyncio

import datetime

import json

import re

import time

import uuid



from .agent import engine as agent_engine

from . import async_reply
from .audio_buffer import (AudioBuffer, InputLimit, MAX_INPUT_BYTES, MAX_INPUT_SECONDS,
                            MAX_INPUT_STREAMS, collect_pages, input_audio, ogg_pages,
                            pcm_opus_pages, streaming_source, synthesize, synthesize_cached,
                            write_frames)
from .spoken import OPEN_TTS_WAIT_S, speakable

from . import voice_response

from .voice_response import failure_phrases, failure_speech, failure_text

from .speech_trace import SpeechTrace, reason_name

from . import aapi_handlers

from .aapi_handlers import aapi_handler

from . import station_state

from .legacy_replay import CONTROL_REPLIES, control_response








def _struct_member(payload, key):

    """Вложенный Struct из google.protobuf.Struct (Payload события) или None."""

    value = payload.fields.get(key) if payload is not None else None

    return value.struct_value if value is not None and value.WhichOneof('kind') == 'struct_value' else None





def _string_member(struct, key):

    """Строка из google.protobuf.Struct или '' (ключ отсутствует/не строка)."""

    value = struct.fields.get(key) if struct is not None else None

    return value.string_value if value is not None and value.WhichOneof('kind') == 'string_value' else ''





def _bool_member(struct, key):

    """bool из google.protobuf.Struct или False (ключ отсутствует/не bool)."""

    value = struct.fields.get(key) if struct is not None else None

    return value.bool_value if value is not None and value.WhichOneof('kind') == 'bool_value' else False









def _typed_callback_type(payload):

    """Тип typed-колбэка: Payload.typed_callback_serialized — base64 google.protobuf.Any;
    type_url без префикса → «NAlice.TGetInformersTypedCallback» (или '')."""

    raw = _string_member(payload, 'typed_callback_serialized')

    if not raw:

        return ''

    try:

        import base64

        from google.protobuf import any_pb2

        message = any_pb2.Any.FromString(base64.b64decode(raw))

    except Exception:

        return ''

    return message.type_url.rsplit('/', 1)[-1]





def _semantic_frame_name(payload):

    """Имя семантического фрейма: первый ключ Payload.typed_semantic_frame (или '')."""

    frame = _struct_member(payload, 'typed_semantic_frame')

    return next(iter(frame.fields), '') if frame is not None else ''



def _printable_preview(raw, limit=400):
    """Читаемые строки из сырого дампа возможности (protobuf без схемы): состояние экрана
    и плеера колонка присылает текстом внутри структуры — строка для контекста устройства."""
    strings = re.findall(r'[A-Za-zА-Яа-яЁё0-9 ._/:()+\-,—]{4,}', (raw or b'').decode('utf-8', 'ignore'))
    return ' | '.join(dict.fromkeys(string.strip() for string in strings))[:limit]





def service_event_fields(request, event_obj):

    """Данные служебного server_action TextInput для контекста JSON-сценариев.



    Это не фраза пользователя (Text пустой, микрофоны ни при чём): цель колбэка —

    Payload.analytics.purpose (sync_timezone/get_informers/…), флаг тишины —

    Payload.request_params.disable_output_speech, таймзона устройства —

    Application.Timezone. Из них сценарий (protocol_event-триггер) собирает

    тихий ответ облака вместо устной заглушки.

    """

    analytics = _struct_member(event_obj.Payload, 'analytics')

    params = _struct_member(event_obj.Payload, 'request_params')

    return {'event_name': getattr(event_obj, 'Name', '') or '',
            'callback': _typed_callback_type(event_obj.Payload),
            'frame': _semantic_frame_name(event_obj.Payload),

            'purpose': _string_member(analytics, 'purpose'),

            'disable_output_speech': _bool_member(params, 'disable_output_speech'),

            'timezone': getattr(getattr(request, 'Application', None), 'Timezone', '') or ''}





# Event.Type = server_action (6): служебное событие приложения станции, не фраза.

SERVER_ACTION_EVENT_TYPE = 6





def is_service_text_input(incoming):
    """TextInput — служебный server_action (Type=6 с именем события): браузерные
    колбэки Android TV-приложения (@@mm_typed_callback и пр.), не речь."""

    event_obj = (
        getattr(getattr(incoming.Event, 'TextInput', None).Request, 'Event', None)
        if incoming.WhichOneof('Message') == 'Event' and incoming.Event.WhichOneof('Event') == 'TextInput'
        else None
    )

    if event_obj is None:
        return False

    return getattr(event_obj, 'Type', 0) == SERVER_ACTION_EVENT_TYPE and bool(getattr(event_obj, 'Name', ''))





def _unclaimed_reply(settings):

    """Условие обработчика стандартной реплики «не привязано»: включена и задан текст."""

    from .behavior import standard_for

    return bool(settings.get('unclaimed') and standard_for(settings.get('standard_replies'), 'unclaimed'))


class Conversation:

    def __init__(self, runtime, server_type, send, trace_context=None, delivered=None):

        self.runtime, self.server_type, self.send = runtime, server_type, send

        self.delivered = delivered  # delivered(message_id): колонка подтвердила доставку (Ack)

        self.audio = AudioBuffer()  # входные аудиоканалы: буферы и лимиты (audio_buffer.py)

        self.completed = set()

        self.task = None

        self.history = []

        self.next_output_channel = 2

        self.output_channel = None

        # v9 §2: структурная трасса; контекст (session/sn) живёт с соединением

        self.trace_context = trace_context if trace_context is not None else {}

        self.trace_by_channel = {}

        # TASK-069: биометрия говорящего и состояние возможностей колонки (сессия устройства)

        self.speaker = None

        self.capabilities = {}



    @property

    def channels(self):

        """Входные аудиоканалы: словари состояния AudioBuffer; вызывающий дополняет
        state своими ключами (incoming, trace, queue, asr_task)."""

        return self.audio.channels



    @property

    def thread(self):

        """Тред агента: колонка (SN) — история переживает переподключение; без SN — соединение."""

        sn = self.trace_context.get('sn')

        return 'voice:' + sn if sn else 'voice:session:' + str(self.trace_context.get('session') or id(self))



    def new_trace(self, run_id=None):

        sink = getattr(self.runtime, 'speech_sink', None)

        return SpeechTrace(sink, self.trace_context, run_id) if sink else None



    def event(self, incoming, kind, channel=None):

        output = self.server_type()

        output.Event.Header.MessageId = uuid.uuid4().hex

        output.Event.Header.RefMessageId = incoming.Event.Header.MessageId

        if channel is not None:

            output.Event.Header.StreamId = channel

        getattr(output.Event, kind).SetInParent()

        return output



    async def message(self, message):

        await self.send(b'AAPI'+message.SerializeToString())



    async def close_stream(self, channel, reason=0, trace=None):

        if trace is None:

            trace = self.trace_by_channel.get(channel)

        state = self.channels.get(channel)

        if state and state.get('asr_task'):

            state['asr_task'].cancel()

            await asyncio.gather(state['asr_task'], return_exceptions=True)

        output = self.server_type()

        output.StreamControl.StreamId = channel

        output.StreamControl.Close.Reason = reason

        if trace:

            trace.event('stream_close', 's2c', channel=channel, reason=reason_name(reason))

        await self.message(output)



    async def cancel(self):

        if self.task and not self.task.done():

            self.task.cancel()

            await asyncio.gather(self.task, return_exceptions=True)

        self.task = None

        if self.output_channel is not None:

            channel, self.output_channel = self.output_channel, None

            await self.close_stream(channel, 4)



    async def shutdown(self):

        if self.task:

            self.task.cancel()

            await asyncio.gather(self.task, return_exceptions=True)

        for state in self.channels.values():

            if state.get('asr_task'):

                state['asr_task'].cancel()

                await asyncio.gather(state['asr_task'], return_exceptions=True)

        self.channels.clear()



    async def receive_audio(self, data):

        if len(data) < 4:

            return

        channel = int.from_bytes(data[:4], 'big')

        state = self.channels.get(channel)

        if state is None:

            return

        if state.get('await_spotter'):

            # Wake-word audio is a separate logical Opus stream; ASR starts after SpotterEnd.

            return

        trace = state.get('trace')

        if trace:

            trace.bump('audio_bytes', len(data) - 4)

            trace.bump('audio_chunks')

        try:

            self.audio.push(channel, data[4:], buffered=not state.get('queue'))

        except InputLimit:

            if trace:

                trace.event('limit_exceeded', 'server', cause='duration_or_size',

                            **trace.snapshot())

            await self.close_stream(channel, 1)

            self.channels.pop(channel, None)

            raise ValueError('Превышен лимит длительности или размера входного аудио')

        if state.get('queue'):

            if state['asr_task'].done():

                await state['asr_task']

            await asyncio.wait_for(state['queue'].put(bytes(data[4:])), timeout=5)



    async def receive(self, incoming, trigger):

        """AAPI-событие колонки: обработчики реестра aapi_handlers в порядке объявления,
        первый вернувший True его обработал. False — обработчика нет («Без ответа»)."""

        aapi_handlers.count(trigger)

        aapi_handlers.notify_observers(self, incoming, trigger)  # TASK-059: наблюдатели фоном

        for handler in aapi_handlers.handlers(trigger, self.runtime.settings):

            if await handler(self, incoming, trigger):

                aapi_handlers.count(trigger, handled=True)

                return True

        return False



    # Стандартные ответы обслуживаются даже при выключенных AI-флагах:
    # непривязанное устройство получает реплику вместо тишины и Vins-таймаута.
    @aapi_handler('TextInput', 'VoiceInput', enabled=_unclaimed_reply)

    async def _on_unclaimed(self, incoming, trigger):

        cfg = self.runtime.settings


        from .behavior import standard_for

        canned = standard_for(cfg.get('standard_replies'), 'unclaimed')

        if canned:

            await self.cancel()

            for channel in list(self.channels):

                await self.close_stream(channel, 4)

            self.channels.clear()

            await self.message(self.event(incoming, 'InputStartAck'))

            trace = self.new_trace()

            request_id = getattr(getattr(incoming.Event, trigger).Header, 'RequestId', '') or ''

            if trace:

                trace.event('standard_reply', 'server', case='unclaimed', request_id=request_id)

            self.task = self.runtime.lifecycle.spawn(self.spoken_reply(

                incoming, canned['text'], trace=trace, request_id=request_id,

                force_tts=True, event_name='standard_reply'))

            return True

        return False



    @aapi_handler('CancelRequest')

    async def _on_cancel(self, incoming, trigger):


        trace = self.new_trace()

        if trace:

            trace.event('cancel', 'c2s', cause='CancelRequest')

        await self.cancel()

        for channel in list(self.channels):

            await self.close_stream(channel, 4)

        self.channels.clear()

        return True



    PLAYER_CALLBACKS = ('music_thin_client_on_started', 'music_thin_client_on_stopped',
                        'music_thin_client_on_finished', 'music_thin_client_on_failed',
                        'music_thin_client_on_recovery', '@@mm_stack_engine_get_next',
                        '@@mm_typed_callback')

    async def _hybrid_forward(self, incoming, request, text, hybrid):
        """«Вместе» (091): фраза наверх TextInput-ом с тем же Header (MessageId/RequestId),
        чтобы ответ Яндекса лёг на ожидание колонки. True — переслано, ждём ответ."""
        import copy as _copy
        frame = _copy.deepcopy(incoming)
        ev = frame.Event.TextInput.Request.Event
        ev.Type = 2  # text_input
        ev.Text = text
        ev.EndOfUtterance = True
        raw = b'AAPI' + frame.SerializeToString()
        message_id = getattr(frame.Event.Header, 'MessageId', '') or ''
        if not await hybrid.forward(raw, message_id):
            self.runtime.recent = (self.runtime.recent + [
                {"kind": "hybrid", "error": "Вместе: Яндекс недоступен — фраза ушла прежнему пути"}])[-30:]
            return False
        answered = await hybrid.wait_response(message_id, timeout=10)  # ответ ветки шлёт колонке сам _on_frame
        if not answered:
            self.runtime.recent = (self.runtime.recent + [
                {"kind": "hybrid", "error": "Вместе: Яндекс не ответил вовремя — фраза ушла прежнему пути"}])[-30:]
            return False
        return True

    @aapi_handler('TextInput', enabled=lambda settings: bool(settings.get('aapi_model')))

    async def _on_text_input(self, incoming, trigger):


        # «Вместе» (091): колбэки контента Яндекса — наверх, пока играет ветка Яндекса
        hybrid = getattr(self.runtime, 'hybrid_session', None)
        mode_fn = getattr(self.runtime, 'hybrid_mode', None)
        name = incoming.Event.TextInput.Request.Event.Name
        if (hybrid is not None and mode_fn is not None and mode_fn() == 'hybrid'
                and getattr(hybrid, 'yandex_player', False) and name in self.PLAYER_CALLBACKS):
            await hybrid.forward(b'AAPI' + incoming.SerializeToString(),
                                 getattr(incoming.Event.Header, 'MessageId', '') or '')
            return

        await self.cancel()

        for channel in list(self.channels):

            await self.close_stream(channel, 4)

        self.channels.clear()

        # InputStartAck обязателен и для служебного server_action: без него

        # станция не принимает ответ (ретрай-цикл колбэка не завершается).

        # Реальное облако шлёт его так же (архив 2026-10-21 c2809aa8…).

        await self.message(self.event(incoming, 'InputStartAck'))

        trace = self.new_trace()

        if trace:

            trace.event('run_start', 'server', kind='TextInput', request_id=incoming.Event.TextInput.Header.RequestId)

        self.task = self.runtime.lifecycle.spawn(self.run(incoming, 'TextInput', trace=trace))

        return True



    @aapi_handler('VoiceInput', enabled=lambda settings: bool(settings.get('aapi_voice')))

    async def _on_voice_input(self, incoming, trigger):


        await self.cancel()

        request_id = incoming.Event.VoiceInput.Header.RequestId

        for old_channel, state in list(self.channels.items()):

            if state['incoming'].Event.VoiceInput.Header.RequestId != request_id:

                await self.close_stream(old_channel, 4)

                self.channels.pop(old_channel, None)

        channel = incoming.Event.Header.StreamId

        if not incoming.Event.Header.HasField('StreamId') or channel == 0:

            raise ValueError('VoiceInput не объявляет аудиоканал')

        if len(self.channels) >= MAX_INPUT_STREAMS and channel not in self.channels:

            raise ValueError('Слишком много входных аудиоканалов')

        if channel in self.channels:

            await self.close_stream(channel, 4)

        trace = self.new_trace()

        state = self.audio.open(channel)

        state.update(incoming=incoming, trace=trace,

                     await_spotter=bool(incoming.Event.VoiceInput.EnableSpotterValidation))

        if trace:

            self.trace_by_channel[channel] = trace

            trace.event('voice_input_start', 'c2s', channel=channel, request_id=request_id or incoming.Event.Header.MessageId,

                        format=incoming.Event.VoiceInput.Format,

                        spotter_validation=bool(incoming.Event.VoiceInput.EnableSpotterValidation))

        if self.runtime.asr_streaming():

            queue, chunks = streaming_source()

            state = self.channels[channel]

            state['queue'] = queue

            async def partial(text, request=incoming):

                if trace:

                    trace.asr_partial(text)

                await self.recognition(request, text, final=False)

            async def recognize():

                result = await self.runtime.recognize_stream(chunks(), incoming.Event.VoiceInput.Format, partial)

                if self.channels.get(channel) is state:

                    # Real devices wait for server endpoint/STOP_RECORDING, not client Close.

                    if trace:

                        trace.event('endpoint', 'server', decision='STOP_RECORDING')

                    self.channels.pop(channel, None)

                    await self.close_stream(channel, 2)

                    key = incoming.Event.VoiceInput.Header.RequestId or incoming.Event.Header.MessageId

                    if key not in self.completed:

                        self.completed.add(key)

                        self.task = self.runtime.lifecycle.spawn(self.run(incoming,'VoiceInput',b'\0',state['asr_task'],trace=trace))

                return result

            state['asr_task'] = self.runtime.lifecycle.spawn(recognize())

        await self.message(self.event(incoming, 'InputStartAck'))

        return True



    @aapi_handler('StreamControl')

    async def _on_spotter_end(self, incoming, trigger):

        if incoming.StreamControl.WhichOneof('Action') != 'SpotterEnd':

            return False


        channel = incoming.StreamControl.StreamId

        state = self.channels.get(channel)

        if state:

            if state['incoming'].Event.VoiceInput.EnableSpotterValidation:

                reply = self.event(state['incoming'],'SpotterValidation')

                reply.Event.SpotterValidation.Result = 1

                reply.Event.SpotterValidation.CanceledCauseOfMultiactivation = False

                await self.message(reply)

            state['await_spotter'] = False

            if state.get('trace'):

                state['trace'].event('spotter_end', 'c2s', channel=channel)

        return True



    @aapi_handler('StreamControl')

    async def _on_stream_close(self, incoming, trigger):

        if incoming.StreamControl.WhichOneof('Action') != 'Close':

            return False


        channel = incoming.StreamControl.StreamId

        state = self.channels.pop(channel, None)

        if state is None:

            return False

        trace = state.get('trace')

        if trace:

            trace.event('client_close', 'c2s', channel=channel,

                        reason=reason_name(incoming.StreamControl.Close.Reason), **trace.snapshot())

        if self.audio.too_long(state):

            if state.get('asr_task'):

                state['asr_task'].cancel()

                await asyncio.gather(state['asr_task'], return_exceptions=True)

            await self.close_stream(channel, 1)

            return True

        reason_code = incoming.StreamControl.Close.Reason

        if reason_code == 5 or not state['size']:

            # Реальный wire (capture): колонка закрыла микрофон NO_SPEECH → сервер

            # досылает финальный пустой AsrResult, закрывает канал OK и отвечает репликой.

            cause = reason_name(reason_code) if reason_code else 'no_audio'

            if trace:

                trace.event('asr_abandoned', 'server', cause=cause)

            if state.get('asr_task'):

                state['asr_task'].cancel()

                await asyncio.gather(state['asr_task'], return_exceptions=True)

            await self.recognition(state['incoming'], '', final=True)

            await self.close_stream(channel, 0, trace)

            self.task = self.runtime.lifecycle.spawn(self.retry_prompt(state['incoming'], trace))

            return True

        if reason_code in (1, 4):

            if trace:

                trace.event('asr_abandoned', 'server', cause=reason_name(reason_code))

            if state.get('asr_task'):

                state['asr_task'].cancel()

                await asyncio.gather(state['asr_task'], return_exceptions=True)

            return True

        request_id = state['incoming'].Event.VoiceInput.Header.RequestId or state['incoming'].Event.Header.MessageId

        if request_id in self.completed:

            if state.get('asr_task'):

                state['asr_task'].cancel()

                await asyncio.gather(state['asr_task'], return_exceptions=True)

            return True

        self.completed.add(request_id)

        if len(self.completed) > 128:

            self.completed = {request_id}

        await self.cancel()

        if state.get('queue'):

            await asyncio.wait_for(state['queue'].put(None), timeout=5)

        self.task = self.runtime.lifecycle.spawn(self.run(state['incoming'], 'VoiceInput', b'\0' if state.get('asr_task') else bytes(state['audio']), state.get('asr_task'), trace=trace))

        return True



    @aapi_handler('Ack')

    async def _on_ack(self, incoming, trigger):

        """Подтверждение доставки сообщения сервера: отметка в журнале действий (delivered)."""

        if self.delivered is not None:

            self.delivered(incoming.Event.Ack.ReceivedHeader.MessageId)

        return True



    @aapi_handler('LogSpotter')

    async def _on_log_spotter(self, incoming, trigger):

        """Лог споттера колонки: ответ LogAck."""

        await self.message(self.event(incoming, 'LogAck'))

        return True



    # TASK-069: служебные события состояния колонки. Облако на них не отвечает (образец
    # журнала 2026-11-01: после MatchedUser/CapabilityUpdateState нет сообщений с их
    # MessageId в RefMessageId) — данные сохраняются, события считаются обработанными.

    def _session_record(self):
        """Запись сессии этого соединения в runtime.sessions (нет — None)."""
        sessions = getattr(self.runtime, 'sessions', None)
        return (sessions or {}).get(self.trace_context.get('session')) if sessions is not None else None

    @aapi_handler('MatchedUser')

    async def _on_matched_user(self, incoming, trigger):
        """Биометрия говорящего: колонка распознала голос (взрослый/ребёнок, пол) к текущему
        запросу. Оценки — в контексте разговора и сессии устройства; ответа нет."""
        classification = incoming.Event.MatchedUser.Request.Event.BiometryClassification
        speaker = {'scores': {score.ClassName: round(score.Confidence, 3) for score in classification.Scores
                              if score.ClassName},
                   'simple': {item.Tag: item.ClassName for item in classification.Simple
                              if item.Tag and item.ClassName},
                   'ts': time.time()}
        self.speaker = speaker
        session = self._session_record()
        if session is not None:
            session['speaker'] = speaker
        return True

    @aapi_handler('CapabilityUpdateState')

    async def _on_capability_update_state(self, incoming, trigger):
        """Состояние возможностей колонки (экран, плеер, громкость…). Последнее состояние
        каждого типа — в сессии устройства (память, без записи в БД) и в контексте устройства."""
        event = incoming.Event.CapabilityUpdateState
        raw = bytes(event.AnyCapability.RawCapability) if event.AnyCapability.RawCapability \
            else bytes(event.Capability.value)
        descriptor = event.DESCRIPTOR.fields_by_name['CapabilityType']
        number = event.CapabilityType or event.AnyCapability.CapabilityType
        name = (descriptor.enum_type.values_by_number.get(number)
                or descriptor.enum_type.values_by_number[0]).name
        entry = {'ts': time.time(), 'bytes': len(raw), 'preview': _printable_preview(raw)}
        self.capabilities[name] = entry
        session = self._session_record()
        if session is not None:
            session.setdefault('capabilities', {})[name] = dict(entry, raw=raw)
        return True

    @aapi_handler('ContinueRequest')

    async def _on_continue_request(self, incoming, trigger):
        """Колонка просит продолжить ответ (RequestMessageId). Поведение облака не разобрано
        (образца в журнале нет): ответа нет, событие — в журнал сервера до разбора."""
        self.runtime.server_log.log('warning', 'ws',
                                    'ContinueRequest получен — поведение не разобрано, ответа нет (нужен образец)',
                                    device_sn=self.trace_context.get('sn') or None,
                                    request_message_id=incoming.Event.ContinueRequest.RequestMessageId)
        return True



    @aapi_handler(*CONTROL_REPLIES)

    async def _on_control(self, incoming, trigger):

        """Служебные события с каркасным ответом облака (legacy_replay.control_response)."""

        reply = control_response(b'AAPI' + incoming.SerializeToString())

        if reply is None:

            return False

        await self.send(reply)

        return True



    async def recognition(self, incoming, text, final):

        if not final and not (text or '').strip():

            return  # пустые паршлы не отправляются: колонка не должна мигать пустым текстом

        asr = self.event(incoming, 'AsrResult')

        voice_response.fill_asr_result(asr.Event, text, final)

        await self.message(asr)



    async def retry_prompt(self, incoming, trace=None):

        """NO_SPEECH по реальному wire: сервер отвечает репликой-повтором (не молчит)."""

        await self.spoken_reply(incoming, 'Извините, я не расслышала. Скажите ещё раз.',

                                trace=trace, event_name='no_speech_retry')



    async def _synthesize_speech(self, text, wait=None):
        return await synthesize(self.runtime, text, wait)

    async def _synthesize_cached(self, parts, wait=None):
        return await synthesize_cached(self.runtime, parts, wait)

    async def _stream_tts_channel(self, incoming, pages, speech, trace=None):

        """TtsSpeak + аудио-канал + close — проводка озвучки (общая для путей)."""

        channel = self.next_output_channel

        self.next_output_channel += 2

        self.output_channel = channel

        if trace:

            self.trace_by_channel[channel] = trace

        speak = self.event(incoming, 'TtsSpeak', channel)

        speak.Event.TtsSpeak.Format = 'audio/opus'

        await self.message(speak)

        sent_bytes = await write_frames(self.send, channel, pages, speech)

        await self.close_stream(channel, 0, trace)

        if trace:

            trace.event('tts_audio', 's2c', channel=channel, pages=len(pages), bytes=sent_bytes)

        self.output_channel = None



    async def speak_audio(self, incoming, text, trace=None):

        """Озвучка уже отправленной реплики: TtsSpeak + аудиоканал + close.



        Теги <speaker …>/<[pause …]> в синтез не идут — провайдер читает их

        буквально. Если ответ ещё не отправлен, поднимайте HasVoiceResponse

        перед отправкой (как облако) — иначе станция ждёт голоса зря."""

        if not self.runtime.settings.get('aapi_tts'):

            return

        plain = re.sub(r'<[^>]+>', '', text or '').strip()

        if not plain:

            return

        speech = None

        try:

            if trace:  # TASK-088: начался синтез — ход в состоянии «отвечает» (строка шапки, api/live)

                trace.event('tts_start', 'server')

            pages, speech = await self._synthesize_speech(plain)

            if trace:

                trace.event('tts_done', 'server', first_page_bytes=len(pages[0]) if pages else 0)

            await self._stream_tts_channel(incoming, pages, speech, trace)

        except Exception as exc:

            if trace:

                trace.event('tts_failed', 'server', error=type(exc).__name__)

            self.runtime.recent = (self.runtime.recent + [{'kind': 'tts', 'error': type(exc).__name__ + ': ' + str(exc)[:120]}])[-30:]

        finally:

            if speech:

                await speech.aclose()



    async def spoken_reply(self, incoming, text, trace=None, request_id='', force_tts=False,
                           event_name='spoken_reply', speech_text=None):
        """Готовая реплика без модели: карточка AliceResponse + озвучка тем же конвейером
        (сборка — voice_response.spoken_reply; speech_text — что сказать, если голос
        отличается от текста карточки)."""
        return await voice_response.spoken_reply(self, incoming, text, trace=trace, request_id=request_id,
                                                 force_tts=force_tts, event_name=event_name,
                                                 speech_text=speech_text)


    async def _chat_or_defer(self, chat, incoming, request, text, after, trace=None):
        """Ответ модели, если он пришёл за after секунд; иначе — короткий ответ сразу
        (колонка закрывает диалог), модель дорабатывает в фоне, ответ доставит Push. None — отложен."""
        task = asyncio.ensure_future(chat)
        try:
            done, _ = await asyncio.wait({task}, timeout=after)
        except asyncio.CancelledError:  # колонка отменила запрос — модель не нужна
            task.cancel()
            raise
        if done:
            return task.result()
        if trace:
            trace.event('async_wait', 'server', after_s=after)
        self.runtime.lifecycle.spawn(self._deliver_later(task, text, trace))
        await self.spoken_reply(incoming, async_reply.wait_text(self.runtime), trace=trace,
                                request_id=getattr(request.Header, 'RequestId', '') or '', event_name='async_wait_reply')
        return None

    async def _deliver_later(self, task, text, trace=None):
        """Модель ответила (или упала) — ответ на хранение и Push колонке: она сама его запросит."""
        try:
            answer = await task
        except Exception as exc:
            self.runtime.recent = (self.runtime.recent + [{'kind': 'voice-pipeline', 'error': type(exc).__name__+': '+str(exc)[:160]}])[-30:]
            answer = {'text': failure_text('model', exc), 'speech': failure_speech('model', exc)}
        await self._prerender(answer, trace)
        answer_id = async_reply.keep(answer, text)
        sn = self.trace_context.get('sn') or ''
        session = next((s for s in (getattr(self.runtime, 'sessions', None) or {}).values()
                        if sn and s.get('device_sn') == sn and s.get('send')), None)
        try:  # колонка могла переподключиться — живое соединение по SN, иначе это
            await (session['send'] if session else self.send)(
                b'AAPI' + async_reply.push(self.server_type, answer_id).SerializeToString())
        except Exception as exc:
            if trace:
                trace.event('async_push_failed', 'server', error=type(exc).__name__)
            return
        if trace:
            trace.event('async_push', 's2c', answer_len=len(answer.get('text') or ''))

    async def _prerender(self, answer, trace=None):
        """Звук отложенного ответа — до Push: колонка запросит ответ и получит его сразу, без
        ожидания синтеза (колонка не ждёт; синтезу даётся OPEN_TTS_WAIT_S). Не вышло — синтез
        повторится по запросу колонки, как обычно."""
        spoken = speakable(answer.get('speech') or (answer.get('text') or '').strip())
        if not (self.runtime.settings.get('aapi_tts') and spoken):
            return
        try:
            if trace:  # TASK-088: pre-render звука отложенного ответа — тоже «отвечает» (api/live)
                trace.event('tts_start', 'server')
            pages, speech = await self._synthesize_cached([spoken], wait=OPEN_TTS_WAIT_S)
            answer['audio'] = await collect_pages(pages, speech)
        except Exception as exc:
            if trace:
                trace.event('tts_failed', 'server', error=type(exc).__name__, prerender=True)

    async def _async_answer(self, incoming, request, event_obj, trace=None):
        """TextInput колонки по Push: отложенный ответ модели обычным AliceResponse с голосом."""
        answer_id = _string_member(_struct_member(event_obj.Payload, 'analytics'), 'origin_info')
        entry = async_reply.take(answer_id)
        if entry is None:
            await self.spoken_reply(incoming, async_reply.MISSING_TEXT, trace=trace,
                                    request_id=getattr(request.Header, 'RequestId', '') or '', event_name='async_missing')
            return
        if trace:
            trace.event('async_answer', 'server')
        await self._model_reply(incoming, request, entry['text'], entry['answer'], trace, screen=True)

    async def _model_reply(self, incoming, request, text, answer, trace=None, screen=False):
        """Ответ модели колонке: карточка, голос, действия модели и директивы
        (сборка — voice_response.model_reply). Общий для обычного и отложенного (Push) ответа."""
        return await voice_response.model_reply(self, incoming, request, text, answer, trace, screen=screen)


    async def run(self, incoming, kind, audio=None, asr_task=None, trace=None):

        stage = 'request'  # этап для текста ошибки пользователю (failure_text)

        try:

            request = getattr(incoming.Event, kind)

            if request.Request.ResetSession:

                self.history.clear()

                if agent_engine(self.runtime) == 'graph':

                    await self.runtime.agent.reset(self.runtime, self.thread)

            if audio is not None:

                if trace is None:

                    trace = self.new_trace()

                stage = 'asr'

                if asr_task is not None:

                    recognized = await asr_task

                else:

                    body, filename = input_audio(audio, request.Format)

                    raw, _ = await self.runtime.provider('stt', audio=body, filename=filename)

                    recognized = json.loads(raw)


                text = recognized.get('text', '').strip()

                if not text:

                    # Реальный wire (capture): финальный пустой AsrResult + close OK + реплика

                    trace.event('no_speech', 'server', decision='NO_SPEECH')

                    await self.recognition(incoming, '', final=True)

                    await self.close_stream(incoming.Event.Header.StreamId, 0, trace)

                    await self.retry_prompt(incoming, trace)

                    return

                trace.event('asr_final', 's2c', text_len=len(text))

                await self.recognition(incoming, text, final=True)

            else:

                event_inner = getattr(request.Request, 'Event', None)

                text = getattr(event_inner, 'Text', '') if event_inner else ''

            dialog = getattr(self.runtime, 'dialog', None)

            # Server action events (Type=6 server_action): handle via protocol_event scenarios

            event_obj = getattr(request.Request, 'Event', None)

            event_type = getattr(event_obj, 'Type', None) if event_obj else None

            event_name = getattr(event_obj, 'Name', None) if event_obj else None

            if dialog is not None and event_type is not None and event_name:

                # Service callback (e.g. @@mm_typed_callback, @@mm_semantic_frame, etc.)

                # event_type == 6 (server_action) — matched by protocol_event triggers.

                # Сценариям отдаются поля запроса (service_event_fields): цель колбэка,

                # флаг тишины и таймзона устройства — из них JSON-сценарий собирает

                # тихий ответ облака (set_timezone/set_location) вместо устной заглушки.

                ctx = {'input': {'message_id': incoming.Event.Header.MessageId,

                                 'request_id': getattr(request.Header, 'RequestId', '') or '',

                                 'dialog_id': getattr(request.Header, 'DialogId', '') or '',

                                 'sn': self.trace_context.get('sn') or '',

                                 'text': ''},

                       'clock': {'now': datetime.datetime.now()},

                       'sequence': next(self.runtime.sequence)}

                ctx['input'].update(service_event_fields(request, event_obj))

                if event_name == '@@mm_semantic_frame' and ctx['input']['purpose'] == async_reply.PURPOSE:

                    # колонка забирает отложенный ответ модели по Push (async_reply.py)

                    await self._async_answer(incoming, request, event_obj, trace)

                    return

                context = getattr(self.runtime, '_scenario_context', None)

                if context is not None:

                    ctx.update(context(sn=ctx['input']['sn'],

                                       device_id=self.trace_context.get('device_id') or ''))

                dialog_id = getattr(request.Header, 'DialogId', '') or incoming.Event.Header.MessageId

                # TASK-058: колбэк — обработчик WS. Маршрут колбэка → внутреннее событие-запрос
                # данных (informers.requested и пр.), ответ колонке собирает сценарий-поставщик
                # (триггер kind: provide); поставщика нет — умолчание колбэка (тихое подтверждение).
                # Без маршрута — прежний путь event-сценариев (совместимость расширений),
                # дальше «Без ответа».
                fields = {key: ctx['input'].get(key) for key in ('callback', 'purpose', 'frame')}

                provide = aapi_handlers.route(event_name, fields)

                aapi_handlers.count(event_name)

                aapi_handlers.notify_observers(self, incoming, event_name)  # TASK-059

                handled = False

                if provide:

                    report = await dialog.handle_event(provide, ctx, dialog_id, self.send)

                    handled = bool(report.get('handled'))

                if not handled:

                    # обработчики расширений на это имя события (TASK-059) — после встроенных

                    for ext_handler in aapi_handlers.handlers(event_name, self.runtime.settings):

                        if await ext_handler(self, incoming, event_name):

                            handled = True

                            break

                if not handled:

                    report = await dialog.handle_event(event_name, ctx, dialog_id, self.send)

                    handled = bool(report.get('handled'))

                if handled:

                    aapi_handlers.count(event_name, handled=True)

                    if trace:

                        trace.event('server_action_handled', 'server',

                                    event=event_name, scenario=report.get('scenario'))

                    return

                if provide:

                    # умолчание колбэка без поставщика — тихое подтверждение (как system.service_ack)

                    message = await dialog.quiet_ack(ctx)

                    await self.send(b"AAPI" + message.SerializeToString())

                    aapi_handlers.count(event_name, handled=True)

                    if trace:

                        trace.event('callback_default_ack', 'server', event=event_name, provide=provide)

                    return

                # «Эмулятор → Без ответа»: колбэк без маршрута и сценария (образец — поля запроса)

                self.runtime.callback_misses.note(event_name, sn=ctx['input']['sn'],

                                                  example=json.dumps(ctx['input'], ensure_ascii=False, default=str))

            # Владелец диалога: агент ждёт «да/нет» на своё действие — реплика его, не роутера сценариев

            agent_waits = bool(text) and agent_engine(self.runtime) == 'graph'                 and await self.runtime.agent.has_pending(self.runtime, self.thread)

            if agent_waits and trace:

                trace.event('agent_confirm_answer', 'server')

            if dialog is not None and text and not agent_waits:

                # R07: сценарии до заглушки; пустой каталог → handled=False → прежний путь

                ctx = {'input': {'message_id': incoming.Event.Header.MessageId,

                                 'request_id': getattr(request.Header, 'RequestId', '') or '',

                                 'dialog_id': getattr(request.Header, 'DialogId', '') or '',

                                 'sn': self.trace_context.get('sn') or '',

                                 'text': text},

                       'clock': {'now': datetime.datetime.now()},

                       'sequence': next(self.runtime.sequence)}

                # TASK-079: таймеры/будильники/экраны «Часов» — из состояния запроса

                # (EnvironmentStateRaw); своих планировщиков таймеров на сервере больше нет

                ctx['station'] = station_state.from_request(request)

                # «Вместе» (091): режим маршрута и владелец плеера — сценариям и контрактам SDK
                hybrid = getattr(self.runtime, 'hybrid_session', None)
                mode_fn = getattr(self.runtime, 'hybrid_mode', None)
                if hybrid is not None and mode_fn is not None:
                    ctx['route_mode'] = mode_fn()
                    if hybrid.yandex_player:
                        ctx['player_owner'] = 'yandex' 

                context = getattr(self.runtime, '_scenario_context', None)

                if context is not None:  # guard/bindings: гео владельца, комната колонки

                    ctx.update(context(sn=ctx['input']['sn'],

                                       device_id=self.trace_context.get('device_id') or ''))

                if getattr(self.runtime, 'animations', None) is not None:

                    ctx['animations'] = {a['id']: a['url'] for a in self.runtime.animations.list() if a.get('url')}

                dialog_id = getattr(request.Header, 'DialogId', '') or incoming.Event.Header.MessageId

                stage = 'scenario'

                report = await dialog.handle_text(text, ctx, dialog_id, self.send, deliver=False)

                if report.get('handled'):

                    # Сообщения ещё не отправлены: сначала синтезируем озвучку,

                    # поднимаем HasVoiceResponse как облако (capture: True, когда

                    # идёт аудио) — только потом доставляем. Станция играет голос

                    # лишь при этом флаге, «молчаливая» карточка его не ждёт.

                    pages = speech = None

                    plain = re.sub(r'<[^>]+>', '', report.get('speech') or '').strip()

                    if self.runtime.settings.get('aapi_tts') and plain:

                        try:

                            if trace:  # TASK-088: синтез ответа сценария — «отвечает»

                                trace.event('tts_start', 'server')

                            pages, speech = await self._synthesize_cached(report.get('speech_parts') or [plain])

                            if trace:

                                trace.event('tts_done', 'server', first_page_bytes=len(pages[0]) if pages else 0)

                        except Exception as exc:

                            if trace:

                                trace.event('tts_failed', 'server', error=type(exc).__name__)

                            self.runtime.recent = (self.runtime.recent + [{'kind': 'tts', 'error': type(exc).__name__ + ': ' + str(exc)[:120]}])[-30:]

                    if trace:

                        trace.event('scenario_handled', 'server',

                                    scenario=report.get('scenario'), reason=report.get('reason'),

                                    status=report.get('status'), voice=bool(pages))

                    for message in report.get('messages') or []:

                        if pages and message.WhichOneof('Message') == 'Event' \
                                and message.Event.WhichOneof('Event') == 'AliceResponse':

                            message.Event.AliceResponse.VoiceResponse.HasVoiceResponse = True

                        await self.send(b'AAPI' + message.SerializeToString())

                    if pages:

                        await self._stream_tts_channel(incoming, pages, speech, trace)

                    elif speech:

                        await speech.aclose()

                    return

                # «Вместе» (091): промах — настоящему Яндексу текстом; при отказе — прежний путь
                hybrid = getattr(self.runtime, 'hybrid_session', None)
                mode_fn = getattr(self.runtime, 'hybrid_mode', None)
                if hybrid is not None and mode_fn is not None and mode_fn() == 'hybrid':
                    if await self._hybrid_forward(incoming, request, text, hybrid):
                        return

                # сценария нет — фраза в очередь «Эмулятор → Без ответа» (в любом режиме «Интеллект»)

                self.runtime.phrase_misses.note(text, sn=self.trace_context.get('sn') or '')

            answer = None

            # agent.async_reply_s: модель думает дольше — короткий ответ сразу, её ответ потом по Push

            defer = async_reply.after(self.runtime)

            stage = 'model'

            chat = self.runtime.chat(text, history=self.history, thread=self.thread,

                                                 sn=self.trace_context.get('sn') or '',

                                                 device_id=self.trace_context.get('device_id') or '',

                                                 dialog_id=getattr(request.Header, 'DialogId', '') or '',

                                                 request_id=getattr(request.Header, 'RequestId', '') or '',

                                                 speaker=self.speaker)  # TASK-069: кто говорит

            answer = await (self._chat_or_defer(chat, incoming, request, text, defer, trace) if defer else chat)

            if answer is None:

                return

            await self._model_reply(incoming, request, text, answer, trace)


        except asyncio.CancelledError:

            if trace:

                trace.event('run_cancelled', 'server', cause='CancelledError')

            raise

        except Exception as exc:

            if trace:

                trace.event('pipeline_error', 'server', error=type(exc).__name__+': '+str(exc)[:120], stage=stage)

            self.runtime.recent = (self.runtime.recent + [{'kind': 'voice-pipeline', 'error': type(exc).__name__+': '+str(exc)[:160]}])[-30:]

            if audio is not None:

                await self.close_stream(incoming.Event.Header.StreamId, 1, trace)

            # Пользователь узнаёт, что сломалось: карточка с текстом и голос, если TTS жив

            # (spoken_reply при сбое синтеза отдаёт карточку без голоса)

            try:

                await self.spoken_reply(incoming, failure_text(stage, exc), trace=trace,

                                        request_id=getattr(getattr(incoming.Event, kind).Header, 'RequestId', '') or '',

                                        event_name='failure_reply', speech_text=failure_speech(stage, exc))

            except Exception:

                pass

        finally:

            if asr_task and not asr_task.done():

                asr_task.cancel()

                await asyncio.gather(asr_task, return_exceptions=True)

            if 'speech' in locals() and speech:

                await speech.aclose()

