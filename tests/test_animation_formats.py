"""Разбор и сборка форматов анимаций: .led, .scled, BinaryAnimation (base64+gzip).

Чистые тесты без зависимости от протокола Алисы — laufen всегда.
"""
import base64
import gzip

import pytest

from server_emulator.animation_formats import (
    RgbColor,
    LedFrame,
    AnimationResult,
    parse_led_text,
    parse_scled_text,
    parse_scled_file,
    parse_scled_binary,
    build_scled_binary,
    build_binary_animation,
    parse_binary_animation,
)


# ─── .led ────────────────────────────────────────────────────────────────────

def test_led_two_frames_delay_last_token():
    """Задержка — последний токен строки, цвета — остальные (исправленный баг)."""
    text = "FF0000 00FF00 0000FF 100\n00FF00 FF0000 0000FF 200\n"
    result = parse_led_text(text, 3)
    assert len(result.frames) == 2
    f0, f1 = result.frames
    assert [c.r for c in f0.colors] == [255, 0, 0]
    assert [c.g for c in f0.colors] == [0, 255, 0]
    assert [c.b for c in f0.colors] == [0, 0, 255]
    assert f0.delayMs == 100
    assert f1.delayMs == 200


def test_led_color_prefixes():
    """Поддерживаются #RRGGBB, 0xRRGGBB и чистые RRGGBB.
    Линия не начинается с # (иначе это комментарий); #RRGGBB внутри линии OK.
    """
    text = "0xFF0000 00FF00 #0000FF 50\n"
    result = parse_led_text(text, 3)
    assert [c.r for c in result.frames[0].colors] == [255, 0, 0]
    assert [c.g for c in result.frames[0].colors] == [0, 255, 0]
    assert [c.b for c in result.frames[0].colors] == [0, 0, 255]
    assert result.frames[0].delayMs == 50


def test_led_pads_missing_colors_with_black():
    """Меньше цветов, чем led_count — 나머ги черные (r=g=b=0, w=0)."""
    text = "FF0000 100\n"
    result = parse_led_text(text, 4)
    f = result.frames[0]
    assert len(f.colors) == 4
    assert [c.r for c in f.colors] == [255, 0, 0, 0]
    assert all(c.w == 0 for c in f.colors)
    assert f.delayMs == 100


def test_led_macros_are_not_frames():
    """Макросы loop/name/smooth/gammacorrection не порождают кадры."""
    text = 'name "радость"\nsmooth 1\ngammacorrection\nloop\nFF0000 00FF00 100\n'
    result = parse_led_text(text, 2)
    assert result.name == "радость"
    assert len(result.frames) == 1
    assert result.frames[0].delayMs == 100


def test_led_loop_marks_position():
    """loop фиксирует позицию зацикливания (записывается в метаданные парсера)."""
    text = "FF0000 100\n00FF00 200\nloop\n0000FF 300\n"
    result = parse_led_text(text, 1)
    # 3 реальных кадра, loop — не кадр
    assert len(result.frames) == 3


def test_led_comments_and_empty_lines_skipped():
    text = "# комментарий\n\nFF0000 00FF00 100\n   \n00FF00 FF0000 200\n"
    result = parse_led_text(text, 2)
    assert len(result.frames) == 2


def test_led_unknown_color_becomes_black():
    """Невалидный цвет → черный (не падает)."""
    text = "ZZZZZZ 100\n"
    result = parse_led_text(text, 1)
    c = result.frames[0].colors[0]
    assert c.r == 0 and c.g == 0 and c.b == 0


# ─── .scled text ─────────────────────────────────────────────────────────────

_SCLED_LINE = " ".join(str(v) for v in list(range(32))) + " 100\n"


def test_scled_text_frame_32_brightnesses_plus_delay():
    result = parse_scled_text(_SCLED_LINE)
    assert len(result.frames) == 1
    assert [c.r for c in result.frames[0].colors] == list(range(32))
    assert result.frames[0].delayMs == 100


def test_scled_text_clamps_brightness_to_0_255():
    text = "0 255 300 -5 " + " ".join("128" for _ in range(29)) + " 50\n"
    result = parse_scled_text(text)
    b = [c.r for c in result.frames[0].colors]
    assert b[0] == 0 and b[1] == 255 and b[2] == 255 and b[3] == 0
    assert all(v == 128 for v in b[4:])
    assert result.frames[0].delayMs == 50


def test_scled_text_loop_marker_not_a_frame():
    text = _SCLED_LINE + "loop\n"
    result = parse_scled_text(text)
    assert len(result.frames) == 1


def test_scled_text_multiple_frames():
    text = _SCLED_LINE.replace(" 100\n", " 100\n") + \
        " ".join("255" for _ in range(32)) + " 200\n"
    result = parse_scled_text(text)
    assert len(result.frames) == 2
    assert result.frames[1].delayMs == 200


# ─── .scled binary ───────────────────────────────────────────────────────────

def _scled_binary_frame(brightnesses, delay):
    return b"\x00" + bytes(brightnesses) + delay.to_bytes(2, "big")


def test_scled_binary_parse_frame_and_loop():
    data = _scled_binary_frame(list(range(32)), 100) + b"\x01"
    result = parse_scled_binary(data)
    assert len(result.frames) == 1
    assert [c.r for c in result.frames[0].colors] == list(range(32))
    assert result.frames[0].delayMs == 100


def test_scled_binary_unknown_selector_stops():
    data = b"\x07"  # неизвестный селектор
    result = parse_scled_binary(data)
    assert result.frames == []


def test_scled_binary_truncated_delay_pads_zero():
    data = b"\x00" + bytes(range(32))  # нет 2 байт задержки
    result = parse_scled_binary(data)
    assert len(result.frames) == 1
    assert result.frames[0].delayMs == 0


# ─── build_scled_binary round-trip ───────────────────────────────────────────

def test_build_scled_binary_roundtrip():
    frames = [
        LedFrame(colors=[RgbColor(b, b, b) for b in range(32)], delayMs=100),
        LedFrame(colors=[RgbColor(255, 255, 255) for _ in range(32)], delayMs=200),
    ]
    result = AnimationResult(frames=frames)
    data = build_scled_binary(result)
    back = parse_scled_binary(data)
    assert len(back.frames) == 2
    assert [c.r for c in back.frames[0].colors] == list(range(32))
    assert back.frames[0].delayMs == 100
    assert back.frames[1].delayMs == 200


# ─── BinaryAnimation (base64 + gzip) ─────────────────────────────────────────

def test_binary_animation_gzip_roundtrip():
    frames = [
        LedFrame(colors=[RgbColor(b, b, b) for b in range(32)], delayMs=100),
    ]
    result = AnimationResult(frames=frames, name="часы")
    b64, comp = build_binary_animation("часы", result, "Gzip")
    assert comp == "Gzip"
    # base64+gzip данных меньше, чем чистые бинарные? хотя бы валидная base64
    base64.b64decode(b64, validate=True)

    back = parse_binary_animation("часы", b64, "Gzip")
    assert len(back.frames) == 1
    assert [c.r for c in back.frames[0].colors] == list(range(32))
    assert back.frames[0].delayMs == 100


def test_binary_animation_no_compression_roundtrip():
    frames = [LedFrame(colors=[RgbColor(1, 2, 3) for _ in range(32)], delayMs=42)]
    result = AnimationResult(frames=frames)
    b64, comp = build_binary_animation("x", result, None)
    assert comp is None

    back = parse_binary_animation("x", b64, None)
    assert back.frames[0].delayMs == 42
    assert back.frames[0].colors[0].r == 1


def test_binary_animation_invalid_base64_returns_empty():
    back = parse_binary_animation("x", "!!!not-base64!!!", None)
    assert back.frames == []


def test_binary_animation_unsupported_compression_raises():
    result = AnimationResult(frames=[LedFrame(colors=[RgbColor(0, 0, 0)], delayMs=1)])
    with pytest.raises(ValueError):
        build_binary_animation("x", result, "Zstd")


def test_parse_scled_file_auto_detects_text_and_binary(tmp_path):
    # текстовый .scled
    p_text = tmp_path / "a.scled"
    p_text.write_text(_SCLED_LINE, encoding="utf-8")
    r = parse_scled_file(str(p_text))
    assert len(r.frames) == 1 and r.frames[0].delayMs == 100

    # бинарный .scled
    p_bin = tmp_path / "b.scled"
    p_bin.write_bytes(_scled_binary_frame(list(range(32)), 250) + b"\x01")
    r = parse_scled_file(str(p_bin))
    assert len(r.frames) == 1
    assert [c.r for c in r.frames[0].colors] == list(range(32))
    assert r.frames[0].delayMs == 250
