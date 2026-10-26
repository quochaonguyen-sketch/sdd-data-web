@echo off
call "%~dp0run_worker.cmd" %*
exit /b %errorlevel%
