"""Исключение устройств из эмуляции: список SN, маршрутизация, неразрыв WS."""
import asyncio

from server_emulator.runtime import Runtime


class FakeRequest:
    """Минимальный request для resolve-решений (тело не читается)."""

    def __init__(self, method="GET", host="quasar.yandex.net", path="/check_updates",
                 query=None, headers=None):
        self.method = method
        self.host = host
        self.path = path
        self.query = query or {}
        self.headers = headers or {}
        self.match_info = {}


def _registry(runtime):
    runtime.identities.mutate({'operation': 'user', 'id': 'u', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-REAL', 'user_id': 'u',
                               'device_ids': ['real-id'], 'profile': {}})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-EMU', 'user_id': 'u',
                               'device_ids': ['emu-id'], 'profile': {}})


async def test_excluded_sn_resolved_from_headers(tmp_path):
    runtime = Runtime(tmp_path)
    _registry(runtime)
    runtime.settings['emulation_excluded'] = ['SN-REAL']
    assert runtime.emulation_excluded_sn(FakeRequest(headers={'x-uprx-device-id': 'real-id'})) == 'SN-REAL'
    assert runtime.emulation_excluded_sn(FakeRequest(headers={'x-uprx-device-id': 'emu-id'})) is None
    assert runtime.emulation_excluded_sn(FakeRequest(query={'device_id': 'real-id'})) == 'SN-REAL'
    assert runtime.emulation_excluded_sn(FakeRequest(headers={'x-uprx-device-id': 'ghost'})) is None
    runtime.settings['emulation_excluded'] = []
    assert runtime.emulation_excluded_sn(FakeRequest(headers={'x-uprx-device-id': 'real-id'})) is None
    await runtime.close()


async def test_set_mode_disconnect_keeps_excluded_sessions(tmp_path):
    runtime = Runtime(tmp_path)
    _registry(runtime)
    runtime.settings['emulation_excluded'] = ['SN-REAL']
    closed = []

    class FakeWS:
        async def close(self, **kwargs):
            closed.append(kwargs)
    runtime.sessions['a'] = {'device_sn': 'SN-REAL', 'ws': FakeWS()}
    runtime.sessions['b'] = {'device_sn': 'SN-EMU', 'ws': FakeWS()}
    runtime.disconnect_proxy = None
    result = await runtime.set_mode('emulate', 'disconnect')
    assert result['disconnected'] == 1  # закрыт только эмулируемый
    assert closed and all(closed[0].get('code') == 1012 for _ in [0])
    await runtime.close()


def test_exclusion_in_defaults(tmp_path):
    import json
    runtime = Runtime(tmp_path)
    assert runtime.settings.get('emulation_excluded') == []
