"""StateStore — CRUD, версионирование, откат, close."""
import json

import pytest

from server_emulator.state_store import MigrationConflict, StateStore

SETTINGS_V1 = {"mode": "emulate", "model": {"url": ""}}
SETTINGS_V2 = {"mode": "proxy", "model": {"url": ""}}  # saved через API, не напрямую
IDENTITIES = {
    "users": [{"id": "u1", "name": "Owner", "profile": {"timezone": "Europe/Moscow"}}],
    "devices": [
        {"sn": "SN-A", "user_id": "u1", "device_ids": ["dev-1", "dev-2"], "profile": {}, "state": {}},
        {"sn": "SN-B", "user_id": "u1", "device_ids": ["dev-3"], "profile": {}, "state": {}},
    ],
    "unassigned": [{"device_id": "dev-x"}],
}


async def test_clean_install_creates_schema_and_empty_loads(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    assert store.load_settings() is None
    data = store.load_identity_data()
    assert data == {"users": [], "devices": [], "unassigned": []}
    await store.close()


async def test_settings_revision_history(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_settings(SETTINGS_V1)
    store.save_settings({"mode": "emulate", "model": {"url": ""}, "stt": {"url": "http://x"}})
    latest = store.load_settings()
    assert latest["stt"]["url"] == "http://x"
    history = store.settings_history()
    assert len(history) == 2
    assert history[0] == latest  # DESC: свежая ревизия первой
    assert history[1] == SETTINGS_V1
    await store.close()
    # История переживает переоткрытие БД
    store2 = StateStore(tmp_path / "emulator.sqlite3")
    assert len(store2.settings_history()) == 2
    assert store2.load_settings() == latest
    await store2.close()


async def test_identity_roundtrip_and_owner_devices(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_identity_data(IDENTITIES)
    data = store.load_identity_data()
    by_sn = {d["sn"]: d for d in data["devices"]}
    assert by_sn["SN-A"]["device_ids"] == ["dev-1", "dev-2"]
    assert by_sn["SN-B"]["user_id"] == "u1"
    assert data["users"][0]["profile"]["timezone"] == "Europe/Moscow"
    assert [d["device_id"] for d in data["unassigned"]] == ["dev-x"]  # + source/ip/метки времени
    await store.close()


async def test_duplicate_alias_rejected_with_rollback(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_identity_data(IDENTITIES)
    broken = json.loads(json.dumps(IDENTITIES))
    broken["devices"][1]["device_ids"].append("dev-1")  # alias уже у SN-A
    with pytest.raises(MigrationConflict):
        store.save_identity_data(broken)
    # Откат целиком: прежние данные не тронуты
    assert [d["sn"] for d in store.load_identity_data()["devices"]] == ["SN-A", "SN-B"]
    await store.close()


async def test_unknown_user_reference_rejected(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_identity_data(IDENTITIES)
    broken = json.loads(json.dumps(IDENTITIES))
    broken["devices"][0]["user_id"] = "ghost"
    with pytest.raises(MigrationConflict):
        store.save_identity_data(broken)
    assert store.load_identity_data()["devices"][0]["user_id"] == "u1"
    await store.close()


async def test_repeated_close_is_noop_and_operations_raise(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_settings(SETTINGS_V1)
    await store.close()
    await store.close()  # идемпотентно
    with pytest.raises(RuntimeError):
        store.load_settings()
    with pytest.raises(RuntimeError):
        store.save_settings(SETTINGS_V2)
    with pytest.raises(RuntimeError):
        store.set_meta("closed", "1")
