@echo off
rem Установка эмулятора (TASK-039): Python 3.11+, git clone последнего тега, venv, зависимости.
rem Повторный запуск безопасен: существующий клон и .venv не пересоздаются.
rem Git CLI нужен только на первичной установке; версия и обновление работают без него.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set REPO=https://github.com/angellfear/alice-emulator.git

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>nul
if errorlevel 1 (
  echo Нужен Python 3.11 или новее: https://www.python.org/downloads/
  exit /b 1
)

rem Вне клона: клон на тег последнего релиза в подпапку alice-emulator.
if exist ".git" goto venv

echo Клонирую последний релиз из %REPO% ...
set REF=
for /f "usebackq tokens=2" %%a in (`git ls-remote --tags --sort=-v:refname "%REPO%" "refs/tags/v*" ^| findstr /v "\^{}"`) do (
  if not defined REF set "REF=%%a"
)
if not defined REF (
  echo Тегов v* пока нет - клонирую основную ветку.
  git clone "%REPO%" alice-emulator || exit /b 1
  goto cloned
)
set "TAG=%REF:refs/tags/=%"
echo Клонирую тег %TAG% ...
git clone --branch "%TAG%" --depth 1 "%REPO%" alice-emulator || exit /b 1

:cloned
cd alice-emulator

:venv
if not exist ".venv" (
  echo Создаю виртуальное окружение .venv ...
  python -m venv .venv || exit /b 1
)
.venv\Scripts\python -m pip install --upgrade pip >nul
rem Editable: после обновления рабочей копии на новый тег код подхватывается без переустановки.
.venv\Scripts\python -m pip install -q -e ".[emulator]"
if errorlevel 1 exit /b 1

echo Установка завершена. Запуск: start.bat (или .venv\Scripts\python -m server_emulator.server)
