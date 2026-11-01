"""Необработанные ошибки API — JSON с адресом; в журнале сервера — место сбоя."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from server_emulator.server_log import where
from server_emulator.ui import api


def _dispatch(action, handler):
    api.ROUTES[action] = {"handler": handler, "methods": ("GET",)}
    try:
        request = make_mocked_request("GET", f"/emulator/api/{action}", match_info={"action": action})
        return asyncio.run(api.dispatch(request, None, None))
    finally:
        api.ROUTES.pop(action, None)


def test_dispatch_unexpected_error_returns_json():
    async def boom(request, runtime, app):
        return web.json_response({"raw": b"\x00"})  # TypeError внутри json.dumps — как в журнале с Mac

    response = _dispatch("test-boom", boom)
    assert response.status == 500
    error = json.loads(response.text)["error"]
    assert "api/test-boom" in error and "TypeError" in error


def test_dispatch_http_exception_passes_through():
    async def missing(request, runtime, app):
        raise web.HTTPNotFound()

    with pytest.raises(web.HTTPNotFound):
        _dispatch("test-missing", missing)


def test_where_none():
    assert where(None) is None
    assert where(ValueError("x")) is None  # не выброшено — трейсбека нет


def test_where_frames():
    def innermost():
        raise RuntimeError("сбой")

    try:
        innermost()
    except RuntimeError as exc:
        result = where(exc)
    assert result and all(":" in item for item in result)
    assert result[-1].endswith("innermost")
