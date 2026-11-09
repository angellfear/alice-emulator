"""Перемотка плеера («перемотай на минуту вперёд»): audio_player_rewind.

Payload — Struct {amount_ms, type: Forward|Backward} (справочник directives.md:
protos-сообщения нет, решение владельца 2026-11-07). Длительность — слоты шаблона
или разбор фразы; нет длительности — уточняющий вопрос (своё значение по умолчанию
эмулятор не выдумывает). Речи в ответе нет: tts_play_placeholder + директива,
как у штатных ответов плеера без речи (media-pause).
"""
from server_emulator.sdk import ask, say

QUESTION = "На сколько перемотать?"
PLACEHOLDER = {"name": "tts_play_placeholder", "type": "client_action", "is_led_silent": True,
               "analytics_type": "tts_play_placeholder", "payload": {"channel": "Dialog"}}


def _slots_seconds(slots):
    """Секунды из сущностей шаблона — как в timers.timer.set (_slots_seconds)."""
    fixed = slots.get("duration_fixed")
    if fixed is not None:
        return int(fixed)
    number, unit = slots.get("duration_number"), slots.get("duration_unit")
    if number is None or unit is None:
        return None
    seconds = int(number) * int(unit)
    extra_number, extra_unit = slots.get("duration_number2"), slots.get("duration_unit2")
    if extra_number is not None and extra_unit is not None:
        seconds += int(extra_number) * int(extra_unit)
    return seconds


def _direction(ctx):
    """Forward|Backward: сущность шаблона {rewind_direction}, иначе из реплики
    (phrase-триггеры слотов не дают): «назад»/«обратно» — назад, иначе вперёд."""
    direction = ctx.slots.get("rewind_direction")
    if direction in ("Forward", "Backward"):
        return direction
    text = ctx.text.lower().replace("ё", "е")
    if "назад" in text or "обратно" in text:
        return "Backward"
    if "вперед" in text or "наперед" in text:
        return "Forward"
    return None


async def handle(ctx):
    direction = _direction(ctx)
    seconds = _slots_seconds(ctx.slots)
    if seconds is None:
        seconds = ctx.parse_duration()
    if seconds is None:
        if direction is None:
            raise ValueError("Не удалось понять направление перемотки")
        ctx.state["rewind_direction"] = direction  # слоты в продолжение не переходят
        return ask(QUESTION, then="on_amount", expect="duration")
    return _rewind(direction, seconds)


async def on_amount(ctx, answer):
    seconds = ctx.parse_duration(answer)
    if seconds is None:
        raise ValueError("Не удалось понять длительность перемотки")
    direction = ctx.state.get("rewind_direction")
    if direction not in ("Forward", "Backward"):
        raise ValueError("Не удалось понять направление перемотки")
    return _rewind(direction, seconds)


def _rewind(direction, seconds):
    """Директива перемотки: тип — Forward|Backward из фразы, amount_ms — длительность."""
    spec = say("")
    spec["card"] = False
    spec["directives"] = [
        PLACEHOLDER,
        {"name": "audio_player_rewind", "type": "client_action", "is_led_silent": True,
         "payload": {"amount_ms": int(seconds) * 1000, "type": direction}},
    ]
    return spec
