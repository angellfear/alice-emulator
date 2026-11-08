"""Вид news: встроенный поставщик-заглушка и сценарий «что нового» на выбранном поставщике.

Контракт вида: headlines(ctx, kind) → {"items": ["заголовок", ...], "note"?}, ctx —
ExtensionContext плагина. Сценарий informers.news произносит заголовки репликой
(образца ответа настоящего сервера на «новости» в журнале нет — директивы не угадываем).
"""
import datetime
import importlib.util
from pathlib import Path

import pytest

import server_emulator
from server_emulator import providers
from server_emulator.runtime import Runtime

MODULE = Path(server_emulator.__file__).parent / "plugins" / "news-stub" / "news" / "news_stub.py"


def load_provider():
    """Модуль встроенного плагина как отдельный (без импорта остального кода ядра)."""
    spec = importlib.util.spec_from_file_location("builtin_news_stub", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExtCtx:
    """Минимальный ExtensionContext-двойник."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key, default=None):
        return self._values.get(key, default)


def make_runtime(tmp_path, headlines=None):
    """Runtime с владельцем и колонкой; builtin-плагины выключены, поставщик новостей —
    заглушка в реестре (если задан список заголовков)."""
    runtime = Runtime(tmp_path)
    state = runtime.settings.setdefault("extensions", {})
    for ext_id in list(state["installed"]):
        if ext_id.startswith("builtin."):
            state["enabled"][ext_id] = False
    runtime.extensions.load_enabled()
    runtime.identities.mutate({"operation": "user", "id": "owner", "name": "O"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-N", "user_id": "owner",
                               "name": "Станция"})
    if headlines is not None:
        async def headlines_fn(ctx, kind):
            return {"items": list(headlines)}

        providers.PROVIDERS["news"] = {"golden-news": {
            "id": "golden-news", "title": "Новости (заглушка)", "service": "",
            "kind": "news", "ext_id": "golden-news", "origin": "builtin", "file": "",
            "headlines": headlines_fn}}
    return runtime


async def _say(runtime, text):
    ctx = {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg", "sn": "SN-N",
                     "text": text},
           "clock": {"now": datetime.datetime(2026, 10, 27, 21, 7)}, "sequence": 1,
           "route_mode": "emulate", "player_owner": None,
           **runtime._scenario_context(sn="SN-N")}
    return await runtime.dialog.handle_text(text, ctx, "dg", None, deliver=False)


async def test_news_stub_provider_returns_fixed_headlines():
    """Заглушка без сети: фиксированные заголовки из самого плагина."""
    module = load_provider()
    result = await module.headlines(ExtCtx(), "top")
    items = result["items"]
    assert 2 <= len(items) <= 3 and all(str(x).strip() for x in items)


async def test_builtin_news_stub_in_registry(tmp_path):
    """Свежий Runtime: builtin.news-stub в реестре вида news; вкладка «Новости» есть."""
    runtime = Runtime(tmp_path)
    try:
        records = providers.PROVIDERS["news"]
        assert "builtin.news-stub" in records
        assert records["builtin.news-stub"]["origin"] == "builtin"
        assert callable(records["builtin.news-stub"]["headlines"])
        assert providers.selected("news", runtime.settings)["id"] == "builtin.news-stub"
    finally:
        await runtime.close()


async def test_news_speaks_provider_headlines(tmp_path):
    """«Что нового»: реплика — заголовки выбранного поставщика, директив плеера нет."""
    runtime = make_runtime(tmp_path, ["Событие одно", "Событие два"])
    try:
        report = await _say(runtime, "что нового")
        assert report["handled"], report
        assert report["scenario"] == "informers.news"
        assert "Событие одно." in report["speech"] and "Событие два." in report["speech"]
        report = await _say(runtime, "расскажи новости")  # phrase-триггер по contains
        assert report["scenario"] == "informers.news"
    finally:
        await runtime.close()


async def test_news_empty_items_speaks_note(tmp_path):
    """Пустая лента с note — произносим note поставщика; без note — «Свежих новостей нет.»."""
    runtime = make_runtime(tmp_path)

    async def empty(ctx, kind):
        return {"items": [], "note": "Лента пока не отвечает."}

    providers.PROVIDERS["news"] = {"golden-news": {
        "id": "golden-news", "title": "Новости (заглушка)", "service": "",
        "kind": "news", "ext_id": "golden-news", "origin": "builtin", "file": "",
        "headlines": empty}}
    try:
        report = await _say(runtime, "что нового")
        assert report["speech"] == "Лента пока не отвечает."

        async def silent(ctx, kind):
            return {"items": []}

        providers.PROVIDERS["news"]["golden-news"]["headlines"] = silent
        report = await _say(runtime, "что нового")
        assert report["speech"] == "Свежих новостей нет."
    finally:
        await runtime.close()


async def test_news_without_provider_no_data_source(tmp_path):
    """Поставщика новостей нет — честный ответ «нет источника данных» («Простой»)."""
    runtime = make_runtime(tmp_path)
    try:
        runtime.settings["intelligence"] = "scenarios"
        report = await _say(runtime, "что нового")
        assert report["handled"], report
        assert report["speech"] == "Для этого нет источника данных."
    finally:
        await runtime.close()


async def test_news_provider_failure_is_honest(tmp_path):
    """Сбой поставщика — честный отказ вместо сбоя сценария."""
    runtime = make_runtime(tmp_path)

    async def broken(ctx, kind):
        raise ConnectionError("лента недоступна")

    providers.PROVIDERS["news"] = {"golden-news": {
        "id": "golden-news", "title": "Новости (заглушка)", "service": "",
        "kind": "news", "ext_id": "golden-news", "origin": "builtin", "file": "",
        "headlines": broken}}
    try:
        report = await _say(runtime, "что нового")
        assert report["handled"], report
        assert report["speech"] == "Нет данных о новостях: поставщик недоступен."
    finally:
        await runtime.close()
