"""Общий движок py-скриптов (TASK-017): загрузка модуля по пути с кешем по mtime,
вызов функции с таймаутом (sync/async, 1 или 2 аргумента), текст сбоя.

Один движок для эндпоинтов (<id>.py рядом с JSON, функция handle) и py-сценариев
(TASK-019: handle и функции продолжения диалога). Префикс имени модуля задаётся
видом скриптов (endpoint_script_/scenario_script_), чтобы одноимённые модули
разных видов не пересекались в sys.modules. Движок не знает ни про эндпоинты,
ни про сценарии.
"""
import asyncio
import importlib.util
import inspect
import traceback

TIMEOUT = 10.0


class ScriptError(Exception):
    pass


def failure(exc):
    """Текст ошибки скрипта для ответа 500 и журнала (последние строки трассировки)."""
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return "".join(lines[-3:]).strip()


class ScriptRunner:
    def __init__(self, prefix="script", timeout=TIMEOUT):
        self.prefix = prefix
        self.timeout = timeout
        self._cache = {}  # path → (mtime_ns, module)

    def _module(self, path):
        """Модуль по пути; перечитывается при изменении mtime (правка без рестарта)."""
        stat = path.stat()
        cached = self._cache.get(path)
        if cached and cached[0] == stat.st_mtime_ns:
            return cached[1]
        spec = importlib.util.spec_from_file_location(self.prefix + "_" + path.stem.replace(".", "_"), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._cache[path] = (stat.st_mtime_ns, module)
        return module

    def function(self, path, name="handle"):
        """Функция модуля по имени (handle, продолжения диалога TASK-020) или ScriptError."""
        handle = getattr(self._module(path), name, None)
        if not callable(handle):
            raise ScriptError(f"{path.name}: нет функции {name}(...)")
        return handle

    def forget(self, path):
        self._cache.pop(path, None)

    async def run(self, path, *args, function="handle"):
        """Вызов функции модуля: sync или async, один аргумент или два (первое
        имя функции без параметров — только один; арность решает по сигнатуре).
        Таймаут ScriptRunner.timeout — зависший скрипт не блокирует сервер."""
        handle = self.function(path, function)
        arguments = args if len(inspect.signature(handle).parameters) >= 2 else args[:1]
        result = handle(*arguments)
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, self.timeout)
        return result
