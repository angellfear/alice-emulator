"""Общий движок py-скриптов (TASK-017): mtime-перечитывание, таймаут, арность,
отсутствие функции, выбор функции по имени. Используется эндпоинтами и py-сценариями."""
import asyncio
import os

import pytest

from server_emulator.scripting import ScriptError, ScriptRunner, failure


def _write(tmp_path, name, code):
    path = tmp_path / name
    path.write_text(code, encoding="utf-8")
    return path


async def test_module_reread_on_mtime_change(tmp_path):
    path = _write(tmp_path, "a.py", "def handle(x):\n    return 1\n")
    runner = ScriptRunner(prefix="test_script")
    assert await runner.run(path, object()) == 1

    # правка содержимого без смены mtime — работает прежний модуль (кеш по mtime_ns)
    saved_ns = os.stat(path).st_mtime_ns
    path.write_text("def handle(x):\n    return 2\n", encoding="utf-8")
    os.utime(path, ns=(os.stat(path).st_atime_ns, saved_ns))
    assert await runner.run(path, object()) == 1

    # смена mtime — перечитывание (шаг в секундах: валидация pyc у importlib — до секунды)
    os.utime(path, ns=(os.stat(path).st_atime_ns, saved_ns + 5_000_000_000))
    assert await runner.run(path, object()) == 2


async def test_async_function_timeout(tmp_path):
    path = _write(tmp_path, "slow.py", "import asyncio\nasync def handle(x):\n    await asyncio.sleep(60)\n")
    runner = ScriptRunner(prefix="test_script", timeout=0.05)
    with pytest.raises(asyncio.TimeoutError):
        await runner.run(path, object())


async def test_sync_one_argument_function(tmp_path):
    path = _write(tmp_path, "sync.py", "def handle(x):\n    return x * 2\n")
    runner = ScriptRunner(prefix="test_script")
    assert await runner.run(path, 21) == 42


async def test_missing_function_is_clear_error(tmp_path):
    path = _write(tmp_path, "empty.py", "VALUE = 1\n")
    runner = ScriptRunner(prefix="test_script")
    with pytest.raises(ScriptError, match="нет функции handle"):
        await runner.run(path, object())


async def test_function_name_selects_entry(tmp_path):
    path = _write(tmp_path, "multi.py",
                  "def handle(ctx, answer):\n    return ('handle', ctx, answer)\n"
                  "def on_duration(ctx, answer):\n    return ('on_duration', ctx, answer)\n")
    runner = ScriptRunner(prefix="test_script")
    ctx, answer = object(), 5
    assert await runner.run(path, ctx, answer) == ("handle", ctx, answer)
    assert await runner.run(path, ctx, answer, function="on_duration") == ("on_duration", ctx, answer)
    # функция одного аргумента получает только первый
    path2 = _write(tmp_path, "one.py", "def handle(ctx):\n    return ctx\n")
    assert await runner.run(path2, ctx, answer) is ctx


def test_failure_keeps_last_traceback_lines():
    try:
        raise ValueError("бум")
    except ValueError as exc:
        text = failure(exc)
    assert "ValueError" in text and "бум" in text
