@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
set "SDD_BOOTSTRAP=%~dp0..\spx_py_laucher\.venv\Scripts\python.exe"
if exist "%SDD_BOOTSTRAP%" (
  "%SDD_BOOTSTRAP%" -m venv .venv
) else (
  py -3 -m venv .venv
)
if errorlevel 1 goto failed
:install
if exist requirements.lock.txt (
  ".venv\Scripts\python.exe" -m pip install -r requirements.lock.txt
) else (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
if errorlevel 1 goto failed
if not exist .env copy /y .env.example .env >nul
echo Cai dat hoan tat. Chay run.cmd de mo launcher.
pause
exit /b 0
:failed
echo Cai dat that bai. Can Python 3.11 tro len, co pip va tkinter.
pause
exit /b 1
