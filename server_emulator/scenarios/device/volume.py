"""Громкость: «громче», «тише», «громкость 5», «выключи звук», «максимальная громкость».

Установка уровня (число, слово, процент) — тихий ответ директивами настоящего сервера
(журнал 2026-10-24 00:24:56, 23:07:45, 2026-10-29 22:14:24: голосовое «громкость два» →
звук без речи и карточек: sound_set_level{new_level, new_percent_level} +
tts_play_placeholder). Шкала колонки — из её состояния (capability 23 TVolumeCapability:
Level/MaxLevel/PercentLevel), процент = уровень·100/MaxLevel; своего учёта громкости на
сервере нет (главное правило tasks/PLAN-station-skills.md).

Шаги («громче/тише»), «выключи звук» и «на всю» — ответных образцов фраз в журнале нет;
шаги уровня наблюдались только как проводной Push из приложения (2026-10-23…29, TASK-025) —
прежние ветки сохранены как есть, БЛОКЕР — нужна запись.
"""
from server_emulator.sdk import say


async def push(ctx, action, **arguments):
    payload = {"action": action}
    if arguments:
        payload["arguments"] = arguments
    await ctx.action("device.push", **payload)


def set_level_reply(level, max_level=10):
    """Ответ по образцу: без речи и карточек, директива sound_set_level с обеими шкалами.

    new_level — float шкалы колонки (0..MaxLevel), new_percent_level — процент 0..100
    (образец «громкость два»: {new_level: 2.0, new_percent_level: 20.0}).
    """
    level = float(level)
    percent = round(level * 100.0 / float(max_level or 10), 1)
    return {"text": "", "card": False,
            "directives": [{"name": "sound_set_level", "type": "client_action",
                            "is_led_silent": True,
                            "payload": {"new_level": level, "new_percent_level": percent}}]}


async def handle(ctx):
    slots = ctx.slots or {}
    mode = slots.get("volume_fixed") or slots.get("volume_mode") or ctx.parse_lexicon("modes")
    max_level = (ctx.volume or {}).get("max_level") or 10  # шкала колонки из её состояния
    if mode == "mute":  # образца фразы нет — прежний проводной путь (БЛОКЕР, нужна запись)
        await push(ctx, "sound_set_level", level=0)
        return say("Звук выключен.")
    if mode == "max":
        await push(ctx, "sound_set_level", level=max_level)
        return say("Максимальная громкость.")
    if mode == "up":
        await push(ctx, "sound_louder")
        return say("Громче.")
    if mode is not None:  # любой иной режим словаря — «тише», как ветка else прежнего flow
        await push(ctx, "sound_quiter")
        return say("Тише.")
    level = slots.get("number")
    if level is not None:
        level = int(level)
    else:
        level = ctx.parse_number(minimum=0, maximum=100)
    if level is None:
        raise ValueError("Не удалось понять громкость")
    if level > max_level:  # «громкость на 50» — проценты, шкала колонки не больше MaxLevel
        level = level * max_level / 100.0
    return set_level_reply(level, max_level)
