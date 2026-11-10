"""Снимок настроек на фикстуре базы: GET /api/settings и Runtime.public().

Фикстура tests/fixtures/settings/settings-base.json — обезличенное содержимое БД
текущего формата (легаси-ключей нет). Снимок settings-public.json зафиксирован на коде
до перехода на типизированные модели: после перехода JSON должен совпасть (критерий
приёмки). Служебное поле `_rev` (time.time()) в снимок не входит — тест проверяет
его наличие и тип отдельно.
"""
import json
from pathlib import Path

from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "settings"
BASE = json.loads((FIXTURES / "settings-base.json").read_text(encoding="utf-8"))
SNAPSHOT = json.loads((FIXTURES / "settings-public.json").read_text(encoding="utf-8"))


def _runtime_with_base(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.store.save_settings(BASE)  # старая база: ключи до миграций, без новых умолчаний
    runtime.settings = runtime.settings_service.load()
    return runtime


async def test_settings_roundtrip_full_form(tmp_path):
    """Живой сценарий приёмки: GET → тот же JSON обратно PUT → 200, настройки не
    изменились. В фикстуре заполнены все поля, которые пишут формы UI (agent —
    вкладка «AI агент», voice_stack — применение пресетов, self_update, upstream,
    memory, extensions)."""
    runtime = _runtime_with_base(tmp_path)
    try:
        runner, url = await listen(create_app(runtime=runtime))
        try:
            async with ClientSession(headers={"X-Emulator": "1"}) as client:
                async with client.get(url + "/emulator/api/settings") as response:
                    form = await response.json()
                async with client.put(url + "/emulator/api/settings", json=form) as response:
                    assert response.status == 200, await response.text()
                async with client.get(url + "/emulator/api/settings") as response:
                    again = await response.json()
        finally:
            await runner.cleanup()
        form.pop("_rev"), again.pop("_rev")
        assert form == again == SNAPSHOT
    finally:
        runtime.store.close()


def test_default_passes_all_section_models(tmp_path):
    """DEFAULT проходит каждую модель раздела (критерий шага 3)."""
    from copy import deepcopy

    from server_emulator.settings_service import DEFAULT, SettingsService
    from server_emulator.settings_models import (
        AgentSettings, AliasesSettings, ExtensionsSettings, MemorySettings,
        ModelSettings, PhraseOrderSettings, ProvidersSettings, SelfUpdateSettings,
        SttSettings, TtsSettings, UpstreamSettings, WeatherSettings, checked_section)
    for model, key in ((ModelSettings, "model"), (SttSettings, "stt"), (TtsSettings, "tts"),
                       (MemorySettings, "memory"), (AgentSettings, "agent"),
                       (SelfUpdateSettings, "self_update"), (UpstreamSettings, "upstream"),
                       (WeatherSettings, "weather"), (AliasesSettings, "aliases"),
                       (ProvidersSettings, "providers"), (PhraseOrderSettings, "phrase_order"),
                       (ExtensionsSettings, "extensions")):
        assert checked_section(DEFAULT[key], model, key) is not None
    runtime = Runtime(tmp_path)
    try:
        saved = SettingsService(runtime.store, runtime.scenario_dir).validate(
            deepcopy(DEFAULT), runtime.settings)
        assert saved["agent"]["max_model_calls"] == 6 and saved["agent"]["keep_messages"] == 20
        assert saved["voice_stack"]["speak"] is True
    finally:
        runtime.store.close()


def test_default_aliases_empty(tmp_path):
    """Умолчание aliases без *.local: колонка ходит на настоящие домены Яндекса."""
    runtime = Runtime(tmp_path)
    try:
        assert runtime.settings["aliases"] == {}
    finally:
        runtime.store.close()


def test_public_snapshot_matches_base(tmp_path):
    runtime = _runtime_with_base(tmp_path)
    try:
        public = runtime.public()
        assert isinstance(public["_rev"], float)  # ревизия формы — нестабильное время
        public.pop("_rev")
        assert public == SNAPSHOT
    finally:
        runtime.store.close()


async def test_settings_api_snapshot(tmp_path):
    runtime = _runtime_with_base(tmp_path)
    try:
        runner, url = await listen(create_app(runtime=runtime))
        try:
            async with ClientSession() as client:
                async with client.get(url + "/emulator/api/settings") as response:
                    assert response.status == 200
                    value = await response.json()
        finally:
            await runner.cleanup()
        assert isinstance(value["_rev"], float)
        value.pop("_rev")
        assert value == SNAPSHOT
    finally:
        runtime.store.close()
