"""Run standalone: python server.py (from this folder), or python -m server_emulator.server."""
import argparse
import json
from pathlib import Path
import sys
if not __package__:
    import types
    __package__ = "_alice_emulator_local"
    package = types.ModuleType(__package__)
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[__package__] = package
from aiohttp import web
from .auth import auth_middleware, mount_auth
from .legacy_replay import control_response, create_app as legacy_create_app
from .runtime import Runtime, mount_ui, STANDALONE_KEY


def create_app(config=None, archive=None, runtime=None):
    if archive is not None:
        return legacy_create_app(config, archive)
    runtime = runtime or Runtime()
    # H01 (v5): standalone — всегда полная эмуляция; прокси-компонента отсутствует.
    # Переопределение в памяти: файл настроек не переписываем молча.
    runtime.settings["mode"] = "emulate"
    # B04: панель требует входа после bootstrap; трафик устройств не охраняется
    app = web.Application(middlewares=[auth_middleware(runtime.auth)], client_max_size=16*1024**2)
    app[STANDALONE_KEY] = True
    mount_auth(app, runtime.auth)
    mount_ui(app, runtime)
    # M08: планировщик таймеров. Стартуем из on_startup: create_app вызывается
    # и вне цикла событий (python -m server_emulator.server), а create_task
    # требует работающий loop.
    async def start_background(app):
        runtime.start_background()
    app.on_startup.append(start_background)
    async def close_runtime(app):
        await runtime.close()
    app.on_cleanup.append(close_runtime)
    async def dispatch(request):
        if request.path == "/" and request.host.split(":",1)[0] in ("127.0.0.1", "localhost"):
            raise web.HTTPFound("/emulator/")
        return await runtime.dispatch(request)
    app.router.add_route("*", "/{path:.*}", dispatch)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--state", type=Path, default=Path(__file__).with_name("state"))
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error("Для TLS нужны оба --cert и --key")
    tls = None
    if args.cert:
        import ssl
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.cert, args.key)
    runtime = Runtime(args.state)
    app = create_app(runtime=runtime)
    web.run_app(app,host=args.host,port=args.port,ssl_context=tls,access_log=None)

if __name__ == "__main__":
    main()
