"""media.player — управление плеером эмулятора: следующий/предыдущий, продолжить,
что играет, лайк/дизлайк, скорость, «начни сначала», «другая версия», перемотка.

Перемотка (±секунды, на N минут) честно отклоняется: в образце директивы audio_play
нет поля позиции (docs/contracts/player.md) — колонке нечего отправить.
"""
from server_emulator import media as media_mod

SEEK_REPLY = {"text": "Перемотку колонка по образцу директивы не поддерживает — скажите «начни сначала».",
              "card": False}


async def handle(ctx):
    player = ctx.media
    slots = ctx.slots or {}
    command = slots.get("player_command")
    if command == "restart":
        return player.restart() or {"text": "Сейчас ничего не играет.", "card": False}
    if command == "repeat":
        return player.repeat() or {"text": "Сейчас ничего не играет.", "card": False}
    if command == "another":
        return player.another_version(ctx.library) or \
            {"text": "Другой версии этого трека в библиотеке нет.", "card": False}
    if command in ("like", "unlike"):
        track = player.current()
        if not track:
            return {"text": "Сейчас ничего не играет.", "card": False}
        user = (ctx.user or {}).get("id") or ctx.sn
        media_mod.mark_track(ctx.library, user, track.get("kind") or "music",
                             track.get("relative", ""), "like" if command == "like" else "dislike")
        if command == "unlike":  # дизлайк — пропустить и больше не предлагать
            reply = player.step(+1)
            return reply or {"text": "Ладно, больше не предложу. Очередь кончилась.", "card": False}
        return {"text": "Добавил в избранное.", "card": False}
    if "seek_dir" in slots or "duration_unit" in slots:  # «перемотай на 10 минут вперед»
        return dict(SEEK_REPLY)
    if "speed" in slots or "number" in slots:  # «замедли», «ускорь в полтора раза», «поставь скорость 2»
        reply = player.set_speed(slots.get("speed") or slots.get("number"))
        return reply or {"text": "Сейчас ничего не играет.", "card": False}
    if command == "speed_get":
        value = player.get_speed()
        return {"text": f"Скорость воспроизведения {value}." if value else "Сейчас ничего не играет.",
                "card": False}
    if command is None:
        text = (ctx.text or "").lower().replace("ё", "е")
        if "что" in text or "какая" in text or "какой" in text:
            command = "what"
        elif "продолж" in text:
            command = "resume"
        elif text.startswith("дальше") or "следующ" in text:
            command = "next"
        elif text.startswith("назад") or "предыдущ" in text:
            command = "prev"
        else:
            return None  # не команда плеера — отказ, реплика следующему кандидату
    if command == "what":
        return player.now_playing() or {"text": "Сейчас ничего не играет.", "card": False}
    if command == "resume":
        reply = player.resume()
        return reply or {"text": "Ничего не играет — включите музыку или сказку.", "card": False}
    if command == "next":
        reply = player.step(+1)
        return reply or {"text": "Это последний трек в очереди.", "card": False}
    reply = player.step(-1)  # prev
    return reply or {"text": "Это первый трек в очереди.", "card": False}
