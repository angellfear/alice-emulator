"""Открыть приложение: «открой ютуб», «запусти …» → поиск в приложениях колонки.

Список приложений приходит от колонки в каждом запросе (ctx.device["apps"] —
TAppLaunchCapability.InstalledAppsList, cap 39); пока его нет, ответ честный: список
не получен. Названного приложения нет в списке — «нет среди установленных».
Ищем по AppName колонки и по человеческому имени приложения (app_directives.human_name:
«youtube-proxy» — это ютуб).

Найдено — и есть образец запуска (app_directives: ютуб запускается webview-картой,
запись .85 2026-11-10) — тихий ответ с картой, как настоящий сервер. Для прочих
приложений образца запуска нет: директиву не выдаём, честная карточка «запуск
не поддержан».
"""
from server_emulator import app_directives, station_state
from server_emulator.sdk import say


async def handle(ctx):
    name = (ctx.slots or {}).get("app") or ctx.parse_rest("commands")
    if not name:
        raise ValueError("Не понял, какое приложение открыть")
    apps = station_state.user_apps(ctx.device.get("apps") or [])
    if not apps:
        return say("Список приложений колонки пока не получен.")
    named = [dict(app, alias=app_directives.human_name(app["name"])) for app in apps]
    app = ctx.find_in(named, lexicon="commands", fields=["name", "alias"])
    if app is None:
        return say(f"Приложения «{name}» нет среди установленных на этой колонке.")
    title = app.get("name") if isinstance(app, dict) else str(app)
    items = app_directives.launch_items(title) or app_directives.launch_items(name)
    if items is not None:
        return {"text": "", "card": False, "directives": items}
    return say(f"Приложение «{title}» найдено. Запуск пока не поддерживается: "
               "директива запуска не восстановлена в схеме.")
