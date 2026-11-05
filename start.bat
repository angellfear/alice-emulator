@echo off
rem Запуск эмулятора с иконкой в трее, без консоли. Python — из .venv проекта.
rem Нет .venv и pythonw — подсказка вместо тихого сбоя (TASK-104: первая установка).
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" tools\start_emulator.pyw %*
  exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw tools\start_emulator.pyw %*
  exit /b 0
)
echo .venv нет и pythonw не найден - сначала запустите install.bat из этой папки.
pause
exit /b 1
