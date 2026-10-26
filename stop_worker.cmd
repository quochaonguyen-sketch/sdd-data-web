@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Không tìm thấy Python. Kiểm tra thư mục .venv hoặc chạy setup.cmd.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m sdd_data.source_worker --stop
set "SDD_STOP_RESULT=%errorlevel%"
echo Worker sẽ ngừng nhận tác vụ và hoàn tất bước ghi đang chạy trước khi tắt.
pause
exit /b %SDD_STOP_RESULT%
