"""Какие приложения установлены («какие приложения установлены?»): список из состояния
колонки (ctx.device["apps"] — TAppLaunchCapability.InstalledAppsList, cap 39), без системных
пакетов, имена — AppName колонки.

Запись .85 2026-11-10 (обмены 126116-118): сервер отвечает прогрессивным текстом
«На колонке установлены Яндекс Браузер, YouTube, Кинопоиск, Музыка и ещё несколько
приложений. Хотите про какое-нибудь узнать поподробнее?» — свой перечень он строит
не только из списка колонки (Кинопоиска и Музыки в нём нет). Эмулятор называет то,
что колонка реально прислала; пустой список — честно о состоянии, промахом фраза
не становится.
"""
from server_emulator import station_state
from server_emulator.sdk import say


def handle_names(apps):
    """Имена приложений для ответа (без системных пакетов, без повторов)."""
    names = []
    for app in station_state.user_apps(apps):
        name = app["name"]
        if name and name not in names:
            names.append(name)
    return names


async def handle(ctx):
    names = handle_names(ctx.device.get("apps") or [])
    if not names:
        return say("Список приложений колонки пока не получен.")
    if len(names) == 1:
        return say(f"На этой колонке установлено одно приложение: {names[0]}.")
    return say("На колонке установлены " + ", ".join(names) + ".")
