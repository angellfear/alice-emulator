@echo off
rem Установка эмулятора (TASK-039): Python 3.11+, рабочая копия, .venv, зависимости.
rem Первая установка работает на чистой Windows 10+ (TASK-104): Python ставится сам
rem (winget, иначе установщик python.org без прав администратора), код берётся git-ом
rem или ZIP с GitHub — ничего предустановленного не нужно.
rem Повторный запуск безопасен: существующая рабочая копия и .venv не пересоздаются.
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "REPO=https://github.com/angellfear/alice-emulator.git"
set "OWNER=angellfear"
set "NAME=alice-emulator"
set "PYVER=3.12.10"

rem --- 1. Python 3.11+: PATH, py-лаунчер или известные папки установки ---
call :find_python
if defined PY goto have_python
echo Python не найден - устанавливаю %PYVER% ...
call :install_python || exit /b 1

:have_python
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Найден Python старше 3.11 - обновите: https://www.python.org/downloads/
  exit /b 1
)

rem --- 2. Рабочая копия: клон (.git), распакованный ZIP или скачать ---
if exist ".git" goto venv
if exist "pyproject.toml" if exist "server_emulator" (
  echo Код уже в текущей папке ZIP без git - клон не нужен.
  goto venv
)
if exist "alice-emulator\pyproject.toml" (cd /d alice-emulator & goto venv)
git --version >nul 2>nul
if errorlevel 1 (
  echo Git не найден - скачиваю ZIP с GitHub ...
  call :download_zip || exit /b 1
) else (
  call :clone_repo || exit /b 1
)
cd /d alice-emulator

:venv
if not exist ".venv" (
  echo Создаю виртуальное окружение .venv ...
  "%PY%" -m venv .venv || exit /b 1
)
".venv\Scripts\python" -m pip install --upgrade pip >nul
rem Editable: после обновления рабочей копии на новый тег код подхватывается без переустановки.
".venv\Scripts\python" -m pip install -q -e ".[emulator]"
if errorlevel 1 exit /b 1

echo Установка завершена. Запуск: start.bat (или .venv\Scripts\python -m server_emulator.server)
exit /b 0

:find_python
rem Подходящий интерпретатор: python/py из PATH или известные папки установки; путь - в PY.
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 for /f "delims=" %%p in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%p"
if defined PY exit /b 0
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 for /f "delims=" %%p in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%p"
if defined PY exit /b 0
for %%d in ("%LOCALAPPDATA%\Programs\Python\Python313" "%LOCALAPPDATA%\Programs\Python\Python312" "%LOCALAPPDATA%\Programs\Python\Python311" "%ProgramFiles%\Python313" "%ProgramFiles%\Python312" "%ProgramFiles%\Python311") do (
  if not defined PY if exist "%%~d\python.exe" (
    "%%~d\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~d\python.exe"
  )
)
exit /b 0

:install_python
rem winget, иначе установщик python.org в %LOCALAPPDATA%\AliceEmulator\Python312 (без UAC).
rem PATH после установки обновится только в новых сессиях - дальше используем полный путь.
set "PY="
winget --version >nul 2>nul
if errorlevel 1 goto installer
echo Устанавливаю Python через winget ...
winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
for %%d in ("%LOCALAPPDATA%\Programs\Python\Python312" "%ProgramFiles%\Python312") do (
  if not defined PY if exist "%%~d\python.exe" set "PY=%%~d\python.exe"
)
if defined PY exit /b 0
echo winget не установил Python - пробую установщик python.org ...

:installer
set "PYDIR=%LOCALAPPDATA%\AliceEmulator\Python312"
set "INSTALLER=%TEMP%\python-%PYVER%-amd64.exe"
echo Скачиваю установщик Python %PYVER% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe' -OutFile '%INSTALLER%' -UseBasicParsing"
if errorlevel 1 (echo Скачать установщик не удалось - проверьте интернет & exit /b 1)
echo Устанавливаю Python в %PYDIR% - несколько минут, без прав администратора ...
"%INSTALLER%" /quiet /norestart InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0 TargetDir="%PYDIR%"
if errorlevel 1 (echo Установщик Python завершился с ошибкой & exit /b 1)
set "PY=%PYDIR%\python.exe"
if not exist "%PY%" (
  echo Python не появился в %PYDIR% - установите вручную: https://www.python.org/downloads/
  exit /b 1
)
exit /b 0

:clone_repo
echo Клонирую из %REPO% ...
set "REF="
for /f "usebackq tokens=2" %%a in (`git ls-remote --tags --sort=-v:refname "%REPO%" "refs/tags/v*" 2^>nul ^| findstr /v "\^{}"`) do (
  if not defined REF set "REF=%%a"
)
if not defined REF (
  echo Тегов v* пока нет - клонирую основную ветку.
  git clone "%REPO%" alice-emulator || (echo Клон не удался - проверьте интернет & exit /b 1)
  exit /b 0
)
set "TAG=%REF:refs/tags/=%"
echo Клонирую тег %TAG% ...
git clone --branch "%TAG%" --depth 1 "%REPO%" alice-emulator || (echo Клон не удался - проверьте интернет & exit /b 1)
exit /b 0

:download_zip
rem Без git: последний релиз GitHub, без релизов - ZIP ветки main; распаковка в alice-emulator.
set "DEST=%~dp0alice-emulator"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop';[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;$tag=$null;try{$tag=(Invoke-RestMethod 'https://api.github.com/repos/%OWNER%/%NAME%/releases/latest').tag_name}catch{};if($tag){$u='https://github.com/%OWNER%/%NAME%/archive/refs/tags/'+$tag+'.zip'}else{$u='https://github.com/%OWNER%/%NAME%/archive/refs/heads/main.zip'};$z=Join-Path $env:TEMP '%NAME%.zip';$d=Join-Path $env:TEMP '%NAME%-unzip';if(Test-Path $d){Remove-Item $d -Recurse -Force};Invoke-WebRequest -Uri $u -OutFile $z -UseBasicParsing;Expand-Archive -Path $z -DestinationPath $d -Force;$root=(Get-ChildItem $d | Select-Object -First 1).FullName;if(Test-Path '%DEST%'){Remove-Item '%DEST%' -Recurse -Force};Move-Item $root '%DEST%';Remove-Item $z -Force;Remove-Item $d -Recurse -Force"
if errorlevel 1 (echo Скачать или распаковать ZIP не удалось - проверьте интернет & exit /b 1)
exit /b 0
