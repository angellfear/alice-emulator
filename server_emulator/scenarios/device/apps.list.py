"""Какие приложения установлены («какие приложения установлены?»): список из
внутреннего списка эмулятора (runtime.installed_apps) формулировкой настоящего сервера.

Запись .85 2026-11-10 (обмены 126116-118): сервер отвечает прогрессивным текстом
«На колонке установлены Яндекс Браузер, YouTube, Кинопоиск, Музыка и ещё несколько
приложений. Хотите про какое-нибудь узнать поподробнее?» — список у него свой.
Эмулятор берёт имена из ctx.device["apps"]; пустой список — честно о состоянии
эмулятора, промахом фраза не становится.
"""
from server_emulator.sdk import say


def _names(apps):
    names = []
    for app in apps or []:
        name = str(app.get("name") if isinstance(app, dict) else app or "").strip()
        if name and name not in names:
            names.append(name)
    return names


async def handle(ctx):
    names = _names(ctx.device.get("apps") or [])
    if not names:
        return say("Список приложений колонки пока не получен.")
    if len(names) == 1:
        return say(f"На этой колонке установлено одно приложение: {names[0]}.")
    return say("На колонке установлены " + ", ".join(names) + ".")
