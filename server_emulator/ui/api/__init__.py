"""Админ-API эмулятора: реестр доменов вместо одного замыкания (RF-02).

Домен регистрируется декоратором route(action, methods); mount_ui спрашивает
реестр ДО legacy-цепочки, поэтому домены переезжают по одному без смены
поверхности (tests/test_admin_api_surface.py фиксирует контракт). Единые
гарантии домена: список методов, write-guard (X-Emulator + same-origin),
маппинг ошибок (плохое значение → 400, недоступность провайдера → 502,
нет протокола станции → 503 setup_required).
"""
import asyncio

from aiohttp import ClientError, web

from ...schema import SetupRequired

ROUTES = {}


def route(action, methods=("GET",)):
    def deco(fn):
        ROUTES[action] = {"handler": fn, "methods": tuple(methods)}
        return fn
    return deco


def get(action):
    return ROUTES.get(action)


async def dispatch(request, runtime, app):
    """Обработка домена из реестра: методы, guard и ошибки — как в legacy-цепочке."""
    entry = ROUTES[request.match_info["action"]]
    if request.method not in entry["methods"]:
        raise web.HTTPMethodNotAllowed(request.method, list(entry["methods"]))
    if request.method != "GET":
        if request.headers.get("X-Emulator") != "1":
            raise web.HTTPForbidden()
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden()
    try:
        return await entry["handler"](request, runtime, app)
    except SetupRequired as exc:  # протокол ещё не извлечён из APK станции
        return web.json_response({"error": "setup_required", "message": str(exc)}, status=503)
    except (ValueError, KeyError, StopIteration) as exc:
        return web.json_response({"error": str(exc) or "Объект не найден"}, status=400)
    except (asyncio.TimeoutError, OSError, ClientError):
        return web.json_response({"error": "Соединение с провайдером недоступно или истекло время ожидания"}, status=502)


def guarded(handler):
    # Управление каталогом — те же гарантии, что у PUT настроек эмулятора:
    # X-Emulator + same-origin; ошибки значений → JSON 400.
    async def wrapped(request):
        if request.headers.get("X-Emulator") != "1":
            raise web.HTTPForbidden()
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden()
        try:
            return await handler(request)
        except SetupRequired as exc:
            return web.json_response({"error": "setup_required", "message": str(exc)}, status=503)
        except (ValueError, KeyError, StopIteration) as exc:
            return web.json_response({"error": str(exc) or "Объект не найден"}, status=400)
    return wrapped


from . import (actions, agent_ops, animations, catalog, endpoints, extensions, identities,  # noqa: E402,F401  (регистрация доменов)
               me, media, misses, mcp, memory, params_api, quasar_config_api, routines, scenario_drafts,
               scenarios, self_update, server_config, server_log, settings, setup, sound_cache, speech,
               status, traffic, updates, voice_stack, voice_test, ws_handlers)
