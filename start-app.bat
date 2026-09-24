@echo off
rem ===========================================================================
rem StoryFlow - start the local application (API + embedded runtime + built UI).
rem
rem   start-app.bat            start on http://127.0.0.1:8765 and open the browser
rem   start-app.bat --fake     deterministic offline demo (fake providers)
rem   start-app.bat /check     validate everything, do NOT start the server
rem   start-app.bat --port 9000 --no-open ...   extra arguments are passed to
rem                              "python -m storyflow.api" unchanged
rem
rem First time on a machine: run scripts\setup.bat.  Stop the app with Ctrl+C.
rem Exit codes: 0 ok, 1 a pre-flight check failed, otherwise the server's code.
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"
set "PYTHONPATH=%ROOT%\backend"
set "CHECK="
set "PASS="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="/check" (
    set "CHECK=1"
) else (
    set PASS=!PASS! %1
)
shift
goto parse
:parsed

echo [StoryFlow] Checking pinned external sources...
python "%ROOT%\bootstrap.py" --check
if errorlevel 1 (
    echo.
    echo [StoryFlow] The pinned external sources are missing or differ from sources.lock.json.
    echo [StoryFlow] Run:  scripts\setup.bat
    set "RC=1"
    goto done
)

if not exist "%PY%" (
    echo.
    echo [StoryFlow] The backend virtual environment is missing: backend\.venv
    echo [StoryFlow] Run:  scripts\setup.bat
    set "RC=1"
    goto done
)

echo [StoryFlow] Running startup checks ^(doctor --quick^)...
"%PY%" -m storyflow doctor --quick
if errorlevel 1 (
    echo.
    echo [StoryFlow] Startup checks FAILED. Apply the fixes printed above and retry.
    echo [StoryFlow] Full first-time setup: scripts\setup.bat   ^|   Help: docs\OPERATIONS.md
    set "RC=1"
    goto done
)

if defined CHECK (
    echo [StoryFlow] Checking database migration state ^(dry run, nothing is changed^)...
    "%PY%" -m storyflow migrate --dry-run
) else (
    echo [StoryFlow] Applying the safe migration policy ^(older database =^> verified backup first^)...
    "%PY%" -m storyflow migrate
)
if errorlevel 1 (
    echo.
    echo [StoryFlow] Migration was refused or failed; the database was NOT modified destructively.
    echo [StoryFlow] See the message above and docs\OPERATIONS.md ^(section "Safe migration policy"^).
    set "RC=1"
    goto done
)

if defined CHECK (
    echo.
    echo [StoryFlow] /check: all pre-flight checks passed. The server was not started.
    set "RC=0"
    goto done
)

echo.
echo [StoryFlow] Starting StoryFlow at http://127.0.0.1:8765  ^(Ctrl+C to stop^)
"%PY%" -m storyflow.api --open-browser !PASS!
set "RC=!errorlevel!"
echo.
if "!RC!"=="0" (
    echo [StoryFlow] StoryFlow stopped cleanly. Your data is in runtime\ ^(back it up with scripts\backup.bat^).
) else (
    echo [StoryFlow] StoryFlow exited with code !RC!. Check the messages above and runtime\logs\.
)

:done
endlocal & exit /b %RC%
