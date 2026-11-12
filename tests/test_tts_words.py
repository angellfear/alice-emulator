"""«Знаки и числа произносить словами» (галочка voice_stack.speak_words):
подготовка текста только в тракте TTS — числа num2words, знаки таблицей замен,
разметка <speaker>/<[…]> не тронута, ключ кеша — по подготовленному тексту."""
import asyncio

import numpy as np

from server_emulator.audio_buffer import synthesize_cached
from server_emulator.settings_models import VoiceStackSettings
from server_emulator.spoken import spoken_words, tts_text


class FakeCache:
    """is_static получает тот же текст, что уходит в ключ кеша (audio_buffer)."""

    def __init__(self):
        self.statics = []

    def find(self, text):
        return None

    def is_static(self, text):
        self.statics.append(text)
        return False


class FakeTTS:
    def __init__(self):
        self.spoken = []

    async def synth_pcm(self, text):
        self.spoken.append(text)
        return np.array([], dtype="<f4"), 48000


class FakeRuntime:
    def __init__(self, stack=None):
        self.settings = {"voice_stack": stack or {}}
        self.tts_cache = FakeCache()
        self.tts = FakeTTS()

    def get_tts(self):
        return self.tts


def test_numbers():
    assert spoken_words("Будильник на 7 часов") == "Будильник на семь часов"
    assert spoken_words("Температура −5°, ветер 3 м/с") == \
        "Температура минус пять градусов, ветер три м/с"
    assert spoken_words("Прогноз 3,5 метра и 15%") == \
        "Прогноз три целых пять десятых метра и пятнадцать процентов"
    assert spoken_words("Курс 92.4") == "Курс девяносто две целых четыре десятых"


def test_signs():
    assert spoken_words("5 > 3") == "пять больше три"
    assert spoken_words("A < B") == "A меньше B"
    assert spoken_words("№5, +2 = 7") == "номер пять, плюс два равно семь"
    assert spoken_words("Цена $100 или 50 ₽, скидка 10%") == \
        "Цена долларов сто или пятьдесят рублей, скидка десять процентов"


def test_english_lang():
    assert spoken_words("5 > 3", lang="en") == "five greater than three"
    assert spoken_words("50%", lang="en") == "fifty percent"


def test_markup_untouched():
    text = '<speaker speed="105%">Погода на 3 дня</speaker>'
    result = spoken_words(text)
    assert result == '<speaker speed="105%">Погода на три дня</speaker>'
    assert spoken_words("<[100500]>Разведи 5 капель") == "<[100500]>Разведи пять капель"


def test_flag_off_text_unchanged():
    runtime = FakeRuntime({"speak": True, "card": True})
    text = "Температура −5° и 15%"
    assert tts_text(runtime, text) == text


def test_flag_on_prepares_text():
    runtime = FakeRuntime({"speak_words": True})
    assert tts_text(runtime, "Будильник на 7:00, −5°") == \
        "Будильник на семь:ноль, минус пять градусов"


def test_cache_key_uses_prepared_text():
    runtime = FakeRuntime({"speak_words": True})
    asyncio.run(synthesize_cached(runtime, ["Температура <speaker>−5°</speaker>"], wait=5))
    assert runtime.tts_cache.statics == ["Температура минус пять градусов"]
    assert runtime.tts.spoken == ["Температура минус пять градусов"]


def test_cache_key_without_flag_is_original():
    runtime = FakeRuntime()
    asyncio.run(synthesize_cached(runtime, ["Будильник на 7:00"], wait=5))
    assert runtime.tts_cache.statics == ["Будильник на 7:00"]
    assert runtime.tts.spoken == ["Будильник на 7:00"]


def test_voice_stack_settings_model():
    assert VoiceStackSettings().speak_words is False
    assert VoiceStackSettings(speak_words=True, speak=False).card is True
