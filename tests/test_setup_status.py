"""Состояние протокола в окне настройки: файлы на месте ещё не значат «загружен»."""

import json
from server_emulator import protocol_import
from google.protobuf import descriptor_pb2


def test_empty_dir_not_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICA_SCHEMAS_DIR", str(tmp_path))
    result = protocol_import.setup_status()
    assert result["schemas"] is False
    assert result["schemas_error"] == ""


def test_corrupted_files_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICA_SCHEMAS_DIR", str(tmp_path))
    # write corrupted proto files
    (tmp_path / "alice-api.pb").write_bytes(b"\xff\xff\xff\xff garbage")
    (tmp_path / "alica-0000.pb").write_bytes(b"\xff\xff\xff\xff garbage")
    # write json metadata
    json_data = {
        "source": "library",
        "files": {"centaur": 5, "alice-api": 2},
        "imported": 1700000000,
    }
    (tmp_path / "alica-0000.pb.json").write_text(json.dumps(json_data))
    result = protocol_import.setup_status()
    assert result["schemas"] is False
    assert isinstance(result["schemas_error"], str) and result["schemas_error"] != ""
    assert result["protocol_files"] == 5
    assert result["protocol_source"] == "library"
    assert result["protocol_imported"] == 1700000000


def test_valid_empty_sets_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICA_SCHEMAS_DIR", str(tmp_path))
    # create empty FileDescriptorSet
    fd_set = descriptor_pb2.FileDescriptorSet()
    data = fd_set.SerializeToString()
    (tmp_path / "alice-api.pb").write_bytes(data)
    (tmp_path / "alica-0001.pb").write_bytes(data)
    result = protocol_import.setup_status()
    assert result["schemas"] is True
    assert result["schemas_error"] == ""