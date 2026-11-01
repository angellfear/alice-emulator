"""LED-эмоции: «покажи глазки», «подмигни» → анимация на LED-матрице (TASK-025).

Логика бывшего JSON-flow: колонка без экрана — отказ репликой; эмоция — по словарю
`emotions` сценария; ответ — фраза эмоции + директива draw_led_screen.
Фразы и ссылки на анимации те же, что были в таблицах `tables` сценария.
"""
from server_emulator.sdk import say

GIF = {
    "joy": "https://static-alice.s3.yandex.net/led-production/emotions/v3/joy.gif",
    "wink": "https://static-alice.s3.yandex.net/led-production/emotions/v3/wink.gif",
}
PHRASE = {
    "joy": "А вот и глазки!",
    "wink": "Научилась подмигивать специально для таких случаев.",
}
SPEECH_PREFIX = '<speaker voice="emotional_alice.gpu" emotion="calm_happy"> '


async def handle(ctx):
    if str(ctx.device.get("display") or "") == "none":
        return say("На этой колонке нет анимаций: у модели нет LED-экрана.")
    emotion = (ctx.slots or {}).get("led_emotion") or ctx.parse_lexicon("emotions")
    if emotion not in PHRASE:
        raise ValueError("Не понял, какую эмоцию показать")
    reply = say(PHRASE[emotion])
    reply["speech_prefix"] = SPEECH_PREFIX  # поля compose-reply, которых нет у say()
    reply["directives"] = [{
        "name": "draw_led_screen",
        "is_led_silent": True,
        "payload": {
            "till_end_of_speech": False,
            "animation_sequence": [{"frontal_led_image": GIF[emotion]}],
        },
    }]
    return reply
