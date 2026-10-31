@echo off
rem Запуск эмулятора с иконкой в трее, без консоли. Python — из .venv проекта, иначе pythonw из PATH.
chcp 65001 >nul
cd /d "%~dp0"
set "PYW=pythonw"
if exist ".venv\Scripts\pythonw.exe" set "PYW=.venv\Scripts\pythonw.exe"
start "" "%PYW%" tools\start_emulator.pyw %*
