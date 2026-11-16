"""Перевод строки в запускаторах: cmd не разбирает .bat с одними LF.

Файл .bat попадает к человеку ровно таким, каким лежит в репозитории (ZIP с GitHub,
клон без core.autocrlf), поэтому CRLF обязан быть внутри файла: без него cmd режет
строки посередине и вместо установки выводит «is not recognized».
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_windows_launchers_have_crlf():
    for name in ("install.bat", "start.bat"):
        data = (ROOT / name).read_bytes()
        assert data.count(b"\r\n") == data.count(b"\n"), f"{name}: не каждая строка заканчивается CRLF"
        assert not data.replace(b"\r\n", b"").count(b"\r"), f"{name}: CR не в паре с LF"
        # BOM перед @echo off: cmd читает его как часть команды первой строки
        assert not data.startswith(b"\xef\xbb\xbf"), f"{name}: BOM в начале файла"


def test_shell_launchers_have_lf():
    for name in ("install.sh", "start.sh"):
        assert b"\r" not in (ROOT / name).read_bytes(), f"{name}: CR ломает bash"
