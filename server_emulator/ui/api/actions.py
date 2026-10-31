"""Домен actions: каталог серверных команд (CATALOG) и их отправка в живую
WS-сессию устройства (push NAlice.TServerMessage, журнал последних 100
вызовов с состоянием доставки) (RF-03). Ветка перенесена дословно из
legacy-замыкания legacy_api (server_emulator/runtime.py).
"""
from aiohttp import web

from ...actions import CATALOG, push_message
from ...schema import load_type

from . import route


@route("actions", methods=("GET", "POST"))
async def actions(request, runtime, app):
    from ...runtime import ROOT  # локально: runtime.py тянет весь эмулятор, наверху был бы цикл импорта
    if request.method=='GET':
        return web.json_response({'catalog':CATALOG,'history':runtime.action_log})
    if request.method!='POST':
        raise web.HTTPMethodNotAllowed(request.method,['GET','POST'])
    value=await request.json()
    session=runtime.sessions.get(value.get('connection_id'))
    if not session or not session.get('user_id') or session.get('device_sn')!=value.get('device_sn'):
        raise ValueError('Выберите активную сессию нужного SN с назначенным владельцем')
    cls=load_type(ROOT/'schemas','alice-api.pb','NAlice.NAliceApi.TServerMessage')
    message=push_message(cls,value['action'],value.get('arguments',{}))
    item={'message_id':message.Event.Header.MessageId,'connection_id':value['connection_id'],'device_sn':session['device_sn'],'user_id':session['user_id'],'action':value['action'],'delivery':'sending','execution':'unconfirmed'}
    runtime.action_log=(runtime.action_log+[item])[-100:]
    try:
        await session['send'](b'AAPI'+message.SerializeToString())
    except (ConnectionError,RuntimeError):
        item['delivery']='failed'
        raise ValueError('Сессия закрылась до отправки команды')
    if item['delivery']=='sending':
        item['delivery']='sent'
    return web.json_response(item)
