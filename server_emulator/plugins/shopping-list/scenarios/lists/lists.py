"""Список покупок: добавить, показать, удалить, очистить.

Директивы по образцам настоящего сервера (захват 2026-11-02): «Добавила молоко в ваш
список покупок.», перечисление «В вашем списке покупок сейчас лежит 3 товара: 1) молоко
2) яйца 3) хлеб», очистка «Ваш список покупок теперь пуст.» — обычный речевой ответ.
Состояние — по пользователю (ctx.data, переживает перезапуск), как у Яндекса список
живёт на сервере. Фразы объявления плагина (phrases/lists.json), сюда попадает и по
голой фразе «список покупок». Позиции — отдельные слова и перечисления через «,» и «и»
(как в корпусе: «йогурт хлеб кефир и шампунь»); формулировки свои.
"""
import re

from server_emulator.sdk import ask, say


def _items(value):
    return [str(item) for item in value] if isinstance(value, list) else []


def _enumerate(items):
    return ", ".join(f"{index}) {name}" for index, name in enumerate(items, 1))


def _word_count(count):
    """«1 товар», «2 товара», «5 товаров»."""
    if count % 10 == 1 and count % 100 != 11:
        return "1 товар" if count == 1 else f"{count} товар"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} товара"
    return f"{count} товаров"


def _requested(text):
    """Товары из запроса: перечисления через «,», « и », « а также»; каждое слово — позиция."""
    parts = re.split(r"\s+и\s+|,\s*|\s+а также\s+", text.strip(" .!?"))
    return [word for part in parts for word in part.split() if word]


def _after_marker(text, markers):
    for marker in markers:
        if marker in text:
            return text[text.index(marker) + len(marker):]
    return ""


def handle(ctx):
    text = ctx.text.lower().strip(" .!?")
    data = ctx.data
    items = _items(data.get("items"))

    if "удал" in text and "из" in text:
        rest = _after_marker(text, ("из списка покупок", "из покупок", "из списка", "из"))
        name = rest.strip(" .!?")
        if not name:
            return ask("Что убрать из списка? Назовите товар.", then="on_remove")
        return _remove(data, items, name)

    if "очист" in text or ("весь" in text and ("убер" in text or "удал" in text)):
        if not items:
            return say("Список покупок и так пуст.")
        data.set("items", [])
        return say("Ваш список покупок теперь пуст.")

    if ("покаж" in text or "прочит" in text or "прочт" in text
            or text == "список покупок" or "что в" in text or "что купить" in text):
        if not items:
            return say("Ваш список покупок пуст.")
        return say(f"В вашем списке покупок сейчас лежит {_word_count(len(items))}: {_enumerate(items)}.")

    rest = _after_marker(text, ("добавь", "положи", "закинь"))
    for skip in ("в список покупок", "в покупках", "в список", "в покупок", "в покупки", "в покупку"):
        rest = rest.replace(skip, " ", 1)
    requested = _requested(rest)
    if not requested:
        return ask("Что добавить в список покупок?", then="on_add")
    return _add(data, items, requested)


def on_add(ctx, answer):
    """Ответ на «Что добавить в список покупок?» — колонка слушает без «Алиса»."""
    requested = _requested(str(answer).lower())
    if not requested:
        return say("Не расслышала, что добавить.")
    return _add(ctx.data, _items(ctx.data.get("items")), requested)


def on_remove(ctx, answer):
    """Ответ на «Что убрать из списка?»."""
    name = str(answer).lower().strip(" .!?")
    if not name:
        return say("Не расслышала, что убрать.")
    return _remove(ctx.data, _items(ctx.data.get("items")), name)


def _remove(data, items, name):
    kept = [item for item in items if item.lower() != name]
    if len(kept) == len(items):
        return say(f"В списке покупок нет «{name}».")
    data.set("items", kept)
    return say(f"Убрала «{name}» из списка покупок.")


def _add(data, items, requested):
    known = [item.lower() for item in items]
    added = [name for name in requested if name.lower() not in known]
    if not added:
        return say("Эти товары уже в списке покупок.")
    items.extend(added)
    data.set("items", items)
    listed = ", ".join(added) if len(added) == 1 else ", ".join(added[:-1]) + " и " + added[-1]
    return say(f"Добавила {listed} в ваш список покупок.")
