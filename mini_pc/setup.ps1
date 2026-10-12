# Creates this folder's own Python environment (.venv) for the mini PC program.
# Run it once on the mini PC: right-click setup.ps1 > Run with PowerShell.
# Use -Python C:\path\to\python.exe to pick a specific Python, and -Dev to add test tools.
param(
    [string]$Python,
    [switch]$Dev
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$venvRoot = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'

function Test-PythonVersion([string]$Executable) {
    try {
        & $Executable -B -c 'import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $venvPython) -or -not (Test-PythonVersion $venvPython)) {
    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($Python) {
        $candidates.Add($Python)
    } else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) {
            foreach ($version in @('-3.13', '-3.12', '-3.11', '-3')) {
                try {
                    $found = & $launcher.Source $version -c 'import sys; print(sys.executable)' 2>$null
                    if ($LASTEXITCODE -eq 0 -and $found) { $candidates.Add([string]$found) }
                } catch { }
            }
        }
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand) { $candidates.Add($pythonCommand.Source) }
    }

    $selectedPython = $null
    foreach ($candidate in $candidates) {
        if (Test-PythonVersion $candidate) {
            $selectedPython = $candidate
            break
        }
    }
    if (-not $selectedPython) {
        throw 'Python 3.10 or newer with tkinter is required. Install it from python.org (tick "tcl/tk" and "py launcher"), then run setup.ps1 again.'
    }
    Write-Host "Creating the Python environment with $selectedPython ..."
    & $selectedPython -B -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the .venv folder.' }
}

$extras = if ($Dev) { '[recording,test]' } else { '[recording]' }
& $venvPython -B -m pip install --disable-pip-version-check -e "$projectRoot$extras"
if ($LASTEXITCODE -ne 0) { throw 'Could not install the mini PC program and its dependencies.' }

& $venvPython -B -c 'import tkinter, pyarrow, pandas_market_calendars, websocket; root = tkinter.Tk(); root.withdraw(); root.destroy(); print("Mini PC program is ready.")'
if ($LASTEXITCODE -ne 0) { throw 'The window system (tkinter) check failed. Use a full python.org installation.' }

$token = Join-Path $projectRoot 'schwab_auth\schwab_token.json'
if (-not (Test-Path -LiteralPath $token)) {
    Write-Host ''
    Write-Host 'Next: put your Schwab credentials in schwab_auth\schwab_token.json'
    Write-Host '(copy it from the old Options_Matrix\oma_app\schwab_auth folder, or run schwab_auth\authorize_schwab.bat).'
}
Write-Host "Done. Start the program with 'Start Mini PC.pyw' (or 'Start Mini PC.bat')."
