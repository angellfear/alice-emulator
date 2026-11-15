"""Вход в модели по подписке — один раз, в терминале.

    python -m server_emulator.voice_stack.login chatgpt [--state <папка состояния>]

Вход по коду устройства делает LiteLLM (его Authenticator печатает ссылку и код и ждёт
подтверждения в браузере); файл входа ложится в папку состояния эмулятора, дальше токен
обновляется сам. Код устройства требует человека, поэтому вход — отдельной командой, не
из работающего сервера.
"""
import argparse
from pathlib import Path

from .adapters import chatgpt

DEFAULT_STATE = Path(__file__).resolve().parent.parent / "state"


def login_chatgpt(state):
    from litellm.llms.chatgpt.authenticator import Authenticator
    path = chatgpt.auth_file(state)
    Authenticator(auth_file=str(path)).get_access_token()
    return path


SERVICES = {"chatgpt": login_chatgpt}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Вход в модели по подписке")
    parser.add_argument("service", choices=sorted(SERVICES))
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    path = SERVICES[args.service](args.state)
    print(f"Готово: вход сохранён в {path}")


if __name__ == "__main__":
    main()
