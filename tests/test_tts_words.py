"""«Знаки и числа произносить словами» (галочка voice_stack.speak_words):
подготовка текста только в тракте TTS — числа num2words, знаки таблицей замен,
разметка <speaker>/<[…]> не тронута, ключ кеша — по подготовленному тексту."""
import asyncio

import numpy as np
import pytest

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
    assert spoken_words("Температура −5°, ветер 3 м/с") ==         "Температура минус пять градусов, ветер три метра в секунду"
    assert spoken_words("Прогноз 3,5 метра и 15%") ==         "Прогноз три целых пять десятых метра и пятнадцать процентов"
    assert spoken_words("Курс 92.4") == "Курс девяносто две целых четыре десятых"


@pytest.mark.parametrize("number, word", [
    (1, "градус"), (2, "градуса"), (5, "градусов"), (11, "градусов"), (21, "градус"), (22, "градуса"),
    (100, "градусов"), (101, "градус"),
])
def test_degree_declension(number, word):
    assert spoken_words(f"{number}°").endswith(" " + word)
    assert spoken_words(f"{number} °C") == spoken_words(f"{number}°").replace(word, word + " Цельсия")


def test_degree_forms():
    assert spoken_words("20°") == "двадцать градусов"
    assert spoken_words("20°C") == "двадцать градусов Цельсия"
    assert spoken_words("20 °С") == "двадцать градусов Цельсия"  # кириллическая С
    assert spoken_words("21°F") == "двадцать один градус по Фаренгейту"
    assert spoken_words("+2°") == "плюс два градуса"
    assert spoken_words("-5°") == "минус пять градусов"
    assert spoken_words("−5°C") == "минус пять градусов Цельсия"
    assert spoken_words("2,5°") == "две целых пять десятых градуса"
    assert spoken_words("Сегодня +21°, ночью -2°.") == "Сегодня плюс двадцать один градус, ночью минус два градуса."
    assert spoken_words("Угол °") == "Угол градусов"  # без числа — запасное слово таблицы


def test_ranges():
    assert spoken_words("10–15 °C") == "от десяти до пятнадцати градусов Цельсия"
    assert spoken_words("+2…+5°") == "от плюс двух до плюс пяти градусов"
    assert spoken_words("1–21°") == "от одного до двадцати одного градуса"
    assert spoken_words("20–25 %") == "от двадцати до двадцати пяти процентов"


def test_time():
    assert spoken_words("в 21:00") == "в двадцать один час"
    assert spoken_words("в 7:05") == "в семь ноль пять"
    assert spoken_words("в 7:30") == "в семь тридцать"
    assert spoken_words("в 1:00") == "в один час"


def test_units_and_currency():
    assert spoken_words("12 км/ч") == "двенадцать километров в час"
    assert spoken_words("1 м/с") == "один метр в секунду"
    assert spoken_words("745 мм рт. ст.") == "семьсот сорок пять миллиметров ртутного столба"
    assert spoken_words("1013 гПа") == "одна тысяча тринадцать гектопаскалей"
    assert spoken_words("5 м²") == "пять метров в квадрате"
    assert spoken_words("1 000 000 ₽") == "один миллион рублей"
    assert spoken_words("$100") == "сто долларов"
    assert spoken_words("5€ и 21 £ и 22 руб.") == "пять евро и двадцать один фунт стерлингов и двадцать два рубля"
    assert spoken_words("5‰") == "пять промилле"
    assert spoken_words("5′ 7″") == "пять минут семь секунд"
    assert spoken_words("50 %") == "пятьдесят процентов"
    assert spoken_words("10 000") == "десять тысяч"  # группы разрядов с пробелом — одно число


def test_numeral_gender_follows_noun():
    assert spoken_words("1 минута, 21 секунда, 2 минуты") == "одна минута, двадцать одна секунда, две минуты"
    assert spoken_words("1 час, 2 часа, 21 окно") == "один час, два часа, двадцать одно окно"


def test_fractions_powers_dates():
    assert spoken_words("1½") == "один и одна вторая"
    assert spoken_words("¼ и ¾ и ⅓ и ⅔") == "одна четвёртая и три четвёртых и одна треть и две трети"
    assert spoken_words("м³") == "м в кубе"
    assert spoken_words("5/10") == "пять дробь десять"
    assert spoken_words("13.11.2026") == "тринадцать одиннадцать две тысячи двадцать шесть"


@pytest.mark.parametrize("sign, word", [
    ("≈", "примерно"), ("~", "примерно"), ("±", "плюс-минус"), ("≥", "больше или равно"),
    ("≤", "меньше или равно"), ("≠", "не равно"), ("×", "умножить на"), ("÷", "разделить на"),
    ("→", "стрелка"), ("§", "параграф"), ("@", "собака"), ("*", "звёздочка"), ("^", "в степени"),
    ("\\", "обратный слэш"), ("/", "слэш"), ("£", "фунтов"), ("′", "минут"), ("″", "секунд"),
    ("‰", "промилле"), ("−", "минус"), ("&", "и"), ("#", "решётка"), ("№", "номер"),
])
def test_every_sign_has_words(sign, word):
    assert spoken_words(f"a {sign} b") == f"a {word} b"


def test_pause_and_blank_signs():
    assert spoken_words("a | b") == "a, b"
    assert spoken_words("a_b") == "a b"


def test_decor_removed():
    assert spoken_words("Солнечно ☀️ ✅ 🌧 • «ясно» „тест“") == "Солнечно ясно тест"
    assert spoken_words("a b c​d") == "a b cd"
    assert spoken_words("Ну… ладно") == "Ну… ладно"  # многоточие и тире — паузы, остаются
    assert spoken_words("а — б") == "а — б"


def test_signs():
    assert spoken_words("5 > 3") == "пять больше три"
    assert spoken_words("A < B") == "A меньше B"
    assert spoken_words("№5, +2 = 7") == "номер пять, плюс два равно семь"
    assert spoken_words("Цена $100 или 50 ₽, скидка 10%") ==         "Цена сто долларов или пятьдесят рублей, скидка десять процентов"


def test_english_lang():
    assert spoken_words("5 > 3", lang="en") == "five greater than three"
    assert spoken_words("50%", lang="en") == "fifty percent"
    assert spoken_words("1° and 21°C", lang="en") == "one degree and twenty-one degrees Celsius"
    assert spoken_words("7:05 and 1½ ≈ x", lang="en") == "seven oh five and one and one half approximately x"


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
        "Будильник на семь часов, минус пять градусов"


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
