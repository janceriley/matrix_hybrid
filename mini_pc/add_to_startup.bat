@echo off
rem Runs add_to_startup.ps1 even on PCs that block PowerShell scripts (the Windows
rem default), then keeps the window open so you can read the result.
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0add_to_startup.ps1"
set "exit_code=%errorlevel%"
echo.
pause
exit /b %exit_code%
