"""Открыть приложение: «открой ютуб», «запусти …» → поиск в приложениях колонки.

Список приложений приходит от колонки в каждом запросе (ctx.device["apps"] —
TAppLaunchCapability.InstalledAppsList, cap 39); пока его нет, ответ честный: список
не получен. Названного приложения нет в списке — «нет среди установленных».
Ищем по AppName колонки и по человеческому имени приложения (app_directives.human_name:
«youtube-proxy» — это ютуб).

Найдено — и есть образец запуска (app_directives: ютуб запускается webview-картой,
запись .85 2026-11-10) — тихий ответ с картой, как настоящий сервер. Системные экраны
(«открой Браузер» → open_browser_directive, «открой Настройки» → open_android_settings,
запись .85 2026-11-12 13:56:15/13:56:21: client_action без payload) — открываются и без
поиска в списке: колонка понимает директиву сама. Прочие приложения из списка колонки —
launch_app по пакету (образец «запусти телемост», запись .85 2026-11-12 14:28:31):
речью — звук <speaker audio='open_sound.opus'>. «открой часы» — отказ сервера
«Я не могу открывать сайты и приложения на этом устройстве.» + divui-карта (14:28:04).
«открой будильник» — экран списка будильников (14:28:11); если будильников нет, к
экрану реплика «У вас нет установленных будильников.» (образец — колонка без
будильников; с будильниками ответ тихий — речи в образце нет).
"""
from server_emulator import app_directives, station_state
from server_emulator.sdk import say


async def handle(ctx):
    name = (ctx.slots or {}).get("app") or ctx.parse_rest("commands")
    if not name:
        raise ValueError("Не понял, какое приложение открыть")
    key = str(name).strip().lower().replace("ё", "е")
    if key == "будильник":
        # open_screen_alarm_list_directive без payload — как в образце 14:28:11
        spec = {"card": False,
                "directives": [{"name": "open_screen_alarm_list_directive",
                                "type": "client_action", "is_led_silent": True}]}
        if not ctx.alarms:
            spec["speech"] = "У вас нет установленных будильников."
        return spec
    if key in app_directives.NOT_HERE_APPS:
        return {"text": app_directives.NOT_HERE_TEXT,
                "directives": [app_directives.not_here_directive()]}
    items = app_directives.action_items(name)
    if items is None:
        apps = station_state.user_apps(ctx.device.get("apps") or [])
        if not apps:
            return say("Список приложений колонки пока не получен.")
        named = [dict(app, alias=app_directives.human_name(app["name"])) for app in apps]
        app = ctx.find_in(named, lexicon="commands", fields=["name", "alias"])
        if app is None:
            return say(f"Приложения «{name}» нет среди установленных на этой колонке.")
        title = app.get("name") if isinstance(app, dict) else str(app)
        items = (app_directives.launch_items(title) or app_directives.launch_items(name)
                 or app_directives.action_items(title))
        if items is None:
            # приложение из InstalledAppsList — запуск по пакету (user_apps пропускает
            # только записи с пакетом); речи, кроме звука из образца, нет
            return {"speech": app_directives.OPEN_SOUND_SPEECH, "card": False,
                    "directives": [app_directives.launch_app_directive(app)]}
    return {"text": "", "card": False, "directives": items}
