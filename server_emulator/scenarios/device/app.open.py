"""Открыть приложение: «открой ютуб», «запусти …» → поиск в приложениях колонки (TASK-025).

Найдено — и есть образец запуска (app_directives: ютуб запускается webview-картой,
запись .85 2026-11-10) — тихий ответ с картой, как настоящий сервер. Образца для
прочих приложений нет — честная карточка «найдено, запуск не поддержан».
"""
from server_emulator import app_directives
from server_emulator.sdk import say


async def handle(ctx):
    name = (ctx.slots or {}).get("app") or ctx.parse_rest("commands")
    if not name:
        raise ValueError("Не понял, какое приложение открыть")
    app = ctx.find_in(ctx.device.get("apps") or [], lexicon="commands", field="name")
    if app is None:
        return say(f"Приложение «{name}» не найдено.")
    title = app.get("name") if isinstance(app, dict) else str(app)
    items = app_directives.launch_items(title) or app_directives.launch_items(name)
    if items is not None:
        return {"text": "", "card": False, "directives": items}
    return say(f"Приложение «{title}» найдено. Запуск пока не поддерживается: "
               "директива запуска не восстановлена в схеме.")
