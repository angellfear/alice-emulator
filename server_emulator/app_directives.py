"""Штатные директивы приложений колонки — сборка по образцам.

Запуск ютуба на станции с экраном — webview-карта: настоящий сервер на «открой ютуб»
(.85 «Прозрачно», запись 2026-11-10) отвечает тихим ответом с tts_play_placeholder +
divui_show_view (TDivUIShowViewDirective, Div2Card YouTubeWebviewData: webview
m.youtube.com с youtube_inject). Карта — константы формы в app_cards/youtube.json;
<uuid> в ней заменяется свежим request_id при сборке. Для прочих приложений образца
запуска нет — сценарий отвечает «найдено» без запуска.
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
