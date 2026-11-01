"""media.player — «следующий/предыдущий/продолжи/что играет»: очередь плеера эмулятора."""
async def handle(ctx):
    text = (ctx.text or "").lower().replace("ё", "е")
    player = ctx.media
    if "что" in text or "какая" in text or "какой" in text:
        return player.now_playing() or {"text": "Сейчас ничего не играет.", "card": False}
    if "продолж" in text:
        reply = player.resume()
        return reply or {"text": "Ничего не играет — включите музыку или сказку.", "card": False}
    if text.startswith("дальше") or "следующ" in text:
        reply = player.step(+1)
        return reply or {"text": "Это последний трек в очереди.", "card": False}
    if text.startswith("назад") or "предыдущ" in text:
        reply = player.step(-1)
        return reply or {"text": "Это первый трек в очереди.", "card": False}
    return silent()
