@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please create the development virtual environment first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --name WTVB-Monitor --add-data "%CD%\app\static;app\static" --distpath release --workpath work\build --specpath work\spec launcher.py
if errorlevel 1 pause
