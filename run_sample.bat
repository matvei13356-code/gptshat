@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe parser.py --sample %*
set "parser_exit=%errorlevel%"
pause
exit /b %parser_exit%
