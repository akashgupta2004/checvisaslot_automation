@echo off
setlocal
cd /d "%~dp0"
python "%~dp0gui.py"
if errorlevel 1 (
  echo.
  echo GUI exited with an error. Check the message above.
  pause
)
