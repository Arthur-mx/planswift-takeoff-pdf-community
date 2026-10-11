@echo off
setlocal
cd /d "%~dp0"
py -3.13 -c "import sys; assert sys.version_info[:2] == (3,13)" >nul 2>&1
if not errorlevel 1 (
    py -3.13 install.py
    goto finished
)
py -3 -c "import sys; assert (3,10) <= sys.version_info[:2] <= (3,13)" >nul 2>&1
if not errorlevel 1 (
    py -3 install.py
    goto finished
)
python -c "import sys; assert (3,10) <= sys.version_info[:2] <= (3,13)" >nul 2>&1
if not errorlevel 1 (
    python install.py
    goto finished
)
echo Install 64-bit Python 3.13 from https://www.python.org/downloads/windows/
echo Include pip, Tcl/Tk and the Python launcher. Then run this file again.
:finished
echo.
pause
