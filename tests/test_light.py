"""Ядровые примитивы state.light колонки (scenario_engine/light.py, действие light.set):
ими пользуются инструмент модели и пользовательские сценарии. Фразы «включи свет…» —
не ядро: сценарий умного дома у встроенного плагина home-assistant, его проверки —
в tests/test_home_assistant_light.py."""
import pytest

from server_emulator.scenario_engine.light import apply_light, entities_of


def test_apply_light_mutates_device_state(tmp_path):
    from server_emulator.identity import IdentityRegistry
    from server_emulator.state_store import StateStore
    identities = IdentityRegistry(tmp_path, StateStore(tmp_path / "e.sqlite3"))
    identities.mutate({'operation': 'user', 'id': 'u', 'name': 'U'})
    identities.mutate({'operation': 'device', 'sn': 'SN-1', 'user_id': 'u', 'device_ids': ['d1'], 'profile': {}})
    device = next(d for d in identities.data['devices'] if d['sn'] == 'SN-1')
    identities.mutate({'operation': 'state', 'sn': 'SN-1',
                       'state': dict(device['state'], light={'кухня': {'on': False, 'brightness': 100}})})
    assert apply_light(identities, 'SN-1', 'кухня', 'on', 40) == {'on': True, 'brightness': 40}
    stored = entities_of(next(d for d in identities.data['devices'] if d['sn'] == 'SN-1'))
    assert stored['кухня'] == {'on': True, 'brightness': 40}
    with pytest.raises(ValueError):
        apply_light(identities, 'SN-1', 'баня', 'on')
