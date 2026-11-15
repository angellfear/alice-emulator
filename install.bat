@echo off
rem Установка эмулятора: Python 3.11-3.13, рабочая копия, .venv, зависимости.
rem Первая установка работает на чистой Windows 10+: Python ставится сам
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
rem Подходящие версии Python - 3.11-3.13: у зависимостей (zstandard, onnxruntime, ...)
rem есть готовые сборки под них, а на более новых (3.14/3.15) pip уходит в компиляцию
rem под MSVC и падает на машине без Visual C++. Более новый Python из PATH пропускаем -
rem тогда скрипт ставит собственный Python %PYVER%.
set "PYCHECK=import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)"

rem --- 1. Python 3.11-3.13: PATH, py-лаунчер или известные папки установки ---
call :find_python
if defined PY goto have_python
echo Python не найден - устанавливаю %PYVER% ...
call :install_python || exit /b 1

:have_python
"%PY%" -c "%PYCHECK%" >nul 2>nul
if not errorlevel 1 goto python_ok
echo Найден Python неподходящей версии (нужен 3.11-3.13) - устанавливаю %PYVER% ...
call :install_python || exit /b 1

:python_ok

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
rem Существующий .venv могли создать старой версией скрипта на неподходящем Python - проверяем и пересоздаём.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python" -c "%PYCHECK%" >nul 2>nul
  if not errorlevel 1 goto venv_ready
  echo Существующий .venv создан неподходящим Python - пересоздаю окружение ...
  rmdir /s /q ".venv"
)
if not exist ".venv" (
  echo Создаю виртуальное окружение .venv ...
  "%PY%" -m venv .venv || exit /b 1
)
:venv_ready
".venv\Scripts\python" -m pip install --upgrade pip >nul
rem Editable: после обновления рабочей копии на новый тег код подхватывается без переустановки.
rem Без -q: с ним установка зависимостей на несколько минут выглядит зависшей.
".venv\Scripts\python" -m pip install -e ".[emulator]"
if errorlevel 1 exit /b 1

echo Установка завершена. Запуск: start.bat (или .venv\Scripts\python -m server_emulator.server)
exit /b 0

:find_python
rem Подходящий интерпретатор: python/py из PATH или известные папки установки; путь - в PY.
set "PY="
rem python из WindowsApps - заглушка Магазина: её запуск открывает Microsoft Store, пропускаем.
for /f "delims=" %%p in ('where python 2^>nul') do (
  if not defined PY (
    echo %%p | findstr /i "WindowsApps" >nul
    if errorlevel 1 (
      "%%p" -c "%PYCHECK%" >nul 2>nul
      if not errorlevel 1 set "PY=%%p"
    )
  )
)
if defined PY exit /b 0
rem py-лаунчер: пробуем подходящие минорные версии по имени (py -3 сам выберет самый новый,
rem вплоть до непригодного 3.15, поэтому перечисляем явно).
for %%m in (3.13 3.12 3.11) do (
  if not defined PY (
    py -%%m -c "%PYCHECK%" >nul 2>nul
    if not errorlevel 1 for /f "delims=" %%p in ('py -%%m -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%p"
  )
)
if defined PY exit /b 0
rem Известные папки установки; первой - папка, куда этот скрипт ставит Python сам.
for %%d in ("%LOCALAPPDATA%\AliceEmulator\Python312" "%LOCALAPPDATA%\Programs\Python\Python313" "%LOCALAPPDATA%\Programs\Python\Python312" "%LOCALAPPDATA%\Programs\Python\Python311" "%ProgramFiles%\Python313" "%ProgramFiles%\Python312" "%ProgramFiles%\Python311") do (
  if not defined PY if exist "%%~d\python.exe" (
    "%%~d\python.exe" -c "%PYCHECK%" >nul 2>nul
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
set /a TRIES=0
:download
rem curl.exe встроен в Windows 10 1803+ и рисует полоску прогресса; иначе PowerShell без полоски.
if exist "%SystemRoot%\System32\curl.exe" (
  "%SystemRoot%\System32\curl.exe" -f -# -L --retry 2 --retry-delay 2 -o "%INSTALLER%" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe' -OutFile '%INSTALLER%' -UseBasicParsing"
)
if not errorlevel 1 goto download_ok
set /a TRIES+=1
if %TRIES% LSS 2 (
  echo Скачивание не удалось - пробую ещё раз ...
  ping -n 3 127.0.0.1 >nul
  goto download
)
echo Скачать установщик не удалось - проверьте интернет & exit /b 1
:download_ok
echo Устанавливаю Python в %PYDIR% - несколько минут, без прав администратора ...
"%INSTALLER%" /quiet /norestart InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0 TargetDir="%PYDIR%"
rem 3010 - установка прошла, но требуется перезагрузка: это успех.
set "RC=%errorlevel%"
if "%RC%"=="3010" (
  echo Python установлен, потребуется перезагрузка Windows.
  set "RC=0"
)
if not "%RC%"=="0" (echo Установщик Python завершился с ошибкой & exit /b 1)
set "PY=%PYDIR%\python.exe"
if not exist "%PY%" (
  echo Python не появился в %PYDIR% - установите вручную: https://www.python.org/downloads/
  exit /b 1
)
rem Установщик возвращает управление до полной записи файлов (Lib, DLLs) - ждём работающий Python.
set /a TRIES=0
:wait_python
"%PY%" -c "%PYCHECK%" >nul 2>nul
if not errorlevel 1 exit /b 0
set /a TRIES+=1
if %TRIES% LSS 45 (
  ping -n 3 127.0.0.1 >nul
  goto wait_python
)
echo Python в %PYDIR% не заработал после установки - установите вручную: https://www.python.org/downloads/
exit /b 1

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
set "ZIPURL="
for /f "usebackq delims=" %%u in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop';[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;$tag=$null;try{$tag=(Invoke-RestMethod 'https://api.github.com/repos/%OWNER%/%NAME%/releases/latest').tag_name}catch{};if($tag){Write-Output ('https://github.com/%OWNER%/%NAME%/archive/refs/tags/'+$tag+'.zip')}else{Write-Output ('https://github.com/%OWNER%/%NAME%/archive/refs/heads/main.zip')}"`) do set "ZIPURL=%%u"
if not defined ZIPURL (echo Не удалось определить адрес ZIP - проверьте интернет & exit /b 1)
echo Скачиваю ZIP: %ZIPURL%
if exist "%SystemRoot%\System32\curl.exe" (
  "%SystemRoot%\System32\curl.exe" -f -# -L -o "%TEMP%\%NAME%.zip" "%ZIPURL%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%ZIPURL%' -OutFile '%TEMP%\%NAME%.zip' -UseBasicParsing"
)
if errorlevel 1 (echo Скачать ZIP не удалось - проверьте интернет & exit /b 1)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop';$d=Join-Path $env:TEMP '%NAME%-unzip';if(Test-Path $d){Remove-Item $d -Recurse -Force};Expand-Archive -Path (Join-Path $env:TEMP '%NAME%.zip') -DestinationPath $d -Force;$root=(Get-ChildItem $d | Select-Object -First 1).FullName;if(Test-Path '%DEST%'){Remove-Item '%DEST%' -Recurse -Force};Move-Item $root '%DEST%';Remove-Item (Join-Path $env:TEMP '%NAME%.zip') -Force;Remove-Item $d -Recurse -Force"
if errorlevel 1 (echo Распаковать ZIP не удалось - проверьте интернет & exit /b 1)
exit /b 0
