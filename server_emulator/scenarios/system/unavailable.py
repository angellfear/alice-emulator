"""«Известная функция Алисы недоступна» (090): фразы из корпуса справки, для которых у
эмулятора нет сценария и нет источника данных (кино, видео, караоке, Лавка, шоу, диалоговые
игры…). Срабатывает только в «Простом» — в «Модели»/«Агенте» отказ (None), фраза уходит
модели. Текст — стандартный ответ simple_unavailable (правка в настройках действует).
Группа knowledge и рецепты — не сюда: это сценарии данных 081 (ctx.no_data_source()).
"""
from server_emulator import behavior
from server_emulator.sdk import say


def handle(ctx):
    if ctx.route_mode == "hybrid" or ctx.intelligence != "scenarios":
        return None  # «Вместе»: промах уходит Яндексу; «Модель/Агент»: фраза уходит модели
    reply = behavior.standard_for(ctx.settings.get("standard_replies") or [], "simple_unavailable")
    return say(reply["text"] if reply else "В простом режиме это недоступно.")
