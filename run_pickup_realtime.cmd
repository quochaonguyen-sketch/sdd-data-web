@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m sdd_data.pickup_realtime %*
