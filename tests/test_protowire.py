import pytest
from server_emulator.protowire import decode, encode, fixed32, fixed64, to_source, Struct, decode_named

# -------------------------------------------------------------------
# 1. Round‑trip for several binary samples
# -------------------------------------------------------------------
SAMPLES = [
    b"\n\x071234567",
    b"\x12\x15\n\x13\n\x02id\x12\r\x1a\x0bdelayed_tvo",
    b"\x08\x96\x01",
    b"\x08\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01",  # decodes to -1
    b"\x15\x00\x00\x80?",
    b"\x19\x00\x00\x00\x00\x00\x00\xf0?",
    b"\n\x00",
]

@pytest.mark.parametrize("data", SAMPLES)
def test_roundtrip(data: bytes) -> None:
    """encode(decode(x)) == x for a set of binary samples"""
    assert encode(decode(data)) == data

# -------------------------------------------------------------------
# 2. Simple decode expectations
# -------------------------------------------------------------------
def test_decode_simple() -> None:
    assert decode(b"\n\x071234567") == [(1, "1234567")]
    assert decode(b"\x08\x01") == [(1, 1)]
    assert type(decode(b"\x08\x01")[0][1]) is int

# -------------------------------------------------------------------
# 3. Nested message structure
# -------------------------------------------------------------------
def test_decode_nested() -> None:
    data = b"\x12\x15\n\x13\n\x02id\x12\r\x1a\x0bdelayed_tvo"
    expected = [(2, [(1, [(1, "id"), (2, [(3, "delayed_tvo")])])])]
    assert decode(data) == expected

# -------------------------------------------------------------------
# 4. to_source round‑trip and formatting checks
# -------------------------------------------------------------------
@pytest.mark.parametrize("data", SAMPLES)
def test_to_source_roundtrip(data: bytes) -> None:
    msg = decode(data)
    src = to_source(msg)
    # evaluate source back to a Python object and re‑encode
    rebuilt = eval(src, {"fixed32": fixed32, "fixed64": fixed64})
    assert encode(rebuilt) == data
    # no stray "[" lines after the opening one
    lines = src.splitlines()
    assert [l for l in lines[1:] if l.strip() == "["] == []

def test_to_source_cyrillic() -> None:
    src = to_source([(2, "Недавнее")])
    assert "Недавнее" in src

def test_to_source_fixed32_comment() -> None:
    src = to_source([(3, fixed32(1065353216))])
    # 1065353216 corresponds to float 1.0, comment should contain "# 1"
    assert "# 1" in src

# -------------------------------------------------------------------
# 5. Invalid inputs raise ValueError
# -------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        b"\x0b",               # unsupported wire‑type 3
        b"\x08",               # truncated varint
        b"\x0a\x05ab",        # length 5 but only 2 payload bytes
        b"\x00\x01",          # field number 0
    ],
)
def test_decode_invalid(bad: bytes) -> None:
    with pytest.raises(ValueError):
        decode(bad)

# -------------------------------------------------------------------
# 6. encode type checking
# -------------------------------------------------------------------
def test_encode_type_error() -> None:
    with pytest.raises(TypeError):
        encode([(1, 1.5)])  # float is not supported

# -------------------------------------------------------------------
# 7. decode_named and Struct tests
# -------------------------------------------------------------------
def test_gallery_roundtrip() -> None:
    GALLERY = encode([(2, [(2, Struct({"id": "delayed_tvo", "carousel_name": "Недавнее", "y": -2.0})),
                           (1002, [(1, "Смотрели недавно"), (3, "delayed_tvo")])])])
    # round‑trip of raw decode
    assert encode(decode(GALLERY)) == GALLERY
    # decode_named produces a Struct at the expected position
    named = decode_named(GALLERY)
    assert named[0][1][0] == (2, {"id": "delayed_tvo", "carousel_name": "Недавнее", "y": -2.0})
    assert isinstance(named[0][1][0][1], Struct)
    # encode(decode_named(...)) must equal original bytes
    assert encode(decode_named(GALLERY)) == GALLERY

def test_struct_value_kinds_roundtrip() -> None:
    m = [(1, Struct({"s": "x", "n": 1.5, "b": True, "f": False, "z": None,
                     "d": {"k": "v"}, "l": [1.0, "a", None], "e": {}, "el": []}))]
    data = encode(m)
    assert decode_named(data) == m
    assert encode(decode_named(data)) == data

def test_to_source_struct_eval() -> None:
    m = [(1, Struct({"s": "x", "n": 1.5, "b": True, "f": False, "z": None,
                     "d": {"k": "v"}, "l": [1.0, "a", None], "e": {}, "el": []}))]
    data = encode(m)
    src = to_source(decode_named(data))
    assert "Struct(" in src
    rebuilt = eval(src, {"Struct": Struct, "fixed32": fixed32, "fixed64": fixed64})
    assert encode(rebuilt) == data

def test_decode_named_plain_list() -> None:
    # message that is not a Struct stays as plain list
    m = [(1, [(1, "id"), (2, 5)])]
    data = encode(m)
    assert decode_named(data) == m

def test_decode_named_nan_stays_plain() -> None:
    # NaN as fixed64 should prevent Struct conversion
    m = [(1, [(1, [(1, "k"), (2, [(2, fixed64(0x7FF8000000000000))])])])]
    data = encode(m)
    assert decode_named(data) == m

def test_endpoints_protobuf_script_template() -> None:
    """Шаблон скрипта форматируется (фигурные скобки экранированы) и собирает исходное тело."""
    from server_emulator.ui.api.endpoints import PROTOBUF_SCRIPT

    data = encode([(1, Struct({"k": "v"}))])
    code = PROTOBUF_SCRIPT.format(id="x", size=len(data), content_type="application/x-protobuf",
                                  message=to_source(decode_named(data)))
    namespace = {}
    exec(compile(code, "script", "exec"), namespace)
    assert encode(namespace["MESSAGE"]) == data
