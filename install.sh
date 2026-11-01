#!/usr/bin/env bash
# Установка эмулятора (TASK-039): Python 3.11+, git clone последнего тега, venv, зависимости.
# Повторный запуск безопасен: существующий клон и .venv не пересоздаются.
set -e
cd "$(dirname "$0")"

REPO=https://github.com/angellfear/alice-emulator.git

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Нужен Python 3.11 или новее: https://www.python.org/downloads/" >&2
  exit 1
fi

if [ ! -e .git ]; then
  echo "Клонирую последний релиз из $REPO ..."
  TAG=$(git ls-remote --tags --sort=-v:refname "$REPO" "refs/tags/v*" | grep -v '\^{}' | head -1 | cut -f2 | sed 's|refs/tags/||')
  if [ -n "$TAG" ]; then
    echo "Клонирую тег $TAG ..."
    git clone --branch "$TAG" --depth 1 "$REPO" alice-emulator
  else
    echo "Тегов v* пока нет — клонирую основную ветку."
    git clone "$REPO" alice-emulator
  fi
  cd alice-emulator
fi

if [ ! -e .venv ]; then
  echo "Создаю виртуальное окружение .venv ..."
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip -q
# Editable: после обновления рабочей копии на новый тег код подхватывается без переустановки.
.venv/bin/python -m pip install -q -e ".[emulator]"

echo "Установка завершена. Запуск: ./start.sh (или .venv/bin/python -m server_emulator.server)"
