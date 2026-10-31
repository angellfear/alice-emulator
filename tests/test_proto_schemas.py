import zipfile
from pathlib import Path

import pytest

from server_emulator import proto_schemas

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_zip(tmp_path: Path, files: dict[str, str], name: str = "p.zip") -> Path:
    """Create a zip at ``tmp_path / name`` with *files* mapping member → text."""
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as z:
        for member, text in files.items():
            z.writestr(member, text)
    return zip_path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def set_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect proto_schemas.DIRECTORY to a temporary location and clear caches."""
    monkeypatch.setattr(proto_schemas, "DIRECTORY", tmp_path / "proto")
    # ensure clean state before each test
    proto_schemas._cached_pool.cache_clear()
    proto_schemas._cached_names.cache_clear()
    yield
    # clean up after test
    proto_schemas._cached_pool.cache_clear()
    proto_schemas._cached_names.cache_clear()

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

FOO = (
    'syntax = "proto3";\n'
    'package NTest;\n'
    'import "test/common.proto";\n'
    'import "test/options.proto";\n'  # intentionally missing – will become a stub
    '\nmessage TGetFooBarResponse {\n'
    '  optional string Title = 1;\n'
    '  optional int32 Count = 2;\n'
    '  repeated .NTest.TItem Items = 3;\n'
    '}\n'
)
COMMON = (
    'syntax = "proto3";\n'
    'package NTest;\n'
    '\nmessage TItem {\n'
    '  optional string Id = 1;\n'
    '}\n'
)
LEGACY = (
    'syntax = "proto2";\n'
    'package NTest;\n'
    '\nmessage TLegacy {\n'
    '  string Name = 1;\n'
    '  optional int32 Age = 2;\n'
    '}\n'
)
BROKEN = (
    'syntax = "proto3";\n'
    'package NTest;\n'
    '\nmessage TBroken {\n'
    '  optional .NTest.TMissing Value = 1;\n'
    '}\n'
)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_member_path():
    cases = {
        "a__b__c.proto": "a/b/c.proto",
        "dir/x.proto": "dir/x.proto",
        "x.txt": None,
        "../evil.proto": None,
        "root.proto": "root.proto",
    }
    for inp, exp in cases.items():
        assert proto_schemas._member_path(inp) == exp


def test_fix_proto2():
    fixed = proto_schemas._fix_proto2(LEGACY)
    assert "required string Name = 1;" in fixed
    assert "optional int32 Age = 2;" in fixed
    # proto3 should stay unchanged
    untouched = proto_schemas._fix_proto2(FOO)
    assert untouched == FOO


def test_compile_zip_success(tmp_path: Path):
    zip_path = make_zip(
        tmp_path,
        {
            "test__foo.proto": FOO,
            "test__common.proto": COMMON,
            "test__legacy.proto": LEGACY,
            "test__broken.proto": BROKEN,
        },
    )
    fdps, report = proto_schemas.compile_zip(zip_path)
    names = {fdp.name for fdp in fdps}
    assert {"test/foo.proto", "test/common.proto", "test/legacy.proto"}.issubset(names)
    assert report["stubs"] == ["test/options.proto"]
    dropped = report["dropped"]
    assert len(dropped) == 1 and dropped[0]["name"] == "test/broken.proto"
    assert report["found"] == 4


def test_compile_zip_errors(tmp_path: Path):
    # not a zip file
    bad = tmp_path / "not.zip"
    bad.write_text("just text", encoding="utf-8")
    with pytest.raises(ValueError):
        proto_schemas.compile_zip(bad)
    # zip without .proto entries
    empty_zip = make_zip(tmp_path, {"readme.txt": "hi"}, name="empty.zip")
    with pytest.raises(ValueError):
        proto_schemas.compile_zip(empty_zip)


def test_import_zip_roundtrip_encode_decode(tmp_path: Path):
    zip_path = make_zip(
        tmp_path,
        {"test__foo.proto": FOO, "test__common.proto": COMMON},
    )
    proto_schemas.import_zip(zip_path, "p.zip", "a", "replace")
    assert "NTest.TGetFooBarResponse" in proto_schemas.names()
    assert proto_schemas.meta()["last"]["mode"] == "replace"

    name = "NTest.TGetFooBarResponse"
    value = {"Title": "Привет", "Count": 3, "Items": [{"Id": "a"}]}
    data = proto_schemas.encode_message(name, value)
    assert proto_schemas.decode_typed(name, data) == value

    # malformed data – extra bytes
    with pytest.raises(ValueError):
        proto_schemas.decode_typed(name, data + b"\x50\x01")
    # completely invalid bytes
    with pytest.raises(ValueError):
        proto_schemas.decode_typed(name, b"\xff\xff\xff")


def test_import_zip_update_vs_replace(tmp_path: Path):
    zip1 = make_zip(tmp_path, {"test__foo.proto": FOO, "test__common.proto": COMMON}, name="zip1.zip")
    proto_schemas.import_zip(zip1, "zip1", "a", "replace")
    zip2 = make_zip(
        tmp_path,
        {"test__other.proto": (
            'syntax = "proto3";\npackage NTest;\n\nmessage TOther {\n  optional string X = 1;\n}\n'
        )},
        name="zip2.zip",
    )
    # update – keep previous messages
    proto_schemas.import_zip(zip2, "zip2", "b", "update")
    names = proto_schemas.names()
    assert "NTest.TGetFooBarResponse" in names and "NTest.TOther" in names
    assert len(proto_schemas.meta()["sources"]) == 2
    # replace – drop previous set
    proto_schemas.import_zip(zip2, "zip2", "b", "replace")
    names2 = proto_schemas.names()
    assert "NTest.TOther" in names2 and "NTest.TGetFooBarResponse" not in names2
    assert len(proto_schemas.meta()["sources"]) == 1


def test_import_zip_update_replaces_same_named(tmp_path: Path):
    zip1 = make_zip(tmp_path, {"test__foo.proto": FOO, "test__common.proto": COMMON}, name="zip1.zip")
    proto_schemas.import_zip(zip1, "zip1", "a", "replace")
    # modify common.proto – add extra field to TItem
    common2 = COMMON.replace("optional string Id = 1;", "optional string Id = 1;\n  optional string Extra = 2;")
    zip3 = make_zip(tmp_path, {"test__common.proto": common2}, name="zip3.zip")
    proto_schemas.import_zip(zip3, "zip3", "c", "update")
    fields = proto_schemas.message_class("NTest.TItem").DESCRIPTOR.fields_by_name
    assert "Extra" in fields
    # the report should count one replaced file
    assert proto_schemas.meta()["last"]["replaced"] == 1


def test_import_zip_invalid_mode(tmp_path: Path):
    zip_path = make_zip(tmp_path, {"test__foo.proto": FOO}, name="bad.zip")
    with pytest.raises(ValueError):
        proto_schemas.import_zip(zip_path, "bad", "x", "bogus")


def test_response_type():
    names = ["NAlice.NRpc.TGetUserSettingsResponse", "NTest.TGetFooBarResponse"]
    # known mapping
    assert proto_schemas.response_type("get_user_settings", names) == "NAlice.NRpc.TGetUserSettingsResponse"
    # heuristic based on method name
    assert proto_schemas.response_type("/gproxy/get_foo_bar", names) == "NTest.TGetFooBarResponse"
    # exact last component not found
    assert proto_schemas.response_type("/gproxy/get_foo_bar", ["X.YTGetFooBarResponse"]) is None


def test_names_without_import():
    with pytest.raises(ValueError) as exc:
        proto_schemas.names()
    assert "не импортированы" in str(exc.value)


def test_compile_zip_cascade(tmp_path: Path):
    """Файл, использующий типы несобравшегося, отбрасывается в том же проходе с причиной."""
    user = ('syntax = "proto3";\npackage NTest;\nimport "test/broken.proto";\n\n'
            'message TUser {\n  optional .NTest.TBroken Value = 1;\n}\n')
    only_import = ('syntax = "proto3";\npackage NTest;\nimport "test/broken.proto";\n\n'
                   'message TFree {\n  optional string X = 1;\n}\n')
    zip_path = make_zip(tmp_path, {"test__broken.proto": BROKEN, "test__user.proto": user, "test__free.proto": only_import})
    fdps, report = proto_schemas.compile_zip(zip_path)
    dropped = {d["name"]: d["error"] for d in report["dropped"]}
    assert set(dropped) == {"test/broken.proto", "test/user.proto"}
    assert "test/broken.proto" in dropped["test/user.proto"]
    assert "test/free.proto" in {fdp.name for fdp in fdps}  # только импорт без типов — собирается


def test_compile_zip_common_folder(tmp_path: Path):
    user = (
        'syntax = "proto3";\n'
        'package NTest;\n'
        'import "test/common.proto";\n'
        '\n'
        'message TUser {\n'
        '  optional .NTest.TItem Item = 1;\n'
        '}\n'
    )
    zip_path = make_zip(
        tmp_path,
        {
            "wrap/test/common.proto": COMMON,
            "wrap/test/user.proto": user,
        },
    )
    fdps, report = proto_schemas.compile_zip(zip_path)
    names = {fdp.name for fdp in fdps}
    assert "test/common.proto" in names
    assert "test/user.proto" in names
    assert report["stubs"] == []
    assert report["dropped"] == []


def test_compile_zip_duplicates(tmp_path: Path):
    user = (
        'syntax = "proto3";\n'
        'package NTest;\n'
        'import "test/Common2.proto";\n'
        '\n'
        'message TUser {\n'
        '  optional .NTest.TItem Item = 1;\n'
        '}\n'
    )
    zip_path = make_zip(
        tmp_path,
        {
            "test__common.proto": COMMON,
            "test__Common2.proto": COMMON,
            "test__user.proto": user,
        },
    )
    fdps, report = proto_schemas.compile_zip(zip_path)
    names = {fdp.name for fdp in fdps}
    assert report["duplicates"] == 1
    assert report["dropped"] == []
    assert "test/user.proto" in names
    common_in = "test/common.proto" in names
    common2_in = "test/Common2.proto" in names
    assert common_in != common2_in  # ровно один из дубликатов собран


def test_fix_oneofs():
    fixed = proto_schemas._fix_oneofs(
        'message T {\n'
        '  oneof V {\n'
        '    optional string A = 1;\n'
        '    required int32 B = 2;\n'
        '  }\n'
        '  optional string C = 3;\n'
        '}\n'
    )
    assert "    string A = 1;" in fixed
    assert "    int32 B = 2;" in fixed
    assert "  optional string C = 3;" in fixed


def test_compile_zip_skips_google_package(tmp_path: Path):
    wkt = (
        'syntax = "proto3";\n'
        'package google.protobuf;\n'
        '\n'
        'message Any {\n'
        '  string type_url = 1;\n'
        '}\n'
    )
    zip_path = make_zip(
        tmp_path,
        {
            "x__wkt.proto": wkt,
            "test__common.proto": COMMON,
        },
    )
    fdps, report = proto_schemas.compile_zip(zip_path)
    assert report["found"] == 1


def test_compile_zip_import_case(tmp_path: Path):
    user = (
        'syntax = "proto3";\n'
        'package NTest;\n'
        'import "test/common.proto";\n'
        '\n'
        'message TUser {\n'
        '  optional .NTest.TItem Item = 1;\n'
        '}\n'
    )
    zip_path = make_zip(
        tmp_path,
        {
            "test__Common.proto": COMMON,
            "test__user.proto": user,
        },
    )
    fdps, report = proto_schemas.compile_zip(zip_path)
    names = {fdp.name for fdp in fdps}
    assert report["stubs"] == []
    assert report["dropped"] == []
    assert {"test/Common.proto", "test/user.proto"} <= names
    user_fdp = next(fdp for fdp in fdps if fdp.name == "test/user.proto")
    assert user_fdp.dependency == ["test/Common.proto"]
