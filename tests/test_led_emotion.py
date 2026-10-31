"""LED-эмоции: draw_led_screen по каркасу перехвата 2026-10-24 (.227).

«Покажи глазки» → emotions/v3/joy.gif, «подмигни» → v3/wink.gif; payload
Struct, IsLedSilent, реплика с speaker-префиксом — всё как в реальном ответе.
"""
import datetime

from server_emulator.runtime import Runtime
from server_emulator.schema import load_type

JOY = "https://static-alice.s3.yandex.net/led-production/emotions/v3/joy.gif"
WINK = "https://static-alice.s3.yandex.net/led-production/emotions/v3/wink.gif"


def _ctx(text):
    return {"input": {"message_id": "m", "request_id": "r", "dialog_id": "dg",
                      "sn": "", "text": text},
            "clock": {"now": datetime.datetime.now()}}


def _response(sent):
    cls = load_type("server_emulator/schemas", "alice-api.pb",
                    "NAlice.NAliceApi.TServerMessage")
    for raw in sent:
        message = cls.FromString(raw[4:])
        if message.Event.WhichOneof("Event") == "AliceResponse":
            return message.Event.AliceResponse
    raise AssertionError("нет AliceResponse")


def _led_directive(response):
    for directive in response.Response.Directives:
        if directive.Name == "draw_led_screen":
            return directive
    raise AssertionError("нет draw_led_screen")


async def _say(runtime, phrase):
    sent = []

    async def send(data):
        sent.append(data)

    report = await runtime.dialog.handle_text(phrase, _ctx(phrase), "dg", send)
    return report, sent


async def test_eyes_emotion_matches_capture(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(builtin_scenarios=True)
    try:
        report, sent = await _say(runtime, "алиса покажи глазки")  # wake-слово + фраза
        assert report["handled"] and report["scenario"] == "device.led.emotion"
        response = _response(sent)
        directive = _led_directive(response)
        assert directive.Type == "client_action" and directive.IsLedSilent is True
        payload = directive.Payload
        assert payload["till_end_of_speech"] is False
        assert payload["animation_sequence"][0]["frontal_led_image"] == JOY
        speech = response.VoiceResponse.OutputSpeech.Text
        assert speech.startswith('<speaker voice="emotional_alice.gpu" emotion="calm_happy"> ')
        assert "глазки" in speech and "глазки" in response.Response.Cards[0].Text
    finally:
        await runtime.close()


async def test_wink_emotion(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update(builtin_scenarios=True)
    try:
        report, sent = await _say(runtime, "подмигни")
        assert report["handled"] and report["status"] == "finished"
        directive = _led_directive(_response(sent))
        assert directive.Payload["animation_sequence"][0]["frontal_led_image"] == WINK
    finally:
        await runtime.close()


async def test_led_finalize_adds_tts_placeholder(tmp_path):
    """Каркас реальных ответов: tts_play_placeholder добавляется к LED-ответу."""
    runtime = Runtime(tmp_path)
    runtime.settings.update(builtin_scenarios=True)
    try:
        _, sent = await _say(runtime, "покажи глазки")
        response = _response(sent)
        names = [d.Name for d in response.Response.Directives]
        assert "tts_play_placeholder" in names and "draw_led_screen" in names
    finally:
        await runtime.close()
