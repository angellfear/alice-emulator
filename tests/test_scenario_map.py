"""Карта сценариев: генератор отрабатывает, SVG валидны, все сценарии реестра на карте."""
import re
import sys
from pathlib import Path
from xml.dom import minidom

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import scenario_map  # noqa: E402
from server_emulator.scenario_engine import library  # noqa: E402


def test_scenario_map_covers_registry():
    data, files = scenario_map.build()
    plugin_ids = {item["id"] for item, _origin, _path in scenario_map.plugin_scenarios(data.manifests)}
    expected = set(library.SCENARIOS) | plugin_ids
    assert {s.id for s in data.scenarios} == expected
    assert len(data.scenarios) == len(expected)
    pages = "".join(text for rel, text in files.items() if rel != scenario_map.OVERVIEW)
    for sid in expected:
        assert f'id="{sid}"' in pages, sid
        assert sid in files[scenario_map.OVERVIEW], sid
    for rel, text in files.items():
        minidom.parseString(text.encode("utf-8"))
        assert "viewBox" in text
    # ссылки обзора ведут на существующие страницы
    for href in re.findall(r'href="(scenario-map/[^"#]+)', files[scenario_map.OVERVIEW]):
        assert href in files, href


def test_scenario_map_has_no_private_data():
    _data, files = scenario_map.build()
    blob = "\n".join(files.values())
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", blob)
    for marker in ("tasks/", "TASK-", "state/", "Co-Authored", "token="):
        assert marker not in blob, marker
