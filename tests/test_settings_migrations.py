"""Миграции настроек: устаревшие алиасы *.local → настоящие домены (колонка ходит на них)."""
from server_emulator.runtime import Runtime

LEGACY = {"quasar.local": "quasar.yandex.net", "rpc.local": "rpc.alice.yandex.net",
          "uniproxy.local": "uniproxy.alice.yandex.net"}


def test_legacy_aliases_removed_user_aliases_kept(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        settings = dict(runtime.settings, aliases=dict(LEGACY, **{"my.upstream.test": "http://127.0.0.1:9000"}))
        runtime.settings_service.persist(settings)
        runtime.settings = runtime.settings_service.load()
        # ровно три устаревшие пары убраны, свой алиас остался
        assert runtime.settings["aliases"] == {"my.upstream.test": "http://127.0.0.1:9000"}
    finally:
        runtime.store.close()


def test_default_aliases_empty(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        assert runtime.settings["aliases"] == {}  # дефолт без *.local
    finally:
        runtime.store.close()
