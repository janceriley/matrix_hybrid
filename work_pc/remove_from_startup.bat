@echo off
rem Runs remove_from_startup.ps1 even on PCs that block PowerShell scripts (the
rem Windows default), then keeps the window open so you can read the result.
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0remove_from_startup.ps1"
set "exit_code=%errorlevel%"
echo.
pause
exit /b %exit_code%
