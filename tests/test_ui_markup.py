"""Разметка index.html: элементы, которые скрипты ищут по id при старте, не теряются при правках."""
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "server_emulator" / "ui"
HTML = (UI / "index.html").read_text(encoding="utf-8")


def html_ids():
    return set(re.findall(r'\bid="([\w-]+)"', HTML))


def required_ids(script):
    """id, которые скрипт берёт через $('id') / getElementById('id') и без которых не работает."""
    source = (UI / script).read_text(encoding="utf-8")
    return set(re.findall(r"\$\('([\w-]+)'\)", source)) | set(re.findall(r"getElementById\('([\w-]+)'\)", source))


def test_setup_section_and_dialog_are_in_index():
    assert 'id="setup"' in HTML and 'id="setup-dialog"' in HTML
    assert re.search(r'<section id="setup"[^>]*></section>', HTML)
    assert re.search(r'<dialog id="setup-dialog".*?</dialog>', HTML, re.S)


@pytest.mark.parametrize("script", ["setup.js", "server_config.js"])
def test_ids_required_by_script_exist_in_index(script):
    missing = sorted(required_ids(script) - html_ids())
    assert not missing, f"{script}: в index.html нет элементов {missing}"


def test_setup_ids_are_extracted_nontrivially():
    # защита самой проверки: регулярка видит и секцию, и элементы окна
    assert {"setup", "setup-dialog", "setup-form", "setup-protocol"} <= required_ids("setup.js")
