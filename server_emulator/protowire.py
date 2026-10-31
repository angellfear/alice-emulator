"""
Модуль `protowire` — схематичный (schema‑less) кодер/декодер формата
Protocol Buffers wire format.

Поддерживается только базовый набор wire‑type’ов:
0 – varint,
1 – 64‑bit (fixed64),
2 – length‑delimited,
5 – 32‑bit (fixed32).

Функции:
    encode(message) → bytes
    decode(data)   → list[(field_number, value), ...]
    to_source(message, indent=4) → str

Типы `fixed32` и `fixed64` – подклассы `int`, которые помечают,
что значение должно быть записано как фиксированный 32‑/64‑битный
little‑endian unsigned int.
"""

from __future__ import annotations

import json
import math
import struct
from typing import Any, List, Tuple

# ----------------------------------------------------------------------
# Fixed‑size integer‑wrapper classes
# ----------------------------------------------------------------------


class fixed32(int):
    """32‑битный фиксированный unsigned int (wire‑type 5)."""

    def __new__(cls, value: int) -> "fixed32":
        return int.__new__(cls, value & 0xFFFFFFFF)

    def __repr__(self) -> str:  # pragma: no cover
        return f"fixed32({int(self)})"


class fixed64(int):
    """64‑битный фиксированный unsigned int (wire‑type 1)."""

    def __new__(cls, value: int) -> "fixed64":
        return int.__new__(cls, value & 0xFFFFFFFFFFFFFFFF)

    def __repr__(self) -> str:  # pragma: no cover
        return f"fixed64({int(self)})"


# ----------------------------------------------------------------------
# Varint helpers
# ----------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    """Encode unsigned integer as protobuf varint."""
    if value < 0:
        raise ValueError("varint must be unsigned")
    out = bytearray()
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            out.append(to_write | 0x80)
        else:
            out.append(to_write)
            break
    return bytes(out)


def _decode_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """Decode varint from *data* starting at *pos*.
    Returns (value, new_pos).  Raises ValueError on truncation or >10 bytes."""
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(data):
            raise ValueError("обрезанный varint")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
        if shift >= 70:  # >10 bytes
            raise ValueError("varint слишком длинный")
    return result, pos


# ----------------------------------------------------------------------
# Core encoder
# ----------------------------------------------------------------------


def encode(message: List[Tuple[int, Any]]) -> bytes:
    """
    Encode список кортежей ``(field_number, value)`` в protobuf‑wire.
    Порядок кортежей сохраняется; допускаются повторяющиеся номера полей.
    """
    out = bytearray()
    for field_number, value in message:
        if not isinstance(field_number, int) or field_number <= 0:
            raise TypeError(f"некорректный номер поля {field_number}")

        # bool must be processed before int (bool is subclass of int)
        if isinstance(value, bool):
            wire_type = 0
            tag = (field_number << 3) | wire_type
            out += _encode_varint(tag)
            out += _encode_varint(1 if value else 0)
        elif isinstance(value, int) and not isinstance(value, (fixed32, fixed64)):
            wire_type = 0
            tag = (field_number << 3) | wire_type
            # two‑complement 64‑bit representation for negative numbers
            unsigned = value & ((1 << 64) - 1)
            out += _encode_varint(tag)
            out += _encode_varint(unsigned)
        elif isinstance(value, fixed32):
            wire_type = 5
            tag = (field_number << 3) | wire_type
            out += _encode_varint(tag)
            out += int(value).to_bytes(4, "little")
        elif isinstance(value, fixed64):
            wire_type = 1
            tag = (field_number << 3) | wire_type
            out += _encode_varint(tag)
            out += int(value).to_bytes(8, "little")
        elif isinstance(value, str):
            wire_type = 2
            tag = (field_number << 3) | wire_type
            payload = value.encode("utf-8")
            out += _encode_varint(tag)
            out += _encode_varint(len(payload))
            out += payload
        elif isinstance(value, (bytes, bytearray)):
            wire_type = 2
            tag = (field_number << 3) | wire_type
            payload = bytes(value)
            out += _encode_varint(tag)
            out += _encode_varint(len(payload))
            out += payload
        elif isinstance(value, dict):  # google.protobuf.Struct
            payload = encode(_struct_message(value))
            out += _encode_varint((field_number << 3) | 2)
            out += _encode_varint(len(payload))
            out += payload
        elif isinstance(value, list):
            wire_type = 2
            tag = (field_number << 3) | wire_type
            payload = encode(value)  # рекурсия
            out += _encode_varint(tag)
            out += _encode_varint(len(payload))
            out += payload
        else:
            raise TypeError(f"неподдерживаемый тип значения поля {field_number}: {type(value)}")
    return bytes(out)


# ----------------------------------------------------------------------
# Core decoder
# ----------------------------------------------------------------------


def _classify_payload(payload: bytes) -> Any:
    """Classify length‑delimited payload according to правила из описания."""
    if len(payload) == 0:
        return ""

    # попытка UTF‑8
    try:
        text = payload.decode("utf-8")
        utf_ok = True
    except UnicodeDecodeError:
        utf_ok = False

    # проверка «без управляющих символов», пункт b
    if utf_ok:
        if all((b >= 0x20 or b in (0x09, 0x0A, 0x0D)) and b != 0x7F for b in payload):
            return text

    # попытка вложенного сообщения, пункт c
    try:
        nested = decode(payload)  # рекурсия; decode уже проверит каноничность
        if encode(nested) == payload:
            return nested
    except ValueError:
        pass

    # пункт d – любой корректный UTF‑8 считается строкой
    if utf_ok:
        return text

    # пункт e – иначе возвращаем raw bytes
    return payload


def decode(data: bytes) -> List[Tuple[int, Any]]:
    """
    Декодировать protobuf‑сообщение в список кортежей.
    При невозможности полной обратной генерации (неканонические байты)
    генерируется ``ValueError("не восстанавливается побайтно")``.
    """
    message = _decode_internal(data)
    # проверка каноничности (обратимый байт‑по‑байту)
    if encode(message) != data:
        raise ValueError("не восстанавливается побайтно")
    return message


def decode_named(data: bytes) -> List[Tuple[int, Any]]:
    """decode + вложенные google.protobuf.Struct как Struct({ключ: значение}) — если побайтно обратимо."""
    message = decode(data)
    named = _structify(message)
    return named if encode(named) == data else message


# ----------------------------------------------------------------------
# google.protobuf.Struct / Value / ListValue — единственные типы с известной
# схемой в ответах gproxy: map<string, Value> fields = 1; Value — oneof
# null(1) number(2, double) string(3) bool(4) struct(5) list(6); ListValue — repeated Value = 1.
# ----------------------------------------------------------------------


class Struct(dict):
    """Struct из ответа: ключи — строки, значения — str/float/bool/None/dict/list."""

    def __repr__(self) -> str:
        return f"Struct({_py(dict(self), 0)})"


_FAIL = object()


def _struct_message(value: dict) -> list:
    return [(1, [(1, str(k)), (2, _value_message(v))]) for k, v in value.items()]


def _value_message(v: Any) -> list:
    if v is None:
        return [(1, 0)]
    if isinstance(v, bool):
        return [(4, v)]
    if isinstance(v, (int, float)):
        return [(2, fixed64(struct.unpack("<Q", struct.pack("<d", float(v)))[0]))]
    if isinstance(v, str):
        return [(3, v)]
    if isinstance(v, dict):
        return [(5, _struct_message(v))]
    if isinstance(v, (list, tuple)):
        return [(6, [(1, _value_message(x)) for x in v])]
    raise TypeError(f"Struct: неподдерживаемое значение {type(v)}")


def _as_value(msg: Any) -> Any:
    if not isinstance(msg, list) or len(msg) != 1:
        return _FAIL
    n, x = msg[0]
    if n == 1 and type(x) is int and x == 0:
        return None
    if n == 2 and isinstance(x, fixed64):
        f = struct.unpack("<d", int(x).to_bytes(8, "little"))[0]
        return f if math.isfinite(f) else _FAIL
    if n == 3 and isinstance(x, str):
        return x
    if n == 4 and type(x) is int and x in (0, 1):
        return bool(x)
    if n == 5:
        return {} if x == "" else _as_struct(x)
    if n == 6:
        if x == "":
            return []
        if not isinstance(x, list) or any(k != 1 for k, _ in x):
            return _FAIL
        items = [_as_value(v) for _, v in x]
        return _FAIL if any(i is _FAIL for i in items) else items
    return _FAIL


def _as_struct(msg: Any) -> Any:
    if not isinstance(msg, list) or not msg:
        return _FAIL
    out = {}
    for n, entry in msg:
        if n != 1 or not isinstance(entry, list) or len(entry) != 2:
            return _FAIL
        (kn, key), (vn, value) = entry
        if kn != 1 or vn != 2 or not isinstance(key, str) or key in out:
            return _FAIL
        value = _as_value(value)
        if value is _FAIL:
            return _FAIL
        out[key] = value
    return out


def _structify(msg: list) -> list:
    out = []
    for n, v in msg:
        if isinstance(v, list):
            s = _as_struct(v)
            v = Struct(s) if s is not _FAIL else _structify(v)
        out.append((n, v))
    return out


def _py(v: Any, level: int, step: int = 4) -> str:
    """Python-литерал значения Struct: короткое — в строку, длинное — с переносами."""
    if v is None or isinstance(v, bool):
        return repr(v)
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, dict):
        parts = [f"{json.dumps(str(k), ensure_ascii=False)}: {_py(x, level + step)}" for k, x in v.items()]
        open_, close = "{", "}"
    else:
        parts = [_py(x, level + step) for x in v]
        open_, close = "[", "]"
    flat = open_ + ", ".join(parts) + close
    if len(flat) + level <= 100 and "\n" not in flat:
        return flat
    pad = " " * (level + step)
    return open_ + "\n" + "".join(f"{pad}{p},\n" for p in parts) + " " * level + close


def _decode_internal(data: bytes) -> List[Tuple[int, Any]]:
    pos = 0
    result: List[Tuple[int, Any]] = []
    while pos < len(data):
        # чтение тега
        tag, pos = _decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if field_number == 0:
            raise ValueError("Нулевой номер поля")
        if wire_type not in (0, 1, 2, 5):
            raise ValueError(f"Неподдерживаемый wire‑type {wire_type}")

        if wire_type == 0:  # varint
            raw_val, pos = _decode_varint(data, pos)
            # интерпретируем как signed 64‑bit (по спецификации protobuf)
            value: Any = raw_val if raw_val < (1 << 63) else raw_val - (1 << 64)
        elif wire_type == 1:  # 64‑bit
            if pos + 8 > len(data):
                raise ValueError("обрезанные 64‑битные данные")
            raw = data[pos : pos + 8]
            pos += 8
            value = fixed64(int.from_bytes(raw, "little"))
        elif wire_type == 5:  # 32‑bit
            if pos + 4 > len(data):
                raise ValueError("обрезанные 32‑битные данные")
            raw = data[pos : pos + 4]
            pos += 4
            value = fixed32(int.from_bytes(raw, "little"))
        else:  # wire_type == 2, length‑delimited
            length, pos = _decode_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("обрезанные length‑delimited данные")
            payload = data[pos : pos + length]
            pos += length
            value = _classify_payload(payload)

        result.append((field_number, value))
    return result


# ----------------------------------------------------------------------
# Преобразование в красивый python‑исходник
# ----------------------------------------------------------------------


def _repr_literal(value: Any) -> str:
    """Вернуть строку‑литерал, пригодную для eval()."""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int) and not isinstance(value, (fixed32, fixed64)):
        return str(value)
    if isinstance(value, fixed32):
        return f"fixed32({int(value)})"
    if isinstance(value, fixed64):
        return f"fixed64({int(value)})"
    if isinstance(value, str):
        # double‑quoted, без экранирования ASCII символов
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (bytes, bytearray)):
        return repr(bytes(value))
    raise TypeError(f"не поддерживаемый тип {type(value)}")


def to_source(message: List[Tuple[int, Any]], indent: int = 4) -> str:
    """
    Печатает сообщение в виде красиво отформатированного python‑литерала.
    """
    return "\n".join(["["] + _format_message(message, indent, indent) + ["]"])


def _format_message(msg: List[Tuple[int, Any]], cur_indent: int, step: int) -> List[str]:
    """Строки полей сообщения (без скобок) с отступом cur_indent."""
    lines = []
    inner = " " * cur_indent
    for field_no, val in msg:
        # простые типы
        if isinstance(val, Struct):
            lines.append(f"{inner}({field_no}, Struct({_py(dict(val), cur_indent)})),")
        elif not isinstance(val, list):
            val_repr = _repr_literal(val)
            comment = ""
            if isinstance(val, fixed32):
                f = struct.unpack("<f", int(val).to_bytes(4, "little"))[0]
                if math.isfinite(f):
                    comment = f"  # {f:g}"
            elif isinstance(val, fixed64):
                f = struct.unpack("<d", int(val).to_bytes(8, "little"))[0]
                if math.isfinite(f):
                    comment = f"  # {f:g}"
            lines.append(f"{inner}({field_no}, {val_repr}),{comment}")
        else:
            # вложенный список – печатаем многострочно
            lines.append(f"{inner}({field_no}, [")
            lines.extend(_format_message(val, cur_indent + step, step))
            lines.append(f"{inner}]),")
    return lines


# ----------------------------------------------------------------------
# Самотестирование
# ----------------------------------------------------------------------


if __name__ == "__main__":
    samples = [
        b"\n\x071234567",
        b"\x12\x15\n\x13\n\x02id\x12\r\x1a\x0bdelayed_tvo",
        b"\x08\x96\x01",
        b"\x08\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01",
        b"\x15\x00\x00\x80?",
        b"\x19\x00\x00\x00\x00\x00\x00\xf0?",
        b"\n\x00",
    ]

    for sample in samples:
        decoded = decode(sample)
        assert encode(decoded) == sample, f"Roundtrip failed for {sample!r}"

    # неверный, но потенциально восстанавливаемый varint – допускаем любое поведение
    try:
        decode(b"\x80}\x01")
    except ValueError:
        pass

    # гарантированный недостаток данных – должно бросить исключение
    try:
        decode(b"\x0b")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for truncated input")
