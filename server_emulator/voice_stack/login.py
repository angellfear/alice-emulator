"""Вход в модели по подписке — один раз, в терминале.

    python -m server_emulator.voice_stack.login chatgpt [--state <папка состояния>]

Вход по коду устройства делает LiteLLM (его Authenticator печатает ссылку и код и ждёт
подтверждения в браузере); файл входа ложится в папку состояния эмулятора, дальше токен
обновляется сам. Код устройства требует человека, поэтому вход идёт отдельным процессом:
в терминале командой или кнопкой «Войти» в панели (LoginRunner запускает этот же модуль
и показывает ссылку с кодом из его вывода).
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import adapters
from .adapters import chatgpt

DEFAULT_STATE = Path(__file__).resolve().parent.parent / "state"


def login_chatgpt(state):
    from litellm.llms.chatgpt.authenticator import Authenticator
    path = chatgpt.auth_file(state)
    Authenticator(auth_file=str(path)).get_access_token()
    return path


SERVICES = {"chatgpt": login_chatgpt}

OUTPUT_LIMIT = 200  # строк журнала входа, хранимых для показа в UI
CODE_COOLDOWN_SECONDS = 5 * 60  # litellm Authenticator: пауза перед перевыпуском кода после недавней попытки


class LoginRunner:
    """Вход подписки как дочерний процесс (кнопка «Войти» в панели). Authenticator печатает
    ссылку и код в консоль и блокирует поток до подтверждения в браузере — внутри работающего
    сервера (asyncio, pythonw без консоли) ему делать нечего, litellm сам отказывается
    (can_block_current_thread). Процесс один за раз; строки вывода — журнал для диалога входа."""

    def __init__(self, state):
        self.state = Path(state)
        self.proc = None
        self.lines = []
        self.exit = None
        self.service = None

    def status(self):
        auth_ready = cooldown = 0
        if self.service:
            try:
                module = adapters.get(self.service)
            except ValueError:
                module = None
            if module is not None and callable(getattr(module, "auth_file", None)):
                path = module.auth_file(self.state)
                auth_ready = path.is_file()
                try:  # время молчаливого ожидания litellm: код нельзя перевыпустить сразу
                    requested = json.loads(path.read_text(encoding="utf-8")).get("device_code_requested_at")
                    if isinstance(requested, (int, float)):
                        cooldown = int(max(0.0, CODE_COOLDOWN_SECONDS - (time.time() - float(requested))))
                except (OSError, ValueError):
                    pass
        return {"service": self.service, "running": self.proc is not None and self.proc.poll() is None,
                "done": self.exit is not None, "exit": self.exit, "output": list(self.lines),
                "auth_ready": auth_ready, "cooldown": cooldown}

    def start(self, service):
        if service not in SERVICES:
            raise ValueError("service: " + "|".join(sorted(SERVICES)))
        if self.proc is not None and self.proc.poll() is None:
            return self.status()  # вход уже идёт — UI просто показывает его вывод
        self.service, self.lines, self.exit = service, [], None
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "server_emulator.voice_stack.login", service, "--state", str(self.state)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            cwd=Path(__file__).resolve().parents[2], env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        threading.Thread(target=self._pump, daemon=True).start()
        return self.status()

    def _pump(self):
        proc = self.proc
        try:
            for line in proc.stdout:  # блокирующее чтение в своём потоке, не в цикле сервера
                self.lines.append(line.rstrip())
                del self.lines[:-OUTPUT_LIMIT]
        finally:
            proc.wait()
            self.exit = proc.returncode

    def cancel(self):
        if self.proc is None or self.proc.poll() is not None:
            return False
        self.proc.terminate()
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Вход в модели по подписке")
    parser.add_argument("service", choices=sorted(SERVICES))
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    path = SERVICES[args.service](args.state)
    print(f"Готово: вход сохранён в {path}")


if __name__ == "__main__":
    main()
