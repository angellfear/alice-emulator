"""media.radio — «включи радио <имя>»: потоки из настроек (settings.media.radio)."""
from server_emulator.media import MAX_RADIO


def radio_list(ctx):
    value = (ctx.settings.get("media") or {}).get("radio") or []
    return [(item.get("name") or "", item.get("url") or "") for item in value
            if isinstance(item, dict) and item.get("url")]


async def handle(ctx):
    name = (ctx.slots or {}).get("query") or ""
    if not name:
        text = (ctx.text or "").lower().replace("ё", "е")
        for prefix in ("включи радио ", "поставь радио ", "включай радио ", "включи радио", "поставь радио"):
            if text.startswith(prefix):
                name = text[len(prefix):].strip()
                break
    stations = radio_list(ctx)
    if not name:
        names = ", ".join(n for n, _u in stations) if stations else ""
        return {"text": ("Радио: " + names + ". Назовите название станции.") if names
                else "Список радиостанций пуст — добавьте потоки во вкладке «Голос → Медиа».", "card": False}
    lowered = name.lower().replace("ё", "е")
    match = next(((n, u) for n, u in stations if lowered in n.lower().replace("ё", "е")
                  or n.lower().replace("ё", "е") in lowered), None)
    if match is None:
        return {"text": "Станции «" + name + "» нет в списке радио. Скажите: включи радио — и я перечислю.",
                "card": False}
    return ctx.media.play_radio(match[0], match[1], text="Включаю " + match[0] + ".")
