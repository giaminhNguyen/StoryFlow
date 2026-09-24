@echo off
rem StoryFlow restore wrapper: python -m storyflow restore --from DIR [--db PATH] [--artifact-root DIR] [--force]
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"
set "PYTHONPATH=%ROOT%\backend"

if "%~1"=="" goto usage
if /i "%~1"=="/?" goto usage
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
if not exist "%PY%" (
    echo [restore] backend\.venv is missing. Run scripts\setup.bat first.
    endlocal & exit /b 1
)
echo [restore] SAFETY: stop StoryFlow first ^(Ctrl+C in its window^). A database that is in use is refused.
echo [restore] SAFETY: without --force a non-empty target is refused; with --force the old data is moved to
echo [restore]         "<target>.pre-restore-<timestamp>" ^(never deleted^). Database and artifacts are restored together.
"%PY%" -m storyflow restore %*
set "RC=%errorlevel%"
if "%RC%"=="0" (
    echo [restore] Restore finished. Start the app with start-app.bat.
) else (
    echo [restore] Restore did not complete ^(exit code %RC%^). See the message above; existing data was not overwritten.
)
endlocal & exit /b %RC%

:usage
echo Usage: scripts\restore.bat --from BACKUP_DIR [--db PATH] [--artifact-root DIR] [--force] [--json]
echo.
echo   BACKUP_DIR is a directory produced by scripts\backup.bat ^(contains manifest.json^).
echo   Stop StoryFlow before restoring. --force keeps the replaced data as ^<target^>.pre-restore-^<timestamp^>.
endlocal & exit /b 2
