"""Знания по схеме «сценарий → база → плагин» (081): один сценарий-классификатор на девять
доменов (обоснование в «Итоге» задания). Сценарий только забирает запись датасета
(`ctx.dataset_record`, контракт docs/contracts/datasets.md); данные готовят плагины.
Плагина нет — `ctx.no_data_source()` (090): «Простой» — «Для этого нет источника данных.»,
«Модель/Агент» — отказ, фраза уходит модели. Запись устарела — говорим, на какое время данные.
Ответы — своими словами.
"""
from server_emulator.sdk import SDKError, format_duration, say

MAX_AGE = {"facts": None, "translate": None, "prices": 600, "places": 1800, "route": 600,
           "traffic": 300, "news": 900, "tv": 600, "recipes": None}


def _domain(text):
    """Домен по ключевым словам (порядок важен: частное раньше общего)."""
    checks = (
        ("news", ("новост",)),
        ("traffic", ("пробк", "на дорогах")),
        ("prices", ("в рублях", "курс", "сколько стоит")),
        ("translate", ("переведи", "по-английски", "по английски", "по-французски", "на французском")),
        ("recipes", ("рецепт", "приготов")),
        ("tv", ("программ", "по тв")),
        ("route", ("идти до", "долго ли ехать", "пройти", "доехать")),
        ("places", ("рядом", "поближе", "куда сходить")),
    )
    for domain, words in checks:
        if any(word in text for word in words):
            return domain
    return "facts"


def _query(text, domain):
    """Хвост фразы — предмет запроса (для плагина-источника)."""
    for marker in ("какова ", "какая ", "кто такой ", "кто такая ", "сколько калорий в ",
                   "значение имени ", "где тут рядом ", "где рядом ", "что рядом ",
                   "куда сходить ", "найди ", " поближе", "сколько идти до ",
                   "долго ли ехать до ", "как пройти в ", "как доехать в ", "какой курс ",
                   "сколько стоит ", "переведи на ", "скажи программу ", "какие рецепты есть с ",
                   "какие есть рецепты ", "найди рецепт ", "давай приготовим ", "новости ",
                   "расскажи ", "включи "):
        if marker in text:
            text = text.replace(marker, " ", 1)
    parts = text.split()
    if domain == "prices" and len(parts) > 1 and parts[-1] == "в рублях":
        parts = parts[:-2] or parts
    return " ".join(parts).strip(" .!?,") or text.strip(" .!?, ")


def _lang(text):
    for lang in ("английский", "французский", "немецкий", "испанский", "итальянский"):
        if lang[:6] in text:
            return lang
    return "английский"


def _num(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


async def handle(ctx):
    text = ctx.text.lower().strip(" .!?,")
    domain = _domain(text)
    query = _query(text, domain)
    key, args = query, None
    if domain == "translate":
        key, args = query, {"lang": _lang(text)}
    elif domain == "prices":
        key = f"good:{query}" if "стоит" in text else f"currency:{query}"
    elif domain == "places":
        args = {k: (ctx.settings.get("weather") or {}).get(k)
                for k in ("latitude", "longitude")}
    try:
        record = await ctx.dataset_record(domain, key, max_age=MAX_AGE[domain], args=args)
    except SDKError:
        return ctx.no_data_source()
    if record is None or record["value"] is None:
        return ctx.no_data_source()
    stale = f" Данные обновлены {format_duration(record['age_s'])} назад." if record["stale"] else ""
    value = record["value"]
    if domain == "facts":
        text = str(value.get("answer"))
    elif domain == "translate":
        text = f"Перевод: {value.get('text')} ({value.get('lang')})."
    elif domain == "prices":
        text = f"{value.get('unit')}: {_num(value.get('value'))}."
    elif domain == "places":
        rows = [f"{n}) {item.get('name')} — {item.get('address')}"
                for n, item in enumerate(value.get("items") or [], 1)][:3]
        text = ("Рядом: " + "; ".join(rows) + ".") if rows else "Ничего поблизости не нашлось."
    elif domain == "route":
        text = f"В пути около {_num(value.get('minutes'))} минут ({_num(value.get('km'))} км)."
    elif domain == "traffic":
        text = f"Пробки: {_num(value.get('score'))} баллов." + (f" {value['text']}" if value.get("text") else "")
    elif domain == "news":
        rows = [f"{n}) {item.get('title')} ({item.get('source')})"
                for n, item in enumerate(value.get("items") or [], 1)][:3]
        text = ("Главное: " + "; ".join(rows) + ".") if rows else "Свежих новостей нет."
    elif domain == "tv":
        rows = [f"{n}) {item.get('channel')} — {item.get('title')} в {item.get('start')}"
                for n, item in enumerate(value.get("items") or [], 1)][:3]
        text = ("Программа: " + "; ".join(rows) + ".") if rows else "Программы нет."
    else:
        steps = value.get("steps") or []
        text = (f"{value.get('title')}: ингредиенты — {', '.join(value.get('ingredients') or [])}. "
                f"Кратко: {steps[0] if steps else 'без шагов'}.")
    return say(text + stale)
