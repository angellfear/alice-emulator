"""Миграции настроек: устаревшие алиасы *.local → настоящие домены (колонка ходит на них),
выбор поставщика музыки music.provider → providers.music (общий реестр видов),
URL погоды server.weather.url → настройка плагина builtin.open-meteo."""
from server_emulator.runtime import Runtime

LEGACY = {"quasar.local": "quasar.yandex.net", "rpc.local": "rpc.alice.yandex.net",
          "uniproxy.local": "uniproxy.alice.yandex.net"}

WEATHER_URL_DEF = {"key": "server.weather.url", "scope": "server", "category": "common",
                   "user_editable": False, "type": "string", "title": "Провайдер погоды (URL)",
                   "default_value": "https://api.open-meteo.com/v1/forecast"}


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


def test_music_provider_moved_to_providers(tmp_path):
    """settings["music"]["provider"] → settings["providers"]["music"]: выбор поставщика
    переехал в общий реестр видов; старый ключ читается один раз и удаляется."""
    runtime = Runtime(tmp_path)
    try:
        runtime.store.save_settings(dict(runtime.settings, music={"provider": "spotify"}))
        runtime.settings = runtime.settings_service.load()
        assert runtime.settings["providers"]["music"] == "spotify"
        assert "music" not in runtime.settings
        # повторная загрузка: ключа больше нет — миграция однократна, значение на месте
        assert runtime.settings_service.load()["providers"]["music"] == "spotify"
    finally:
        runtime.store.close()


def _seed_old_weather_param(runtime, value):
    runtime.quasar_params.def_upsert(dict(WEATHER_URL_DEF))
    if value is not None:
        runtime.quasar_params.set_value("server", "server.weather.url", value)


def test_weather_url_moved_to_open_meteo_plugin(tmp_path):
    """Параметр server.weather.url переехал в настройку url плагина builtin.open-meteo:
    непустое значение не по умолчанию переносится (погода не молчит на дефолте), параметр
    удаляется из каталога вместе со значениями — миграция однократна."""
    first = Runtime(tmp_path)
    try:
        _seed_old_weather_param(first, "https://weather.local/v1/forecast")
    finally:
        first.store.close()
    runtime = Runtime(tmp_path)
    try:
        stored = runtime.settings["extensions"]["settings"]["builtin.open-meteo"]
        assert stored["url"] == "https://weather.local/v1/forecast"
        assert runtime.quasar_params.raw_value("server", "server.weather.url") is None
        assert runtime.quasar_params.def_get("server.weather.url") is None
        # повторный запуск: значения нет — перенос не случается, настройка на месте
        runtime.extensions.load_enabled()
        assert runtime.settings["extensions"]["settings"]["builtin.open-meteo"]["url"] == \
            "https://weather.local/v1/forecast"
    finally:
        runtime.store.close()


def test_weather_url_default_and_own_setting_not_migrated(tmp_path):
    """Значение по умолчанию и пустое — переносить нечего (пустая настройка плагина
    значит дефолт); своя настройка плагина сильнее старого значения (оно лишь удаляется)."""
    first = Runtime(tmp_path)
    try:
        _seed_old_weather_param(first, "https://api.open-meteo.com/v1/forecast")  # дефолт
        first.store.save_settings(dict(first.settings, extensions={
            **first.settings["extensions"], "settings": {"builtin.open-meteo": {"url": "https://own.local/api"}}}))
    finally:
        first.store.close()
    runtime = Runtime(tmp_path)
    try:
        assert runtime.settings["extensions"]["settings"]["builtin.open-meteo"] == \
            {"url": "https://own.local/api"}  # своя настройка не перезаписана
        assert runtime.quasar_params.def_get("server.weather.url") is None
    finally:
        runtime.store.close()
