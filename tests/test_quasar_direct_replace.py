"""TASK-063: «Напрямую» — замены regexp в ответе Яндекса.

Только локальные серверы: «Яндекс» — локальная заглушка (как в tests/test_quasar_source.py).
"""
import gzip
import json

from aiohttp import ClientSession, web

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen
from tests.test_quasar_source import HEADERS, alias_upstream, registry, state_of

API = {"X-Emulator": "1"}
BODY = {"status": "ok", "config": {"name": "yandex-station", "volume": 5, "tags": ["a", "b"]}}


async def start(tmp_path, rules=None, reply=None, compress=False):
    """Эмулятор с устройством в режиме «Напрямую» и заглушкой Яндекса; reply — готовые байты тела."""
    runtime = Runtime(tmp_path)
    registry(runtime)
    state = dict(state_of(runtime), quasar_source="direct")
    if rules is not None:
        state["quasar_direct_replace"] = rules
    runtime.identities.mutate({"operation": "state", "sn": "SN-Q", "state": state})
    payload = reply if reply is not None else json.dumps(BODY, ensure_ascii=False).encode()

    async def handler(request):
        if compress:
            return web.Response(body=gzip.compress(payload), content_type="application/json",
                                headers={"Content-Encoding": "gzip"})
        return web.Response(body=payload, content_type="application/json")

    app = web.Application()
    app.router.add_route("*", "/{p:.*}", handler)
    upstream_runner, upstream_url = await listen(app)
    alias_upstream(runtime, int(upstream_url.rsplit(":", 1)[1]))
    runner, url = await listen(create_app(runtime=runtime))
    return runtime, upstream_runner, runner, url


async def stop(runtime, upstream_runner, runner):
    await upstream_runner.cleanup()
    await runner.cleanup()
    await runtime.close()


def rule(pattern, replace, enabled=True):
    return {"enabled": enabled, "pattern": pattern, "replace": replace}


async def sync(url):
    async with ClientSession() as client:
        async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
            return response.status, dict(response.headers), await response.read()


async def test_enabled_rule_changes_response_disabled_does_not(tmp_path):
    rules = [rule('"volume": 5', '"volume": 9'), rule('yandex-station', 'nope', enabled=False)]
    runtime, upstream_runner, runner, url = await start(tmp_path, rules)
    try:
        status, _headers, body = await sync(url)
        sent = json.loads(body)
        assert status == 200 and sent["config"]["volume"] == 9
        assert sent["config"]["name"] == "yandex-station"  # выключенное правило не сработало
        last = state_of(runtime)["quasar_direct_last"]
        assert last["replaced"] == 1
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_rules_apply_in_order_with_groups(tmp_path):
    rules = [rule(r'"name": "(\w+)-(\w+)"', r'"name": "\2-\1"'),   # yandex-station → station-yandex
             rule(r'station-yandex', 'second-pass'),                 # видит результат предыдущего правила
             rule(r'(?P<n>"volume": )5', r'\g<n>7')]
    runtime, upstream_runner, runner, url = await start(tmp_path, rules)
    try:
        _status, _headers, body = await sync(url)
        sent = json.loads(body)
        assert sent["config"]["name"] == "second-pass" and sent["config"]["volume"] == 7
        assert state_of(runtime)["quasar_direct_last"]["replaced"] == 3
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_gzip_upstream_gives_plain_body_with_valid_length(tmp_path):
    runtime, upstream_runner, runner, url = await start(tmp_path, [rule('"volume": 5', '"volume": 10')], compress=True)
    try:
        # raw-ответ: aiohttp-клиент сам распаковал бы gzip, а нужно проверить заголовки как есть
        async with ClientSession(auto_decompress=False) as client:
            async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
                raw = await response.read()
                assert "Content-Encoding" not in response.headers
                assert int(response.headers["Content-Length"]) == len(raw)
        assert json.loads(raw)["config"]["volume"] == 10  # несжатый изменённый
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_unchanged_response_is_byte_exact_gzip(tmp_path):
    """Правило ничего не нашло — колонке уходит оригинал (сжатый) без изменений."""
    runtime, upstream_runner, runner, url = await start(tmp_path, [rule('no-such-text', 'x')], compress=True)
    try:
        async with ClientSession(auto_decompress=False) as client:
            async with client.get(url + "/get_sync_info?device_id=q-id", headers=HEADERS) as response:
                raw = await response.read()
                assert response.headers["Content-Encoding"] == "gzip"
        assert gzip.decompress(raw) == json.dumps(BODY, ensure_ascii=False).encode()
        assert "replaced" not in state_of(runtime)["quasar_direct_last"]
        assert "body_sent" not in state_of(runtime)["quasar_direct_last"]
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_rule_breaking_json_sends_original_and_logs_error(tmp_path):
    rules = [rule('"volume": 5', '"volume": 6'), rule(r'"config": \{', '')]  # теряется скобка объекта
    runtime, upstream_runner, runner, url = await start(tmp_path, rules)
    try:
        status, _headers, body = await sync(url)
        assert status == 200 and json.loads(body) == BODY  # исходный ответ Яндекса
        errors = [item["error"] for item in runtime.recent if item.get("kind") == "quasar_replace"]
        assert len(errors) == 1 and "сломали JSON" in errors[0] and "2" in errors[0]
        last = state_of(runtime)["quasar_direct_last"]
        assert last["body"] == BODY and "body_sent" not in last
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_put_invalid_regex_returns_400_and_keeps_state(tmp_path):
    runtime, upstream_runner, runner, url = await start(tmp_path, [rule("keep", "me")])
    try:
        async with ClientSession() as client:
            async with client.put(url + "/emulator/api/quasar_source", headers=API,
                                  json={"sn": "SN-Q", "replace": [rule("ok", "x"), rule("(unclosed", "y")]}) as r:
                assert r.status == 400
                assert "Строка 2" in (await r.json())["error"]
            async with client.put(url + "/emulator/api/quasar_source", headers=API,
                                  json={"sn": "SN-Q", "replace": [rule("(a)", r"\2")]}) as r:
                assert r.status == 400  # группы 2 нет — ошибка шаблона замены
            async with client.put(url + "/emulator/api/quasar_source", headers=API,
                                  json={"sn": "SN-Q", "replace": [rule("", "x")]}) as r:
                assert r.status == 400
            async with client.put(url + "/emulator/api/quasar_source", headers=API,
                                  json={"sn": "SN-Q", "replace": [rule("a", "b")] * 51}) as r:
                assert r.status == 400
            async with client.put(url + "/emulator/api/quasar_source", headers=API,
                                  json={"sn": "SN-Q", "replace": [rule("a" * 2001, "b")]}) as r:
                assert r.status == 400
            assert state_of(runtime)["quasar_direct_replace"] == [rule("keep", "me")]
            async with client.get(url + "/emulator/api/quasar_source?sn=SN-Q") as r:
                assert (await r.json())["replace"] == [rule("keep", "me")]
            # валидное сохранение; пустой список убирает поле; режим не затрагивается
            async with client.put(url + "/emulator/api/quasar_source", headers=API,
                                  json={"sn": "SN-Q", "replace": [rule("a", "b", enabled=False)]}) as r:
                assert r.status == 200
            assert state_of(runtime)["quasar_direct_replace"] == [rule("a", "b", enabled=False)]
            assert state_of(runtime)["quasar_source"] == "direct"
            async with client.put(url + "/emulator/api/quasar_source", headers=API,
                                  json={"sn": "SN-Q", "replace": []}) as r:
                assert r.status == 200
            assert "quasar_direct_replace" not in state_of(runtime)
            async with client.get(url + "/emulator/api/quasar_source?sn=SN-Q") as r:
                assert (await r.json())["replace"] == []
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_last_keeps_original_and_body_sent(tmp_path):
    runtime, upstream_runner, runner, url = await start(tmp_path, [rule("yandex-station", "my-station")])
    try:
        await sync(url)
        last = state_of(runtime)["quasar_direct_last"]
        assert last["body"] == BODY  # оригинал Яндекса
        assert last["body_sent"]["config"]["name"] == "my-station"
        assert last["replaced"] == 1
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/quasar_source?sn=SN-Q") as r:
                data = await r.json()
        assert data["direct_last"]["body_sent"] == last["body_sent"]
    finally:
        await stop(runtime, upstream_runner, runner)


async def test_rules_are_per_device(tmp_path):
    runtime, upstream_runner, runner, url = await start(tmp_path, [rule('"volume": 5', '"volume": 1')])
    try:
        runtime.identities.mutate({"operation": "device", "sn": "SN-2", "user_id": "u", "device_ids": ["q2-id"]})
        runtime.identities.mutate({"operation": "state", "sn": "SN-2", "state": {"quasar_source": "direct"}})
        async with ClientSession() as client:
            async with client.get(url + "/get_sync_info?device_id=q2-id",
                                  headers=dict(HEADERS, **{"X-YaQuasarDeviceId": "q2-id"})) as response:
                assert (await response.json())["config"]["volume"] == 5  # у SN-2 правил нет
        _status, _headers, body = await sync(url)
        assert json.loads(body)["config"]["volume"] == 1
    finally:
        await stop(runtime, upstream_runner, runner)
