@echo off
setlocal

cd /d "%~dp0"

echo ===================================================
echo   Contribution Scheduler Setup and Runner
echo ===================================================
echo.

echo [1/3] Activating virtual environment...
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
    echo Virtual environment activated.
) else (
    echo [WARNING] Virtual environment not found at .venv. 
    echo Will attempt to use global Python...
)

echo.
echo [2/3] Installing requirements...
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Checking Playwright browsers...
playwright install chromium

echo.
echo [3/3] Starting Contribution Scheduler...
python -m src.main %*

echo.
echo Finished.
pause
