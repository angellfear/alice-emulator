"""Классы риска инструментов: что агент выполняет сам, а что — после подтверждения.

| класс       | примеры                                          | подтверждение |
|-------------|--------------------------------------------------|---------------|
| read        | speakers_list, db_query, web_search/fetch, scenario_*| нет           |
| device      | свет, громкость, speaker_command, alice_action   | нет           |
| external    | действия MCP вне колонки (замки, покупки)        | да            |
| config      | memory_instructions, scenario_activate/revert,   | да            |
|             | routine_create/update                            |               |
| destructive | routine_delete, удаление                         | да            |

Внешний MCP-инструмент: поле risk; без него — из прежнего mode (auto → device,
confirm → external). Заготовка без исполнителя (server_id пуст) — external.
"""
from .. import mcp_hub, web_tools
from ..model_actions import TOOL_NAME as ALICE_ACTION
from . import scenario_edit, scenarios

MEMORY = {"manage_memory": "device", "search_memory": "read", "memory_instructions": "config"}
ROUTINES = {"routine_list": "read", "routine_create": "config", "routine_update": "config", "routine_delete": "destructive"}

RISKS = ("read", "device", "external", "config", "destructive")
CONFIRM = {"external", "config", "destructive"}
BUILTIN = {"speakers_list": "read", "db_query": "read", "speaker_command": "device", **web_tools.RISKS}


def mode_of(risk):
    """Прежний mode для legacy-движка и старых клиентов API."""
    return "confirm" if risk in CONFIRM else "auto"


def tool_risk(runtime, name):
    if name == ALICE_ACTION:
        return "device"
    if name in MEMORY:
        return MEMORY[name]
    if name in ROUTINES:
        return ROUTINES[name]
    if name in scenario_edit.RISKS:
        return scenario_edit.RISKS[name]
    if name in scenarios.RISKS:
        return scenarios.RISKS[name]
    if name in BUILTIN:
        return BUILTIN[name]
    tool = mcp_hub.find_tool(runtime, name)
    if tool is None or not tool.get("server_id"):
        return "external"
    if tool.get("risk") in RISKS:
        return tool["risk"]
    return "device" if tool.get("mode", "auto") == "auto" else "external"


def needs_confirm(runtime, name, arguments=None):
    if name == "scenario_run":
        return scenarios.risk_of(runtime, arguments) in CONFIRM
    return tool_risk(runtime, name) in CONFIRM
