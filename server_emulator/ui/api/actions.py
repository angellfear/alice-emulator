"""Домен actions: каталог серверных команд (CATALOG) и их отправка в живую
WS-сессию устройства (push NAlice.TServerMessage, журнал последних 100
вызовов с состоянием доставки) . Отправка и ожидание квитанции
(SyncPushResponse у ExecuteDirective) — runtime.push_action.
"""
from aiohttp import web

from ...actions import CATALOG

from . import route


@route("actions", methods=("GET", "POST"))
async def actions(request, runtime, app):
    if request.method=='GET':
        return web.json_response({'catalog':CATALOG,'history':runtime.action_log})
    if request.method!='POST':
        raise web.HTTPMethodNotAllowed(request.method,['GET','POST'])
    value=await request.json()
    session=runtime.sessions.get(value.get('connection_id'))
    if not session or not session.get('user_id') or session.get('device_sn')!=value.get('device_sn'):
        raise ValueError('Выберите активную сессию нужного SN с назначенным владельцем')
    item=await runtime.push_action(value['device_sn'],value['action'],value.get('arguments') or {})
    if item.get('delivery')=='no_session':
        raise ValueError('В «Прозрачно» нет мостового соединения — колонка не на связи'
                         if item.get('reason')=='transparent' else 'Колонка не на связи (нет активной сессии)')
    if item.get('delivery')=='failed':
        raise ValueError('Сессия закрылась до отправки команды')
    return web.json_response(item)
