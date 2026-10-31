"""Маршрут запроса (TASK-044): route_mode_for по заголовкам/query/реестру; смена
маршрута устройства закрывает только его WS-соединения (бывший emulation_excluded)."""
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


async def test_route_mode_from_headers_and_query(tmp_path):
    runtime = Runtime(tmp_path)
    _registry(runtime)
    assert runtime.route_mode_for(FakeRequest(headers={'x-uprx-device-id': 'real-id'})) \
        == ('SN-REAL', 'real-id', 'emulate')  # без своего маршрута — default
    runtime.identities.mutate({'operation': 'route_mode', 'sn': 'SN-REAL', 'mode': 'hybrid'})
    assert runtime.route_mode_for(FakeRequest(headers={'x-uprx-device-id': 'real-id'}))[2] == 'hybrid'
    assert runtime.route_mode_for(FakeRequest(query={'device_id': 'emu-id'})) \
        == ('SN-EMU', 'emu-id', 'emulate')
    # неизвестное устройство и запрос без маркеров — default_route_mode
    assert runtime.route_mode_for(FakeRequest(headers={'x-uprx-device-id': 'ghost'}))[2] == 'emulate'
    assert runtime.route_mode_for(FakeRequest()) == (None, None, 'emulate')
    runtime.settings['default_route_mode'] = 'proxy'
    assert runtime.route_mode_for(FakeRequest())[2] == 'proxy'
    await runtime.close()


async def test_set_device_route_mode_closes_only_its_sessions(tmp_path):
    runtime = Runtime(tmp_path)
    _registry(runtime)
    closed = []

    class FakeWS:
        async def close(self, **kwargs):
            closed.append(kwargs)
    runtime.sessions['a'] = {'device_sn': 'SN-REAL', 'ws': FakeWS()}
    runtime.sessions['b'] = {'device_sn': 'SN-EMU', 'ws': FakeWS()}
    runtime.sessions['c'] = {'device_id': 'real-id', 'ws': FakeWS()}  # handshake без sync: SN через DeviceId
    result = await runtime.set_device_route_mode('SN-REAL', 'hybrid')
    assert result == {'sn': 'SN-REAL', 'mode': 'hybrid', 'disconnected': 2}
    assert len(closed) == 2 and all(item['code'] == 1012 for item in closed)
    assert runtime.identities.resolve('real-id')['route_mode'] == 'hybrid'
    # сброс в None возвращает устройство на default_route_mode
    result = await runtime.set_device_route_mode('SN-REAL', None)
    assert result['mode'] is None
    assert runtime.identities.resolve('real-id')['route_mode'] is None
    await runtime.close()


async def test_route_mode_persists_and_survives_device_edit(tmp_path):
    runtime = Runtime(tmp_path)
    _registry(runtime)
    runtime.identities.mutate({'operation': 'route_mode', 'sn': 'SN-REAL', 'mode': 'proxy'})
    # правка устройства (привязка) не теряет его маршрут
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-REAL', 'name': 'Гостиная',
                               'user_id': 'u', 'device_ids': ['real-id'], 'profile': {}})
    assert runtime.identities.resolve('real-id')['route_mode'] == 'proxy'
    await runtime.close()
