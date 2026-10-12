@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo This program needs its Python environment first.
    echo Double-click setup.bat in the mini_pc folder, then start the program again.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" -B -m options_matrix.run_app %*
exit /b 0
