@echo off
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
".venv\Scripts\python.exe" -m sdd_data.delivery_realtime %*
exit /b %errorlevel%
