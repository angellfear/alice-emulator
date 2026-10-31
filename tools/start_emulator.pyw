"""Запуск эмулятора с иконкой в трее без консоли (pythonw tools/start_emulator.pyw). TASK-045."""
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
logs = ROOT / "server_emulator" / "state" / "logs"
logs.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=logs / "launcher.log", level=logging.INFO, encoding="utf-8")
try:
    import windows_tray
    windows_tray.main(open_panel="--no-open" not in sys.argv)
except Exception:
    logging.exception("Трей не запустился")
    raise
