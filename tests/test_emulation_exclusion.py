"""Маршрут запроса: устройство СОЕДИНЕНИЯ (IP) приоритетнее заявленного device_id,
заявка заголовка — фолбэк при неопознанном IP; смена маршрута устройства закрывает
только его WS-соединения (бывший emulation_excluded)."""
from server_emulator.runtime import Runtime


class FakeRequest:
    """Минимальный request для resolve-решений (тело не читается)."""

    def __init__(self, method="GET", host="quasar.yandex.net", path="/check_updates",
                 query=None, headers=None, remote="127.0.0.1"):
        self.method = method
        self.host = host
        self.path = path
        self.query = query or {}
        self.headers = headers or {}
        self.match_info = {}
        self.remote = remote


def _registry(runtime):
    runtime.identities.mutate({'operation': 'user', 'id': 'u', 'name': 'U'})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-REAL', 'user_id': 'u',
                               'device_ids': ['real-id'], 'profile': {}})
    runtime.identities.mutate({'operation': 'device', 'sn': 'SN-EMU', 'user_id': 'u',
                               'device_ids': ['emu-id'], 'profile': {}})


def _connection(runtime):
    """Доверенные наблюдения соединения (WS-handshake/get_sync_info): IP → SN."""
    runtime.identities.observe('real-id', ip='192.0.2.10', trusted=True)
    runtime.identities.observe('emu-id', ip='192.0.2.20', trusted=True)


async def test_route_mode_from_headers_and_query(tmp_path):
    runtime = Runtime(tmp_path)
    _registry(runtime)
    assert runtime.route_mode_for(FakeRequest(headers={'x-uprx-device-id': 'real-id'})) \
        == ('SN-REAL', 'real-id', 'emulate')  # без своего маршрута — эмуляция
    runtime.identities.mutate({'operation': 'route_mode', 'sn': 'SN-REAL', 'mode': 'hybrid'})
    assert runtime.route_mode_for(FakeRequest(headers={'x-uprx-device-id': 'real-id'}))[2] == 'hybrid'
    assert runtime.route_mode_for(FakeRequest(query={'device_id': 'emu-id'})) \
        == ('SN-EMU', 'emu-id', 'emulate')
    # неизвестное устройство и запрос без маркеров — всегда эмуляция (маршрута по умолчанию нет)
    assert runtime.route_mode_for(FakeRequest(headers={'x-uprx-device-id': 'ghost'}))[2] == 'emulate'
    assert runtime.route_mode_for(FakeRequest()) == (None, None, 'emulate')
    await runtime.close()


async def test_route_prefers_connection_device_over_claimed_header(tmp_path):
    """Смешанный парк: экран станции шлёт запрос с device_id другой колонки
    (заявка из профиля аккаунта, не своей станции) — маршрут берётся у устройства
    соединения, каждый при своём режиме; заявленный device_id не теряется."""
    runtime = Runtime(tmp_path)
    _registry(runtime)
    runtime.identities.mutate({'operation': 'route_mode', 'sn': 'SN-REAL', 'mode': 'hybrid'})
    _connection(runtime)
    # экран hybrid-станции (192.0.2.10) подписал запрос device_id эмулируемой колонки
    assert runtime.route_mode_for(FakeRequest(headers={'X-Ya-Device-Id': 'emu-id'}, remote='192.0.2.10')) \
        == ('SN-REAL', 'emu-id', 'hybrid')
    # и наоборот: соединение второй колонки — её emulate, даже если заявка чужая
    assert runtime.route_mode_for(FakeRequest(headers={'x-uprx-device-id': 'real-id'}, remote='192.0.2.20')) \
        == ('SN-EMU', 'real-id', 'emulate')
    await runtime.close()


async def test_route_mismatch_of_claim_and_connection_is_journaled(tmp_path):
    runtime = Runtime(tmp_path)
    _registry(runtime)
    runtime.identities.mutate({'operation': 'route_mode', 'sn': 'SN-REAL', 'mode': 'hybrid'})
    _connection(runtime)
    runtime.route_mode_for(FakeRequest(headers={'X-Ya-Device-Id': 'emu-id'}, remote='192.0.2.10'))
    entry = next(item for item in runtime.recent if 'route_mismatch' in item)
    assert entry['route_mismatch'] == {'connection': 'SN-REAL', 'claimed': 'SN-EMU', 'device_id': 'emu-id'}
    await runtime.close()


async def test_route_falls_back_to_claimed_header_when_ip_unknown_or_ambiguous(tmp_path):
    """IP не опознан или два устройства на одном IP — прежнее поведение: маршрут по заявке."""
    runtime = Runtime(tmp_path)
    _registry(runtime)
    runtime.identities.mutate({'operation': 'route_mode', 'sn': 'SN-REAL', 'mode': 'hybrid'})
    assert runtime.route_mode_for(FakeRequest(headers={'X-Ya-Device-Id': 'real-id'}, remote='192.0.2.99')) \
        == ('SN-REAL', 'real-id', 'hybrid')
    runtime.identities.observe('real-id', ip='192.0.2.30', trusted=True)
    runtime.identities.observe('emu-id', ip='192.0.2.30', trusted=True)  # NAT: неоднозначность
    assert runtime.route_mode_for(FakeRequest(headers={'X-Ya-Device-Id': 'emu-id'}, remote='192.0.2.30')) \
        == ('SN-EMU', 'emu-id', 'emulate')
    await runtime.close()


async def test_route_by_connection_without_any_claim(tmp_path):
    """Известное соединение без маркеров устройства — маршрут устройства соединения."""
    runtime = Runtime(tmp_path)
    _registry(runtime)
    runtime.identities.mutate({'operation': 'route_mode', 'sn': 'SN-REAL', 'mode': 'hybrid'})
    _connection(runtime)
    assert runtime.route_mode_for(FakeRequest(remote='192.0.2.10')) == ('SN-REAL', None, 'hybrid')
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
    assert result == {'sn': 'SN-REAL', 'mode': 'hybrid', 'disconnected': 0}  # emulate → hybrid: WS не рвётся
    result = await runtime.set_device_route_mode('SN-REAL', 'proxy')
    assert result == {'sn': 'SN-REAL', 'mode': 'proxy', 'disconnected': 2}  # в «Прозрачно» — рвётся
    await runtime.closer.drain()  # закрытие идёт в фоне
    assert len(closed) == 2 and all(item['code'] == 1012 for item in closed)
    assert runtime.identities.resolve('real-id')['route_mode'] == 'proxy'
    # сброс в None — снова без своего маршрута (эмуляция)
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
