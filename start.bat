@echo off
rem Runs the music bot + control panel on this PC. Needs Python 3.11+ and FFmpeg.
cd /d "%~dp0"

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg was not found. Install it with:  winget install Gyan.FFmpeg
  echo then close this window and run start.bat again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up Python packages...
  python -m venv .venv
  if errorlevel 1 (
    echo Python was not found. Install Python 3.12 from python.org ^(tick "Add to PATH"^).
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo Installing the Python packages failed - see above.
  pause
  exit /b 1
)

if not defined NO_BROWSER set OPEN_BROWSER=1
echo Control panel: http://localhost:8484/administrator   ^(close this window to stop the bot^)
".venv\Scripts\python.exe" -m musicbot.panel
pause
