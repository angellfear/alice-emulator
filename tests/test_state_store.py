"""B02/AUD-06: StateStore — CRUD, версионирование, миграция, откат, close."""
import json

import pytest

from server_emulator.state_store import MigrationConflict, StateStore, migrate_legacy

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


def test_clean_install_creates_schema_and_empty_loads(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    assert store.load_settings() is None
    data = store.load_identity_data()
    assert data == {"users": [], "devices": [], "unassigned": []}
    store.close()


def test_settings_revision_history(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_settings(SETTINGS_V1)
    store.save_settings({"mode": "emulate", "model": {"url": ""}, "stt": {"url": "http://x"}})
    latest = store.load_settings()
    assert latest["stt"]["url"] == "http://x"
    history = store.settings_history()
    assert len(history) == 2
    assert history[0] == latest  # DESC: свежая ревизия первой
    assert history[1] == SETTINGS_V1
    store.close()
    # История переживает переоткрытие БД
    store2 = StateStore(tmp_path / "emulator.sqlite3")
    assert len(store2.settings_history()) == 2
    assert store2.load_settings() == latest
    store2.close()


def test_identity_roundtrip_and_owner_devices(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_identity_data(IDENTITIES)
    data = store.load_identity_data()
    by_sn = {d["sn"]: d for d in data["devices"]}
    assert by_sn["SN-A"]["device_ids"] == ["dev-1", "dev-2"]
    assert by_sn["SN-B"]["user_id"] == "u1"
    assert data["users"][0]["profile"]["timezone"] == "Europe/Moscow"
    assert [d["device_id"] for d in data["unassigned"]] == ["dev-x"]  # B03: + source/ip/метки времени
    store.close()


def test_duplicate_alias_rejected_with_rollback(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_identity_data(IDENTITIES)
    broken = json.loads(json.dumps(IDENTITIES))
    broken["devices"][1]["device_ids"].append("dev-1")  # alias уже у SN-A
    with pytest.raises(MigrationConflict):
        store.save_identity_data(broken)
    # Откат целиком: прежние данные не тронуты
    assert [d["sn"] for d in store.load_identity_data()["devices"]] == ["SN-A", "SN-B"]
    store.close()


def test_unknown_user_reference_rejected(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_identity_data(IDENTITIES)
    broken = json.loads(json.dumps(IDENTITIES))
    broken["devices"][0]["user_id"] = "ghost"
    with pytest.raises(MigrationConflict):
        store.save_identity_data(broken)
    assert store.load_identity_data()["devices"][0]["user_id"] == "u1"
    store.close()


def _legacy_files(tmp_path, identities=IDENTITIES, settings=SETTINGS_V1):
    (tmp_path / "identities.json").write_text(json.dumps(identities), encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def test_migration_imports_backs_up_unlinks_and_is_idempotent(tmp_path):
    _legacy_files(tmp_path)
    store = StateStore(tmp_path / "emulator.sqlite3")
    report = migrate_legacy(tmp_path, store)
    assert report["imported"] == ["identities.json", "settings.json"]
    assert report["backup"] and (tmp_path / report["backup"]).is_dir()
    for name in ("identities.json", "settings.json"):
        assert not (tmp_path / name).exists()  # атомарное переключение на БД
        assert (tmp_path / report["backup"] / name).exists()  # копии сохранены
    assert store.load_settings() == SETTINGS_V1
    assert len(store.load_identity_data()["devices"]) == 2
    assert store.meta("legacy_migrated") == "1"
    assert migrate_legacy(tmp_path, store) is None  # повторный запуск — no-op
    store.close()


def test_migration_conflict_rolls_back_and_keeps_files(tmp_path):
    broken = json.loads(json.dumps(IDENTITIES))
    broken["devices"][1]["device_ids"].append("dev-1")
    _legacy_files(tmp_path, identities=broken)
    store = StateStore(tmp_path / "emulator.sqlite3")
    with pytest.raises(MigrationConflict) as exc:
        migrate_legacy(tmp_path, store)
    report = json.loads(str(exc.value))
    assert report["imported"] == [] and report["conflict"]
    # Файлы не тронуты (откат до unlink), БД без частичных данных, миграцию можно повторить
    for name in ("identities.json", "settings.json"):
        assert (tmp_path / name).exists()
    assert store.load_identity_data()["devices"] == []
    # Починка файла на месте → повторный запуск миграции успешен
    (tmp_path / "identities.json").write_text(json.dumps(IDENTITIES), encoding="utf-8")
    report2 = migrate_legacy(tmp_path, store)
    assert report2["imported"] == ["identities.json", "settings.json"]
    assert len(store.load_identity_data()["devices"]) == 2
    store.close()


def test_migration_conflict_settings_not_reimported_over_existing(tmp_path):
    # settings в БД уже есть (seed) → миграция не перезаписывает их из JSON
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_settings({"mode": "emulate", "model": {"url": ""}, "seeded": True})
    (tmp_path / "settings.json").write_text(json.dumps({"mode": "proxy"}), encoding="utf-8")
    report = migrate_legacy(tmp_path, store)
    assert "settings.json" in report["imported"]  # файл изъят (backup есть)
    assert store.load_settings()["seeded"] is True  # но данные БД не затёрты
    store.close()


def test_stray_other_files_after_migration_skipped_not_deleted(tmp_path):
    _legacy_files(tmp_path)
    store = StateStore(tmp_path / "emulator.sqlite3")
    migrate_legacy(tmp_path, store)
    # identities.json не мог появиться из кода после миграции; не удаляем молча
    (tmp_path / "identities.json").write_text('{"users": []}', encoding="utf-8")
    report = migrate_legacy(tmp_path, store)
    assert report["imported"] == [] and report["skipped_stray"] == ["identities.json"]
    assert (tmp_path / "identities.json").exists()
    assert len(store.load_identity_data()["devices"]) == 2  # БД не перезаписана stray-файлом
    store.close()


def test_repeated_close_is_noop_and_operations_raise(tmp_path):
    store = StateStore(tmp_path / "emulator.sqlite3")
    store.save_settings(SETTINGS_V1)
    store.close()
    store.close()  # идемпотентно
    with pytest.raises(RuntimeError):
        store.load_settings()
    with pytest.raises(RuntimeError):
        store.save_settings(SETTINGS_V2)
    with pytest.raises(RuntimeError):
        store.set_meta("closed", "1")
    with pytest.raises(RuntimeError):
        migrate_legacy(tmp_path, store)
