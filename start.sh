#!/usr/bin/env bash
# Запуск эмулятора из клона: venv создаётся install.sh; при отсутствии — установка выполняется автоматически.
set -e
cd "$(dirname "$0")"
if [ ! -e .venv/bin/python ]; then
  bash install.sh
fi
exec .venv/bin/python -m server_emulator.server "$@"
