"""Вход аккаунта поставщика (точки login_start/login_callback/login_status).

Кнопка «Войти в Spotify»: Authorization Code + PKCE через authlib — на локальной
заглушке (токен-эндпойнт подменой константы ACCOUNTS_URL; настоящий Spotify из
тестов не дёргается). PKCE: челлендж authorize-URL = S256(verifier заявки плагина);
колбэк меняет код на токены (authlib сам валидирует state против
authorization_response и кладёт code_verifier в тело); токены — в meta state/
(ключ provider_login:<ext_id>), наружу (браузер, GET провайдеров) — только статус;
refresh — login_status при истёкшем access.
"""
import json
import time
import urllib.parse

from aiohttp import ClientSession, web

from authlib.oauth2.rfc7636 import create_s256_code_challenge

from server_emulator import providers
from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

API = {"X-Emulator": "1"}
CLIENT_ID = "test-client-id"


async def start(tmp_path):
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    await runtime.extensions.set_values("builtin.spotify", {"client_id": CLIENT_ID})
    return runtime, runner, url


async def stop(runtime, runner):
    await runner.cleanup()
    await runtime.close()


async def api_post(url, client, body):
    async with client.post(url + "/emulator/api/providers_login", json=body,
                           headers=API) as r:
        return r.status, await r.json()


async def providers_view(url, client):
    async with client.get(url + "/emulator/api/providers") as r:
        assert r.status == 200
        return await r.json()


def spotify_record():
    return providers.PROVIDERS["music"]["builtin.spotify"]


def meta_tokens(runtime):
    raw = runtime.store.meta("provider_login:builtin.spotify")
    return json.loads(raw) if raw else None


async def fake_spotify(requests):
    """Подмена accounts.spotify.com: /api/token пишет form-тело в requests и отдаёт токен."""
    async def token(request):
        form = await request.post()
        requests.append({k: form[k] for k in form})
        return web.json_response({"access_token": "AT-" + str(len(requests)),
                                  "refresh_token": "RT-" + str(len(requests)),
                                  "token_type": "Bearer", "expires_in": 3600})
    app = web.Application()
    app.router.add_post("/api/token", token)
    return await listen(app)


async def test_login_button_exposed_only_for_declaring_provider(tmp_path):
    runtime, runner, url = await start(tmp_path)
    try:
        async with ClientSession() as client:
            data = await providers_view(url, client)
            items = {p["id"]: p for p in data["kinds"]["music"]["providers"]}
            assert items["builtin.spotify"]["login"] is True
            assert items["builtin.spotify"]["status"]["login_authorized"] is False
            assert items["builtin.yandex-music"]["login"] is False  # не объявляет — кнопки нет
    finally:
        await stop(runtime, runner)


async def test_login_start_builds_pkce_url(tmp_path):
    runtime, runner, url = await start(tmp_path)
    try:
        async with ClientSession() as client:
            status, body = await api_post(url, client,
                                          {"kind": "music", "id": "builtin.spotify", "action": "start"})
            assert status == 200 and "accounts.spotify.com/authorize" in body["url"]
            params = {k: v[0] for k, v in
                      urllib.parse.parse_qs(urllib.parse.urlsplit(body["url"]).query).items()}
            assert params["response_type"] == "code" and params["client_id"] == CLIENT_ID
            assert params["redirect_uri"].startswith("http://127.0.0.1:")
            assert params["redirect_uri"].endswith("/emulator/api/providers_login_callback")
            verifier = providers.PROVIDERS["music"]["builtin.spotify"]["_module"].PENDING[params["state"]]["verifier"]
            assert params["code_challenge"] == create_s256_code_challenge(verifier)
            assert params["code_challenge_method"] == "S256"
    finally:
        await stop(runtime, runner)


async def test_login_start_without_client_id_gives_text_error(tmp_path):
    runtime, runner, url = await start(tmp_path)
    await runtime.extensions.set_values("builtin.spotify", {"client_id": ""})
    try:
        async with ClientSession() as client:
            status, body = await api_post(url, client,
                                          {"kind": "music", "id": "builtin.spotify", "action": "start"})
            assert status == 502 and "Client ID" in body["error"]
    finally:
        await stop(runtime, runner)


async def test_login_callback_exchanges_code_and_stores_tokens(tmp_path, monkeypatch):
    runtime, runner, url = await start(tmp_path)
    requests = []
    token_runner, token_url = await fake_spotify(requests)
    try:
        spotify_module = spotify_record()["_module"]
        monkeypatch.setattr(spotify_module, "ACCOUNTS_URL", token_url + "/api/token")
        async with ClientSession() as client:
            _status, body = await api_post(url, client,
                                           {"kind": "music", "id": "builtin.spotify", "action": "start"})
            state = urllib.parse.parse_qs(urllib.parse.urlsplit(body["url"]).query)["state"][0]
            verifier = spotify_module.PENDING[state]["verifier"]
            challenge = urllib.parse.parse_qs(urllib.parse.urlsplit(body["url"]).query)["code_challenge"][0]
            assert challenge == create_s256_code_challenge(verifier)
            async with client.get(url + "/emulator/api/providers_login_callback",
                                  params={"code": "THE-CODE", "state": state}) as r:
                assert r.status == 200
                page = await r.text()
            assert "выполнен" in page and "THE-CODE" not in page  # код/токены в странице не светятся
            assert spotify_module.PENDING.get(state) is None  # заявка погашена колбэком
            saved = meta_tokens(runtime)
            assert saved["access_token"].startswith("AT-") and saved["refresh_token"].startswith("RT-")
            assert requests[0]["grant_type"] == "authorization_code"
            assert requests[0]["code"] == "THE-CODE"
            assert requests[0]["client_id"] == CLIENT_ID
            assert requests[0]["code_verifier"] == verifier
            assert requests[0]["redirect_uri"].endswith("/emulator/api/providers_login_callback")
            async with ClientSession() as client:
                data = await providers_view(url, client)
                item = {p["id"]: p for p in data["kinds"]["music"]["providers"]}["builtin.spotify"]
                assert item["status"]["login_authorized"] is True
    finally:
        await token_runner.cleanup()
        await stop(runtime, runner)


async def test_login_callback_error_from_spotify(tmp_path):
    runtime, runner, url = await start(tmp_path)
    try:
        async with ClientSession() as client:
            _status, body = await api_post(url, client,
                                           {"kind": "music", "id": "builtin.spotify", "action": "start"})
            state = urllib.parse.parse_qs(urllib.parse.urlsplit(body["url"]).query)["state"][0]
            async with client.get(url + "/emulator/api/providers_login_callback",
                                  params={"error": "access_denied", "state": state}) as r:
                assert r.status == 400 and "отклонил вход" in (await r.text())
            assert meta_tokens(runtime) is None
    finally:
        await stop(runtime, runner)


async def test_login_callback_unknown_state(tmp_path):
    runtime, runner, url = await start(tmp_path)
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/providers_login_callback",
                                  params={"code": "x", "state": "no-such"}) as r:
                assert r.status == 400 and "неизвестная заявка" in (await r.text())
            assert meta_tokens(runtime) is None
    finally:
        await stop(runtime, runner)


async def test_status_refreshes_expired_access(tmp_path, monkeypatch):
    runtime, runner, url = await start(tmp_path)
    requests = []
    token_runner, token_url = await fake_spotify(requests)
    try:
        runtime.store.set_meta("provider_login:builtin.spotify", json.dumps(
            {"access_token": "AT-old", "refresh_token": "RT-seeded",
             "token_type": "Bearer", "expires_at": time.time() - 10}))
        spotify_module = spotify_record()["_module"]
        monkeypatch.setattr(spotify_module, "ACCOUNTS_URL", token_url + "/api/token")
        async with ClientSession() as client:
            status, body = await api_post(url, client,
                                          {"kind": "music", "id": "builtin.spotify", "action": "status"})
            assert status == 200 and body["authorized"] is True and body["expires_in"] > 3500
            assert requests[0]["grant_type"] == "refresh_token"
            assert requests[0]["refresh_token"] == "RT-seeded"
            fresh = meta_tokens(runtime)
            assert fresh["access_token"].startswith("AT-") and fresh["access_token"] != "AT-old"
            assert fresh["refresh_token"] == "RT-1"  # ротация: refresh-токен из ответа
    finally:
        await token_runner.cleanup()
        await stop(runtime, runner)


async def test_status_without_login_is_not_authorized(tmp_path):
    runtime, runner, url = await start(tmp_path)
    try:
        async with ClientSession() as client:
            status, body = await api_post(url, client,
                                          {"kind": "music", "id": "builtin.spotify", "action": "status"})
            assert status == 200 and body["authorized"] is False
    finally:
        await stop(runtime, runner)


async def test_login_unavailable_provider_gives_400(tmp_path):
    runtime, runner, url = await start(tmp_path)
    try:
        async with ClientSession() as client:
            status, body = await api_post(url, client,
                                          {"kind": "music", "id": "builtin.yandex-music", "action": "start"})
            assert status == 400 and "не объявляет вход" in body["error"]
    finally:
        await stop(runtime, runner)
