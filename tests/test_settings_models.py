"""Модели разделов настроек: валидация через pydantic, ошибка — 4xx с именем поля."""
import pytest

from server_emulator.runtime import Runtime
from server_emulator.settings_models import FetchStateSettings, OAuthSettings, checked_section


def test_oauth_section():
    assert checked_section({"mode": "upstream"}, OAuthSettings, "oauth") == {"mode": "upstream"}
    assert checked_section({}, OAuthSettings, "oauth") == {"mode": "auto"}  # умолчание модели
    with pytest.raises(ValueError, match="oauth.mode"):
        checked_section({"mode": "bogus"}, OAuthSettings, "oauth")
    with pytest.raises(ValueError, match="oauth.oops"):
        checked_section({"mode": "auto", "oops": 1}, OAuthSettings, "oauth")  # extra="forbid"


def test_fetch_state_section():
    assert checked_section({"min_interval_s": "5"}, FetchStateSettings, "fetch_state") == {"min_interval_s": 5.0}
    assert checked_section({"min_interval_s": None}, FetchStateSettings, "fetch_state") == {"min_interval_s": 10.0}
    assert checked_section({}, FetchStateSettings, "fetch_state") == {"min_interval_s": 10.0}
    with pytest.raises(ValueError, match="fetch_state.min_interval_s"):
        checked_section({"min_interval_s": "abc"}, FetchStateSettings, "fetch_state")
    with pytest.raises(ValueError, match="fetch_state.min_interval_s"):
        checked_section({"min_interval_s": -1}, FetchStateSettings, "fetch_state")  # ge=0


def test_section_models_field_errors():
    from server_emulator.settings_models import (AgentSettings, ExtensionsSettings,
                                                 PhraseOrderSettings, ProvidersSettings,
                                                 SelfUpdateSettings, UpstreamSettings,
                                                 VoiceStackSettings)
    assert checked_section({}, SelfUpdateSettings, "self_update")["repo"] == "angellfear/alice-emulator"
    with pytest.raises(ValueError, match="self_update.repo"):
        checked_section({"repo": "norepo"}, SelfUpdateSettings, "self_update")
    with pytest.raises(ValueError, match="self_update.channel"):
        checked_section({"channel": "dev"}, SelfUpdateSettings, "self_update")
    with pytest.raises(ValueError, match="self_update.interval_hours"):
        checked_section({"interval_hours": 0}, SelfUpdateSettings, "self_update")
    assert checked_section({"kinds": ["погода"], "plugins": {"погода": ["builtin.open-meteo"]}},
                           PhraseOrderSettings, "phrase_order")["plugins"]["погода"] == ["builtin.open-meteo"]
    with pytest.raises(ValueError, match="phrase_order.plugins"):
        checked_section({"plugins": {"погода": [42]}}, PhraseOrderSettings, "phrase_order")
    assert checked_section({"погода": "builtin.open-meteo"}, ProvidersSettings, "providers") == {"погода": "builtin.open-meteo"}
    assert checked_section({"погода": None}, ProvidersSettings, "providers") == {"погода": ""}  # прежнее приведение
    assert checked_section({"sources": ["https://ext.example.test"]}, ExtensionsSettings, "extensions")["sources"] == ["https://ext.example.test"]
    with pytest.raises(ValueError, match="extensions.sources"):
        checked_section({"sources": ["  "]}, ExtensionsSettings, "extensions")
    with pytest.raises(ValueError, match="extensions.installed"):
        checked_section({"installed": {"ext": "строка"}}, ExtensionsSettings, "extensions")
    upstream = checked_section({}, UpstreamSettings, "upstream")
    assert upstream["dns_servers"] == ["77.88.8.8", "1.1.1.1"] and upstream["journal"]["max_age_days"] == 14
    with pytest.raises(ValueError, match="upstream.journal.max_age_days"):
        checked_section({"journal": {"max_age_days": "год"}}, UpstreamSettings, "upstream")
    agent = checked_section({}, AgentSettings, "agent")
    assert agent["daily_model_calls"] == 0 and agent["pii"] == ["credit_card", "secret"]
    assert checked_section({"speak": False, "card": False}, VoiceStackSettings, "voice_stack") == {
        "speak": False, "card": True, "asr": "", "tts": "", "llm": ""}  # без озвучки карточка включена
    with pytest.raises(ValueError, match="voice_stack.speak"):
        checked_section({"speak": "да"}, VoiceStackSettings, "voice_stack")


def test_provider_sections_keep_adapter_fields():
    """Пресеты voice_stack пишут в секции adapter и его поля — форма открыта,
    неизвестные ключи проходят как есть."""
    from server_emulator.settings_models import ModelSettings, SttSettings
    assert checked_section({"url": "", "adapter": "openai", "port": 59123, "stub_text": "заглушка"},
                           ModelSettings, "model") == {
        "url": "", "model": "", "api_key": "", "temperature": 0.3, "stub_text": "заглушка",
        "adapter": "openai", "port": 59123}
    with pytest.raises(ValueError, match="model.url"):
        checked_section({"url": "not-a-url"}, ModelSettings, "model")  # нет схемы — как раньше
    with pytest.raises(ValueError, match="stt.url"):
        checked_section({"url": "ftp://x"}, SttSettings, "stt")


def test_secrets_do_not_leak():
    """api_key/webhook_token/token — SecretStr: repr модели и текст ошибки
    без реального значения; в настройки возвращается как было."""
    from server_emulator.settings_models import ModelSettings, SelfUpdateSettings
    model = ModelSettings(url="https://api.example.test/v1", api_key="test-secret-value")
    assert "test-secret-value" not in repr(model) and "test-secret-value" not in str(model)
    assert checked_section({"api_key": "test-secret-value"}, ModelSettings, "model")["api_key"] == "test-secret-value"
    with pytest.raises(ValueError) as exc:
        checked_section({"url": "ssh://x", "api_key": "test-secret-value"}, ModelSettings, "model")
    assert "test-secret-value" not in str(exc.value)
    saved = checked_section({"token": "env:TEST_TOKEN"}, SelfUpdateSettings, "self_update")
    assert saved["token"] == "env:TEST_TOKEN"  # ссылка env — не секрет и не маскируется здесь


def test_validate_uses_models(tmp_path):
    """Ручные проверки oauth/fetch_state заменены моделями: 4xx с именем поля,
    умолчания модели доливаются в раздел, разделы без формы — из current."""
    runtime = Runtime(tmp_path)
    try:
        current = runtime.settings
        form = dict(current, oauth={}, fetch_state={"min_interval_s": None})
        saved = runtime.settings_service.validate(form, current)
        assert saved["oauth"] == {"mode": "auto"}
        assert saved["fetch_state"] == {"min_interval_s": 10.0}
        with pytest.raises(ValueError, match="oauth.mode"):
            runtime.settings_service.validate(dict(current, oauth={"mode": "bogus"}), runtime.settings)
        with pytest.raises(ValueError, match="fetch_state.min_interval_s"):
            runtime.settings_service.validate(dict(current, fetch_state={"min_interval_s": "abc"}), runtime.settings)
        saved = runtime.settings_service.validate(dict(current), current)
        assert saved["oauth"] == current["oauth"] and saved["fetch_state"] == current["fetch_state"]
    finally:
        runtime.store.close()
