"""/glagol/token: вызывающий (заголовки) ≠ цель (query device_id) — v9 §8."""
import base64
import json

import pytest
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen


def _setup_owner_with_devices(runtime):
    runtime.identities.mutate({'operation': 'user', 'id': 'alice', 'name': 'Alice'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-TV', 'user_id': 'alice',
                               'device_ids': ['tv-id'], 'profile': {}})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-SPK', 'user_id': 'alice',
                               'device_ids': ['speaker-id'], 'profile': {}})
    runtime.identities.mutate({'operation': 'user', 'id': 'bob', 'name': 'Bob'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-BOB', 'user_id': 'bob',
                               'device_ids': ['bob-tv'], 'profile': {}})


def _claims(token):
    body = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))


async def test_token_for_another_device_same_owner(tmp_path):
    """ТВ спрашивает токен колонки того же владельца: заголовок = вызывающий, query = цель."""
    runtime = Runtime(tmp_path)
    _setup_owner_with_devices(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + '/glagol/token?device_id=speaker-id',
                                  headers={'Host': 'quasar.local',
                                           'x-uprx-device-id': 'tv-id'}) as r:
                body = await r.json()
                assert r.status == 200
        claims = _claims(body['token'])
        assert claims['sub'] == 'speaker-id'      # токен ЦЕЛИ, не вызывающего
        assert claims['uid'] == 'alice'
        speaker = runtime.identities.resolve('speaker-id')
        assert runtime.quasar.verify(body['token'].encode(), speaker)
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_token_cross_owner_forbidden(tmp_path):
    """ТВ Алисы просит токен устройства Боба — 403, владелец не обходится."""
    runtime = Runtime(tmp_path)
    _setup_owner_with_devices(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + '/glagol/token?device_id=bob-tv',
                                  headers={'Host': 'quasar.local',
                                           'x-uprx-device-id': 'tv-id'}) as r:
                assert r.status == 403
                assert (await r.json())['error'] == 'forbidden'
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_token_unknown_target_404_and_not_observed_as_caller(tmp_path):
    """Неизвестная цель — 404; её id НЕ регистрируется как наблюдение вызывающего."""
    runtime = Runtime(tmp_path)
    _setup_owner_with_devices(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + '/glagol/token?device_id=ghost-id',
                                  headers={'Host': 'quasar.local',
                                           'x-uprx-device-id': 'tv-id'}) as r:
                assert r.status == 404
        pending = [d['device_id'] for d in runtime.identities.data['unassigned']]
        assert 'ghost-id' not in pending  # цель не наблюдается как вызывающий
        assert 'tv-id' not in pending     # известный вызывающий не в pending
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_token_unbound_caller_403(tmp_path):
    """Вызывающий без владельца — отказ; токен другому устройству не выдаётся."""
    runtime = Runtime(tmp_path)
    _setup_owner_with_devices(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + '/glagol/token?device_id=speaker-id',
                                  headers={'Host': 'quasar.local',
                                           'x-uprx-device-id': 'stranger'}) as r:
                assert r.status == 403
                assert (await r.json())['error'] == 'caller_not_assigned'
        # stranger отклонён и токена не получил; в pending не попадает —
        # реестр пополняется только /get_sync_info от колонки
        assert 'stranger' not in [d['device_id'] for d in runtime.identities.data['unassigned']]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_token_self_without_headers_backward_compatible(tmp_path):
    """Без заголовков устройство запрашивает токен для себя — прежняя семантика."""
    runtime = Runtime(tmp_path)
    _setup_owner_with_devices(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + '/glagol/token?device_id=speaker-id',
                                  headers={'Host': 'quasar.local'}) as r:
                assert r.status == 200
                token = (await r.json())['token']
        assert _claims(token)['sub'] == 'speaker-id'
        assert runtime.quasar.verify(token.encode(), runtime.identities.resolve('speaker-id'))
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_token_without_query_returns_caller_self(tmp_path):
    """Нет query device_id — токен вызывающего для самого себя."""
    runtime = Runtime(tmp_path)
    _setup_owner_with_devices(runtime)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + '/glagol/token',
                                  headers={'Host': 'quasar.local',
                                           'x-uprx-device-id': 'tv-id'}) as r:
                assert r.status == 200
                claims = _claims((await r.json())['token'])
        assert claims['sub'] == 'tv-id' and claims['uid'] == 'alice'
    finally:
        await runner.cleanup()
        await runtime.close()
