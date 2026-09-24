@echo off
rem StoryFlow backup wrapper: python -m storyflow backup [--to DIR] [--label TXT] [--no-artifacts]
rem Safe while the app is running (online SQLite backup; database first, then artifacts).
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"
set "PYTHONPATH=%ROOT%\backend"

if /i "%~1"=="/?" goto usage
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
if not exist "%PY%" (
    echo [backup] backend\.venv is missing. Run scripts\setup.bat first.
    endlocal & exit /b 1
)
"%PY%" -m storyflow backup %*
set "RC=%errorlevel%"
if not "%RC%"=="0" echo [backup] Backup did not complete ^(exit code %RC%^). Nothing was modified in runtime\.
endlocal & exit /b %RC%

:usage
echo Usage: scripts\backup.bat [--to DIR] [--label TEXT] [--no-artifacts] [--json]
echo.
echo   Creates a verified backup of the database and artifacts ^(default: runtime\backups\^).
echo   Safe to run while StoryFlow is running. Restore with scripts\restore.bat.
endlocal & exit /b 0
