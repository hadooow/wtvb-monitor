@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create .venv and install requirements-dev.txt first. See README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\build_release.py
if errorlevel 1 (
  echo Release build failed. Review the output above.
  pause
  exit /b 1
)
echo Release ZIP is in the release folder.
pause
