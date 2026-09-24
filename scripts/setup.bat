@echo off
rem ===========================================================================
rem StoryFlow - reproducible setup (idempotent; safe to re-run).
rem
rem   scripts\setup.bat          full setup
rem   scripts\setup.bat /check   only validate prerequisites, change nothing
rem
rem Steps: prerequisites (python >= 3.11, node >= 20, git) -> bootstrap.py (pinned
rem external sources) -> backend\.venv + pinned requirements -> frontend npm ci +
rem build -> python -m storyflow doctor.  No credentials are read or stored.
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "ROOT=%%~fI"
cd /d "%ROOT%"
set "CHECK="
if /i "%~1"=="/check" set "CHECK=1"
set "VENV=%ROOT%\backend\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYTHONPATH=%ROOT%\backend"

echo [setup] Repository: %ROOT%
if defined CHECK echo [setup] /check mode: validating prerequisites only, nothing will be changed.

rem ---- 1. prerequisites ------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [setup] ERROR: python was not found on PATH. Install Python 3.13 from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
    goto fail
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [setup] ERROR: Python 3.11 or newer is required ^(tested with 3.13^). Found:
    python --version
    goto fail
)
for /f "delims=" %%V in ('python --version') do echo [setup] OK   %%V

where node >nul 2>nul
if errorlevel 1 (
    echo [setup] ERROR: node was not found on PATH. Install Node.js 20 or newer ^(tested with 24^) from https://nodejs.org/.
    goto fail
)
node -e "process.exit(parseInt(process.versions.node.split('.')[0], 10) >= 20 ? 0 : 1)"
if errorlevel 1 (
    echo [setup] ERROR: Node.js 20 or newer is required. Found:
    node --version
    goto fail
)
for /f "delims=" %%V in ('node --version') do echo [setup] OK   node %%V

where git >nul 2>nul
if errorlevel 1 (
    echo [setup] ERROR: git was not found on PATH. Install Git for Windows from https://git-scm.com/download/win.
    goto fail
)
for /f "delims=" %%V in ('git --version') do echo [setup] OK   %%V

if defined CHECK (
    echo.
    echo [setup] /check: prerequisites satisfied. Current state:
    if exist "%PY%" ( echo [setup]   backend\.venv : present ) else ( echo [setup]   backend\.venv : missing ^(created by a full setup^) )
    if exist "%ROOT%\frontend\node_modules" ( echo [setup]   frontend\node_modules : present ) else ( echo [setup]   frontend\node_modules : missing ^(installed by a full setup^) )
    if exist "%ROOT%\frontend\dist\index.html" ( echo [setup]   frontend\dist : built ) else ( echo [setup]   frontend\dist : not built ^(built by a full setup^) )
    python "%ROOT%\bootstrap.py" --check
    if errorlevel 1 ( echo [setup]   pinned external sources : NOT ready ^(fetched by a full setup^) ) else ( echo [setup]   pinned external sources : OK )
    goto ok
)

rem ---- 2. pinned external sources -------------------------------------------
echo.
echo [setup] Fetching/verifying pinned external sources ^(bootstrap.py^)...
python "%ROOT%\bootstrap.py"
if errorlevel 1 (
    echo [setup] ERROR: bootstrap.py failed. Read the message above ^(network access to the pinned git remotes is required
    echo [setup]        the first time; existing local changes in external\ are never reset or overwritten^).
    goto fail
)

rem ---- 3. backend virtual environment ---------------------------------------
echo.
if not exist "%PY%" (
    echo [setup] Creating backend\.venv ...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [setup] ERROR: could not create the virtual environment at backend\.venv.
        goto fail
    )
) else (
    echo [setup] backend\.venv already exists - reusing it.
)
echo [setup] Installing pinned backend requirements ^(backend\requirements-dev.txt^)...
"%PY%" -m pip install -r "%ROOT%\backend\requirements-dev.txt"
if errorlevel 1 (
    echo [setup] ERROR: pip install failed. Check your network connection and retry.
    goto fail
)

rem ---- 4. frontend -----------------------------------------------------------
echo.
echo [setup] Installing frontend dependencies ^(npm ci^) and building the UI...
pushd "%ROOT%\frontend"
call npm ci
if errorlevel 1 (
    popd
    echo [setup] ERROR: npm ci failed. Check your network connection and retry.
    goto fail
)
call npm run build
if errorlevel 1 (
    popd
    echo [setup] ERROR: the frontend build failed. See the messages above.
    goto fail
)
popd

rem ---- 5. doctor --------------------------------------------------------------
echo.
echo [setup] Running the health check ^(python -m storyflow doctor^)...
"%PY%" -m storyflow doctor
set "DOC=!errorlevel!"
if not "!DOC!"=="0" (
    echo [setup] The doctor reported problems ^(exit code !DOC!^). Apply the fixes listed above, then re-run scripts\setup.bat.
    goto fail
)

echo.
echo [setup] Optional: real subtitle provider dependencies ^(only needed for STORYFLOW_SUBTITLE_PROVIDER=external^):
echo [setup]     "%PY%" -m pip install -r "%ROOT%\backend\requirements-subtitle.txt"
echo [setup] Optional: copy backend\.env.example to backend\.env to select real providers ^(see docs\OPERATIONS.md^).
echo [setup] Setup complete. Start the app with:  start-app.bat

:ok
endlocal & exit /b 0

:fail
echo.
echo [setup] Setup did not complete. Fix the problem above and re-run scripts\setup.bat ^(it is safe to repeat^).
endlocal & exit /b 1
