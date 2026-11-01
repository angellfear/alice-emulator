"""media.player — «следующий/предыдущий/продолжи/что играет»: очередь плеера эмулятора."""
async def handle(ctx):
    player = ctx.media
    command = (ctx.slots or {}).get("player_command")
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
