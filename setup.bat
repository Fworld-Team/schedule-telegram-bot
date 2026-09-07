@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
set "VENV_PY=.venv\Scripts\python.exe"

rem Never recreate a working environment over itself with another Python version.
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        echo Using the existing Python virtual environment...
        goto :install_dependencies
    )
)

rem A damaged environment must be moved or removed first; overlaying it can mix
rem binary packages from different Python versions (for example Pillow).
if exist ".venv" (
    echo.
    echo The existing .venv is damaged or incompatible.
    echo Rename or remove it, then run setup.bat again.
    pause
    exit /b 1
)

py -3.12 --version >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3.12"
if not defined PYTHON_CMD (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
    python --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if defined PYTHON_CMD (
    %PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    if errorlevel 1 set "PYTHON_CMD="
)

if not defined PYTHON_CMD (
    echo.
    echo Python 3.10 or newer was not found. Install it from python.org,
    echo enable the Python Launcher, then run setup.bat again.
    pause
    exit /b 1
)

echo Creating Python virtual environment...
%PYTHON_CMD% -m venv .venv
if errorlevel 1 goto :failed

:install_dependencies
echo Installing dependencies...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :failed
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

if not exist "token.txt" copy /Y "token.example.txt" "token.txt" >nul

echo Running tests...
"%VENV_PY%" -m unittest discover -s tests -v
if errorlevel 1 goto :failed

echo.
echo Setup finished. Paste the BotFather token into token.txt, then run run.bat.
pause
exit /b 0

:failed
echo.
echo Setup failed. Read the error above, check your internet connection, and retry.
pause
exit /b 1
