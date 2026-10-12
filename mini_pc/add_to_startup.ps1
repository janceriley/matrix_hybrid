# Opens Options Matrix Studio automatically whenever you sign in to Windows.
# Run once: right-click > Run with PowerShell. Undo with remove_from_startup.ps1.
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw 'Run setup.ps1 in this folder first.'
}
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'Options Matrix Studio.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '-B -m options_matrix.run_app'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = 'Options Matrix Studio'
$shortcut.Save()
Write-Host "Added: $shortcutPath"
Write-Host 'Options Matrix Studio will open each time you sign in to Windows.'
