"""Пакет расширений эмулятора: схема манифеста schema.json,
менеджер manager.py (установка, состояние, хуки), источники sources.py
(скачивание/распаковка — общие с реестром сценариев). Контракт:
docs/contracts/extensions.md. Точки подключения берут включённые
расширения из runtime.extensions (ExtensionManager.loaded, contribution_dirs()).
"""
from ..version import released

# Версия эмулятора для сверки requires.emulator и версии встроенных расширений —
# номер релиза из .release-please-manifest.json, его поднимает release-please.
EMULATOR_VERSION = released()
