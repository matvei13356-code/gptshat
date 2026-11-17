@echo off
cd /d "%~dp0"
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
  echo Install Python 3.11 or newer from python.org with Python Launcher.
  pause
  exit /b 1
)
py -3 -m venv .venv
if errorlevel 1 goto :fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :fail
echo Ready. Read README_RU.md, then run run_sample.bat.
pause
exit /b 0
:fail
echo Installation failed. See the error above.
pause
exit /b 1
