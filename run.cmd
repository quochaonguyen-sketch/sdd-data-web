@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
set "SDD_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%SDD_PYTHON%" goto execute
set "SDD_PYTHON=%~dp0..\spx_py_laucher\.venv\Scripts\python.exe"
if exist "%SDD_PYTHON%" goto execute
echo Chua co Python cho project. Hay chay setup.cmd truoc.
if "%~1"=="" pause
exit /b 1
:execute
"%SDD_PYTHON%" "%~dp0launcher.py" %*
set "SDD_RESULT=%errorlevel%"
if "%~1"=="" if not "%SDD_RESULT%"=="0" pause
exit /b %SDD_RESULT%
