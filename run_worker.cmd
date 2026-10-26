@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Không tìm thấy Python. Chạy setup.cmd trước.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m sdd_data.source_background %*
set "SDD_WORKER_RESULT=%errorlevel%"
if not "%SDD_WORKER_RESULT%"=="0" (
  echo.
  echo Khong bat them duoc worker. Xem thong bao ben tren.
  echo Neu worker da chay nen, mo http://127.0.0.1:3000/data-sources
  pause
)
exit /b %SDD_WORKER_RESULT%
