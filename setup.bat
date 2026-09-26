@echo off
rem One-click setup: double-click after cloning. Runs scripts\setup.bat (which asks the few questions it needs).
call "%~dp0scripts\setup.bat" %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
