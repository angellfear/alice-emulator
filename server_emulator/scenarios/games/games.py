"""Простые игры (TASK-080): «шар судьбы», монетка, кубик — случайные ответы.

Образец (захват 2026-11-02): монетка — «Орёл.» короткой репликой; формулировки свои.
"""
import random

from server_emulator.sdk import say

PREDICTIONS = [
    "Всё сложится наилучшим образом.",
    "Скоро ждёт приятная новость.",
    "Сегодня хороший день для смелых решений.",
    "Стоит немного подождать — и всё уладится.",
    "Ближайшая неожиданность будет приятной.",
    "Ваш упорный труд скоро принесёт плоды.",
]


def handle(ctx):
    text = ctx.text.lower().strip(" .!?,")
    if "монетк" in text or "орел" in text or "орёл" in text:
        return say(random.choice(("Орёл.", "Решка.")))
    if "кубик" in text or "кость" in text:
        return say(f"На кубике выпало {random.randint(1, 6)}.")
    return say(random.choice(PREDICTIONS))
