"""Штатные директивы приложений колонки — сборка по образцам.

Запуск ютуба на станции с экраном — webview-карта: настоящий сервер на «открой ютуб»
(.85 «Прозрачно», запись 2026-11-10) отвечает тихим ответом с tts_play_placeholder +
divui_show_view (TDivUIShowViewDirective, Div2Card YouTubeWebviewData: webview
m.youtube.com с youtube_inject). Карта — константы формы в app_cards/youtube.json;
<uuid> в ней заменяется свежим request_id при сборке. Прочие приложения из списка
колонки запускаются директивой launch_app по пакету (образец «запусти телемост»),
для приложений вне списка образца запуска нет.
"""
from __future__ import annotations

import json
import random
import uuid
from pathlib import Path

from .media_directives import tts_placeholder

CARDS_DIR = Path(__file__).parent / "app_cards"

# карточка запуска для найденного образца: имя в списке приложений (как в wire —
# webview-карта YouTubeWebviewData) → файл карты формы
WEBVIEW_APPS = {
    "ютуб": "youtube.json",
    "youtube": "youtube.json",
}

# Человеческое имя по AppName колонки, когда он технический: в списке установленных .85
# приложение YouTube называется «youtube-proxy» (ru.yandex.quasar.centaur.youtube_proxy_app),
# а по образцу 2026-11-10 его открывает «открой ютуб». Поиск приложения идёт и по этому имени.
_NAME_ALIASES = (("youtube", "Ютуб"),)


def human_name(app_name):
    """Имя приложения для поиска в фразе ("" — своего AppName достаточно)."""
    key = str(app_name or "").strip().lower()
    for marker, alias in _NAME_ALIASES:
        if marker in key:
            return alias
    return ""


def app_card_resource(name):
    """Ресурс карты запуска для приложения или None (образца нет — запуск не собирается)."""
    key = str(name or "").strip().lower().replace("ё", "е")
    resource = WEBVIEW_APPS.get(key)
    return CARDS_DIR / resource if resource else None


def youtube_launch_directive():
    """divui_show_view с картой запуска ютуба: константы формы + свежий request_id
    (в образце log_id карты и request_id webview — один uuid)."""
    resource = json.loads((CARDS_DIR / "youtube.json").read_text(encoding="utf-8"))
    spec = dict(resource["directive"])
    card = json.loads(json.dumps(spec["Div2Card"], ensure_ascii=False))
    body = card["StringBody"]
    # в записи — uuid с дефисами (log_id == request_id webview); из seeded random —
    # детерминированность эталонов: случай сценария сидируется на каждый кейс
    request_id = str(uuid.UUID(int=random.getrandbits(128), version=4))
    body["card"]["log_id"] = request_id
    for state in body["card"]["states"]:
        for item in state["div"].get("items", []):
            for sub in item.get("items", []):
                if sub.get("custom_props", {}).get("request_id") == "<uuid>":
                    sub["custom_props"]["request_id"] = request_id
    card["StringBody"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    spec["Div2Card"] = card
    return {"name": "divui_show_view", "type": "client_action", "is_led_silent": True,
            "analytics_type": "divui_show_view", "proto": "NAlice.TLayeredDivUICapability.TDivUIShowViewDirective",
            "fields": spec}


def launch_items(name):
    """Стек ответа на «открой <приложение>» по образцу: tts_play_placeholder +
    divui_show_view с картой приложения. None — образца запуска нет."""
    if app_card_resource(name) is None:
        return None
    return [tts_placeholder(), youtube_launch_directive()]


# Системные экраны колонки: имя в реплике → директива открытия. Образцы записи
# .85 2026-11-12 13:56:15 («открой Браузер» → open_browser_directive) и 13:56:21
# («открой Настройки» → open_android_settings): client_action, IsLedSilent, payload
# пустой (PayloadRaw — ноль байт).
SYSTEM_APPS = {
    "браузер": "open_browser_directive",
    "яндекс браузер": "open_browser_directive",
    "настройки": "open_android_settings",
}


def action_items(name):
    """Стек ответа на «открой <системный экран>»: tts_play_placeholder + директива без
    payload (в образце tts_play_placeholder первой). None — образца нет."""
    key = str(name or "").strip().lower().replace("ё", "е")
    directive = SYSTEM_APPS.get(key)
    if directive is None:
        return None
    return [tts_placeholder(),
            {"name": directive, "type": "client_action", "is_led_silent": True}]


# Запуск приложения из InstalledAppsList по пакету — образец «запусти телемост»
# (.85 «Прозрачно», запись 2026-11-12 14:28:31): launch_app (client_action,
# IsLedSilent) с TLaunchAppDirective{PackageToLaunch, IntentAction MAIN,
# Analytics.AppLaunch{PackageName, VisibleName}}; речью сервер отвечает звук
# <speaker audio='open_sound.opus'> (тот же текст в OutputSpeech.Text); на проводе
# launch_app до tts_play_placeholder (его дописывает каркас ответа).
LAUNCH_APP_PROTO = "NAlice.TAppLaunchCapability.TLaunchAppDirective"
MAIN_INTENT = "android.intent.action.MAIN"
OPEN_SOUND_SPEECH = "<speaker audio='open_sound.opus'>"


def launch_app_directive(app):
    """launch_app по пакету из InstalledAppsList колонки; VisibleName — имя,
    как его сообщила колонка (в образце «Яндекс Телемост» с неразрывным пробелом)."""
    return {"name": "launch_app", "type": "client_action", "is_led_silent": True,
            "proto": LAUNCH_APP_PROTO,
            "fields": {"PackageToLaunch": app["package"], "IntentAction": MAIN_INTENT,
                       "Analytics": {"AppLaunch": {"PackageName": app["package"],
                                                   "VisibleName": app["name"]}}}}


# Приложения, которые сервер на этом устройстве не открывает: «открой часы»
# (запись 2026-11-12 14:28:04) — реплика «Я не могу открывать сайты и приложения
# на этом устройстве.» + simple_text карточка с ней же + divui_show_view картой
# отказа (форма — константы образца в app_cards/clock_refusal.json).
NOT_HERE_APPS = ("часы",)
NOT_HERE_TEXT = "Я не могу открывать сайты и приложения на этом устройстве."


def not_here_directive():
    """divui_show_view с картой отказа по константам образца."""
    resource = json.loads((CARDS_DIR / "clock_refusal.json").read_text(encoding="utf-8"))
    return {"name": "divui_show_view", "type": "client_action", "is_led_silent": True,
            "analytics_type": resource["analytics_type"],
            "proto": "NAlice.TLayeredDivUICapability.TDivUIShowViewDirective",
            "fields": resource["directive"]}
