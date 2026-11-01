"""Тесты обработчиков эндпоинтов на Python (режим «script»)."""
import os

import pytest
from aiohttp import ClientSession

from server_emulator.endpoint_scripts import (
    ScriptRequest,
    ScriptRunner,
    encode,
    reply,
    Reply,
)
from server_emulator.endpoints import EndpointCatalog
from server_emulator.runtime import Runtime

from tests.conftest import listen
from tests.test_endpoints import endpoint


def test_upsert_script_writes_py_and_public_flags(tmp_path):
    catalog = EndpointCatalog(tmp_path)
    script = 'def handle(request):\n    return {"a": 1}\n'
    item = catalog.upsert(endpoint(id="s", mode="script"), script=script)

    py_path = catalog.script_path(item)
    assert py_path.exists()
    assert catalog.read_script("s") == script

    data = catalog.public()
    assert any(
        e.get("id") == "s" and e.get("has_script") is True for e in data["endpoints"]
    )


def test_upsert_rejects_bad_script(tmp_path):
    catalog = EndpointCatalog(tmp_path)

    # синтаксическая ошибка в скрипте
    with pytest.raises(ValueError, match="Скрипт"):
        catalog.upsert(
            endpoint(id="s", mode="script"),
            script="def handle(\n",
        )

    # режим script без кода и без файла
    with pytest.raises(ValueError):
        catalog.upsert(endpoint(id="s", mode="script"))


def test_remove_deletes_script(tmp_path):
    catalog = EndpointCatalog(tmp_path)
    script = 'def handle(request):\n    return {"a": 1}\n'
    item = catalog.upsert(endpoint(id="s", mode="script"), script=script)

    assert catalog.script_path(item).exists()
    catalog.remove("s")
    assert not catalog.script_path(item).exists()


@pytest.mark.asyncio
async def test_runner_contract(tmp_path):
    directory = tmp_path / "scripts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "s.py"

    request = ScriptRequest(
        "GET", "h", "/p", {}, {"X-YaQuasarPlatform": "yandexmidi"}, b""
    )
    emulator = object()

    # (а) async handle(request, emulator)
    path.write_text(
        'async def handle(request, emulator):\n'
        '    return {"p": request.headers.get("X-YaQuasarPlatform")}\n',
        encoding="utf-8",
    )
    result = await ScriptRunner().run(path, request, emulator)
    assert isinstance(result, Reply)
    assert result.body == {"p": "yandexmidi"}
    assert result.status == 200

    # (б) sync handle(request) -> None
    path.write_text('def handle(request):\n    return None\n', encoding="utf-8")
    result = await ScriptRunner().run(path, request, emulator)
    assert result is None

    # (в) reply с явным статусом/типом/телом
    path.write_text(
        'from server_emulator.endpoint_scripts import reply\n'
        'def handle(request):\n'
        '    return reply(b"x", status=202, content_type="text/plain")\n',
        encoding="utf-8",
    )
    result = await ScriptRunner().run(path, request, emulator)
    assert encode(result, "application/json") == (202, b"x", "text/plain", {})

    # перезапись файла с другим кодом после смены mtime -> новый результат
    t = os.stat(path).st_mtime_ns
    path.write_text('def handle(request):\n    return None\n', encoding="utf-8")
    os.utime(path, ns=(t, t + 10**9))
    result = await ScriptRunner().run(path, request, emulator)
    assert result is None


@pytest.mark.asyncio
async def test_dispatch_runs_script_and_500_on_error(tmp_path):
    from server_emulator.server import create_app

    runtime = Runtime(directory=tmp_path)
    runtime.endpoints.upsert(
        endpoint(
            id="dyn",
            host="quasar.yandex.net",
            method="GET",
            path="/dyn",
            mode="script",
        ),
        script=(
            'def handle(request):\n'
            '    return {"platform": request.headers.get("X-YaQuasarPlatform", "")}\n'
        ),
    )

    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(
                url + "/dyn",
                headers={"Host": "quasar.yandex.net", "X-YaQuasarPlatform": "yandexstation_2"},
            ) as response:
                assert response.status == 200
                body = await response.json()
                assert body == {"platform": "yandexstation_2"}

            # тот же эндпоинт, но скрипт падает с RuntimeError -> 500
            runtime.endpoints.upsert(
                endpoint(
                    id="dyn",
                    host="quasar.yandex.net",
                    method="GET",
                    path="/dyn",
                    mode="script",
                ),
                script=(
                    'def handle(request):\n'
                    '    raise RuntimeError("boom")\n'
                ),
            )
            async with client.get(
                url + "/dyn",
                headers={"Host": "quasar.yandex.net", "X-YaQuasarPlatform": "yandexstation_2"},
            ) as response:
                assert response.status == 500
                body = await response.json()
                assert body["error"] == "endpoint_script"
                assert "boom" in body["detail"]
    finally:
        await runner.cleanup()
        await runtime.close()