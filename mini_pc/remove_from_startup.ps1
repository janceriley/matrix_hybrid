# Stops Options Matrix Studio from opening automatically at sign-in.
$ErrorActionPreference = 'Stop'
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'Options Matrix Studio.lnk'
if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath
    Write-Host "Removed: $shortcutPath"
} else {
    Write-Host 'It was not set to open at sign-in.'
}
