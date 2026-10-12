# Creates this folder's own Python environment (.venv) for the work PC viewer.
# Run it once on the work PC: right-click setup.ps1 > Run with PowerShell.
# The viewer uses the matrix code in the sibling mini_pc folder, so copy the
# whole multi_matrix folder to this PC first.
# Use -Python C:\path\to\python.exe to pick a specific Python, and -Dev to add test tools.
param(
    [string]$Python,
    [switch]$Dev
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$miniPc = Join-Path (Split-Path $projectRoot -Parent) 'mini_pc'
$venvRoot = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath (Join-Path $miniPc 'options_matrix\app.py'))) {
    throw "The mini_pc folder was not found next to this folder ($miniPc). Copy the whole multi_matrix folder to this PC."
}

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

# Shared matrix code first (without the recorder's pyarrow), then the viewer.
& $venvPython -B -m pip install --disable-pip-version-check -e "$miniPc"
if ($LASTEXITCODE -ne 0) { throw 'Could not install the shared matrix code from the mini_pc folder.' }
$target = if ($Dev) { "$projectRoot[test]" } else { $projectRoot }
& $venvPython -B -m pip install --disable-pip-version-check -e "$target"
if ($LASTEXITCODE -ne 0) { throw 'Could not install the work PC viewer.' }
if ($Dev) {
    & $venvPython -B -m pip install --disable-pip-version-check "pyarrow>=15.0.0"
}

& $venvPython -B -c 'import tkinter, options_matrix.app, matrix_viewer.viewer_app; root = tkinter.Tk(); root.withdraw(); root.destroy(); print("Work PC viewer is ready.")'
if ($LASTEXITCODE -ne 0) { throw 'The window system (tkinter) check failed. Use a full python.org installation.' }
Write-Host "Done. Start the viewer with 'Start Work PC.pyw' (or 'Start Work PC.bat')."
