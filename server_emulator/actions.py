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
    {'id':'player_pause','name':'Пауза плеера','parameters':{}},
    {'id':'clear_queue','name':'Очистить очередь','parameters':{}},
    {'id':'push_tts','name':'Произнести текст','parameters':{'text':'строка'}},
]

# Каналы доставки push-действий. Важные действия — квитируемый
# SyncPush{ExecuteDirective} со статусом выполнения (started/completed/failed);
# косметика — обычный Push без квитанции, push_tts — Push (реплику колонка
# запрашивает сама TextInput'ом с тем же фреймом).
EXECUTE_ACTIONS = {'push_update_config', 'player_pause', 'clear_queue'}


def push_message(server_type, action, arguments):
    if action not in {a['id'] for a in CATALOG}:
        raise ValueError('Неизвестное действие')
    if action=='push_tts':
        text = arguments.get('text') if isinstance(arguments, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Произнести текст: {text: строка}')
        output=server_type()
        output.Event.Header.MessageId=uuid.uuid4().hex
        output.Event.Header.Ack=int(time.time()*1000)
        push=output.Event.Push
        push.DeduplicationPushId=uuid.uuid4().hex
        push.PushIds.append(uuid.uuid4().hex)
        directive=push.Directives.add()
        directive.Name='@@mm_semantic_frame'
        directive.Type='server_action'
        directive.IsLedSilent=True
        json_format.ParseDict({'typed_semantic_frame': {'tts_semantic_frame': {'text': text.strip()}}},
                              directive.Payload, descriptor_pool=server_type.DESCRIPTOR.file.pool)
        output.Timings.SendingTime.seconds=int(time.time())
        return output
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


def execute_message(server_type, action, arguments=None):
    """Квитируемое действие: SyncPush{ExecuteDirective{Directive, ResponsePolicy{
    OnFinishedPolicy}}} — колонка отвечает SyncPushResponse с ExecutionStatus
    (started/completed/failed), корреляция — RefMessageId нашего кадра. Payload
    директивы — как у Push-пути: каталог задаёт форму wire (player_pause{smooth:false})."""
    if action not in EXECUTE_ACTIONS:
        raise ValueError('Действие не квитируется: ' + action)
    output = server_type()
    output.Event.Header.MessageId = uuid.uuid4().hex
    output.Event.Header.Ack = int(time.time() * 1000)
    push = output.Event.SyncPush
    push.PushId = uuid.uuid4().hex
    push.DeduplicationPushId = uuid.uuid4().hex
    directive = push.SyncRequest.ExecuteDirective.Directive
    directive.Name = action
    directive.Type = 'client_action'
    directive.IsLedSilent = True
    payload = {}
    if action == 'player_pause':
        payload = {'smooth': False}
    if isinstance(arguments, dict) and arguments:
        raise ValueError('Недопустимые параметры действия')
    if payload:
        json_format.ParseDict(payload, directive.Payload,
                              descriptor_pool=server_type.DESCRIPTOR.file.pool)
    push.SyncRequest.ExecuteDirective.ResponsePolicy.OnFinishedPolicy.SetInParent()
    output.Timings.SendingTime.seconds = int(time.time())
    return output


async def send_action(runtime, sn, action, arguments):
    """Команда колонке по SN: push в её живую WS-сессию, запись в журнал действий.
    Общая для MCP-инструмента alice_speaker_command (mcp_hub). Квитируемые действия
    ждут SyncPushResponse (runtime.push_action)."""
    session = next((s for s in runtime.sessions.values()
                    if s.get('device_sn') == sn and s.get('send') and s.get('user_id')), None)
    if session is None:
        raise ValueError('Колонка не в сети: нет активной WS-сессии ' + sn)
    item = await runtime.push_action(sn, action, arguments or {})
    if item.get('delivery') == 'no_session':
        raise ValueError('Сессия закрылась до отправки команды')
    item['origin'] = 'mcp'
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
