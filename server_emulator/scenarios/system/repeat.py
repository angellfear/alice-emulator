"""Повторить сказанное: последние произнесённые колонке реплики.

Журнал ведёт голосовой тракт (runtime.note_said при каждой доставке ответа);
сценарий читает его через SDK ctx.said — записи этого устройства, новыми последними.
"""

from server_emulator.sdk import say

MAX_REPEAT = 20  # за один раз повторяем не больше
NOTHING_SAY = "Я пока ничего не говорила, повторять нечего."


async def handle(ctx):
    said = ctx.said
    n = ctx.slots.get("number")  # «повтори N» — слот шаблона
    count = max(1, min(int(n), MAX_REPEAT)) if n is not None else 1  # без числа — последнее сообщение
    items = list(reversed(said[-count:]))  # от новых к старым
    if not items:
        return say(NOTHING_SAY)
    parts = [str(item["text"]) for item in items]
    # карточка — нумерованный список (1. самое свежее), озвучка — сами реплики без номеров
    text = "\n".join(f"{number}. {part}" for number, part in enumerate(parts, 1))
    return {"text": text, "card": True, "speech_parts": parts}
