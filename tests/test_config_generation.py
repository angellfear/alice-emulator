"""Новое поколение конфига Яндекса: поставляемая защита get_sync_info и детект.

Фикстура — обезличенный живой ответ нового поколения (серийник с префиксом модели
платформы, ключ wifi_hash и сертификат glagol — заглушки, крупные не относящиеся
узлы ответа убраны). Только локальные серверы: «Яндекс» — заглушка.
"""
import json
from pathlib import Path

import pytest

from server_emulator.quasar_replace import (RULES_DIR, SUPPORTED_CONFIG_GENERATION, config_generation,
                                            effective_direct_replace, shipped_direct_replace)
from server_emulator.runtime import Runtime
from tests.test_quasar_direct_replace import rule, start, stop, sync
from tests.test_quasar_source import state_of

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'quasar_new_generation.json').read_text(encoding='utf-8'))


def centaur(body):
    return body['config']['system_config']['centaur']


def use_package_rules(monkeypatch):
    monkeypatch.setenv('ALICA_QUASAR_RULES_DIR', str(RULES_DIR))


def new_generation(marker):
    """Минимальный ответ с одним маркером нового поколения."""
    centaur = {'audio_config': {'vqe_metrics_enabled': True}}
    if marker == 'requested_owner':
        centaur['runtime_owner'] = {'config_revision': 1, 'requested_owner': 'com.yandex.io.sdk'}
    elif marker == 'io_client':
        centaur['is_io_client_features_enabled_v1'] = True
    elif marker == 'switch_strategy':
        centaur['meson'] = {'switchStrategy': 'UPDATER_LIKE_REBOOT'}
    return {'status': 'ok', 'config': {'system_config': {'centaur': centaur}}}


def test_shipped_defaults_are_the_four_protections(monkeypatch):
    use_package_rules(monkeypatch)
    rules = shipped_direct_replace()
    assert [r['pattern'] for r in rules] == [
        '"reboot_policy":\\{[^{}]*\\}',
        '"requested_owner":"[^"]*"',
        '"switchStrategy":"UPDATER_LIKE_REBOOT"',
        '"quarkIntegrationVersion":8',
    ]
    assert all(r['enabled'] for r in rules)


def test_shipped_rules_absent_without_file(tmp_path, monkeypatch):
    monkeypatch.setenv('ALICA_QUASAR_RULES_DIR', str(tmp_path))
    assert shipped_direct_replace() == []
    assert effective_direct_replace([rule('a', 'b')]) == [rule('a', 'b')]


def test_supported_generation_constant():
    assert SUPPORTED_CONFIG_GENERATION == 'old'


@pytest.mark.parametrize('marker', ['requested_owner', 'io_client', 'switch_strategy'])
def test_config_generation_detects_each_marker(marker):
    assert config_generation(new_generation(marker)) == 'new'


def test_config_generation_old_and_unexpected():
    old = {'status': 'ok', 'config': {'system_config': {'centaur': {
        'audio_config': {'vqe_metrics_enabled': True},
        'meson': {'quarkIntegrationVersion': 9, 'switchStrategy': 'PROCESS_RESTART_AWAIT'}}}}}
    assert config_generation(old) == 'old'
    assert config_generation({'status': 'ok'}) is None  # без config — форма неожиданна
    assert config_generation({'status': 'ok', 'config': {'system_config': {}}}) is None


def test_config_generation_on_live_fixture():
    assert config_generation(FIXTURE) == 'new'


async def test_new_device_gets_clean_answer_and_state(tmp_path, monkeypatch):
    use_package_rules(monkeypatch)
    reply = json.dumps(FIXTURE, ensure_ascii=False, separators=(',', ':')).encode()
    runtime, upstream_runner, runner, url = await start(tmp_path, reply=reply)
    try:
        status, _headers, body = await sync(url)
        assert status == 200
        sent = json.loads(body)
        cent = centaur(sent)
        assert cent['runtime_owner'] == {'config_revision': 1, 'reboot_policy': {}, 'requested_owner': None}
        assert cent['meson'] == {'nightIntervalEnd': 6, 'nightIntervalStart': 2,
                                 'quarkIntegrationVersion': 9, 'switchStrategy': 'PROCESS_RESTART_AWAIT'}
        last = state_of(runtime)['quasar_direct_last']
        assert last['replaced'] == 4 and last['body_sent'] == sent
        assert state_of(runtime)['upstream_config_generation'] == 'new'
        await runtime.server_log.flush()
        entries = (await runtime.server_log.query(source='quasar'))['items']
        assert any('нового поколения' in e['message'] for e in entries)
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_device_rules_apply_after_defaults(tmp_path, monkeypatch):
    use_package_rules(monkeypatch)
    reply = json.dumps(FIXTURE, ensure_ascii=False, separators=(',', ':')).encode()
    own = [rule('"requested_owner":null', '"requested_owner":"ru.yandex.quasar.centaur_app"')]
    runtime, upstream_runner, runner, url = await start(tmp_path, rules=own, reply=reply)
    try:
        status, _headers, body = await sync(url)
        assert status == 200
        sent = json.loads(body)
        owner = centaur(sent)['runtime_owner']
        assert owner['requested_owner'] == 'ru.yandex.quasar.centaur_app'  # правило устройства сильнее
        assert owner['reboot_policy'] == {}  # поставляемая защита действует
        assert state_of(runtime)['quasar_direct_last']['replaced'] == 5
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_old_generation_passes_untouched(tmp_path, monkeypatch):
    use_package_rules(monkeypatch)
    old = {'status': 'ok', 'config': {'system_config': {'centaur': {
        'audio_config': {'vqe_metrics_enabled': True}}}}}
    reply = json.dumps(old, ensure_ascii=False, separators=(',', ':')).encode()
    runtime, upstream_runner, runner, url = await start(tmp_path, reply=reply)
    try:
        status, _headers, body = await sync(url)
        assert status == 200 and json.loads(body) == old  # заменам нечего менять — байт-в-байт
        assert state_of(runtime)['upstream_config_generation'] == 'old'
        await runtime.server_log.flush()
        entries = (await runtime.server_log.query(source='quasar'))['items']
        assert not any('нового поколения' in e['message'] for e in entries)
    finally:
        await stop(runtime, upstream_runner, runner)
