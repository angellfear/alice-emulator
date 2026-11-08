"""informers.news — «новости», «что нового» → реплика с заголовками поставщика.

Заголовки — у выбранного поставщика новостей (вид news, providers.py):
headlines(ctx, kind) → {"items": ["заголовок", ...], "note"?}; ctx — ExtensionContext
плагина (настройки свежие). Кеша/очереди у ядра нет — частоту обновления ведёт сам
поставщик. Образца ответа настоящего сервера на «новости» в журнале нет — директивы
карточки не угадываем: только речь (отметка в описании сценария; по записи карточка
добавится по образцу). Поставщика нет — честный отказ; пустая лента с note —
проносим note пользователю.
"""
from server_emulator import providers
providers.require("news", ("headlines",))  # потребитель вида объявляет требования
from server_emulator.sdk import say

MAX_HEADLINES = 3  # реплика не читает всю ленту


async def handle(ctx):
    # владелец совпавшей записи справочника — его поставщик; прямой запуск — выбранный
    entry = ctx.phrase_entry or {}
    provider = providers.for_ext("news", entry.get("ext_id")) if entry.get("ext_id")         else providers.selected("news", ctx.settings)
    try:
        result = await providers.invoke(ctx, provider, provider["headlines"], "")
    except Exception:  # noqa: BLE001 — сбой поставщика не роняет сценарий
        result = None
    if result is None:
        return {"text": "Нет данных о новостях: поставщик недоступен."}
    items = [str(item).strip() for item in (result.get("items") or [])
             if isinstance(result, dict) and str(item).strip()]
    if not items:
        note = str(result.get("note") or "").strip()
        return say(note or "Свежих новостей нет.")
    return say(". ".join(items[:MAX_HEADLINES]) + ".")
