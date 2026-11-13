"""Родные форматы анимаций колонок: .led (кольцо/панель/окно), .scled (часы Мини 2),
сборка бинарника для директивы draw_scled_animations.

Кадр .led — {"colors": [[r, g, b, w], …N], "delayMs": int}, кадр .scled —
{"brightnesses": [0..255 × 32], "delayMs": int}. Результат разбора —
ParsedAnimation: кадры, loop_from (индекс кадра, с которого анимация зацикливается,
-1 — без цикла), name.

.led — как LedPattern::load прошивки: строка = кадр (N цветов hex RGB или RGBW + delay мс),
`loop` — начало цикла, `name X`, макросы-префиксы кадра rotate/step1/step2/fill/unfill
(разворачиваются в серию кадров), `gradient led1 цвет led2 цвет delay`. Директивы
smooth/logsmooth/gammacorrection/background/no_size_optimization читаются и не
применяются: кадры остаются такими, как написаны.

.scled — как ScLedPattern прошивки: текст — 32 яркости (разделитель `|` игнорируется) + delay,
`loop`; бинарник — блоки [0][32 яркости][delay uint16 BE] и [1] (метка цикла перед
следующим кадром).
"""
from __future__ import annotations

import base64
import gzip
import re
import struct
from dataclasses import dataclass, field

SCLED_LEDS = 32
SCLED_DIGITS = 4          # цифры: 4 × сегменты A–G (индексы 0..27)
SCLED_SEGMENTS = 7
# 28 PLUS1, 29 PLUS2, 30 MINUS, 31 DEGREE
MAX_DELAY_MS = 0xFFFF

_HEX_COLOR = re.compile(r"(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_LED_MACROS = ("rotate", "step1", "step2", "fill", "unfill")
_LED_DIRECTIVES = ("smooth", "logsmooth", "gammacorrection", "background", "no_size_optimization")


@dataclass
class ParsedAnimation:
    frames: list = field(default_factory=list)
    loop_from: int = -1
    name: str = ""


# --- .led -------------------------------------------------------------------

def read_color(word: str) -> list[int]:
    """Цвет hex: RRGGBB или RRGGBBWW (с префиксом # / 0x) → [r, g, b, w]."""
    text = word.strip()
    if text[:1] == "#":
        text = text[1:]
    elif text[:2].lower() == "0x":
        text = text[2:]
    if not _HEX_COLOR.fullmatch(text):
        raise ValueError(f"цвет «{word}»: нужен hex RRGGBB или RRGGBBWW")
    values = [int(text[i:i + 2], 16) for i in range(0, len(text), 2)]
    return values + [0] * (4 - len(values))


def _rotated(colors: list, count: int) -> list:
    count %= len(colors)
    return colors[len(colors) - count:] + colors[:len(colors) - count] if count else list(colors)


def _gradient(led1: int, color1: list, led2: int, color2: list, count: int) -> list:
    if led1 == led2:
        raise ValueError("gradient: точки совпадают")
    if not (0 <= led1 < count and 0 <= led2 < count):
        raise ValueError(f"gradient: номера LED от 0 до {count - 1}")
    start, end, from_color, to_color = (led1, led2, color1, color2) if led1 < led2 else (led2, led1, color2, color1)
    result = [[0, 0, 0, 0] for _ in range(count)]

    def mix(percent):
        return [int(round(from_color[k] + (to_color[k] - from_color[k]) * percent)) & 0xFF for k in range(4)]

    for i in range(start, end + 1):
        result[i] = mix((i - start) / (end - start))
    i = (end + 1) % count
    while i != start:
        result[i] = mix(((count - i) + start) / (count - (end - start)))
        i = (i + 1) % count
    return result


def _expand_macro(macro: str, colors: list, delay: int) -> list:
    count = len(colors)
    black = [0, 0, 0, 0]
    frames = []

    def add(frame_colors):
        frames.append({"colors": [list(c) for c in frame_colors], "delayMs": delay})

    if macro == "rotate":
        for i in range(count):
            add(_rotated(colors, i))
    elif macro == "step1":
        for i in range(count):
            add(_rotated(colors[:i + 1] + [black] * (count - i - 1), i - 1))
        add(colors)
        for i in range(count - 1):
            add(_rotated([black] * (i + 1) + colors[:count - i - 1], i + 1))
    elif macro == "step2":
        for i in range(1, count + 1):
            add(colors[:i] + [black] * (count - i))
        for i in range(1, count):
            add([black] * i + colors[i:])
    elif macro == "fill":
        for i in range(count):
            add(colors[:i + 1] + [black] * (count - i - 1))
    elif macro == "unfill":
        for i in range(count):
            add([black] * i + colors[i:])
    return frames


def parse_led_text(text: str, led_count: int) -> ParsedAnimation:
    """Разбор .led для дисплея из led_count светодиодов; ValueError с номером строки."""
    if led_count < 1:
        raise ValueError("число LED должно быть положительным")
    result = ParsedAnimation()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        head = tokens[0].lower()
        try:
            if head == "loop":
                result.loop_from = len(result.frames)
                continue
            if head == "name":
                result.name = tokens[1] if len(tokens) > 1 else ""
                continue
            if head in _LED_DIRECTIVES:
                continue
            macro = None
            if head in _LED_MACROS:
                macro, tokens = head, tokens[1:]
            if tokens and tokens[0].lower() == "gradient":
                if len(tokens) != 6:
                    raise ValueError("gradient: led1 цвет led2 цвет delay")
                colors = _gradient(int(tokens[1]), read_color(tokens[2]), int(tokens[3]),
                                   read_color(tokens[4]), led_count)
                delay = int(tokens[5])
            else:
                if len(tokens) != led_count + 1:
                    raise ValueError(f"нужно {led_count} цветов и задержка, получено {len(tokens)} слов")
                colors = [read_color(word) for word in tokens[:led_count]]
                delay = int(tokens[-1])
            if delay < 0:
                raise ValueError("задержка не может быть отрицательной")
        except ValueError as exc:
            raise ValueError(f"строка {number}: {exc}") from None
        if macro:
            expanded = _expand_macro(macro, colors, delay)
            if result.loop_from == len(result.frames) and not expanded:
                continue
            result.frames.extend(expanded)
        else:
            result.frames.append({"colors": colors, "delayMs": delay})
    return result


def build_led_text(frames: list, loop_from: int = -1, name: str = "") -> str:
    """Кадры → текст .led (RRGGBBWW на LED, как LedPattern::getInRawFormat)."""
    lines = [f"name {name}"] if name else []
    for index, frame in enumerate(frames):
        if loop_from == index:
            lines.append("loop")
        words = ["".join(f"{int(v) & 0xFF:02x}" for v in (list(c) + [0] * 4)[:4]) for c in frame["colors"]]
        lines.append(" ".join(words + [str(int(frame["delayMs"]))]))
    return "\n".join(lines) + "\n"


# --- .scled -----------------------------------------------------------------

def _scled_frame(values: list, delay: int) -> dict:
    if len(values) != SCLED_LEDS:
        raise ValueError(f"нужно {SCLED_LEDS} яркостей, получено {len(values)}")
    if any(v < 0 or v > 255 for v in values):
        raise ValueError("яркость сегмента 0..255")
    if not 0 <= delay <= MAX_DELAY_MS:
        raise ValueError(f"задержка 0..{MAX_DELAY_MS} мс")
    return {"brightnesses": list(values), "delayMs": delay}


def parse_scled_text(text: str) -> ParsedAnimation:
    result = ParsedAnimation()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("loop"):
            result.loop_from = len(result.frames)
            continue
        try:
            numbers = [int(word) for word in line.replace("|", " ").split()]
            if len(numbers) != SCLED_LEDS + 1:
                raise ValueError(f"нужно {SCLED_LEDS} яркостей и задержка, получено {len(numbers)} чисел")
            result.frames.append(_scled_frame(numbers[:SCLED_LEDS], numbers[SCLED_LEDS]))
        except ValueError as exc:
            raise ValueError(f"строка {number}: {exc}") from None
    return result


def parse_scled_binary(data: bytes) -> ParsedAnimation:
    result = ParsedAnimation()
    position = 0
    while position < len(data):
        key = data[position]
        if key == 1:
            result.loop_from = len(result.frames)
            position += 1
            continue
        if key != 0:
            raise ValueError(f"байт {position}: ожидался 0 или 1, получен {key}")
        end = position + 1 + SCLED_LEDS + 2
        if end > len(data):
            raise ValueError("бинарник .scled оборван на середине кадра")
        values = list(data[position + 1:position + 1 + SCLED_LEDS])
        delay = struct.unpack(">H", data[end - 2:end])[0]
        result.frames.append(_scled_frame(values, delay))
        position = end
    return result


def parse_scled_bytes(data: bytes) -> ParsedAnimation:
    """.scled из файла: бинарник (начинается с байта 0/1, не печатный текст) или текст."""
    return parse_scled_binary(data) if data[:1] in (b"\x00", b"\x01") else parse_scled_text(data.decode("utf-8", errors="replace"))


def build_scled_text(frames: list, loop_from: int = -1) -> str:
    lines = []
    for index, frame in enumerate(frames):
        if loop_from == index:
            lines.append("loop")
        values = frame["brightnesses"]
        digits = " ".join(
            " ".join(str(v) for v in values[d * SCLED_SEGMENTS:(d + 1) * SCLED_SEGMENTS])
            for d in range(SCLED_DIGITS))
        lines.append(f"{digits} | {' '.join(str(v) for v in values[28:])} {int(frame['delayMs'])}")
    return "\n".join(lines) + "\n"


def build_scled_binary(frames: list, loop_from: int = -1) -> bytes:
    """Кадры → бинарник ScLedPattern::loadFromBinary. Метка цикла ставится перед кадром
    loop_from; метка после последнего кадра прошивку роняет, поэтому её не пишем."""
    out = bytearray()
    for index, frame in enumerate(frames):
        if loop_from == index:
            out.append(1)
        frame = _scled_frame(list(frame["brightnesses"]), int(frame["delayMs"]))
        out.append(0)
        out.extend(bytes(frame["brightnesses"]))
        out.extend(struct.pack(">H", frame["delayMs"]))
    return bytes(out)


def build_scled_animation_payload(name: str, frames: list, loop_from: int = -1,
                                  policy: str | None = None) -> dict:
    """Payload директивы draw_scled_animations (DrawSegmentAnimationParser прошивки):
    animations[{name, base64_encoded_value, compression_type}] + animation_stop_policy.
    Без policy цикл анимации (loop_from >= 0) → RepeatLastTillNextDirective, иначе PlayOnce."""
    if not frames:
        raise ValueError("Анимация без кадров")
    binary = gzip.compress(build_scled_binary(frames, loop_from))
    if policy is None:
        policy = "RepeatLastTillNextDirective" if 0 <= loop_from < len(frames) else "PlayOnce"
    return {"animations": [{"name": name, "base64_encoded_value": base64.b64encode(binary).decode("ascii"),
                            "compression_type": "Gzip"}],
            "animation_stop_policy": policy}
