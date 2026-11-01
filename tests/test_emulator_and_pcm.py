import gzip
import hashlib
from pathlib import Path

from aiohttp import ClientSession, web
import pytest

from server_emulator.schema import load_type
from server_emulator.server import control_response, create_app

ROOT = Path(__file__).resolve().parents[1]


def test_control_response_correlates_and_does_not_fake_voice_reply():
    cls = load_type(ROOT / 'server_emulator' / 'schemas', 'alice-api.pb', 'NAlice.NAliceApi.TClientMessage')
    message = cls()
    message.Event.Header.MessageId = 'client-id'
    message.Event.SynchronizeStateRequest.SetInParent()
    reply = control_response(b'AAPI'+message.SerializeToString())
    server = load_type(ROOT / 'server_emulator' / 'schemas', 'alice-api.pb', 'NAlice.NAliceApi.TServerMessage').FromString(reply[4:])
    assert server.Event.Header.RefMessageId == 'client-id'
    assert server.Event.WhichOneof('Event') == 'SynchronizeStateResponse'
    message.Event.VoiceInput.SetInParent()
    assert control_response(b'AAPI'+message.SerializeToString()) is None


async def test_offline_replay_preserves_gzip_matches_body_and_omits_cookies():
    packed = gzip.compress(b'{"ok":true}')

    class FixtureArchive:
        def events(self):
            yield {'id': 'fixture', 'kind': 'http', 'method': 'POST', 'upstream': 'https://rpc.alice.yandex.net/test',
                   'status': 200, 'payloads': {'request_sent': {'hash': hashlib.sha256(b'input').hexdigest()}, 'response_original': {'hash': 'response'}},
                   'headers': {'response_original': [['Content-Encoding', 'gzip'], ['Content-Type', 'application/json'], ['Set-Cookie', 'secret=value']]}}

        def payload(self, digest):
            return packed

        def close(self):
            pass

    config = {'response_role': 'response_original', 'strict_body': True, 'aliases': {'rpc.alice.yandex.net': 'rpc.alice.yandex.net'}}
    runner = web.AppRunner(create_app(config, FixtureArchive()))
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = 'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])+'/test'
    try:
        async with ClientSession(auto_decompress=False) as client:
            async with client.post(url, data=b'input', headers={'Host': 'rpc.alice.yandex.net'}) as response:
                assert response.status == 200
                assert await response.read() == packed
                assert response.headers['Content-Encoding'] == 'gzip'
                assert 'Set-Cookie' not in response.headers
            async with client.post(url, data=b'wrong', headers={'Host': 'rpc.alice.yandex.net'}) as response:
                assert response.status == 404
    finally:
        await runner.cleanup()
