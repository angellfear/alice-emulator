"""Открыть приложение: «открой ютуб», «запусти …» → поиск в приложениях колонки (TASK-025).

Логика бывшего JSON-flow. Запуск не выполняется: директива TSendAndroidAppIntentDirective
не восстановлена в схеме — сценарий только сообщает, найдено ли приложение.
"""
from server_emulator.sdk import say


async def handle(ctx):
    name = ctx.parse_rest("commands")
    if not name:
        raise ValueError("Не понял, какое приложение открыть")
    app = ctx.find_in(ctx.device.get("apps") or [], lexicon="commands", field="name")
    if app is None:
        return say(f"Приложение «{name}» не найдено.")
    return say(f"Приложение «{app['name']}» найдено. Запуск пока не поддерживается: "
               "директива запуска не восстановлена в схеме.")
