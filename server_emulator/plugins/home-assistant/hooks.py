"""Хуки жизненного цикла расширения: менеджер вызывает их через ScriptRunner с
таймаутом; ошибка хука откатывает операцию. Все функции необязательны —
расширению Home Assistant переносить нечего, хуки оставлены пустыми как точки
расширения (миграция настроек понадобится при смене имён полей settings).
"""


def on_install(settings):
    """После распаковки и проверки, до включения (settings — значения настроек)."""


def on_enable(settings):
    """При включении расширения."""


def on_disable(settings):
    """При выключении; файлы не удаляются. Останавливает фоновую WS-задачу умного
    дома (sdk.extension_runtime — синхронный stop, отдельный поток хука)."""
    from server_emulator import sdk
    sdk.extension_runtime("builtin.home-assistant").stop()


def migrate(from_version, settings):
    """При обновлении: from_version — версия, с которой переносим настройки."""
