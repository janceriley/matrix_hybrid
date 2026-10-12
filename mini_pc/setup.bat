@echo off
rem Runs setup.ps1 even on PCs that block PowerShell scripts (the Windows default),
rem then keeps the window open so you can read the result.
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "exit_code=%errorlevel%"
echo.
pause
exit /b %exit_code%
