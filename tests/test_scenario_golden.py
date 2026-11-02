"""TASK-023: эталонные ответы встроенных сценариев (tests/golden/scenarios.json).

tests/golden/scenarios.json записан tools/record_scenario_golden.py на текущем
коде (до перевода сценариев): для каждого сценария библиотеки — фразы и колбэки
из триггеров плюс вариации со значениями, прогон через стандартный диалоговый
путь (dialog.handle_text / handle_event) с фиксированными временем, гео и
данными погоды (заглушка плагина weather).

Эталон фиксирует наблюдаемое поведение (сообщения, речь, директивы, effects, status,
reason); трасса шагов `trace` — деталь исполнителя (JSON-flow и py-сценарий различаются) —
в эталон не входит (TASK-050).

Здесь тот же прогон повторяется и сверяется с эталоном байт-в-байт. Прогон
детерминирован: время/гео/погода зафиксированы, из ответов берётся проекция без
uuid/timestamps, ключи JSON отсортированы — два запуска подряд дают одинаковый
текст и не меняют эталон. Файл эталона тест не перезаписывает.
"""
import asyncio
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "scenarios.json"
TOOL = ROOT / "tools" / "record_scenario_golden.py"


def tool():
    """tools/record_scenario_golden.py как модуль (tools — не пакет)."""
    spec = importlib.util.spec_from_file_location("record_scenario_golden", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_golden_covers_builtin_library():
    """Эталон покрывает все сценарии встроенной библиотеки (сейчас 19, 087: media снят)."""
    from server_emulator.scenario_engine.library import ERRORS, SCENARIOS

    assert ERRORS == []
    assert len(SCENARIOS) == 19
    recorded = json.loads(GOLDEN.read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in recorded["scenarios"]]
    assert ids == sorted(SCENARIOS)  # все 19, каждый по одному разу


def test_golden_has_no_executor_trace():
    """trace — деталь исполнителя: ни эталон, ни проекция прогона его не содержат."""
    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    recorded = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert "trace" not in set(keys(recorded))
    assert "trace" not in tool().report_view({"handled": True, "trace": [{"step": "x"}]})


async def test_replay_matches_golden_two_runs_stable(tmp_path):
    """Два запуска подряд: одинаковый текст между собой и с эталоном."""
    module = tool()
    first = module.dump(await module.record(workdir=tmp_path / "run1"))
    second = module.dump(await module.record(workdir=tmp_path / "run2"))
    assert second == first  # детерминированность: байт-в-байт между прогонами
    assert first == GOLDEN.read_text(encoding="utf-8")  # эталон не меняется
