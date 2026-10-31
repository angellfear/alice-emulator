"""ExtensionContext (TASK-053): настройки расширения для его кода — только чтение.

Код расширения получает свой ExtensionContext: режимы — `ctx.extension`
(intelligence.answer), действия — `execution.extension` (Executor/SDK),
адаптеры голосового стека — аргумент `extension=`, если функция его принимает
(voice_stack.call_adapter). Значения — values(ext_id) менеджера: defaults
манифеста + сохранённые, строки вида `env:ИМЯ` раскрыты через auth.secret —
как секреты провайдеров (agent/memory.py, settings_service), в настройках
хранится ссылка, само значение живёт в окружении процесса. Контекст создаётся
заново на каждый вызов кода расширения: правка настроек во вкладке
«Расширения» действует без рестарта. repr/str маскируют строковые значения,
чтобы секреты не утекали в журналы и трассы.

Владельца действия Executor и SDK находят через action_context(name): менеджер
текущего Runtime передаётся bind'ом (ставит ExtensionManager.__init__, как
voice_stack.adapters.configure / behavior.set_extension_layers) — из прод-кода
это единственный Runtime, в тестах — последний созданный. Сам контекст никто
не кэширует: чужие настройки код расширения получить не может.
"""
from __future__ import annotations

from types import MappingProxyType

from ..auth import secret

MASK = "***"  # маска строкового значения в repr/str (секреты не читаются из журнала)

_manager = None  # ExtensionManager текущего Runtime — ставит ExtensionManager.__init__


def bind_manager(manager):
    """Сделать менеджер доступным Executor/SDK (action_context) — вызывает
    ExtensionManager.__init__ последнего созданного Runtime."""
    global _manager
    _manager = manager


def action_context(name):
    """ExtensionContext расширения-владельца действия name (префикс
    contributes.actions/id или объявление в permissions.actions, правило
    регистрации TASK-033) или None — встроенное/неизвестное действие, менеджера
    нет. Новый объект на каждый вызов (TASK-053)."""
    manager = _manager
    if manager is None or not hasattr(manager, "action_context"):
        return None
    try:
        return manager.action_context(name)
    except Exception:  # noqa: BLE001 — поиск контекста не должен ломать действие
        return None


def _expand(value):
    """env:ИМЯ → значение из окружения (auth.secret); не-строки — как есть."""
    return secret(value) if isinstance(value, str) else value


def _masked(value):
    """Значение для repr/str: строки заменяются маской, остальное — как есть."""
    return MASK if isinstance(value, str) and value else value


class ExtensionContext:
    """Настройки одного расширения для его кода (TASK-053), только чтение.

    id — id расширения; settings — MappingProxyType всех значений (defaults
    манифеста + сохранённые; строки `env:ИМЯ` раскрыты через auth.secret);
    get(key, default=None) — значение одной настройки. Экземпляр создаётся
    заново на каждый вызов кода расширения (режим — ctx.extension, действие —
    execution.extension, адаптер — аргумент extension), поэтому правка настроек
    действует без рестарта. repr/str маскируют строковые значения.
    """

    __slots__ = ("_id", "_settings")

    def __init__(self, ext_id, values):
        self._id = str(ext_id)
        self._settings = MappingProxyType(
            {str(key): _expand(value) for key, value in dict(values or {}).items()})

    @property
    def id(self):
        """id расширения."""
        return self._id

    @property
    def settings(self):
        """Все значения настроек (MappingProxyType) — только чтение."""
        return self._settings

    def get(self, key, default=None):
        """Значение настройки key или default."""
        return self._settings.get(key, default)

    def __repr__(self):
        values = ", ".join(f"{key}={_masked(value)!r}" for key, value in self._settings.items())
        return f"ExtensionContext(id={self._id!r}, settings={{{values}}})"

    __str__ = __repr__
