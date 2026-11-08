"""Встроенные плагины (server_emulator/plugins/<имя>/, id builtin.<имя>): всегда
установлены, включены по умолчанию, включение/выключение и настройки — как у внешних;
удалить/обновить/откатить и установить чужое с id builtin.* нельзя (API — 400).
Реестр поставщиков собирает их музыку без установки (test_music_provider.py —
механика реестра на внешних расширениях).
"""
import json

import pytest

from server_emulator.extensions import EMULATOR_VERSION
from server_emulator.extensions.manager import BUILTIN_DIR, MANIFEST
from server_emulator import music, providers
from server_emulator.runtime import Runtime

BUILTINS = ("builtin.spotify", "builtin.yandex-music")


def make_runtime(tmp_path):
    """Runtime с выключенными встроенными плагинами — база для проверок операций."""
    runtime = Runtime(tmp_path)
    state = runtime.settings.setdefault("extensions", {})
    for ext_id in state["installed"]:
        if ext_id.startswith("builtin."):
            state["enabled"][ext_id] = False
    runtime.extensions.load_enabled()
    return runtime


async def test_builtin_plugins_installed_without_install(tmp_path):
    """Встроенные плагины видны без установки: запись с builtin=True, версия = версия
    эмулятора, включены по умолчанию; манифест валиден настоящим менеджером."""
    runtime = Runtime(tmp_path)
    try:
        state = runtime.settings["extensions"]
        for ext_id in BUILTINS:
            entry = state["installed"][ext_id]
            assert entry["builtin"] is True
            assert entry["version"] == EMULATOR_VERSION
            assert state["enabled"][ext_id] is True
            manifest = runtime.extensions._load_manifest(ext_id)
            assert manifest["id"] == ext_id and manifest["license"] == "MIT"
            assert runtime.extensions._active_dir(ext_id).is_dir()
        listing = {item["id"]: item for item in runtime.extensions.listing()["extensions"]}
        for ext_id in BUILTINS:
            assert listing[ext_id]["builtin"] is True and listing[ext_id]["enabled"] is True
            assert listing[ext_id]["class"] == "code"  # music/*.py — код
            assert "music" in listing[ext_id]["folders"]
    finally:
        await runtime.close()


async def test_builtin_providers_in_registry_by_default(tmp_path):
    """Без внешних расширений «Поставщики данных» показывает музыку встроенных:
    оба в реестре, авто — первый по алфавиту, origin builtin, Яндекс — по service."""
    runtime = Runtime(tmp_path)
    try:
        assert sorted(providers.PROVIDERS["music"]) == sorted(BUILTINS)
        records = providers.PROVIDERS["music"]
        assert records["builtin.spotify"]["origin"] == "builtin"
        assert records["builtin.yandex-music"]["service"] == "yandex-music"
        assert records["builtin.yandex-music"]["origin"] == "builtin"
        assert music.is_yandex_music(records["builtin.yandex-music"])
        assert music.provider_key(records["builtin.yandex-music"]) == "MUSIC_PROVIDER_YANDEX_MUSIC"
        assert not music.is_yandex_music(records["builtin.spotify"])
        assert music.selected(runtime.settings)["id"] == "builtin.spotify"  # авто: по алфавиту
    finally:
        await runtime.close()


async def test_builtin_disable_and_enable(tmp_path):
    """Выключение встроенного — как у внешнего: поставщик уходит из реестра,
    включение возвращает (настройки сохраняются в settings["extensions"])."""
    runtime = Runtime(tmp_path)
    try:
        await runtime.extensions.disable("builtin.spotify")
        assert "builtin.spotify" not in providers.PROVIDERS["music"]
        assert runtime.settings["extensions"]["enabled"]["builtin.spotify"] is False
        await runtime.extensions.enable("builtin.spotify")
        assert "builtin.spotify" in providers.PROVIDERS["music"]
        values = await runtime.extensions.set_values("builtin.yandex-music", {"market": "en"})
        assert values["values"]["market"] == "en"  # настройки встроенного — как у внешнего
    finally:
        await runtime.close()


async def test_builtin_ops_forbidden(tmp_path):
    runtime = make_runtime(tmp_path)
    try:
        with pytest.raises(ValueError, match="нельзя удалить"):
            await runtime.extensions.uninstall("builtin.spotify")
        with pytest.raises(ValueError, match="не обновляется"):
            await runtime.extensions.update("builtin.spotify")
        with pytest.raises(ValueError, match="нечего откатывать"):
            await runtime.extensions.rollback("builtin.yandex-music")
        # записи и после отказов на месте
        assert runtime.settings["extensions"]["installed"]["builtin.spotify"]["builtin"] is True
    finally:
        await runtime.close()


async def test_builtin_entry_without_folder_is_removed(tmp_path):
    """Запись встроенного, чьей папки в поставке больше нет, уходит из installed/enabled:
    карточки с ошибкой не остаётся; настоящие встроенные остаются, настройки сохраняются."""
    runtime = Runtime(tmp_path)
    try:
        state = runtime.settings.setdefault("extensions", {})
        state["installed"]["builtin.gone"] = {"version": EMULATOR_VERSION, "builtin": True,
                                              "source": "", "dev": None}
        state["enabled"]["builtin.gone"] = True
        state["settings"]["builtin.gone"] = {"market": "de"}
        runtime.extensions.load_enabled()
        assert "builtin.gone" not in state["installed"]
        assert "builtin.gone" not in state["enabled"]
        assert state["settings"].get("builtin.gone") == {"market": "de"}  # настройки — на случай возврата
        listing = {item["id"]: item for item in runtime.extensions.listing()["extensions"]}
        assert "builtin.gone" not in listing
        assert listing["builtin.spotify"]["builtin"] is True  # настоящие встроенные на месте
        assert "error" not in listing["builtin.spotify"]
    finally:
        await runtime.close()


async def test_install_with_builtin_id_rejected(tmp_path):
    """Установить чужое расширение с id builtin.* нельзя: id зарезервирован."""
    runtime = make_runtime(tmp_path)
    ext = tmp_path / "ext" / "fake-builtin"
    ext.mkdir(parents=True)
    manifest = {"id": "builtin.fake", "name": "F", "version": "1.0.0", "description": "d",
                "author": "A", "license": "MIT",
                "requires": {"emulator": f">={EMULATOR_VERSION}", "sdk": ">=2.0.0"},
                "permissions": {}}
    (ext / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="builtin\\."):
            await runtime.extensions.install({"url": str(ext)}, confirm=True)
        assert "builtin.fake" not in runtime.settings["extensions"]["installed"]
    finally:
        await runtime.close()


async def test_builtin_plugin_dir_is_packaged(tmp_path):
    """Каталог plugins/ объявлен в package-data: манифесты и музыка попадают в поставку."""
    import tomllib
    pyproject = BUILTIN_DIR.parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["server_emulator"]
    assert "plugins/*/extension.json" in patterns
    assert "plugins/*/music/*.py" in patterns
