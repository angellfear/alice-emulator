"""Пакет расширений эмулятора (TASK-028/029): схема манифеста schema.json,
менеджер manager.py (установка, состояние, хуки), источники sources.py
(скачивание/распаковка — общие с реестром сценариев). Контракт:
docs/contracts/extensions.md. Точки подключения TASK-030+ берут включённые
расширения из runtime.extensions (ExtensionManager.loaded, contribution_dirs()).
"""

# Версия эмулятора для сверки requires.emulator (зеркало version в pyproject.toml).
EMULATOR_VERSION = "0.1.0"
