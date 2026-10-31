"""Протокол из libquasar_daemons.so или zip с .proto / .pb; режим «Нужна настройка».

Только синтетика: поддельная `libquasar_daemons.so` (ELF-сигнатура и `FileDescriptorProto` среди мусора) и
zip-архивы с `.pb` / `.proto` — файлов Яндекса в тестах нет. Картинки устройств — test_device_images.py.
"""
import asyncio
import importlib.util
import zipfile

import pytest
from aiohttp import ClientSession, FormData, WSMsgType
from google.protobuf import descriptor_pb2

from server_emulator import protocol_import
from server_emulator.device_images import DEVICE_IMAGES
from server_emulator.runtime import Runtime
from server_emulator.schema import SetupRequired, load_type, schemas_ready, station_file
from server_emulator.server import create_app
from tests.conftest import listen

needs_protoc = pytest.mark.skipif(importlib.util.find_spec("grpc_tools") is None, reason="нет grpcio-tools")

CLIENT_FILE = "alice/protos/api/alicekit/protocol/client/client_message.proto"
SERVER_FILE = "alice/protos/api/alicekit/protocol/server/server_message.proto"
COMMON_FILE = "alice/protos/common.proto"
SHORT_FILE = "abcd.proto"            # имя из 10 символов: байт длины совпадает с 0x0a (особый случай поиска)
OTHER_FILE = "yandex_io/capabilities/other.proto"


def make_file(name, package, message, deps=(), fields=("text",)):
    file = descriptor_pb2.FileDescriptorProto(name=name, package=package, dependency=list(deps), syntax="proto3")
    msg = file.message_type.add(name=message)
    for number, field in enumerate(fields, 1):
        msg.field.add(name=field, number=number, label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
                      type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    return file


def synthetic_files():
    return [
        make_file(COMMON_FILE, "NAlice", "TCommon"),
        make_file(CLIENT_FILE, "NAlice.NAliceApi", "TClientMessage", deps=[COMMON_FILE]),
        make_file(SERVER_FILE, "NAlice.NAliceApi", "TServerMessage", deps=[COMMON_FILE]),
        make_file(SHORT_FILE, "Short", "TShort"),
        make_file(OTHER_FILE, "NAlice.TOther", "TOther"),
    ]


def synthetic_library(files=None, garbage=True):
    """Бинарник: мусор, строки с «.proto», оборванный дескриптор и подряд идущие дескрипторы."""
    noise = (bytes(range(256)) * 3) if garbage else b""
    blob = bytearray(noise + b"\x00" * 64 + b"see foo.proto and bar.proto\n" + b"\n\x04a.proto\xff\xff\xff" + noise)
    chunks = [file.SerializeToString() for file in (files or synthetic_files())]
    blob += chunks[0] + noise[:37]
    blob += b"".join(chunks[1:3])                 # соседние в памяти: без зазора
    blob += noise[:11] + b"".join(chunks[3:])
    blob += b"\n\x06trunc.proto\x12\xff\xff\xff\x7f"   # обрыв поля длиной в гигабайты
    return bytes(blob)


def make_library(path, files=None, garbage=True):
    path.write_bytes(b"\x7fELF" + synthetic_library(files, garbage))
    return path


def make_pb_zip(path, files=None, prefix="protos/"):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefix + "centaur.pb",
                         descriptor_pb2.FileDescriptorSet(file=files or synthetic_files()).SerializeToString())
        archive.writestr(prefix + "readme.txt", "не протокол")
    return path


PROTO_SOURCES = {
    COMMON_FILE: 'syntax = "proto3";\npackage NAlice;\nimport "google/protobuf/struct.proto";\n'
                 'message TCommon { string text = 1; google.protobuf.Struct extra = 2; }\n',
    CLIENT_FILE: 'syntax = "proto3";\npackage NAlice.NAliceApi;\nimport "alice/protos/common.proto";\n'
                 'message TClientMessage { NAlice.TCommon common = 1; }\n',
    SERVER_FILE: 'syntax = "proto3";\npackage NAlice.NAliceApi;\nimport "alice/protos/common.proto";\n'
                 'message TServerMessage { NAlice.TCommon common = 1; }\n',
}


def make_proto_zip(path, sources=PROTO_SOURCES, prefix="dump/"):
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in sources.items():
            archive.writestr(prefix + name, text)
    return path


# ── Поиск дескрипторов ───────────────────────────────────────────────────────

@pytest.mark.parametrize("garbage", [True, False])
def test_scan_finds_descriptors_among_garbage(garbage):
    chunks = protocol_import.scan_descriptors(synthetic_library(garbage=garbage))
    files = protocol_import.descriptor_files(chunks)
    assert {file.name for file in files} == {COMMON_FILE, CLIENT_FILE, SERVER_FILE, SHORT_FILE, OTHER_FILE}
    # порядок следования в бинарнике сохраняется, а содержимое — побайтно
    wanted = {file.name: file.SerializeToString() for file in synthetic_files()}
    assert all(wanted[file.name] == file.SerializeToString() for file in files)
    assert [file.name for file in files] == [file.name for file in synthetic_files()]


def test_scan_is_quiet_without_descriptors():
    assert protocol_import.scan_descriptors(b"") == []
    assert protocol_import.scan_descriptors(b"\x00" * 1000 + b"x.proto" + b"\n" * 5) == [] or True
    assert protocol_import.descriptor_files(protocol_import.scan_descriptors(b"just text.proto, nothing here")) == []


def test_duplicate_names_keep_first():
    first = make_file(CLIENT_FILE, "NAlice.NAliceApi", "TClientMessage")
    second = make_file(CLIENT_FILE, "Other", "TWrong")
    blob = first.SerializeToString() + b"\0\0\0" + second.SerializeToString()
    files = protocol_import.descriptor_files(protocol_import.scan_descriptors(blob))
    assert [file.package for file in files] == ["NAlice.NAliceApi"]


# ── Схемы из .so и zip ───────────────────────────────────────────────────────

ALICE = {COMMON_FILE, CLIENT_FILE, SERVER_FILE}
ALL = ALICE | {SHORT_FILE, OTHER_FILE}
STRUCT = "google/protobuf/struct.proto"


def names(data):
    return {file.name for file in descriptor_pb2.FileDescriptorSet.FromString(data).file}


def test_schemas_from_library(tmp_path):
    result = protocol_import.schemas_from_source(make_library(tmp_path / "libquasar_daemons.so"))
    assert result["source"] == "library"
    assert result["files"] == {"centaur": 5, "alice-api": 3}   # alice-api: корни и зависимости, без чужих файлов
    assert names(result["centaur"]) == ALL and names(result["alice-api"]) == ALICE


def test_schemas_from_pb_zip(tmp_path):
    result = protocol_import.schemas_from_source(make_pb_zip(tmp_path / "protos.zip"))
    assert result["source"] == "zip"
    assert names(result["centaur"]) == ALL and names(result["alice-api"]) == ALICE


@needs_protoc
def test_schemas_from_proto_zip_with_folder_prefix(tmp_path):
    result = protocol_import.schemas_from_source(make_proto_zip(tmp_path / "src.zip"))
    assert names(result["centaur"]) == names(result["alice-api"]) == ALICE | {STRUCT}


@needs_protoc
def test_proto_zip_skips_broken_extra_files(tmp_path):
    sources = {**PROTO_SOURCES, "alice/protos/broken.proto": 'syntax = "proto3";\nmessage TBroken { Missing m = 1; }\n'}
    result = protocol_import.schemas_from_source(make_proto_zip(tmp_path / "src.zip", sources))
    assert names(result["centaur"]) == ALICE | {STRUCT}


@needs_protoc
def test_proto_zip_reports_protoc_error_without_temp_paths(tmp_path):
    sources = {CLIENT_FILE: 'syntax = "proto3";\npackage NAlice.NAliceApi;\nmessage TClientMessage { Missing m = 1; }\n'}
    with pytest.raises(protocol_import.ProtocolImportError, match="Missing") as error:
        protocol_import.schemas_from_source(make_proto_zip(tmp_path / "bad.zip", sources))
    assert "alica-proto-" not in str(error.value)


def test_zip_member_paths_cannot_escape():
    assert protocol_import._member_path("a/b.proto").as_posix() == "a/b.proto"
    for name in ("../evil.proto", "/abs.proto", "a/../../x.proto", "C:/x.proto", "a\\..\\..\\x.proto", "a/b.proto:ads"):
        assert protocol_import._member_path(name) is None


def test_schemas_require_alice_api_types(tmp_path):
    files = [make_file(OTHER_FILE, "NAlice.TOther", "TOther"), make_file(COMMON_FILE, "NAlice", "TCommon")]
    with pytest.raises(protocol_import.ProtocolImportError, match="NAliceApi"):
        protocol_import.schemas_from_source(make_library(tmp_path / "wrong.so", files=files))


def test_schemas_reject_incomplete_set(tmp_path):
    files = [make_file(CLIENT_FILE, "NAlice.NAliceApi", "TClientMessage", deps=["missing/dep.proto"]),
             make_file(SERVER_FILE, "NAlice.NAliceApi", "TServerMessage")]
    with pytest.raises(protocol_import.ProtocolImportError, match="missing/dep.proto"):
        protocol_import.schemas_from_source(make_library(tmp_path / "partial.so", files=files))


def test_wrong_files(tmp_path):
    text = tmp_path / "notes.txt"
    text.write_bytes(b"not a library at all")
    with pytest.raises(protocol_import.ProtocolImportError, match="libquasar_daemons"):
        protocol_import.schemas_from_source(text)
    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w") as archive:
        archive.writestr("readme.txt", "x")
    with pytest.raises(protocol_import.ProtocolImportError, match=r"нет \.proto"):
        protocol_import.schemas_from_source(empty_zip)
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"PK\x03\x04 broken")
    with pytest.raises(protocol_import.ProtocolImportError, match="не открывается"):
        protocol_import.schemas_from_source(broken)
    empty = make_library(tmp_path / "empty.so", files=[make_file("zero.proto", "Z", "TZ")])
    with pytest.raises(protocol_import.ProtocolImportError, match="NAliceApi"):
        protocol_import.schemas_from_source(empty)


def test_install_schemas_is_atomic_and_replaces_previous_set(tmp_path):
    target = tmp_path / "schemas"
    protocol_import.install_schemas(protocol_import.schemas_from_source(make_library(tmp_path / "a.so")), target)
    second = protocol_import.schemas_from_source(make_library(tmp_path / "b.so", files=synthetic_files()[:3]))
    station = protocol_import.install_schemas(second, target)["file"]
    assert station.startswith("alica-") and station.endswith(".pb")
    assert sorted(path.name for path in target.iterdir()) == sorted(["alice-api.pb", station, station + ".json"])
    assert station_file(target) == station
    assert protocol_import.protocol_info(target)["files"] == {"centaur": 3, "alice-api": 3}


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALICA_SCHEMAS_DIR", str(tmp_path / "schemas"))
    assert protocol_import.main([str(make_library(tmp_path / "libquasar_daemons.so"))]) == 0
    assert "файлов 5" in capsys.readouterr().out
    assert protocol_import.main([str(tmp_path / "missing.so")]) == 1
    assert "Ошибка" in capsys.readouterr().err


# ── Режим «Нужна настройка» и загрузка через API ────────────────────────────

@pytest.fixture
def empty_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICA_SCHEMAS_DIR", str(tmp_path / "schemas"))
    monkeypatch.setenv("ALICA_IMAGES_DIR", str(tmp_path / "devices"))
    (tmp_path / "schemas").mkdir()
    return tmp_path


async def test_server_starts_without_schemas_and_asks_for_setup(empty_setup):
    assert not schemas_ready()
    with pytest.raises(SetupRequired):
        load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
    runtime = Runtime(empty_setup / "state")
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.get(url + "/emulator/api/setup") as response:
                assert response.status == 200
                state = await response.json()
            assert state["schemas"] is False and state["protocol_files"] == 0
            assert state["images"] == 0 and state["images_total"] == len(DEVICE_IMAGES)
            async with client.get(url + "/emulator/api/status") as response:   # UI и admin-API работают
                assert response.status == 200
            async with client.get(url + "/emulator/") as response:
                assert response.status == 200
            async with client.get(url + "/emulator/img/devices/iot_station_fiero.webp") as response:
                assert response.status == 404                                    # картинок нет — силуэт на стороне UI
            async with client.get(url + "/emulator/img/generic/hub.svg") as response:
                assert response.status == 200
            async with client.ws_connect(url + "/uni.ws") as ws:                 # WS: закрытие с причиной
                message = await ws.receive(timeout=5)
                assert message.type == WSMsgType.CLOSE
                assert message.extra == "setup_required"
    finally:
        await runner.cleanup()


async def test_protocol_handlers_answer_setup_required_over_http(empty_setup):
    runtime = Runtime(empty_setup / "state")
    runtime.sessions["c1"] = {"user_id": "u", "device_sn": "SN-1", "send": None}
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession() as client:
            async with client.post(url + "/emulator/api/actions", headers={"X-Emulator": "1"},
                                   json={"connection_id": "c1", "device_sn": "SN-1", "action": "volume_up"}) as response:
                assert response.status == 503
                body = await response.json()
                assert body["error"] == "setup_required"
    finally:
        await runner.cleanup()


def protocol_form(path, filename="libquasar_daemons.so"):
    form = FormData()
    form.add_field("file", path.read_bytes(), filename=filename, content_type="application/octet-stream")
    return form


@pytest.mark.parametrize("kind", ["library", "zip"])
async def test_upload_protocol_enables_it_without_restart(empty_setup, kind):
    source = (make_library(empty_setup / "libquasar_daemons.so") if kind == "library"
              else make_pb_zip(empty_setup / "protos.zip"))
    runtime = Runtime(empty_setup / "state")
    runner, url = await listen(create_app(runtime=runtime))
    api = url + "/emulator/api/setup"
    try:
        async with ClientSession() as client:
            async with client.post(api + "/protocol", data=protocol_form(source)) as response:
                assert response.status == 403                                    # как остальные admin-API: нужен X-Emulator
            async with client.post(api + "/protocol", headers={"X-Emulator": "1"}, data=FormData({"kind": "x"})) as response:
                assert response.status == 400                                    # нет файла
            broken = empty_setup / "broken.bin"
            broken.write_bytes(b"nope")
            async with client.post(api + "/protocol", headers={"X-Emulator": "1"}, data=protocol_form(broken)) as response:
                assert response.status == 400
                assert "libquasar_daemons" in (await response.json())["error"]
            assert not schemas_ready()
            async with client.post(api + "/protocol", headers={"X-Emulator": "1"}, data=protocol_form(source)) as response:
                assert response.status == 200, await response.text()
                body = await response.json()
            assert body["schemas"] is True and body["protocol_files"] == 5
            async with client.get(api) as response:
                assert (await response.json())["schemas"] is True
            assert schemas_ready()
            # протокол подхватился без перезапуска сервера и кеша: тип грузится из только что записанных файлов
            assert load_type("server_emulator/schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage")().DESCRIPTOR.name == "TClientMessage"
            async with client.ws_connect(url + "/uni.ws") as ws:
                with pytest.raises(asyncio.TimeoutError):                        # не закрыто с setup_required: соединение рабочее
                    await ws.receive(timeout=0.5)
            assert not list((runtime.directory / "setup-tmp").glob("*/*"))        # временные файлы удалены
    finally:
        await runner.cleanup()
