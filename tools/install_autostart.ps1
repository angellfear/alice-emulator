# Ярлыки эмулятора (TASK-045): автозапуск (Startup) и, с -Desktop, ярлык на рабочем столе.
# -Remove убирает ярлык автозапуска. Ярлык запускает pythonw tools/start_emulator.pyw (трей без консоли).
param([switch]$Remove, [switch]$Desktop)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'Алиса — эмулятор.lnk'
if ($Remove) { Remove-Item -LiteralPath $startup -ErrorAction SilentlyContinue; return }
$venv = Join-Path $root '.venv\Scripts\pythonw.exe'
$pythonw = if (Test-Path $venv) { $venv } else { (Get-Command pythonw.exe).Source }
$icon = Join-Path $root 'server_emulator\state\alica.ico'
& ($pythonw -replace 'pythonw\.exe$', 'python.exe') -c "import sys; sys.path[:0]=[r'$root', r'$root\tools']; import windows_tray as t; t.STATE.mkdir(parents=True, exist_ok=True); t.write_icon(t.STATE / 'alica.ico')"
$shell = New-Object -ComObject WScript.Shell
function New-Link($path, $arguments) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = $arguments
    $link.WorkingDirectory = $root
    $link.IconLocation = "$icon,0"
    $link.Description = 'Эмулятор сервера Алисы'
    $link.Save()
}
$script = '"' + (Join-Path $root 'tools\start_emulator.pyw') + '"'
if ($Desktop) { New-Link (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Алиса — эмулятор.lnk') $script; return }
New-Link $startup ($script + ' --no-open')
