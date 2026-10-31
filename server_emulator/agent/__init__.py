"""Агент эмулятора: движок ответа модели (docs/contracts/agent.md).

Два движка за одним входом Runtime.chat:
* graph  — LangChain create_agent (LangGraph): треды в state/agent.sqlite3, классы риска
           инструментов, подтверждение через interrupt, сценарии как инструменты;
* legacy — прежний цикл Runtime._chat_legacy (история передаётся вызывающим).

Выбор: settings.agent.engine, переопределяется переменной EMULATOR_AGENT_ENGINE.
Без установленных зависимостей (extra «agent») остаётся legacy.
"""
import os

ENGINES = ("graph", "legacy")


def create(directory):
    """AgentService или None, если LangChain/LangGraph не установлены."""
    try:
        from .service import AgentService
    except ImportError:
        return None
    return AgentService(directory)


def create_routines(runtime):
    """Задания по расписанию поверх агента или None (нет агента или APScheduler)."""
    if runtime.agent is None:
        return None
    try:
        from .routines import Routines
    except ImportError:
        return None
    return Routines(runtime, runtime.directory / "routines.sqlite3")


def engine(runtime):
    """graph — только в режиме «Интеллект: агент» (intelligence.py) при установленном LangGraph."""
    from ..intelligence import level
    if level(runtime) != "agent":
        return "legacy"
    value = os.environ.get("EMULATOR_AGENT_ENGINE") or (runtime.settings.get("agent") or {}).get("engine") or "graph"
    if value not in ENGINES:
        value = "graph"
    return "graph" if value == "graph" and getattr(runtime, "agent", None) is not None else "legacy"
