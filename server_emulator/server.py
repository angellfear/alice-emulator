"""Run standalone: python server.py (from this folder), or python -m server_emulator.server."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
if not __package__:
    import types
    __package__ = "_alice_emulator_local"
    package = types.ModuleType(__package__)
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[__package__] = package
from aiohttp import web
from . import control, server_config, server_log
from .auth import auth_middleware, mount_auth
from .control import EXIT_KEY, INFO_KEY
from .server_config import RESTART_CODE
from .legacy_replay import control_response, create_app as legacy_create_app
from .runtime import Runtime, mount_ui, STANDALONE_KEY

RUNTIME_KEY = web.AppKey("emulator_runtime", Runtime)  # фронт TLS (TASK-067) достаёт runtime из serve()


def create_app(config=None, archive=None, runtime=None, control_token=None):
    if archive is not None:
        return legacy_create_app(config, archive)
    runtime = runtime or Runtime()
    # TASK-043/044: standalone имеет собственный проход наверх (upstream) и режимы
    # маршрута по устройствам — форс «всегда эмуляция» снят.
    # B04: панель требует входа после bootstrap; трафик устройств не охраняется
    app = web.Application(middlewares=[auth_middleware(runtime.auth)], client_max_size=16*1024**2)
    app[STANDALONE_KEY] = True
    app[RUNTIME_KEY] = runtime
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
    app[INFO_KEY] = {}
    if control_token:  # TASK-045: остановка/перезапуск для трея — до общего маршрута устройств
        control.mount(app, control_token)
    async def dispatch(request):
        if request.path == "/" and request.host.split(":",1)[0] in ("127.0.0.1", "localhost"):
            raise web.HTTPFound("/emulator/")
        return await runtime.dispatch(request)
    app.router.add_route("*", "/{path:.*}", dispatch)
    return app


async def serve(app, state, config):
    """Слушатели по настройкам (основной порт с TLS или без, HTTP-порт), ожидание штатного выхода.

    Возвращает код выхода: 0 — стоп, RESTART_CODE — перезапуск (в том числе после отката настроек,
    с которыми сервер не поднялся)."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def exit_(code):
        if not done.done():
            done.set_result(code)

    app[EXIT_KEY] = exit_
    info = app[INFO_KEY]
    runtime = app.get(RUNTIME_KEY)
    errors, plan = [], []
    try:
        tls = server_config.ssl_context(state, config)
    except ValueError as exc:
        tls = None
        errors.append(f"порт {config['port']} (SSL): {exc}")
    # TASK-067: фронт TLS — слушает публичный порт сырым TCP, по SNI отдаёт соединение
    # внутреннему aiohttp-слушателю (TLS терминирует он) или ведёт туннелем к настоящему хосту
    front = None
    if config["ssl"] and tls is not None and config.get("front", True) and runtime is not None:
        from .front import Front
        front = Front(runtime)
        plan.append(("front", config["port"], tls))
    elif not config["ssl"]:
        plan.append(("http", config["port"], None))
    elif tls is not None:
        plan.append(("https", config["port"], tls))
    if config["http_port"]:
        plan.append(("http", config["http_port"], None))
    if not plan:  # SSL не настроен и HTTP выключен: основной порт без TLS, чтобы открыть панель и поправить
        plan.append(("http", config["port"], None))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        listeners = []
        for scheme, port, context in plan:
            entry = {"scheme": "https" if scheme == "front" else scheme, "host": config["host"], "port": port,
                     "ok": True, "front": scheme == "front"}
            try:
                if scheme == "front":
                    before = set(runner.addresses)
                    await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
                    internal_port = next(a[1] for a in runner.addresses if a not in before)
                    await front.start(config["host"], port, internal_port,
                                      cert_path=server_config.resolve(state, config.get("cert")),
                                      dns_servers=config.get("front_dns"))
                    runtime.client_peer = front.client_peer  # реальный адрес колонки для обработчиков
                else:
                    await web.TCPSite(runner, config["host"], port, ssl_context=context).start()
            except OSError as exc:
                entry.update(ok=False, error=str(exc), front=False)
                errors.append(f"порт {port}: {exc}")
            listeners.append(entry)
        info.update(listeners=listeners, errors=errors)
        if not all(entry["ok"] for entry in listeners) and server_config.rollback(state):
            server_log.log_event("warning", "server",
                                 "Сервер не поднялся с новыми настройками слушателей — возвращены прежние: "
                                 + "; ".join(errors))
            print("Сервер не поднялся с новыми настройками — возвращены прежние: " + "; ".join(errors), flush=True)
            return RESTART_CODE
        if not any(entry["ok"] for entry in listeners):
            server_log.log_event("error", "server", "Ни один порт не открыт: " + "; ".join(errors))
            print("Ни один порт не открыт: " + "; ".join(errors), file=sys.stderr, flush=True)
            return 1
        server_config.confirm(state)
        # TASK-068: старт — версия и слушатели (событие для «Дополнительно → Лог»)
        server_log.log_event("info", "server",
                             "Сервер запущен" + (f" (версия {info['version']})" if info.get("version") else "")
                             + " — слушаю " + ", ".join(f"{entry['scheme']}://{entry['host']}:{entry['port']}"
                                                        + (" (фронт TLS)" if entry.get("front") else "")
                                                        for entry in listeners if entry["ok"]))
        for entry in listeners:
            print(f"{'Слушаю' if entry['ok'] else 'Ошибка'} {entry['scheme']}://{entry['host']}:{entry['port']}"
                  + (" (фронт TLS)" if entry.get("front") else "")
                  + ("" if entry["ok"] else f" — {entry['error']}"), flush=True)
        for error in errors:
            if "SSL" in error:
                print("Ошибка " + error, file=sys.stderr, flush=True)
        code = await done
        # TASK-068: штатная остановка или перезапуск (настройки/обновление) — в журнал
        server_log.log_event("info", "server",
                             "Сервер перезапускается" if code == RESTART_CODE else "Сервер остановлен")
        return code
    finally:
        if front is not None:
            await front.stop()
            if runtime is not None:
                runtime.client_peer = None
        await runner.cleanup()
        await server_log.flush()  # записи о старте/остановке не теряются при выходе


def main(argv=None):
    parser = argparse.ArgumentParser(description="Эмулятор сервера Алисы. Без аргументов — настройки из state/server.json")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--http-port", type=int, help="второй порт без TLS (0 — выключен)")
    parser.add_argument("--ssl", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--state", type=Path, default=Path(__file__).with_name("state"))
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    args = parser.parse_args(argv)
    if args.cert and args.ssl is None:
        args.ssl = True
    overrides = {"host": args.host, "port": args.port, "http_port": args.http_port, "ssl": args.ssl,
                 "cert": str(args.cert) if args.cert else None, "key": str(args.key) if args.key else None}
    config, overridden = server_config.effective(server_config.load(args.state), overrides)
    runtime = Runtime(args.state)
    app = create_app(runtime=runtime, control_token=control.write_token(args.state))
    from .version import current
    app[INFO_KEY] = {"state": str(Path(args.state).resolve()), "config": config, "overridden": overridden,
                     "url": server_config.local_url(config), "launcher": bool(os.environ.get("ALICA_LAUNCHER")),
                     "version": current()["version"]}  # версию ждёт updater в /__control/status (TASK-040)
    try:
        code = asyncio.run(serve(app, args.state, config))
    except KeyboardInterrupt:
        code = 0
    if code == RESTART_CODE and not os.environ.get("ALICA_LAUNCHER"):
        # без трея сервер перезапускает себя сам — с прежней командной строкой
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    sys.exit(code)  # UPDATE_CODE (TASK-040) без self-exec: updater запустит сервер после переключения тега


if __name__ == "__main__":
    main()
