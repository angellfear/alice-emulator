"""Typed local-device action adapter. No external MCP transport is required."""
import time
import uuid
from google.protobuf import json_format


CATALOG = [
    {'id':'sound_louder','name':'Громче','parameters':{}},
    {'id':'sound_quiter','name':'Тише','parameters':{}},
    {'id':'sound_set_level','name':'Установить громкость','parameters':{'level':'0..10'}},
    {'id':'start_bluetooth','name':'Включить Bluetooth','parameters':{}},
    {'id':'stop_bluetooth','name':'Выключить Bluetooth','parameters':{}},
    {'id':'push_update_config','name':'Обновить конфигурацию','parameters':{}},
    {'id':'draw_led_screen','name':'Показать анимацию (LED)','parameters':{'animation':'id или URL'}},
]


def push_message(server_type, action, arguments):
    if action not in {a['id'] for a in CATALOG}:
        raise ValueError('Неизвестное действие')
    if action=='draw_led_screen':
        frames = arguments.get('frames') if isinstance(arguments, dict) else None
        if not (isinstance(frames, list) and frames and all(isinstance(u, str) and u.startswith('http') for u in frames)):
            raise ValueError('Анимация: {frames: [URL,…]} — кадры последовательности wire')
        arguments, payload = {}, {'animation_sequence':[{'frontal_led_image': u} for u in frames],
                                  'till_end_of_speech': False}
        output=server_type()
        output.Event.Header.MessageId=uuid.uuid4().hex
        output.Event.Header.Ack=int(time.time()*1000)
        push=output.Event.Push
        push.DeduplicationPushId=uuid.uuid4().hex
        push.PushIds.append(uuid.uuid4().hex)
        directive=push.Directives.add()
        directive.Name=action
        directive.Type='client_action'
        directive.IsLedSilent=True
        json_format.ParseDict(payload,directive.Payload,descriptor_pool=server_type.DESCRIPTOR.file.pool)
        output.Timings.SendingTime.seconds=int(time.time())
        return output
    if not isinstance(arguments,dict) or set(arguments)-({'level'} if action=='sound_set_level' else set()):
        raise ValueError('Недопустимые параметры действия')
    payload = {}
    if action=='sound_set_level':
        level=arguments.get('level')
        if isinstance(level,bool) or not isinstance(level,(int,float)) or not 0<=level<=10:
            raise ValueError('Громкость должна быть числом от 0 до 10')
        payload={'new_level':level,'new_percent_level':level*10}
    output=server_type()
    output.Event.Header.MessageId=uuid.uuid4().hex
    output.Event.Header.Ack=int(time.time()*1000)
    push=output.Event.Push
    push.DeduplicationPushId=uuid.uuid4().hex
    push.PushIds.append(uuid.uuid4().hex)
    directive=push.Directives.add()
    directive.Name=action
    directive.Type='client_action'
    directive.IsLedSilent=True
    if payload:
        json_format.ParseDict(payload,directive.Payload,descriptor_pool=server_type.DESCRIPTOR.file.pool)
    output.Timings.SendingTime.seconds=int(time.time())
    return output


async def send_action(runtime, sn, action, arguments):
    """Команда колонке по SN: push в её живую WS-сессию, запись в журнал действий.
    Общая для MCP-инструмента alice_speaker_command (mcp_hub)."""
    from pathlib import Path
    from .schema import load_type
    session = next((s for s in runtime.sessions.values()
                    if s.get('device_sn') == sn and s.get('send') and s.get('user_id')), None)
    if session is None:
        raise ValueError('Колонка не в сети: нет активной WS-сессии ' + sn)
    cls = load_type(Path(__file__).parent/'schemas', 'alice-api.pb', 'NAlice.NAliceApi.TServerMessage')
    message = push_message(cls, action, arguments or {})
    item = {'message_id': message.Event.Header.MessageId, 'connection_id': session['connection_id'],
            'device_sn': sn, 'user_id': session['user_id'], 'action': action,
            'delivery': 'sending', 'execution': 'unconfirmed', 'origin': 'mcp'}
    runtime.action_log.append(item)  # на месте: сессионная копия runtime делит список с панелью
    del runtime.action_log[:-100]
    try:
        await session['send'](b'AAPI' + message.SerializeToString())
    except (ConnectionError, RuntimeError):
        item['delivery'] = 'failed'
        raise ValueError('Сессия закрылась до отправки команды')
    item['delivery'] = 'sent'
    return item


def push_directive(server_type, name, payload=None, items=None):
    """Push с одной произвольной директивой (TASK-059: sdk.send_directive) — тот же
    конверт, что у push_message, без проверки каталога. items (TASK-079) — перечень
    элементов [{"name", "payload"?, "payload_raw"?, "is_led_silent"?}]: несколько штатных
    директив (таймеры/будильники) уходят одним Push-сообщением."""
    output = server_type()
    output.Event.Header.MessageId = uuid.uuid4().hex
    output.Event.Header.Ack = int(time.time() * 1000)
    push = output.Event.Push
    push.DeduplicationPushId = uuid.uuid4().hex
    push.PushIds.append(uuid.uuid4().hex)
    elements = items if items is not None else [{"name": name, "payload": payload}]
    for element in elements:
        directive = push.Directives.add()
        directive.Name = str(element["name"])
        directive.Type = 'client_action'
        if element.get("is_led_silent") is not None:
            directive.IsLedSilent = bool(element["is_led_silent"])
        if element.get("payload"):
            json_format.ParseDict(element["payload"], directive.Payload,
                                  descriptor_pool=server_type.DESCRIPTOR.file.pool)
        if element.get("payload_raw"):
            directive.PayloadRaw = bytes(element["payload_raw"])
    output.Timings.SendingTime.seconds = int(time.time())
    return output
