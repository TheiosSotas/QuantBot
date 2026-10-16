@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
pushd "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" windows_launcher.py %1
    goto finished
)
py -3 -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
if not errorlevel 1 (
    py -3 windows_launcher.py %1
    goto finished
)
python -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
if not errorlevel 1 (
    python windows_launcher.py %1
    goto finished
)
echo Python 3.10+ was not found. Install Python with Add Python to PATH enabled.
:finished
echo.
echo Finished. If an error appeared, keep the message above.
pause
popd
endlocal
