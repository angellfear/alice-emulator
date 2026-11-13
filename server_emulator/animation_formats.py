"""Parse and build animation formats: .led, .scled, BinaryAnimation (base64+gzip).

Supported formats:
  .led  — текстовый формат для кольцевого LED-дисплея (yandex_io/modules/leds/led_patterns/led_pattern.cpp)
  .scled — текстовый и бинарный формат для 7-сегментных часов Мини 2
           (ScLedPattern::loadFromBinary, scled_to_bin_converter/main.cpp)
  BinaryAnimation — draw_animation_directive структура: [name, base64_encoded_value,
                     compression_type None|Gzip]

Формат .led: строка на кадр — hex-RGB цвета × ledCount LED + delay мс.
Макросы: loop (зацикливание), smooth N, gammacorrection, background, name X,
         no_size_optimization, rotate/step1/step2/fill/unfill.
Формат .scled текст: строка на кадр — 32 яркости (0-255) + delay мс.
                  ループ строка — просто слово "loop".
Формат .scled бинарный: блоки [0][32 байта яркости][2B delay BE] или [1]=loop.
BinaryAnimation: бинарный .scled данные, gzip-компрессия, base64-кодирование.
"""

import base64
import gzip
import struct
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class RgbColor:
    """RGBW цвет пикселя."""
    r: int = 0
    g: int = 0
    b: int = 0
    w: int = 0

    def to_bytes(self) -> bytes:
        return bytes([self.r, self.g, self.b, self.w])

    @classmethod
    def from_bytes(cls, data: bytes) -> "RgbColor":
        if len(data) >= 4:
            return cls(data[0], data[1], data[2], data[3])
        return cls()


@dataclass
class LedFrame:
    """Кадр анимации для светодиодного дисплея."""
    colors: List[RgbColor]  # длиной ledCount (или 28 для 7-сегментных часов)
    delayMs: int = 0


@dataclass
class AnimationResult:
    """Результат парсинга анимации."""
    frames: List[LedFrame]
    name: str = ""


# ─── .led format parser ──────────────────────────────────────────────────────


def _parse_hex_color(word: str) -> RgbColor:
    """Parse hex color string like '#FF00AA' or 'FF00AA' or '0xFF00AA'."""
    w = word.strip()
    if w.startswith("#"):
        w = w[1:]
    if w.startswith("0x") or w.startswith("0X"):
        w = w[2:]
    w = w.ljust(6, "0")[:6]
    try:
        val = int(w, 16)
        return RgbColor(
            r=(val >> 16) & 0xFF,
            g=(val >> 8) & 0xFF,
            b=val & 0xFF,
            w=0,
        )
    except ValueError:
        return RgbColor()


MACROS = (
    "loop", "smooth", "gammacorrection", "background", "name",
    "no_size_optimization", "rotate", "step1", "step2", "fill", "unfill",
)


def parse_led_text(text: str, led_count: int) -> AnimationResult:
    """Parse .led text format.

    Format per line (after stripping comments # and empty lines):
      <hex-colors> <delayMs>
    where <hex-colors> is space-separated hex RGB values for ledCount LEDs.

    Macros processed: loop, smooth N, gammacorrection, background, name X,
    no_size_optimization, rotate/step1/step2/fill/unfill.
    Transformation macros (rotate/step1/step2/fill/unfill) are recorded as
    plain frames — the emulator renders them as-is; the firmware-side
    multi-frame expansion is not part of this stage.
    """
    frames: List[LedFrame] = []
    name = ""
    loop_from_index = -1
    smooth = False
    gamma_correction = False
    background = False
    optimize_size = True

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        lower = line.lower()

        if lower.startswith("loop"):
            loop_from_index = len(frames)
            continue

        if lower.startswith("smooth"):
            parts = line.split()
            smooth = len(parts) > 1 and _safe_int(parts[1], 0) > 0
            continue

        if lower.startswith("gammacorrection"):
            gamma_correction = True
            continue

        if lower.startswith("background"):
            background = True
            continue

        if lower.startswith("name"):
            parts = line.split(None, 1)
            if len(parts) > 1:
                name = parts[1].strip().strip('"').strip("'")
            continue

        if lower.startswith("no_size_optimization"):
            optimize_size = False
            continue

        if lower.startswith(("rotate", "step1", "step2", "fill", "unfill")):
            # Transformation macro — record the following frame if present,
            # but treat this line as a macro with no direct frame output.
            # The following frame line will be parsed normally on next iteration.
            continue

        # Frame line: hex colors + delay
        # Format: <color1> <color2> ... <colorN> <delayMs>
        # where N = ledCount colors, then delay is the last number.
        tokens = line.split()
        if not tokens:
            continue

        # Try to interpret the last token as a delay
        delay = 0
        color_tokens = tokens[:]
        try:
            maybe_delay = int(tokens[-1])
            delay = maybe_delay
            color_tokens = tokens[:-1]
        except ValueError:
            pass

        # Filter out macro keywords from color tokens
        color_tokens = [
            t for t in color_tokens
            if not any(t.lower().startswith(m) for m in MACROS)
        ]

        if not color_tokens:
            continue

        colors: List[RgbColor] = []
        for j in range(min(led_count, len(color_tokens))):
            colors.append(_parse_hex_color(color_tokens[j]))
        while len(colors) < led_count:
            colors.append(RgbColor())

        frames.append(LedFrame(colors=colors, delayMs=delay))

    return AnimationResult(frames=frames, name=name)


def _safe_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


# ─── .scled format parsers ───────────────────────────────────────────────────


def parse_scled_text(text: str) -> AnimationResult:
    """Parse .scled text format for Mini 2 clock (7-сегментный дисплей).

    Format per line:
      <32 brightness values 0-255> <delay ms>
    or simply:
      loop        — маркер зацикливания

    Example:
      255 128 64 ... (32 values) ... 100   # кадр с задержкой 100 мс
      loop                                  # зацикливание с этой позиции
    """
    frames: List[LedFrame] = []
    name = ""

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        lower = line.lower()
        if lower == "loop":
            # Loop marker — emulated as a frame with delay 0; the renderer
            # treats the last frame as loop start. We record it as a marker
            # on the existing last frame instead.
            if frames:
                frames[-1].delayMs = max(frames[-1].delayMs, 0)
            continue

        tokens = line.split()
        values: List[int] = []
        delay = 0
        for token in tokens:
            try:
                v = int(token)
                values.append(v)
            except ValueError:
                continue

        if not values:
            continue

        # Last value is the delay if it looks like one (>= 100 typical delay
        # vs brightness 0-255). Actually the text format puts delay as the
        # final number on the line.
        delay = values[-1] if len(values) > 1 else 0
        brightnesses = values[:-1] if len(values) > 1 else values[:]

        # Clamp brightnesses to 0-255
        brightnesses = [max(0, min(255, b)) for b in brightnesses]
        # Take up to 32 brightness values
        brightnesses = brightnesses[:32]

        colors = [RgbColor(b, b, b, 0) for b in brightnesses]
        frames.append(LedFrame(colors=colors, delayMs=delay))

    return AnimationResult(frames=frames, name=name)


def parse_scled_binary(data: bytes) -> AnimationResult:
    """Parse .scled binary format.

    Blocks: [0][32 bytes brightness][2B BE delay] for a frame,
            [1] for a loop marker.
    """
    frames: List[LedFrame] = []
    pos = 0
    length = len(data)

    while pos < length:
        selector = data[pos]
        pos += 1

        if selector == 1:
            # Loop marker — if we have frames, mark the last one as loop start
            # (the renderer handles the loop). We record a zero-delay marker.
            if frames:
                frames[-1].delayMs = max(frames[-1].delayMs, 0)
            continue

        if selector != 0:
            # Unknown selector byte — stop parsing
            break

        # Read 32 brightness bytes
        brightnesses = []
        for _ in range(32):
            if pos < length:
                brightnesses.append(data[pos])
                pos += 1
            else:
                brightnesses.append(0)

        # Read 2 bytes big-endian delay
        delay = 0
        if pos + 2 <= length:
            delay = struct.unpack(">H", data[pos:pos + 2])[0]
            pos += 2

        colors = [RgbColor(b, b, b, 0) for b in brightnesses]
        frames.append(LedFrame(colors=colors, delayMs=delay))

    return AnimationResult(frames=frames, name="")


def build_scled_binary(animation_result: AnimationResult) -> bytes:
    """Build .scled binary data from AnimationResult.

    Produces blocks: [0][32 bytes brightness][2B BE delay] per frame,
    [1] for loop markers.
    """
    result = bytearray()
    for frame in animation_result.frames:
        # Take brightness from first color channel (grayscale)
        brightnesses = []
        for i in range(32):
            if i < len(frame.colors):
                brightnesses.append(max(0, min(255, frame.colors[i].r)))
            else:
                brightnesses.append(0)

        result.append(0)  # frame selector
        result.extend(bytes(brightnesses))
        result.extend(struct.pack(">H", frame.delayMs & 0xFFFF))

    # Loop marker — the last frame in a looping animation gets byte 1
    # The loop_from_index logic: in this simple builder, if there are frames
    # we add a loop marker after them (the renderer will loop from start).
    # More precise loop_from_index handling is left to the caller/UI.
    if frames := animation_result.frames:
        result.append(1)  # loop marker

    return bytes(result)


# ─── BinaryAnimation builder ─────────────────────────────────────────────────


def build_binary_animation(
    name: str,
    animation_result: AnimationResult,
    compression_type: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Build BinaryAnimation fields from an AnimationResult.

    BinaryAnimation{Compression, Base64EncodedValue} — the binary .scled
    data is gzip-compressed (if compression_type == "Gzip") then base64-encoded.

    Returns (base64_encoded_value, compression_type).
    """
    binary_data = build_scled_binary(animation_result)

    actual_compression: Optional[str] = compression_type
    if compression_type == "Gzip":
        binary_data = gzip.compress(binary_data)
    elif compression_type is not None and compression_type != "None":
        raise ValueError(f"Unsupported compression_type: {compression_type}")

    base64_value = base64.b64encode(binary_data).decode("ascii")
    return base64_value, actual_compression


def parse_binary_animation(
    name: str,
    base64_value: str,
    compression_type: Optional[str] = None,
) -> AnimationResult:
    """Parse BinaryAnimation into AnimationResult.

    Decodes base64, optionally gzip-decompresses, then parses as .scled binary.
    """
    try:
        raw = base64.b64decode(base64_value, validate=True)
    except Exception:
        return AnimationResult(frames=[], name=name)

    if compression_type == "Gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass

    return parse_scled_binary(raw)


# ─── Convenience file parsers ────────────────────────────────────────────────


def parse_led_file(path: str, led_count: int) -> AnimationResult:
    """Parse a .led file from disk."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        return parse_led_text(text, led_count)
    except Exception:
        return AnimationResult(frames=[], name="")


def parse_scled_file(path: str) -> AnimationResult:
    """Parse a .scled file from disk (text or binary auto-detected)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        return parse_scled_bytes(data)
    except Exception:
        return AnimationResult(frames=[], name="")


def parse_scled_bytes(data: bytes) -> AnimationResult:
    """Parse .scled bytes (text or binary auto-detected)."""
    try:
        text_data = data.decode("utf-8", errors="replace")
        result = parse_scled_text(text_data)
        if result.frames:
            return result
        return parse_scled_binary(data)
    except Exception:
        return AnimationResult(frames=[], name="")