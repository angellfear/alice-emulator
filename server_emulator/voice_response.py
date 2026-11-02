"""Сборка protobuf-ответа колонке (TASK-008, DEBT-02): AliceResponse — карточки,
директивы (громкость, ShouldListen, экран Push), события AsrResult и фразы сбоев
конвейера.

Модуль знает alice-api protobuf, но не голосовые сервисы: не импортирует voice_stack
адаптеры и audio_buffer — страницы аудио (Ogg/Opus) для него непрозрачные байты.
Сборка принимает Conversation (duck-typing: event/message/send/runtime/sequence/
recent/pending_calls/history и методы синтеза) — protobuf-часть отделена от аудио.
"""
import asyncio

import re

import uuid

from . import async_reply

from .model_actions import execute as execute_model_actions, parse_calls

from .response_compose import add_volume_directive, finalize

from .spoken import speakable


FAILURE_STAGE = {'asr': 'Не получилось распознать речь', 'model': 'Не получилось получить ответ модели',
                 'tts': 'Озвучка недоступна',
                 'scenario': 'Не получилось выполнить команду', 'request': 'Не получилось обработать запрос'}


FAILURE_REASONS = ('сервис ответил ошибкой', 'сервис не ответил вовремя', 'нет связи с сервисом',
                   'сервис не настроен', 'внутренняя ошибка сервера')


def _failure(stage, exc):
    name, message = type(exc).__name__, str(exc)
    status = getattr(exc, 'status_code', None) or (re.search(r'HTTP (\d{3})', message) or [None, None])[1]
    if status:
        reason = FAILURE_REASONS[0]
    elif isinstance(exc, asyncio.TimeoutError) or 'Timeout' in name:
        reason = FAILURE_REASONS[1]
    elif isinstance(exc, OSError) or 'Connect' in name or 'недоступ' in message:
        reason = FAILURE_REASONS[2]
    elif 'Настройте' in message or 'нужен API-ключ' in message:
        reason = FAILURE_REASONS[3]
    else:
        reason = FAILURE_REASONS[4]
    return FAILURE_STAGE.get(stage, FAILURE_STAGE['request']), reason, status


def _failure_phrase(head, reason, status=None):
    return f'{head}: {reason}{" " + str(status) if status else ""}. Подробности — в журнале эмулятора.'


def failure_text(stage, exc):
    """Сбой конвейера → текст карточки: что сломалось и почему (с кодом HTTP, если есть)."""
    return _failure_phrase(*_failure(stage, exc))


def failure_speech(stage, exc):
    """То же для голоса — без кода HTTP: фраза из конечного набора, озвучка из звукового кеша."""
    head, reason, _ = _failure(stage, exc)
    return _failure_phrase(head, reason)


def failure_phrases():
    """Все озвучиваемые фразы сбоев (раздел «AI агент» звукового кеша); сбой TTS не озвучивается."""
    return [_failure_phrase(head, reason) for stage, head in FAILURE_STAGE.items() if stage != 'tts'
            for reason in FAILURE_REASONS]

def fill_asr_result(event, text, final):
    """Поля AsrResult финального/частичного распознавания в outgoing.Event."""
    event.AsrResult.EndOfUtt = final
    event.AsrResult.ResponseCode = 0
    event.AsrResult.MessagesCount = 1
    recognition = event.AsrResult.Recognition.add(Normalized=text, ParentModel='sherpa-t-one')
    for word in text.split():
        recognition.Words.add(Value=word)
    event.AsrResult.AggregatedRecognition.RecognizedText = text
    event.AsrResult.AggregatedRecognition.IsFinal = final


async def spoken_reply(conv, incoming, text, trace=None, request_id='', force_tts=False,
                   event_name='spoken_reply', speech_text=None):

        """Готовая реплика без модели: карточка AliceResponse + озвучка тем же конвейером
        (speech_text — что сказать, если голос отличается от текста карточки)."""

        pages = None

        speech = None

        tts_error = None

        if force_tts or conv.runtime.settings.get('aapi_tts'):

            try:

                if trace:  # TASK-088: начался синтез — ход в состоянии «отвечает» (строка шапки, api/live)

                    trace.event('tts_start', 'server')

                # стандартные/готовые реплики — через звуковой кеш (tts_cache): повтор без синтеза

                pages, speech = await conv._synthesize_cached([speech_text or text])

            except Exception as exc:

                tts_error = exc

                if trace:

                    trace.event('tts_failed', 'server', error=type(exc).__name__)

                conv.runtime.recent = (conv.runtime.recent + [{'kind': 'tts', 'error': type(exc).__name__ + ': ' + str(exc)[:120]}])[-30:]


        outgoing = conv.event(incoming, 'AliceResponse')

        response = outgoing.Event.AliceResponse

        response.Header.RequestId = request_id

        response.Response.Cards.add(Type='simple_text', Text=text)

        if tts_error is not None:

            response.Response.Cards.add(Type='simple_text', Text=failure_text('tts', tts_error))

        response.VoiceResponse.OutputSpeech.Text = text

        response.VoiceResponse.HasVoiceResponse = bool(pages)

        finalize(outgoing, next(conv.runtime.sequence))

        await conv.message(outgoing)

        if trace:

            trace.event(event_name, 's2c', has_voice=bool(pages))

        if pages:

            await conv._stream_tts_channel(incoming, pages, speech, trace)

            if 'speech' in locals() and speech:
                await speech.aclose()

async def model_reply(conv, incoming, request, text, answer, trace=None, screen=False):
    """Ответ модели колонке: карточка, голос, действия модели и директивы.
    Общий для обычного и отложенного (Push) ответа."""

    if trace:

        trace.event('model_done', 'server', answer_len=len(answer.get('text', '')),

                    tool_calls=len(answer.get('tool_calls', [])))

    answer['text'] = (answer.get('text') or '').strip()  # модели начинают ответ с «\n\n»

    if answer.get('tool_calls'):

        conv.runtime.pending_calls = (conv.runtime.pending_calls + answer['tool_calls'])[-30:]

        if not answer['text']:

            answer['text'] = 'Для выполнения действия подтвердите вызов инструмента в панели эмулятора.'

    pages = None

    speech = None

    tts_error = None

    spoken = speakable(answer.get('speech') or answer['text'])  # без эмодзи и разметки — их синтез читает буквально

    if answer.get('audio'):  # отложенный ответ: звук синтезирован до Push

        pages = answer['audio']

    elif conv.runtime.settings.get('aapi_tts') and spoken:

        try:

            if trace:  # TASK-088: начался синтез — ход в состоянии «отвечает» (строка шапки, api/live)

                trace.event('tts_start', 'server')

            # режим «сценарии»: фраза из пакета озвучки (кеш), как реплики сценариев
            pages, speech = await (conv._synthesize_cached([spoken]) if answer.get('source') == 'scenarios'
                                   else conv._synthesize_speech(spoken))

            if trace:

                trace.event('tts_done', 'server', first_page_bytes=len(pages[0]) if pages else 0)

        except Exception as exc:

            tts_error = exc

            if trace:

                trace.event('tts_failed', 'server', error=type(exc).__name__)

            conv.runtime.recent = (conv.runtime.recent + [{'kind': 'tts', 'error': type(exc).__name__ + ': ' + str(exc)[:120]}])[-30:]


    outgoing = conv.event(incoming, 'AliceResponse')

    response = outgoing.Event.AliceResponse

    response.Header.RequestId = request.Header.RequestId

    response.Header.ResponseId = uuid.uuid4().hex

    response.Response.Cards.add(Type='simple_text', Text=answer['text'])

    if tts_error is not None:  # голоса не будет — карточка объясняет почему

        response.Response.Cards.add(Type='simple_text', Text=failure_text('tts', tts_error))

    response.VoiceResponse.OutputSpeech.Text = answer['text']

    response.VoiceResponse.HasVoiceResponse = bool(pages)

    # ждёт подтверждения или спрашивает — колонка слушает ответ без «Алиса» (и после Push)

    response.VoiceResponse.ShouldListen = bool(answer.get('tool_calls')) or answer['text'].rstrip().endswith('?')

    if screen:  # ответ по Push: экран колонка сама не открывает — карточка директивой

        async_reply.screen_card(response, answer['text'])

    # Модель выбирает карточку/действие/оба: вызовы alice_action исполняются,

    # заметки становятся карточками; прочие инструменты остаются ручными.

    model_calls = parse_calls(answer.get('tool_calls'))                 if conv.runtime.settings.get('model_actions') else []

    if model_calls:

        sn = conv.trace_context.get('sn') or ''

        notes, executed, deferred = execute_model_actions(conv.runtime, model_calls, sn, response)

        for note in notes:

            response.Response.Cards.add(Type='simple_text', Text=note)

        if trace:

            trace.event('model_actions', 'server', executed=[c['kind'] for c in executed],

                        deferred=[c['kind'] for c in deferred], notes=len(notes))

        remaining = [c for c in (answer.get('tool_calls') or [])

                     if c.get('id') not in {c2.get('id') for c2 in model_calls}]

        answer['tool_calls'] = remaining

    for directive in answer.get('directives') or []:

        if directive.get('kind') == 'volume':

            add_volume_directive(response, directive['level'])

    for message in answer.get('scenario_messages') or []:

        if message.WhichOneof('Message') == 'Event' and message.Event.WhichOneof('Event') == 'AliceResponse':

            present = {d.Name for d in response.Response.Directives}

            response.Response.Directives.extend(d for d in message.Event.AliceResponse.Response.Directives

                                                if d.Name not in present)

    finalize(outgoing, next(conv.runtime.sequence))

    await conv.message(outgoing)

    if trace:

        trace.event('alice_response', 's2c', has_voice=bool(pages))

    conv.history =(conv.history + [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': answer['text']}])[-12:]

    if pages:

        await conv._stream_tts_channel(incoming, pages, speech, trace)
