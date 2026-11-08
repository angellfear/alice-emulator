"""Плагины данных сценариев: код, который наполняет датасеты в БД (шаг refresh).

Плагин — async (key, args) → значение записи датасета; запись в БД делает
executor. Сценарий знает только форму записи, не провайдера.

weather — диспетчер выбранного поставщика: данные даёт поставщик вида weather
(providers.py, forecast(ctx, location) — форма Open-Meteo), датасет — кеш записей.
Поставщика нет или он не ответил — исключение: сценарий получает None (или старую
запись с пометкой) и отвечает честно. Настоящий источник — встроенный плагин
builtin.open-meteo (plugins/open-meteo/), внешний — расширение с папкой weather/.
"""
from __future__ import annotations


def weather_plugin(runtime):
    from . import providers as _prov
    _prov.require("weather", ("forecast",))  # потребитель вида объявляет требования
    """Плагин weather (диспетчер): ключ/аргументы датасета → прогноз выбранного
    поставщика. args: {"location": {"latitude", "longitude", "timezone"?}} —
    координаты дома из параметров пользователя."""
    async def run(key, args):
        from . import providers
        provider = providers.selected("weather", runtime.settings)
        if provider is None:
            raise LookupError("нет включённого поставщика погоды")
        context = None
        manager = getattr(runtime, "extensions", None)
        if manager is not None:
            try:
                context = manager.context(provider["ext_id"])
            except Exception:  # noqa: BLE001 — записи плагина нет (например, тест-заглушка)
                context = None
        if context is None:
            from .extensions.context import ExtensionContext
            context = ExtensionContext(provider["ext_id"], {})
        location = dict((args or {}).get("location") or {})
        return await provider["forecast"](context, location)
    return run
