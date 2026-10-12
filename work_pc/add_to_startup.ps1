# Opens Options Matrix Viewer automatically whenever you sign in to Windows.
# Run once by double-clicking add_to_startup.bat. Undo with remove_from_startup.bat.
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw 'Run setup.bat in this folder first.'
}
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'Options Matrix Viewer.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '-B -m matrix_viewer.run_app'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = 'Options Matrix Viewer'
$shortcut.Save()
Write-Host "Added: $shortcutPath"
Write-Host 'Options Matrix Viewer will open each time you sign in to Windows.'
